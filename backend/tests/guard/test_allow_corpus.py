from __future__ import annotations

import pytest

from app.services import sql_guard
from tests.guard.corpus import ALLOW_CASES, DEFAULT_SCHEMA, DEMO_TABLES


def _check(sql: str) -> sql_guard.GuardResult:
    return sql_guard.check(sql, allowed=DEMO_TABLES, dialect="mysql", default_schema=DEFAULT_SCHEMA)


@pytest.mark.parametrize("sql", ALLOW_CASES, ids=[s[:38] for s in ALLOW_CASES])
def test_合法只读查询被放行并强制带上limit(sql: str) -> None:
    out = _check(sql)

    assert "LIMIT" in out.sql_final.upper()
    assert ";" not in out.sql_final
    assert "/*" not in out.sql_final
    assert out.sql_final.upper().count(" LIMIT ") == 1, "不得出现双 LIMIT"
    if "OFFSET" in sql.upper():
        assert out.sql_final.upper().endswith("LIMIT 5 OFFSET 10"), "分页偏移不得被改坏"
    if "LIMIT" not in sql.upper():
        # 注入值是 HARD_LIMIT + 1，多取一行用来探测 truncated
        assert out.sql_final.endswith("LIMIT 1001")
    # 幂等：重生成的 SQL 必须还能被严格解析
    reparsed = sql_guard.parse(out.sql_final, dialect="mysql")
    # 防 rewrite 漂移：再 parse + 再生成必须得到同一份 SQL
    assert reparsed.sql(dialect="mysql", comments=False) == out.sql_final


def test_table_refs恰好是sql里引用的表含别名解析() -> None:
    out = _check(
        "SELECT o.id, SUM(o.amount) AS total FROM order_main o "
        "JOIN customer c ON c.id=o.customer_id GROUP BY o.id LIMIT 10"
    )
    assert {(t.db, t.name) for t in out.tables} == {
        (DEFAULT_SCHEMA, "order_main"),
        (DEFAULT_SCHEMA, "customer"),
    }


def test_cte名不算表引用() -> None:
    out = _check("WITH monthly AS (SELECT amount s FROM order_main) SELECT * FROM monthly LIMIT 5")
    assert [t.name for t in out.tables] == ["order_main"]
