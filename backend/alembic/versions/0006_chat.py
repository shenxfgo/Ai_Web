"""chat_sessions / chat_messages：问数留痕两表（口径见 docs/metadata-model.md §2.7 as-built(0006)）

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.settings import get_settings

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = get_settings().pg.schema_name


def upgrade() -> None:
    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        # 可空 + SET NULL：删源不该连带删掉"当时问过什么"这条审计链
        sa.Column("datasource_id", sa.BigInteger(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("is_pinned", sa.Boolean(), server_default=sa.false(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["user_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_chat_sessions_user_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            [f"{SCHEMA}.data_sources.id"],
            name="fk_chat_sessions_datasource_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chat_sessions"),
        schema=SCHEMA,
    )
    op.create_index("ix_chat_sessions_user_id", "chat_sessions", ["user_id"], schema=SCHEMA)

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=True),
        sa.Column("question_resolved", sa.Text(), nullable=True),
        sa.Column(
            "retrieved",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        # 审计列宁可为 0 也不放估算值：P2 的 llm_client.complete() 不返回 usage，所以恒 0
        sa.Column("prompt_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("sql_raw", sa.Text(), nullable=True),
        sa.Column("sql_final", sa.Text(), nullable=True),
        sa.Column(
            "guard_result",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("executed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "result_columns",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "result_stats",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "chart_spec",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("conclusion", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('user','assistant','system')", name="ck_chat_messages_role_allowed"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            [f"{SCHEMA}.chat_sessions.id"],
            name="fk_chat_messages_session_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chat_messages"),
        schema=SCHEMA,
    )
    op.create_index("ix_chat_messages_session_id", "chat_messages", ["session_id"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("ix_chat_messages_session_id", table_name="chat_messages", schema=SCHEMA)
    op.drop_table("chat_messages", schema=SCHEMA)
    op.drop_index("ix_chat_sessions_user_id", table_name="chat_sessions", schema=SCHEMA)
    op.drop_table("chat_sessions", schema=SCHEMA)
