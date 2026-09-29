"""`aiweb.chat_sessions` / `aiweb.chat_messages` —— 列定义逐条对 `docs/metadata-model.md` §2.7。

一句话形状：**一条问数留两行，行集永远不在这两张表里**（ADR-0004：结果集落
`data/results/*.csv`）。所以 `chat_messages` 只有列定义（`result_columns`）和统计
（`result_stats`，含结果文件引用），没有任何装"行"的列。

两支与别处不同的口径：
- `datasource_id` 可空且 **SET NULL**：删源不该把"当时问过什么"这条审计链一起删掉，
  而 `data_sources`/`meta_*` 那一套都是 CASCADE——会话是用户资产，不是源的从属物。
- `prompt_tokens`/`completion_tokens` 是 NOT NULL 恒 0（§2.7 as-built ③）：审计列宁可为 0
  也不放估算值；`llm_client.complete()` 今天不把响应体的 `usage` 带回来。
"""

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
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (Index("ix_chat_sessions_user_id", "user_id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # 可空：一次会话可以在选定数据源之前就被建出来（"新建对话"先占位）。
    datasource_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("data_sources.id", ondelete="SET NULL")
    )
    # 缺省拿首问原文当标题，所以 NOT NULL；改名是 P9 的事。
    title: Mapped[str] = mapped_column(Text, nullable=False)
    is_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint("role IN ('user','assistant','system')", name="role_allowed"),
        Index("ix_chat_messages_session_id", "session_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    session_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    # 改写前原文 / 术语对齐后。① 改写开关 `AIWEB_RETRIEVAL__ENABLE_REWRITE` 缺省 false，
    # 关掉时两列同值等于冗余，所以 P2 只写 question_text（§2.7 as-built ①）。
    question_text: Mapped[str | None] = mapped_column(Text)
    question_resolved: Mapped[str | None] = mapped_column(Text)
    # ★可解释性的载体：[{card_id,table_uid,score_kw,score_vec,fused}]。
    # P2 只有 score_kw 有值，向量与融合是 P4——留 null 而不是不留，为的是能区分"没命中"和"没算"。
    retrieved: Mapped[list[Any] | None] = mapped_column(JSONB)
    prompt_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    # LLM 原文 vs 守卫重写后实际执行的那句（含注入的 LIMIT）——两列都要留，
    # 否则"守卫改了什么"这条 tracc 就断了。
    sql_raw: Mapped[str | None] = mapped_column(Text)
    sql_final: Mapped[str | None] = mapped_column(Text)
    guard_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # 三类早退（检索为空 / 畸形返回 / 守卫拒绝）都落成 executed=false + error_code：
    # 拒了但没痕迹等于没拒（§2.7 as-built ④）。
    executed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    # 结果侧只有"列定义 + 统计（含结果文件引用）"，没有行。早退时这两列是 null。
    result_columns: Mapped[list[Any] | None] = mapped_column(JSONB)
    result_stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # {type,x,series,title,top_n,other_label,fallback}：存的就是 chart_advisor 的形状，
    # EChartsOption 由 P8 的前端从这个形状生成，不在库里存前端配置。
    chart_spec: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    conclusion: Mapped[str | None] = mapped_column(Text)
    # 这两列可空而不是恒 0/空串：`no_schema_found` 那条早退发生在第一次 LLM 往返之前，
    # 当时既没有耗时也没有模型可写。
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    model: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["ChatMessage", "ChatSession"]
