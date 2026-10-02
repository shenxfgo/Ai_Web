"""统一错误 envelope：{error:{code,message,detail}}，HTTP 状态码由异常类决定。"""

from __future__ import annotations

from typing import Any, Final

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError


class AppError(Exception):
    code: str = "internal_error"
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR

    def __init__(self, message: str = "", detail: Any = None) -> None:
        self.message = message or self.__class__.__doc__ or self.code
        self.detail = detail
        super().__init__(self.message)


class NotFound(AppError):
    code = "not_found"
    status_code = status.HTTP_404_NOT_FOUND


class Forbidden(AppError):
    code = "forbidden"
    status_code = status.HTTP_403_FORBIDDEN


class Unauthorized(AppError):
    code = "unauthorized"
    status_code = status.HTTP_401_UNAUTHORIZED


class Conflict(AppError):
    code = "conflict"
    status_code = status.HTTP_409_CONFLICT


class InvalidRequest(AppError):
    code = "invalid_request"
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY


class SourceUnreachable(AppError):
    """连不上源库、或者连上了但源库说"你不该连"。

    单列一档而不是塞进 500：这是用户填错一格就能自救的情况，前端要把它显示成表单错误
    而不是"服务异常"。400 而不是 422——422 归请求体格式，这里是请求合法、目标不对。
    """

    code = "source_unreachable"
    status_code = status.HTTP_400_BAD_REQUEST


class NotImplementedSource(AppError):
    """功能对某种 kind 还没做（区别于"这是 bug"）。"""

    code = "not_implemented"
    status_code = status.HTTP_501_NOT_IMPLEMENTED


class SyncAlreadyRunning(Conflict):
    """同一个数据源已有一个未结束的 `sync_jobs`（metadata-model §6 的互斥）。

    互斥由 `ux_sync_running` 这个部分唯一索引在库里保证，不是代码里查一次再插入——
    两个请求同时进来时"先查后插"两个都查不到。单列一档而不是裸 `Conflict`：
    前端要能只对"再点一次就好"这一种情况显示"同步进行中"而不是"保存失败"。
    """

    code = "sync_already_running"


class ExtractScopeTooLarge(AppError):
    """抽取范围内的表数超过 `AIWEB_EXTRACT__MAX_TABLES`（metadata-model §6）。

    和 source_unreachable 同一个理由用 400：请求本身合法，是目标太大，用户改一格配置就能自救。
    detail 里带三个出路，是 §6 点名要求结构化返回的东西，不是我们多加的礼貌。
    """

    code = "extract_scope_too_large"
    status_code = status.HTTP_400_BAD_REQUEST


class QueryTimeout(AppError):
    """源库执行超过超时上限被中断（工单 011 验收 ②，safety §4.1/§4.3）。

    单列一档而不是塞进 500：用户能把问句改小、或把 timeout_ms 调大来自救，前端要显示成
    "查询超时、该改哪一格"而不是"服务异常"。detail 里点名**上限来自哪个配置项**——
    是数据源级 `timeout_ms`（L4）还是全局 `AIWEB_QUERY__TIMEOUT_MS`，因为这两格改的方法不同。
    """

    code = "query_timeout"
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR


class ReadonlyCapabilityMissing(AppError):
    """执行前 `SHOW GRANTS` 探到源账号不是只读账号，硬阻断（工单 011 拍板，safety §4.1）。

    与数据源登记时的"黄色警告不阻断回 200"分档（见 datasource_service.grants_verdict 的口径）：
    登记只是提示，真正要跑 SQL 了这一层必须拦下来——一个能写的账号一旦进了执行链路，
    三层防御里最外面的会话只读就可能被 `SET SESSION ... READ WRITE` 之类绕掉。403 而不是 400：
    请求本身没错，是这条链路被授权层拒绝。
    """

    code = "readonly_capability_missing"
    status_code = status.HTTP_403_FORBIDDEN


