"""双层阶段词表里**唯一那一次翻译**：9 值细粒度 `phase` → 5 值粗粒度 `stage`。

接缝选在一个纯函数上，因为工单 017 验收 5 要钉的是"全仓找不到第二处翻译"这件事——
把它写进 `sync_service` 或 `sse.py` 里的任何一处，这条断言就只能靠人肉 grep。期望值
来自两份文档的字面：`docs/metadata-model.md` §2.5 的 phase CHECK（9 值，本轮不动）与
§2.8 的 stage CHECK（5 值，新表建出来的那一格），**不是**从代码里读回来的。

> 为什么四个实体类细值（tables/columns/indexes/fks）归 `upsert` 而不是 `extract`：
> §7 的批语义是"一批（P2 就是一个 schema）一个事务把表/列/索引/关系落进元数据库"，
> 这四个名字说的是**正在写哪一类实体**，读源库的那几条查询在 `collect()` 里已经跑完了。
> 于是 `extract` 这一档只剩 connect/discover 两个值——它们确实还在"对着源库说话"。
> （2026-09-30 拍板：验收 1 要求事件流里出现 upsert，而"映射唯一"不许靠 emit 点写字面解开。）
"""

from __future__ import annotations

import re

import sqlalchemy as sa

from app.core import sync_vocabulary
from app.models.meta import SyncJob

# §2.5 的 9 值 → §2.8 的 5 值，逐个手写（独立真相源，改代码不会让这里跟着变绿）
_EXPECTED: dict[str, str] = {
    "connect": "extract",
    "discover": "extract",
    "tables": "upsert",
    "columns": "upsert",
    "indexes": "upsert",
    "fks": "upsert",
    "card_build": "card_build",
    "embed": "embed",
    "done": "done",
}


def test_九个细值各自落到哪一档是逐条写死的() -> None:
    for phase, stage in _EXPECTED.items():
        assert sync_vocabulary.stage_of(phase) == stage, phase


def test_stage_取值只能是新表_CHECK_里那五个() -> None:
    """粗档的词表就是 `sync_job_event.stage` 的 CHECK：映射函数吐出别的值，写库当场被拒。

    这一条不是多余的谨慎——`upsert` 与 `extract` 都**不在** `sync_jobs.phase` 的 CHECK 里，
    两个词表谁抄谁都会抄出个非法值（roadmap §P3 验收 2 的 as-built 就是在说这件事）。
    """
    allowed = {"extract", "embed", "upsert", "card_build", "done"}
    assert {sync_vocabulary.stage_of(p) for p in _EXPECTED} <= allowed


def test_orm_的_phase_check_里不许有没人认领的细值() -> None:
    """漂移门：给 `sync_jobs.phase` 加第六个粗档没翻译的细值，这里当场红。

    词表从 ORM 的约束里读，而不是再抄一遍字面——抄第二份 9 值清单本身就是这里要避免的重复。
    """
    check = next(
        c
        for c in SyncJob.__table__.constraints
        # 名字过了 `core/db.py` 的命名约定，源码里那句 "phase_allowed" 到这里已是
        # ck_sync_jobs_phase_allowed，所以按后缀认。
        if isinstance(c, sa.CheckConstraint) and (c.name or "").endswith("phase_allowed")
    )
    in_db = set(re.findall(r"'([a-z_]+)'", str(check.sqltext)))
    assert in_db, "读不到 CHECK 的字面，这条断言就白写了"
    for phase in sorted(in_db):
        assert isinstance(sync_vocabulary.stage_of(phase), str), phase


def test_词表外的_phase_要抛错_不许默默归进某一档() -> None:
    """默默回退到 `extract` 的代价是"进度条永远停在第一段"，而那正是最像没坏的形状。"""
    try:
        sync_vocabulary.stage_of("not_a_phase")
    except ValueError as exc:
        assert "not_a_phase" in str(exc), str(exc)
    else:
        raise AssertionError("词表外的 phase 必须抛 ValueError")
