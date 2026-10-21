"""`cartesian_product`（第一层第 19 枚，037）的**判据逐格**用例。

语料（`tests/guard/corpus.py`）那一族只有三拒一放行，它们断的是"这条 SQL 会被拒/会过"；
本文件断的是**为什么**——safety §1.2 ⑥.1 那条判据是"`on`/`using`/`side`/`kind`/`method` 五格全空
**且该作用域没有 `WHERE`**"，五个 key 加一个 `WHERE` 就是六格，每一格都得有一条一对一的钉子，
否则那一格哪天掉了（改判据、或 sqlglot 升级把修饰词挪了家）红的是"少挡一条"或"多误伤一类"，
答不出红在哪一格。§8.2 的 as-built 注明写这一族不进语料、住在这里，就是这个理由。

`NATURAL JOIN` 那一格是本轮"先实测再落规则"抓出来的**误伤**：判据第一版只查 `side`/`kind`，
而 sqlglot 30.19 把 `NATURAL` 住在 `args["method"]`、`side`/`kind` 都是空（实测
`.scratch/p5-sql-guard-hardening/evidence/probe_cartesian_ast037.txt` 末两段）。
所以下面每条放行用例都**同时**断两件事：守卫放行它，**且**它靠的是哪一格 key、那一格里住的是什么值。
只断前一件的话，"key 挪家"这一类要等到误伤真发生那天才红。

期望值来源：上面那份实测文件（五格取值逐条抄自它），不是从 `_join_violations` 的实现反推。
"""

from __future__ import annotations

import pytest
from sqlglot import exp, parse_one

from app.services import sql_guard
from app.services.sql_guard import GuardRuleId
from tests.guard.corpus import DEFAULT_SCHEMA, DEMO_TABLES


def _guard(sql: str) -> sql_guard.GuardResult:
    return sql_guard.guard(sql, allowed=DEMO_TABLES, dialect="mysql", default_schema=DEFAULT_SCHEMA)


def _join_keys(sql: str) -> dict[str, object]:
    """独立地重解析一遍，取第一个 `exp.Join` 的五格 key（不复用被测函数里的取值路径）。"""
    join = next(iter(parse_one(sql, dialect="mysql").find_all(exp.Join)))
    return {key: join.args.get(key) for key in ("on", "using", "side", "kind", "method")}


# 每条：(SQL, 那格 key, 实测住在里面的值)。七条把判据的六格（`on`/`using`/`side`/`kind`/`method`
# 五格 + 该作用域的 `WHERE`）逐一隔离：每条**只有指定的那一格非空**，其余四格必须全空，
# 断言里就是这么写的。`kind` 有两条（`CROSS` 与 `STRAIGHT_JOIN`），因为它们在 §9 里的下场不同。
# `expected` 为 `None` 的那两行（`on`/`using`）钉的是"这一格非空"，不钉表达式字面——
# 连接条件长什么样不是判据的一部分，判据只看它在不在场。
_PASSING_CELLS: list[tuple[str, str | None, object]] = [
    ("SELECT c.id, o.id FROM customer c NATURAL JOIN order_main o LIMIT 10", "method", "NATURAL"),
    ("SELECT c.id, o.id FROM customer c CROSS JOIN order_main o LIMIT 10", "kind", "CROSS"),
    (
        "SELECT c.id, o.id FROM customer c STRAIGHT_JOIN order_main o LIMIT 10",
        "kind",
        "STRAIGHT_JOIN",
    ),
    (
        "SELECT c.id, o.id FROM customer c LEFT JOIN order_main o LIMIT 10",
        "side",
        "LEFT",
    ),
    (
        "SELECT c.id, o.id FROM customer c JOIN order_main o ON c.id=o.customer_id LIMIT 10",
        "on",
        None,
    ),
    (
        "SELECT o.id, i.id FROM order_main o JOIN order_item i USING (order_id) LIMIT 10",
        "using",
        None,
    ),
    (
        "SELECT c.id, o.id FROM customer c, order_main o WHERE c.id=o.customer_id LIMIT 10",
        None,
        True,  # 五格全空，靠的是该作用域 `where` 在场
    ),
]


