"""命名约定推断 JOIN 边（architecture §5.3 的落库侧）。

无外键的分析库是现实常态：源库没建约束，但 `order_item.product_id → product.id` 这层含义
确实存在。这里把它推出来写成 `meta_relation(source_kind='inferred')`，供人工 accept 成 manual。

规则全部是**减法**（§5.3：列名同、另一端的主键是该列、类型族兼容），因为一条错的边会让
AI 拿去 JOIN 且运行期不报错——宁可推不出，不可推错。
"""

from __future__ import annotations

from collections.abc import Sequence

from app.extractor.base import InferredRelation, RawColumn, RawForeignKey

# 工单 007 备忘：常数打分，出自 roadmap §P4 / verification §2.1。
# 与 architecture §5.3 的加权公式冲突（且"0.7 配 ≥0.8 门槛"会让边永远进不了 prompt），
# 那条冲突已记进 §5.3 等 010 拍板，这里不自行选一个。
INFERRED_CONFIDENCE = 0.7

# §5.3 点名的三种后缀
_ID_SUFFIXES = ("_id", "_no", "_code")
# "类型族兼容"里唯一真正会漂移的一族：主键被 ALTER 成 bigint 是最常见的迁移事故
_INT_FAMILY = frozenset(
    {"int", "integer", "bigint", "smallint", "mediumint", "tinyint", "int8", "int4", "int2"}
)


def _family(data_type: str) -> frozenset[str]:
    base = data_type.partition("(")[0].strip().lower()
    return _INT_FAMILY if base in _INT_FAMILY else frozenset({base})


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 3:
        return f"{word[:-3]}y"
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _candidate_tables(noun: str) -> frozenset[str]:
    """`product_id` 的宿主表可能写作 `product`，也可能写作 `products`（§5.3 的单/复数变体）。

    只认一侧的话，换一个命名风格的库就整批推不出边，而那不会报错——只是"没有结果"。
    """
    return frozenset({noun, f"{noun}s", _singular(noun)})


def infer_relations(
    columns: Sequence[RawColumn], foreign_keys: Sequence[RawForeignKey]
) -> list[InferredRelation]:
    """从"列 + 真外键"推出候选边；同一条边上真外键优先，故被真外键覆盖的列直接跳过。

    只认**单列主键**的表：复合主键下"该列是它的主键"这句话没有唯一答案，
    §5.3 也没规定选哪一列，宁可不推。
    """
    primary_keys: dict[tuple[str, str], str] = {}
    ambiguous: set[tuple[str, str]] = set()
    dtype_by_column: dict[tuple[str, str, str], str] = {}
    for column in columns:
        dtype_by_column[(column.schema_name, column.table_name, column.column_name)] = (
            column.data_type
        )
        if not column.is_primary_key:
            continue
        key = (column.schema_name, column.table_name)
        if key in primary_keys:
            ambiguous.add(key)
        primary_keys[key] = column.column_name
    for key in ambiguous:
        # 同名表出现在两个 schema 时按 (schema, table) 分桶，不会误判成复合主键。
        primary_keys.pop(key, None)

    # 已经有真外键的列不再补推断边（§4：extracted 压过 inferred）
    enforced = {(fk.schema_name, fk.table_name, fk.from_column) for fk in foreign_keys}

    edges: list[InferredRelation] = []
    for column in columns:
        noun = next(
            (
                column.column_name[: -len(suffix)]
                for suffix in _ID_SUFFIXES
                if column.column_name.endswith(suffix)
            ),
            "",
        )
        if not noun:
            continue
        origin = (column.schema_name, column.table_name)
        if (origin[0], origin[1], column.column_name) in enforced:
            continue
        for target in sorted(_candidate_tables(noun)):
            target_key = (column.schema_name, target)
            if target_key == origin:
                continue  # 自环：节点表 `node.node_id → node.id` 这类，写进清单只是噪音
            to_column = primary_keys.get(target_key)
            if to_column is None:
                continue
            other = dtype_by_column.get((*target_key, to_column), "")
            if _family(column.data_type) & _family(other):
                edges.append(
                    InferredRelation(
                        schema_name=column.schema_name,
                        table_name=column.table_name,
                        column_name=column.column_name,
                        to_table_name=target,
                        to_column_name=to_column,
                        confidence=INFERRED_CONFIDENCE,
                    )
                )
    return edges
