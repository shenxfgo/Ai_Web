"""会话级只读/超时的 SET 语句序列：safety §4.1（MySQL）与 §4.2（PG）的原文落地。

这是纯函数，把文档里那两段 SQL 一字不差地拼出来，好让"顺序对不对、有没有漏 READ ONLY"
在离线就能钉死——真中断/真拒绝留给 live 用例（§4.1 的引擎拒写只有真连才验得出）。

期望值口径全部来自 §4.1/§4.2 的代码块：
- MySQL：READ ONLY 必须在最前（"前不能在已有事务里"），MAX_EXECUTION_TIME 用 timeout_ms 原值，
  wait_timeout 用 timeout_ms/1000 + 10，最后 autocommit=1。
- PG：default_transaction_read_only=on + statement_timeout='<n>ms'（字符串带单位）。
"""

from __future__ import annotations

from app.services.nl2sql.executor import session_statements


def test_mysql_顺序_with_值() -> None:
    stmts = session_statements("mysql", timeout_ms=15000)
    assert stmts[0] == "SET SESSION TRANSACTION READ ONLY"  # 必须最先
    assert "SET SESSION MAX_EXECUTION_TIME = 15000" in stmts
    # wait_timeout 单位秒：15000/1000 + 10 = 25
    assert "SET SESSION wait_timeout = 25" in stmts
    assert "SET SESSION autocommit = 1" in stmts


def test_pg_会话只读_with_statement_timeout() -> None:
    stmts = session_statements("postgres", timeout_ms=8000)
    assert "SET default_transaction_read_only = on" in stmts
    assert "SET statement_timeout = '8000ms'" in stmts
