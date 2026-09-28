"""L1 的第二块纯函数：把"卡片命中行 + 列命中行"收成候选表。

oracle 来自 `docs/architecture.md` §5.1 L1（排序主键 as-built(0009) 已从"命中列数"改成
"**命中的不同词数**"，命中列数降为次键——理由与实测数据写在 §5.1 那条 as-built 注里）
与 §5.2 (4)（同表多卡时 `boost = 0.05×(n-1)`，切片的 table_columns 卡不单独占位）。
期望值全部手算：命中词数、命中列数、卡数、boost 四个数都是喂进去的行数的直接函数，不含算法。
"""

from __future__ import annotations

from app.services.nl2sql.retriever import CardMatch, ColumnHit, rank_candidates


def _card(card_id: int, table_id: int, uid: str, *, seq: int = 0, score: float = 1.0) -> CardMatch:
    return CardMatch(
        card_id=card_id,
        table_id=table_id,
        table_uid=f"ai_web_demo.{uid}",
        datasource_id=1,
        kind="table" if seq == 0 else "table_columns",
        seq=seq,
        title=f"ai_web_demo.{uid}",
        score=score,
        text_preview="",
    )


def _hit(table_id: int, column: str, term: str, field: str = "comment_zh") -> ColumnHit:
    return ColumnHit(table_id=table_id, column_name=column, field=field, term=term)


def _feed() -> tuple[list[CardMatch], list[ColumnHit]]:
    cards = [
        # order_main：主卡 + 一张切片卡都命中（宽表两张卡 → boost 0.05）
        _card(1, 10, "order_main", score=1.0),
        _card(2, 10, "order_main", seq=1, score=1.0),
        _card(3, 20, "payment_record", score=1.0),
        _card(4, 30, "customer", score=0.4),
    ]
    hits = [
        _hit(10, "id", "订单"),
        _hit(10, "order_no", "订单"),
        _hit(10, "amount", "订单"),
        _hit(10, "amount", "金额"),  # 同一列被两个词命中，只算一列
        _hit(20, "order_id", "订单"),
        _hit(20, "amount", "金额"),
    ]
    return cards, hits


def test_按命中词数降序排且同表多卡叠加boost() -> None:
    cards, hits = _feed()
    ranked = rank_candidates(cards, hits, k=5)
    assert [r.table_uid for r in ranked] == [
        "ai_web_demo.order_main",
        "ai_web_demo.payment_record",
        "ai_web_demo.customer",
    ]
    # 主键是命中词数，次键才是命中列数：order_main 与 payment_record 都命中 {订单, 金额} 两个词，
    # 靠"命中列数 3 > 2"分出先后。
    assert [r.matched_term_count for r in ranked] == [2, 2, 0]
    assert [r.matched_column_count for r in ranked] == [3, 2, 0]
    # score_kw = 该表最好那张卡的分数 + 0.05×(命中卡数-1)（§5.2 (4)）
    assert ranked[0].score_kw == 1.05
    assert ranked[1].score_kw == 1.0
    # 只有一张卡的表不吃 boost；没有任何列命中的表仍留在候选里（它命中的是表注释）
    assert ranked[2].score_kw == 0.4


def test_宽表靠单个词堆出的命中数盖不过窄表的两个词() -> None:
    """§5.1 那条 as-built 注的回归位：`product_stats_wide` 的 10 列全被 `金额` 一个词命中。

    旧口径（主键=命中列数）会让它排在只命中 5 列的 `order_main` 前面，验收 1 当场破；
    新口径下"两个不同词"比"十列同一个词"更值得信任，所以 order_main 回到第一。
    """
    cards = [_card(1, 10, "order_main"), _card(2, 40, "product_stats_wide")]
    hits = [_hit(10, c, "订单") for c in ("id", "order_no", "customer_id", "status", "amount")]
    hits += [_hit(10, "amount", "金额")]
    hits += [_hit(40, f"refund_amt_{n}d", "金额") for n in (1, 7, 14, 30, 90)]
    hits += [_hit(40, f"return_rate_{n}d", "金额") for n in (1, 7, 14, 30, 90)]
    ranked = rank_candidates(cards, hits, k=5)
    assert [r.table_uid for r in ranked] == [
        "ai_web_demo.order_main",
        "ai_web_demo.product_stats_wide",
    ]
    assert [r.matched_term_count for r in ranked] == [2, 1]
    assert [r.matched_column_count for r in ranked] == [5, 10]


def test_命中理由带着列名与命中的词() -> None:
    """验收 2 的"可解释"：UI 要能说出是哪一列的哪个字段被哪个词命中。"""
    cards, hits = _feed()
    order_main = rank_candidates(cards, hits, k=5)[0]
    assert {(h.column_name, h.term) for h in order_main.hits} == {
        ("id", "订单"),
        ("order_no", "订单"),
        ("amount", "订单"),
        ("amount", "金额"),
    }
    # 卡片原文也一并带回来，010 拼 prompt 直接用它，不再回库取
    assert [c.card_id for c in order_main.cards] == [1, 2]


def test_k是名额上限() -> None:
    cards, hits = _feed()
    assert [r.table_uid for r in rank_candidates(cards, hits, k=2)] == [
        "ai_web_demo.order_main",
        "ai_web_demo.payment_record",
    ]


def test_零命中返回空列表而不是猜一张表() -> None:
    """阶梯 L4 的判据（013 要用）：宁缺勿滥，空就是空。"""
    assert rank_candidates([], [], k=5) == []
