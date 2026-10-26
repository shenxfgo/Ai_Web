"""`meta_table` 的三列 → 表全名那一次拼接，全仓**唯一**的主人（工单 039 让它出生）。

纪律与 `core/sync_vocabulary.stage_of`、`core/vector_columns.vector_column_for` 同一条：
一件事一个主人，两处都用得上就住在 `core/`（`core` 不许反向 import `services`）。
这一份除了 `from __future__ import annotations` 之外**什么都不 import**——它是纯字符串拼接，
没有比这更干净的落点了。

为什么这一片非要给它建主人：`sql_guard.check(allowed=...)` 收的那批 `QualifiedTable` 有两个出生地。
ask 那一路由检索结果喂（`retriever.load_schema_tables` 把卡片行的三列拼成 `full_name`，
`pipeline` 再 `QualifiedTable.parse` 回三元组），validate 这一路由 `meta_table` 直接喂。
"catalog 是空串时不能打头"这条规则在施工前实物是**三处各写一遍**
（`kb_service._full_name`、`join_graph._full_name`、`retriever` 里的一件 inline 复制，
三处 docstring 还互相点名"同一条规则"）。留两份的代价不是抽象的：同一件 SQL 在 ask 被
`table_not_allowed` 拒、在 validate 放行（或反之），而"校验的结果和真实问数一模一样"是
`POST /api/chat/validate` 存在的全部理由——它是守卫的第一个 HTTP 调用方，两份拼法在它出生这天
就会变成用户看得见的矛盾。

签名收 `str`，而 `join_graph` 那两个调用点传的是 `meta_table` 那三列的**行值**
（`row["from_catalog"]` 这类）：三列在 ORM 里都是 `Text, nullable=False`
（`catalog_name` 还带 `server_default ''`，metadata-model §1），所以取值只会是 `str` 或空串，
不会是 `None`。旧 `join_graph._full_name` 收 `object` 并多一层 `str(part)`，那一层对幸存
下来的 `str` 是恒等（`if part` 先一步把空串筛掉），删掉它与三处旧实物**逐字同果**；
它能差别输出的输入（非字符串）在这三列的取值域里不存在。
"""

from __future__ import annotations


def table_full_name(catalog_name: str, schema_name: str, table_name: str) -> str:
    """三列规范化名 → 一个点分全名，空的那几格不占位。

    MySQL 的 `catalog_name` 恒为空串（`docs/metadata-model.md` §1：MySQL 走 `('', <db>)`、
    PG 走 `(<db>, <schema>)`），无条件拼 `.` 会端出 `.ai_web_demo.order_main` 那种带点开头的名字，
    而模型会把它原样抄进 SQL——那条表名在源库里根本不存在。
    """
    return ".".join(part for part in (catalog_name, schema_name, table_name) if part)
