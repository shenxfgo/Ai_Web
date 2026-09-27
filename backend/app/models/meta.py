"""元数据快照六表（列定义逐条对 `docs/metadata-model.md` §2.4）。

两点全局约定：
- 所有表都 CASCADE 到 `data_sources`：删源=删它的整套快照，不留孤儿。
- `catalog_name`/`schema_name` 是**跨方言规范化列**（§1）：MySQL 走 `('' , <db>)`，
  PG 走 `(<db>, <schema>)`。唯一性只在四元组内成立，`UNIQUE (datasource_id, table_name)`
  会让两个库的 `orders` 互相覆盖。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    quoted_name,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_DS_FK_TARGET = "data_sources.id"


def _ds_fk() -> tuple[Any, ...]:
    # 每次调用都要新的 ForeignKey 对象：共享同一个会在第二张表上炸
    # "This ForeignKey already has a parent"。
    return (BigInteger, ForeignKey(_DS_FK_TARGET, ondelete="CASCADE"))


# COLLATION 在 PG 的 pg_get_keywords() 里是 reserved，但 SQLAlchemy 的保留字表（SQL:2008）
# 没有它，于是 DDL 渲染成不带引号的 `collation TEXT`，建表当场 syntax error。
# 文档 §2.4 把这一列就叫 collation，改列名会牵动 008 的卡片模板，所以这里强制加引号。
COLLATION_COL = quoted_name("collation", True)


class MetaDatabase(Base):
    """PG=catalog / MySQL=schema：统一的"顶层可选单位"。"""

    __tablename__ = "meta_database"
    __table_args__ = (
        UniqueConstraint(
            "datasource_id", "catalog_name", "schema_name", name="uq_meta_database_key"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    datasource_id: Mapped[int] = mapped_column(*_ds_fk(), nullable=False)
    catalog_name: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    schema_name: Mapped[str] = mapped_column(Text, nullable=False)
    raw_collation: Mapped[str | None] = mapped_column(Text)
    raw_engine: Mapped[str | None] = mapped_column(Text)
    table_count: Mapped[int | None] = mapped_column(Integer)
    approx_rows: Mapped[int | None] = mapped_column(BigInteger)
    approx_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    # 权限不可见时置 false，用于"该账号看不到 N 张表"的黄条告警（§6）
    is_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    sync_job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("sync_jobs.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MetaTable(Base):
    __tablename__ = "meta_table"
    __table_args__ = (
        CheckConstraint("table_type IN ('BASE TABLE','VIEW')", name="table_type_allowed"),
        UniqueConstraint(
            "datasource_id", "catalog_name", "schema_name", "table_name", name="uq_meta_table_key"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    # ADR-0005：分隔符必须是 chr(31)（单元分隔符）——用 `_`/`-` 会让 `a_b`+`c` 与 `a`+`b_c`
    # 撞出同一个 md5。类型是 char(32) 不是 uuid：md5 不是 uuid 形状，定长 32 走 btree 精确命中。
    # 表达式用 `||` 不用 ADR 原文的 concat_ws：PG 里 concat_ws 是 stable，生成列要求 immutable。
    table_uid: Mapped[str] = mapped_column(
        CHAR(32),
        Computed(
            "md5(datasource_id::text || chr(31) || catalog_name || chr(31) || schema_name"
            " || chr(31) || table_name)",
            persisted=True,
        ),
        nullable=False,
        unique=True,
    )
    datasource_id: Mapped[int] = mapped_column(*_ds_fk(), nullable=False)
    database_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("meta_database.id", ondelete="CASCADE"), nullable=False
    )
    catalog_name: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    schema_name: Mapped[str] = mapped_column(Text, nullable=False)
    table_name: Mapped[str] = mapped_column(Text, nullable=False)
    table_type: Mapped[str] = mapped_column(Text, nullable=False)
    # ---- 同步列 vs 人工列（§3）：comment_raw 由同步写，下面三个人工列同步永不覆盖 ----
    comment_raw: Mapped[str | None] = mapped_column(Text)
    comment_zh: Mapped[str | None] = mapped_column(Text)
    business_desc: Mapped[str | None] = mapped_column(Text)
    # 'one row = ?'：对 NL2SQL 准确率贡献最大的一个人工字段
    granularity: Mapped[str | None] = mapped_column(Text)
    engine: Mapped[str | None] = mapped_column(Text)
    row_format: Mapped[str | None] = mapped_column(Text)
    collation: Mapped[str | None] = mapped_column(COLLATION_COL, Text)
    charset: Mapped[str | None] = mapped_column(Text)
    approx_rows: Mapped[int | None] = mapped_column(BigInteger)
    data_bytes: Mapped[int | None] = mapped_column(BigInteger)
    index_bytes: Mapped[int | None] = mapped_column(BigInteger)
    # 统计新鲜度：太旧就该提示 AI 别信 row count
    last_analyze_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 手工排除的临时表/备份表（_old/_bak）
    is_hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # 本次同步未出现 → 打标记而不是物理删（chat_messages 还引用着它，§5）
    is_stale: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MetaColumn(Base):
    __tablename__ = "meta_column"
    __table_args__ = (UniqueConstraint("table_id", "column_name", name="uq_meta_column_key"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    table_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("meta_table.id", ondelete="CASCADE"), nullable=False
    )
    ordinal_position: Mapped[int] = mapped_column(Integer, nullable=False)
    column_name: Mapped[str] = mapped_column(Text, nullable=False)
    # 归一化后（'int' / 'varchar(64)' / 'decimal(18,2)'）与方言原文（longtext / enum('a','b')）
    data_type: Mapped[str] = mapped_column(Text, nullable=False)
    raw_data_type: Mapped[str] = mapped_column(Text, nullable=False)
    nullable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    default_value: Mapped[str | None] = mapped_column(Text)
    is_generated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    comment_raw: Mapped[str | None] = mapped_column(Text)
    comment_zh: Mapped[str | None] = mapped_column(Text)
    business_desc: Mapped[str | None] = mapped_column(Text)
    is_primary_key: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    is_unique: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_indexed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # 中文枚举值直接决定 where 条件能否命中（'已完成' → status='completed'）
    enum_values: Mapped[list[str] | None] = mapped_column(JSONB)
    # 低基数 distinct 值采样，默认关（§1 的 sample_distinct）
    sample_values: Mapped[list[Any] | None] = mapped_column(JSONB)
    char_length: Mapped[int | None] = mapped_column(Integer)
    numeric_precision: Mapped[int | None] = mapped_column(Integer)
    numeric_scale: Mapped[int | None] = mapped_column(Integer)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MetaIndex(Base):
    __tablename__ = "meta_index"
    __table_args__ = (UniqueConstraint("table_id", "index_name", name="uq_meta_index_key"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    table_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("meta_table.id", ondelete="CASCADE"), nullable=False
    )
    index_name: Mapped[str] = mapped_column(Text, nullable=False)
    is_unique: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # BTREE / FULLTEXT / SPATIAL / HASH（PG 侧是 gin/gist）
    index_type: Mapped[str] = mapped_column(Text, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    is_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # cardinality / sub_part 非空 = 没有退回逐表 SHOW CREATE TABLE 方案（§10 末验收锚点）
    cardinality: Mapped[int | None] = mapped_column(BigInteger)
    funcdef: Mapped[str | None] = mapped_column(Text)


class MetaIndexColumn(Base):
    __tablename__ = "meta_index_column"
    __table_args__ = (
        UniqueConstraint("index_id", "seq_in_index", name="uq_meta_index_column_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    index_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("meta_index.id", ondelete="CASCADE"), nullable=False
    )
    seq_in_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # NULL 表示这一位是表达式索引的表达式，名字落在 funcdef 里
    column_name: Mapped[str | None] = mapped_column(Text)
    collation: Mapped[str | None] = mapped_column(COLLATION_COL, Text)
    # 前缀索引长度（MySQL 特有，SQLAlchemy Inspector 会把它丢掉）
    sub_part: Mapped[int | None] = mapped_column(Integer)


class MetaRelation(Base):
    """外键 + 命名约定推断 + 人工补录，统一一张表（§2.4）。"""

    __tablename__ = "meta_relation"
    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('extracted','inferred','manual')", name="source_kind_allowed"
        ),
        UniqueConstraint(
            "datasource_id",
            "source_kind",
            "from_table_id",
            "from_column_name",
            "to_table_id",
            "to_column_name",
            name="uq_meta_relation_key",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    datasource_id: Mapped[int] = mapped_column(*_ds_fk(), nullable=False)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    fk_name: Mapped[str | None] = mapped_column(Text)
    from_table_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("meta_table.id", ondelete="CASCADE"), nullable=False
    )
    from_column_name: Mapped[str] = mapped_column(Text, nullable=False)
    to_table_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("meta_table.id", ondelete="CASCADE"), nullable=False
    )
    to_column_name: Mapped[str] = mapped_column(Text, nullable=False)
    on_delete: Mapped[str | None] = mapped_column(Text)
    on_update: Mapped[str | None] = mapped_column(Text)
    deferability: Mapped[str | None] = mapped_column(Text)
    # 只有 inferred 才打分；≥0.8 才进 prompt（§4）
    confidence: Mapped[float | None] = mapped_column(
        Numeric(4, 3), server_default=text("1.0"), default=1.0
    )
    # "逻辑外键存在但库没建约束"这类现实情况
    is_authors_enforced: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SyncJob(Base):
    """同步作业（§2.5）：`partial` 是必需态，元数据表带 synced_at 天然支持部分成功。"""

    __tablename__ = "sync_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','success','partial','failed','cancelled')",
            name="status_allowed",
        ),
        CheckConstraint(
            "phase IN ('connect','discover','tables','columns','indexes','fks',"
            "'card_build','embed','done')",
            name="phase_allowed",
        ),
        # "同一数据源只有一个未结束 job" 由数据库保证，不是代码 race（§6）
        Index(
            "ux_sync_running",
            "datasource_id",
            unique=True,
            postgresql_where=text("status IN ('pending','running')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    datasource_id: Mapped[int] = mapped_column(*_ds_fk(), nullable=False)
    triggered_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'"), default="pending"
    )
    phase: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'connect'"), default="connect"
    )
    progress: Mapped[float] = mapped_column(
        Numeric(5, 2), nullable=False, server_default=text("0"), default=0
    )
    counters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    warnings: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    errors: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), default=list
    )
    # 与上次成功同步的指纹，命中就短路（§6 第 3 条实现约束）
    manifest_digest: Mapped[str | None] = mapped_column(Text)
    # 僵尸回收依据：主事务未提交时靠独立连接更新它，前端进度才动得起来
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "MetaColumn",
    "MetaDatabase",
    "MetaIndex",
    "MetaIndexColumn",
    "MetaRelation",
    "MetaTable",
    "SyncJob",
]
