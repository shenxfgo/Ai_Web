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


# §6 的三条出路。放在错误类旁边而不是抛出点：编排层（sync_service）与测试桩都要引用同一份，
# 于是"编排层把 detail 换成别的词"会当场红，而不是前端拿到任意三句话照样绿。
# 第三条 §6 原文是"admin 用 ?force=true 覆盖上限"——那个开关 P3 才有，现在写上去就是撒谎，
# 所以改成今天真能走的路（改配置）。P3 接上 ?force 时再把文案换回原文。
SCOPE_REMEDIES: Final = (
    "配 include_tables 白名单",
    "只同步部分 schema（include_schemas）",
    "调高 AIWEB_EXTRACT__MAX_TABLES 上限（admin 改配置）",
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
