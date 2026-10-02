"""知识库卡片两张表（列定义逐条对 `docs/metadata-model.md` §2.6）。

一句话口径：卡片是 prompt 的唯一素材，profile 是"embedding 模型 + 维度 + 模板版本"的一等公民——
换模型时必须新建 profile 全量重建，不能让新旧向量混在同一个索引里。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    literal_column,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.settings import get_settings


class KbIndexProfile(Base):
    __tablename__ = "kb_index_profile"
    __table_args__ = (
        UniqueConstraint("name", name="uq_kb_index_profile_name"),
        # 三态词表的库内落点（ADR-0013）。不能靠 is_active + built_at 推导：「建好待切」与
        # 「已退役」在库里同形（都是 NOT is_active 且 built_at NOT NULL），
        # 推不出来的那一态只能是一列。
        CheckConstraint("status IN ('draft','active','retired')", name="status_allowed"),
        # 「全表至多一行 active」没有自然键列，所以键是表达式本身：命中谓词的行全取同一个值 true，
        # 第二行 active 当场撞唯一约束。见 ADR-0013。
        Index(
            "ux_kb_index_profile_active",
            text("(status = 'active')"),
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    # 形如 `text-embedding-3-small@1536@tplv1`：一眼能看出这批卡是谁生成的
    name: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    # 与 settings.embedding.dimension 同源，但它是**历史事实**：换维度就换新 profile
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    card_template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    distance_fn: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'cosine'"))
    # 档案的状态机（ADR-0013）。`is_active` 从此只是它的镜像、不是第二真相——但双写还没接上：
    # 027 关 `profile_row(is_active=True)` 那条后门，031 换读侧的尺子并把 `is_active` 删掉。
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 全量重建完成的时间；NULL = 这套 profile 还没灌完，检索侧不该用它
    built_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KbCard(Base):
    __tablename__ = "kb_card"
    __table_args__ = (
        CheckConstraint("kind IN ('table','table_columns','term')", name="kind_allowed"),
        UniqueConstraint("doc_uid", name="uq_kb_card_doc_uid"),
        # 五条索引各守一条检索路（§2.6 + ADR-0012）：两套维度的向量 / trgm 模糊 / simple FTS /
        # 源内软删过滤。
        # HNSW 选它而不是 IVFFlat 的硬理由：IVFFlat 要先有数据训练、空表建不了，
        # 而同步是"先建空卡再灌"的流程。WITH 里的两个数是文档给的固定调参。
        Index(
            "ix_kb_card_emb",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
        # 第二套维度的向量（ADR-0012）：调参与 ix_kb_card_emb 逐字同调，两条索引互不干扰。
        # `embedding_1024` 这个字面在源码里只有三处——那份唯一的映射（`core/vector_columns.py`，
        # 工单 029 让它出生，031 的读侧复用同一个）、这里的列与索引定义、迁移 0008 的加列 DDL；
        # 机械门是 `test_vector_columns.py::test_列名字面在源码里只出现在映射与列定义本身`，
        # 第四处出现当场红（`1536 → embedding` 那一半扫不了，理由写在那条用例的头部）。
        Index(
            "ix_kb_card_emb_1024",
            "embedding_1024",
            postgresql_using="hnsw",
            postgresql_ops={"embedding_1024": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
        Index(
            "ix_kb_card_search_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
        # 'simple' 配置不做词干切分——默认配置会把中文整句切碎，关键词路就废了。
        # 列引用必须是 text() 而不是裸字符串：函数参数位置上的 str 会被当成字面量，
        # 渲染出 `to_tsvector('simple', 'search_text')`，建索引当场报"列不存在"。
        Index(
            "ix_kb_card_search_fts",
            func.to_tsvector(literal_column("'simple'"), text("search_text")),
            postgresql_using="gin",
        ),
        Index(
            "ix_kb_card_ds_kind",
            "datasource_id",
            "kind",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    datasource_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # term 卡（人工录入的业务术语）不挂在任何表上，所以可空；表卡跟着表一起没。
    table_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("meta_table.id", ondelete="CASCADE")
    )
    # 同一张表切多卡时的段号，主卡恒为 0（kb-workflow §6）
    seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    # md5(kind|table_uid|seq|index_profile_id)：重建卡片靠它幂等 upsert，不会越建越多
    doc_uid: Mapped[str] = mapped_column(CHAR(32), nullable=False)
    # profile 是卡片的一等公民列：换模型/改模板 = 新建 profile 全量重建，
    # 检索只命中 active profile 的卡（§2.6 末）。RESTRICT：删 profile 前必须先删它的卡。
    index_profile_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("kb_index_profile.id", ondelete="RESTRICT"), nullable=False
    )
    # 'db.orders（订单表）'——§2.6 没给它 NOT NULL：源库连表注释都没有时，标题只能是全名，
    # 全名再缺就没有这张卡了，所以留可空而不是拿空串冒充标题。
    title: Mapped[str | None] = mapped_column(Text)
    # 送 embedding 与拼 prompt 的完整文档 / 关键词检索用的扁平文本（去【】与项目符）
    text_md: Mapped[str] = mapped_column(Text, nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # {approx_rows, column_count, tags, confidence}：检索命中后给人看的旁证，不参与向量
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    # 可空：007 的同步只保证"卡片在库里"，向量化是 P4 的另一条路（ADR-0003）
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 哪一次同步造出了这张卡；界面"这批卡片有多新"直接读它，不靠猜
    sync_job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("sync_jobs.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 列宽 = settings.embedding.dimension（metadata-model §2.6 的 `vector(dim)` 不硬编码）。
    # 可空 = ADR-0003 的"未启用向量端点时卡片照常生成"：链路不依赖它，检索才依赖。
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(get_settings().embedding.dimension)
    )
    # 第二套维度（ADR-0012）：换 embedding 模型时新旧两批向量必须同时在场，激活与回滚才走得通。
    # 读哪一列由**这一批卡所属档案**的 dimensions 决定（普通同步=active 那行，reindex=draft 那行），
    # 而那层映射只住在一个纯函数里（`core/vector_columns.py`）。
    embedding_1024: Mapped[list[float] | None] = mapped_column(Vector(1024))


__all__ = ["KbCard", "KbIndexProfile"]
