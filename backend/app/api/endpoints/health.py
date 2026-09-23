from __future__ import annotations

from fastapi import APIRouter

from app.settings import get_settings

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """进程存活探针：不碰数据库、不鉴权。"""
    settings = get_settings()
    return {
        "status": "ok",
        "environment": settings.app.environment,
        "retrieval_mode": "vector+keyword" if settings.embedding.configured else "keyword",
    }
