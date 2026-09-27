"""data_sources：源库登记（口径见 docs/metadata-model.md §2.2）

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.settings import get_settings

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与应用侧 MetaData 取同一个来源：写死字符串会让表和 ORM 看到两个 schema
SCHEMA = get_settings().pg.schema_name


def upgrade() -> None:
    op.create_table(
        "data_sources",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("catalog_name", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("connect_user", sa.Text(), nullable=False),
        # 口令密文走二进制：doc 特意标了"别用 text"
        sa.Column("secret_enc", sa.LargeBinary(), nullable=False),
        sa.Column("server_version", sa.Text(), nullable=True),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("include_schemas", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("include_tables", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("exclude_tables", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("readonly_enforced", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("row_limit", sa.Integer(), server_default=sa.text("1000"), nullable=False),
        sa.Column("timeout_ms", sa.Integer(), server_default=sa.text("15000"), nullable=False),
        sa.Column("allow_global_access", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("last_sync_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
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
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('mysql','postgres')", name="kind_allowed"),
        sa.CheckConstraint("status IN ('draft','active','disabled')", name="status_allowed"),
        sa.ForeignKeyConstraint(
            ["created_by"],
            [f"{SCHEMA}.users.id"],
            name="fk_data_sources_created_by_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_data_sources"),
        sa.UniqueConstraint("name", name="uq_data_sources_name"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("data_sources", schema=SCHEMA)
