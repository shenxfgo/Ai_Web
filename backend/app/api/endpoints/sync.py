"""元数据同步接口：一次请求 = 一次完整同步（工单 007 拍板：200 直接带计数）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.sync import SyncJobOut, SyncJobRequest
from app.services import datasource_service, sync_service

router = APIRouter()


@router.post("/sync/jobs")
async def create_sync_job(
    payload: SyncJobRequest,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SyncJobOut:
    """同步行权按 owner 档（与 DELETE/test 同一把尺）：同步会拿这个源的凭据去连库，
    能触发它就等于能试探它的口令，'global' 档的人不该有这个能力。
    """
    row = await datasource_service.get_owned(db, actor, payload.datasource_id)
    outcome = await sync_service.run_sync(db, row, actor=actor)
    # 字段逐个搬过一遍会漏改就错位；SyncJobOut 与 SyncOutcome 同构，转换交给 pydantic
    return SyncJobOut.model_validate(outcome, from_attributes=True)
