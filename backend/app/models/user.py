"""`aiweb.users` —— 列定义逐条对 `docs/metadata-model.md` §2.1。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Identity,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('admin','member')", name="role_allowed"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    # citext 而非 text：登录要按用户名直查，lower() 表达式唯一索引只解决"不许重名"，
    # 不解决"输入 alice 查得到 Alice"。
    username: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''"), default=""
    )
    email: Mapped[str | None] = mapped_column(CITEXT, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'member'"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # 三列都必须显式 timezone=True：Mapped[datetime] 在 PG 上会渲染成 timestamp
    # （无时区），存进去的时刻到了展示层就整体偏一个时区。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 令牌里带 tv，比对这里；改密时 +1，之前签发的令牌全部作废
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
