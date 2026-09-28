"""阶梯 L1（结构化关键词）的检索器。接缝与配方住在 `docs/architecture.md` §5.1/§5.2。

P2 只实现 `LikeRetriever`：`Retriever` Protocol 里**没有任何向量概念**（没有 dim、没有
`<=>`、没有 ef_search），所以 P4 换 `HybridRetriever` 时 `pipeline` 一行都不用改——
协议上只允许出现"问句 + 候选表 + 分数 + 命中理由"。

分三截读：

1. **切词、命中判定与排序（纯函数，不碰数据库）**：`query_terms` / `column_hits` /
   `rank_candidates`。
2. **召回（只碰元数据库）**：`_recall_statement` 在 `kb_card` 的卡片文本列上做 §5.2 (2)
   的关键词路，`_candidate_columns` 把候选表的列原料读回来喂给第 1 截。
   **绝不连源库**——源库要等 011 的只读执行才碰，而且那时也不走这条路。
3. **接缝**：`Retriever`（Protocol）与 `LikeRetriever`（唯一实现）。
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol, cast

from sqlalchemy import Select, Table, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kb import KbCard, KbIndexProfile
from app.models.meta import MetaColumn, MetaRelation, MetaTable
from app.models.user import User
from app.schemas.kb import (
    KbSearchCardOut,
    KbSearchHitOut,
    KbSearchItemOut,
    KbSearchOut,
    KbSearchRequest,
)
from app.services import datasource_service
from app.services.nl2sql.prompt_builder import RelationEdge, SchemaTable
from app.settings import get_settings

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
    return candidates[:k]


# ============================================================ 召回（只碰元数据库）

# §5.2 (2) 关键词路的召回窗口：先粗召回再聚合到表，最终名额由 `k` 收口。
_RECALL_LIMIT: Final = 80
# (2a) 精确/前缀那一路的权重：文档给的就是 1.0，标识符对上就是最硬的信号。
_EXACT_SCORE: Final = 1.0
# 预览端点给正文开头的截断长度：够人眼判断"命中的是不是这张表"，又不至于把整卡刷进日志。
_PREVIEW_CHARS: Final = 200

# 与 kb_service 同一套写法：列级 SELECT 走 Core 的 Table 对象，别让 ORM 把整行实体拉回来。
_CARD = cast("Table", KbCard.__table__)
_PROFILE = cast("Table", KbIndexProfile.__table__)
_META_TABLE = cast("Table", MetaTable.__table__)
_META_RELATION = cast("Table", MetaRelation.__table__)
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


async def _active_profile_id(session: AsyncSession) -> int | None:
    """当前生效的那套 profile。检索只命中它的卡（§2.6 末：新旧两套混在一个索引里不可解释）。"""
    return await session.scalar(select(KbIndexProfile.id).where(KbIndexProfile.is_active).limit(1))


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
    """L1 的最小实现：元数据库上的 ILIKE + pg_trgm，按 §5.1 的（命中词数, 命中列数, uid）排序。"""

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
            # 一个词都没切出来（全是标点或单字）：不发 SQL，也不猜表。
            return []

        visible = {ds.id for ds, _ in await datasource_service.list_visible(session, actor)}
        scoped = visible & set(datasource_ids) if datasource_ids else visible
        if not scoped:
            return []

        profile_id = await _active_profile_id(session)
        if profile_id is None:
            # 一张卡都没建过（没跑过同步，或同步过但卡片那一段没落）。这里**故意不抬错**：
            # Protocol 的返回类型是 `list[RetrievedTable]`，改成抛异常会连累 010/012 的调用方，
            # 而"没有生效 profile"和"问句没命中"在 P2 都是同一个答案——空候选。
            # 可诊断性归 013：那里的 `NO_SCHEMA_FOUND` 分类要把这两种分开，给不同的下一步
            # （前者是"去跑同步/建卡片"，后者是"换个说法或补录知识"）。
            return []

        rows = (
            (
                await session.execute(
                    _recall_statement(
                        terms,
                        profile_id=profile_id,
                        datasource_ids=sorted(scoped),
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
        limit = k if k is not None else get_settings().retrieval.final_tables_k
        return rank_candidates(cards, hits, k=limit)


# ============================================================ 预览端点用的那一层


# 端点只认协议，不认实现：P4 换 HybridRetriever 改的是这一行的赋值，往上一行都不动。
_RETRIEVER: Retriever = LikeRetriever()


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
    )


async def search_preview(
    session: AsyncSession, *, actor: User, payload: KbSearchRequest
) -> KbSearchOut:
    """`POST /kb/search`：给一句问句，把候选表和它们的命中理由原样端出去。

    `datasource_ids` 里点名了一个看不见的源 → 403（`get_authorized` 那一把尺），不是安静少一张：
    预览端点的全部用途就是让人判断"为什么没命中"，把越权请求降级成空结果会把这个诊断废掉。
    真检索路径（012 的 `/chat/ask`）不走这里，那边按 §5.2 (5) 静默过滤。
    """
    for ds_id in payload.datasource_ids:
        await datasource_service.get_authorized(session, actor, ds_id)

    started = time.perf_counter()
    found = await _RETRIEVER.search(
        session,
        actor=actor,
        question=payload.query,
        datasource_ids=payload.datasource_ids,
        k=payload.k,
    )
    return KbSearchOut(
        items=[_item_out(r) for r in found],
        took_ms=round((time.perf_counter() - started) * 1000),
    )


async def load_schema_tables(
    session: AsyncSession, *, actor: User, table_uids: Sequence[str]
) -> list[SchemaTable]:
    """把检索给的 `table_uid` 补成 prompt 素材：当前生效 profile 的卡片**全文** + 直连关联边。

    这一步住在检索侧而不是 prompt 侧，是因为它补的正是检索输出的两处"够自己用但不够 prompt 用"：
    `CardMatch.text_preview` 只有 200 字（召回只要判别力），而关联边根本不在召回查询里。
    `prompt_builder` 因此保持纯函数——它收的永远是齐料，不碰库。

    逐表两次查询（卡片、边）而不是批量：传入顺序就是检索的相关度顺序，
    而【候选表】段的顺序是要进 prompt 的语义（§4.1 ④），批量后重排更容易出错。
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
        # catalog 在 MySQL 恒为空串，不能打头（kb_service._full_name 同一条规则）。
        full_name = ".".join(part for part in table[2:5] if part)

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

        target = _META_TABLE.alias("meta_table_prompt_target")
        rows = (
            await session.execute(
                select(
                    _META_RELATION.c.from_column_name,
                    _META_RELATION.c.source_kind,
                    _META_RELATION.c.confidence,
                    _META_RELATION.c.to_column_name,
                    target.c.catalog_name.label("to_catalog_name"),
                    target.c.schema_name.label("to_schema_name"),
                    target.c.table_name.label("to_table_name"),
                )
                .join(target, target.c.id == _META_RELATION.c.to_table_id)
                .where(_META_RELATION.c.from_table_id == table_id)
                # 顺序要稳：同一张表两次问数拿到同一份 JOIN 段，golden 才不会被排序抖动打穿。
                .order_by(
                    _META_RELATION.c.source_kind,
                    _META_RELATION.c.from_column_name,
                    target.c.table_name,
                    _META_RELATION.c.to_column_name,
                )
            )
        ).mappings()
        relations = tuple(
            RelationEdge(
                from_column=str(row["from_column_name"]),
                to_table_full=".".join(
                    part
                    for part in (
                        row["to_catalog_name"],
                        row["to_schema_name"],
                        row["to_table_name"],
                    )
                    if part
                ),
                to_column=str(row["to_column_name"]),
                kind=str(row["source_kind"]),
                confidence=None if row["confidence"] is None else float(row["confidence"]),
            )
            for row in rows
        )
        out.append(SchemaTable(full_name=full_name, segments=segments, relations=relations))
    return out