class ResultFileUnavailable(AppError):
    """下载侧拿不到结果文件时**唯一**的那一档（工单 013，safety §7）。

    "文件不存在""名字越界""realpath 逃到目录外"必须落成同一个 code 和同一句话：分了档就等于
    给探测者一个可数的 oracle——"这个名字在这台机上存在过"是从 404 换成 403 就能问出来的。
    404 而不是 403，正是 §7 那句"不区分'不存在'与'无权限'"的字面要求：403 本身就在宣称
    "东西在这儿，只是不给你"。归属判定（这条结果是不是这个用户的）归 P8 的端点，但它查出
    "不是你的"时也必须落到**这一档**，不许换成 `Forbidden`——那等于用状态码把"文件存在"漏出去。
    """

    code = "result_file_unavailable"
    status_code = status.HTTP_404_NOT_FOUND


class ReindexEmbeddingMissing(AppError):
    """四键没配齐时 `POST /api/kb/reindex` 的那一档（工单 029 验收 8，spec 故事 16）。

    400 而不是 503：请求本身合法，是这台机器此刻没端点可用，用户改一格 `.env` 就能自救——
    与 `source_unreachable` 同一条理由。区别于 `services/embedding_client.py` 里那个
    同名事实的 `EmbeddingNotConfigured`（503，作业跑起来才发现没配）：那一档的主语是"客户端
    被使用时没配置"，这里的主语是"这个请求不该被受理"。共用一个 code 字符串是刻意的——
    两件事实是同一件（这台机器没配向量端点），分两个 code 只会让前端为同一句话写两个分支。

    detail 必须把**两条出路**都点名（故事 16 的字面）：配真端点，或者起闸内替身。
    """

    code = "embedding_not_configured"
    status_code = status.HTTP_400_BAD_REQUEST


class ReindexMatchesActive(Conflict):
    """要建的那套三元组与库里 `status='active'` 那行**同名**（工单 029 撞名口径的补集）。

    为什么不复用那一行：档案名是三元组拼的（§2.6），同名就是同一套基准；reindex 的作业会往
    这套基准的卡上逐张改向量，于是 spec 故事 3 那句"重建中不影响线上检索"当场为假。
    而"同一套三元组的新基准"本来就不存在——`uq_kb_index_profile_name` 说的正是这件事。

    出路有两条且都不是 reindex：想换基准就改模型/维度（名字跟着变），想给现有卡片补向量就
    跑一次普通同步（决策 15：四键配齐且三元组与 active 一致时，普通同步会把 embed 档真写完）。
    """

    code = "reindex_matches_active_profile"


class ProfileNotBuilt(AppError):
    """激活一套**还在建**的档案：`draft` 且 `built_at IS NULL`。

    工单 030 验收 4 / spec 故事 6。
    为什么单独立一档而不用 `InvalidRequest`（422）：这一格的事实是"现在还不行，等它跑完就行"，
    422 的主语是"请求体本身写错了"。400 与 `embedding_not_configured` 同一条理由——请求合法，
    是这台机器此刻的状态不支持，用户做一个动作（等作业终局，或去看那颗作业的 `errors[]`）就能自救。

    主语是 draft 不是"任何非 active"：`NULL` 在 draft 身上有两种读法（还在跑 / 跑崩了），两种都
    不该上线；在 retired 身上它还有第三种读法——0008 归置出来的 P2/P3 历史行从来没有作业，所以
    迁移不碰这一列。那一档正是回滚要指回去的基准，把它一起拦掉等于取消"可回滚"（spec 故事 6
    后半句"`retired` 的档案可以再激活"要的就是这一格）。
    """

    code = "profile_not_built"
    status_code = status.HTTP_400_BAD_REQUEST


