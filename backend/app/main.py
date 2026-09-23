from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.router import api_router
from app.core import db
from app.core.errors import register_exception_handlers
from app.core.logging import (
    configure_logging,
    current_request_id,
    get_logger,
    new_request_id,
    request_id_ctx,
)
from app.settings import get_settings

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.logging.level, settings.logging.json_output)
    if settings.jwt.using_default_secret:
        logger.warning("JWT_SECRET 仍是默认值，token 可被伪造（生产环境为 fatal）")
    if settings.pg.configured:
        try:
            async with db.get_engine().connect() as conn:
                version = (await conn.execute(text("select version()"))).scalar_one()
            logger.info("元数据库就绪：%s / schema=%s", str(version)[:40], settings.pg.schema_name)
        except Exception as exc:
            logger.error("元数据库不可达：%s；先跑 uv run python scripts/check_env.py", exc)
    else:
        logger.warning("元数据库未配置：填 backend/.env 的 AIWEB_PG__* 后再跑迁移")
    yield
    await db.dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app.name,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    @app.middleware("http")
    async def _request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        token = new_request_id()
        try:
            request_id = current_request_id()
            response: Response = await call_next(request)
        finally:
            request_id_ctx.reset(token)
        response.headers[settings.app.request_id_header] = request_id
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.app.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[settings.app.request_id_header],
    )

    register_exception_handlers(app)
    app.include_router(api_router, prefix="/api")
    return app


app = create_app()
