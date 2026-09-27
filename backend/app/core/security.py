"""口令哈希与访问令牌。口令永不出库、永不进日志、永不进响应体。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from app.core.errors import Unauthorized
from app.settings import get_settings

# argon2id 是 docs/metadata-model.md §2.1 指定的算法；元组顺序即写入时用的算法（取第一个）。
_password_hash = PasswordHash((Argon2Hasher(),))

# 只认这一个算法：写进配置里"可配"就等于把 `none` 也请进攻击面（roadmap 分组 5 禁的正是它）
_ALGORITHM = "HS256"

# 一段固定的 argon2id 串，专供"账号不存在"这条路拿去验：结果必然 False，但耗时和验真口令
# 同一量级。不这么做的话，登录口对不存在账号秒回、对存在账号卡上百毫秒，
# 响应时间本身就是一台用户名枚举机。对应明文没人需要知道，也不需要知道。
TIMING_FILLER_DIGEST = (
    "$argon2id$v=19$m=65536,t=3,p=4$I8ILGobswpwVAJ2Rf5SLYQ$"
    + "H2nGXmOb1QGFcxPZySbpsVHUeCvAgkvXGghLnaIAZOM"
)


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(digest: str, password: str) -> bool:
    """口令错与串坏都返回 False，不抛。

    这里的参数顺序 (digest, password) 和 pwdlib 自己的 (password, hash) 正好相反，
    所以下面调的是 verify(password, digest)。按直觉把两个实参传反不会报错，只会永远
    返回 False——变成一条测不出来的登录故障。
    """
    try:
        return _password_hash.verify(password, digest)
    except Exception:
        return False


def create_access_token(
    *, user_id: int, role: str, token_version: int, ttl_s: int | None = None
) -> str:
    """签发访问令牌。`tv` 载荷用来比对库里的 token_version：改密即让旧令牌全失效。"""
    settings = get_settings()
    now = datetime.now(UTC)
    ttl = settings.jwt.access_ttl_s if ttl_s is None else ttl_s
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "tv": token_version,
        "iat": int(now.timestamp()),
        "exp": now + timedelta(seconds=ttl),
    }
    return jwt.encode(payload, settings.jwt.secret, algorithm=_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """验签并解出载荷；任何不合法都收成 Unauthorized，不给调用方留裸异常。

    PyJWT 抛的不是一个类而是七种（ExpiredSignatureError / InvalidSignatureError /
    InvalidTokenError 基类…），逐个 catch 必漏一种——漏出来的那种会冒成 HTTP 500，
    而 500 会让前端以为"服务坏了"，实际只是令牌过期。
    """
    settings = get_settings()
    try:
        return jwt.decode(
            token,
            settings.jwt.secret,
            algorithms=[_ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("登录已过期，请重新登录") from exc
    except jwt.PyJWTError as exc:
        raise Unauthorized("访问令牌无效") from exc
