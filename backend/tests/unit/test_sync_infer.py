"""命名约定推断 JOIN 边（architecture §5.3 的落库侧，验收 4 的判据）。

纯函数，不碰库：输入是抽取层的 `Raw*`，输出 `InferredRelation`。
放得下纯函数就该放得下——这一条的规则全是"什么时候**不**连边"，
用真库构造反例的代价远高于直接构造两个 `RawColumn`。

期望值口径：
- §5.3 的推断条件（列名同、另一端的主键是该列、类型族兼容）
- §5.3 的加权打分公式（010 拍板，取代 007 的常数 0.7；逐档权重钉在 `test_relation_infer.py`）
- 演示库脚本 `scripts/init_demo_mysql.sql` 里 `user_activity_log` 的设计意图
  （verification §1.1：故意只写列名不建外键）
"""

from __future__ import annotations

from app.extractor.base import RawColumn, RawForeignKey
from app.services import relation_infer as ri


def _col(table: str, name: str, *, dtype: str = "int", pk: bool = False) -> RawColumn:
    return RawColumn(
        catalog_name="",
        schema_name="ai_web_demo",
        table_name=table,
        column_name=name,
        ordinal_position=1,
        data_type=dtype,
        raw_data_type=dtype,
        nullable=False,
        default=None,
        generated=False,
        comment=None,
        char_length=None,
        num_precision=None,
        num_scale=None,
        enum_values=None,
        is_primary_key=pk,
    )


def _fk(table: str, col: str, to_table: str) -> RawForeignKey:
    return RawForeignKey(
        catalog_name="",
        schema_name="ai_web_demo",
        table_name=table,
        fk_name=f"fk_{table}_{col}",
        from_column=col,
        to_catalog=None,
        to_schema=None,
        to_table=to_table,
        to_column="id",
        seq=1,
        on_delete="NO ACTION",
        on_update="NO ACTION",
    )


def _shape(edges: object) -> list[tuple[str, str, str, str]]:
    return [(e.table_name, e.column_name, e.to_table_name, e.to_column_name) for e in edges]  # type: ignore[attr-defined]


def test_推断_product_id_指向_product_的主键() -> None:
    """验收 4：`user_activity_log` 没建外键，但 `product_id` 按命名约定推出 `→ product.id`。"""
    edges = ri.infer_relations(
        [_col("user_activity_log", "product_id"), _col("product", "id", pk=True)],
        (),
    )
    assert _shape(edges) == [("user_activity_log", "product_id", "product", "id")]
    # §5.3 加权公式（工单 010 拍板，取代 007 的常数 0.7）：这条边四项全中——
    # 目标是单列 PK(+0.25)、两侧都是 int 类型完全相同(+0.15)、_id 后缀的名词 product
    # 正是宿主表名(+0.15)、表名就是去后缀的原形(+0.10)，基数 0.35 → **1.00**。
    # 演示库里真实存在的那条推断边正是它（architecture §5.3 as-built 用它当算例）。
    assert edges[0].confidence == 1.0  # type: ignore[index]


def test_类型族兼容但不完全相同时只少那一档加成() -> None:
    """键被 ALTER 成 bigint 的迁移场景：族兼容照样连边，但 +0.15 拿不到 → 0.85。

    这一档是公式在 `infer_relations` 里唯一真正会变的加成分量（其余三项被前置减法钉成恒真），
    所以它必须被钉住——否则"加权公式"和写死一个常数没有区别。
    """
    edges = ri.infer_relations(
        [
            _col("order_item", "product_id", dtype="bigint"),
            _col("product", "id", pk=True, dtype="int"),
        ],
        (),
    )
    assert [e.confidence for e in edges] == [0.85]  # type: ignore[union-attr]


def test_推断不凭空造边也不覆盖真外键() -> None:
    """三条"宁缺毋滥"，演示库正是按它们设计的。

    - `user_id` 的列注释写着"与 customer.id 对齐"，但**没有任何文档定义 user↔customer
      同义词表**，所以推不出来才是对的：凭空连边的代价是 AI 真拿去 JOIN，且不报错。
    - `session_id` 同理，库里没有 `session` 表。
    - `order_item.order_id` 已有真外键，再补一条 inferred 就是同一对表两条边，
      而 §4 的优先级要求 extracted 压过 inferred。
    - `refund_record.order_id` 名字对得上但类型族不同（varchar vs int）：不是同一个键。
    """
    columns = [
        _col("user_activity_log", "user_id"),
        _col("user_activity_log", "session_id", dtype="char(32)"),
        _col("customer", "id", pk=True),
        _col("order_item", "order_id"),
        _col("order_main", "id", pk=True),
        _col("refund_record", "order_id", dtype="varchar(32)"),
    ]
    edges = ri.infer_relations(columns, (_fk("order_item", "order_id", "order_main"),))
    assert edges == []


def test_自引用不产生自环() -> None:
    """`category.parent_id`：真外键是 extracted（自引用成环不报错，验收 3），

    而推断侧要防的是"节点表自己连自己"这种自环——BFS 那边靠 `max_hops` 截断（§5.3），
    但把自环写进 `meta_relation` 会让 UI 的待确认清单出现一条毫无意义的边。
    列名 `parent_id` 的宿主表叫 `parent` 时才成立，这里给它一个真叫 `node` 的表来暴露问题。
    """
    edges = ri.infer_relations(
        [_col("node", "node_id"), _col("node", "id", pk=True)],
        (),
    )
    assert edges == []


def test_单复数都认得() -> None:
    """§5.3 的"t2 表名是 c 去后缀的单/复数变体"：`products` 与 `product` 都要能命中。

    只支持一侧的话，换一个命名风格的库就整批推不出边，而这不是错误、只是"没有结果"——
    最安静的一类失效。
    """
    assert _shape(
        ri.infer_relations([_col("order_item", "product_id"), _col("products", "id", pk=True)], ())
    ) == [("order_item", "product_id", "products", "id")]
    assert _shape(
        ri.infer_relations([_col("order_item", "products_id"), _col("product", "id", pk=True)], ())
    ) == [("order_item", "products_id", "product", "id")]


def test_整数族之间算兼容_varchar_与_int_不算() -> None:
    """§5.3 的"类型族兼容"：键被 ALTER 成 bigint 是常见迁移，族内要认；跨族不认。"""
    edges = ri.infer_relations(
        [
            _col("order_item", "product_id", dtype="bigint"),
            _col("product", "id", pk=True, dtype="int"),
        ],
        (),
    )
    assert _shape(edges) == [("order_item", "product_id", "product", "id")]