class ProfileCardsIncomplete(AppError):
    """要上线的那套档案卡片行数不齐（工单 030 验收 5 / spec 决策 9 的"整档案激活"）。

    为什么必须是**整档案**的尺而不是逐源：`kb_index_profile` 是全局的（档案名里没有 datasource，
    §2.6），而 `POST /api/kb/reindex` 是按源的一颗作业。于是"draft 建好了"这句话只对它跑过的那个源
    成立——直接切过去，别的源会在向量路里**静默缺席**（返回的结果里根本没有那个源的表，也不报错）。
    这是 `docs/architecture.md` §5.2 (1) 列的最坏失效形状，所以门槛在这里，不在检索侧。

    detail 必须点名**那个源的全名**并给出应有/实有两个数：多源场景下要做的动作是"给那个源再跑一次
    reindex"，只回"行数不齐"等于把定位的活推给看的人。

    为什么是 400 不用 409：与 `profile_not_built` 同一条理由——请求本身合法，是这台机器此刻的状态
    不支持，用户跑一次作业就能自救。区别于 `reindex_matches_active_profile`（409）：那一档说的是
    "这个请求不该受理"（重建的就是线上那套），这一档说的是"这套档案还没准备好"。
    """

    code = "profile_cards_incomplete"
    status_code = status.HTTP_400_BAD_REQUEST


class VectorColumnMissing(AppError):
    """档案那一维在 `kb_card` 上**没有预置的向量列**（工单 029 的 embed 档）。

    为什么要单独一档：`core/vector_columns.py` 抬的是 `ValueError`，而 `sync_jobs.errors[]`
    只收得到"分过类的"错误——非 `AppError` 会被 `_error_entry` 兜成 `sync_failed`，再经
    `describe_source_error` 翻成"源库返回了无法识别的错误，请检查连接参数"。那句话的主语是
    错的：该改的是**元数据库的列**（加一条迁移）或档案的维度（换 model），源库什么都没做错。

    真会走到：`AIWEB_EMBEDDING__DIMENSION` 允许 1~2000 里任何值，而列只有迁移建过的那两维。
    配置写个没预置的宽度再跑 reindex，这条路今天就有。
    """

    code = "vector_column_missing"
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR


# §6 的三条出路。放在错误类旁边而不是抛出点：编排层（sync_service）与测试桩都要引用同一份，
# 于是"编排层把 detail 换成别的词"会当场红，而不是前端拿到任意三句话照样绿。
# 第三条是 §6 原文（工单 021 换回：P2 那句"admin 改配置"是开关未落地时的替代话，不许撒谎）。
# 已知别扭：实装形状是请求体 `{"force":true}`（architecture §7），而 §6 原文字面是
# `?force=true`；三处字面（§6 表 / 本常量 / test_extract_mysql_client.py）必须逐字一致，
# 措辞统一与否归 025 收口——这里不为好看开第二条入口（查询参数与请求体并存是禁止的）。
SCOPE_REMEDIES: Final = (
    "配 include_tables 白名单",
    "只同步部分 schema（include_schemas）",
    "admin 用 ?force=true 覆盖上限",
)


def error_body(code: str, message: str, detail: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "detail": detail}}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            error_body(exc.code, exc.message, exc.detail), status_code=exc.status_code
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 只回定位用的三件套，不回 exc.errors() 的整条：pydantic 的 `input` 装的是**整个**
        # 请求体，哪怕错的是 port 那一格，同一份 body 里的 connect_password 也会跟着回显出去。
        # 输出模型白名单只挡成功路径，挡不住这里。
        detail = [
            {"loc": list(err.get("loc", ())), "type": err.get("type"), "msg": err.get("msg")}
            for err in exc.errors()
        ]
        return JSONResponse(
            error_body("invalid_request", "请求参数不合法", detail),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    @app.exception_handler(SQLAlchemyError)
    async def _db_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        # 不把驱动原文回给前端，避免泄露连接串与库内对象名
        from app.core.logging import get_logger

        # stdlib logger 不认任意关键字参数：写成 path=... 会让这个处理器自己抛
        # TypeError，用户拿到没有 envelope 的裸 500。
        get_logger(__name__).exception("数据库访问失败 path=%s", request.url.path, exc_info=exc)
        return JSONResponse(
            error_body("database_error", "数据访问失败"),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
