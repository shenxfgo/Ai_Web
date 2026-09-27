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


class SourceUnreachable(AppError):
    """连不上源库、或者连上了但源库说"你不该连"。

    单列一档而不是塞进 500：这是用户填错一格就能自救的情况，前端要把它显示成表单错误
    而不是"服务异常"。400 而不是 422——422 归请求体格式，这里是请求合法、目标不对。
    """

    code = "source_unreachable"
    status_code = status.HTTP_400_BAD_REQUEST


class NotImplementedSource(AppError):
    """功能对某种 kind 还没做（区别于"这是 bug"）。"""

    code = "not_implemented"
    status_code = status.HTTP_501_NOT_IMPLEMENTED


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
        # 只回定位用的三件套，不回 exc.errors() 的整条：pydantic 的 `input` 装的是**整个**
        # 请求体，哪怕错的是 port 那一格，同一份 body 里的 connect_password 也会跟着回显出去。
        # 输出模型白名单只挡成功路径，挡不住这里。
        detail = [
            {"loc": list(err.get("loc", ())), "type": err.get("type"), "msg": err.get("msg")}
            for err in exc.errors()
        ]
        return JSONResponse(
            error_body("invalid_request", "请求参数不合法", detail),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    @app.exception_handler(SQLAlchemyError)
    async def _db_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        # 不把驱动原文回给前端，避免泄露连接串与库内对象名
        from app.core.logging import get_logger

        # stdlib logger 不认任意关键字参数：写成 path=... 会让这个处理器自己抛
        # TypeError，用户拿到没有 envelope 的裸 500。
        get_logger(__name__).exception("数据库访问失败 path=%s", request.url.path, exc_info=exc)
        return JSONResponse(
            error_body("database_error", "数据访问失败"),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
