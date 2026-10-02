"""card_build 那两处守卫的唯一决策点（工单 027-C / spec 故事 9 / 决策 8）。

接缝：`card_build_guard(active)` 回一个 `CardBuildDecision`——`profile_id` 非空就写卡（挂那一套
档案），`warning` 非空就不写卡。它是"这次能不能写卡"的唯一答案住在的地方：`sync_cards` 那边
不许再有第二种判断，否则同一个问题两处口径，改一处漏一处。
**只管普通同步**：reindex 那颗作业的靶由 `run_sync` 在调用点直接用 `sync_jobs.reindex_profile_id`，
不进这个函数（工单 029 已定口径）。

期望值口径（工单 027 字面，不是从实现反推）：
- ① 库里没有 active 档案 → 不写卡、不建档案（"没有 active 就自动建一套"那条兜底是被关掉的
  东西本身，不是便利）；
- ② settings 三元组与 active 不一致 → 同样不写卡，追加一条结构化告警；
- 两条都要求 detail **点得出两个值**（配置的与档案的），下一步文案指向 `POST /api/kb/reindex`；
- 已定口径："不写卡 ≠ 作业失败"——所以这个函数只回告警，不回异常、不回状态码；
- 已定口径：比对**不缓存结论**——每次现比，缓存分不清"当时一致"和"后来被改"。

`ActiveProfile` 由调用方从库里读好传进来，本函数只碰 settings 与它，不碰数据库（spec §8 的
测试分层：能在没有 PG 的机器上穷举的判断，就别挂 pg marker）。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from app.services.kb_service import ActiveProfile
from app.services.sync_service import card_build_guard

REINDEX = "POST /api/kb/reindex"


@pytest.fixture
def configured(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Any]:
    """按真实配置通道改"这台机器现在打算用哪套档案"，用完清回缓存。

    走环境变量而不是桩掉 `get_settings`：守卫读的是 Settings 对象，桩只能盖住一次调用，
    盖不住"这条链路上有几个读取点"（020 的分批用例同一条理由）。
    """
    from app.settings import get_settings

    def apply(*, model: str, dimension: int, card_template_version: int) -> None:
        monkeypatch.setenv("AIWEB_EMBEDDING__MODEL", model)
        monkeypatch.setenv("AIWEB_EMBEDDING__DIMENSION", str(dimension))
        monkeypatch.setenv("AIWEB_EXTRACT__CARD_TEMPLATE_VERSION", str(card_template_version))
        get_settings.cache_clear()

    try:
        yield apply
    finally:
        get_settings.cache_clear()


def _active(
    model: str = "text-embedding-3-small", dimensions: int = 1536, tplv: int = 1
) -> ActiveProfile:
    return ActiveProfile(
        index_profile_id=7,
        model=model,
        dimensions=dimensions,
        card_template_version=tplv,
    )


def test_三元组一致时判决就是用那一套档案(configured: Any) -> None:
    """工单 027 验收 4：一致时卡片照常落，`index_profile_id` 等于那一行的 id。"""
    configured(model="text-embedding-3-small", dimension=1536, card_template_version=1)
    active = _active()
    decision = card_build_guard(active)
    assert decision.profile_id == active.index_profile_id
    assert decision.warning is None


def test_没有_active_档案时给一条告警并指得出下一步(configured: Any) -> None:
    """工单 027 涉及层 ① + 验收 3：读不到档案就不写卡，出路只有 reindex。

    这一条刻意在"配置已经就绪"的情况下跑（model/dim 都配好了）：如果实现把"没配向量端点"
    和"库里没有 active 档案"混成一档，运维会被告知去配一个本来就配好了的东西。
    """
    configured(model="text-embedding-3-small", dimension=1536, card_template_version=1)
    decision = card_build_guard(None)
    assert decision.profile_id is None
    assert decision.warning is not None
    assert decision.warning["code"] == "NO_ACTIVE_PROFILE"
    assert REINDEX in decision.warning["detail"], decision.warning
    assert set(decision.warning) == {"code", "detail"}, "告警形状与 RELATION_OUT_OF_SCOPE 同一条"


def test_只改维度时detail点得出两个数(configured: Any) -> None:
    """工单 027 验收 2 的方向一：`AIWEB_EMBEDDING__DIMENSION` 相对 active 档案变了。

    1024 与 1536 都要出现在 detail 里——只说"不一致"等于让运维再跑一次 SQL 去查是哪套。
    """
    configured(model="text-embedding-3-small", dimension=1024, card_template_version=1)
    decision = card_build_guard(_active())
    assert decision.profile_id is None
    assert decision.warning is not None
    assert decision.warning["code"] == "PROFILE_TRIPLET_MISMATCH"
    assert "1024" in decision.warning["detail"] and "1536" in decision.warning["detail"]
    assert REINDEX in decision.warning["detail"], decision.warning


def test_只改模型时detail点得出两个模型名(configured: Any) -> None:
    """工单 027 验收 2 的方向二：换模型不换维度时列宽没变，
    但新旧向量不在同一个语义空间（ADR-0012）。
    """
    configured(model="bge-large-zh-v1.5", dimension=1536, card_template_version=1)
    decision = card_build_guard(_active())
    assert decision.warning is not None
    assert decision.warning["code"] == "PROFILE_TRIPLET_MISMATCH"
    assert "bge-large-zh-v1.5" in decision.warning["detail"]
    assert "text-embedding-3-small" in decision.warning["detail"]


def test_比对不缓存结论_改完配置下一次当场跟着变(configured: Any) -> None:
    """工单 027 已定口径：三元组比对每次现比（对齐 011 那条"缓存分不清当时一致和后来被改"）。

    先跑一次一致的、再把维度改成 1024 跑第二次：若实现把第一次的结论 memo 在模块上，
    第二次会照样判"写卡"，于是"改歪了配置"这一轮照常写卡——正是要拦的那种静默。
    """
    configured(model="text-embedding-3-small", dimension=1536, card_template_version=1)
    assert card_build_guard(_active()).warning is None

    configured(model="text-embedding-3-small", dimension=1024, card_template_version=1)
    assert card_build_guard(_active()).warning is not None
