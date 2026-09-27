"""登录与当前身份。口令只在 verify 的一瞬经过内存：不写日志、不进响应体。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Unauthorized
from app.core.security import TIMING_FILLER_DIGEST, create_access_token, verify_password
from app.deps import get_current_user, get_db
from app.models.user import User
from app.settings import get_settings

router = APIRouter()


class LoginRequest(BaseModel):
    # 不在这里做强度校验：登录口限制长度只为挡掉超长 body，口令策略属于改密口
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    """白名单式输出：只有列在这里的字段会出网，password_hash 天然不在其中。

    from_attributes 只是允许直接吃 ORM 对象，不影响导出哪些列。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str
    role: str
    token_version: int


@router.post("/auth/login")
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    # username 列是 citext，== 即大小写不敏感匹配
    user = (
        await db.execute(select(User).where(User.username == payload.username))
    ).scalar_one_or_none()
    # 账号不存在与口令错共用一句文案，也共用一次 argon2 的耗时：否则登录口不管是文案还是
    # 快慢，都在回答"这个用户名注册过没有"（假串见 security.TIMING_FILLER_DIGEST）。
    if user is None:
        verify_password(TIMING_FILLER_DIGEST, payload.password)
        raise Unauthorized("用户名或口令错误")
    if not verify_password(user.password_hash, payload.password):
        raise Unauthorized("用户名或口令错误")
    if not user.is_active:
        raise Unauthorized("账号已停用，请联系管理员")

    user.last_login_at = func.now()  # 取服务端时钟，不传应用侧时间
    await db.commit()

    return {
        "access_token": create_access_token(
            user_id=user.id, role=user.role, token_version=user.token_version
        ),
        "token_type": "bearer",
        "expires_in": get_settings().jwt.access_ttl_s,
        "user": UserOut.model_validate(user),
    }


@router.get("/auth/me")
async def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(user)
