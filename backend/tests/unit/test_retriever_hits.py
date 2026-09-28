"""L1 的第三块纯函数：在一批候选列上数"哪一列的哪个字段被哪个词命中"。

这块是**验收 2 的可解释性**来源（"命中理由：哪一列、哪个注释匹配的"），所以判定规则要和
SQL 那一路严格一致——`docs/architecture.md` §5.2 (2a) 的"精确/前缀 ILIKE `%term%`"就是
**子串包含**，且 PG 的 `ILIKE` 不分大小写，所以 Python 侧比对必须 casefold。

期望值全部按 §5.1 L1 点名的三个字段（列名、`comment_raw`、`comment_zh`）手算。
"""

from __future__ import annotations

from app.services.nl2sql.retriever import CandidateColumn, column_hits


def _col(
    table_id: int, name: str, raw: str | None = None, zh: str | None = None
) -> CandidateColumn:
    return CandidateColumn(table_id=table_id, column_name=name, comment_raw=raw, comment_zh=zh)


def _feed() -> tuple[list[CandidateColumn], tuple[str, ...]]:
    columns = [
        _col(10, "amount", "订单金额"),
        _col(10, "phone", "手机号", "联系电话"),
        _col(10, "status", "订单状态", "状态"),
    ]
    return columns, ("金额", "手机", "状态")


def test_命中理由精确到列与字段与词() -> None:
    hits = column_hits(*_feed())
    assert [(h.column_name, h.field, h.term) for h in hits] == [
        ("amount", "comment_raw", "金额"),
        # 一列的多个字段各自命中就各记一条：界面要说清"是原始注释对上还是人工注释对上"，
        # 人工修订过的 comment_zh 命中，比源库原话命中更可信。
        ("phone", "comment_raw", "手机"),
        ("status", "comment_raw", "状态"),
        ("status", "comment_zh", "状态"),
    ]


def test_列名比对不分大小写因为ilike本来就不分() -> None:
    columns = [_col(10, "pay_amount"), _col(10, "ID")]
    assert [(h.column_name, h.field, h.term) for h in column_hits(columns, ("Amount", "id"))] == [
        ("pay_amount", "column_name", "Amount"),
        ("ID", "column_name", "id"),
    ]


def test_没有原料的注释列不参与比对也不会炸() -> None:
    # 源库没注释时 comment_raw/comment_zh 都是 None，比对必须跳过而不是 str(None)
    assert column_hits([_col(10, "secret")], ("金额",)) == []
