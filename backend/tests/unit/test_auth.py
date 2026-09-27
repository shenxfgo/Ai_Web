"""登录切片的口令哈希与访问令牌：哈希、签发、验签。

期望值来自文档口径，不从被测代码反算：
- `docs/metadata-model.md` §2.1：password_hash 用 argon2id
- `docs/architecture.md` §依赖风险：哈希走 pwdlib、JWT 走 PyJWT（不用 passlib / python-jose）
- `docs/roadmap.md` 分组 5：算法白名单只允许 HS256/HS384/HS512，禁 none
"""

from __future__ import annotations

import jwt
import pytest

from app.core.errors import Unauthorized
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_校验通过且同一口令两次哈希不同() -> None:
    """同一口令必须能校验通过，但两次哈希结果不能相同（否则等于没加盐）。"""
    digest = hash_password("S3cret-口令")
    assert verify_password(digest, "S3cret-口令") is True
    assert verify_password(digest, "S3cret-wrong") is False
    assert hash_password("S3cret-口令") != digest


def test_哈希串里写明是_argon2id() -> None:
    """元数据模型指定 argon2id；串里认出算法，换算法时迁移脚本才有判据。"""
    assert hash_password("x").startswith("$argon2id$")


# ---------------------------------------------------------------------- JWT


def test_令牌往返带得出身份(jwt_secret: str) -> None:
    token = create_access_token(user_id=7, role="admin", token_version=2)
    claims = decode_access_token(token)
    assert claims["sub"] == "7"
    assert claims["role"] == "admin"
    assert claims["tv"] == 2


def test_过期令牌报登录过期而不是裸异常(jwt_secret: str) -> None:
    """过期与伪造必须是两种文案：前者前端要静默刷新，后者要直接踢回登录页。"""
    token = create_access_token(user_id=1, role="member", token_version=0, ttl_s=-1)
    with pytest.raises(Unauthorized) as boom:
        decode_access_token(token)
    assert "过期" in boom.value.message


def test_签名不对的令牌被拒(jwt_secret: str) -> None:
    # 密钥取满 32 字节：PyJWT 2.10+ 对短密钥会告警，测试输出里混进那条告警
    # 会被误读成"我们的实现有问题"，而这里想验证的只是签名不匹配必须被拒。
    forged = jwt.encode({"sub": "1", "role": "admin", "tv": 0}, "x" * 40, algorithm="HS256")
    with pytest.raises(Unauthorized):
        decode_access_token(forged)


def test_算法为none的令牌被拒(jwt_secret: str) -> None:
    """PyJWT 只在 algorithms 白名单里列出的算法上验签——这条锁住那个不可配的前提。"""
    none_token = jwt.encode({"sub": "1", "exp": 9999999999}, None, algorithm="none")
    with pytest.raises(Unauthorized):
        decode_access_token(none_token)
