"""FastAPI 依赖。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import httpx
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import get_sessionmaker
from app.core.errors import Unauthorized
from app.core.security import decode_access_token
from app.models.user import User
from app.services.job_queue import JobQueue, PostgresJobQueue
from app.services.llm_client import LlmClient
from app.settings import get_settings


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


def make_job_queue(session: AsyncSession) -> JobQueue:
    """作业队列唯一的装配点（ADR-0011）：返回类型写 Protocol 而不是 `PostgresJobQueue`。

    端点只该认这条缝的形状——换介质时改动面是这一个函数，不是每个调用点。介质只出现在
    `return` 那一行，所以 SSE 那种"会话不属于请求"的地方也能从这里拿到同一个缝。
    """
    return PostgresJobQueue(session)


def get_job_queue(db: AsyncSession = Depends(get_db)) -> JobQueue:
    return make_job_queue(db)


def get_job_queue_factory() -> Callable[[AsyncSession], JobQueue]:
    """装配点的第二个入口，留给长挂的响应：SSE 的会话不属于请求，队列要现场造。"""
    return make_job_queue


def get_stream_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """流式响应（SSE）自己的会话**工厂**，注意返回的是工厂而不是会话。

    `get_db` 那种 yield 依赖在这里会致命：FastAPI 把依赖的退出栈包在**端点函数**外面
    （`fastapi/routing.py` 的 `async with AsyncExitStack()`），而 `StreamingResponse` 的体
    是在那个栈关掉之后才被迭代的——那时请求级会话早就把连接还回池了。一条要挂几分钟的流
    必须自己开会话、自己在 `finally` 里关。给用例留的口子也正好开在这一层：override
    这个函数就能把流指到测试库。
    """
    return get_sessionmaker()


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
