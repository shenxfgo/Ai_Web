"""同步任务的 DTO（architecture §7 的 `POST /sync/jobs`）。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, StrictBool


class SyncJobRequest(BaseModel):
    # §7 的 body 形状只有这一个字段；工单 007 的拍板表把路径定成了 `/api/sync/jobs`
    datasource_id: int
    # 工单 021（architecture §7 的 `{"force":false}`）：本片新增的入队字段**只有这一个**——
    # 任何字段都不许映射到 row_limit / max_cell_chars / result_dir（P2 红线沿用）。
    # strict 不是讲究：lax 的 pydantic 会把 "true"/"1" 收成真，而这是"admin 明说过覆盖"
    # 的确认性动作，猜不得——含糊输入一律 422，不落 pending 行。
    force: StrictBool = False


class SyncJobAccepted(BaseModel):
    """`POST /sync/jobs` 在 202 那一帧的响应体：只有 `job_id`（工单 016 验收 1）。

    这里刻意**不**带 status/counters：请求返回时作业还排在队列里，任何计数都是编的。
    """

    job_id: int


class SyncJobOut(BaseModel):
    """作业跑完之后的投影，归详情/SSE 的收尾帧（工单 017）；P2 那种"200 直接带计数"作废。

    `counters` 不收成固定 schema：它就是 `sync_jobs.counters` 那一格 JSONB 的原样投影，
    §2.5 允许它按方言长字段。前端按 key 取值，缺 key 显示 0。
    """

    job_id: int
    status: str = Field(pattern="^(success|partial|failed)$")
    counters: dict[str, int]
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    # 由 `finished_at - started_at` 现算（两个钟都由库生成）。`sync_jobs` 里**没有** duration
    # 这一列——§2.5 从没定过它，worker stdout 上那个 `duration_ms` 是 `SyncOutcome` 的字段，
    # 不落库。所以这里可空：作业还没收尾时它就是 None。
    duration_ms: int | None = None
