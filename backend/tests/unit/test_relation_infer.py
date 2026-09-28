"""§5.3 加权打分公式的落库侧（工单 010 拍板：007 的常数 `0.7` 换成公式）。

期望值全部来自 architecture §5.3 那一行公式的手算，绝不从实现里反算：
`0.35 + 0.25[一侧是PK] + 0.15[类型完全相同] + 0.15[后缀且前缀匹配] + 0.10[表名变体]`。
"""

from __future__ import annotations

from app.services import relation_infer as ri


def _score(**bonuses: bool) -> float:
    """按 §5.3 的四个方括号项打分；没写出来的项一律不加分。"""
    return ri.inferred_confidence(
        target_is_pk=bonuses.get("pk", False),
        identical_type=bonuses.get("type", False),
        suffix_noun_matches=bonuses.get("suffix", False),
        table_name_variant=bonuses.get("variant", False),
    )


def test_一项加成都拿不到时是地板_0_7() -> None:
    # 0.7 曾是 007 落库时的常数；公式化之后它是"下限"，而不是任何一条真实边的分数。
    assert _score() == 0.7


def test_少拿哪一项就掉哪一档() -> None:
    """§5.3 逐项手算（基数 0.35 + 0.25 / 0.15 / 0.15 / 0.10），用"全拿减一项"来钉每一项的权重。

    单独一项加分钉不住：`_score(pk=True)` 的裸算式是 0.6，会被 0.7 的地板吃掉——
    地板本来就是"任何边不会比历史常数更低"的承诺，两者在这一档重叠。
    """
    assert _score(pk=True, type=True, suffix=True) == 0.9  # 少 0.10（表名变体）
    assert _score(pk=True, type=True, variant=True) == 0.85  # 少 0.15（后缀与名词匹配）
    assert _score(pk=True, suffix=True, variant=True) == 0.85  # 少 0.15（类型完全相同）
    assert _score(type=True, suffix=True, variant=True) == 0.75  # 少 0.25（目标是 PK）


def test_掉到地板以下时地板接管() -> None:
    """0.7 是地板不是任何一条真实边的分数：`infer_relations` 的减法规则保证它只在下限处出现。

    裸算式低于 0.7 的组合（如只有 PK 的 0.35+0.25=0.6）一律抬到 0.7——这条钳位是"007 时代
    落库的那批行不会因为重算而变低"的承诺；architecture §5.3 as-built 记着它今天不接管任何边。
    """
    assert _score(pk=True) == 0.7
    assert _score(type=True) == 0.7
    assert _score(type=True, suffix=True) == 0.7  # 裸算式 0.65
