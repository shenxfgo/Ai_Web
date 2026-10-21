"""第二层 `EXPLAIN` dry-run 的**形状与裁决**（工单 037 涉及层第 2 格、验收 5；safety §3.2 第 4 条
明写了第二层那三枚码的钉子不住在 `tests/guard/` 而在本文件——`guard()` 不碰库，语料驱动不到它们）。

为什么形状也要不连库钉一条（与 029/031 的 `test_sync_embed_sql.py`、`test_retriever_vector_sql.py`
同一条理由，这里更极端）：**PG 那一支今天根本没有真跑**。执行路径对 PG 直接 501
（`executor.py` 那一发 `NotImplementedSource`），`dry_run.py` 里那四条语句一条都发不出去，
所以"PG 的 dry-run 行为正确"这句话今天没有任何真库证据能撑——能撑住它的只有文本。
形状错了（次序颠倒、`SET LOCAL` 掉了、忘了 `ROLLBACK`）在真库里看不见，因为压根没人发过。

**这一族用例断的是文本与纯函数，不是行为。** 不许把下面任何一条写成"PG 的只读事务证过了"。
行为用例等"PG 只读执行通路"落地那天补（safety §3.1 的 as-built(P5-037) 明写了这一格）。
裁决那一半同理：它吃的是**真库读回来的那串数字**（实测文件在下面的分段注释里点名了），
但它自己不发语句——"真发出去、真被源库拒"那一档归 MySQL live 那一族用例。

期望值口径：`docs/nl2sql-safety.md` §3.1.1 那四条逐字对照（`BEGIN READ ONLY` 在 `EXPLAIN` 之前、
`SET LOCAL statement_timeout='3000ms'` 的字面、`ROLLBACK` 在最后），MySQL 一支按 §3.1
那句"`EXPLAIN <sql_final>`"。
"""

from __future__ import annotations

import pytest

from app.services.nl2sql.dry_run import explain_statements, explain_verdict
from app.services.sql_guard import GuardRuleId, SqlGuardError

# 一条过完第一层守卫、真会发给源库的形状（`customer` 是演示库的表，5.7 的 EXPLAIN 认它）
_SQL_FINAL = "SELECT id, name FROM customer WHERE status = 'active' LIMIT 5"


def test_mysql_一支只有一条_explain() -> None:
    """MySQL 5.7 的 `EXPLAIN <select>` 不执行语句，所以单独一条就够，不需要包事务。"""
    assert explain_statements("mysql", _SQL_FINAL) == (f"EXPLAIN {_SQL_FINAL}",)


def test_嵌进去的就是传进来的那句_一字不动() -> None:
    """第二层必须钉在 `sql_final` 上（safety §3.1 第一条，也是 036 那条等价断言的下游）。

    拿模型原文去 EXPLAIN、或者在这里"顺手规范化"一下，两层就各验各的了：第一层证明的是
    "守卫看到的 = 执行的那条"，第二层要证明的是**那一条**在引擎里跑得通、估行不超标。
    所以这里断的是逐字相等——空格、大小写、换行都不许动。
    """
    odd = "select\tID\n from  customer limit 10"
    (statement,) = explain_statements("mysql", odd)
    assert statement.removeprefix("EXPLAIN ") == odd
    assert statement.count("customer") == 1


def test_postgres_一支是四条且次序钉死只读事务包住语句() -> None:
    """§3.1.1 的四条：`BEGIN READ ONLY` 必须在 `EXPLAIN` 之前，`ROLLBACK` 必须在最后。

    `SET LOCAL` 只在这个事务里有效，所以它排在 `BEGIN` 之后、`EXPLAIN` 之前；
    末条 `ROLLBACK` 不是可选项——PG 的 AUTOCOMMIT 连接上不收回这一个事务，
    同一条连接下一次用就会带着一个已完成的只读事务的残留语义。
    """
    statements = explain_statements("postgres", _SQL_FINAL)
    assert len(statements) == 4
    assert statements[0] == "BEGIN READ ONLY"
    assert statements[1] == "SET LOCAL statement_timeout='3000ms'"
    assert statements[2] == f"EXPLAIN {_SQL_FINAL}"
    assert statements[3] == "ROLLBACK"


