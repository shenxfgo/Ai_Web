"""工单 036：守卫**内部**的重生成等价自校验——"守卫看到的 = 执行的那条"第一次能被证明。

比对的两侧按已定口径是 `final`（钳位之后那棵树）与 `parse(sql_final)`，**不是**模型原文那棵 `stmt`：
`guard()` 自己会注入/改写顶层 LIMIT，所以"重生成 ≠ 原文"是设计而不是漂移。

**口径变更（工单 036 施工中按实测改的形，用户 2026-10-20 拍板）**：原本这里是一条不动点
加一条 `Expression.__eq__`，实物上 `__eq__` **两头都不对**——它过火（今天合法放行的 UNION 会被
误杀成漂移），又不及物（sqlglot 30.19 下它看不见注释的丢失）。所以换成语义承重的三格：
**① 不动点 + ② 顶层形状 + ③ 表引用 + ④ 顶层行数**，挑的是"守卫判过的那些承重事实在
执行的那句话里还在不在"，括号、`AS`、空白这类渲染形状不进判定。
证据：`evidence/drift_diagnosis036.txt`（`__eq__` 的树差 `.this: Select vs Subquery`）与
`evidence/cell_probe036.txt`（本文件每一条期望值的实物来源）。

每格都要能**单独红**，且红的时候其余格必须是绿的——一条看不见另一条在防什么的断言等于装饰。
`test_守卫给臂加的括号不算漂移` 就是这次换形的那颗钉子。

本文件一条现有用例都不改：`REJECT_CASES` 54 条、`ALLOW_CASES` 18 条的期望值一字不动，
hint 那一族的新形状（工单 036 验收 4 的 (B) 支）也全部落在这里，不进语料。
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from app.services import sql_guard
from app.services.sql_guard import GuardRuleId, QualifiedTable, SqlGuardError
from tests.guard.corpus import ALLOW_CASES, DEFAULT_SCHEMA, DEMO_TABLES

_CUSTOMER: tuple[QualifiedTable, ...] = (QualifiedTable.parse(f"{DEFAULT_SCHEMA}.customer"),)


def test_词表里有_regeneration_drift_这一枚且值就是成员名() -> None:
    """第 18 枚：**内部一致性故障**，不是用户 SQL 违规。

    它必须与那 17 枚同一枚枚举（归因走 `rule_id` 才有单一词表可统计），
    又必须**不挂** `unsupported_construct`——那等于把我们的 bug 报成用户的 SQL 违规。
    """
    assert GuardRuleId.regeneration_drift == "regeneration_drift"
    assert GuardRuleId.regeneration_drift.name == GuardRuleId.regeneration_drift.value
    assert GuardRuleId("regeneration_drift") is GuardRuleId.regeneration_drift


def _tree(sql: str, dialect: str = "mysql") -> exp.Expression:
    return sqlglot.parse_one(sql, read=dialect, error_level=sqlglot.ErrorLevel.RAISE)


def _独立表名(tree: exp.Expression) -> set[str]:
    """**独立地**读一遍这棵树引用了哪些表名——不复用 `sql_guard._table_refs`。

    本文件要断的是"四格里只有被点名的那一格红"，也就是"另外三格在这对输入上是绿的"。
    拿守卫自己那把尺去量它自己判过的东西，那一半就成了自证（`tests/guard/test_guard_api.py:17-18`
    写明了同一条纪律：不复用 `_limit_row_count`，否则不变式就成了自证）。
    这里只到**裸表名**那一层：CTE 名要剔掉（语料里 `WITH monthly AS (…) SELECT * FROM monthly`
    那支不剔就会多出一张不存在的表），但不做三段式补全，所以带 schema 的期望值一律用名字比。
    """
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    return {t.name.lower() for t in tree.find_all(exp.Table) if t.name.lower() not in cte_names}


def _独立顶层行数(sql: str, dialect: str = "mysql") -> int | None:
    """同上，独立再读一遍顶层 LIMIT 那个数；不是"数字字面量"那个形状就回 None。"""
    limit = _tree(sql, dialect).args.get("limit")
    if limit is None:
        return None
    node: object = limit.expression
    if isinstance(node, exp.Literal) and node.is_number:
        try:
            return int(str(node.this))
        except ValueError:
            return None
    return None


def _check(
    final: exp.Expression,
    sql_final: str,
    *,
    dialect: str = "mysql",
    refs: tuple[QualifiedTable, ...] = _CUSTOMER,
) -> sql_guard.Violation | None:
    return sql_guard.regeneration_violation(
        final, sql_final, dialect=dialect, default_schema=DEFAULT_SCHEMA, refs=refs
    )


def _guard(
    sql: str,
    *,
    dialect: str = "mysql",
    schema: str = DEFAULT_SCHEMA,
    allowed: frozenset[QualifiedTable] = DEMO_TABLES,
) -> sql_guard.GuardResult:
    return sql_guard.guard(sql, allowed=allowed, dialect=dialect, default_schema=schema)


def test_一条不动点加三格全过时不产生违规() -> None:
    """自校验对**自洽**的那对（树，文本）回 `None`——这是它平时那一面。"""
    final = _tree("SELECT id FROM customer LIMIT 1001")

    assert _check(final, final.sql(dialect="mysql", comments=False)) is None


def test_不动点那一格单独红_同一句话换了写法时其余三格全绿() -> None:
    """① 防的是"端出去的那句话不是生成器自己的输出"（§1.2 ⑧）。

    这一对语义完全相同、承重事实一个没变，只差文本不是自己的不动点：
    关键字大小写、`AS` 补全、空白这类"再序列化一次就归位"的写法——
    今天最可能的成因是把 `.sql(...)` 的某个参数写错了。
    """
    final = _tree("SELECT id FROM customer")
    text = "select id from customer"

    violation = _check(final, text)

    assert violation is not None
    assert violation.code is GuardRuleId.regeneration_drift
    assert violation.node == "不动点"
    # 其余三格在这一对上必须是绿的，否则这条用例并没有真的把格分开。
    # 这三下量的是**独立读出来的**事实（见 `_独立表名`／`_独立顶层行数`），不是守卫自己那把尺。
    again = _tree(text)
    assert type(final) is type(again)
    assert _独立表名(again) == {"customer"}
    assert final.args.get("limit") is None and _独立顶层行数(text) is None


def test_顶层形状那一格单独红_括号让树变了类型而文本自己是自洽的() -> None:
    """② 防的是"钳位改了树却没重新生成"这一族的形状面。

    `(SELECT id FROM customer)` 是**自己的**不动点（探针实测 True），表也是那一张，
    行数两边都读不出来——唯一变的是顶层类型：守卫那棵树是 `Select`，端出去的解析回来是
    `Subquery`。这一格今天不会自己冒出来（钳位加在顶层不会造括号），它钉的是"以后再往
    `final` 上动刀时，形状变了必须被看见"。
    """
    final = _tree("SELECT id FROM customer")
    text = "(SELECT id FROM customer)"

    violation = _check(final, text)

    assert violation is not None
    assert violation.node == "顶层形状"
    assert "Select" in violation.message and "Subquery" in violation.message
    again = _tree(text)
    assert again.sql(dialect="mysql", comments=False) == text
    assert _独立表名(again) == {"customer"}
    assert final.args.get("limit") is None and _独立顶层行数(text) is None


def test_表引用那一格单独红_端出去的文本引用了白名单没判过的那张表() -> None:
    """③ 是四格里**唯一涉密**的一格：白名单判的是 `refs`，执行的那句话引用另一批表，
    就是"未授权的表跟着 SQL 一起出去了"。

    比集合不比顺序——生成器把 `o JOIN c` 写成 `c JOIN o` 不是换表。
    """
    final = _tree("SELECT id FROM customer")
    text = "SELECT id FROM payment_record"

    violation = _check(final, text, refs=_CUSTOMER)

    assert violation is not None
    assert violation.node == "表引用"
    assert "payment_record" in violation.message and "customer" in violation.message
    again = _tree(text)
    assert again.sql(dialect="mysql", comments=False) == text
    assert type(final) is type(again)
    assert final.args.get("limit") is None and _独立顶层行数(text) is None


def test_顶层行数那一格单独红_LIMIT的数在两端不一样() -> None:
    """④ 防的是钳位那一刀：`max_rows+1` 是内存上限，它若在重生成里变了，
    "结果集有上界"这条承诺就不成立了。

    这一对文本自洽、形状相同、表相同，只有守卫那棵树读出的行数与端出去的不一致。
    """
    final = _tree("SELECT id FROM customer LIMIT 1001")
    text = "SELECT id FROM customer LIMIT 5"

    violation = _check(final, text)

    assert violation is not None
    assert violation.node == "顶层行数"
    assert "1001" in violation.message and "5" in violation.message
    assert _独立顶层行数(final.sql(dialect="mysql", comments=False)) == 1001
    assert _tree(text).sql(dialect="mysql", comments=False) == text


def test_端出去的文本连再解析都过不去时归因还是regeneration_drift() -> None:
    """最前面那一格：`sql_final` 连解析都回不来，后面三格根本没有输入。

    归因**不许**滑回 `unsupported_construct`——那条码的意思是"用户的 SQL 我们不认识"，
    而这里用户那句话守卫早就判过并放行了。
    """
    violation = _check(_tree("SELECT id FROM customer"), "SELECT id FROM customer WHERE (")

    assert violation is not None
    assert violation.code is GuardRuleId.regeneration_drift
    assert violation.node == "ParseError"
    assert "再解析" in violation.message


def test_端出去的文本落回两条语句时那一格红() -> None:
    """同一趟出口的另一种形状：`sqlglot.parse` 不抛错，但返回两条——多语句在守卫内部
    重新出现时，走的是这一格不是 `multi_statement`（那条是**入口**规则）。
    """
    violation = _check(_tree("SELECT id FROM customer"), "SELECT id FROM customer; SELECT 2")

    assert violation is not None
    assert violation.code is GuardRuleId.regeneration_drift
    assert violation.node == "语句数=2"


def test_守卫给臂加的括号不算漂移() -> None:
    """换掉 `Expression.__eq__` 的那颗钉子。

    `SELECT … LIMIT 5000 UNION ALL SELECT … LIMIT 3` 今天**合法放行**，而生成器会给带
    LIMIT 的那条臂加括号；重 parse 回来顶层还是 `Union`、`.this` 从 `Select` 变成
    `Subquery`——`__eq__` 判它漂移，语义其实一字未变。三格按承重事实比，所以放行。

    钳位只长在**最外层**，这条的最外层行数读出来是 3（小于上限），所以模型自己写的
    `LIMIT 5000` 原样留着、也不该被抬成 `LIMIT 1001`。两个数都来自 `evidence/cell_probe036.txt`。
    """
    out = _guard(
        "SELECT id FROM customer LIMIT 5000 UNION ALL SELECT id FROM payment_record LIMIT 3"
    )

    assert out.ok is True, out.violations
    assert out.sql_final == (
        "(SELECT id FROM customer LIMIT 5000) UNION ALL SELECT id FROM payment_record LIMIT 3"
    )
    assert "LIMIT 1001" not in out.sql_final


def test_四格里没有一格是今天合法输入的常态() -> None:
    """永真性：`ALLOW_CASES` 18 条逐条走守卫，自校验一次都不许红——它红一次就拒一次答，
    误杀在这里的代价与漏放一样高。

    不动点那一格用 `_tree`（`parse_one`）另算一遍，不走守卫出口那条 `sqlglot.parse` 路径；
    表引用那一格拿守卫自己报的 `tables` 对，但**读的是独立那把尺**（`_独立表名`）——
    白名单判过的那批表，重生成之后必须还是那批。
    """
    for sql in ALLOW_CASES:
        out = _guard(sql)
        assert out.ok is True, (sql, out.violations)
        assert out.sql_final is not None
        again = _tree(out.sql_final)
        assert again.sql(dialect="mysql", comments=False) == out.sql_final, sql
        assert _独立表名(again) == {ref.name.lower() for ref in out.tables}, sql


def test_guard_出口真的又解析了一次_且吃的就是它端出去的那句话(monkeypatch) -> None:
    """接线的证据：这道自校验**住在守卫内部**，不是测试侧重跑一遍同一句话。

    数 `sqlglot.parse` 的调用：一次是入口解析模型原文，一次是出口校验 `sql_final`，
    第二次的入参必须**逐字等于**守卫端出去的那串——否则它校验的是别的东西。
    """
    seen: list[str] = []
    real = sqlglot.parse

    def counting(sql: str, *args: object, **kwargs: object) -> list[exp.Expression | None]:
        seen.append(sql)
        return real(sql, *args, **kwargs)  # type: ignore[operator]

    monkeypatch.setattr(sqlglot, "parse", counting)
    out = _guard("SELECT id FROM customer LIMIT 5")

    assert out.ok is True
    assert out.sql_final is not None
    assert seen == ["SELECT id FROM customer LIMIT 5", out.sql_final]


def test_自校验那一趟解析失败时按拒答处理且两条接缝同码(monkeypatch) -> None:
    """不降级成 warning：这一条红了我们连"执行的是哪句话"都不知道。

    伪造只在**出口那一趟**红（按守卫端出去的那串文本认，入口那趟照真跑），所以同一个断言
    能在 `guard()` 与 `check()` 两条接缝上各跑一遍而不互相干扰。
    `guard()` 给 `ok=False` 且 `sql_final=None`（漂移的 SQL 一律不外发），`check()` 抛同一枚成员。
    """
    sql = "SELECT id FROM customer"
    drift_text = "SELECT id FROM customer LIMIT 1001"  # 钳位之后守卫真正端出去的那句话
    calls: list[str] = []
    real = sqlglot.parse

    def flaky(text: str, *args: object, **kwargs: object) -> list[exp.Expression | None]:
        calls.append(text)
        if text == drift_text:
            raise sqlglot.errors.ParseError("探针伪造：出口那一趟解析失败")
        return real(text, *args, **kwargs)  # type: ignore[operator]

    monkeypatch.setattr(sqlglot, "parse", flaky)
    out = _guard(sql)

    assert out.ok is False
    assert out.sql_final is None
    assert [v.code for v in out.violations] == [GuardRuleId.regeneration_drift]
    assert calls == [sql, drift_text]

    with pytest.raises(SqlGuardError) as ei:
        sql_guard.check(sql, allowed=DEMO_TABLES, dialect="mysql", default_schema=DEFAULT_SCHEMA)
    assert ei.value.rule_id is GuardRuleId.regeneration_drift
    assert calls == [sql, drift_text, sql, drift_text]


@pytest.mark.parametrize(
    ("dialect", "schema", "allowed", "sql"),
    [
        (
            "mysql",
            DEFAULT_SCHEMA,
            DEMO_TABLES,
            "SELECT /*+ NO_ICP */ o.id FROM order_main o JOIN customer c ON c.id = o.customer_id",
        ),
        (
            "postgres",
            "public",
            frozenset({QualifiedTable.parse("public.t1")}),
            "SELECT /*+ SeqScan(t1) */ id FROM t1",
        ),
        # PG 把 `/*+ x */` 当**普通注释**，我们的判据是方言盲的——裸文本层看不到方言
        (
            "postgres",
            "public",
            frozenset({QualifiedTable.parse("public.t1")}),
            "SELECT /*+ x */ 1 FROM (SELECT 1) t",
        ),
    ],
    ids=["mysql_join_hint", "pg_seqscan_hint", "pg_里的hint形状"],
)
def test_hint_三种形状两条接缝同码(
    dialect: str, schema: str, allowed: frozenset[QualifiedTable], sql: str
) -> None:
    """工单 036 验收 4 的 (B) 支落进语料之外：hint 的防线**只有裸文本那一道**。

    带 JOIN 的那支证明它不在"SELECT 后面紧跟什么"那种窄形状里也照样被抓；
    pg 那两支证明即便该方言根本不支持 hint，我们也一并拒——放行它等于放行
    "注释里能塞指令"这个形状本身。`check()` 与 `guard()` 必须给同一枚成员，
    否则归因统计会分裂成两套。
    """
    out = _guard(sql, dialect=dialect, schema=schema, allowed=allowed)
    assert [v.code for v in out.violations] == [GuardRuleId.hint_not_allowed]

    with pytest.raises(SqlGuardError) as ei:
        sql_guard.check(sql, allowed=allowed, dialect=dialect, default_schema=schema)
    assert ei.value.rule_id is GuardRuleId.hint_not_allowed


def test_hint_在_MySQL_的树上活得过_comments_false() -> None:
    """这一条**证伪**了 `docs/nl2sql-safety.md` §2 原来的那句"`comments=False` 会把 hint 丢掉"。

    sqlglot 30.19 把 MySQL 的 `/*+ … */` 落成 `args["hint"]` 这个真的 `exp.Hint` 节点：
    它是 AST 的一部分而不是注释，`comments=False` 拿它没办法
    （`evidence/hint_probe036.txt` 前两支）。
    所以"重生成会把注释抹掉"不能作为 hint 的防线——唯一的防线是裸文本那道 `body.startswith("+")`，
    而它必须在 `comments=False` 之前跑到。
    """
    sql = "SELECT /*+ MAX_EXECUTION_TIME(1) */ id FROM customer"
    tree = _tree(sql)

    assert isinstance(tree.args.get("hint"), exp.Hint)
    assert "/*+" in tree.sql(dialect="mysql", comments=False)

    out = _guard(sql)
    assert out.ok is False
    assert [v.code for v in out.violations] == [GuardRuleId.hint_not_allowed]


def test_加号前有空白时不是_hint_放行是对的且判据与源库同构() -> None:
    """`/* +X */` 在 MySQL 里**不是** hint，是普通注释——我们的判据是同一条：`+` 必须紧跟 `/*`。

    这一对（拒 `/*+`、放 `/* +`）放在一起才叫"同构"：只测放行那一半，
    判据写成 `body.startswith(" ")` 也能过。
    """
    out = _guard("SELECT /* +MAX_EXECUTION_TIME(1) */ id FROM customer")

    assert out.ok is True
    assert out.sql_final == "SELECT id FROM customer LIMIT 1001"
    assert _tree("SELECT /* +MAX_EXECUTION_TIME(1) */ id FROM customer").args.get("hint") is None


def test_hint_写在字符串字面量里是数据_放行且它给子串断言划出范围() -> None:
    """`'/*+ … */'` 落在引号里，tokenizer 先认字符串，树上没有 `exp.Hint`。

    顺带把 `tests/guard/test_allow_corpus.py` 那两条 `";" not in` / `"/*" not in` 的**范围**
    钉住：它们对语料那 18 条成立，不是"sql_final 里永远不许出现 `/*`"的全称量词——
    这一条的 `sql_final` 里就带着 `/*`，而它是合法的。
    """
    sql = "SELECT '/*+ MAX_EXECUTION_TIME(1) */' AS remark FROM customer"
    out = _guard(sql)

    assert out.ok is True
    assert out.sql_final == f"{sql} LIMIT 1001"
    assert "/*" in out.sql_final
    assert _tree(sql).args.get("hint") is None
