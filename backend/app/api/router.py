from __future__ import annotations

from fastapi import APIRouter

from app.api.endpoints import auth, datasources, health, kb, sync

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, tags=["auth"])
api_router.include_router(datasources.router, tags=["datasources"])
api_router.include_router(sync.router, tags=["sync"])
api_router.include_router(kb.router, tags=["kb"])
