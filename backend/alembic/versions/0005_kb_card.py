"""kb_card / kb_index_profile：卡片表 + 四条检索索引（口径见 docs/metadata-model.md §2.6、ADR-0003）

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from app.settings import get_settings

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = get_settings().pg.schema_name


def vector_type(dim: int) -> Vector:
    """`embedding` 的列类型：维度来自 settings，迁移里不出现第二个字面量。

    为什么必须 >0：`AIWEB_EMBEDDING__DIMENSION` 从这一版起兼任**列宽**，而 `vector(0)` 在
    pgvector 里是非法类型（docs/kb-workflow.md §7、ADR-0003 的"可插拔边界"）。settings 的默认值
    恰好是 0，"没配" 与 "配了 0" 在这里分不开，所以只能拒绝——猜一个默认宽度会让换 provider 时
    列宽悄悄不对，报错推迟到写向量那一刻。
    """
    if dim <= 0:
        raise RuntimeError(
            "0005 需要 AIWEB_EMBEDDING__DIMENSION > 0：它是 kb_card.embedding 的列宽，"
            f"而 vector({dim}) 不是合法的 pgvector 类型。未启用 embedding 端点也要填，"
            "填 provider 的维度（text-embedding-3-small 是 1536）。"
        )
    return Vector(dim)


def require_vector_extension(bind: sa.Connection) -> None:
    """缺 `vector` 扩展时先给一句能照着做的中文 hint，而不是让 PG 报类型不存在。

    为什么不照 0001 建 pg_trgm 的办法 `CREATE EXTENSION IF NOT EXISTS`：pgvector 0.8.6 的
    `vector.control` 没有 `trusted`，应用账号执行必失败，而且失败会脏掉整条迁移链
    （roadmap §P1 验收 6 的 as-built 注就是为此改成"引导交给 DBA"的）。
    """
    installed = bind.execute(
        sa.text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
    ).scalar()
    if not installed:
        raise RuntimeError(
            "0005 建 kb_card.embedding 需要 vector 扩展，但它不是 trusted 扩展，"
            "应用账号建不了。请超级用户/DBA 在元数据库执行：CREATE EXTENSION vector;"
            "（0.4+ 才支持 HNSW），然后重跑 alembic upgrade head。"
        )


def _ds_fk() -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["datasource_id"],
        [f"{SCHEMA}.data_sources.id"],
        name="fk_kb_card_datasource_id",
        ondelete="CASCADE",
    )


def upgrade() -> None:
    require_vector_extension(op.get_bind())
    embedding_type = vector_type(get_settings().embedding.dimension)

    # profile 先建：kb_card.index_profile_id 是 NOT NULL 外键，没有它一张卡也插不进去
    op.create_table(
        "kb_index_profile",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("card_template_version", sa.Integer(), nullable=False),
        sa.Column("distance_fn", sa.Text(), server_default=sa.text("'cosine'"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("built_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_kb_index_profile"),
        sa.UniqueConstraint("name", name="uq_kb_index_profile_name"),
        schema=SCHEMA,
    )

    op.create_table(
        "kb_card",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("datasource_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("table_id", sa.BigInteger(), nullable=True),
        sa.Column("seq", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("doc_uid", sa.CHAR(32), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("text_md", sa.Text(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("index_profile_id", sa.BigInteger(), nullable=False),
        sa.Column("embedding", embedding_type, nullable=True),
        sa.Column("embedded_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("sync_job_id", sa.BigInteger(), nullable=True),
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
        sa.CheckConstraint("kind IN ('table','table_columns','term')", name="kind_allowed"),
        _ds_fk(),
        sa.ForeignKeyConstraint(
            ["table_id"],
            [f"{SCHEMA}.meta_table.id"],
            name="fk_kb_card_table_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["index_profile_id"],
            [f"{SCHEMA}.kb_index_profile.id"],
            name="fk_kb_card_index_profile_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["sync_job_id"],
            [f"{SCHEMA}.sync_jobs.id"],
            name="fk_kb_card_sync_job_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_kb_card"),
        sa.UniqueConstraint("doc_uid", name="uq_kb_card_doc_uid"),
        schema=SCHEMA,
    )

    # 四条索引各守一条检索路（§2.6）。HNSW 与 IVFFlat 的取舍是"空表建不了就得改迁移"，
    # 所以随建表同批建，不等"启用 embedding profile"。
    op.create_index(
        "ix_kb_card_emb",
        "kb_card",
        ["embedding"],
        schema=SCHEMA,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_with={"m": 16, "ef_construction": 64},
    )
    op.create_index(
        "ix_kb_card_search_trgm",
        "kb_card",
        ["search_text"],
        schema=SCHEMA,
        postgresql_using="gin",
        postgresql_ops={"search_text": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_kb_card_search_fts",
        "kb_card",
        [sa.text("to_tsvector('simple', search_text)")],
        schema=SCHEMA,
        postgresql_using="gin",
    )
    op.create_index(
        "ix_kb_card_ds_kind",
        "kb_card",
        ["datasource_id", "kind"],
        schema=SCHEMA,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    for name in (
        "ix_kb_card_ds_kind",
        "ix_kb_card_search_fts",
        "ix_kb_card_search_trgm",
        "ix_kb_card_emb",
    ):
        op.drop_index(name, table_name="kb_card", schema=SCHEMA)
    op.drop_table("kb_card", schema=SCHEMA)
    op.drop_table("kb_index_profile", schema=SCHEMA)
