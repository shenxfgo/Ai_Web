"""`pg_catalog` 的行 → `Raw*` 的映射（纯函数，不连库）。

期望值口径（逐条出处，都不从实现反推）：
- `docs/metadata-model.md` §8.2 A–E：这五条 SQL 各自的列与取值（别名就是驱动回来的键名）
- 同文 §7：`Raw*` 的字段语义（跨方言归一后的形状）
- 同文 §9：`character varying(64)→varchar(64)`、`timestamp with time zone→timestamptz`、
  `double precision→float8`、`integer[]→int[]` 这四条是文档逐字给的映射
- 同文 §2.4 注 ②（视图不给造新鲜度，方言无关）与注 ④（PG 的 `last_analyze_at` 取
  `last_analyze` / `last_autoanalyze` 的较晚者）
- `scripts/init_demo_pg.sql` 的 015 自检（`docs/verification.md` §1.5）：数组列清单
  （`product.tags` text[]、`hit_ids` int4[]、`scores` numeric[]、`occurred_at` timestamptz[]、
  `top_keywords` varchar(64)[]）、serial 列的判据是默认值 `nextval%`

**假件行值的形状**来自本机 PG 18.6 + psycopg3 的实测（元数据库上真发过这五条 SQL，见工单
交付记录）：表达式列 `(NOT a.attnotnull)` 回 Python bool、`(x.ord)::int` 回 int、
`pg_stat_*` 的时间戳回**带时区**的 datetime（所以这里不补时区，与 MySQL 那侧的
`_stamp_to_aware` 是两条路）。**只有 `jsonb_agg(...)` 回 list 这一条不是实测**：元数据库里一个
枚举类型都没有，那一段 CASE 本机全部回 NULL，所以它是按 PG 的 jsonb→Python 默认映射推出来的，
归 live 半边补验（同 metadata-model §8.2 末注"本片不猜的一格"）。真库语义（演示库里到底有几张表、
哪个类型归一成什么）也不在这里验，那是 `tests/integration/test_extract_pg_live.py` 的活。
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.extractor import postgres as pg

_SHANGHAI = dt.timezone(dt.timedelta(hours=8))


def _table_row(name: str, *, kind: str = "BASE TABLE", comment: str | None = "订单主表") -> dict:
    return {
        "schema_name": "demo",
        "table_name": name,
        "table_type": kind,
        "comment": comment,
        "is_heap": kind == "BASE TABLE",
        "size_bytes": 81920,
        "approx_rows": 2000,
        "last_analyze": dt.datetime(2026, 9, 30, 3, 1, tzinfo=_SHANGHAI),
        "last_autoanalyze": dt.datetime(2026, 9, 30, 3, 2, tzinfo=_SHANGHAI),
    }


def _column_row(
    table: str,
    column: str,
    *,
    typname: str,
    raw: str,
    ordinal: int = 1,
    comment: str | None = "备注",
) -> dict:
    return {
        "schema_name": "demo",
        "table_name": table,
        "ordinal_position": ordinal,
        "column_name": column,
        "raw_data_type": raw,
        "data_type": typname,
        "nullable": True,
        "default_value": None,
        "is_generated": False,
        "comment": comment,
        "enum_values": None,
    }


def _index_row(
    table: str,
    index: str,
    column: str | None,
    seq: int,
    *,
    unique: bool = False,
    primary: bool = False,
) -> dict:
    return {
        "schema_name": "demo",
        "table_name": table,
        "index_name": index,
        "indisunique": unique,
        "indisprimary": primary,
        "index_type": "btree",
        "comment": None,
        "cardinality_hint": 7,
        "def": f"CREATE INDEX {index} ON demo.{table} USING btree ({column})",
        "column_name": column,
        "seq_in_index": seq,
    }


def _fk_row(table: str, column: str, to_table: str, to_column: str = "id", seq: int = 1) -> dict:
    return {
        "schema_name": "demo",
        "table_name": table,
        "constraint_name": f"fk_{table}_{column}",
        "from_column": column,
        "seq": seq,
        "to_schema": "demo",
        "to_table": to_table,
        "to_column": to_column,
        "on_delete": "CASCADE",
        "on_update": "NO ACTION",
    }


# ------------------------------------------------------------------ B：表


def test_tables_把_pg_class_行摊成_rawtable_并带上真库名() -> None:
    out = pg.rows_to_tables([_table_row("order_main")], catalog_name="ai_web_demo_pg")
    table = out[0]
    assert (table.catalog_name, table.schema_name, table.table_name) == (
        "ai_web_demo_pg",
        "demo",
        "order_main",
    ), "PG 的 catalog 是真库名，不许照抄 MySQL 那个空串（工单 024 已定口径）"
    assert table.table_type == "BASE TABLE"
    assert table.comment == "订单主表"
    assert table.approx_rows == 2000


def test_tables_的引擎名来自_is_heap_而尺寸两格留空() -> None:
    """§2.4 只有 `data_bytes` / `index_bytes` 两格，而 B 条给的是 `pg_total_relation_size`
    （表 + 索引 + TOAST 的合计）——塞进任意一格都会让跨方言求和把索引数两遍，所以两格都空。
    """
    heap = pg.rows_to_tables([_table_row("t")], catalog_name="ai_web_demo_pg")[0]
    assert heap.engine == "heap", "§8.2 B 的 `amname='heap'` 子查询：堆表的访问方法就叫 heap"
    assert (heap.data_bytes, heap.index_bytes) == (None, None)
    # 非堆（别的表 AM）不该被硬编成 'heap'
    other = dict(_table_row("t"), is_heap=False)
    assert pg.rows_to_tables([other], catalog_name="c")[0].engine is None
    assert (heap.charset, heap.collation, heap.row_format) == (None, None, None), (
        "PG 没有 MySQL 那套库级字符集/行格式：这三格在 PG 侧恒空，不能拿库名冒充"
    )


def test_tables_把空注释归成_none_而不是空串() -> None:
    """`COMMENT ON TABLE x IS ''` 是合法语句；卡片层按 `None` 判缺注释，空串会假装它有。"""
    row = dict(_table_row("t_no_comment"), comment="")
    assert pg.rows_to_tables([row], catalog_name="ai_web_demo_pg")[0].comment is None


def test_新鲜度取_analyze_与_autoanalyze_的较晚者_视图恒空() -> None:
    """§2.4 注 ④（两列取较晚）与注 ②（视图不给造新鲜度，方言无关）。

    视图在 PG 里本来就不进 `pg_stat_user_tables`，那两列天然是 NULL；这里仍要钉住 VIEW 分支
    恒 NULL，因为承诺写在代码上才算数——真库那一份在 live 用例。
    """
    later_auto = dt.datetime(2026, 9, 30, 5, 0, tzinfo=_SHANGHAI)
    early = dt.datetime(2026, 1, 1, tzinfo=_SHANGHAI)
    rows = [
        dict(_table_row("a"), last_analyze=later_auto, last_autoanalyze=None),
        dict(_table_row("b"), last_analyze=early),
        dict(_table_row("c", kind="VIEW"), last_analyze=None, last_autoanalyze=None),
    ]
    out = pg.rows_to_tables(rows, catalog_name="ai_web_demo_pg")
    assert out[0].last_analyze_at == later_auto, "只有 manual analyze 时就用它"
    assert out[1].last_analyze_at is not None and out[1].last_analyze_at > early, (
        "两个都在时取较晚的那个（假件里 last_autoanalyze 晚一天）"
    )
    assert out[2].last_analyze_at is None, "§2.4 注 ②：视图恒 NULL，别拿基表的统计充数"
    assert out[0].last_analyze_at is not None and out[0].last_analyze_at.tzinfo is not None, (
        "PG 的这两列本身是 timestamptz，回来的必须是 aware datetime（不补时区也不许丢时区）"
    )


def test_scope_counts_只按两类相加() -> None:
    counts = pg.rows_to_scope_counts(
        [{"table_type": "BASE TABLE", "n": 9}, {"table_type": "VIEW", "n": 1}]
    )
    assert (counts.total, counts.base_table, counts.view) == (10, 9, 1), (
        "演示库 2026-09-27 拍板的分母：9 表 + 1 视图 = 10，不是 11（下划线前缀那个被范围过滤排除）"
    )


# ------------------------------------------------------------------ C：列


def test_columns_归一值进_data_type_而_format_type_原文留给_raw_data_type() -> None:
    """§9 逐字给的四条映射，加上 023 的 raw 存方言原文这一口径。"""
    rows = [
        _column_row("product", "name", typname="varchar", raw="character varying(64)"),
        _column_row("order_main", "paid_at", typname="timestamptz", raw="timestamp with time zone"),
        _column_row("order_main", "amount", typname="float8", raw="double precision"),
        _column_row("search_hit", "hit_ids", typname="_int4", raw="integer[]"),
        _column_row("customer", "id", typname="int4", raw="integer"),
    ]
    out = pg.rows_to_columns(rows, catalog_name="ai_web_demo_pg")
    assert [c.data_type for c in out] == ["varchar(64)", "timestamptz", "float8", "int[]", "int"]
    assert [c.raw_data_type for c in out] == [
        "character varying(64)",
        "timestamp with time zone",
        "double precision",
        "integer[]",
        "integer",
    ], "§9：方言原文不许在抽取层就被抹掉"
    assert [c.catalog_name for c in out] == ["ai_web_demo_pg"] * 5


def test_columns_的长度精度只给白名单里的三种类型() -> None:
    """§8.2 C 的 `modifiers` CASE 白名单是 `varchar`/`bpchar`/`numeric`，其余三格全空。

    数组列的 `typname` 是 `_varchar` 这种下划线形态，落在白名单之外——`(64)` 属于元素而不属于
    列，填进 `char_length` 就等于宣称这列能存 64 字符，而那是一句假话。
    """
    rows = [
        _column_row("kb_card", "doc_uid", typname="bpchar", raw="character(32)"),
        _column_row("order_main", "discount", typname="numeric", raw="numeric(5,2)"),
        _column_row("t", "d0", typname="numeric", raw="numeric(10,0)"),
        _column_row("t", "n", typname="text", raw="text"),
        _column_row("t", "v", typname="int4", raw="integer"),
        _column_row(
            "search_hit", "top_keywords", typname="_varchar", raw="character varying(64)[]"
        ),
    ]
    out = pg.rows_to_columns(rows, catalog_name="ai_web_demo_pg")
    assert [(c.char_length, c.num_precision, c.num_scale) for c in out] == [
        (32, None, None),
        (None, 5, 2),
        (None, 10, 0),
        (None, None, None),
        (None, None, None),
        (None, None, None),
    ], "numeric 的 scale=0 也要留（与 MySQL 的 decimal(10) 归一后同一形状）"


def test_columns_把_null_flag_默认值_生成列翻成归一形状() -> None:
    rows = [
        dict(
            _column_row("order_main", "amount", typname="numeric", raw="numeric(10,2)"),
            nullable=False,
            default_value="((quantity * unit_price))",
            is_generated=True,
        ),
        dict(
            _column_row("customer", "id", typname="int4", raw="integer"),
            default_value="nextval('demo.customer_id_seq'::regclass)",
        ),
        dict(_column_row("t", "c", typname="text", raw="text"), comment=""),
    ]
    out = pg.rows_to_columns(rows, catalog_name="ai_web_demo_pg")
    assert out[0].nullable is False
    assert out[0].generated is True, "§8.2 C 的 `a.attgenerated <> ''`"
    assert out[1].default == "nextval('demo.customer_id_seq'::regclass)", (
        "015 自检按这条判 serial（`pg_get_expr(...) LIKE 'nextval%'`）：默认值必须是原文"
    )
    assert out[2].comment is None, "空注释归成 None，与表那一侧同一口径"


def test_columns_的枚举靠_typtype_旗标认而不是靠类型名() -> None:
    """PG 的枚举是独立类型对象，`format_type` 打出来只有它的名字（实测形如 `order_status`）。

    没有这个旗标就会把类型名当 `data_type` 落库，于是 MySQL 的 `enum` 对上 PG 的
    `order_status`——正是 §9 要避免的"同一语义两种字面"。演示 PG 夹具（015）把状态列建成
    `text` + 注释里的取值清单，没有 `CREATE TYPE`，所以这一支在真库上取不到样本，只能钉形状。
    """
    rows = [
        dict(
            _column_row("order_main", "status", typname="order_status", raw="order_status"),
            enum_values=["pending", "paid", "shipped", "completed", "cancelled", "refunding"],
        )
    ]
    out = pg.rows_to_columns(rows, catalog_name="ai_web_demo_pg")
    assert out[0].data_type == "enum", "值域里没有 order_status 这个基名"
    assert out[0].raw_data_type == "order_status", "方言原文照留"
    assert out[0].enum_values is not None and len(out[0].enum_values) == 6, (
        "015 自检里 order_main.status 的枚举基数正是 6"
    )


def test_columns_不认识非列表形状的_enum_values() -> None:
    """驱动把 JSONB 解码成 list；真回出别的形状就说明上游变了，当场报错而不是猜。"""
    with pytest.raises(ValueError):
        pg._enum_values("{'a','b'}")


# ------------------------------------------------------------------ D：索引


def test_indexes_把同一索引的若干行归成一个对象并按_seq_保序() -> None:
    rows = [
        _index_row("order_item", "idx_order_item_composite", "order_id", 1, unique=True),
        _index_row("order_item", "idx_order_item_composite", "product_id", 2, unique=True),
        _index_row("order_item", "pk_order_item", "id", 1, unique=True, primary=True),
    ]
    out = pg.rows_to_indexes(rows, catalog_name="ai_web_demo_pg")
    assert len(out) == 2
    composite = next(i for i in out if i.index_name == "idx_order_item_composite")
    assert [c.column_name for c in composite.columns] == ["order_id", "product_id"], (
        "顺序来自 §8.2 D 的 WITH ORDINALITY，落库时 seq_in_index 要跟着它"
    )
    assert [c.seq_in_index for c in composite.columns] == [1, 2]
    assert composite.is_unique is True and composite.is_primary is False
    assert composite.index_type == "btree"
    assert composite.catalog_name == "ai_web_demo_pg"


def test_indexes_的表达式那一位留_null_而_cardinality_不许拿_idx_scan_填() -> None:
    """§8.2 D 末注：`attnum=0` 表示表达式索引列，`column_name` 回 NULL。

    `cardinality` 这一格必须空着：`idx_scan` 是这个索引被扫过多少次，不是这组列有多少不同值
    （§8.2 自己称它为 `cardinality_hint`），填进去就是拿错单位写一列名字管得着它的数据。
    假件里 `cardinality_hint` 非空，正是为了让这条断言能红。
    """
    rows = [
        _index_row("product", "ix_product_name_lower", None, 1),
        _index_row("product", "ix_product_name", "name", 1),
    ]
    out = pg.rows_to_indexes(rows, catalog_name="ai_web_demo_pg")
    expression = next(i for i in out if i.index_name == "ix_product_name_lower")
    assert expression.columns[0].column_name is None
    assert all(i.cardinality is None for i in out), "偏离 4：PG 侧基数得从 pg_stats.n_distinct 另拿"
    assert expression.columns[0].sub_part is None, "前缀索引是 MySQL 特有的形状"


# ------------------------------------------------------------------ E：外键


def test_fks_用真库名填_to_catalog_并把两侧列都带上() -> None:
    out = pg.rows_to_fks(
        [_fk_row("order_item", "order_id", "order_main")], catalog_name="ai_web_demo_pg"
    )
    fk = out[0]
    assert (fk.catalog_name, fk.to_catalog) == ("ai_web_demo_pg", "ai_web_demo_pg"), (
        "PG 的外键跨不了库，两侧都在 current_database() 里；MySQL 那侧这两格是空串"
    )
    assert (fk.from_column, fk.to_table, fk.to_column, fk.seq) == (
        "order_id",
        "order_main",
        "id",
        1,
    )
    assert (fk.on_delete, fk.on_update) == ("CASCADE", "NO ACTION"), (
        "§8.2 E 的 confupdtype 原文只写了「同理解析」，这里按 confdeltype 同一张字母表补全"
    )


def test_fks_的复合外键按_ord_给每一分配序号() -> None:
    rows = [
        _fk_row("order_item", "warehouse_id", "warehouse", "id", seq=1),
        _fk_row("order_item", "slot_code", "warehouse", "code", seq=2),
    ]
    out = pg.rows_to_fks(rows, catalog_name="ai_web_demo_pg")
    assert [(f.from_column, f.to_column, f.seq) for f in out] == [
        ("warehouse_id", "id", 1),
        ("slot_code", "code", 2),
    ]