@pytest.mark.parametrize("sql,key,expected", _PASSING_CELLS)
def test_每一格放行都由它自己那一格撑着(sql: str, key: str | None, expected: object) -> None:
    """放行 + 归因：这一支过，是因为**指定的那一格**非空，不是因为别的格碰巧也在场。

    "其余四格必须全空"这一半才是这条用例的价值：只断"它过了"，那么一支因为 `WHERE` 在场的放行
    也会被读成"`method` 那格起作用"，那一格真掉了的时候这里照样绿。

    `side` 与 `kind` 那两行的放行**不等于安全**，各有各的记录：`FROM a STRAIGHT_JOIN b`
    （不带 `ON`）在 MySQL 里就是笛卡尔积，这一格今天挡不住（§9 的 known limitation，
    下面那条用例钉的就是这个现状）；
    `LEFT JOIN` 不带 `ON`/`USING` 是源库当场拒的语法，落到第二层而不是第一层。
    判据对这两行"非空即跳过"是**有意的取舍**（要区分"写法自带语义"与"根本没写条件"），
    不是把它们当成安全。
    """
    out = _guard(sql)
    assert out.ok, out.violations
    assert out.violations == ()

    if key is None:
        # `WHERE` 那一半：五格必须**全空**，否则这支证的就不是 WHERE 格
        joined = parse_one(sql, dialect="mysql").find(exp.Join)
        assert joined is not None
        assert all(not joined.args.get(name) for name in ("on", "using", "side", "kind", "method"))
        return

    keys = _join_keys(sql)
    other = {name: value for name, value in keys.items() if name != key}
    assert all(not value for value in other.values()), other
    if expected is not None:
        assert keys[key] == expected
    else:
        assert keys[key]


_REJECT_CELLS = [
    # （SQL，说明这一支钉的是判据的哪一格）
    ("SELECT * FROM customer, order_main", "本节 §8.1 原话那唯一一支：无条件逗号连接"),
    ("SELECT * FROM customer JOIN order_main", "覆盖面比原话宽的那格：显式 JOIN 不写 ON"),
    (
        "SELECT * FROM (SELECT c.id FROM customer c, order_main o) t",
        "逐 scope：内层作用域里的无条件逗号连接也要被看见",
    ),
    (
        "WITH monthly AS (SELECT c.id FROM customer c, order_main o) SELECT * FROM monthly",
        "逐 scope 的 CTE 那一支：`find_all` 得进 CTE 的作用域",
    ),
]


@pytest.mark.parametrize("sql,cell", _REJECT_CELLS)
def test_判据的每一格被拿掉时当场拒(sql: str, cell: str) -> None:
    """上面那张放行表的**对偶**：五格全空且作用域无 `WHERE` → 拒，且归因必须是这一枚码。

    `cell` 只进断言消息，不进期望值——期望值（这一枚码）对四支都相同，不同的只是它们各自证的那一格。
    """
    out = _guard(sql)
    assert not out.ok, cell
    assert [v.code for v in out.violations] == [GuardRuleId.cartesian_product], cell


def test_外层有_where_但内层作用域没有_仍然拒() -> None:
    """判据按 `find_ancestor(exp.Select)` 取**该作用域**的 `WHERE`，不是整条 SQL 的。

    写成"整条语句没有 WHERE 就全放行"的实现能过上面第一条与放行表最后一条，过不了这一条——
    而那一版正好把子查询里最常见的那类无条件逗号连接放过去。
    """
    sql = "SELECT * FROM (SELECT c.id FROM customer c, order_main o) t WHERE t.id > 1"
    out = _guard(sql)
    assert not out.ok
    assert [v.code for v in out.violations] == [GuardRuleId.cartesian_product]


def test_无关_where_照样放行_这是今天挡不住的那一格() -> None:
    """safety §1.2 ⑥.1"代价要说清"那一格与 §9 的 known limitation：**这一支今天不误伤，也不挡**。

    `FROM a, b WHERE a.x > 1` 是货真价实的笛卡尔积，但它与合法的老式写法在 AST 上给出的信息量
    差别就只有"有没有 WHERE"这一格；再往下判要比对 `WHERE` 里的列属于哪张表，那是把连接图重建一遍。
    这条用例钉的是**现状**（不许哪天有人把它改成"断言拒"从而悄悄扩大误伤面），
    不是"这样做是对的"。交付记录与 §9 都不许把它写成"笛卡尔积已全面挡住"。
    """
    out = _guard("SELECT * FROM customer c, order_main o WHERE c.status = 1")
    assert out.ok, out.violations


def test_三表无条件连接报两枚_每枚指向它自己的那格() -> None:
    """违规是**逐个 join 节点**收集的，不是"这条语句有一个笛卡尔积"。

    前端一次展示全部违规（§1.1 的 `guard()` 接缝）与 038 回喂模型都靠这个形状：
    三表只报一枚的话，第二张表那一格就没人认领。`node` 那一栏要指到具体的表。
    """
    out = _guard("SELECT * FROM customer c, order_main o, payment_record p")
    assert not out.ok
    assert [v.code for v in out.violations] == [GuardRuleId.cartesian_product] * 2
    assert [v.node for v in out.violations] == ["order_main AS o", "payment_record AS p"]
