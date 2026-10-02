""" "配置那一套" 与 "库里 active 那一套" 之间唯一的比对口径（工单 027 / ADR-0013 / spec 故事 9）。

接缝：`triplet_drift()` 是纯函数，不碰库也不读 settings——它是同步链路上唯一回答
"这次能不能写卡"的地方，所以它必须能在没有 PG 的机器上被穷举（spec §8 的测试分层）。

期望值口径（文档字面，不是从实现反推）：
- 三元组 = `(model, dimensions, card_template_version)`，就是 `profile_name()` 的那三段
  （`docs/metadata-model.md` §2.6）。比对逐格进行，**任何一格不一致都不写卡**（工单 027 已定口径：
  不写卡 ≠ 作业失败）。
- 告警文案必须点得出**两个数**（工单 027 验收 2："配置的与档案的"）。只说"不一致"的告警等于
  让运维再跑一次 SQL 去查是哪个配置改歪了。
- 顺序稳定（模型 → 维度 → 卡片模板版本）：这条 detail 会进 `sync_jobs.warnings` 与事件流，
  同一个错误配置改两格时，两次同步该给出可以逐字对比的两条告警，而不是同一句话的两种排列。
"""

from __future__ import annotations

from app.services.kb_service import Triplet, triplet_drift


def test_三格全一致时没有漂移() -> None:
    """spec 故事 9 的反面：一致才写卡。空表就是"可以往下走"的唯一信号。"""
    active: Triplet = ("text-embedding-3-small", 1536, 1)
    assert triplet_drift(configured=active, active=active) == []


def test_只改维度时那条要点得出两个数() -> None:
    """工单 027 验收 2 的方向一：`AIWEB_EMBEDDING__DIMENSION` 相对 active 档案变了。

    1024 与 1536 两个数都必须在文案里——只写"维度不一致"的那条告警，读的人仍然不知道
    库里是哪套、配置里是哪套。
    """
    drift = triplet_drift(
        configured=("text-embedding-3-small", 1024, 1),
        active=("text-embedding-3-small", 1536, 1),
    )
    assert len(drift) == 1, drift
    assert "维度" in drift[0], drift[0]
    assert "1024" in drift[0] and "1536" in drift[0], drift[0]


def test_只改模型时那条要点得出两个模型名() -> None:
    """工单 027 验收 2 的方向二：`AIWEB_EMBEDDING__MODEL` 变了，维度没变。

    这一格最容易被漏：换模型不换维度时向量列宽没变，"看起来什么都不用重建"，
    但新旧向量根本不在同一个语义空间里（ADR-0012 要第二列的正是这件事）。
    """
    drift = triplet_drift(
        configured=("bge-large-zh-v1.5", 1536, 1),
        active=("text-embedding-3-small", 1536, 1),
    )
    assert len(drift) == 1, drift
    assert "模型" in drift[0], drift[0]
    assert "bge-large-zh-v1.5" in drift[0] and "text-embedding-3-small" in drift[0], drift[0]


def test_只改卡片模板版本时那条要点得出两个版本号() -> None:
    """三段里第三段是 `card_template_version`：模板正文改了，旧卡片就是旧模板的产物。"""
    drift = triplet_drift(
        configured=("text-embedding-3-small", 1536, 2),
        active=("text-embedding-3-small", 1536, 1),
    )
    assert len(drift) == 1, drift
    assert "卡片模板版本" in drift[0], drift[0]
    assert "2" in drift[0] and "1" in drift[0], drift[0]


def test_三格全不一致时给三条且顺序固定() -> None:
    """顺序是文案契约的一部分：模型 → 维度 → 卡片模板版本。"""
    drift = triplet_drift(
        configured=("bge-large-zh-v1.5", 1024, 2),
        active=("text-embedding-3-small", 1536, 1),
    )
    assert len(drift) == 3, drift
    # 按前缀断而不是按分隔符切：`split("：")` 把期望绑在实现的文案标点上，改标点就假红
    for line, label in zip(drift, ("模型", "维度", "卡片模板版本"), strict=True):
        assert line.startswith(label), (label, line)
