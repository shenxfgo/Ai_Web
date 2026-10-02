"""混合检索的两块纯函数：RRF 融合与请求旋钮裁决（工单 031 验收 1/2，spec 故事 24/25/27）。

oracle 出处两条，都不是从实现反推的：
- 融合公式住在 `docs/architecture.md` §5.2 (3)：`score = Σ 1/(rrf_k + rank)`，**吃名次不吃分数**
  （ADR-0014）。期望分数写成"两个手算的倒数之和"这种字面，不写成小数——小数要四舍五入，
  而 `1/61 + 1/64` 对着 §5.2 就能核。
- 缺省那三格住在 `docs/architecture.md` §7 与 §5.1：`ef_search` 取
  `AIWEB_RETRIEVAL__HNSW_EF_SEARCH`、`top_vector` 取向量路自己的召回窗口常量
  （与关键词路的 `_RECALL_LIMIT` **不共用**）、`rrf_k` 只在 `.env`（60）不进请求体。

验收 2 那两条是一对，都留着：第一条是工单字面（`score_kw` 乘 2 / 加 1000 名次不变），它钉的是
"融合层的输入里没有分数"；但它对"按 `score_kw` 排名次"这种错法没有牙——任何跨表均匀的单调变换
都保序。第二条把分数改成**跨表不平**（宽表打到一千倍）而计数键一字不动，名次照旧才是真证据
（故事 25 括号里那句"否则 009 那次宽表打穿 `order_main` 会当场复现"要的就是这一格）。
"""

from __future__ import annotations

from app.services.nl2sql.retriever import (
    CardMatch,
    ColumnHit,
    FusedTable,
    RetrievedTable,
    fuse_rankings,
    rank_candidates,
    resolve_tuning,
    rrf_fuse,
)
from app.settings import RetrievalGroup

# 两路的输入就是"名次表"：一串按各路次序排好的 table_uid。
_KEYWORD = ("t_a", "t_b", "t_c")
_VECTOR = ("t_d", "t_e", "t_b", "t_a")


def _shape(fused: list[FusedTable]) -> tuple[tuple[str, int | None, int | None], ...]:
    """`(uid, 关键词名次, 向量名次)`——名次与次序一起看，光看次序分不出名次算错在哪。"""
    return tuple((item.table_uid, item.keyword_rank, item.vector_rank) for item in fused)


def _positions(fused: list[FusedTable]) -> dict[str, int]:
    """`uid → 融合后的位次`（0 起）。

    验"次序翻没翻"用位次而不是切片：表头那一名可能被另一路的头名并列占着
    （名次相等时按 uid 收口），比谁在前比"前两名是谁"说的是同一件事但少扯上无关的表。
    """
    return {item.table_uid: index for index, item in enumerate(fused)}


def test_三组手算名次折成融合次序与分数() -> None:
    """k=60 的一处完整对照：五张表、三种命中形状（两路都有 / 只在关键词 / 只在向量）。"""
    fused = rrf_fuse(_KEYWORD, _VECTOR, rrf_k=60)

    # 名次取自喂进去的两串：t_a 是关键词第 1、向量第 4；t_b 是关键词第 2、向量第 3。
    assert _shape(fused) == (
        ("t_a", 1, 4),
        ("t_b", 2, 3),
        ("t_d", None, 1),
        ("t_e", None, 2),
        ("t_c", 3, None),
    )

    scores = {item.table_uid: item.rrf_score for item in fused}
    assert scores["t_a"] == 1 / 61 + 1 / 64
    assert scores["t_b"] == 1 / 62 + 1 / 63
    assert scores["t_d"] == 1 / 61
    assert scores["t_e"] == 1 / 62
    assert scores["t_c"] == 1 / 63
    # t_a 与 t_b 只差在 1.6e-5 量级：这两张表的先后是 `rrf_k` 说的，不是随便排的。
    assert scores["t_a"] > scores["t_b"]


def test_rrf_k_从_60_换到_10_时融合次序真的变() -> None:
    """验收 1 的后半：同一份名次，只有 `rrf_k` 变，**次序**翻过来——那个数必须真被读到。

    挑的是 `rrf_k` 这句话最清楚的形状：`t_solo` 只在关键词路排第 1，`t_both` 两路都排在尾巴上。
    k 大 → 名次之间的分母差得少 → "两路都露过面"赢；k 小 → 靠前值很多 → 单路的头名赢。
    写死常数（或把 `rrf_k` 读成 settings 里别的键）的实现会在第二个断言红。
    """
    keyword = ("t_solo", *(f"t_{n:02d}" for n in range(1, 40)), "t_both")
    vector = (*(f"v_{n:02d}" for n in range(1, 41)), "t_both")

    at_60 = _positions(rrf_fuse(keyword, vector, rrf_k=60))
    at_10 = _positions(rrf_fuse(keyword, vector, rrf_k=10))
    # t_solo 是关键词第 1 名，t_both 两路都是第 41 名——k=60 时"两路都露过面"值钱。
    assert at_60["t_both"] < at_60["t_solo"]
    # k=10 时反过来：单路的头名值钱。写死常数（或把 `rrf_k` 读成 settings 里别的键）
    # 的实现两次算出同一个次序，这一条当场红。
    assert at_10["t_solo"] < at_10["t_both"]
    # 手算核一遍这档次序确实反号：k=60 时 2/101 > 1/61，k=10 时 1/11 > 2/51。
    assert 2 * (1 / 101) > 1 / 61
    assert 1 / 11 > 2 * (1 / 51)


