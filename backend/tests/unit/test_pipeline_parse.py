"""三种畸形 LLM 返回的兜底（architecture §4.1 ⑦ 的 as-built(P2-012) ④，010 拍板归本片）。

契约是"user 消息末尾要求模型只回一个 JSON 对象 `{"sql","explanation","clarify"}`，
不包代码围栏、不附解释文字"（§4.1 ④ as-built ①）。实际会给出四种残形状，分档只有两条线：
**没补全任何东西的算恢复，要猜模型本来想说什么的算不可恢复**。
"""

from __future__ import annotations

import pytest

from app.core.errors import AppError
from app.services.nl2sql.pipeline import parse_llm_draft

_SQL = "SELECT dt, amt FROM ai_web_demo.order_main LIMIT 10"


def test_契约形态_干净_JSON_直接取三段() -> None:
    draft = parse_llm_draft(f'{{"sql": "{_SQL}", "explanation": "按月汇总", "clarify": ""}}')
    assert draft.sql == _SQL
    assert draft.explanation == "按月汇总"
    assert draft.clarify is None


def test_围栏包住_JSON_剥掉外壳就行() -> None:
    body = f'{{"sql": "{_SQL}", "explanation": "", "clarify": ""}}'
    assert parse_llm_draft(f"```json\n{body}\n```").sql == _SQL


def test_JSON_后面粘了解释文字_取到大括号为止() -> None:
    raw = f'{{"sql": "{_SQL}", "explanation": "", "clarify": ""}}\n\n以上就是你要的查询。'
    assert parse_llm_draft(raw).sql == _SQL


def test_JSON_前面粘了客套话_同样取到那一段() -> None:
    raw = f'好的，以下是 SQL：\n{{"sql": "{_SQL}", "explanation": "", "clarify": ""}}'
    assert parse_llm_draft(raw).sql == _SQL


def test_围栏里是裸_SQL_也算恢复_因为一个字都没补() -> None:
    # 模型没按契约回 JSON，但它给的东西是完整的——守卫在前，解析器不授权。
    assert parse_llm_draft(f"```sql\n{_SQL}\n```").sql == _SQL


def test_被_max_tokens_截断_JSON_不猜_直接坏响应() -> None:
    # 大括号不闭合就意味着后半截没了。补全出来的 SQL 没人能证明模型本来想说什么，
    # 而这条链路的失败成本是执行一条没人授权的语句（同一条理由见 safety §8 的白名单口径）。
    with pytest.raises(AppError) as caught:
        parse_llm_draft('{"sql": "SELECT dt, amt FROM ai_web_demo.order_main LI')
    assert caught.value.code == "llm_bad_response"


def test_纯散文里根本没有_JSON_也算坏响应() -> None:
    with pytest.raises(AppError) as caught:
        parse_llm_draft("抱歉，我无法访问数据库结构信息。")
    assert caught.value.code == "llm_bad_response"


def test_clarify_非空时本轮是追问而不是_SQL() -> None:
    draft = parse_llm_draft('{"sql": "", "explanation": "", "clarify": "您要哪一年的数据？"}')
    assert draft.sql is None
    assert draft.clarify == "您要哪一年的数据？"


def test_空响应也是坏响应而不是静默返回空串() -> None:
    with pytest.raises(AppError) as caught:
        parse_llm_draft("   ")
    assert caught.value.code == "llm_bad_response"
