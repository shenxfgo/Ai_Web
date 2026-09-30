"""sync_job_event：同步进度的唯一真相表（口径见 docs/metadata-model.md §2.8、ADR-0010）
外加 sync_jobs.force 一列（工单 021 拍板：与本片同批，不为一个布尔单开一次迁移）。

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.settings import get_settings

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = get_settings().pg.schema_name


def upgrade() -> None:
    op.create_table(
        "sync_job_event",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        # job 内单调 +1；SSE 的游标就是它，断线重连按 seq > cursor 追读，一条不丢
        sa.Column("seq", sa.BigInteger(), nullable=False),
        # 粗粒度对外档；与 sync_jobs.phase 那 9 个细值之间的翻译只住在 stage_of() 里
        sa.Column("stage", sa.Text(), nullable=False),
        # 细粒度真相（排障用）。可空：不是每个事件都对应一次 phase 变化
        sa.Column("phase", sa.Text(), nullable=True),
        # 该事件时刻的累计账 {done, total, base_table, view, cards}
        sa.Column(
            "counters",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        # {table, code, detail, skipped}：哪张表的卡片失败、embed 档为什么被跳过
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "stage IN ('extract','embed','upsert','card_build','done')",
            name="ck_sync_job_event_stage_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            [f"{SCHEMA}.sync_jobs.id"],
            name="fk_sync_job_event_job_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sync_job_event"),
        # 唯一的 (job_id, seq) 自带一棵 b-tree，正好服务 `job_id=? AND seq>? ORDER BY seq`，
        # 所以 §2.8 里单独那行 INDEX (job_id, seq) 不建——第二棵同键索引是纯写放大。
        sa.UniqueConstraint("job_id", "seq", name="uq_sync_job_event_job_id_seq"),
        schema=SCHEMA,
    )
    # force 是"入队那一刻用户有没有说过覆盖规模上限"，只能落在行上：worker 读不到请求。
    # 默认 false——加了列而默认不是 false，等于给历史行补了一个"可以超限"的意思。
    op.add_column(
        "sync_jobs",
        sa.Column("force", sa.Boolean(), server_default=sa.false(), nullable=False),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("sync_jobs", "force", schema=SCHEMA)
    # 唯一约束随表一起走，没有单独的 drop_index
    op.drop_table("sync_job_event", schema=SCHEMA)
