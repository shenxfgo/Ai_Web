"""`information_schema` 的行 → `Raw*` 的映射（纯函数，不连库）。

期望值口径：
- `docs/metadata-model.md` §7：`Raw*` 的字段语义（跨方言归一后的形状）
- 同文 §8.1：这五条 SQL 的列顺序与取值（`IS_NULLABLE='YES'`、`(NON_UNIQUE=0)` 这类表达式
  在驱动里回来的形状是实测过的：5.7 的布尔表达式回 int 0/1
- 同文 §9：`enum('a','b')` 要拆成值清单，中文枚举值直接决定 where 能否命中
- 同文 §10.1：5.7 的 IS 中文注释可能整片回 `???`，占比 >0.3 要打 `CHARSET_SUSPECT`
"""

from __future__ import annotations

from app.extractor.mysql import (
    apply_index_flags,
    comment_charset_suspect,
    rows_to_columns,
    rows_to_fks,
    rows_to_indexes,
    rows_to_tables,
)


def test_columns_把_is_的是_否_与_enum_定义翻成归一形状() -> None:
    rows = [
        {
            "TABLE_NAME": "product",
            "COLUMN_NAME": "status",
            "ORDINAL_POSITION": 3,
            "DATA_TYPE": "enum",
            "COLUMN_TYPE": "enum('on_sale','off_sale','draft')",
            "IS_NULLABLE": "NO",
            "COLUMN_DEFAULT": None,
            "EXTRA": "",
            "COLUMN_COMMENT": "上下架状态",
            "CHARACTER_MAXIMUM_LENGTH": None,
            "NUMERIC_PRECISION": None,
            "NUMERIC_SCALE": None,
            "COLLATION_NAME": "utf8mb4_general_ci",
            "enum_def": "enum('on_sale','off_sale','draft')",
        },
        {
            "TABLE_NAME": "product",
            "COLUMN_NAME": "id",
            "ORDINAL_POSITION": 1,
            "DATA_TYPE": "bigint",
            "COLUMN_TYPE": "bigint(20)",
            "IS_NULLABLE": "NO",
            "COLUMN_DEFAULT": None,
            "EXTRA": "auto_increment",
            "COLUMN_COMMENT": "",
            "CHARACTER_MAXIMUM_LENGTH": None,
            "NUMERIC_PRECISION": 19,
            "NUMERIC_SCALE": 0,
            "COLLATION_NAME": None,
            "enum_def": None,
        },
    ]
    cols = rows_to_columns(rows)
    status, id_col = cols
    assert status.enum_values == ("on_sale", "off_sale", "draft")
    assert status.nullable is False and id_col.nullable is False
    assert status.data_type == "enum"
    assert status.raw_data_type == "enum('on_sale','off_sale','draft')"
    assert status.ordinal_position == 3
    # §7：空注释归一成 None，而不是空串——008 的卡片模板靠 None 判"这列没注释"
    assert id_col.comment is None
    # EXTRA=auto_increment 不是生成列：`generated` 只认 GENERATED/VIRTUAL STORED
    assert id_col.generated is False and status.generated is False
    assert id_col.num_precision == 19 and id_col.num_scale == 0


def test_columns_认得生成列与_set_值清单() -> None:
    rows = [
        {
            "TABLE_NAME": "t",
            "COLUMN_NAME": "full_name",
            "ORDINAL_POSITION": 1,
            "DATA_TYPE": "varchar",
            "COLUMN_TYPE": "varchar(200)",
            "IS_NULLABLE": "YES",
            "COLUMN_DEFAULT": None,
            "EXTRA": "VIRTUAL GENERATED",
            "COLUMN_COMMENT": "全名",
            "CHARACTER_MAXIMUM_LENGTH": 200,
            "NUMERIC_PRECISION": None,
            "NUMERIC_SCALE": None,
            "COLLATION_NAME": "utf8mb4_general_ci",
            "enum_def": None,
        },
        {
            "TABLE_NAME": "t",
            "COLUMN_NAME": "tags",
            "ORDINAL_POSITION": 2,
            "DATA_TYPE": "set",
            "COLUMN_TYPE": "set('a','b')",
            "IS_NULLABLE": "YES",
            "COLUMN_DEFAULT": "a",
            "EXTRA": "",
            "COLUMN_COMMENT": None,
            "CHARACTER_MAXIMUM_LENGTH": None,
            "NUMERIC_PRECISION": None,
            "NUMERIC_SCALE": None,
            "COLLATION_NAME": "utf8mb4_general_ci",
            "enum_def": "set('a','b')",
        },
    ]
    gen, tags = rows_to_columns(rows)
    assert gen.generated is True and gen.char_length == 200
    assert tags.enum_values == ("a", "b")
    assert tags.default == "a"


