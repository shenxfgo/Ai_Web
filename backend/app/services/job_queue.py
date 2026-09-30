"""作业队列（ADR-0011）：API 进程与 worker 进程之间唯一的交接口。

进程分离之后，"发起一次同步"这件事被拆成两处：**入队**在 API 进程（只写一行
`sync_jobs`，然后回 202），**领取并执行**在 worker 进程。这一层就是那条缝——
`JobQueue` 是 Protocol 而不是基类，因为存在的理由是"API 与 worker 之间必须有可替换
的交接口"，不是为了将来换 Redis 预搭架子（介质是 PG，见 ADR-0010）。

语句在这一层造好、也在这一层执行；形状由 `tests/unit/test_job_queue.py` 钉，
行为由 `tests/integration/test_sync_enqueue_pg.py` 钉。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol, cast

from sqlalchemy import Table, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Insert, Update

from app.core.errors import SyncAlreadyRunning
from app.models.meta import SyncJob

# 与 sync_service 同一套理由：没启用 sqlalchemy 的 mypy 插件时 `__table__` 被标成 FromClause
_JOB = cast("Table", SyncJob.__table__)


def enqueue_stmt(datasource_id: int, actor_id: int) -> Insert:
    """入队那一行的语句：只写 pending，钟（`started_at`）留给 claim 那一步。

    不写 `started_at` 是这条缝的关键证据：响应返回时那一列还是 NULL，
    "202 不是假的"就能在用例里断出来（工单 016 验收 1）。
    """
    return (
        pg_insert(_JOB)
        .values(datasource_id=datasource_id, triggered_by=actor_id, status="pending")
        .returning(_JOB.c.id)
    )


def claim_stmt() -> Update:
    """领一个作业：把最老的 pending 改成 running，同时把这一轮的钟点上。

    两处条件各有各的分工，少一处都会出事：

    - 子查询的 `FOR UPDATE SKIP LOCKED`——两个 worker 同时扫队列时各拿一个，
      谁都不等谁（ADR-0011 要的是"多 worker 能并行"，不是"排队串行"）。
    - 外层那句 `AND status = 'pending'`——"我抢到了"由**这条 UPDATE 真正改到了几行**判定。
      行锁只护住语句执行的瞬间，018 的僵尸回收之后会把僵尸改成终局；那时少了这个条件，
      判据就只剩"我动了一行"，两个 worker 会同时抽同一个源。`claim` 读的是 `RETURNING`
      带回的行：PG 保证带回的就是本语句改写的那些行，所以"带回一行"与"更新行数为 1"
      是同一件事，而前者顺带把 `started_at` 一起拿回来（不需要第二条 SQL）。
    """
    picked = (
        select(_JOB.c.id)
        .where(_JOB.c.status == "pending")
        .order_by(_JOB.c.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    return (
        update(_JOB)
        .where(_JOB.c.id == picked, _JOB.c.status == "pending")
        .values(
            status="running",
            phase="connect",
            started_at=func.now(),
            heartbeat_at=func.now(),
        )
        .returning(_JOB.c.id, _JOB.c.datasource_id, _JOB.c.started_at)
    )


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """worker 领到的那一行：钟由库里生成，worker 与 `run_sync` 都以它为"本轮"的起点。"""

    job_id: int
    datasource_id: int
    started_at: dt.datetime


class JobQueue(Protocol):
    """API 侧 `enqueue` / worker 侧 `claim`。

    `subscribe`（进度事件订阅）归工单 017：它要和新表 `sync_job_event` 一起才有意义，
    在这一片挂进 Protocol 就是一个没有人实现的空方法。
    """

    async def enqueue(self, datasource_id: int, actor_id: int) -> int:
        """落一行待办并返回 job_id；同数据源已有未结束作业时抛 `SyncAlreadyRunning`。"""
        ...

    async def claim(self) -> ClaimedJob | None:
        """领走最老的一个待办作业；队列空（或都被别的 worker 抢了）返回 None。"""
        ...


class PostgresJobQueue:
    """`JobQueue` 的 PG 实现——队列介质就是 `sync_jobs` 这张表本身（ADR-0010）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(self, datasource_id: int, actor_id: int) -> int:
        # 单独提交：撞锁要**立刻**回 409 让请求结束，而不是等抽取跑完再说
        try:
            result = await self._session.execute(enqueue_stmt(datasource_id, actor_id))
            job_id = result.scalar_one()
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            # 只认这个索引名：别把 FK、CHECK 之类的约束失败也报成"已经在同步了"
            if "ux_sync_running" in str(getattr(exc, "orig", exc)):
                raise SyncAlreadyRunning("该数据源已有一个未结束的同步任务") from exc
            raise
        return int(job_id)

    async def claim(self) -> ClaimedJob | None:
        result = await self._session.execute(claim_stmt())
        row = result.first()
        # 两条路都要提交：领到了是把 running 与那口钟落定（`run_sync` 会另开语句读它），
        # 没领到是结束事务、放掉子查询那句 FOR UPDATE 可能握住的锁。
        await self._session.commit()
        if row is None:
            # 空队列，或者这一行被别的 worker 在同一瞬间改成了 running：都算"没领到"，
            # 不是错误，也分不出来（不必分——worker 的循环据此安静转下一轮）。
            return None
        return ClaimedJob(
            job_id=int(row.id), datasource_id=int(row.datasource_id), started_at=row.started_at
        )
