"""`missing_card_sources()`：整档案上线那把尺的**纯半边**（工单 030 决策 9 / 验收 5）。

接缝为什么切在这里：`kb_service.missing_card_sources()` 收三份"每源表数"的字典、回缺的那些源，
不碰数据库——真库那一面（`meta_table` 的非陈旧表数、`kb_card` 的按表去重行数）在
`tests/integration/test_kb_activate_pg.py` 里验。这一片只验**比较规则**本身，而比较规则里
最容易写错的一格是"比的是哪些源"：它不看"这台机器登记了几个源"，看的是"当前生效那套档案
在哪些源上有卡"。这两者的差就是会被静默缺席伤到的那批源。

`actual > required` 放行那一格（`test_目标档案比尺多出陈旧表的卡不算缺`）在这里而不是只在 pg 侧，
是因为它在真库里要造很久以后的状态（同步把一张表刷成陈旧），而它作为规则本身一眼就能判。
"""

from app.services.kb_service import MissingCardSource, missing_card_sources


def test_目标档案少一张就点名那个源与两个数() -> None:
    missing = missing_card_sources(required={7: 3}, active={7: 3}, target={7: 2})
    assert missing == [MissingCardSource(datasource_id=7, required=3, actual=2)]


def test_目标档案完全没有那个源时实有是零() -> None:
    """整档案激活最常见的那一格：reindex 只跑了一个源，另一个源**一行卡都没有**。

    `target` 里根本没有这个键，而不是键在、值为 0——两条都要走到同一个判定，否则
    "缺一个源"会写成 `KeyError` 或者被 `.get(..., 0)` 的漏网分支放过。
    """
    missing = missing_card_sources(required={7: 2, 8: 2}, active={7: 2, 8: 2}, target={7: 2})
    assert missing == [MissingCardSource(datasource_id=8, required=2, actual=0)]


def test_齐的档案一条都不缺() -> None:
    assert missing_card_sources(required={7: 3}, active={7: 3}, target={7: 3}) == []


def test_尺上没有的源不管_它从来没进过检索面() -> None:
    """比较的是 `active` 的键集（工单口径"源集合 ⊇ **当前 active 有卡片的源集合**"）。

    9 号源登记了、也从没同步进生效档案，它就不在"正在服务检索"的那批源里。把它一起算进来，
    第一次上线就会被一个跟自己无关的源挡在门外，而决策 9 拦的是"切过去之后谁消失了"。
    """
    missing = missing_card_sources(required={7: 1, 9: 5}, active={7: 1}, target={7: 1})
    assert missing == []


def test_目标档案比尺多出陈旧表的卡不算缺() -> None:
    """`actual > required` 放行：多出来的那些只能来自已经变陈旧的表，它不是静默缺席。

    按"相等"拦的话，某个源在 draft 建好之后删过一张表，这套档案就永远上不了线——
    而验收 3 要的恰恰是"旧档案的卡一张都不许动"，卡片比尺多是常态而不是异常。
    """
    missing = missing_card_sources(required={7: 2}, active={7: 3}, target={7: 3})
    assert missing == []


def test_缺的源按_id_升序出去_点名顺序不能靠字典序() -> None:
    """多个源一起缺时顺序是确定的：报错文本直接进 HTTP 响应，字典迭代序会让它漂。"""
    missing = missing_card_sources(
        required={7: 2, 8: 2, 9: 2}, active={9: 2, 8: 2, 7: 2}, target={}
    )
    assert [item.datasource_id for item in missing] == [7, 8, 9]


def test_生效档案自己也不齐时照旧按尺判() -> None:
    """`active` 只出键集，它自己的行数不参与判定：门槛问的是"新那套够不够"。

    这里 active 只有 1 张而尺要 3 张（生效那套本来就没建全），target 建全了 3 张 → 放行。
    反过来若拿 active 的行数当尺，"补齐了的老源"会因为旧基准缺卡而永远上不了线。
    """
    missing = missing_card_sources(required={7: 3}, active={7: 1}, target={7: 3})
    assert missing == []
