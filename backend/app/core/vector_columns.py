""" "维度 → kb_card 的向量列名"全仓**唯一**的那一次翻译（ADR-0012，工单 029 让它出生）。

纪律与 `core/sync_vocabulary.stage_of` 逐字相同：写侧（029 的 embed 档要往哪一列 UPDATE）与
读侧（031 的向量路要 ORDER BY 哪一列、`<=>` 算子作用在哪一列）都要读这一份，而它俩分属
`services/` 的两个模块——映射住在任何一方内部，另一方就只剩"再抄一遍 dict"这条路，
而抄错的失败模式（关键词路照常出结果、向量路读到一个语义无关的列）不报错。

它住在 `core/` 而不是 `services/`：`core` 不许反向 import `services`（`docs/architecture.md` §2
的分层），而这一份只 import `typing`，两侧都用得上而不成环。

**为什么键是字面量而不是 settings**：`kb_card.embedding` 的宽度来自
`AIWEB_EMBEDDING__DIMENSION`（0005 的 as-built），`embedding_1024` 的宽度是迁移里的字面量
（0008，ADR-0012 对 §2.6"dim 不硬编码在迁移里"那句话的翻案只对第一列成立）。这里键上写的
1536 是对**这台机器建库时那一列的实际宽度**的声明，不是对配置的引用——配置改了不会让列跟着变宽，
真要把第三个维度变成可选，得先有一条迁移，再在这里加一格。顺序反过来的话，用例
`test_映射给出的列名在_ORM_上真的存在` 会先红。
"""

from __future__ import annotations

from typing import Final

#: 维度 → 列名。两格，一一对应 `kb_card` 上那两条 HNSW 索引（§2.6 as-built P4-026）。
_DIMENSION_TO_COLUMN: Final[dict[int, str]] = {
    1536: "embedding",
    1024: "embedding_1024",
}

#: 预置了哪几个维度。给报错文案与漂移门用例用；顺序就是 `_DIMENSION_TO_COLUMN` 的声明顺序。
PREPROVISIONED_DIMENSIONS: Final[tuple[int, ...]] = tuple(_DIMENSION_TO_COLUMN)

#: 库里全部向量列，从上面那份映射**投影**出来（不是第二份清单）。
#: 有它是因为有些动作的对象不是"这一维的那一列"而是"所有向量列"——`kb_card_upsert()`
#: 改文本时要一次洗掉全部向量（旧向量不再描述新内容，留哪一根都是留一份过期数据）。
#: 自己抄一遍列名就等于在这座"只准有一处知道列名"的房子里开第二个窗。
ALL_VECTOR_COLUMNS: Final[tuple[str, ...]] = tuple(_DIMENSION_TO_COLUMN.values())


def vector_column_for(dimensions: int) -> str:
    """这一维的向量落在哪一列；未预置的维度抛 ValueError，不猜、不静默回落到第一列。

    回落到"最近的一列"是最像没坏的形状：写入可能当场被列宽拒掉（那还算好消息），
    也可能列宽恰好兼容而落进一批语义无关的数，此后检索返回的是**看起来正常**的错结果
    （`docs/architecture.md` §5.2 (1) 那一类最坏的失效）。
    """
    column = _DIMENSION_TO_COLUMN.get(dimensions)
    if column is None:
        supported = "、".join(
            f"{dim}→{_DIMENSION_TO_COLUMN[dim]}" for dim in PREPROVISIONED_DIMENSIONS
        )
        raise ValueError(
            f"维度 {dimensions} 没有预置的向量列（kb_card 上现有的：{supported}）。"
            "要么先加一条迁移建这一维的列，要么把档案的维度改成已有的那一格。"
        )
    return column


__all__ = ["ALL_VECTOR_COLUMNS", "PREPROVISIONED_DIMENSIONS", "vector_column_for"]
