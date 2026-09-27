"""数据源登记接口。口令只在请求体里存在一瞬，进 service 就被 encrypt 掉。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.datasource import (
    ConnectionTestOut,
    ConnectionTestRequest,
    DataSourceCreate,
    DataSourceOut,
)
from app.services import datasource_service

router = APIRouter()


@router.post("/datasources", status_code=status.HTTP_201_CREATED)
async def create_datasource(
    payload: DataSourceCreate,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DataSourceOut:
    row, tier = await datasource_service.create(db, actor=actor, payload=payload)
    return datasource_service.render(row, tier)


@router.get("/datasources")
async def list_datasources(
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DataSourceOut]:
    """只返回有权的：§7 给这个端点的是裸数组，没有分页（源的量级是个位数到几十）。"""
    return [
        datasource_service.render(row, tier)
        for row, tier in await datasource_service.list_visible(db, actor)
    ]


@router.get("/datasources/{ds_id}")
async def get_datasource(
    ds_id: int,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DataSourceOut:
    row, tier = await datasource_service.get_authorized(db, actor, ds_id)
    return datasource_service.render(row, tier)


@router.delete("/datasources/{ds_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_datasource(
    ds_id: int,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """§7：DELETE 要 owner/admin。'global' 档的人能读不能删——看得见 ≠ 管得着。"""
    row = await datasource_service.get_owned(db, actor, ds_id)
    await datasource_service.soft_delete(db, row)


@router.post("/datasources/{ds_id}/test")
async def test_datasource(
    ds_id: int,
    payload: ConnectionTestRequest | None = None,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectionTestOut:
    """§7 的"建库前先探规模"，权限同 DELETE：owner/admin。"""
    row = await datasource_service.get_owned(db, actor, ds_id)
    # 临时口令优先：UI 的"先试再存"就是拿没进库的那一个来试（取用规则在 service 里）
    return await datasource_service.test_connection(
        db, row, temporary_password=payload.connect_password if payload else None
    )
