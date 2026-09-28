"""卡片落库语句的**形状**：冲突键是谁、谁被覆盖、谁必须被清空。

接缝选在编译出的 SQL 文本上（与 `test_sync_upsert_sql.py` 同一理由）：这一片写错的后果
都不是"当场报错"，而是"库里多出一份没人知道的重复卡片"或"旧向量继续冒充新卡片的向量"，
真跑用例照样绿。期望值口径：metadata-model §2.6 的 `doc_uid char(32) UNIQUE`
与 kb-workflow §5/§7 的重建语义。
"""

from __future__ import annotations

import re

from sqlalchemy.dialects import postgresql

from app.services import kb_service


def _sql(stmt: object) -> str:
    rendered = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    return re.sub(r"\s+", " ", rendered).strip()


def _set_clause(stmt: object) -> dict[str, str]:
    """把 `DO UPDATE SET` 后面拆成 {列名: 表达式}——按括号深度切，不按逗号切（COALESCE 带逗号）。"""
    tail = _sql(stmt).split("DO UPDATE SET ", 1)[1]
    parts: list[str] = []
    depth, current = 0, []
    for ch in tail:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    out: dict[str, str] = {}
    for part in parts:
        name, _, expr = part.partition("=")
        out[name.strip().strip('"').lower()] = " ".join(expr.split())
    return out


def test_卡片按doc_uid冲突而不是按表() -> None:
    """§2.6：`doc_uid char(32) UNIQUE` 是卡片唯一的身份键。

    冲突键若写成 `(table_id, seq)`，换 embedding profile 全量重建时新卡会**覆盖**旧卡，
    于是旧 profile 的向量被就地换成新的——§2.6 末"可回滚"当场失效。
    """
    low = _sql(kb_service.kb_card_upsert()).lower()
    assert "on conflict (doc_uid) do update set" in low


def test_正文列每轮吃新值() -> None:
    """卡片文本是同步产物：模板/注释/列一变，库里那行必须跟着变（§5 的构建语义）。

    `title` 也在这组里：它由 `full_name + 表注释` 算出来，源库补了注释而 title 不吃新值的话，
    界面上就永远是"旧标题配新正文"。
    """
    set_ = _set_clause(kb_service.kb_card_upsert())
    for name in ("datasource_id", "kind", "table_id", "seq", "title", "text_md", "search_text"):
        assert set_[name] == f"excluded.{name}", name
    # jsonb 的 `=` 直接整值替换，不需要像人工列那样 COALESCE——卡片没有人写字段（P2）
    assert set_["meta"] == "excluded.meta"
    assert set_["token_count"] == "excluded.token_count"
    assert set_["updated_at"] == "now()"


def test_重建把向量清成NULL等下一次灌() -> None:
    """§5：卡片内容变了要触发该表 embedding 重算；§7 的重建是同一条语义。

    写成"不动这两列"的话，旧向量会顶着新文本一直用到下次换 profile，
    而检索结果里看不出它已经过期——这是"AI 答非所问"最难查的一种成因。
    置 NULL 之后，向量路和关键词路的降级都由 `embedding IS NULL` 一个条件描述（ADR-0003）。
    """
    set_ = _set_clause(kb_service.kb_card_upsert())
    # 必须是语句文本里的 `NULL`（literal_column），不是绑定参数——写成绑参的话语句形状
    # 看着一样，但 PG 收到的类型是 untyped parameter，向量的清理照样成立，
    # 而"这里被刻意清空"这件事从 SQL 里读不出来了。
    assert set_["embedding"] == "NULL"
    assert set_["embedded_at"] == "NULL"


def test_profile指针与身份键不进set_() -> None:
    """一条卡片的 `doc_uid` 里已经拌了 `index_profile_id`，两者不该被"改"。

    把 profile 指针放进 `DO UPDATE SET` 等于允许一条语句把卡片从旧 profile 搬到新 profile，
    而 §2.6 要的是"两套 profile 各自一批行"。`doc_uid` 同理：它是冲突键，改它没有意义，
    只是如果哪天写错了 set_，PG 会照改不误。
    """
    set_ = _set_clause(kb_service.kb_card_upsert())
    assert "index_profile_id" not in set_
    assert "doc_uid" not in set_
    assert "created_at" not in set_
