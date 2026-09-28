"""知识库卡片的 DTO（architecture §7 的 `/kb/cards`）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class KbCardProfileOut(BaseModel):
    """§7 点名要的"embedding 元信息（维度/模型/建索引时间）"里属于 profile 的那三个。

    `name` 是 §2.6 的 `model@dim@tplv` 拼法，给人一眼看出这批卡是谁生成的；
    `provider` 不露：P2 只有 `openai_compatible` 一种，露出来前端只会多写一个用不上的分支。
    """

    id: int
    name: str
    model: str
    dimensions: int
    card_template_version: int


class KbCardOut(BaseModel):
    """一张卡的一个段落。字段逐个列写，不是把 `kb_card` 整行倒出来。

    与 `datasource_service.render` 同一条理由：表上有 `datasource_id`、`search_text`、
    `embedding` 这些列，整行倒出来等于让"读卡片"顺带漏出卡片属于哪个源、
    以及一大串没人要的浮点数。想露脸得先进这个模型。
    """

    id: int
    kind: str
    seq: int
    doc_uid: str
    title: str | None
    text_md: str
    token_count: int
    meta: dict[str, Any]
    # 建索引时间：NULL = 这张卡还没向量化（ADR-0003 的"卡片先于向量"）
    embedded_at: datetime | None
    index_profile: KbCardProfileOut


class KbSearchRequest(BaseModel):
    """`POST /kb/search` 的 body（§7）。

    §7 那一行还写着 `top_vector / ef_search / trgm_threshold / mode`，P2 一个都不收：它们
    全是向量路的调参，而 P2 只有 ILIKE 一条路（阈值走 `AIWEB_RETRIEVAL__TRGM_THRESHOLD`）。
    不收的方式是**拒**（`extra="forbid"` → 422），不是静默忽略：传了就当场告诉前端"P2 没有这个
    旋钮"，比默默按默认值跑完再让人怀疑参数没生效要诚实得多。
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=200)
    # 空 = 在我看得见的所有源里找；非空 = 只在指定的这几个里找
    datasource_ids: list[int] = Field(default_factory=list)
    k: int = Field(default=5, ge=1, le=50)


class KbSearchHitOut(BaseModel):
    """一条命中理由：哪一列、比的是哪个字段、被哪个词对上的（工单 009 验收 2）。"""

    column_name: str
    field: str
    term: str


class KbSearchCardOut(BaseModel):
    card_id: int
    kind: str
    seq: int
    score: float
    text_preview: str


class KbSearchItemOut(BaseModel):
    """一张候选表。粒度在**表**不在卡——§5.2 (4) 明确"同一张表的多张卡不各自占名额"。

    不带 `datasource_id` / `table_id`：与 `KbCardOut` 同一条理由，`table_uid` 就是对外标识，
    内部自增 id 出现在响应里只会诱导前端拿它拼 URL。
    """

    table_uid: str
    title: str | None
    score_kw: float
    matched_term_count: int
    matched_column_count: int
    hits: list[KbSearchHitOut]
    cards: list[KbSearchCardOut]


class KbSearchOut(BaseModel):
    items: list[KbSearchItemOut]
    took_ms: int
