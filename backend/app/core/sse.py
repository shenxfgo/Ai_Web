"""SSE 帧格式与同步进度流（`docs/architecture.md` §2 的 `core/sse.py`、§6.2 的帧与心跳）。

这一层只做三件事：把事件表读成帧、在没有新事件时按时发心跳、给订阅方一个"敲门"的句柄。
它**不认识** PG 的 `LISTEN`——`JobWake` 只是句柄的形状，置位它的回调住在
`services/job_queue.subscribe` 里（依赖方向是 services → core，core 这一侧只 import
`sync_vocabulary` 那一个纯词表模块，而它同层住在 `core/` 且只 import `typing`：
换掉队列介质时这一层不用改）。

为什么叫醒句柄用 `Event` 而不是 `Queue`：一个作业被叫醒十次和一次要做的事完全相同
（把 `seq > cursor` 的行读完），去重语义正好由 `Event` 免费提供；用 Queue 的话
每条 NOTIFY 都要再空跑一次 SELECT，而 PG 一次能广播无数条。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, cast

from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sync_vocabulary import is_terminal_stage
from app.models.meta import SyncJob, SyncJobEvent

_EVENT = cast("Table", SyncJobEvent.__table__)
_JOB = cast("Table", SyncJob.__table__)

# §6.2：无新事件时每 15s 至少一帧注释行，否则远端链路 idle 超时静默断流，
# 前端表现为"卡住"而不是"慢"。用例靠 monkeypatch 这个常量把窗口设小，不真等 15s。
PING_INTERVAL_S: Final = 15.0

# 断线重连的间隔提示。工单 017 写的是 `Retry-After`，SSE 里对应的机制是**帧字段**
# `retry: <毫秒>`（浏览器收到即生效，不需要 HTTP 头），所以这里发的是帧而不是头。
RETRY_MS: Final = 3000

# 帧名。`done` 在这里是**帧名**而不是粗档（终局档的判定走 `sync_vocabulary.is_terminal_stage`），
# 与 §6.1 的问数流同名是有意的：一条 SSE 流的最后一帧在这个项目里就叫 done。
EVENT_PROGRESS: Final = "progress"
EVENT_DONE: Final = "done"

# §6.2 要求生成器里冒出去的异常转成帧；帧名与 HTTP envelope 的 `error` 同词，
# 前端解帧时不必认两套。
EVENT_ERROR: Final = "error"


def sse_format(event: str, data: dict[str, Any], *, event_id: int | None = None) -> str:
    """一帧的字节：`[id: …\\n]event: …\\ndata: <json>\\n\\n`（§6.2 的严格形状）。

    `data` 只用一行：SSE 允许 data 跨行，但 JSON 编码之后里面没有裸换行，分帧交给
    `json.dumps` 就够。`ensure_ascii=False` 是为了让 `payload.detail` 那些中文在人
    工 curl 的现场仍然读得动。
    """
    lines = [] if event_id is None else [f"id: {event_id}"]
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"


def sse_ping() -> str:
    """心跳那一帧：注释行（以冒号开头），浏览器按规范忽略内容、只当连接还活着。"""
    return ": ping\n\n"


def sse_retry() -> str:
    return f"retry: {RETRY_MS}\n\n"


class JobWake:
    """一次订阅的句柄：实现方在驱动回调里 `notify()`，消费方在流里 `wait()`。

    它传的是"去看表"这个指示，**不是事实**——`NOTIFY` 在无监听者那一刻永久丢失
    （ADR-0010），所以只许它敲门。`aclose()` 必须由消费方的 `finally` 调到：连接是池化的，
    带着 LISTEN 回池等于把下一个请求的叫醒信号接到上一个作业上。
    """

    def __init__(self) -> None:
        self._ready = asyncio.Event()
        self._on_close: Callable[[], Awaitable[None]] | None = None

    def attach(self, on_close: Callable[[], Awaitable[None]]) -> None:
        """实现方注册退订动作（`remove_listener` 那一类）。

        为什么是"先造句柄、再回头注册"而不是构造参数：驱动回调要能闭包引用这个句柄，
        而回调又必须先于 `add_listener` 定义好——构造参数会把这两件事拧成死结。
        消费方仍然只认 `aclose()`，不需要知道退订动作长什么样。
        """
        self._on_close = on_close

    def notify(self) -> None:
        # 同步方法：asyncpg 的监听回调不是协程，置位必须不 await 也做得完。
        self._ready.set()

    async def wait(self, timeout_s: float) -> bool:
        """等到叫醒返回 True；超时返回 False（调用方据此发一帧心跳）。"""
        try:
            await asyncio.wait_for(self._ready.wait(), timeout_s)
        except TimeoutError:
            return False
        return True

    def clear(self) -> None:
        self._ready.clear()

    async def aclose(self) -> None:
        if self._on_close is not None:
            await self._on_close()


@dataclass(frozen=True, slots=True)
class EventRow:
    """事件表的一行，只带渲染帧要用的那几列。"""

    seq: int
    stage: str
    phase: str | None
    counters: dict[str, Any]
    payload: dict[str, Any]


async def read_events(session: AsyncSession, job_id: int, after_seq: int) -> list[EventRow]:
    """按 `seq > cursor ORDER BY seq` 读出还没发出去的事件（§2.8 的游标语义）。

    这条查询就是"断线重连一条不丢"的全部机制：游标是客户端带来的，表是 append-only 的，
    所以补读不需要服务端记任何状态。走 `UNIQUE (job_id, seq)` 那棵 b-tree。
    """
    rows = (
        await session.execute(
            select(
                _EVENT.c.seq,
                _EVENT.c.stage,
                _EVENT.c.phase,
                _EVENT.c.counters,
                _EVENT.c.payload,
            )
            .where(_EVENT.c.job_id == job_id, _EVENT.c.seq > after_seq)
            .order_by(_EVENT.c.seq)
        )
    ).all()
    return [
        EventRow(
            seq=int(row.seq),
            stage=str(row.stage),
            phase=row.phase,
            counters=dict(row.counters),
            payload=dict(row.payload),
        )
        for row in rows
    ]


def progress_frame(row: EventRow) -> str:
    """一帧 `event: progress`。

    负载是**摊平**的：库里把计数收在 `counters` 一格里（jsonb 好演进、§2.8 就是这么定的），
    而 spec 故事 7 要的是 `{stage, done, total, base_table, view}` 五个键在顶层。两套形状
    之间只在这里转换一次——端点里再展一遍就会出现"库里改了帧没改"那种分叉。
    """
    data: dict[str, Any] = {"stage": row.stage, "phase": row.phase, **row.counters}
    data["payload"] = row.payload
    return sse_format(EVENT_PROGRESS, data, event_id=row.seq)


async def job_is_finished(session: AsyncSession, job_id: int) -> bool:
    """这一轮作业收没收尾——判据是 `finished_at` 那一格有没有被点上。

    为什么不用 `status`：终局那三个状态值（success/partial/failed）在 `schemas/sync.py` 的
    pattern 里已经有一份，读侧再抄一份就是"两处规则可以各自漂移"的老路。而 `finished_at`
    与终局那条事件行是**同一条 UPDATE、同一个事务**写下的（`sync_service` 的 `finally`），
    所以"点过钟"与"事件流里有最后一行"是同一件事的两种写法，判哪个都对，判钟最便宜。

    作业行不存在也算"收尾"：那一行被删掉了（`sync_job_event` 对它 CASCADE，事件也跟着没了），
    这条流再继续等就是在等一个已经不存在的东西。
    """
    finished_at = (
        await session.execute(select(_JOB.c.finished_at).where(_JOB.c.id == job_id))
    ).scalar_one_or_none()
    return finished_at is not None


async def stream_job_events(
    session: AsyncSession,
    wake: JobWake,
    job_id: int,
    cursor: int,
    *,
    ping_after_s: float | None = None,
) -> AsyncIterator[str]:
    """事件表 → SSE 帧的异步生成器：先读表、再等叫醒，发完终局那一行就收。

    顺序是这条流的正确性所在：

    ① **先清位再读表**。清位与读表之间到达的叫醒会把位重新置上，下一轮循环立刻再读一次，
       于是"读完了才敲门"这种丢事件的窗口不存在。反过来（先等再读）就会漏：NOTIFY 落在
       两次读之间的话，没人再叫门，那一行只能等到下一次心跳才吐出去。
    ② 表永远是真相，叫醒只是加速。一次叫醒都没收到（订阅之前作业就跑完了）也只是慢一轮，
       不会少一帧——这正是 ADR-0010 选事件表而不是纯推 NOTIFY 的理由。
    ③ 结束条件有两个，都只认库里的事实：发过终局档那一行就收（`run_sync` 的收尾写在
       `finally` 里，成功、partial、失败三条路都会留下那一行，不需要猜"作业看起来结束了没"）；
       或者**一轮什么都没读到而钟已经点过**——那是客户端已经追平的情况，此时不给它一个
       结尾，它就会挂在心跳上永远不返回。

    每轮读完都 `rollback()`：一条 SSE 可以挂几分钟，挂在 idle-in-transaction 上会把快照钉住
    （连接参数换成 REPEATABLE READ 时更是直接读不到新行），而这事实在这里毫无价值。

    作业一直停在 `pending`（这台机器上根本没有 worker 在跑）时这条流只发心跳、不会自己结束——
    那是正确的：进度条该显示"还在排队"，而客户端断开就是这一页不看了。
    """
    interval = PING_INTERVAL_S if ping_after_s is None else ping_after_s
    while True:
        wake.clear()
        rows = await read_events(session, job_id, cursor)
        await session.rollback()
        for row in rows:
            cursor = row.seq
            yield progress_frame(row)
        if rows and is_terminal_stage(rows[-1].stage):
            return
        if not rows and await job_is_finished(session, job_id):
            return
        if not await wake.wait(interval):
            yield sse_ping()