def test_tables_保留视图并带大小与引擎() -> None:
    rows = [
        {
            "TABLE_SCHEMA": "ai_web_demo",
            "TABLE_NAME": "v_daily_sales",
            "table_type": "VIEW",
            "TABLE_COMMENT": "",
            "ENGINE": None,
            "ROW_FORMAT": None,
            "TABLE_COLLATION": None,
            "TABLE_ROWS": None,
            "DATA_LENGTH": None,
            "INDEX_LENGTH": None,
            "CREATE_TIME": None,
            "UPDATE_TIME": None,
        },
        {
            "TABLE_SCHEMA": "ai_web_demo",
            "TABLE_NAME": "order_main",
            "table_type": "BASE TABLE",
            "TABLE_COMMENT": "订单主表（一笔订单一行）",
            "ENGINE": "InnoDB",
            "ROW_FORMAT": "Dynamic",
            "TABLE_COLLATION": "utf8mb4_general_ci",
            "TABLE_ROWS": 30000,
            "DATA_LENGTH": 1638400,
            "INDEX_LENGTH": 49152,
            "CREATE_TIME": None,
            "UPDATE_TIME": None,
        },
    ]
    tables = rows_to_tables(rows)
    assert [t.table_type for t in tables] == ["VIEW", "BASE TABLE"], "视图不能丢（卡片走降级模板）"
    order = tables[1]
    assert order.comment == "订单主表（一笔订单一行）"
    assert (order.approx_rows, order.data_bytes, order.index_bytes) == (30000, 1638400, 49152)
    assert order.schema_name == "ai_web_demo"
    # §1：MySQL 的 catalog 恒空串
    assert order.catalog_name == ""


def test_indexes_按索引分组并保住列序与前缀长度() -> None:
    rows = [
        {
            "TABLE_NAME": "order_main",
            "INDEX_NAME": "PRIMARY",
            "is_unique": 1,
            "is_primary": 1,
            "INDEX_TYPE": "BTREE",
            "NULLABLE": "",
            "COLUMN_NAME": "id",
            "SEQ_IN_INDEX": 1,
            "CARDINALITY": 29874,
            "SUB_PART": None,
            "COLLATION": "A",
            "index_comment": "",
        },
        {
            "TABLE_NAME": "order_main",
            "INDEX_NAME": "idx_cust_time",
            "is_unique": 0,
            "is_primary": 0,
            "INDEX_TYPE": "BTREE",
            "NULLABLE": "",
            "COLUMN_NAME": "customer_id",
            "SEQ_IN_INDEX": 1,
            "CARDINALITY": 2980,
            "SUB_PART": None,
            "COLLATION": "A",
            "index_comment": "客户维度",
        },
        {
            "TABLE_NAME": "order_main",
            "INDEX_NAME": "idx_cust_time",
            "is_unique": 0,
            "is_primary": 0,
            "INDEX_TYPE": "BTREE",
            "NULLABLE": "",
            "COLUMN_NAME": "created_at",
            "SEQ_IN_INDEX": 2,
            "CARDINALITY": 29874,
            "SUB_PART": None,
            "COLLATION": "A",
            "index_comment": "客户维度",
        },
        {
            "TABLE_NAME": "customer",
            "INDEX_NAME": "uk_phone",
            "is_unique": 1,
            "is_primary": 0,
            "INDEX_TYPE": "BTREE",
            "NULLABLE": "",
            "COLUMN_NAME": "phone",
            "SEQ_IN_INDEX": 1,
            "CARDINALITY": 2990,
            "SUB_PART": 8,
            "COLLATION": "A",
            "index_comment": "",
        },
    ]
    indexes = rows_to_indexes(rows)
    assert [(i.table_name, i.index_name) for i in indexes] == [
        ("order_main", "PRIMARY"),
        ("order_main", "idx_cust_time"),
        ("customer", "uk_phone"),
    ]
    composite = indexes[1]
    # 复合索引列序
    assert [c.column_name for c in composite.columns] == ["customer_id", "created_at"]
    assert composite.cardinality == 2980, "§8.1 D：cardinality 取该索引第 1 行那一份"
    assert composite.comment == "客户维度"
    assert indexes[0].is_primary and indexes[0].is_unique
    # 验收锚点：SUB_PART 非空 = 没退回 Inspector/SHOW CREATE TABLE
    assert indexes[2].columns[0].sub_part == 8


