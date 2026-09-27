"""users：登录与身份（口径见 docs/metadata-model.md §2.1）

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.settings import get_settings

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与应用侧 MetaData 取同一个来源：写死字符串会让表和 ORM 看到两个 schema，
# 表现为"迁移成功但查询报关系不存在"。
SCHEMA = get_settings().pg.schema_name


def upgrade() -> None:
    # 不写 SCHEMA "aiweb"：SQLAlchemy 渲染列类型时不会给 CITEXT 加限定名，
    # 扩展装进 aiweb 就等于装在 search_path 外面，建表当场报 type "citext" does not exist。
    # 与 0001 的 pg_trgm 保持同一落点（默认 schema），见 docs/metadata-model.md §2。
    try:
        op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    except Exception as exc:
        raise RuntimeError(
            "建 citext 失败。请让 DBA 在元数据库执行：CREATE EXTENSION IF NOT EXISTS citext; "
            f"（原始错误：{exc}）"
        ) from exc

    op.create_table(
        "users",
        # GENERATED ALWAYS（metadata-model §1）：id 由库生成，不接受应用侧塞值
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("username", postgresql.CITEXT(), nullable=False),
        sa.Column("display_name", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), server_default=sa.text("'member'"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_login_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("token_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("role IN ('admin','member')", name="role_allowed"),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("users", schema=SCHEMA)
