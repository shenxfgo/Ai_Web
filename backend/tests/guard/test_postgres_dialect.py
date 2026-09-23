"""postgres 方言切片：与 mysql 共用规则，但标识符大小写与锁定子句写法不同。

语料文件是 docs/nl2sql-safety.md §8 的逐条转写，不放方言专属用例，所以单开一个文件。
"""

from __future__ import annotations

import pytest

from app.services import sql_guard
from app.services.sql_guard import QualifiedTable, SqlGuardError
from tests.guard.corpus import DEFAULT_SCHEMA, run


def _run(sql: str):
    return run(sql, dialect="postgres")


def test_加引号的表名保留大小写不能被折叠() -> None:
    # PG 里 "Customer" 与 customer 是两张不同的表：折叠大小写等于放过一张未授权的同名表
    with pytest.raises(SqlGuardError) as ei:
        _run('SELECT * FROM "Customer" LIMIT 1')
    assert ei.value.rule_id == "table_not_allowed"


def test_未加引号的名字仍按小写比对白名单() -> None:
    out = _run("SELECT ID FROM CUSTOMER")
    assert out.tables == (QualifiedTable("", DEFAULT_SCHEMA, "customer"),)
    assert "LIMIT 1001" in out.sql_final


@pytest.mark.parametrize(
    ("sql", "rule"),
    [
        ("SELECT * FROM pg_catalog.pg_tables LIMIT 1", "table_not_allowed"),
        ("COPY customer TO STDOUT", "top_level_not_select"),
        ("SELECT * FROM customer FOR NO KEY UPDATE", "locking_clause"),
        ("SELECT * FROM customer FOR KEY SHARE", "locking_clause"),
        ("DROP TABLE customer", "top_level_not_select"),
    ],
)
def test_pg专属攻击语料被拒(sql: str, rule: str) -> None:
    with pytest.raises(SqlGuardError) as ei:
        _run(sql)
    assert ei.value.rule_id == rule


@pytest.mark.parametrize(
    "sql",
    [
        "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM t) SELECT * FROM t LIMIT 5",
        "SELECT date_trunc('month', created_at) m, count(*) FROM order_main GROUP BY 1",
        "SELECT to_char(created_at,'YYYY-MM') FROM customer LIMIT 3",
        "SELECT id::text FROM customer LIMIT 3",
        "SELECT substring(name from 1 for 2) FROM customer LIMIT 3",
    ],
)
def test_pg常用只读写法放行(sql: str) -> None:
    out = _run(sql)
    assert "LIMIT" in out.sql_final.upper()
    assert out.ok and not out.violations


def test_pg反斜杠不是转义符掩码不能按mysql语义吞掉后半句() -> None:
    # PG 标准字符串里 `'a\'` 在第三个引号处就结束了；按 MySQL 语义会以为整段还是字面量，
    # 于是把后面的 mysql.user 掩掉 → 归因从 table_not_allowed 漂成解析失败。
    with pytest.raises(SqlGuardError) as ei:
        _run(r"SELECT 'a\' AS x, password FROM mysql.user")
    assert ei.value.rule_id == "table_not_allowed"


def test_白名单是按数据源算的不是全库() -> None:
    with pytest.raises(SqlGuardError) as ei:
        sql_guard.check("SELECT * FROM customer", allowed=frozenset(), dialect="postgres")
    assert ei.value.rule_id == "table_not_allowed"
