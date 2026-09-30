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
from typing import Any, Final, Protocol, cast

from sqlalchemy import Table, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.sql import Insert, Update

from app.core.errors import SyncAlreadyRunning
from app.core.sse import JobWake
from app.models.meta import SyncJob

# 与 sync_service 同一套理由：没启用 sqlalchemy 的 mypy 插件时 `__table__` 被标成 FromClause
_JOB = cast("Table", SyncJob.__table__)

# 叫醒通道的名字（metadata-model §2.8 ②：payload 只有 job_id，它是闹钟不是真相）。
# 它住在**这条缝上**而不是任何一侧，因为两侧必须拼写一致：发布方是 worker 里的 `sync_service`，
# 订阅方是 API 进程里的 SSE 端点（工单 017），写错一个字母的后果是"进度永远不动"而不是报错。
SYNC_EVENT_CHANNEL: Final = "sync_job_event"


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
    """API 侧 `enqueue` / worker 侧 `claim` / 读侧 `subscribe`。

    三个方法共同构成"跨进程的交接口"这一条缝：前两个换的是**写**的方向（谁去跑作业），
    第三个换的是**读**的方向（谁在听作业）。它们都只认 `job_id`，介质细节一律不外露。
    """

    async def enqueue(self, datasource_id: int, actor_id: int) -> int:
        """落一行待办并返回 job_id；同数据源已有未结束作业时抛 `SyncAlreadyRunning`。"""
        ...

    async def claim(self) -> ClaimedJob | None:
        """领走最老的一个待办作业；队列空（或都被别的 worker 抢了）返回 None。"""
        ...

    async def subscribe(self, job_id: int) -> JobWake:
        """订阅一个作业的叫醒信号，返回句柄；用完必须 `aclose()`。

        只负责"有人敲门"，事实一律回表里读（ADR-0010）：`NOTIFY` 在无监听者那一刻
        永久丢失，把进度寄托在它上面就等于把进度寄托在网络运气上。
        """
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

    async def subscribe(self, job_id: int) -> JobWake:
        """新开一条**专用连接**挂 `LISTEN`，把 payload 对得上的那一声转成敲门。

        为什么不复用 `self._session` 那条连接——这是本机跑出来的结论，不是猜的：会话的连接
        只在**事务期间**归它用。读侧每轮读完都要 `rollback()`（挂着 idle-in-transaction 一条
        几分钟的快照会让 VACUUM 收不掉死元组，而且连接参数一旦换成 REPEATABLE READ 就再也
        读不到新行），那一 rollback 就把连接还给了池：真池子上去是"带着 LISTEN 的连接被下一个
        借用者拿走"，NullPool（测试与 worker）上是"这条连接当场被关掉"。两种都表现为
        进度永远不动，而且都不报错。

        代价是一条流占两条连接（听的那一条 + 读的那一条）。这是有意的取舍：叫醒通道天生
        就该是长命且独占的，而读侧必须是短事务的。

        退订挂在 `wake.attach()` 里而不是返回值里：`remove_listener` 会发 `UNLISTEN`，
        漏了它这条连接就带着一个没人收的闹钟回池。
        """
        # 引擎取的是 `session.bind` 而不是 `session.get_bind()`，这不是随手选的写法：
        # `get_bind()` 在 Session 层就把引擎拆成了**同步** `Engine`（本机实测：`type()` 结果是
        # `sqlalchemy.engine.base.Engine`），对它的 `.connect()` 会在没有 greenlet 的上下文里
        # 发起真 IO，抛 `MissingGreenlet`。`AsyncSession.bind` 才是那个 `AsyncEngine`。
        engine = cast("AsyncEngine", self._session.bind)
        conn = await engine.connect()
        wake = JobWake()

        async def _unsubscribe() -> None:
            try:
                await raw.remove_listener(SYNC_EVENT_CHANNEL, _on_notify)
            finally:
                # UNLISTEN 一失败就跳过 close 的话，这条连接带着监听永远不在池里回来。
                await conn.close()

        def _on_notify(_conn: object, _pid: int, _channel: str, payload: str) -> None:
            # 一个通道上跑着所有作业，靠 payload 认作业；认不上就当没听见。
            # 不许在这里补读表：回调是驱动侧的同步调用，任何 await 都会把这条连接上后来的
            # 事件排在那后面。
            if payload == str(job_id):
                wake.notify()

        wake.attach(_unsubscribe)
        try:
            fairy = await conn.get_raw_connection()
            raw: Any = fairy.driver_connection
            await raw.add_listener(SYNC_EVENT_CHANNEL, _on_notify)
        except Exception:
            # 订阅没挂上就没有东西会来摘它：这一侧不关，连接直接漏出池外
            await conn.close()
            raise
        return wake
