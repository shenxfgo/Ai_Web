"""同步任务的 DTO（architecture §7 的 `POST /sync/jobs`）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SyncJobRequest(BaseModel):
    # §7 的 body 形状只有这一个字段；工单 007 的拍板表把路径定成了 `/api/sync/jobs`
    datasource_id: int


class SyncJobOut(BaseModel):
    """P2 是"同步执行、200 直接带计数"；P3 加 SSE 时改成 202 + `job_id`，路径不动。

    `counters` 不收成固定 schema：它就是 `sync_jobs.counters` 那一格 JSONB 的原样投影，
    §2.5 允许它按方言长字段。前端按 key 取值，缺 key 显示 0。
    """

    job_id: int
    status: str = Field(pattern="^(success|partial|failed)$")
    counters: dict[str, int]
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    duration_ms: int
