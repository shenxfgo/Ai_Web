"""`POST /api/chat/validate` 的服务半边（工单 039，safety §8.4）。

这一层的存在理由只有一个：**白名单的来源**。ask 那一路的 `allowed` 来自检索结果
（`pipeline.py` 把 `load_schema_tables` 给的 `full_name` 逐枚 `QualifiedTable.parse`），
而 validate 没有问句、因此没有检索——它必须另找一个来源，且这个来源与 ask 那一路
在"哪些表今天算存在"这一格上不能给出两个答案。

守卫本身**复用不复制**：这里调的是同一支 `check()`，参数口径与 `pipeline.py` 那一句逐项对齐
（`default_schema=""`、`max_rows=resolve_row_limit(ds)`、`dialect=ds.kind`）。
本片给守卫加的是**调用方**，不是规则。

**只跑第一层**（口径第 3 条）：不连源库、不 dry-run、不执行、不落 `chat_*`。所以这个模块里
不该出现任何连接路径上的 import。`resolve_row_limit` 看着像例外，其实不是——它是 `executor`
里那枚不碰连接的纯判定（L4 覆盖 L3），validate 复用的是**那把尺**，与 ask 钳位钳到同一个数。
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import SqlGuardRejected
from app.core.table_names import table_full_name
from app.models.meta import MetaTable
from app.models.user import User
from app.schemas.chat import ChatValidateOut, ChatValidateRequest
from app.services import datasource_service
from app.services.nl2sql.executor import resolve_row_limit
from app.services.sql_guard import Dialect, QualifiedTable, SqlGuardError, check


async def allowed_from_source(
    session: AsyncSession, *, datasource_id: int
) -> frozenset[QualifiedTable]:
    """该源**今天还存在的表**的白名单——validate 那一路的 `allowed`。

    尺子是 `is_stale = false` 这一条，而且**只有**这一条：它与
    `kb_service.required_card_count_stmt()` 是同一把尺（那里读的是"该源应建卡片的表数"）。
    ask 那一路的候选来自卡片，陈旧表本轮不建卡，
    所以它看不见陈旧表；validate 若把 `is_stale=true` 也收进来，同一件引用陈旧表的 SQL 就会
    **validate 放行、ask 拒**——验收 5 要防的是"两条路给不同判定"，而它的成因除了拼法漂移
    还有这种集合口径漂移，后者更隐蔽。

    `is_hidden` 不减：它是人工列（metadata-model §3），隐藏表照样出卡、照样能被合法引用，
    把它当过滤器等于用 UI 可见性冒充权限（权限收紧归 P6 的 grants，见 safety §5）。

    名字**不在这里拼**：三列规范化名交给 `core/table_names.table_full_name`，再走
    `QualifiedTable.parse` 归一（小写、空的那几格不占位）。这两步的**顺序与内容**就是 ask
    那一路的顺序，共用一枚拼法函数不等于两条路自动相等——所以它另有一条用例在两路之间比
    （`test_chat_validate_pg.py::test_validate构造的白名单与检索那一路逐枚相等`）。
    """
    rows = await session.execute(
        select(MetaTable.catalog_name, MetaTable.schema_name, MetaTable.table_name).where(
            MetaTable.datasource_id == datasource_id, MetaTable.is_stale.is_(False)
        )
    )
    return frozenset(
        QualifiedTable.parse(table_full_name(catalog, schema, table))
        for catalog, schema, table in rows
    )


async def validate_sql(
    session: AsyncSession, *, actor: User, payload: ChatValidateRequest
) -> ChatValidateOut:
    """判一句话，把守卫的那一档翻成 HTTP 的那一档。

    顺序是有意义的：先 `get_authorized`（404/403 都在它里面出）再查白名单。反过来等于让无权者
    拿"这一发慢不慢"去探那个源有多少张表；而 `get_authorized` 还代劳了软删那一档——软删算 404
    不算 403，回 403 等于替调用者确认"这个 id 下曾有过一个源"。

    `SqlGuardError` → `SqlGuardRejected` 的**换名只做在 HTTP 这一路**（core 不 import services，
    所以映射不能住在 `AppError` 那一侧）：对外的 `rule_id` 的主人是 `Violation.code`，对外的
    `field` 的主人是 `Violation.node`。**这一句不覆盖另一条外出口**：同一批 `Violation` 落进
    `chat_messages.guard_result` 时那一格叫 `code`（012 的形状），落进 `retries[]` 时又叫
    `rule_id`（038 在 pipeline 那一侧另起的名）——三个对外键名、两处主人，收拢它要动 012/038
    的面，账在 041，细节写在 safety §8.4 的 as-built ②。`detail` 里再放一份 `message` 不是冗余
    ——那三格合起来是"守卫看到的那一条违规"的完整快照，只回一个码就得再去查表才知道它说什么。
    """
    ds, _tier = await datasource_service.get_authorized(session, actor, payload.datasource_id)
    allowed = await allowed_from_source(session, datasource_id=ds.id)
    try:
        guarded = check(
            payload.sql,
            allowed=allowed,
            dialect=cast("Dialect", ds.kind),
            default_schema="",
            max_rows=resolve_row_limit(ds),
        )
    except SqlGuardError as exc:
        raise SqlGuardRejected(
            exc.message,
            detail={
                "rule_id": str(exc.rule_id),
                "field": exc.violation.node,
                "message": exc.message,
            },
        ) from exc
    # 到不了 `sql_final is None` 那一支：`check()` 在非 ok 时必定已抛（它与 `guard()` 共用一套规则，
    # 只是命中首条即抛）。这里的 cast 是给类型桩收的尾，不是给运行时兜的底。
    return ChatValidateOut(
        ok=True,
        sql_final=cast("str", guarded.sql_final),
        tables=[table_full_name(t.catalog, t.db, t.name) for t in guarded.tables],
    )
