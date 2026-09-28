"""`meta_*` 行 → `TableMeta`：卡片生成前的那半步映射。

原料**只来自元数据库**，不是抽取层的 `Raw*`：中文注释落在 `meta_table.comment_zh`/
`meta_column.comment_zh`，而那是人工列（§3：同步永不覆盖）。从 `Raw*` 生成卡片等于
让用户补录的中文永远进不了 prompt——而 §5 设计要点第 2 条把中文注释当成 NL2SQL 的主要线索。

期望值口径：metadata-model §2.4 的列名 + kb-workflow §5 模板的变量名。
"""

from __future__ import annotations

from app.services.kb_service import TableMeta, table_meta_from_rows

# meta_table 的一行（只写映射会用到的列；其余列存在与否不该影响这半步）
_TABLE = {
    "id": 10,
    "table_uid": "a" * 32,
    "catalog_name": "",
    "schema_name": "ai_web_demo",
    "table_name": "order_main",
    "table_type": "BASE TABLE",
    "comment_raw": "Order header",
    "comment_zh": "订单主表",
    "granularity": None,
    "approx_rows": 12345,
    "last_analyze_at": None,
}

_COLUMNS = (
    {
        "column_name": "id",
        "data_type": "bigint",
        "nullable": False,
        "is_primary_key": True,
        "is_unique": True,
        "comment_raw": None,
        "comment_zh": "订单ID",
        "default_value": None,
        "enum_values": None,
    },
    {
        "column_name": "status",
        "data_type": "enum",
        "nullable": False,
        "is_primary_key": False,
        "is_unique": False,
        "comment_raw": "order status",
        "comment_zh": None,
        "default_value": "pending",
        "enum_values": ["pending", "paid"],
    },
)


def _meta(**override: object) -> TableMeta:
    kwargs: dict[str, object] = {
        "dialect_name": "mysql",
        "server_version": "5.7.44-log",
        "table": _TABLE,
        "columns": _COLUMNS,
        "indexes": (),
        "relations": (),
    }
    kwargs.update(override)
    return table_meta_from_rows(**kwargs)


def test_表头映射_全名是schema点表名_中文注释优先() -> None:
    """§5 的 `【表】ai_web_demo.order_main` / `【说明】订单主表`。

    MySQL 侧 catalog 恒空串（§1 的规范化列），所以全名不能拿 catalog 打头——
    渲染出 `.ai_web_demo.order_main` 的话，模型抄进 SQL 的表名就是错的。
    """
    table = _meta()

    assert table.full_name == "ai_web_demo.order_main"
    assert table.comment_zh == "订单主表"
    assert table.comment_raw == "Order header"
    assert table.approx_rows == 12345
    assert table.table_type == "BASE TABLE"


def test_方言与主版本从源库版本串里拆出来() -> None:
    """§5 的 `【方言】mysql 5.7 — 不支持 CTE...` 要的是主版本，不是整串版本。

    `data_sources.server_version` 存的是 `5.7.44-log` 这种原文；模板的比较条件是
    `server_major=='5.7'`，所以拆分必须在这半步做完。拆错（拿整串去比）的结果是
    方言约束永远不出现——MySQL 5.7 的库照样被喂 CTE 写法。
    """
    assert _meta(server_version="5.7.44-log").server_major == "5.7"
    assert _meta(server_version="8.0.36").server_major == "8.0"
    assert _meta(server_version="").server_major == ""


def test_列逐字段映射_中文注释缺失时留null让模板降级() -> None:
    """`comment_zh or comment_raw or '（无注释）'` 的三级降级是**模板**的事（§5）。

    所以这一步不能替它兜底：`status` 没有中文注释时必须交 `None`，让模板去吃
    `comment_raw="order status"`；若这里就填成"（无注释）"，那行英文字面注释就丢了。
    """
    table = _meta()

    assert [c.name for c in table.columns] == ["id", "status"]
    assert table.columns[0].is_pk is True
    assert table.columns[0].is_unique is True
    assert table.columns[1].comment_zh is None
    assert table.columns[1].comment_raw == "order status"
    # 工单 008 口径修正 ①：取值只来自 meta_column.enum_values，这一步不新增也不丢弃
    assert table.columns[1].enum_values == ("pending", "paid")
    assert table.columns[0].enum_values == ()
    assert table.columns[1].default == "pending"


def test_索引按名分组并按seq拼列_前缀长度取该索引的第一个非空值() -> None:
    """§5 的 `【索引】- idx_tags（FULLTEXT）: tags 前缀 100`。

    `meta_index` 与 `meta_index_column` 是父子两张行，模板要的是"一条索引一行、
    列按顺序拼"，分组只能在这里做。`sub_part` 在父子两层都有列，文档只点名前缀长度，
    取子表值。
    """
    table = _meta(
        indexes=(
            {
                "id": 1,
                "index_name": "PRIMARY",
                "index_type": "BTREE",
                "is_unique": True,
            },
            {
                "id": 2,
                "index_name": "idx_user_status",
                "index_type": "BTREE",
                "is_unique": False,
            },
        ),
        index_columns=(
            {"index_id": 2, "seq_in_index": 2, "column_name": "status", "sub_part": None},
            {"index_id": 2, "seq_in_index": 1, "column_name": "user_id", "sub_part": None},
            {"index_id": 1, "seq_in_index": 1, "column_name": "id", "sub_part": None},
        ),
    )

    assert [(i.name, i.columns, i.unique) for i in table.indexes] == [
        ("PRIMARY", ("id",), True),
        ("idx_user_status", ("user_id", "status"), False),
    ]


def test_关系带上目标表全名_推断边带置信度() -> None:
    """§5 的 `【可关联】- user_id → ai_web_demo.user.id [推断,置信 0.7]`。

    目标表的库名必须一起查出来：只给表名的话，跨库时模型会把目标表写进当前库的
    FROM 里，而那条 SQL 在它自己的库里根本不存在。
    """
    table = _meta(
        relations=(
            {
                "from_column_name": "user_id",
                "source_kind": "extracted",
                "confidence": None,
                "to_schema_name": "ai_web_demo",
                "to_catalog_name": "",
                "to_table_name": "user",
                "to_column_name": "id",
            },
            {
                "from_column_name": "sku_code",
                "source_kind": "inferred",
                "confidence": 0.7,
                "to_schema_name": "ai_web_demo",
                "to_catalog_name": "",
                "to_table_name": "product",
                "to_column_name": "code",
            },
        )
    )

    assert table.relations[0].to_table_full == "ai_web_demo.user"
    assert table.relations[0].kind == "extracted"
    assert table.relations[1].kind == "inferred"
    assert table.relations[1].confidence == 0.7


def test_视图与表同构地映射() -> None:
    """§6 末行：视图卡与表卡同构，靠 `table_type` 区分（降权属 meta，见 `_meta`）。"""
    table = _meta(table={**_TABLE, "table_type": "VIEW", "approx_rows": None})

    assert table.table_type == "VIEW"
    assert table.approx_rows is None
