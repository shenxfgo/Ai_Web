"""FastAPI 依赖。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_sessionmaker
from app.core.errors import Unauthorized
from app.core.security import decode_access_token
from app.models.user import User
from app.services.llm_client import LlmClient
from app.settings import get_settings


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


# auto_error=True 时 FastAPI 自己抛的是 403，且不带我们的错误 envelope；
# 关掉它，缺令牌统一走 Unauthorized（401）。
_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    if creds is None:
        raise Unauthorized("未登录：缺少 Authorization: Bearer <token>")
    claims = decode_access_token(creds.credentials)
    user = await db.get(User, int(claims["sub"]))
    if user is None:
        # 令牌有效但账号已不存在：等价于登录态失效，踢回登录页而不是报 500
        raise Unauthorized("登录已失效，请重新登录")
    if not user.is_active:
        raise Unauthorized("账号已停用，请联系管理员")
    if claims["tv"] != user.token_version:
        # 改密会 bump token_version，此前签发的全部令牌当场作废
        raise Unauthorized("登录已失效，请重新登录")
    return user


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
