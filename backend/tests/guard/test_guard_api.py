"""§1.1 的 guard() 接缝：返回全部违规，不抛异常。"""

from __future__ import annotations

from app.services import sql_guard
from tests.guard.corpus import DEFAULT_SCHEMA, DEMO_TABLES


def _guard(sql: str) -> sql_guard.GuardResult:
    return sql_guard.guard(sql, allowed=DEMO_TABLES, dialect="mysql", default_schema=DEFAULT_SCHEMA)


def test_违规一次全部收集便于前端展示() -> None:
    out = _guard("SELECT SLEEP(1) FROM mysql.user")
    assert out.ok is False
    assert out.sql_final is None
    assert [v.code for v in out.violations] == ["sleep_function", "table_not_allowed"]


def test_合法查询返回重生成的SQL与命中表() -> None:
    out = _guard("SELECT id FROM customer")
    assert out.ok is True and out.violations == ()
    assert out.sql_final == "SELECT id FROM customer LIMIT 1001"
    assert out.tables == (sql_guard.QualifiedTable("", "ai_web_demo", "customer"),)


def test_llm输出的代码块围栏被剥掉() -> None:
    out = _guard("```sql\nSELECT id FROM customer LIMIT 5\n```")
    assert out.ok is True, out.violations
