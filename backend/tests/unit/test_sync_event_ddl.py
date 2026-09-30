"""`sync_job_event` 的建表契约（§2.8）与 `sync_jobs.force`（§2.5 + 工单 021 的载体列）。

口径来源：`docs/metadata-model.md` §2.8 的 DDL 块原文、§6 as-built(P3) 第 3 条（进度真相在
事件表）、工单 021 的"force 并进迁移 0007，不为一个布尔单开一次迁移"。
期望值全部是文档字面，不是从模型代码反推的。无 PG 时也要验迁移（verification §2.2 第 2 项）。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

import app.models  # noqa: F401  # 先把 ORM 注册进 Base.metadata
from app.core.db import Base


def _ddl(table_name: str) -> str:
    table = Base.metadata.tables[f"{Base.metadata.schema}.{table_name}"]
    return str(CreateTable(table).compile(dialect=postgresql.dialect())).replace("\n", " ")


def _table(table_name: str) -> sa.Table:
    return Base.metadata.tables[f"{Base.metadata.schema}.{table_name}"]


def _constraints(table_name: str) -> list[str]:
    return sorted(str(c.name or "") for c in _table(table_name).constraints)


def test_事件表八列齐全_一格都不能多() -> None:
    assert set(_table("sync_job_event").columns.keys()) == {
        "id",
        "job_id",
        "seq",
        "stage",
        "phase",
        "counters",
        "payload",
        "created_at",
    }


def test_stage_的_CHECK_就是那五档_一个不多一个不少() -> None:
    """§2.8 的 CHECK 原文。这一格是粗档词表在**库里的**落点，与 `stage_of()` 是一对。

    两边字面必须一致，否则映射函数可以往库里写一个当场被拒的值——工单 017 验收 5 的
    "只出现在迁移 CHECK 与 stage_of 里"说的就是这两处。
    """
    ddl = _ddl("sync_job_event")
    assert "stage TEXT NOT NULL" in ddl, ddl
    assert "CHECK (stage IN ('extract','embed','upsert','card_build','done'))" in ddl, ddl
    assert "ck_sync_job_event_stage_allowed" in _constraints("sync_job_event"), "约束名要能被反查"


def test_seq_与_job_id_一起唯一_这一条就是_sse_的游标依据() -> None:
    ddl = _ddl("sync_job_event")
    assert "job_id BIGINT NOT NULL" in ddl, ddl
    assert "seq BIGINT NOT NULL" in ddl, ddl
    assert "UNIQUE (job_id, seq)" in ddl, ddl
    # 唯一的 (job_id, seq) 已经带出一棵 b-tree，正好服务 `WHERE job_id=? AND seq>? ORDER BY seq`，
    # 所以 §2.8 那行单独的 INDEX (job_id, seq) 不再建（2026-09-30 拍板，见工单 017 的偏离节）。
    assert not _table("sync_job_event").indexes, "第二棵同键索引是纯写放大"


def test_phase_可空_counters_与_payload_是_not_null_的_jsonb() -> None:
    ddl = _ddl("sync_job_event")
    # phase 可空：不是每个事件都对应一次 sync_jobs.phase 变化（done 之外的收尾帧也算事件）
    assert "phase TEXT" in ddl and "phase TEXT NOT NULL" not in ddl, ddl
    for column in ("counters", "payload"):
        assert f"{column} JSONB DEFAULT '{{}}'::jsonb NOT NULL" in ddl, (column, ddl)
    assert "created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in ddl, ddl


def test_事件行跟着作业级联删除_不留孤儿() -> None:
    """§2.8 第 ④ 条：作业行被人删掉时事件跟着走。"""
    fks = list(_table("sync_job_event").foreign_keys)
    assert [fk.target_fullname for fk in fks] == ["sync_jobs.id"], fks
    assert [fk.ondelete for fk in fks] == ["CASCADE"], fks


def test_sync_jobs_多了_force_一列_默认_false() -> None:
    """工单 021 的载体：force 必须在**入队时**落到作业行上——worker 是另一个进程，读不到请求。

    默认 false 是这条断言的重点：加了列而默认不是 false，021 之前入队的行在回填时
    会带上"覆盖规模上限"的意思。
    """
    ddl = _ddl("sync_jobs")
    assert "force BOOLEAN DEFAULT false NOT NULL" in ddl, ddl
