"""`Raw*` → `meta_*` 行字典的四条取舍（`sync_service` 的纯函数段）。

这里钉的都是"错法很安静"的地方：少一条边不会报错，多一条悬空引用会把整批同步炸成
failed，而把人工列写进行字典会让 upsert 的 INSERT 分支拿 NULL 覆盖用户补录的中文。

期望值口径：§2.4（列定义）、§4（extracted 的作用域）、验收 3/4（自引用与范围外键）。
"""

from __future__ import annotations

from app.extractor.base import RawColumn, RawForeignKey, RawIndex, RawIndexColumn, RawTable
from app.services import sync_service as ss

K = ("", "ai_web_demo", "order_item")


def _table(name: str, **kw: object) -> RawTable:
    base: dict[str, object] = {
        "catalog_name": "",
        "schema_name": "ai_web_demo",
        "table_name": name,
        "table_type": "BASE TABLE",
        "comment": None,
        "engine": "InnoDB",
        "charset": "utf8mb4",
        "collation": "utf8mb4_general_ci",
        "approx_rows": 10,
        "data_bytes": 16384,
        "index_bytes": 0,
    }
    base.update(kw)
    return RawTable(**base)  # type: ignore[arg-type]


def _column(table: str, name: str, **kw: object) -> RawColumn:
    base: dict[str, object] = {
        "catalog_name": "",
        "schema_name": "ai_web_demo",
        "table_name": table,
        "column_name": name,
        "ordinal_position": 1,
        "data_type": "int",
        "raw_data_type": "int",
        "nullable": False,
        "default": None,
        "generated": False,
        "comment": None,
        "char_length": None,
        "num_precision": None,
        "num_scale": None,
        "enum_values": None,
        "is_primary_key": False,
    }
    base.update(kw)
    return RawColumn(**base)  # type: ignore[arg-type]


def _fk(from_table: str, col: str, to_table: str, to_schema: str | None) -> RawForeignKey:
    return RawForeignKey(
        catalog_name="",
        schema_name="ai_web_demo",
        table_name=from_table,
        fk_name=f"fk_{col}",
        from_column=col,
        to_catalog=None,
        to_schema=to_schema,
        to_table=to_table,
        to_column="id",
        seq=1,
        on_delete="CASCADE",
        on_update="RESTRICT",
    )


def test_表行不带任何人工列也不带生成列() -> None:
    """§3 + ADR-0005：人工列与生成列都必须缺席本批字典。

    - 人工列若出现，INSERT 分支会拿 `None` 写进去。`COALESCE(现有值, 新值)` 只保护
      DO UPDATE 分支，第一次同步之后用户填的中文照样被冲——保护逻辑是对的，
      但"本轮没提供"被表达成"本轮提供了空"，幂等就破了。
    - `table_uid` 是 `GENERATED ALWAYS ... STORED`，写它 PG 直接报错。
    """
    row = ss.table_rows(1, 2, [_table("order_item")])[0]
    for human in ("comment_zh", "business_desc", "granularity", "is_hidden", "is_stale"):
        assert human not in row, human
    assert "table_uid" not in row
    assert "created_at" not in row and "synced_at" not in row, "同步时刻由语句的 now() 给"
    assert row["comment_raw"] is None and row["table_name"] == "order_item"


def test_列行跳过本轮范围外的表() -> None:
    """外键能把范围外表的列也带进 `Raw*`，那一批整列丢掉，不写孤儿行。"""
    rows = ss.column_rows(
        {K: 7},
        [_column("order_item", "id"), _column("ghost_table", "id")],
    )
    assert [r["column_name"] for r in rows] == ["id"]
    assert rows[0]["table_id"] == 7


def test_悬空外键变成_warning_而不是悬空行() -> None:
    """验收 3 的反面：`category.parent_id` 自引用能落，指向范围外库的外键必须被拦下。

    拦下而不是插进去：`from_table_id`/`to_table_id` 都是 NOT NULL 外键，一条悬空引用
    会让整批同步 failed，而不是少这一条边。
    """
    ids = {K: 7, ("", "ai_web_demo", "order_main"): 8, ("", "ai_web_demo", "category"): 9}
    rows, skipped = ss.relation_rows(
        1,
        ids,
        [
            _fk("order_item", "order_id", "order_main", None),
            # 自引用：两端是同一个 id，成环但不是错
            _fk("category", "parent_id", "category", None),
            _fk("order_item", "warehouse_id", "warehouse", "other_db"),
        ],
    )
    assert [(r["from_table_id"], r["to_table_id"]) for r in rows] == [(7, 8), (9, 9)]
    assert all(r["source_kind"] == "extracted" for r in rows)
    assert [w["code"] for w in skipped] == ["RELATION_OUT_OF_SCOPE"]
    assert "warehouse" in skipped[0]["detail"]


def test_索引子行的自然键回填成_index_id() -> None:
    """§2.4：`meta_index_column.index_id` 要代理键，而代理键只有插完父行才拿得到。

    验收 2 要求 `sub_part` 非空——它活在这一条回填之后的行里，回填丢了就等于退回
    `SHOW CREATE TABLE` 方案。
    """
    parents, children = ss.index_rows(
        {K: 7},
        [
            RawIndex(
                catalog_name="",
                schema_name="ai_web_demo",
                table_name="order_item",
                index_name="idx_item_product",
                is_unique=False,
                is_primary=False,
                index_type="BTREE",
                comment=None,
                cardinality=1200,
                columns=(
                    RawIndexColumn(
                        column_name="product_id",
                        seq_in_index=1,
                        collation="A",
                        sub_part=8,
                    ),
                ),
            )
        ],
    )
    assert parents[0]["cardinality"] == 1200
    assert children[0]["table_id"] == 7  # 半成品：还带自然键
    resolved = ss.resolve_index_ids(children, [(41, 7, "idx_item_product")])
    assert resolved == [
        {
            "index_id": 41,
            "seq_in_index": 1,
            "column_name": "product_id",
            "collation": "A",
            "sub_part": 8,
        }
    ]
    # 父行没插进去时子行跟着丢，不再补一条悬空
    assert ss.resolve_index_ids(children, []) == []


def test_合并分账只搬写完的那些数_失败计数不被抵掉() -> None:
    """`counters` 必须是"确实提交完了的行数"，所以分账只在成功分支里合并（§6 的 `partial`）。

    第一次 live 真跑暴露的反例：一个 catalog 中途 rollback，一行没落，但总账已经边写边涨到
    `tables=10` —— 前端看到"10 张表同步好了 + partial"，而库里一张都没有。合并语义把这条
    路堵在函数边界上。`tables_failed` 不参与合并：它由编排层在 except 分支里加，
    成功的那一批不该把它抵回去。
    """
    total = ss._Tally(tables_failed=1)
    part = ss._Tally(databases=1, tables=10, columns=133, indexes=27)
    total.merge(part)
    assert total.as_dict() == {
        **ss._Tally().as_dict(),
        "databases": 1,
        "tables": 10,
        "columns": 133,
        "indexes": 27,
        "tables_failed": 1,
    }
    total.merge(ss._Tally(tables=3))
    assert total.tables == 13 and total.databases == 1
