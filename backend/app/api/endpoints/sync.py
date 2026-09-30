"""同步接口：发起 = 入队回 202（工单 016，只带 job_id）；进度 = SSE 追事件表（工单 017）。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any, cast

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError, NotFound
from app.core.logging import get_logger
from app.core.sse import (
    EVENT_DONE,
    EVENT_ERROR,
    JobWake,
    sse_format,
    sse_retry,
    stream_job_events,
)
from app.deps import (
    get_current_user,
    get_db,
    get_job_queue,
    get_job_queue_factory,
    get_stream_sessionmaker,
)
from app.models.meta import SyncJob
from app.models.user import User
from app.schemas.sync import SyncJobAccepted, SyncJobOut, SyncJobRequest
from app.services import datasource_service
from app.services.job_queue import JobQueue

router = APIRouter()

_logger = get_logger(__name__)

_JOB = cast("Table", SyncJob.__table__)


@router.post("/sync/jobs", status_code=status.HTTP_202_ACCEPTED)
async def create_sync_job(
    payload: SyncJobRequest,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    queue: JobQueue = Depends(get_job_queue),
) -> SyncJobAccepted:
    """同步行权按 owner 档（与 DELETE/test 同一把尺）：同步会拿这个源的凭据去连库，
    能触发它就等于能试探它的口令，'global' 档的人不该有这个能力。

    这里只入队。作业在 worker 进程里跑（ADR-0011），所以响应体里没有计数——
    请求返回时那一行还是 pending，任何计数都是编的。
    """
    row = await datasource_service.get_owned(db, actor, payload.datasource_id)
    # force 只在这一步有落脚处：worker 是另一个进程读不到请求，"admin 当场说过覆盖上限"
    # 必须在入队那一刻写进行里（工单 021，ADR-0011 的后果）。
    return SyncJobAccepted(job_id=await queue.enqueue(row.id, actor.id, force=payload.force))


def _resolve_cursor(cursor: int | None, last_event_id: str | None) -> int:
    """起始游标：查询参数优先，其次 `Last-Event-ID`，都没有就从头补读。

    两处都认是 SSE 的现实：`EventSource` 只会自动带 `Last-Event-ID`，而本项目的前端
    用 `fetch` + `ReadableStream` 手解帧（§6.2：EventSource 带不了 `Authorization`），
    那种写法只能靠 `?cursor=`。解析不出来一律归 0——"宁可重发也不漏"是安全的一侧，
    而事件表是 append-only 的，重发前端也只需要按 seq 去重。
    """
    raw = cursor if cursor is not None else last_event_id
    if raw is None:
        return 0
    try:
        return max(int(raw), 0)
    except (TypeError, ValueError):
        return 0


async def _outcome(session: AsyncSession, job_id: int) -> dict[str, Any]:
    """收尾帧的负载：`SyncJobOut` 对终局作业行的投影。

    读**作业行**而不是最后一条事件：事件说的是"进度"，而 `warnings`/`errors` 的终局账住在
    `sync_jobs` 那一格（§2.5）。走 `SyncJobOut` 而不是手搓 dict 是为了让 `status` 的
    pattern 真的执行一次——收尾帧说"success"而库里那一格写着别的值，是这条链上最难查的
    不一致之一。
    """
    row = (
        await session.execute(
            select(
                _JOB.c.status,
                _JOB.c.counters,
                _JOB.c.warnings,
                _JOB.c.errors,
                _JOB.c.started_at,
                _JOB.c.finished_at,
            ).where(_JOB.c.id == job_id)
        )
    ).mappings()
    job = row.one()
    duration_ms: int | None = None
    if job.started_at is not None and job.finished_at is not None:
        # 两个钟都由库生成（`func.now()`），相减不需要担心应用与 PG 的时区/时钟差
        duration_ms = int((job.finished_at - job.started_at).total_seconds() * 1000)
    return SyncJobOut(
        job_id=job_id,
        status=job.status,
        counters=job.counters,
        warnings=job.warnings,
        errors=job.errors,
        duration_ms=duration_ms,
    ).model_dump()


@router.get("/sync/jobs/{job_id}/events")
async def stream_sync_job_events(
    job_id: int,
    request: Request,
    cursor: int | None = None,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_stream_sessionmaker),
    queue_for: Callable[[AsyncSession], JobQueue] = Depends(get_job_queue_factory),
) -> StreamingResponse:
    """进度流（§7 那行）：`text/event-stream` 推 `sync_job_event`，以 `event: done` 收。

    权限与 `POST /sync/jobs` 同一把尺（owner/admin 档，`get_owned` 那句判定）——这条流
    把 `errors[].detail` 里的原文（含源库报错）推给客户端，能读进度就能读口令错的提示，
    所以它不是"只读接口"就能松。作业不存在 → 404，存在但不是你的 → 由 `get_owned` 定档。

    鉴权与游标都在**返回响应之前**做完：流一旦开始，状态码就发不出去了。
    """
    job = await db.get(SyncJob, job_id)
    if job is None:
        raise NotFound("没有这个同步作业")
    await datasource_service.get_owned(db, actor, job.datasource_id)
    start = _resolve_cursor(cursor, request.headers.get("last-event-id"))

    async def frames() -> AsyncIterator[str]:
        # 会话在流里开、在流里关（见 `get_stream_sessionmaker` 的理由）
        async with session_factory() as session:
            wake: JobWake = await queue_for(session).subscribe(job_id)
            try:
                yield sse_retry()
                async for frame in stream_job_events(session, wake, job_id, start):
                    yield frame
                yield sse_format(EVENT_DONE, await _outcome(session, job_id))
            except AppError as exc:
                # §6.2：生成器里冒出去的异常必须转成帧，否则前端只看到"连接断开"，
                # 而那条断开的连接背后可能刚刚写坏了元数据。
                yield sse_format(EVENT_ERROR, {"code": exc.code, "message": str(exc)})
            except Exception:
                # 驱动层那一层（元数据库断了、作业行在流期间被 CASCADE 删掉）也要起一帧，
                # 否则这条要求就只覆盖了一半。原文只进日志不进帧——同 errors.py 的
                # SQLAlchemyError 处理器一条理由：驱动原文带连接串与库内对象名。
                _logger.exception("进度流中断 job_id=%s", job_id)
                yield sse_format(
                    EVENT_ERROR,
                    {"code": "internal_error", "message": "进度流中断，原因已记入服务日志"},
                )
            finally:
                # 客户端断开走的是这里：`async for` 被 aclose() 时抛 GeneratorExit，
                # 上面两个 except 都接不住（它们抓的是 Exception，而 GeneratorExit 不是），
                # 但 finally 照样跑。
                # shield 不是多余的讲究：这一步本身被二次取消的话，LISTEN 就挂在一条
                # 已经回池的连接上，下一个借用它的人会替这个作业敲门。
                await asyncio.shield(wake.aclose())

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache, no-transform",
            "x-accel-buffering": "no",
        },
    )
