"""知识库卡片的 DTO（architecture §7 的 `/kb/cards`）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


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
