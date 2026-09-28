"""`token_estimate` 的启发式口径（来源：`docs/kb-workflow.md` §6）。

原文：`CJK 字符数×1 + ASCII 词数×1.3`，并且**写入路径不引真 tokenizer**
（`tiktoken` 只在最后组装 prompt 时精算裁切）。

期望值全是手算的字面量，不是拿同一个公式再算一遍。向上取整是因为这个数用来卡预算，
宁可高估也不能让一份卡片文本挤爆 `AIWEB_RETRIEVAL__TOKEN_BUDGET`。
"""

from __future__ import annotations

from app.services.token_estimate import estimate_tokens


def test_纯中文按每字一个_token() -> None:
    assert estimate_tokens("订单金额") == 4


def test_三个英文词是_点三九向上取整() -> None:
    # 3 × 1.3 = 3.9 → 4
    assert estimate_tokens("pay amount total") == 4


def test_中英混排分别计数后相加() -> None:
    # ASCII 词 pay_amount + decimal = 2 → 2.6；汉字 4 → 合计 6.6 → 7
    assert estimate_tokens("pay_amount 实付金额 decimal") == 7


def test_标点与空白不单独成词() -> None:
    # 只有 amount 一个词 → 1.3 → 2
    assert estimate_tokens("(amount),") == 2


def test_空串是零() -> None:
    assert estimate_tokens("") == 0


def test_文本变长则估计不减() -> None:
    """§2.1 的 ③「单调：文本变长则估计不减」。

    写成前缀链而不是两两比较：这个数用来卡预算，如果"多加一列注释"反而让卡片
    从 901 掉回 899，分块判定就会随内容抖，切片数量在两次同步之间来回变。
    """
    for chain in (
        # 纯中文：逐字加长
        ["", "订", "订单", "订单金额", "订单实付金额"],
        # 纯英文：逐词加长（`total` 让词数与 1.3 的乘法一起变大）
        ["", "pay", "pay amount", "pay amount total"],
        # 中英混排：交替加长，覆盖两类字符同时增长的分支
        ["", "pay_amount", "pay_amount 实付", "pay_amount 实付金额", "pay_amount 实付金额 decimal"],
    ):
        values = [estimate_tokens(text) for text in chain]
        assert values == sorted(values), f"{chain} → {values}"