def test_postgres_那一支今天只有文本_没有任何真库证据() -> None:
    """这一条是给"下一位读测试的人"立的路标，不是给代码立的。

    它断的是一件很小的事（PG 支不碰连接、不抛异常），为的是让上面那条次序断言的来源写得清楚：
    四个值全部来自 §3.1.1 的字面，而 §3.1.1 的字面**从来没有在真 PG 上跑过**。
    行为用例的靶子在 PG 只读执行通路落地那天，属另一件事，不属 P5（工单 037 格子 ①）。
    """
    statements = explain_statements("postgres", _SQL_FINAL)
    assert all(isinstance(s, str) for s in statements)
    assert "EXPLAIN" in statements[2]


@pytest.mark.parametrize("dialect", ["sqlite", "MySQL", "mssql", "oracle"])
def test_没登记过的方言当场报错而不是默默当_mysql(dialect: str) -> None:
    """拼错的方言名不能落到默认分支。

    落到 MySQL 那一支会发出一条 PG 不认的 `EXPLAIN …`——而第二层的存在意义正是"在花钱之前拦住"，
    它自己不能成为新的破口。报错要点名是哪一个值没登记。
    """
    with pytest.raises(ValueError, match=dialect):
        explain_statements(dialect, _SQL_FINAL)


# --- 裁决（safety §3.2）：期望值全部来自真 MySQL 5.7 + 演示库 `ai_web_demo` 的实测读数 ------
# 原始实录：`.scratch/p5-sql-guard-hardening/evidence/probe_explain_null037.txt`
# （13 条"用户真会问"的形状：3 条 `NULL` / 10 条有数）与 `probe_explain_json037.txt`
# （`count(*)` 那一行的 `Extra` 原文）。行形状按 `result.mappings()` 的读法给：
# 每行一个映射，估行在 `rows` 那一列，引擎自述在 `Extra` 那一列。

_ROWS_FULL_SCAN = 30226  # `order_main` 全表扫描的估行（实测 4 条形状都落在这一个数上）
_ROWS_UNION_ARM_2 = 87912  # `order_item` 那一臂的估行


def _row(rows: object, extra: str = "") -> dict[str, object]:
    """一行 `EXPLAIN` 输出。只写裁决读的那两列，其余 10 列不进这一层。"""
    return {"rows": rows, "Extra": extra}


def test_读得出数且不超过阈值就放行() -> None:
    explain_verdict([_row(_ROWS_FULL_SCAN)], max_explain_rows=50_000_000)


def test_尺子取逐行最大值既不是乘积也不是求和() -> None:
    """`UNION` 的两臂是**并列的两次扫描**，不是嵌套循环——实测 `[30226, 87912, None]`。

    阈值取 100000 是为了同时反证掉另外两把尺子：相乘 30226×87912≈2.66e9、相加 118138 都会
    把这条合法 UNION 判成 `too_costly`，取最大值 87912 才判得对。理由与实测都在 §3.2 那张表里。
    """
    explain_verdict(
        [_row(_ROWS_FULL_SCAN), _row(_ROWS_UNION_ARM_2), _row(None)],
        max_explain_rows=100_000,
    )


def test_最大值超阈值时报_too_costly_且正文把两个数都带上() -> None:
    """message 要能回答"该改哪个键、改到多少"——038 那一条要把它原样回喂模型。"""
    with pytest.raises(SqlGuardError) as caught:
        explain_verdict([_row(_ROWS_FULL_SCAN), _row(9)], max_explain_rows=1000)
    assert caught.value.rule_id == GuardRuleId.too_costly
    text = str(caught.value)
    assert "[too_costly]" in text
    assert str(_ROWS_FULL_SCAN) in text and "1000" in text


def test_判据是严格大于_等于阈值放行() -> None:
    """`> AIWEB_QUERY__MAX_EXPLAIN_ROWS`（§3.2 第 1 条原话），不是 `>=`。

    默认阈值 50_000_000 是文档里的数，这里就按它取边界：正好等于放行，多一行拒。
    """
    explain_verdict([_row(50_000_000)], max_explain_rows=50_000_000)
    with pytest.raises(SqlGuardError):
        explain_verdict([_row(50_000_001)], max_explain_rows=50_000_000)


