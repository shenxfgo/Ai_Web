"""同步作业的 worker 进程（ADR-0011）：领作业 → 跑 `run_sync` → 终局落在 `sync_jobs` 那一行。

同一个进程还负责三件**没有作业也在做**的事（工单 018）：在另一条连接上每
`heartbeat_interval_s` 刷一次"我这个作业还活着"、每轮顺手把心跳超时的僵尸行改判 `failed`
（`errors` 追加 `reclaimed`，并给它补一条终局事件让在读流的前端能收尾）、每轮按
`retention_days` 清 `sync_job_event` 的旧行。这三件事都住在这份文件里，因为它们的主语是
"常驻进程"，不是"某一次同步"。

本机从此是两条命令：`dev.ps1 dev` 起 API，`dev.ps1 worker` 起这一份。只起一条的后果是
队列里的作业永远停在 pending——那正是这一片要换来的东西：请求不再等抽取，所以必须
有人替它等。

用例不真起子进程（工单 016 的已定口径），它们 await 的 `run_once` 就是这个常驻循环
每一轮所做的事：进程边界本身另有真进费用例，归 017。

退出码：0 正常退出（Ctrl+C）；1 循环里出了不该出的错——但那必须是打印过原因的。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final, cast

from sqlalchemy import ColumnElement, Table, delete, func, literal, literal_column, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

if sys.platform == "win32":  # asyncpg 与 Proactor 事件循环不兼容
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.core.db import create_engine, dispose_engine, get_sessionmaker  # noqa: E402
from app.core.logging import reconfigure_std_streams  # noqa: E402
from app.models.datasource import DataSource  # noqa: E402
from app.models.meta import SyncJob, SyncJobEvent  # noqa: E402
from app.services import sync_service  # noqa: E402
from app.services.job_queue import PostgresJobQueue  # noqa: E402
from app.settings import Settings, get_settings  # noqa: E402

# 空队列时的扫描间隔。017 上了 NOTIFY 之后这个数只兜"叫醒丢了"的情况，
# 现在它是唯一的推进动力，所以宁可短一点：本机手测不该等半分钟才看到作业动。
IDLE_SECONDS = 2.0

# 与 sync_service 同一份手法：`DeclarativeBase.__table__` 在没开 sqlalchemy 的 mypy 插件下
# 被标成 FromClause，逐个 cast 才不会让每一条语句构造都报一次类型错。
_JOB = cast("Table", SyncJob.__table__)
_EVENT = cast("Table", SyncJobEvent.__table__)

# metadata-model §6"进程崩溃留下僵尸 running"那一格给的**字面量**，逐字抄文档。
# 它同时是 jsonb 数组拼接的右操作数和终局事件的 payload——同一份文字只写一次。
RECLAIM_ENTRY: Final[dict[str, str]] = {"code": "reclaimed", "detail": "heartbeat 超时"}
RECLAIM_ENTRY_JSON: Final = json.dumps([RECLAIM_ENTRY], ensure_ascii=False, separators=(",", ":"))

# 拼接的右操作数写成 jsonb 字面量（§6 那一行的 `'…'::jsonb`）。文档给的是带引号的字面量，
# 这里就发字面量而不是绑参：`RECLAIM_ENTRY_JSON` 是本模块常量，不是外部输入。
_RECLAIM_ENTRY_JSONB: Final = literal(RECLAIM_ENTRY_JSON).cast(JSONB)


def _make_interval(**parts: int) -> ColumnElement[datetime]:
    """PG 的 `make_interval(secs => N)` / `make_interval(days => N)`。

    为什么不是 `func.make_interval(secs=N)`：SQLAlchemy 把 `func.x(**kwargs)` 的 kwargs 当成
    **函数构造选项**而不是 SQL 的命名参数，直接 `TypeError`。而 `text()` 绑参在
    `literal_binds` 编译下渲染不出数字（单测要的就是"换一个入参、文本里的数跟着换"）。
    插进 SQL 文本的唯一代价是必须自己保证它是整数。闸门是 `isinstance` 而不是 `int()`：
    后者会把 180.7 **静默截成** 180、把 `True` 当成 1，而"阈值少了一秒"这件事在编译出来的
    文本里跟本来就该是 180 长得一模一样。收非整数当场抛，不让它进语句。
    """
    unknown = set(parts) - {"years", "months", "weeks", "days", "hours", "mins", "secs"}
    if unknown:
        raise ValueError(f"make_interval 没有这些参数：{sorted(unknown)}")
    for name, value in parts.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(
                f"make_interval 的 {name} 要整数，收到 {type(value).__name__}: {value!r}"
            )
    rendered = ", ".join(f"{name} => {value}" for name, value in parts.items())
    return literal_column(f"make_interval({rendered})")


def heartbeat_update_stmt(job_id: int):
    """刷心跳：只动 `heartbeat_at` 那一格，且只碰未结束的行。

    `status IN ('pending','running')` 是竞态的兜底：收尾与心跳分属两个事务，收尾先落定时
    这一句必须打不中——给已结束的作业刷钟，等于用一条 UPDATE 把"什么时候结束的"再搅浑一次。
    """
    return (
        update(_JOB)
        .where(_JOB.c.id == job_id, _JOB.c.status.in_(("pending", "running")))
        .values(heartbeat_at=func.now())
    )


def reclaim_update_stmt(stale_seconds: int):
    """僵尸回收：改判 `failed`、向 `errors` 追加一条、把 `finished_at` 点上，并交出被改写的 id。

    三处缺一不可，逐条对应 §6 那一行原文：

    ① `pending` 也在候选里（已定口径），但判据是**心跳超时**而不是"在队列里躺久了"——
      从没被领取的行 `heartbeat_at` 是 NULL，比较匹配不上 NULL，天然排除。
    ② 追加走 `errors || …::jsonb`：`sync_jobs` **没有** `error` 这一列，覆盖写会把这一轮
      之前攒下的失败原因抹掉。
    ③ `RETURNING id` 让"我回收了哪些"由这一条语句自己回答。两个 worker 同扫时后到的那个
      拿到空列表，终局事件因此不会被重复补写。
    """
    return (
        update(_JOB)
        .where(
            _JOB.c.status.in_(("pending", "running")),
            _JOB.c.heartbeat_at < func.now() - _make_interval(secs=stale_seconds),
        )
        .values(
            status="failed",
            errors=_JOB.c.errors.op("||")(_RECLAIM_ENTRY_JSONB),
            finished_at=func.now(),
        )
        .returning(_JOB.c.id)
    )


def retention_delete_stmt(retention_days: int):
    """保留期：只删 `sync_job_event` 的旧行。

    删作业行不是保留期而是灭迹（终局连同原因一起销毁），所以这条语句里不许出现 `sync_jobs`；
    `data/results/` 下的 csv 一个字节都不动——删文件不可逆，而 P2 验收 4/5 的现场证据就在里面。
    """
    return delete(_EVENT).where(
        _EVENT.c.created_at < func.now() - _make_interval(days=retention_days)
    )


@dataclass(slots=True)
class _Current:
    """循环与心跳任务之间共享的那一格：这个进程**此刻**在跑哪个作业。

    心跳任务不知道主循环领到了谁，主循环也不等心跳——两边只经这一份状态见面。
    `own_job_id` 是必要的而不是锦上添花：回收扫描不认识"是我自己在跑"，
    少了这一格，一个跑得比阈值久的作业会被自己的 worker 判成僵尸并改判 failed。
    """

    job_id: int | None = None

    def take(self, job_id: int) -> None:
        self.job_id = job_id

    def release(self) -> None:
        self.job_id = None


@dataclass(slots=True)
class HeartbeatReport:
    """一轮心跳的三件成果，用例直接读这三个数。"""

    own_beaten: int = 0
    reclaimed: list[int] = field(default_factory=list)
    pruned: int = 0


def cadence(settings: Settings) -> dict[str, int]:
    """三个配置键 → `beat_forever` 的三个入参，映射只住这一处。

    单独抽出来是为了让"键不许串位"这件事有用例可钉：三个键给成互不相同的值，断带回来的三个
    名字对上三个数。留在 `loop()` 里写三行的话，把 `interval_s` 喂成 `stale_job_reclaim_s`
    在所有既有用例里都是绿的——而已定口径第 2 条管的正是"哪个键喂给哪个参数"。
    """
    return {
        "interval_s": settings.extract.heartbeat_interval_s,
        "stale_seconds": settings.extract.stale_job_reclaim_s,
        "retention_days": settings.result.retention_days,
    }


async def heartbeat_tick(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    own_job_id: int | None,
    stale_seconds: int,
    retention_days: int,
) -> HeartbeatReport:
    """心跳循环的一轮：刷自己的钟 → 扫僵尸 → 清过期事件。

    **会话由调用方给的工厂现开**，这是验收 1 的全部内容：`run_sync` 每库一个事务，那个事务
    还没提交时它写不出"我还活着"，所以钟必须由**另一条连接**去刷，前端才看得到动静。
    复用主会话的话，心跳就排在主事务后面，进度条永远不动。

    三步各自 commit：刷钟不该等回收，回收补的那条终局事件不该等保留期。
    回收与它的终局事件在**同一个事务**里——分开提交会留下"已改判 failed、读侧却还等不到
    最后一帧"的窗口，而那正是 017 交付的流式读侧最怕的形状。
    """
    report = HeartbeatReport()
    async with session_factory() as session:
        if own_job_id is not None:
            beaten = await session.execute(heartbeat_update_stmt(own_job_id))
            report.own_beaten = int(beaten.rowcount or 0)
            await session.commit()

        reclaimed = (await session.execute(reclaim_update_stmt(stale_seconds))).scalars().all()
        report.reclaimed = [int(job_id) for job_id in reclaimed]
        for job_id in report.reclaimed:
            await sync_service.append_terminal_event(session, job_id, dict(RECLAIM_ENTRY))
        await session.commit()

        pruned = await session.execute(retention_delete_stmt(retention_days))
        report.pruned = int(pruned.rowcount or 0)
        await session.commit()
    return report


async def beat_forever(
    session_factory: async_sessionmaker[AsyncSession],
    beating: _Current,
    *,
    interval_s: int,
    stale_seconds: int,
    retention_days: int,
) -> None:
    """每 `interval_s` 走一轮心跳——"每约 10s 变化一次"这句话的主语就是这里。

    三个数全部来自 `Settings`，"哪个键喂给哪个参数"只写在 `cadence()` 一处：写死一个数，
    配置键就又一次只剩定义点没有读取点（P2 遗留的那本账），而串位（`interval_s` 吃到
    `stale_job_reclaim_s`）在所有既有用例里都是绿的——`cadence` 就是让这两件事有地方可钉。

    出错只打印、不带走任务。心跳停了之后，正在跑的作业会在 `stale_seconds` 后被**别的**
    worker 判成僵尸并改判 failed——那比"这一轮没刷上"糟得多，所以这一层必须活着转下去。
    """
    while True:
        try:
            report = await heartbeat_tick(
                session_factory,
                own_job_id=beating.job_id,
                stale_seconds=stale_seconds,
                retention_days=retention_days,
            )
        except Exception as exc:
            print(f"[worker] 心跳轮出错（继续下一轮）：{type(exc).__name__}: {exc}")
        else:
            if report.reclaimed:
                print(f"[worker] 回收 {len(report.reclaimed)} 个僵尸 job：{report.reclaimed}")
        await asyncio.sleep(interval_s)


async def run_once(
    session: AsyncSession,
    *,
    card_build_hook: Callable[[str], None] | None = None,
    on_claim: Callable[[int], None] | None = None,
) -> int | None:
    """领一个作业并跑完它，返回 job_id；队列空返回 None。

    会话由调用方给、也由调用方关：这一层不拥有连接，才不会把"一个作业一个会话"和
    "一个会话跑完所有作业"两种形状混在一个函数里——018 的心跳要在**另一条连接**上刷，
    到时候看的就是这里到底谁握着会话。

    `card_build_hook` 是工单 019 的 test-only 注入点，原样透传给 `run_sync`：注入只有从
    这个进入点进来才等于走真链路（HTTP → 入队 → worker 跑 → 落库）。常驻循环 `loop()`
    不传它，生产路径恒为 None。

    作业级的异常在这里收，不在 `loop()` 里收，因为这一句是整个切片的进入方式：用例直接
    await 本函数（工单 016 已定口径"不真起子进程"），而 `run_sync` 的不变量恰恰是
    "终局写完之后把原因原样抛出去"。收在 loop 里，用例跑的那条路就少了一层真进程有的保护。
    """
    claimed = await PostgresJobQueue(session).claim()
    if claimed is None:
        return None
    if on_claim is not None:
        # 领取成功的那一刻是把钟交给心跳任务的唯一时机：再晚一步（比如等 `run_sync` 起了
        # 事务）,"作业在进行中"这段时间就没有钟可刷，而 180s 的回收阈值恰恰按这段计时。
        on_claim(claimed.job_id)
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
            # 工单 019：卡片构建的 test-only 注入点从进入点穿到 sync_cards，生产 loop() 不传。
            card_build_hook=card_build_hook,
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
    """有活干就接着干，没活干睡 `IDLE_SECONDS`；心跳与回收在**另一条连接**上并行跑。

    心跳必须是一个独立的并发任务，而不是"跑完一个作业之后顺手刷一次"：`run_sync` 每库一个
    事务，那个事务提交之前主会话什么都读不到，而"作业还在动"这件事偏偏要在它提交之前就被看见
    （工单 018 验收 1）。所以这里开第二条引擎（NullPool：每次现开一条连接、还回去就关掉），
    与主会话**结构上不可能**共用同一条连接——不赌池子什么时候会把同一条借出去。
    """
    settings = get_settings()
    beating = _Current()
    hb_engine = create_engine(poolclass=NullPool)
    hb_factory = async_sessionmaker(hb_engine, expire_on_commit=False)
    beat_params = cadence(settings)
    beat = asyncio.create_task(beat_forever(hb_factory, beating, **beat_params))
    print(
        f"[worker] 开始消费同步队列（空转每 {IDLE_SECONDS:g}s 扫一次，"
        f"心跳每 {beat_params['interval_s']:g}s 一次、"
        f"回收阈值 {beat_params['stale_seconds']:g}s、"
        f"事件保留 {beat_params['retention_days']:g} 天，Ctrl+C 退出）"
    )
    try:
        while True:
            session = get_sessionmaker()()
            try:
                ran = await run_once(session, on_claim=beating.take)
            finally:
                await session.close()
                beating.release()
            await asyncio.sleep(0 if ran is not None else IDLE_SECONDS)
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
        await hb_engine.dispose()


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