def test_并列名次按_table_uid_升序收口_同一个库两次跑要同一个次序() -> None:
    """两路互换了名次（各自都是 {1,2}）→ 分数严格相等，次序只能由 uid 定（§5.1 的稳定排序）。"""
    fused = rrf_fuse(("t_b", "t_a"), ("t_a", "t_b"), rrf_k=60)
    assert [item.table_uid for item in fused] == ["t_a", "t_b"]
    assert fused[0].rrf_score == fused[1].rrf_score == 1 / 61 + 1 / 62


def test_两路都空时融合也是空的而不是猜一张() -> None:
    assert rrf_fuse((), (), rrf_k=60) == []


def _card(card_id: int, table_id: int, uid: str, *, score: float) -> CardMatch:
    return CardMatch(
        card_id=card_id,
        table_id=table_id,
        table_uid=f"ai_web_demo.{uid}",
        datasource_id=1,
        kind="table",
        seq=0,
        title=f"ai_web_demo.{uid}",
        score=score,
        text_preview="",
    )


def _hit(table_id: int, column: str, term: str) -> ColumnHit:
    return ColumnHit(table_id=table_id, column_name=column, field="comment_zh", term=term)


# 计数键（命中词数, 命中列数）由 hits 定，与 score 无关：narrow (2,3) > wide (1,10) > third (1,1)。
# narrow 两个词、wide 一个词堆十列——就是 009 那次"宽表打穿 order_main"的形状（§5.1 as-built 注）。


def _keyword_uids(*, narrow: float, wide: float, third: float) -> tuple[str, ...]:
    cards = [
        _card(1, 10, "t_narrow", score=narrow),
        _card(2, 20, "t_wide", score=wide),
        _card(3, 30, "t_third", score=third),
    ]
    hits = [_hit(10, c, t) for c, t in (("id", "订单"), ("amount", "订单"), ("status", "金额"))]
    hits += [_hit(20, f"refund_amt_{n}d", "金额") for n in range(10)]
    hits += [_hit(30, "note", "订单")]
    ranked = rank_candidates(cards, hits, k=len(cards))
    return tuple(r.table_uid for r in ranked)


_VECTOR_SIDE = ("ai_web_demo.t_wide", "ai_web_demo.t_third")
_NARROW = "ai_web_demo.t_narrow"
_WIDE = "ai_web_demo.t_wide"
_THIRD = "ai_web_demo.t_third"


def test_分数跨表均匀放大或平移_融合名次一字不动() -> None:
    """工单 031 验收 2 的字面：`score_kw` 乘 2、加 1000，融合名次不变。

    融合的输入只有两串 uid，所以这一条证的是"分数进不了融合层"（下一条才管名次怎么来的）。
    """
    base = _shape(rrf_fuse(_keyword_uids(narrow=1.0, wide=0.9, third=0.5), _VECTOR_SIDE, rrf_k=60))
    doubled = _shape(
        rrf_fuse(_keyword_uids(narrow=2.0, wide=1.8, third=1.0), _VECTOR_SIDE, rrf_k=60)
    )
    shifted = _shape(
        rrf_fuse(_keyword_uids(narrow=1001.0, wide=1000.9, third=1000.5), _VECTOR_SIDE, rrf_k=60)
    )
    assert doubled == base
    assert shifted == base


def test_把宽表的分数打到窄表的一千倍_名次仍按计数键() -> None:
    """验收 2 那句"反过来说"的正面对齐：这条才是有牙的那一条。

    跨表不平的分数会把"按 `score_kw` 排名次"的实现整个倒过来（wide 1000 > narrow 1.0），
    而计数键一字未动 → 名次也不能动。把它改成按分数排，本用例当场红。
    """
    keyed = _shape(
        rrf_fuse(_keyword_uids(narrow=1.0, wide=1000.0, third=0.5), _VECTOR_SIDE, rrf_k=60)
    )
    ranks = {uid: kw for uid, kw, _ in keyed}
    assert ranks == {_NARROW: 1, _WIDE: 2, _THIRD: 3}
    # 不是空转：分数那一侧真的倒了（wide 那张卡现在是 narrow 的一千倍）。
    inverted = _shape(rrf_fuse((_WIDE, _NARROW, _THIRD), _VECTOR_SIDE, rrf_k=60))
    assert {uid: kw for uid, kw, _ in inverted} == {_WIDE: 1, _NARROW: 2, _THIRD: 3}
    assert keyed != inverted


_ORDER = "ai_web_demo.order_main"
_PAYMENT = "ai_web_demo.payment_record"
_CUSTOMER = "ai_web_demo.customer"


