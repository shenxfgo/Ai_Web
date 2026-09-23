"""统一错误 envelope：{error:{code,message,detail}}，HTTP 状态码由异常类决定。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError


class AppError(Exception):
    code: str = "internal_error"
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR

    def __init__(self, message: str = "", detail: Any = None) -> None:
        self.message = message or self.__class__.__doc__ or self.code
        self.detail = detail
        super().__init__(self.message)


class NotFound(AppError):
    code = "not_found"
    status_code = status.HTTP_404_NOT_FOUND


class Forbidden(AppError):
    code = "forbidden"
    status_code = status.HTTP_403_FORBIDDEN


class Unauthorized(AppError):
    code = "unauthorized"
    status_code = status.HTTP_401_UNAUTHORIZED


class Conflict(AppError):
    code = "conflict"
    status_code = status.HTTP_409_CONFLICT


class InvalidRequest(AppError):
    code = "invalid_request"
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY


def error_body(code: str, message: str, detail: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "detail": detail}}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            error_body(exc.code, exc.message, exc.detail), status_code=exc.status_code
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            error_body("invalid_request", "请求参数不合法", exc.errors()),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    @app.exception_handler(SQLAlchemyError)
    async def _db_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        # 不把驱动原文回给前端，避免泄露连接串与库内对象名
        from app.core.logging import get_logger

        get_logger(__name__).exception("db_error", path=request.url.path, exc_info=exc)
        return JSONResponse(
            error_body("database_error", "数据访问失败"),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
