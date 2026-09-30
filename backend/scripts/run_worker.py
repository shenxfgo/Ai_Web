"""同步作业的 worker 进程（ADR-0011）：领作业 → 跑 `run_sync` → 终局落在 `sync_jobs` 那一行。

本机从此是两条命令：`dev.ps1 dev` 起 API，`dev.ps1 worker` 起这一份。只起一条的后果是
队列里的作业永远停在 pending——那正是这一片要换来的东西：请求不再等抽取，所以必须
有人替它等。

用例不真起子进程（工单 016 的已定口径），它们 await 的 `run_once` 就是这个常驻循环
每一轮所做的事：进程边界本身另有真进费用例，归 017。

退出码：0 正常退出（Ctrl+C）；1 循环里出了不该出的错——但那必须是打印过原因的。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

if sys.platform == "win32":  # asyncpg 与 Proactor 事件循环不兼容
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.core.db import dispose_engine, get_sessionmaker  # noqa: E402
from app.core.logging import reconfigure_std_streams  # noqa: E402
from app.models.datasource import DataSource  # noqa: E402
from app.services import sync_service  # noqa: E402
from app.services.job_queue import PostgresJobQueue  # noqa: E402

# 空队列时的扫描间隔。017 上了 NOTIFY 之后这个数只兜"叫醒丢了"的情况，
# 现在它是唯一的推进动力，所以宁可短一点：本机手测不该等半分钟才看到作业动。
IDLE_SECONDS = 2.0


async def run_once(session: AsyncSession) -> int | None:
    """领一个作业并跑完它，返回 job_id；队列空返回 None。

    会话由调用方给、也由调用方关：这一层不拥有连接，才不会把"一个作业一个会话"和
    "一个会话跑完所有作业"两种形状混在一个函数里——018 的心跳要在**另一条连接**上刷，
    到时候看的就是这里到底谁握着会话。

    作业级的异常在这里收，不在 `loop()` 里收，因为这一句是整个切片的进入方式：用例直接
    await 本函数（工单 016 已定口径"不真起子进程"），而 `run_sync` 的不变量恰恰是
    "终局写完之后把原因原样抛出去"。收在 loop 里，用例跑的那条路就少了一层真进程有的保护。
    """
    claimed = await PostgresJobQueue(session).claim()
    if claimed is None:
        return None
    ds = await session.get(DataSource, claimed.datasource_id)
    if ds is None:
        # 只在一条窄路上发生：claim 提交之后、这一句之前有人删了源。`sync_jobs.datasource_id`
        # 是 ON DELETE CASCADE，job 行跟着走了——没有终局可写，也没有下一轮被它挡住。
        # 这里只报一句、不抛：抛出去等于让 `run_sync` 那条"必有终局"的不变量之外多一种死法。
        print(f"[job {claimed.job_id}] 数据源已删除，跳过")
        return claimed.job_id
    try:
        outcome = await sync_service.run_sync(
            session,
            ds,
            job_id=claimed.job_id,
            synced_before=claimed.started_at,
            # 工单 021：入队那一刻的"覆盖规模上限"由作业行带出来——worker 进程读不到请求，
            # 没有这一格，端点收到的 force 就死在 sync_jobs 那一列里。
            force=claimed.force,
        )
    except Exception as exc:
        # 走到这里终局一定已经写好（`run_sync` 的不变量 1），所以不需要补救，但要把原因
        # 打出来接着转——一个作业把常驻进程带走，此后所有源的同步都会停在 pending。
        print(f"[job {claimed.job_id}] 异常（终局已由 run_sync 写好）：{type(exc).__name__}: {exc}")
        return claimed.job_id
    print(f"[job {outcome.job_id}] status={outcome.status} duration={outcome.duration_ms}ms")
    print(f"          counters={outcome.counters}")
    for err in outcome.errors:
        print(f"          error {err['code']}: {err['detail']}")
    return outcome.job_id


async def loop() -> None:
    """有活干就接着干，没活干睡 `IDLE_SECONDS`。"""
    print(f"[worker] 开始消费同步队列（空转每 {IDLE_SECONDS:g}s 扫一次，Ctrl+C 退出）")
    while True:
        session = get_sessionmaker()()
        try:
            ran = await run_once(session)
        finally:
            await session.close()
        await asyncio.sleep(0 if ran is not None else IDLE_SECONDS)


if __name__ == "__main__":
    reconfigure_std_streams()
    try:
        asyncio.run(loop())
    except KeyboardInterrupt:
        # Ctrl+C 落在 await 上时那一轮的 job 停在 running，靠 018 的僵尸回收兜住——
        # 这里不假装能优雅收尾，只把话说清楚。
        print("\n[worker] 已退出")
    finally:
        asyncio.run(dispose_engine())
