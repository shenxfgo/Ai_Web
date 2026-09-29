"""§1.1 的 guard() 接缝：返回全部违规，不抛异常。"""

from __future__ import annotations

from app.services import sql_guard
from tests.guard.corpus import DEFAULT_SCHEMA, DEMO_TABLES


def _guard(sql: str, *, max_rows: int = 1000) -> sql_guard.GuardResult:
    return sql_guard.guard(
        sql, allowed=DEMO_TABLES, dialect="mysql", default_schema=DEFAULT_SCHEMA, max_rows=max_rows
    )


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


def test_已有_LIMIT_时守卫只重生成不补探针() -> None:
    """钉住 `max_rows` 的**现状语义**：它是"缺 LIMIT 时补多少"，不是"最多允许多少"。

    两条后果都在这条用例里（都是真的，不是想象中的）：
    ① 模型自带 `LIMIT 1000` 时不会有那 `+1` 行探针，执行侧的 `truncated` 因此**恒假**——
      而 prompt 模板正是要模型写 `LIMIT {{ row_limit }}`，所以这是真链路的常态；
    ② 模型写 `LIMIT 5000`（大于上限）时**原样放行**，011 的"缓冲取回的内存上限由守卫那句
      `LIMIT row_limit+1` 钉住"这个前提对它不成立。
    修法（把 `LIMIT n` 钳到 `min(n, max_rows+1)`）动的是 003 的裁决语义，
    另开工单，不塞在本片尾巴上。
    """
    exactly = _guard("SELECT id FROM customer LIMIT 1000", max_rows=1000)
    assert exactly.sql_final == "SELECT id FROM customer LIMIT 1000"
    over = _guard("SELECT id FROM customer LIMIT 5000", max_rows=1000)
    assert over.ok is True and over.sql_final == "SELECT id FROM customer LIMIT 5000"
