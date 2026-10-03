"""知识库卡片的 DTO（architecture §7 的 `/kb/cards`）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# `POST /kb/search` 的三路开关（architecture §5.2：`keyword` 只走 (2)，`vector` 只走 (1)，
# `hybrid` 两路各自出名次再 RRF）。住在 schema 层是因为 import 方向只有一个：
# `retriever` 本来就 import 这一层，反过来会成环。
SearchMode = Literal["keyword", "vector", "hybrid"]


class KbCardProfileOut(BaseModel):
    """§7 点名要的"embedding 元信息（维度/模型/建索引时间）"里属于 profile 的那三个。

    `name` 是 §2.6 的 `model@dim@tplv` 拼法，给人一眼看出这批卡是谁生成的；
    `provider` 不露：P2 只有 `openai_compatible` 一种，露出来前端只会多写一个用不上的分支。
    030 的 `/kb/profiles/{id}/activate` 也复用这一形状，所以这里不另建一个"档案摘要"模型。
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


class KbReindexRequest(BaseModel):
    """`POST /api/kb/reindex` 的 body（architecture §7，工单 029）。

    只有 `datasource_id` 一个字段——reindex 是**按数据源**的一颗作业（spec 决策 ②），
    不接受"顺手改一下这套档案的维度/模型"：那三格是 settings 的事实，改基准的办法是
    改 `.env` 再 reindex，而不是让请求体绕过配置通道（ADR-0012 的"档案是历史事实"）。
    """

    model_config = ConfigDict(extra="forbid")

    datasource_id: int


class KbReindexAccepted(BaseModel):
    """202 那一帧：两颗 id，**没有**任何计数（同 `SyncJobAccepted` 的理由——作业还没跑）。

    `profile_id` 是 spec 故事 7 的落点：030 的 `/kb/profiles/{id}/activate` 要知道"刚才建的
    是那套 draft"，靠返回值而不是靠猜。它与 `job_id` 同源进行里（`sync_jobs.reindex_profile_id`），
    所以重放 `sync_job_event` 也查得到它。
    """

    job_id: int
    profile_id: int


class KbActivateOut(BaseModel):
    """`POST /api/kb/profiles/{id}/activate` 的 200（architecture §7 那行的"档案行"）。

    两格而不是整行：`created_at`/`built_at`/`provider` 这些诊断字段这一片没有读取点，
    §6 的档案列表端点也不建（spec 明确不做），露出来只会诱导前端去猜它们的含义。
    `retired` 可为 `null`——库里本来没有 active 时那一次上线确实没有"被换下去的那一行"，
    编一个零值摘要就等于在响应里写一件库里不存在的事（029 的 202 只回两颗 id 同一条口径）。
    """

    activated: KbCardProfileOut
    retired: KbCardProfileOut | None


class KbSearchRequest(BaseModel):
    """`POST /kb/search` 的 body（§7）。

    P4（工单 031）收 `mode / top_vector / ef_search` 三个旋钮，但仍收不下 §7 那行原来列的第四个
    ——`trgm_threshold` 是 (2b) 那一路的分数门槛，只取 `AIWEB_RETRIEVAL__TRGM_THRESHOLD`；
    `rrf_k` 也不给：它管的是**全库的融合次序**（§5.2 (3)），进请求体等于让一次调用改写排序规则
    （spec 故事 27 的落法）。两者传上来都是 422，不是静默忽略（`extra="forbid"`）。

    **三个旋钮都不带默认值**：缺省即 `None`，由 `resolve_tuning()` 回落到配置或向量路自己的
    窗口。这里写死一个数就是第三个答案——roadmap 验收 3 的 `ef_search=64` 与 §7 示例的 100
    本来就要靠"请求体不给 → 配置说了算"这条规则收口。
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=200)
    # 空 = 在我看得见的所有源里找；非空 = 只在指定的这几个里找
    datasource_ids: list[int] = Field(default_factory=list)
    k: int = Field(default=5, ge=1, le=50)
    # §5.2 的三路开关。没给 = 配齐向量端点走 hybrid、没配走 keyword（§5.1 的降级判据）；
    # 明确给了 `vector`/`hybrid` 而端点没配**不会**被悄悄降级，见 `resolve_tuning()`。
    mode: SearchMode | None = None
    # 向量路的召回窗口（§5.2 (1) 的 `LIMIT $top_vector`）。只 `ge=1`：上界不归本片发明，
    # 大到 HNSW 扫不动是 PG 自己的事，它的失败会在这一发查询里如实长出来。
    top_vector: int | None = Field(default=None, ge=1)
    # 查询期 HNSW 邻居预算，下发形态是 `set_config('hnsw.ef_search', …, true)`。
    # 同样不设上界：pgvector 自己认的范围才是这件事的权威。
    ef_search: int | None = Field(default=None, ge=1)


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

    三格里两格是**名次**（从 1 起，工单 032 / §5.2 (8)）、`rrf_score` 是融合出来的那个倒数和，
    三者都不是相似度。
    名次的 `null` 有两种含义（这一路整个没跑，还是跑了而这张表不在它那一路里），靠请求那一发的
    `mode` 分辨——所以这三格必须能被序列化成 `null`，也就**不许**给默认值：给 0 等于在响应里写
    "第 0 名"这件没发生过的事。
    """

    table_uid: str
    title: str | None
    score_kw: float
    matched_term_count: int
    matched_column_count: int
    hits: list[KbSearchHitOut]
    cards: list[KbSearchCardOut]
    keyword_rank: int | None
    vector_rank: int | None
    rrf_score: float | None


class KbSearchProfileOut(BaseModel):
    """/kb/search 顶层的 `used_profile`：这一句问句是**被哪套基准答的**（工单 032 / spec 故事 29）。

    三格而不是复用 `KbCardProfileOut` 那五格：`model` 与 `card_template_version` 已经出现在
    `name` 那个拼法里（§2.6：`model@dimensions@card_template_version`），再各自端一次等于给前端
    两个真相源；`provider`/`created_at`/`built_at` 这一片没有读取点
    （spec §6 明确不建档案列表端点）。`dimensions` 同样出现在那个拼法里，**仍然单独端一格**，
    因为 spec 故事 29 的验收要的就是这一格的数值（换维度 = 换 `name` 里那段字面，而界面要报
    "当前 1024 维"不该去切一个给人看的串）——`name` 给人读，`dimensions` 给机器读。
    """

    id: int
    name: str
    dimensions: int


class KbSearchOut(BaseModel):
    items: list[KbSearchItemOut]
    took_ms: int
    # 库里一行 active 档案都没有时是 `null` 而不是零值摘要（029 的 202 同一条口径：
    # 那一行不存在，就不能在响应里给它编一个身份）。
    used_profile: KbSearchProfileOut | None
    # §5.2 (4) 那一步的产物清单：本次端出的那几张表的 uid，顺序与 `items` 一致。
    # 它**不含桥表**：桥表是 (6) 图扩展带回来的，从没进过召回，混进来就把两步的产物并成了一个格子
    # （所以这一格与 `hops_used` 是两件事，一个数聚合、一个数图）。它也**不是**"聚合出但被 `k`
    # 砍掉"的那一档——`Retriever.search()` 返回的已经是收口后的清单，预 `k` 候选集这一格协议不
    # 暴露，工单 032 的"只填不造"因此落在这条线上（要那一格得改返回值，牵动 009/031 已交付的
    # 二十处调用面，归收口片或后续片）。
    aggregated_tables: list[str]
    # 图扩展这一步用掉的跳数，口径跟 014：**数桥表张数**不数边数。单表问句是 0。
    hops_used: int
