"""meta_* 六张快照表 + sync_jobs（口径见 docs/metadata-model.md §2.4 / §2.5 / ADR-0005）

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.settings import get_settings

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = get_settings().pg.schema_name

# ADR-0005：分隔符是 chr(31)（单元分隔符），不是 `_`/`-`——合法标识符字符会让
# `a_b`+`c` 与 `a`+`b_c` 撞出同一个 md5。写成 chr(31) 而不是内联 0x1f 字节，
# 是为了让迁移文件保持纯 ASCII（Windows 上 cp936 读文件是真实风险）。
#
# 表达式用 `||` 而不是 ADR 原文的 `concat_ws`：PG 的 proc 目录里 **concat_ws 是 stable**
# （`provolatile='s'`），生成列要求 immutable，照抄会在 CREATE TABLE 当场被拒
# （"generation expression is not immutable"）；md5/chr/`||`/bigint::text 全是 immutable。
# 两者语义在这里等价的前提是四列全 NOT NULL —— concat_ws 会跳过 NULL，`||` 遇 NULL 整串变 NULL。
TABLE_UID_EXPR = (
    "md5(datasource_id::text || chr(31) || catalog_name || chr(31) || schema_name"
    " || chr(31) || table_name)"
)


def _ds_fk() -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["datasource_id"], [f"{SCHEMA}.data_sources.id"], ondelete="CASCADE"
    )


def _ts_fk(col: str, target: str) -> sa.ForeignKeyConstraint:
    """指向 meta_table.id 的 FK；target 不同，约束名也就不同（同 schema 内索引名唯一）。"""
    return sa.ForeignKeyConstraint(
        [col], [f"{SCHEMA}.{target}.id"], name=f"fk_{col}_{target}", ondelete="CASCADE"
    )


# COLLATION 是 PG 的 reserved keyword（pg_get_keywords），但 SQLAlchemy 的保留字表按 SQL:2008
# 收，没有它——不强制加引号就渲染成 `collation TEXT`，建表当场 syntax error。
_COLLATION = sa.quoted_name("collation", True)


def upgrade() -> None:
    # sync_jobs 先建：meta_database.sync_job_id 要引用它
    op.create_table(
        "sync_jobs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("datasource_id", sa.BigInteger(), nullable=False),
        sa.Column("triggered_by", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("phase", sa.Text(), server_default=sa.text("'connect'"), nullable=False),
        sa.Column("progress", sa.Numeric(5, 2), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "counters",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "errors",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("manifest_digest", sa.Text(), nullable=True),
        sa.Column("heartbeat_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','success','partial','failed','cancelled')",
            name="status_allowed",
        ),
        sa.CheckConstraint(
            "phase IN ('connect','discover','tables','columns','indexes','fks',"
            "'card_build','embed','done')",
            name="phase_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            [f"{SCHEMA}.data_sources.id"],
            name="fk_sync_jobs_datasource_id_data_sources",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["triggered_by"],
            [f"{SCHEMA}.users.id"],
            name="fk_sync_jobs_triggered_by_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sync_jobs"),
        schema=SCHEMA,
    )
    # §6：同数据源只允许一个未结束 job。部分唯一索引 = 数据库保证，不是代码 race。
    op.create_index(
        "ux_sync_running",
        "sync_jobs",
        ["datasource_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("status IN ('pending','running')"),
    )

    op.create_table(
        "meta_database",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("datasource_id", sa.BigInteger(), nullable=False),
        sa.Column("catalog_name", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("schema_name", sa.Text(), nullable=False),
        sa.Column("raw_collation", sa.Text(), nullable=True),
        sa.Column("raw_engine", sa.Text(), nullable=True),
        sa.Column("table_count", sa.Integer(), nullable=True),
        sa.Column("approx_rows", sa.BigInteger(), nullable=True),
        sa.Column("approx_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("is_visible", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("sync_job_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _ds_fk(),
        sa.ForeignKeyConstraint(
            ["sync_job_id"],
            [f"{SCHEMA}.sync_jobs.id"],
            name="fk_meta_database_sync_job_id_sync_jobs",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_meta_database"),
        sa.UniqueConstraint(
            "datasource_id", "catalog_name", "schema_name", name="uq_meta_database_key"
        ),
        schema=SCHEMA,
    )

    op.create_table(
        "meta_table",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column(
            "table_uid",
            sa.CHAR(32),
            sa.Computed(TABLE_UID_EXPR),
            nullable=False,
        ),
        sa.Column("datasource_id", sa.BigInteger(), nullable=False),
        sa.Column("database_id", sa.BigInteger(), nullable=False),
        sa.Column("catalog_name", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("schema_name", sa.Text(), nullable=False),
        sa.Column("table_name", sa.Text(), nullable=False),
        sa.Column("table_type", sa.Text(), nullable=False),
        # 同步列（comment_raw）与人工列（comment_zh / business_desc / granularity / is_hidden）
        # 分开摆：§3 的 upsert 只允许写前者。
        sa.Column("comment_raw", sa.Text(), nullable=True),
        sa.Column("comment_zh", sa.Text(), nullable=True),
        sa.Column("business_desc", sa.Text(), nullable=True),
        sa.Column("granularity", sa.Text(), nullable=True),
        sa.Column("engine", sa.Text(), nullable=True),
        sa.Column("row_format", sa.Text(), nullable=True),
        sa.Column(_COLLATION, sa.Text(), nullable=True),
        sa.Column("charset", sa.Text(), nullable=True),
        sa.Column("approx_rows", sa.BigInteger(), nullable=True),
        sa.Column("data_bytes", sa.BigInteger(), nullable=True),
        sa.Column("index_bytes", sa.BigInteger(), nullable=True),
        sa.Column("last_analyze_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("is_hidden", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_stale", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "synced_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("table_type IN ('BASE TABLE','VIEW')", name="table_type_allowed"),
        _ds_fk(),
        sa.ForeignKeyConstraint(
            ["database_id"],
            [f"{SCHEMA}.meta_database.id"],
            name="fk_meta_table_database_id_meta_database",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_meta_table"),
        sa.UniqueConstraint("table_uid", name="uq_meta_table_table_uid"),
        sa.UniqueConstraint(
            "datasource_id",
            "catalog_name",
            "schema_name",
            "table_name",
            name="uq_meta_table_key",
        ),
        schema=SCHEMA,
    )

    op.create_table(
        "meta_column",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("table_id", sa.BigInteger(), nullable=False),
        sa.Column("ordinal_position", sa.Integer(), nullable=False),
        sa.Column("column_name", sa.Text(), nullable=False),
        sa.Column("data_type", sa.Text(), nullable=False),
        sa.Column("raw_data_type", sa.Text(), nullable=False),
        sa.Column("nullable", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("default_value", sa.Text(), nullable=True),
        sa.Column("is_generated", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("comment_raw", sa.Text(), nullable=True),
        sa.Column("comment_zh", sa.Text(), nullable=True),
        sa.Column("business_desc", sa.Text(), nullable=True),
        sa.Column("is_primary_key", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_unique", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_indexed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("enum_values", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("sample_values", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("char_length", sa.Integer(), nullable=True),
        sa.Column("numeric_precision", sa.Integer(), nullable=True),
        sa.Column("numeric_scale", sa.Integer(), nullable=True),
        sa.Column(
            "synced_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _ts_fk("table_id", "meta_table"),
        sa.PrimaryKeyConstraint("id", name="pk_meta_column"),
        sa.UniqueConstraint("table_id", "column_name", name="uq_meta_column_key"),
        schema=SCHEMA,
    )

    op.create_table(
        "meta_index",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("table_id", sa.BigInteger(), nullable=False),
        sa.Column("index_name", sa.Text(), nullable=False),
        sa.Column("is_unique", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("index_type", sa.Text(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("is_visible", sa.Boolean(), server_default=sa.true(), nullable=False),
        # cardinality / 下表的 sub_part 非空 = 没有退回逐表 SHOW CREATE TABLE（§10 末验收锚点）
        sa.Column("cardinality", sa.BigInteger(), nullable=True),
        sa.Column("funcdef", sa.Text(), nullable=True),
        _ts_fk("table_id", "meta_table"),
        sa.PrimaryKeyConstraint("id", name="pk_meta_index"),
        sa.UniqueConstraint("table_id", "index_name", name="uq_meta_index_key"),
        schema=SCHEMA,
    )

    op.create_table(
        "meta_index_column",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("index_id", sa.BigInteger(), nullable=False),
        sa.Column("seq_in_index", sa.Integer(), nullable=False),
        # 可空：NULL 表示这一位是表达式索引的表达式，定义落在 meta_index.funcdef
        sa.Column("column_name", sa.Text(), nullable=True),
        sa.Column(_COLLATION, sa.Text(), nullable=True),
        sa.Column("sub_part", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["index_id"],
            [f"{SCHEMA}.meta_index.id"],
            name="fk_index_id_meta_index",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_meta_index_column"),
        sa.UniqueConstraint("index_id", "seq_in_index", name="uq_meta_index_column_key"),
        schema=SCHEMA,
    )

    op.create_table(
        "meta_relation",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("datasource_id", sa.BigInteger(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("fk_name", sa.Text(), nullable=True),
        sa.Column("from_table_id", sa.BigInteger(), nullable=False),
        sa.Column("from_column_name", sa.Text(), nullable=False),
        sa.Column("to_table_id", sa.BigInteger(), nullable=False),
        sa.Column("to_column_name", sa.Text(), nullable=False),
        sa.Column("on_delete", sa.Text(), nullable=True),
        sa.Column("on_update", sa.Text(), nullable=True),
        sa.Column("deferability", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(4, 3), server_default=sa.text("1.0"), nullable=True),
        sa.Column("is_authors_enforced", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
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
        sa.CheckConstraint(
            "source_kind IN ('extracted','inferred','manual')", name="source_kind_allowed"
        ),
        _ds_fk(),
        _ts_fk("from_table_id", "meta_table"),
        _ts_fk("to_table_id", "meta_table"),
        sa.ForeignKeyConstraint(
            ["created_by"],
            [f"{SCHEMA}.users.id"],
            name="fk_meta_relation_created_by_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_meta_relation"),
        sa.UniqueConstraint(
            "datasource_id",
            "source_kind",
            "from_table_id",
            "from_column_name",
            "to_table_id",
            "to_column_name",
            name="uq_meta_relation_key",
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    # 逆序删：先删引用者，再删被引用者
    for tbl in (
        "meta_relation",
        "meta_index_column",
        "meta_index",
        "meta_column",
        "meta_table",
        "meta_database",
    ):
        op.drop_table(tbl, schema=SCHEMA)
    op.drop_index("ux_sync_running", table_name="sync_jobs", schema=SCHEMA)
    op.drop_table("sync_jobs", schema=SCHEMA)
