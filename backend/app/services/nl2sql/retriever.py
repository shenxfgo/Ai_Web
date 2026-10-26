"""检索器与它的两条腿。接缝与配方住在 `docs/architecture.md` §5.1/§5.2。

P2 交付 `LikeRetriever`（L1 结构化关键词），P4 交付 `HybridRetriever`（关键词 + 向量，RRF 融合）。
`Retriever` Protocol 里**没有任何向量概念**（没有 dim、没有 `<=>`、没有 ef_search），所以换实现
时 `pipeline` 与 `/kb/search` 端点一行都不用改——协议上只允许出现"问句 + 候选表 + 分数 + 命中理由"。

分四截读：

1. **切词、命中判定与排序（纯函数，不碰数据库）**：`query_terms` / `column_hits` /
   `rank_candidates`。
2. **融合与裁决（纯函数）**：`rrf_fuse` / `fuse_rankings` / `resolve_tuning`（工单 031）。
3. **两条腿各自的召回（只碰元数据库；向量路另加一次端点调用）**：`_keyword_ranking` 走 §5.2 (2)
   的 ILIKE + pg_trgm，`_vector_ranking` 走 §5.2 (1) 的 `<=>`。**绝不连源库**——源库要等 011
   的只读执行才碰，而且那时也不走这条路。
4. **接缝**：`Retriever`（Protocol）、两个实现，以及全仓唯一的选择点 `_retriever_for()`。
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Protocol, cast

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Float,
    Select,
    String,
    Table,
    bindparam,
    case,
    func,
    literal,
    or_,
    select,
    text,
)
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import TextClause

from app.core.table_names import table_full_name
from app.models.kb import KbCard, KbIndexProfile
from app.models.meta import MetaColumn, MetaTable
from app.models.user import User
from app.schemas.kb import (
    KbSearchCardOut,
    KbSearchHitOut,
    KbSearchItemOut,
    KbSearchOut,
    KbSearchProfileOut,
    KbSearchRequest,
    SearchMode,
)
from app.services import datasource_service
from app.services.embedding_client import EmbeddingClient
from app.services.kb_service import ActiveProfile, read_active_profile, vector_target_for
from app.services.nl2sql import join_graph
from app.services.nl2sql.prompt_builder import SchemaTable
from app.settings import RetrievalGroup, get_settings

# 一段"词"要么是 ASCII 标识符（含下划线），要么是一串连续汉字；其它字符一律当分隔符。
# 问句里的标点、空格、全角括号都是天然断点，不需要额外的停用词表就能切开。
_SEGMENT_RE: Final = re.compile(r"[0-9A-Za-z_]+|[一-鿿]+")

# 汉字串短到这个长度就整段当一个词：`手机号`、`订单额` 这类三字词本来就自成一个语义单元，
# 再切反而丢掉判别力。超过这个长度只能靠二字滑窗近似（PG 侧没有中文分词器）。
_MAX_WHOLE_CJK_TERM: Final = 4

# ASCII 段拆子词的两条边界：下划线（`pay_amount`）与驼峰接缝（`payAmount`→pay|Amount）。
# 单字母子词丢掉：`a`/`id` 里的 `i` 这种做 ILIKE 只会带来噪声命中。
_SUBLINGUAL: Final = re.compile(r"[_\s]+|(?<=[a-z0-9])(?=[A-Z])")


def _ascii_terms(segment: str) -> list[str]:
    """标识符整串在前、子词在后（§5.2 (2a)：精确/前缀那一路权重最高，要保住整串）。"""
    parts = [p for p in _SUBLINGUAL.split(segment) if len(p) >= 2]
    return [segment, *parts]


def query_terms(question: str) -> tuple[str, ...]:
    """把问句切成检索词，保序去重。

    返回的顺序就是权重顺序的草稿：调用方按词逐个匹配，**不同的词**各自计一次命中
    （§5.1 L1 的排序主键，as-built 0009）。
    """
    terms: list[str] = []
    for segment in _SEGMENT_RE.findall(question):
        if segment.isascii():
            terms.extend(_ascii_terms(segment))
        elif len(segment) < 2:
            # 单字汉字段的 ILIKE 只会放大噪声（`查` 命中"查询口径"），丢。
            continue
        elif len(segment) <= _MAX_WHOLE_CJK_TERM:
            # 整段在前、二字词在后：整段更具体（`手机号` 直接对得上注释原话），
            # 二字词兜住"动词把真词粘住"的情况（`查客户` 自己命中不了，`客户` 能）。
            terms.append(segment)
            terms.extend(segment[i : i + 2] for i in range(len(segment) - 1))
        else:
            terms.extend(segment[i : i + 2] for i in range(len(segment) - 1))
    return tuple(dict.fromkeys(terms))


# ============================================================ 收成候选表（纯函数）


@dataclass(frozen=True)
class CardMatch:
    """一张被命中的卡片。

    `score` 是 §5.2 (2) 的关键词分数：**每个词**在 (2a) ILIKE 与 (2b) trgm 上取 max，再**跨词求和**
    ——所以它是"几个词各自多像"的和，不是 0..1 的相似度，P2 也真的能大于 1。
    (2c) 的 `simple` FTS 没接，理由见 `_term_score`。
    """

    card_id: int
    table_id: int
    table_uid: str
    datasource_id: int
    kind: str
    seq: int
    title: str | None
    score: float
    # 卡片正文的开头一小段：预览端点给人肉验证命中用（§7 的 text_preview），不是排序输入
    text_preview: str


@dataclass(frozen=True)
class ColumnHit:
    """一列被某个词命中。`field` 与 `term` 合起来就是界面上的"命中理由"（验收 2）。"""

    table_id: int
    column_name: str
    field: str
    term: str


@dataclass(frozen=True)
class RetrievedTable:
    """候选表。

    010 拼 prompt 与 012 的 `retrieval` 帧（落 `chat_messages.retrieved`）读的都是这一份。

    那三格（工单 032，§5.2 (8)：两路名次 + 融合分）住在这里而不是只住在 `FusedTable` 里，
    是因为出门的只有这一份：
    `/kb/search` 的响应与 `retrieved` 帧都从它读，名次若留在融合层，端点就得自己再数一遍次序——
    而"第几名"是两路各自算出来的事实，不是末端能反推的东西。
    """

    table_uid: str
    table_id: int
    datasource_id: int
    title: str | None
    score_kw: float
    matched_term_count: int
    matched_column_count: int
    hits: tuple[ColumnHit, ...]
    cards: tuple[CardMatch, ...]
    # 两路各自的名次（1 起）与融合分。`None` 有两种含义，靠请求给的 `mode` 分辨：这一路整个没跑
    # （`mode='keyword'` 时的 `vector_rank`），还是跑了而这张表不在它那一路里（hybrid 时只在一条腿
    # 露面的那张）。012 那句"留 null 才能把'没命中'和'没算'分开"的口径继续成立。
    keyword_rank: int | None = None
    vector_rank: int | None = None
    rrf_score: float | None = None
    # 融合名次，由 `fuse_rankings` 亲自盖（不由末端消费者现数——本类上面那句"名次不是末端能
    # 反推的东西"对第三格同样成立）。`None` 就是"RRF 这一路没跑"，`mode='keyword'` 与
    # `mode='vector'` 两条单腿都拿 `None`，所以它同时是"融合发生过没有"的那把尺。
    fused_rank: int | None = None


# §5.2 (4)：同一张表的多张卡不各自占名额，改成给主卡加一点 boost。
_CARD_BOOST: Final = 0.05


@dataclass(frozen=True)
class CandidateColumn:
    """候选表的一列，只带 L1 **计数面**的三个原料。

    §5.1 L1 点名的面有五个（表名/列名/`comment_raw`/`comment_zh`/业务描述），P2 的计数面只有这三个：
    表名与表注释只进得来召回（卡片全文里就有它们，所以它们影响 `score_kw` 与"在不在候选里"），
    不进计数——计数要给 UI 说出"是哪一列对上的"，表级命中给不出这一句。
    `business_desc` 则三头皆空（没有写入点、不进 `search_text`、不进计数面），见工单 009 偏差表。
    """

    table_id: int
    column_name: str
    comment_raw: str | None
    comment_zh: str | None


def _hit_sources(column: CandidateColumn) -> tuple[tuple[str, str | None], ...]:
    """比对顺序 = 可信度顺序：列名最硬，源库原话次之，人工注释再次。

    字段名直接写在这里，不按字符串反射——`_HIT_FIELDS` 那种写法改了属性名只有运行时才炸。
    """
    return (
        ("column_name", column.column_name),
        ("comment_raw", column.comment_raw),
        ("comment_zh", column.comment_zh),
    )


def column_hits(columns: Sequence[CandidateColumn], terms: Sequence[str]) -> list[ColumnHit]:
    """数出"哪一列的哪个字段被哪个词命中"，顺序是 (列, 词, 字段)。

    判定是**子串包含 + casefold**，刻意与 SQL 那一路的 `ILIKE '%term%'` 严格同构——
    两边不同构的话，召回集和命中理由就会互相打不上（分数说命中了，理由里却找不到那条）。
    """
    hits: list[ColumnHit] = []
    for column in columns:
        for term in terms:
            needle = term.casefold()
            for field, haystack in _hit_sources(column):
                if haystack is not None and needle in haystack.casefold():
                    hits.append(
                        ColumnHit(
                            table_id=column.table_id,
                            column_name=column.column_name,
                            field=field,
                            term=term,
                        )
                    )
    return hits


def rank_candidates(
    cards: Sequence[CardMatch], hits: Sequence[ColumnHit], *, k: int
) -> list[RetrievedTable]:
    """按 §5.1 L1 的排序键（as-built 0009：**命中的不同词数** → 命中列数）收敛候选表。

    排序键是 **(命中词数, 命中列数, table_uid)**。主键原本是"命中列数"，在真语料上被
    68 列的 `product_stats_wide` 打穿了——它 10 列全被 `金额` 这**一个** 2-gram 命中，
    就把只命中 5 列但覆盖两个不同词的 `order_main` 挤下了头名（工单 009 验收 1）。
    一个词在一堆同族列上重复命中，说明不了这张表跟问题的相关性；两个不同的词各自命中
    才算两个独立证据。所以词数当主键、列数降为次键（同一批词命中更多列，仍更值得信任），
    `table_uid` 收尾保证并列时不抖（§2.1 RRF 那条"并列名次要稳定排序"是同一条理由）。
    """
    cards_by_table: dict[int, list[CardMatch]] = {}
    for card in cards:
        cards_by_table.setdefault(card.table_id, []).append(card)
    hits_by_table: dict[int, list[ColumnHit]] = {}
    for hit in hits:
        hits_by_table.setdefault(hit.table_id, []).append(hit)

    candidates: list[RetrievedTable] = []
    for table_id, table_cards in cards_by_table.items():
        table_hits = tuple(hits_by_table.get(table_id, ()))
        ordered = tuple(sorted(table_cards, key=lambda c: c.seq))
        candidates.append(
            RetrievedTable(
                table_uid=ordered[0].table_uid,
                table_id=table_id,
                datasource_id=ordered[0].datasource_id,
                title=next((c.title for c in ordered if c.seq == 0), ordered[0].title),
                score_kw=max(c.score for c in table_cards) + _CARD_BOOST * (len(table_cards) - 1),
                matched_term_count=len({h.term for h in table_hits}),
                matched_column_count=len({h.column_name for h in table_hits}),
                hits=table_hits,
                cards=ordered,
            )
        )
    candidates.sort(key=lambda r: (-r.matched_term_count, -r.matched_column_count, r.table_uid))
    # 名次由**这一路自己**报（工单 032）：`mode='keyword'` 那一路不走融合，`keyword_rank`
    # 是响应里唯一非空的那列名次，它只能来自计数键排序后的位次。截断在调用方（`_final_limit`），
    # 只会砍掉尾巴，所以这里给的位次在砍完之后仍然是 1..n 连续。
    return [replace(item, keyword_rank=index + 1) for index, item in enumerate(candidates[:k])]


# ============================================================ 融合与裁决（纯函数）

# 向量路的召回窗口（§5.2 (1) 的 `LIMIT`）。**与关键词路的 `_RECALL_LIMIT` 是两个独立的数**
# （spec 故事 22）：同名同值看着省事，但调召回窗口时会一起挪走另一路的窗口，
# 而那一格的缺省是有用例钉着的（`test_recall_limit_is_80` 一类）。
_VECTOR_TOP_VECTOR: Final = 80


def _rank_map(uids: Sequence[str]) -> dict[str, int]:
    """一串 uid 折成 `uid → 名次`（从 1 起）。次序就是输入次序，这里不看任何分数。"""
    return {uid: index + 1 for index, uid in enumerate(uids)}


@dataclass(frozen=True)
class FusedTable:
    """RRF 融合后的一张表：两路各自的**名次**加融合分数。

    名次可为 `None`，那是"这一路没命中它"而不是"第 0 名"——032 落 `chat_messages.retrieved`
    的 `score_vec` / `fused` 两格时要的就是这个区分（§5.2 (8)：`mode='keyword'` 那一路
    向量名次回到 null，"没命中"与"没算"分得开）。
    """

    table_uid: str
    keyword_rank: int | None
    vector_rank: int | None
    rrf_score: float


def rrf_fuse(
    keyword_uids: Sequence[str], vector_uids: Sequence[str], *, rrf_k: int
) -> list[FusedTable]:
    """§5.2 (3) 的 RRF：`score = Σ 1/(rrf_k + rank)`，**吃两路各自的名次，不吃分数**（ADR-0014）。

    为什么不加分数：两路的量纲根本不一致——关键词路是"每个词取该卡上的最大值再跨词求和"，
    可以大于 1（见 `CardMatch.score`），向量路是 cosine 相似度落在 0..1。相加等于让量纲大的
    那一路说话。名次没有这个问题：两边都是"第几名"。

    并列（分数严格相等）按 `table_uid` 升序收口——同一个库跑两次必须给同一个次序，
    与 §5.1 那条稳定排序是同一条理由。
    """
    keyword_ranks = _rank_map(keyword_uids)
    vector_ranks = _rank_map(vector_uids)
    fused = [
        FusedTable(
            table_uid=uid,
            keyword_rank=keyword_ranks.get(uid),
            vector_rank=vector_ranks.get(uid),
            rrf_score=(1 / (rrf_k + keyword_ranks[uid]) if uid in keyword_ranks else 0.0)
            + (1 / (rrf_k + vector_ranks[uid]) if uid in vector_ranks else 0.0),
        )
        # `dict.fromkeys` 去重且保序：两路都命中的表只出一条，名次对还是两对。
        for uid in dict.fromkeys((*keyword_uids, *vector_uids))
    ]
    fused.sort(key=lambda item: (-item.rrf_score, item.table_uid))
    return fused


def fuse_rankings(
    keyword: Sequence[RetrievedTable], vector: Sequence[RetrievedTable], *, rrf_k: int
) -> list[RetrievedTable]:
    """两条腿的完整次序 → 融合后的次序（§5.2 (3) 的装配那一半）。

    名次从 `rrf_fuse` 来，**表对象从各自的腿来**：一张两路都命中的表要带关键词路那份
    `hits`/`cards` 出门（界面上的"命中理由"只有 ILIKE 那一路给得出，向量路没有理由可说）。
    所以这里按 uid 建一次反向索引，而不是把两列 `RetrievedTable` 拼起来再排。

    只在向量路出现的表由调用方喂进来（`_vector_ranking` 造的 `RetrievedTable` 里
    `score_kw=0.0`、`hits`/`cards` 为空），融合不看那两个格子。
    """
    fused = rrf_fuse(
        [item.table_uid for item in keyword],
        [item.table_uid for item in vector],
        rrf_k=rrf_k,
    )
    by_uid: dict[str, RetrievedTable] = {}
    # 关键词路排在后，覆盖同 uid 的向量路那份：同一张表有两种形状时，出门的是带命中理由的那一份。
    for item in (*vector, *keyword):
        by_uid[item.table_uid] = item
    # 名次挂在**出门的那一份**上。`replace` 造的是新对象，所以" fused[i] is 那条腿的那个对象"
    # 这种身份断言从今天起不成立——031 的两条用例改成字段等值，被钉的还是同一件事
    # （理由与卡片来自哪条腿），只是不能再靠对象地址说话。
    # `fused_rank` 在这里数、不在消费者那里数：下面调用方那一句 `[: _final_limit(k)]` 只砍尾巴，
    # 所以先数完再砍，砍完之后 1..n 仍然连续；反过来（消费者自己 `enumerate`）就等于把
    # "名额只砍尾巴"这个调用方的性质当成免费事实钉在融合名次上，哪天加一档"按分数重排再截"
    # 就悄悄错位了。
    return [
        replace(
            by_uid[item.table_uid],
            keyword_rank=item.keyword_rank,
            vector_rank=item.vector_rank,
            rrf_score=item.rrf_score,
            fused_rank=index + 1,
        )
        for index, item in enumerate(fused)
    ]


@dataclass(frozen=True)
class SearchTuning:
    """一次检索的三个旋钮，裁决完的结果（请求体 / 配置 / 代码缺省三层的终点）。"""

    mode: SearchMode
    top_vector: int
    ef_search: int


def resolve_tuning(
    *,
    cfg: RetrievalGroup,
    vector_ready: bool,
    mode: SearchMode | None = None,
    top_vector: int | None = None,
    ef_search: int | None = None,
) -> SearchTuning:
    """故事 27：请求给了就用请求的，没给才回落到配置/代码缺省，三格各自独立判。

    只有**缺省的** `mode` 看 `vector_ready`：配齐向量端点才默认 hybrid（§5.1 那句"未配置时
    检索直接走结构化路径、链路不断"，与 `/api/healthz` 的 `retrieval_mode` 同一个判据）。
    请求明确要 `vector`/`hybrid` 而端点没配时，这里**不改口**——悄悄降级等于在响应里写一件
    没发生的事，失败留给那一次真调用去说（`EmbeddingClient` 在未配置时抬
    `EmbeddingNotConfigured`，端点缺哪一格由它逐格点名）。

    `rrf_k` 不在这里：它只住 `.env`，请求体收了就是范围蔓延（spec 故事 27 的落法）。
    """
    return SearchTuning(
        mode=mode if mode is not None else ("hybrid" if vector_ready else "keyword"),
        top_vector=top_vector if top_vector is not None else _VECTOR_TOP_VECTOR,
        ef_search=ef_search if ef_search is not None else cfg.hnsw_ef_search,
    )


# ============================================================ 召回（只碰元数据库）

# §5.2 (2) 关键词路的召回窗口：先粗召回再聚合到表，最终名额由 `k` 收口。
_RECALL_LIMIT: Final = 80
# §5.2 (1) 括号里的 `iterative_scan`：HNSW 一边扫一边被 WHERE 滤掉邻居时，靠它继续往下凑满结果。
# 取值那件事的实测证据写在 `hnsw_tuning_statement()` 的文档串里（同一个事实不两处刻）。
_ITERATIVE_SCAN: Final = "relaxed_order"
# (2a) 精确/前缀那一路的权重：文档给的就是 1.0，标识符对上就是最硬的信号。
_EXACT_SCORE: Final = 1.0
# 预览端点给正文开头的截断长度：够人眼判断"命中的是不是这张表"，又不至于把整卡刷进日志。
_PREVIEW_CHARS: Final = 200

# 与 kb_service 同一套写法：列级 SELECT 走 Core 的 Table 对象，别让 ORM 把整行实体拉回来。
_CARD = cast("Table", KbCard.__table__)
_PROFILE = cast("Table", KbIndexProfile.__table__)
_META_TABLE = cast("Table", MetaTable.__table__)
_META_COLUMN = cast("Table", MetaColumn.__table__)


def _like_pattern(term: str) -> str:
    """把词包成 ILIKE 的模式，并转义 LIKE 的元字符。

    切词允许 ASCII 段带下划线（`pay_amount`），而 `_` 在 LIKE 里是"任意一个字符"——
    不转义的话 `pay_amount` 会连 `payXamount` 一起命中，命中理由就开始说谎了。
    两处 ILIKE（WHERE 的召回判定与分数里的 (2a) 那一路）编译出来都带 `ESCAPE '\'`，
    所以转义串在两路上同义——这一点是 compile 出来核过的，不是想当然。
    """
    escaped = term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    return f"%{escaped}%"


def _term_score(term: str, *, threshold: float) -> Any:
    """一个词在一张卡上的分数 = §5.2 (2) 那几路的 max。

    (2c) 的 `simple` FTS 这一路 P2 不接：PG 没有中文分词器，`simple` 配置会把整串中文当成
    一个 lexeme（§5.2 末那条硬事实），对 L1 等于零贡献；接上只会让 SQL 多一个从不命中的分支。
    """
    text_col = _CARD.c.search_text
    similarity = func.similarity(text_col, term)
    return func.greatest(
        case((text_col.ilike(_like_pattern(term), escape="\\"), _EXACT_SCORE), else_=0.0),
        case((similarity >= threshold, similarity), else_=0.0),
    )


def _recall_statement(
    terms: Sequence[str], *, profile_id: int, datasource_ids: Sequence[int], threshold: float
) -> Select:
    """§5.2 (2) 的关键词路：命中任一词的卡片，带上分数，按**同一个分数**取前 `_RECALL_LIMIT` 张。

    排序键必须就是选出来的那一列（`score`，每词取 max、跨词求和），不能是"各词 max 的 max"：
    后者会让"十个词全精确命中的强卡"和"一个词精确命中的弱卡"在窗口裁剪时并列 1.0，
    截掉谁只取决于 `card.id`——被裁掉的恰恰是最该留的那张。并列时按 `card.id` 升序，
    保证同一个库跑两次是同一个次序（§5.1 那条"并列名次要稳定"是同一条理由）。

    只 JOIN `meta_table` 取 `table_uid`，不 JOIN 任何源库对象——L1 的全部原料都在元数据库里。
    """
    card = _CARD
    scores = [_term_score(term, threshold=threshold) for term in terms]
    total = scores[0]
    for one in scores[1:]:
        total = total + one
    return (
        select(
            card.c.id,
            card.c.table_id,
            card.c.datasource_id,
            card.c.kind,
            card.c.seq,
            card.c.title,
            _META_TABLE.c.table_uid,
            func.left(card.c.text_md, _PREVIEW_CHARS).label("text_preview"),
            total.label("score"),
        )
        .join(_META_TABLE, _META_TABLE.c.id == card.c.table_id)
        .where(
            card.c.deleted_at.is_(None),
            card.c.index_profile_id == profile_id,
            card.c.datasource_id.in_(datasource_ids),
            or_(*[card.c.search_text.ilike(_like_pattern(t), escape="\\") for t in terms]),
        )
        .order_by(total.desc(), card.c.id)
        .limit(_RECALL_LIMIT)
    )


def hnsw_tuning_statement(ef_search: int) -> TextClause:
    """§5.2 (1) 括号里那两格查询期设置，合成一条语句发。

    为什么不是 `SET LOCAL hnsw.ef_search TO :ef`：PG 的 `SET` 不接绑定参数（它后面不是表达式），
    绑上去是 syntax error；`set_config(name, value, is_local)` 是同一件事的函数形态，
    第三格 `true` 就是 LOCAL 的语义——只活到本事务结束，连接还回池子时不带这次的 ef。

    值一律走绑定：`ef_search` 的源头是请求体，拼串等于把注入面开在一条每次检索都发的语句上。

    `iterative_scan` 的取值是 **`relaxed_order`（单数）**。这一格在编译期抓不到：HNSW 的 GUC
    在扩展于本会话装载**之前**只是占位符，`set_config` 对任何拼写都照收不误；装载之后才认
    `off | relaxed_order | strict_order`，而 `docs/architecture.md` §5.2 原文写的 `relaxed_orders`
    在那一刻会抬 `invalid value for parameter`（这台机器 PG 18.6 / pgvector 0.8.6，实测见
    `test_kb_search_hybrid_pg.py`，文档由 031 的文档半边订正）。
    """
    return text(
        "SELECT set_config('hnsw.ef_search', :hnsw_ef_search, true),"
        " set_config('hnsw.iterative_scan', :hnsw_iterative_scan, true)"
    ).bindparams(
        bindparam("hnsw_ef_search", value=str(ef_search), type_=String),
        bindparam("hnsw_iterative_scan", value=_ITERATIVE_SCAN, type_=String),
    )


def vector_recall_statement(
    *, column: str, profile_id: int, datasource_ids: Sequence[int], top_vector: int
) -> Select:
    """§5.2 (1) 的向量路：按余弦**距离**升序取 `top_vector` 张卡（距离升序 = 相似度降序）。

    `column` 只准来自 `vector_column_for()` 那份唯一映射（ADR-0012：换维度是同表加第二列，
    读侧必须知道读的是哪一列）。它进的是 `_CARD.c[column]` 这个受控列对象，不是拼进 SQL 的串，
    所以这里没有注入面——但也没有第二处知道列名。

    WHERE 里三个谓词少一个都不当场报错，失效形状都长得像"一切正常"：
    - `index_profile_id = active`：漏了就是把两套档案的向量混进同一个 ORDER BY（ADR-0012 立论
      要防的正是它，而新旧向量混排出来的次序看起来完全合理）。
    - `datasource_id IN (可见源)`：决策 ⑨/ADR-0008 的落点——权限过滤**进这一句**，不是
      "先取全局 top-80 再筛"（召回窗口会被无权源的卡占满，筛完是空集，而库里明明有权可查的表）。
      谓词形态与关键词路同一条，两路同一把尺。
    - `<列> IS NOT NULL`：ADR-0003「卡片先于向量」的读侧一半。NULL 排不出距离，而 PG 把 NULL
      排在升序末尾——于是"还没灌向量"伪装成"最不相关"，并在 `top_vector` 大于真向量卡数时来占名额。

    `score` 选的是 `1 - 距离`（相似度），只给人看：RRF 吃的是这条 ORDER BY 的**名次**（ADR-0014）。
    """
    card = _CARD
    target = card.c[column]
    # `<=>` 的返回类型得**当场写明**是 Float：`op()` 默认把左边列的类型（Vector）当成结果类型续上，
    # 于是 `1 - 距离` 里的整型 1 会走 Vector 的绑定处理器，报 `expected list or ndarray`。
    # 这一格在 SQL 文本上看不出来（两种写法渲染出的串一模一样），所以只有真库能抓到它。
    distance = target.op("<=>", return_type=Float)(
        bindparam("query_vector", type_=Vector()).cast(Vector())
    )
    return (
        select(
            card.c.id,
            card.c.table_id,
            card.c.datasource_id,
            card.c.kind,
            card.c.seq,
            card.c.title,
            _META_TABLE.c.table_uid,
            (literal(1) - distance).label("score"),
        )
        .join(_META_TABLE, _META_TABLE.c.id == card.c.table_id)
        .where(
            card.c.deleted_at.is_(None),
            card.c.index_profile_id == profile_id,
            card.c.datasource_id.in_(datasource_ids),
            target.is_not(None),
        )
        .order_by(distance)
        # 具名绑定而不是匿名：§5.2 (1) 那句写的就是 LIMIT $top_vector，具名还让
        # 「这一格到底下发了什么值」在真库上可查（切片 D 的用例读 compiled.params）。
        .limit(bindparam("top_vector", value=top_vector))
    )


async def _active_profile_id(session: AsyncSession) -> int | None:
    """当前生效的那套档案的 id。检索只命中它的卡（§2.6 末：新旧两套混在一个索引里不可解释）。

    "哪一行在服务"这个问题在工单 027 之后全仓只有一个答案（`kb_service.read_active_profile`，
    尺子是 `status='active'`）。这里留着这层投影，是因为召回和按表回读两处都只要 id——
    两处各写一遍谓词的话，换一次尺子就要人肉数一遍调用点；031 收掉 `is_active` 那条镜像列
    能一处收完，靠的正是这一层。
    """
    active = await read_active_profile(session)
    return None if active is None else active.index_profile_id


async def _candidate_columns(
    session: AsyncSession, table_ids: Sequence[int]
) -> list[CandidateColumn]:
    """把候选表的列原料读回来喂 `column_hits`。

    只在**候选表**这一小集合上读（≤`_RECALL_LIMIT` 张卡对应的表），按 ordinal_position 排，
    这样命中理由的顺序就是卡片正文里字段的顺序。
    """
    rows = (
        (
            await session.execute(
                select(
                    _META_COLUMN.c.table_id,
                    _META_COLUMN.c.column_name,
                    _META_COLUMN.c.comment_raw,
                    _META_COLUMN.c.comment_zh,
                )
                .where(_META_COLUMN.c.table_id.in_(table_ids))
                .order_by(_META_COLUMN.c.table_id, _META_COLUMN.c.ordinal_position)
            )
        )
        .mappings()
        .all()
    )
    return [
        CandidateColumn(
            table_id=int(row["table_id"]),
            column_name=str(row["column_name"]),
            comment_raw=row["comment_raw"],
            comment_zh=row["comment_zh"],
        )
        for row in rows
    ]


# ============================================================ 两路共用的前置与关键词路


def _final_limit(k: int | None) -> int:
    """`k` 缺省时取配置那一格。**两种实现、三条 mode 都从这里取名额**——名额裁决若有两处，
    `mode='keyword'` 那条"与 P2 逐字同构"（故事 26）就得逐处再证一遍。
    """
    return k if k is not None else get_settings().retrieval.final_tables_k


async def _search_scope(
    session: AsyncSession, *, actor: User, datasource_ids: Sequence[int]
) -> tuple[ActiveProfile, tuple[int, ...]] | None:
    """两路共用的两个前置：actor 看得见的源、当前生效的那套档案。缺任何一个回 `None`。

    `None` 时**故意不抬错**：Protocol 的返回类型是 `list[RetrievedTable]`，改成抛异常会连累
    010/012 的调用方，而"没有生效 profile"和"问句没命中"在 P2 都是同一个答案——空候选。
    可诊断性归 013：那里的 `NO_SCHEMA_FOUND` 分类要把这两种分开，给不同的下一步
    （前者是"去跑同步/建卡片"，后者是"换个说法或补录知识"）。

    返回的是整份 `ActiveProfile` 而不是只有 id：向量路还要拿它的 `dimensions` 定列
    （ADR-0012），而"再读一次档案行"等于把"哪套在服务"这件事在同一句问句里问两遍。
    """
    visible = {ds.id for ds, _ in await datasource_service.list_visible(session, actor)}
    scoped = visible & set(datasource_ids) if datasource_ids else visible
    if not scoped:
        return None

    profile = await read_active_profile(session)
    if profile is None:
        return None
    return profile, tuple(sorted(scoped))


async def _keyword_ranking(
    session: AsyncSession, *, terms: Sequence[str], profile_id: int, scoped: Sequence[int]
) -> list[RetrievedTable]:
    """§5.2 (2) 的关键词路，回**完整次序**（不截断）。

    截断留给调用方是有意的：`rank_candidates` 的 `k` 就是最终名额，而融合要的是一条完整次序
    ——关键词路第 6 名、向量路第 1 名的那张表，它的名次必须在，不能被名额先砍掉。
    这里给 `k` 传卡片数（≥ 候选表数），等于"这一路不砍"。
    """
    rows = (
        (
            await session.execute(
                _recall_statement(
                    terms,
                    profile_id=profile_id,
                    datasource_ids=scoped,
                    threshold=get_settings().retrieval.trgm_threshold,
                )
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        return []

    cards = [
        CardMatch(
            card_id=int(row["id"]),
            table_id=int(row["table_id"]),
            table_uid=str(row["table_uid"]),
            datasource_id=int(row["datasource_id"]),
            kind=str(row["kind"]),
            seq=int(row["seq"]),
            title=row["title"],
            score=float(row["score"]),
            text_preview=str(row["text_preview"]),
        )
        for row in rows
    ]
    hits = column_hits(
        await _candidate_columns(session, sorted({c.table_id for c in cards})), terms
    )
    return rank_candidates(cards, hits, k=len(cards))


# ============================================================ 向量路（只碰元数据库 + 端点）


def _vector_table(rows: Sequence[RowMapping]) -> RetrievedTable:
    """一张表在向量路里的形状：只有身份字段，没有命中理由。

    这是本片补的一条口径（工单 031 的字面只说了"融合吃名次"）：`hits`/`cards` 那两格是
    §5.2 (2) 的 ILIKE 命中计数，向量路给不出"哪一列被哪个词对上"这句话——端点只回了一串
    浮点数，硬凑一条理由等于替它编造它没说过的话。`score_kw=0.0` 同一条理由：那一格是
    关键词路的和分数，向量路的相似度量纲不同（0..1），填进去就是把两路的分数混进一个格子
    （ADR-0014 立论要防的正是这个）。032 扩 `vector_rank`/`rrf_score` 三列时，这一格才第一次
    有地方说"我是被向量路带上来的"。

    `title` 的取法与关键词路同一条规则（主卡 `seq=0` 优先，没有主卡就退回这张表的第一张卡）：
    同一张表在两路印出不同标题，界面上就成了两张表。
    """
    first = rows[0]
    return RetrievedTable(
        table_uid=str(first["table_uid"]),
        table_id=int(first["table_id"]),
        datasource_id=int(first["datasource_id"]),
        title=next((row["title"] for row in rows if int(row["seq"]) == 0), first["title"]),
        score_kw=0.0,
        matched_term_count=0,
        matched_column_count=0,
        hits=(),
        cards=(),
    )


async def _vector_ranking(
    session: AsyncSession,
    *,
    question: str,
    profile: ActiveProfile,
    scoped: Sequence[int],
    tuning: SearchTuning,
) -> list[RetrievedTable]:
    """§5.2 (1) 的向量路：问句 → 端点取一根向量 → `<=>` 距离升序取 `top_vector` 张卡 → 聚合到表。

    三处失败各有各的形状，**这里一处都不兜**：
    - 端点没配、连不上、回的宽度不对：`EmbeddingClient` 那一族 `AppError` 原样抬出去。兜住它
      等于把"这台机器没启用向量路"翻译成"这句问句没命中"，而故事 27 反对的正是这种降级。
    - active 档案那一维没有预置列：`vector_target_for` 抬 `VectorColumnMissing`（工单 031 验收 6）。
      读侧复用写侧那一份映射，全仓因此仍然只有一处知道"1024 落在哪一列"。
    - 一行都没召回：空列表。`top_vector` 大于库里向量卡数是常态（向量列刚建、只灌了一半）。

    先 `hnsw_tuning_statement` 再召回，两条语句走**同一个 session**：§P4 踩坑 ② 说的就是
    SQLAlchemy 惰性 BEGIN——若 `set_config(..., true)` 落在事务之外，它当场生效又当场失效，
    召回用的还是默认那个 40，而 SQL 一个字没变、结果看起来完全正常。切片 D 的单调性用例
    （64 的名次集合 ⊆ 400 的）钉的正是这一格。
    """
    target = vector_target_for(dimensions=profile.dimensions, model=profile.model)
    client = EmbeddingClient(embedding=get_settings().embedding)
    (query_vector,) = await client.embed([question], expected_dimension=target.dimensions)

    await session.execute(hnsw_tuning_statement(tuning.ef_search))
    rows = (
        (
            await session.execute(
                vector_recall_statement(
                    column=target.column,
                    profile_id=profile.index_profile_id,
                    datasource_ids=scoped,
                    top_vector=tuning.top_vector,
                ),
                {"query_vector": query_vector},
            )
        )
        .mappings()
        .all()
    )

    # 距离升序的卡片行聚到表：一张表**第一次**出现的那一行就是它在向量路的名次行，
    # 后面的卡只是同一张表的第二、第三句证据（§5.2 (4)：同一张表不占两个名额）。
    grouped: dict[int, list[RowMapping]] = {}
    for row in rows:
        grouped.setdefault(int(row["table_id"]), []).append(row)
    # 位次在这里就折成名次（工单 032）：`mode='vector'` 那一路同样不走融合，而 `dict` 的迭代序
    # 就是插入序 = 距离升序下这张表第一次出现的行序，与 `rrf_fuse` 那边 `_rank_map` 数的是同一件事。
    return [
        replace(_vector_table(items), vector_rank=index + 1)
        for index, items in enumerate(grouped.values())
    ]


# ============================================================ 接缝


class Retriever(Protocol):
    """`docs/architecture.md` §2.1 那句"关键接缝"的落点：pipeline 只依赖这个协议。

    签名里刻意不出现任何向量概念（维度、距离算子、ef_search 都不在），P4 的
    `HybridRetriever` 实现同一个协议，调用方一行不改。
    """

    async def search(
        self,
        session: AsyncSession,
        *,
        actor: User,
        question: str,
        datasource_ids: Sequence[int] = (),
        k: int | None = None,
    ) -> list[RetrievedTable]: ...


class LikeRetriever:
    """L1 的最小实现：元数据库上的 ILIKE + pg_trgm，按 §5.1 的（命中词数, 命中列数, uid）排序。

    P4 之后它同时是 `HybridRetriever` 的 `mode='keyword'` 那一路（降级路径直接复用这一份，
    不照着再写一遍），所以 009 那一整文件的用例是两条路共用的回归位。
    """

    async def search(
        self,
        session: AsyncSession,
        *,
        actor: User,
        question: str,
        datasource_ids: Sequence[int] = (),
        k: int | None = None,
    ) -> list[RetrievedTable]:
        terms = query_terms(question)
        if not terms:
            # 一个词都没切出来（全是标点或单字）：不发 SQL，也不猜表。这一格在 `_search_scope`
            # **之前**，所以"空问句不查库"（工单 009 那条用例的名字）不是碰巧，是这里的顺序。
            return []

        scope = await _search_scope(session, actor=actor, datasource_ids=datasource_ids)
        if scope is None:
            return []
        profile, scoped = scope

        ranked = await _keyword_ranking(
            session, terms=terms, profile_id=profile.index_profile_id, scoped=scoped
        )
        return ranked[: _final_limit(k)]


class HybridRetriever:
    """§5.2 的两条腿：关键词路 + 向量路，各自出名次，RRF 折成一次融合次序（ADR-0014）。

    三个旋钮在**构造时**给定（`SearchTuning`），不进 `search()` 的签名：协议里一旦出现
    `ef_search` 或维度，`pipeline` 就认识向量了，故事 21 那份协议纪律当场作废。裁决（请求体 /
    配置 / 代码缺省）住在 `_retriever_for()` 那一处，这里只收裁决完的结果。

    三条 `mode` 的形状：
    - `keyword`：**直接调用** `LikeRetriever`，不是照着再写一遍。故事 26 那句"降级路径与 P2
      逐字同构"因此由构造保证，009 那一整文件（pg + live）同时是这两条路的回归位；而这一支
      一个 embedding 请求都不发，端点坏没坏都不影响降级。
    - `vector`：只有向量路，返回的表没有命中理由（见 `_vector_table`）。
    - `hybrid`：两条腿各跑一次，融合后再收口到 `k`。
    """

    def __init__(self, *, tuning: SearchTuning, rrf_k: int) -> None:
        self._tuning = tuning
        self._rrf_k = rrf_k

    async def search(
        self,
        session: AsyncSession,
        *,
        actor: User,
        question: str,
        datasource_ids: Sequence[int] = (),
        k: int | None = None,
    ) -> list[RetrievedTable]:
        if self._tuning.mode == "keyword":
            return await LikeRetriever().search(
                session,
                actor=actor,
                question=question,
                datasource_ids=datasource_ids,
                k=k,
            )

        scope = await _search_scope(session, actor=actor, datasource_ids=datasource_ids)
        if scope is None:
            return []
        profile, scoped = scope

        vector = await _vector_ranking(
            session,
            question=question,
            profile=profile,
            scoped=scoped,
            tuning=self._tuning,
        )
        if self._tuning.mode == "vector":
            return vector[: _final_limit(k)]

        terms = query_terms(question)
        # 切不出词时关键词那条腿直接空（不发 SQL），融合因此退化成向量路的次序，而不是整条
        # 链返回空——这是 hybrid 与 keyword 唯一的形状差别，也是 `mode='vector'` 那一条的用意：
        # 问句全是标点时 ILIKE 无路可走，端点却仍然可能给出有意义的邻近卡。
        keyword = (
            await _keyword_ranking(
                session, terms=terms, profile_id=profile.index_profile_id, scoped=scoped
            )
            if terms
            else []
        )
        return fuse_rankings(keyword, vector, rrf_k=self._rrf_k)[: _final_limit(k)]


# ============================================================ 预览端点用的那一层


# 端点只认协议，不认实现。工单 031 验收 5 那句"换实现只改一行"，P4 之后指的就是下面这一处
# 函数体里的构造：`search_preview()` 与 `pipeline` 都不认识 `HybridRetriever` 这个名字
# （`tests/unit/test_retriever_seam.py` 把这两句话钉成了机械检查）。
def _retriever_for(payload: KbSearchRequest) -> Retriever:
    """请求体的三个旋钮 → 一个检索实现。全仓唯一一处 `HybridRetriever(...)` 的构造点。

    它必须是**工厂而不是一个模块级实例**：P4 之后同一份实现要按请求换 `ef_search`，
    模块级那个单例装不下"这次请求要什么旋钮"。换实现仍只改一处——改的是这里那一行构造，
    而不是散到端点与 pipeline 里各 new 一个。

    `vector_ready` 只在这句裁决里出现一次：配齐向量端点四格才默认 hybrid（§5.1 那句
    "未配置时链路不断"，与 `/api/healthz` 的 `retrieval_mode` 同一个判据）。
    """
    cfg = get_settings()
    return HybridRetriever(
        tuning=resolve_tuning(
            cfg=cfg.retrieval,
            vector_ready=cfg.embedding.configured,
            mode=payload.mode,
            top_vector=payload.top_vector,
            ef_search=payload.ef_search,
        ),
        rrf_k=cfg.retrieval.rrf_k,
    )


def _item_out(r: RetrievedTable) -> KbSearchItemOut:
    return KbSearchItemOut(
        table_uid=r.table_uid,
        title=r.title,
        score_kw=round(r.score_kw, 4),
        matched_term_count=r.matched_term_count,
        matched_column_count=r.matched_column_count,
        hits=[
            KbSearchHitOut(column_name=h.column_name, field=h.field, term=h.term) for h in r.hits
        ],
        cards=[
            KbSearchCardOut(
                card_id=c.card_id,
                kind=c.kind,
                seq=c.seq,
                score=round(c.score, 4),
                text_preview=c.text_preview,
            )
            for c in r.cards
        ],
        # 名次与融合分**不在这里 round**：上面那两处 `round(…, 4)` 管的是分数（展示用，量纲是
        # 相似度），而这三格是次序（工单 032 已定口径"名次是名次不是相似度"）。RRF 的头两名常常
        # 只差 1e-5 量级（`test_retriever_rrf.py` 里 t_a 与 t_b 就是），抹到四位小数并列，
        # 界面拿到的次序就和融合层算出来的不是同一个。
        keyword_rank=r.keyword_rank,
        vector_rank=r.vector_rank,
        rrf_score=r.rrf_score,
    )


async def search_preview(
    session: AsyncSession, *, actor: User, payload: KbSearchRequest
) -> KbSearchOut:
    """`POST /kb/search`：给一句问句，把候选表和它们的命中理由原样端出去。

    `datasource_ids` 里点名了一个看不见的源 → 403（`get_authorized` 那一把尺），不是安静少一张：
    预览端点的全部用途就是让人判断"为什么没命中"，把越权请求降级成空结果会把这个诊断废掉。
    真检索路径（012 的 `/chat/ask`）不走这里，那边按 §5.2 (5) 静默过滤。

    `used_profile` 这一格是**端点侧的第二次读**（检索器在 `_search_scope` 里为同一句问句读过一次）。
    它报的是"组装响应这一刻库里 active 那一行叫什么"，**不是**"答这一句用的是哪一行"——请求会话
    没有设隔离级别，PG 默认 READ COMMITTED 下每条语句一个新快照，一次 `activate` 落在 `search()`
    与这次读之间时两处就不是同一行（并发 activate 归 P6 的档案面）。要报后者得让 `RetrievedTable`
    带上档案 id（协议签名不用动，它已经带名次了），那是 P8 的事，这一格先把"有没有生效档案"
    端给界面。`pipeline` 不经过这里，`retrieved` 帧里也没有档案身份。
    """
    for ds_id in payload.datasource_ids:
        await datasource_service.get_authorized(session, actor, ds_id)

    started = time.perf_counter()
    found = await _retriever_for(payload).search(
        session,
        actor=actor,
        question=payload.query,
        datasource_ids=payload.datasource_ids,
        k=payload.k,
    )
    profile = await read_active_profile(session)
    # `hops_used` 与 `aggregated_tables` 两格**只填不造**（spec 故事 30）：原料分别是 014 的
    # `expansion_for` 与 009 已经交付的"聚合到表"那一步，本片不重做图、不重做聚合。
    # 尺与 pipeline 那一次调用是同一个：同一个函数、同一对配置键（`join_hops` /
    # `max_join_degree`），数的是桥表张数而不是边数——两端各自实现一遍才有"预览说连得上、真跑说
    # 连不上"。**候选集合那一头两处不同**，写清楚：pipeline 在喂图前把跨源候选收窄到第一名那个源
    # （`pipeline.py` 的 `same_source`，因为一条连接串连不了两个库），预览没有这条规矩（它不拒答
    # 跨源，那是 P8 的问句面），于是跨源候选时这里报的是各源内部桥表数之和，而真链路只报主源那份。
    # 候选同源的问句下两者逐字相等——演示库与闸内用例都在这一档。读第二趟图不心疼：图按
    # `(源, 库)` 分键缓存在模块级，而且预览与 `/chat/ask` 不在同一条链路上（那条走 pipeline）。
    cfg = get_settings().retrieval
    uids = [r.table_uid for r in found]
    expansion = await join_graph.expansion_for(
        session, table_uids=uids, hops=cfg.join_hops, max_degree=cfg.max_join_degree
    )
    return KbSearchOut(
        items=[_item_out(r) for r in found],
        took_ms=round((time.perf_counter() - started) * 1000),
        aggregated_tables=uids,
        hops_used=len(expansion.bridge_uids),
        used_profile=(
            None
            if profile is None
            else KbSearchProfileOut(
                id=profile.index_profile_id, name=profile.name, dimensions=profile.dimensions
            )
        ),
    )


async def load_schema_tables(
    session: AsyncSession, *, actor: User, table_uids: Sequence[str]
) -> list[SchemaTable]:
    """把检索给的 `table_uid` 补成 prompt 素材：当前生效 profile 的卡片**全文**。

    这一步住在检索侧而不是 prompt 侧，是因为它补的正是检索输出的那处"够自己用但不够 prompt 用"：
    `CardMatch.text_preview` 只有 200 字（召回只要判别力）。`prompt_builder` 因此保持纯函数——
    它收的永远是齐料，不碰库。

    关联边不在这里查了（010 时查过）：【可 JOIN】的唯一来源是 `join_graph`，
    它自己读 `meta_relation` 并把表全名带在边上。一张表一条边查询留在这里，
    就会有两份"这些表之间有哪些边"的事实，而它们对 `is_stale` 与门槛的口径并不一样。

    逐表查而不是批量：传入顺序就是检索的相关度顺序，而【候选表】段的顺序是要进 prompt 的语义
    （§4.1 ④），批量后重排更容易出错。
    """
    profile_id = await _active_profile_id(session)
    if profile_id is None:
        return []

    out: list[SchemaTable] = []
    for uid in table_uids:
        table = (
            await session.execute(
                select(
                    _META_TABLE.c.id,
                    _META_TABLE.c.datasource_id,
                    _META_TABLE.c.catalog_name,
                    _META_TABLE.c.schema_name,
                    _META_TABLE.c.table_name,
                ).where(_META_TABLE.c.table_uid == uid)
            )
        ).first()
        if table is None:
            # 检索刚把这条 uid 端出来、这里就查不到，只可能是并发删除（同步的 prune 那一刀）。
            # 跳过而不是 404：一次问数不该因为一张表在两步之间消失就整条链路失败。
            continue
        table_id = int(table[0])
        await datasource_service.get_authorized(session, actor, int(table[1]))
        # catalog 在 MySQL 恒为空串，不能打头——拼法本身不住这里，主人是
        # `app/core/table_names.table_full_name`（工单 039：validate 与 ask 必须共用一份，
        # 两份拼法会给出两个白名单，同一件 SQL 在两条路上一个放行一个拒）。
        full_name = table_full_name(table[2], table[3], table[4])

        segments = tuple(
            str(row)
            for row in (
                await session.scalars(
                    select(_CARD.c.text_md)
                    .where(_CARD.c.table_id == table_id, _CARD.c.index_profile_id == profile_id)
                    # 段号而不是 id：identity 按插入顺序长，换 profile 后 id 顺序会变，
                    # 而"主卡在前"是 §6 的语义。
                    .order_by(_CARD.c.seq)
                )
            ).all()
        )
        if not segments:
            continue

        out.append(SchemaTable(uid=uid, full_name=full_name, segments=segments))
    return out
