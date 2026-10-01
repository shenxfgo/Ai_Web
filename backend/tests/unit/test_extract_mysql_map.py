"""`information_schema` 的行 → `Raw*` 的映射（纯函数，不连库）。

期望值口径：
- `docs/metadata-model.md` §7：`Raw*` 的字段语义（跨方言归一后的形状）
- 同文 §8.1：这五条 SQL 的列顺序与取值（`IS_NULLABLE='YES'`、`(NON_UNIQUE=0)` 这类表达式
  在驱动里回来的形状是实测过的：5.7 的布尔表达式回 int 0/1
- 同文 §9：`enum('a','b')` 要拆成值清单，中文枚举值直接决定 where 能否命中
- 同文 §10.1：5.7 的 IS 中文注释可能整片回 `???`，占比 >0.3 要打 `CHARSET_SUSPECT`
- 同文 §2.4 as-built(P3-022)：`last_analyze_at` 的取值口径（UPDATE_TIME 优先 → 退回
  CREATE_TIME → 都空 NULL；视图恒 NULL；naive datetime 按源库机器时区**显式**挂时区）
"""

from __future__ import annotations

import datetime as dt

from app.extractor.base import apply_index_flags
from app.extractor.mysql import (
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


# ---------------------------------------------------------------------------
# P3-022：last_analyze_at 的取值口径（工单 022 已定口径，写进 metadata-model §2.4）
# ---------------------------------------------------------------------------


def _trow(
    name: str,
    *,
    create: object = None,
    update: object = None,
    table_type: str = "BASE TABLE",
) -> dict[str, object]:
    """§8.1 B 的一行最小形状：只关心 CREATE_TIME / UPDATE_TIME，其余给齐键就够。"""
    return {
        "TABLE_SCHEMA": "ai_web_demo",
        "TABLE_NAME": name,
        "table_type": table_type,
        "TABLE_COMMENT": "",
        "ENGINE": "InnoDB",
        "ROW_FORMAT": "Dynamic",
        "TABLE_COLLATION": "utf8mb4_general_ci",
        "TABLE_ROWS": 10,
        "DATA_LENGTH": 16384,
        "INDEX_LENGTH": 0,
        "CREATE_TIME": create,
        "UPDATE_TIME": update,
    }


def test_tables_只有_CREATE_TIME_时退回它并显式挂上源库机器时区() -> None:
    """口径第一支：`UPDATE_TIME` 为空则退回 `CREATE_TIME`。

    时区口径（P3-022 拍板，metadata-model §2.4）：MySQL 的 datetime 无时区、目标列是
    timestamptz，转换必须显式——抽取层按源库所在机器时区 attach，不许靠驱动隐式转换。
    期望值是手算的：东八区 18:30 的墙上时间，同一瞬间在 UTC 是 10:30。
    """
    east8 = dt.timezone(dt.timedelta(hours=8))
    (table,) = rows_to_tables(
        [_trow("order_main", create=dt.datetime(2026, 9, 28, 18, 30))], source_tz=east8
    )
    assert table.last_analyze_at == dt.datetime(2026, 9, 28, 18, 30, tzinfo=east8)
    assert table.last_analyze_at is not None
    in_utc = table.last_analyze_at.astimezone(dt.UTC)
    assert in_utc == dt.datetime(2026, 9, 28, 10, 30, tzinfo=dt.UTC)


def test_tables_时间戳以字符串回来时同样显式解析并挂时区() -> None:
    """防御支钉住：个别驱动/构建组合下 IS 时间列回字符串——口径不变，仍是"显式转换，
    不靠驱动隐式行为"（P3-022 时区拍板，metadata-model §2.4），期望值与第一支同一手算。
    """
    east8 = dt.timezone(dt.timedelta(hours=8))
    (table,) = rows_to_tables([_trow("order_main", create="2026-09-28 18:30:00")], source_tz=east8)
    assert table.last_analyze_at == dt.datetime(2026, 9, 28, 18, 30, tzinfo=east8)


def test_tables_两者都有时取_UPDATE_TIME_只有_UPDATE_TIME_也同样取它() -> None:
    """口径第二支：`UPDATE_TIME` 优先。期望值手算：东八区 2026-09-29 07:15 → UTC 前一日 23:15。

    注意钉的方向：5.7 的 InnoDB `UPDATE_TIME` 在服务器重启后归 NULL（工单 022 明示），
    所以这里只钉"有值时取谁"，真跑落到哪一支由 live 用例看事实，不预设。
    """
    east8 = dt.timezone(dt.timedelta(hours=8))
    rows = [
        _trow(
            "order_main",
            create=dt.datetime(2026, 1, 5, 9, 0),
            update=dt.datetime(2026, 9, 29, 7, 15),
        ),
        _trow("product", update=dt.datetime(2026, 9, 2, 20, 5)),
    ]
    order, product = rows_to_tables(rows, source_tz=east8)
    assert order.last_analyze_at == dt.datetime(2026, 9, 29, 7, 15, tzinfo=east8)
    assert product.last_analyze_at == dt.datetime(2026, 9, 2, 20, 5, tzinfo=east8)


def test_tables_两者都空时_last_analyze_at_是_NULL() -> None:
    """口径第三支：两者都空则 NULL——视图不给造新鲜度，基表也不许造。

    这条在 P3-022 之前是**唯一走得通的支**（映射整列没接，恒 NULL）；接上之后它仍是
    5.7 重启后的真实形态，所以值得单独钉，而不是只藏在上面两条的边角里。
    """
    (table,) = rows_to_tables([_trow("order_main")], source_tz=dt.UTC)
    assert table.last_analyze_at is None


def test_tables_视图那一行恒_NULL_两个时间戳都不许冒充数据更新时间() -> None:
    """P3-022 拍板（metadata-model §2.4）：视图**不给造新鲜度**。

    MySQL 里视图行的 CREATE_TIME 是**定义时间**、UPDATE_TIME 语义上也不是"数据被更新"——
    任何一个填进去都是给【规模】那句"最近更新"喂一个假日期。所以视图这半边不是"恰好为
    NULL"（视图的 UPDATE_TIME 常为 NULL 不是 bug），而是**无论 IS 回了什么都不读**。
    """
    east8 = dt.timezone(dt.timedelta(hours=8))
    rows = [
        _trow(
            "v_daily_sales",
            create=dt.datetime(2026, 9, 1, 8, 0),
            update=dt.datetime(2026, 9, 20, 23, 30),
            table_type="VIEW",
        )
    ]
    (view,) = rows_to_tables(rows, source_tz=east8)
    assert view.last_analyze_at is None


def test_tables_不传时区时按本机时区显式挂上而不是留_naive() -> None:
    """默认口径：真实链路（`collect()`）不传 `source_tz`，取的是**源库所在机器**的时区。

    期望值不经过被测助手，走标准库自己的口径：`naive.astimezone(...)` 在 PEP 与
    `datetime` 文档里明写"naive 输入按系统本地时区解释"——与拍板口径同一句话，两个来源。
    """
    naive = dt.datetime(2026, 9, 28, 18, 30)
    (table,) = rows_to_tables([_trow("order_main", create=naive)])
    got = table.last_analyze_at
    assert got is not None and got.tzinfo is not None, "naive datetime 进 timestamptz 列就是事故"
    assert got.astimezone(dt.UTC) == naive.astimezone(dt.UTC)


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
