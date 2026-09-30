"""元数据同步接口：一次请求 = 把作业放进队列（工单 016：202 只带 job_id，跑由 worker 跑）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db, get_job_queue
from app.models.user import User
from app.schemas.sync import SyncJobAccepted, SyncJobRequest
from app.services import datasource_service
from app.services.job_queue import JobQueue

router = APIRouter()


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
    return SyncJobAccepted(job_id=await queue.enqueue(row.id, actor.id))
