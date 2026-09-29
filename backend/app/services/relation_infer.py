"""命名约定推断 JOIN 边（architecture §5.3 的落库侧）。

无外键的分析库是现实常态：源库没建约束，但 `order_item.product_id → product.id` 这层含义
确实存在。这里把它推出来写成 `meta_relation(source_kind='inferred')`，供人工 accept 成 manual。

规则全部是**减法**（§5.3：列名同、另一端的主键是该列、类型族兼容），因为一条错的边会让
AI 拿去 JOIN 且运行期不报错——宁可推不出，不可推错。
"""

from __future__ import annotations

from collections.abc import Sequence

from app.extractor.base import ExtractWarning, InferredRelation, RawColumn, RawForeignKey

# §5.3 的加权公式（工单 010 拍板取代 007 的常数打分）。
_BASE = 0.35
_W_TARGET_PK = 0.25
_W_IDENTICAL_TYPE = 0.15
_W_SUFFIX_NOUN = 0.15
_W_TABLE_VARIANT = 0.10
# 007 那版常数降为**地板**：本函数的三条前置减法把四项全灭的组合守成了不可达
# （能走到打分的边必然满足"目标列是该表 PK"和"后缀匹配"），所以它只是一个下限承诺，
# 不是任何一条真实边的分数。口径见 architecture §5.3 as-built(P2-0010)。
_FLOOR = 0.7

# §5.3 点名的三种后缀
_ID_SUFFIXES = ("_id", "_no", "_code")
# "类型族兼容"里唯一真正会漂移的一族：主键被 ALTER 成 bigint 是最常见的迁移事故
_INT_FAMILY = frozenset(
    {"int", "integer", "bigint", "smallint", "mediumint", "tinyint", "int8", "int4", "int2"}
)


def _family(data_type: str) -> frozenset[str]:
    base = data_type.partition("(")[0].strip().lower()
    return _INT_FAMILY if base in _INT_FAMILY else frozenset({base})


def _identical_type(a: RawColumn, b: RawColumn) -> bool:
    """§5.3 的"类型完全相同"，比"族兼容"更严的那一档加成。

    光比 `data_type` 不够：varchar(32) 和 varchar(64) 的 `data_type` 都是 `varchar`，
    长度在 `char_length`/`num_precision`/`num_scale` 里——而"长度不同的两个键能不能等值 JOIN"
    正是"完全相同"这句话要回答的事。
    """
    return (
        a.data_type.partition("(")[0].strip().lower()
        == b.data_type.partition("(")[0].strip().lower()
        and a.char_length == b.char_length
        and a.num_precision == b.num_precision
        and a.num_scale == b.num_scale
    )


def inferred_confidence(
    *,
    target_is_pk: bool,
    identical_type: bool,
    suffix_noun_matches: bool,
    table_name_variant: bool,
) -> float:
    """§5.3 的加权公式，逐项手加，不做归一化。

    单独拆出来是因为 `infer_relations` 的前置条件会让四个布尔里的三个恒真——
    公式的分档能力全在"类型是否完全相同"这一项上，而这一点值得被单独钉住测试。
    """
    score = _BASE + (
        _W_TARGET_PK * target_is_pk
        + _W_IDENTICAL_TYPE * identical_type
        + _W_SUFFIX_NOUN * suffix_noun_matches
        + _W_TABLE_VARIANT * table_name_variant
    )
    return round(max(score, _FLOOR), 2)


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
    by_column: dict[tuple[str, str, str], RawColumn] = {}
    for column in columns:
        by_column[(column.schema_name, column.table_name, column.column_name)] = column
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
            other = by_column.get((*target_key, to_column))
            if other is not None and _family(column.data_type) & _family(other.data_type):
                # 三个布尔里只有"类型完全相同"在本函数里真会变化——其余三项是上面那些
                # `continue` 的等价复述（能走到这里就说明后缀、名词、单列 PK 都对了）。
                # 仍然逐项传给公式而不是直接写 1.00：放宽减法规则时分数会跟着掉。
                noun_matches = _singular(noun) == _singular(target)
                edges.append(
                    InferredRelation(
                        schema_name=column.schema_name,
                        table_name=column.table_name,
                        column_name=column.column_name,
                        to_table_name=target,
                        to_column_name=to_column,
                        confidence=inferred_confidence(
                            target_is_pk=True,
                            identical_type=_identical_type(column, other),
                            suffix_noun_matches=noun_matches,
                            table_name_variant=noun_matches and target in _candidate_tables(noun),
                        ),
                    )
                )
    return edges


def high_degree_fields(columns: Sequence[RawColumn], *, max_degree: int) -> list[ExtractWarning]:
    """§5.3 末"候选度 > 8"的**写侧一半**：只告警，一条边都不丢（工单 014 拍板）。

    为什么不是"丢弃"：`infer_relations` 的减法规则保证了每条推断边的目标端必是单列主键，
    而主键按定义带唯一约束——文档那句"需另一端有唯一约束，否则丢弃"的"否则"永远走不到。
    真要丢就得放宽减法规则（让泛列连到任一唯一索引列），那是文档没要求的连接能力。

    计数口径与建图口径对齐：**按库分组**数**表**数（不是列数），因为推断边与 JOIN 图都不跨库；
    否则会出现"告警说这列太泛、图里却照常用它扩展"的两套说法。
    """
    tables_by_field: dict[tuple[str, str], set[str]] = {}
    for column in columns:
        if not column.column_name.endswith(_ID_SUFFIXES):
            continue  # 只有会进入推断的后缀列才是"候选"，`remark` 再泛也不构成 JOIN 噪声
        tables_by_field.setdefault((column.schema_name, column.column_name), set()).add(
            column.table_name
        )
    flagged = sorted(
        (
            (schema, name, len(tables))
            for (schema, name), tables in tables_by_field.items()
            if len(tables) > max_degree
        ),
        # 一个库可能有多条泛列，最泛的排前面——告警会整批端给用户，顺序就是阅读顺序。
        key=lambda row: (row[0], -row[2], row[1]),
    )
    return [
        ExtractWarning(
            code="join_field_too_generic",
            detail=(
                f"`{schema}.{name}` 出现在 {degree} 张表（门槛 {max_degree}）："
                "泛列，但它生成的推断边照旧全部落库、照旧进【可 JOIN】清单；读侧的抑制按**表**"
                "的无向度数算（`join_graph` 的表度门槛），与这里的字段度是两个不同的量，"
                "所以这条只是人工排查线索，不表示本列已被自动降级。"
            ),
        )
        for schema, name, degree in flagged
    ]
