from __future__ import annotations

from fastapi import APIRouter

from app.api.endpoints import auth, chat, datasources, health, kb, sync

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, tags=["auth"])
api_router.include_router(datasources.router, tags=["datasources"])
api_router.include_router(sync.router, tags=["sync"])
api_router.include_router(kb.router, tags=["kb"])
# `chat` 这个 tag 是 039 新开的，但它**只有一支** `POST /chat/validate`（第一层守卫的 HTTP 入口）。
# 问数本体 `POST /chat/ask`、SSE 流式、结果下载端点都**还不存在**（归 P8）；
# tag 出现在这里 ≠ 那一面已经在了，别让 P7/P8 的读者按 `tags=["chat"]` 去数端点。
api_router.include_router(chat.router, tags=["chat"])