def _kw_table(uid: str, table_id: int) -> RetrievedTable:
    """关键词路出来的一张表：带着命中理由和卡片预览，融合之后这两样不许丢。"""
    return RetrievedTable(
        table_uid=uid,
        table_id=table_id,
        datasource_id=1,
        title=uid,
        score_kw=1.5,
        matched_term_count=2,
        matched_column_count=1,
        hits=(_hit(table_id, "amount", "金额"),),
        cards=(_card(table_id, table_id, uid.rsplit(".", 1)[1], score=1.5),),
    )


def _vec_table(uid: str, table_id: int) -> RetrievedTable:
    """向量路出来的一张表：只有身份字段。

    `score_kw=0.0` 与空的 `hits`/`cards` 是本片补的口径（写在 `_vector_ranking` 的文档串里）：
    向量路给不出"哪一列被哪个词对上"这句话，硬凑一条理由等于替端点编造它没说过的话。
    """
    return RetrievedTable(
        table_uid=uid,
        table_id=table_id,
        datasource_id=1,
        title=uid,
        score_kw=0.0,
        matched_term_count=0,
        matched_column_count=0,
        hits=(),
        cards=(),
    )


def test_融合后两路都命中的表带的是关键词路那份形状() -> None:
    """`fuse_rankings` 的装配规则：名次来自融合、表对象来自各自的腿（`hits`/`cards` 不丢）。"""
    keyword = [_kw_table(_ORDER, 10), _kw_table(_PAYMENT, 20)]
    vector = [_vec_table(_CUSTOMER, 30), _vec_table(_PAYMENT, 20)]

    fused = fuse_rankings(keyword, vector, rrf_k=60)

    # 手算：order_main 只在关键词第 1 → 1/61；payment 两路都第 2 → 1/62+1/62；
    # customer 只在向量第 1 → 1/61。payment 最高；剩下两张严格并列，按 uid 升序收口
    # （`customer` < `order_main`），这条同时也是 §5.1 那条稳定排序在装配层的落点。
    assert [item.table_uid for item in fused] == [_PAYMENT, _CUSTOMER, _ORDER]
    # 两路都命中的那张出门的是**关键词路那个对象**（不是字段相等的新对象）：理由与卡片还在。
    assert fused[0] is keyword[1]
    # 只在向量路的那张：形状就是向量路给的那份，没有硬凑的理由。
    assert fused[1] is vector[0]
    assert (fused[1].score_kw, fused[1].matched_term_count, fused[1].hits, fused[1].cards) == (
        0.0,
        0,
        (),
        (),
    )


def test_一条腿为空时融合次序就是另一条自己照抄() -> None:
    """向量那一路上还没有一张卡（或 `mode` 被裁掉一条腿）时不许把另一条腿重排或丢表。"""
    keyword = [_kw_table(_ORDER, 10), _kw_table(_PAYMENT, 20)]
    vector = [_vec_table(_ORDER, 10), _vec_table(_PAYMENT, 20)]

    assert fuse_rankings(keyword, (), rrf_k=60) == keyword
    assert fuse_rankings((), vector, rrf_k=60) == vector
    assert fuse_rankings((), (), rrf_k=60) == []


def test_裁决_请求给了用请求的_没给用配置() -> None:
    """故事 27：这层裁决只住一个地方，且三格各自独立（给一格就只改那一格）。"""
    cfg = RetrievalGroup(hnsw_ef_search=64)
    resolved = resolve_tuning(cfg=cfg, vector_ready=True, ef_search=500)
    assert resolved.ef_search == 500
    # `top_vector` 与 `mode` 都没给：一个取向量路自己的窗口，一个取"配了端点就 hybrid"。
    assert resolved.mode == "hybrid"

    both = resolve_tuning(cfg=cfg, vector_ready=True, mode="vector", top_vector=13, ef_search=500)
    assert (both.mode, both.top_vector, both.ef_search) == ("vector", 13, 500)


def test_裁决_缺省那三格各自来自配置与向量路自己的窗口() -> None:
    cfg = RetrievalGroup(hnsw_ef_search=250)
    resolved = resolve_tuning(cfg=cfg, vector_ready=True)
    assert resolved.ef_search == 250, "配置那一档换值时缺省必须跟着变，否则键是装饰品"
    # 向量路的窗口与关键词路的 `_RECALL_LIMIT` 是两个独立的数（故事 22）；此处验它被读到。
    assert resolved.top_vector == 80


def test_裁决_没配向量端点时缺省模式退成_keyword() -> None:
    """§5.1 那条"未配置向量端点时链路不断"的落点：降级只发生在**没人指定**的时候。"""
    cfg = RetrievalGroup()
    assert resolve_tuning(cfg=cfg, vector_ready=False).mode == "keyword"
    # 有人明确要向量而端点没配：裁决不改口，失败留给那一次真调用去说（悄悄降级就是撒谎）。
    assert resolve_tuning(cfg=cfg, vector_ready=False, mode="vector").mode == "vector"