def test_apply_index_flags_让列知道自己是不是主键() -> None:
    """§7：`RawColumn.is_primary_key` 来自 STATISTICS，不是 IS COLUMNS 的 COLUMN_KEY。"""
    from app.extractor.mysql import rows_to_columns

    cols = rows_to_columns(
        [
            _col("id"),
            _col("phone"),
            _col("customer_id"),
            _col("note"),
        ]
    )
    idx = rows_to_indexes(
        [
            _idx("t", "PRIMARY", "id", 1, unique=1, primary=1),
            _idx("t", "uk_phone", "phone", 1, unique=1),
            _idx("t", "idx_cust", "customer_id", 1, unique=0),
        ]
    )
    out = apply_index_flags(cols, idx)
    by_name = {c.column_name: c for c in out}
    assert by_name["id"].is_primary_key is True
    assert by_name["phone"].is_unique is True and by_name["phone"].is_indexed is True
    assert by_name["customer_id"].is_indexed is True and by_name["customer_id"].is_unique is False
    assert by_name["note"].is_indexed is False


def test_fks_一条约束的多列按_seq_排() -> None:
    rows = [
        {
            "TABLE_NAME": "order_item",
            "CONSTRAINT_NAME": "fk_item_order",
            "COLUMN_NAME": "order_id",
            "ORDINAL_POSITION": 1,
            "REFERENCED_TABLE_SCHEMA": "ai_web_demo",
            "REFERENCED_TABLE_NAME": "order_main",
            "REFERENCED_COLUMN_NAME": "id",
            "DELETE_RULE": "CASCADE",
            "UPDATE_RULE": "NO ACTION",
        }
    ]
    fk = rows_to_fks(rows)[0]
    assert (fk.table_name, fk.to_table, fk.from_column, fk.to_column) == (
        "order_item",
        "order_main",
        "order_id",
        "id",
    )
    assert fk.on_delete == "CASCADE" and fk.fk_name == "fk_item_order"
    assert fk.seq == 1 and fk.to_schema == "ai_web_demo"
    # §1：MySQL 没有 catalog 概念，恒空串
    assert fk.to_catalog == ""


def test_中文注释整片变成问号时必须报_charset_suspect() -> None:
    """§10.1：5.7 部分构建的 IS 走 utf8mb3，中文 TABLE_COMMENT 会回一串 `?`。

    占比阈值 0.3 是文档给的数，不是自己拍的。
    """
    good = ["订单主表", "商品分类表", None, ""]
    bad = ["?????", "????", None, "订单"]
    assert comment_charset_suspect(good) is False
    assert comment_charset_suspect(bad) is True
    assert comment_charset_suspect([]) is False, "一条注释都没有时不算乱码"


def _col(name: str) -> dict[str, object]:
    return {
        "TABLE_NAME": "t",
        "COLUMN_NAME": name,
        "ORDINAL_POSITION": 1,
        "DATA_TYPE": "bigint",
        "COLUMN_TYPE": "bigint(20)",
        "IS_NULLABLE": "NO",
        "COLUMN_DEFAULT": None,
        "EXTRA": "",
        "COLUMN_COMMENT": None,
        "CHARACTER_MAXIMUM_LENGTH": None,
        "NUMERIC_PRECISION": 19,
        "NUMERIC_SCALE": 0,
        "COLLATION_NAME": None,
        "enum_def": None,
    }


def _idx(
    table: str, index: str, column: str, seq: int, *, unique: int, primary: int = 0
) -> dict[str, object]:
    return {
        "TABLE_NAME": table,
        "INDEX_NAME": index,
        "is_unique": unique,
        "is_primary": primary,
        "INDEX_TYPE": "BTREE",
        "NULLABLE": "",
        "COLUMN_NAME": column,
        "SEQ_IN_INDEX": seq,
        "CARDINALITY": 10,
        "SUB_PART": None,
        "COLLATION": "A",
        "index_comment": "",
    }