def test_全_null_但每行都自证_optimized_away_时放行() -> None:
    """§3.2 第 2 条的放行那一档，来源是实测那条最要命的形状。

    `SELECT count(*) FROM t` 给 `rows=NULL` 不是"引擎不知道"：它的 `Extra` 是
    `Select tables optimized away`，`FORMAT=JSON` 里连一个 cost/rows 键都没有——
    引擎在**自证它不扫表**。
    按"读不出来一律拒"的字面执行，第二层专门拦的就是"有多少订单"这类最常见的问句，
    而它默认开、prod 锁死不能关。这一格属"证得了它小"。
    """
    explain_verdict(
        [_row(None, "Select tables optimized away")],
        max_explain_rows=50_000_000,
    )
    # 两行都自证时同样放行（"每一行"这个全称量词在这里是真的被满足，不是被绕过）
    explain_verdict(
        [_row(None, "Select tables optimized away"), _row(None, "Select tables optimized away")],
        max_explain_rows=50_000_000,
    )


def test_只有一行自证_optimized_away_另一行读不出来时拒() -> None:
    """钉住 §3.2 那句"**每一行**的 `Extra` 都含 `optimized away`"里的全称量词。

    上一条件里第二行换成一个不自述的形状（`Extra` 为空），裁决就得翻成拒。
    少了这一条，"每一行"就只是行文，代码写成 `any(...)` 也照样全绿。
    """
    with pytest.raises(SqlGuardError) as caught:
        explain_verdict(
            [_row(None, "Select tables optimized away"), _row(None)],
            max_explain_rows=50_000_000,
        )
    assert caught.value.rule_id == GuardRuleId.dry_run_unreadable


def test_一行数都读不出来又没有自证时报_dry_run_unreadable() -> None:
    with pytest.raises(SqlGuardError) as caught:
        explain_verdict([_row(None), _row(None)], max_explain_rows=50_000_000)
    assert caught.value.rule_id == GuardRuleId.dry_run_unreadable


def test_混合形状按有数那些行裁_不替读不出来的那行买单() -> None:
    """§3.2 第 1 条末句。实测形状是派生表 `[1, None]`。

    两档都要断：读得出的那一格没超阈值时**放行**（不能因为另一行是 `NULL` 就拒），
    超了阈值时报的是 `too_costly` 而不是 `dry_run_unreadable`——我们的读法问题不算成用户的代价，
    反过来也不能把一条证明确实太大的 SQL 报成"读不出来"。
    """
    explain_verdict([_row(1), _row(None)], max_explain_rows=50_000_000)
    with pytest.raises(SqlGuardError) as caught:
        explain_verdict([_row(_ROWS_FULL_SCAN), _row(None)], max_explain_rows=1000)
    assert caught.value.rule_id == GuardRuleId.too_costly


def test_结果里根本没有_rows_这一列时算读不出来() -> None:
    """§3.2 第 2 条列的第二种形状。今天 MySQL 不会这样回，但驱动/版本换了就可能。

    认不出这一列就是"读不出来"，不是"估行为 0"——后者会把整层变成一个永不为真的闸。
    """
    with pytest.raises(SqlGuardError) as caught:
        explain_verdict([{"table": "customer", "Extra": ""}], max_explain_rows=50_000_000)
    assert caught.value.rule_id == GuardRuleId.dry_run_unreadable


def test_explain_回零行算读不出来而不是放行() -> None:
    """空结果在这里是"证不了它小"，不是"每一行都自证了"（§1.2 ⑦ 那条既有裁决）。

    **这一条是文档字面的一处补细**：§3.2 第 2 条把"回零行"列进"一行都读不出来"，
    接着那句"**每一行**的 `Extra` 都含 `optimized away` → 放行"在零行上是空洞为真的
    ——照字面实现会变成"引擎一个字没说 → 放行"。这里按动机实现，偏差记在工单 037 交付记录。
    """
    with pytest.raises(SqlGuardError) as caught:
        explain_verdict([], max_explain_rows=50_000_000)
    assert caught.value.rule_id == GuardRuleId.dry_run_unreadable
