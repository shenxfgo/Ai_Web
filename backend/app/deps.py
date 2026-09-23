"""FastAPI 依赖。鉴权相关依赖（get_current_user / require_admin）随 P2 的 auth 一起落地。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_sessionmaker
from app.services.llm_client import LlmClient
from app.settings import get_settings


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


_http: httpx.AsyncClient | None = None


def get_llm_http() -> httpx.AsyncClient:
    """共享出去而不是每次新建：连接池复用才能省掉每问一次的 TLS 握手。

    这里不绑 base_url——它由 settings.llm.base_url 单点决定，绑两处会漂移。
    """
    global _http
    if _http is None:
        _http = httpx.AsyncClient()
    return _http


async def dispose_llm_http() -> None:
    global _http
    if _http is not None:
        await _http.aclose()
        _http = None


def get_llm() -> LlmClient:
    return LlmClient(llm=get_settings().llm, http=get_llm_http())
