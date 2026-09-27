"""`aiweb.data_sources` —— 列定义逐条对 `docs/metadata-model.md` §2.2。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    LargeBinary,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class DataSource(Base):
    __tablename__ = "data_sources"
    __table_args__ = (
        CheckConstraint("kind IN ('mysql','postgres')", name="kind_allowed"),
        CheckConstraint("status IN ('draft','active','disabled')", name="status_allowed"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    # 全局唯一，而且被 kb-workflow.md 直接当知识库目录名用——重名会把两个源的卡片叠在一起
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    host: Mapped[str] = mapped_column(Text, nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    # MySQL 恒为空串，PG 才写目标库；不设 `database` 单列（§2.2 末注：MySQL 的库走 include_schemas）
    catalog_name: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''"), default=""
    )
    connect_user: Mapped[str] = mapped_column(Text, nullable=False)
    # bytea 而非 text：Fernet 密文是 base64，存 text 也能过，但验收那句
    # `left(convert_from(secret_enc,'UTF8'),12)` 就不是在核对"库里是二进制"了
    secret_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    server_version: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    include_schemas: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    include_tables: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    exclude_tables: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    readonly_enforced: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), default=True
    )
    # L4 数据源级配置：覆盖全局默认（roadmap 的分层表）
    row_limit: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1000"))
    timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("15000"))
    allow_global_access: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'draft'"), default="draft"
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # owner 判定唯一的依据，所以它是 NOT NULL 的外键而不是可空列
    created_by: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
