"""§1.1 的 guard() 接缝：返回全部违规，不抛异常。"""

from __future__ import annotations

from sqlglot import exp, parse_one

from app.services import sql_guard
from tests.guard.corpus import DEFAULT_SCHEMA, DEMO_TABLES


def _guard(sql: str, *, max_rows: int = 1000) -> sql_guard.GuardResult:
    return sql_guard.guard(
        sql, allowed=DEMO_TABLES, dialect="mysql", default_schema=DEFAULT_SCHEMA, max_rows=max_rows
    )


def _top_limit_rows(sql: str) -> int | None:
    """**独立地**再读一遍顶层 LIMIT 的行数——不复用 `_limit_row_count`，否则不变式就成了自证。"""
    limit = parse_one(sql, dialect="mysql").args.get("limit")
    if limit is None:
        return None
    node: object = limit.expression
    if isinstance(node, exp.Literal) and node.is_number:
        try:
            return int(str(node.this))
        except ValueError:
            return None
    return None


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


def test_模型自带的_LIMIT_超上限时被钳回探针() -> None:
    """`max_rows` 是**上限**，不是"只有缺 LIMIT 时才生效的建议值"。

    模型照 prompt 模板写 `LIMIT {{ row_limit }}` 是真链路的常态，所以放行它等于同时废掉两样：
    ① 少掉那一行 `+1` 探针，**这一支的** `truncated` 恒假；② 011 的"缓冲取回的内存上限由守卫那句
    `LIMIT row_limit+1` 钉住"这个前提。
    """
    exactly = _guard("SELECT id FROM customer LIMIT 1000", max_rows=1000)
    assert exactly.ok is True and exactly.sql_final == "SELECT id FROM customer LIMIT 1001"
    over = _guard("SELECT id FROM customer LIMIT 5000", max_rows=1000)
    assert over.sql_final == "SELECT id FROM customer LIMIT 1001"


def test_上限之下的_LIMIT_原样保留() -> None:
    """模型按问题意图选的量是语义，不是噪声——"前 5 名"不该被抬到上限。

    这一支**没有**探针：行数小于上限就不可能触到 `row_limit`，`truncated=false` 在这里是
    正确答案而不是失效（`LIMIT 5 OFFSET 10` 那一类的幂等形状由 `test_allow_corpus.py` 钉）。
    """
    out = _guard("SELECT id FROM customer LIMIT 999", max_rows=1000)
    assert out.sql_final == "SELECT id FROM customer LIMIT 999"


def test_钳位只动行数不动偏移() -> None:
    """MySQL 的 `LIMIT <offset>, <count>` 会重生成成 `LIMIT count OFFSET offset`，
    钳位只许碰 count——把 offset 一起改了就不是"少给几行"而是换了一页数据。"""
    out = _guard("SELECT id FROM customer LIMIT 4990, 5000", max_rows=1000)
    assert out.sql_final == "SELECT id FROM customer LIMIT 1001 OFFSET 4990"


def test_读不出行数的_LIMIT_按缺_LIMIT_同一档处理() -> None:
    """这几条形状都**证明不了**行数在上限之内，出路只有一条：钳成 `max_rows+1`，
    与不带 LIMIT 走同一条路。

    - `LIMIT ALL`：pg 语法，mysql 方言下 sqlglot 落成标识符 `ALL`（重生成出来是 `` `ALL` ``，
      源库根本不认）；
    - `LIMIT 1 + 1`：表达式，不是字面量；
    - `LIMIT 1e3`：是数字字面量，但 `int()` 认不出这个写法；
    - `LIMIT -1`：sqlglot 落成 `Neg` 而非数字字面量。注意理由**不是**"MySQL 里 -1 等于不限"
      （那是 SQLite 的口径，MySQL 给负数 row_count 会报 1210）——我们只是读不出一个确定的
      非负行数，所以按不可证处理。钳位顺带把这条必然失败的语句变成能跑的截断查询。
    """
    for sql in (
        "SELECT id FROM customer LIMIT ALL",
        "SELECT id FROM customer LIMIT -1",
        "SELECT id FROM customer LIMIT 1 + 1",
        "SELECT id FROM customer LIMIT 1e3",
    ):
        out = _guard(sql, max_rows=1000)
        assert out.ok is True, (sql, out.violations)
        assert out.sql_final == "SELECT id FROM customer LIMIT 1001", sql


def test_钳位不变式_守卫输出的顶层_LIMIT_恒不超过_max_rows_plus_1() -> None:
    """011 的内存前提真正依赖的是这一条**上界**，而不是"注入值恰好等于 `max_rows+1`"。

    逐形状断言只覆盖点，不覆盖面；这条把"钳位只作用于顶层"这件事本身钉住：臂内/CTE 里的
    `LIMIT 5000` 不钳（MySQL 的顶层 LIMIT 约束整条语句的返回行数，客户端缓冲因此仍有界），
    但顶层那一个数**任何形状**都不得大于 `max_rows+1`。
    """
    shapes = (
        "SELECT id FROM customer",
        "SELECT id FROM customer LIMIT 5",
        "SELECT id FROM customer LIMIT 999",
        "SELECT id FROM customer LIMIT 1000",
        "SELECT id FROM customer LIMIT 5000",
        "SELECT id FROM customer LIMIT ALL",
        "SELECT id FROM customer LIMIT 1 + 1",
        "SELECT id FROM customer LIMIT 1e3",
        "SELECT id FROM customer LIMIT 4990, 5000",
        "SELECT id FROM customer LIMIT 5000 UNION ALL SELECT id FROM payment_record LIMIT 3",
        "WITH x AS (SELECT id FROM customer LIMIT 5000) SELECT * FROM x",
    )
    for sql in shapes:
        out = _guard(sql, max_rows=1000)
        assert out.ok is True, (sql, out.violations)
        rows = _top_limit_rows(out.sql_final or "")
        assert rows is not None and rows <= 1001, (sql, out.sql_final)
