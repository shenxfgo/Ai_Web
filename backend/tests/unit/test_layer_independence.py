"""三层独立性的**闸内**那半边（工单 040 涉及层第 1 格；roadmap §P5 验收 3/4 的反证族）。

为什么这一族要住在 `tests/unit/` 而不是 `tests/guard/`：`tests/guard/` 量的是**第一层自己**
（语料 57/19 逐条断言），而本文件量的是**三层之间有没有共享状态**——它必须同时 import
`sql_guard`、`dry_run`、`executor` 三个模块，而 `tests/guard/` 那一条封闭集合判据的扫描根
只认 `tests/guard/test_*.py` ∪ `tests/unit/test_dry_run_*.py`（safety §9.2 的扫描根那行），
把这个文件塞进去只会让那把尺的目录字面再多一次例外。

**本文件一条都不连源库。** 它证的是"层与层之间没有代码耦合"，不是"源库真的拦下了什么"——
后者住 `tests/integration/test_layer_independence_live.py`，两边不许互相冒充（工单 040 验收 3
要求 ②③ 那一对的归因互斥且可区分，那是 live 的事）。

一处实测改写了工单字面（读数见 `test_清空黑名单只换归因不换结论` 那条）：
工单 040 与 safety §8.4 都写着"**清空黑名单** → 被 `MAX_EXECUTION_TIME` 截断"，实测清空
`_SLEEP_FUNCTIONS` 之后 `sleep()` **照样被第一层拒**，只是归因从 `sleep_function` 换成
`danger_function`——因为 `_function_violations` 的 else 分支是"拒绝一切 `exp.Anonymous`"，
那才是正向白名单那把刀，三个具名集合只负责把归因拆细。

期望值口径：第三层那四条 SET 的字面与顺序抄 `docs/nl2sql-safety.md` §4.1 的
as-built(P2-011)（`READ ONLY` → `MAX_EXECUTION_TIME` → `wait_timeout` → `autocommit=1`），
第二层那一条抄 §3.1（`EXPLAIN <sql_final>`），第一层那个 `LIMIT 1001` 抄 §1.2 ⑦
（缺 LIMIT 一律钳成 `max_rows+1`）——都不是"实现现在怎么写就怎么断"。
"""

from __future__ import annotations

import pytest

from app.services import sql_guard
from app.services.nl2sql import executor
from app.services.nl2sql.dry_run import explain_statements, explain_verdict
from app.services.nl2sql.executor import session_statements, split_truncated
from app.services.sql_guard import (
    GuardRuleId,
    QualifiedTable,
    SqlGuardError,
    guard,
)

DEMO_DB = "ai_web_demo"
ALLOWED = frozenset(
    {
        QualifiedTable("", DEMO_DB, "customer"),
        QualifiedTable("", DEMO_DB, "order_item"),
    }
)

# 一条过得了第一层的普通查询（无 LIMIT，正是要看钳位把 +1 加在哪）
_LEGAL = f"SELECT id FROM {DEMO_DB}.customer WHERE status = 'active'"
# 本节那对"同一条 SQL 的两种归宿"里的主角
_SLEEP = "SELECT SLEEP(20)"

# §4.1 的字面顺序；timeout_ms=1000 时 wait_timeout 那一格按 §4.1 的 `timeout_ms/1000 + 10` 得 11
_SESSION_1000 = [
    "SET SESSION TRANSACTION READ ONLY",
    "SET SESSION MAX_EXECUTION_TIME = 1000",
    "SET SESSION wait_timeout = 11",
    "SET SESSION autocommit = 1",
]


def as_str(out: sql_guard.GuardResult) -> str:
    """把 `sql_final` 收窄成 str（`Optional` 在这一族里到处要拆，写在助手一处而不是每条用例）。"""
    assert out.sql_final is not None
    return out.sql_final


def test_第一层拒掉的那条_sql_喂给第二层仍然产出同一条_explain_文本() -> None:
    """第二层不吃第一层的裁决：它只吃 `sql_final` 这一个字符串。

    这一格看着轻，但它挡的是一类真实的重构：把第二层做成"`guard()` 的返回值里带一个
    dry-run 标志"、或者让 `explain_statements` 去看 violations——那时"第二层能不能单独关掉"
    （safety §3.3，验收 4 的立论）就不成立了，因为它的输入里已经有了第一层的结论。
    所以断的是**同一件文本**在两路里逐字相同：被第一层拒的那条（`SELECT SLEEP(20)`，
    此刻 `sql_final is None`）手动钳好之后喂进来，与放行那条喂进来，第二层给的都只是
    `EXPLAIN <那一条>`。
    """
    rejected = guard(_SLEEP, allowed=ALLOWED, default_schema=DEMO_DB)
    assert rejected.ok is False and rejected.sql_final is None
    # 拒了以后由调用方决定喂什么——这里喂"假如第一层放行会得到的那条"
    as_if_passed = f"{_SLEEP} LIMIT 1001"
    assert explain_statements("mysql", as_if_passed) == (f"EXPLAIN {as_if_passed}",)
    passed = guard(_LEGAL, allowed=ALLOWED, default_schema=DEMO_DB)
    assert explain_statements("mysql", as_str(passed)) == (f"EXPLAIN {as_str(passed)}",)


def test_第三层的四条_set_与输入无关_只认_kind_与_timeout_ms() -> None:
    """第三层的字面钉死（safety §4.1 as-built(P2-011) 那四条、那个顺序、那三处单位换算）。

    这是工单 040 验收 1 那把变异刀口所在的用例：把 `session_statements` 里
    `MAX_EXECUTION_TIME` 那一行删掉，本条必须红。单位换算也一起钉着——`wait_timeout`
    是**秒**（`timeout_ms // 1000 + 10`），写成毫秒就是一条会先踢掉会话的定时炸弹。
    """
    assert session_statements("mysql", timeout_ms=1000) == _SESSION_1000
    # 同一个 timeout_ms 换到 PG 那一支：两条、单位是带引号的毫秒字面量（§4.2）
    assert session_statements("postgres", timeout_ms=1000) == [
        "SET default_transaction_read_only = on",
        "SET statement_timeout = '1000ms'",
    ]


def test_清空黑名单只换归因不换结论_正向白名单才是那把刀() -> None:
    """实测否掉工单 040 与 safety §8.4 那句"清空黑名单 → 语句走到源库"的前提。

    `_SLEEP_FUNCTIONS` 里那个 `"sleep"` **不是**挡它的刀——`_function_violations` 的 else 分支
    拒绝一切 `exp.Anonymous`（sqlglot 认不出的函数全落成 Anonymous），具名集合只负责把**归因**
    拆成 `sleep_function` / `pg_file_access` / `danger_function` 三档（safety §1.1 那句
    "正向白名单"的真正含义）。所以清空之后：结论仍是拒，码换成 `danger_function`。

    为什么这一格值得永久钉着：将来谁把"清空黑名单"当成"第二/三层独立"的手段去写用例或
    写文档，就会得到一条**根本走不到源库**的所谓反证——而它看着是绿的。
    """
    first = guard(_SLEEP, allowed=ALLOWED, default_schema=DEMO_DB)
    assert first.ok is False
    assert first.violations[0].code is GuardRuleId.sleep_function

    monkey = frozenset()
    original = sql_guard._SLEEP_FUNCTIONS
    sql_guard._SLEEP_FUNCTIONS = monkey
    try:
        second = guard(_SLEEP, allowed=ALLOWED, default_schema=DEMO_DB)
        assert second.ok is False, "清空 _SLEEP_FUNCTIONS 就放行 = 正向白名单那把刀被摘了"
        # 归因换档这一格就是下面这一条：`danger_function` 与 `sleep_function` 是两枚不同的成员，
        # 断到具体成员就把"没换成别的码"一起钉住了，不必再补一条 `is not sleep_function`
        assert second.violations[0].code is GuardRuleId.danger_function
    finally:
        sql_guard._SLEEP_FUNCTIONS = original
    # 恢复之后立刻复红：证明上面那次"换了归因"是 monkeypatch 造成的，不是文件本来就那样
    assert guard(_SLEEP, allowed=ALLOWED, default_schema=DEMO_DB).violations[0].code is (
        GuardRuleId.sleep_function
    )


def test_拆掉第一层的整条函数规则之后_第三层的四条一个字不变() -> None:
    """§8.4 ② 后半那句反证的**闸内形态**：第一层整个不在场时，第三层照原样。

    与上一条的分工要说清：上一条证的是"清黑名单只换归因"，本条把整条 `_function_violations`
    停掉（这才真的让 `SLEEP` 出得了第一层），断的是**第三层那四条与顺序一字不变**——
    这正是工单 040 涉及层第 1 格点名的那条"清黑名单后 L3 的语句序列一个字不变"。
    真连库那一发在 live 文件里，本条不冒充它。

    顺带钉一格"钳位不在函数规则里"：停掉函数规则之后 `sql_final` 仍然是 `... LIMIT 1001`，
    钳位（`guard()` 的 LIMIT 分支）与函数黑名单是两把各自独立的刀。
    """
    before = session_statements("mysql", timeout_ms=1000)
    original = sql_guard._function_violations
    # 这一族两套桩机制并存是有意的：`monkeypatch.setattr` 能自动恢复，但它盖不住
    # "同一个函数被第一层内部两处引用"的那种桩（本条停的 `_function_violations` 是模块内直调，
    # setattr 够用，可那条"清空黑名单"的用例要在**同一个函数体内**看到前/后两个结论，
    # 自动恢复会盖住中间那次复红）。手工 try/finally 那一条在 finally 里立刻再断一次原值，
    # 那次复红就是"桩真的生效过"的见证物。仓内私有属性桩的先例是 monkeypatch 那一套
    # （`test_join_graph.py:401`、`test_sync_live.py:96`），偏离它的理由写在上面这一句里。
    sql_guard._function_violations = lambda stmt: []
    try:
        out = guard(_SLEEP, allowed=ALLOWED, default_schema=DEMO_DB, max_rows=1000)
        assert out.ok is True
        assert out.sql_final == f"{_SLEEP} LIMIT 1001"
        assert session_statements("mysql", timeout_ms=1000) == before
        assert session_statements("mysql", timeout_ms=1000) == _SESSION_1000
    finally:
        sql_guard._function_violations = original
    assert guard(_SLEEP, allowed=ALLOWED, default_schema=DEMO_DB).ok is False


def test_第二层拒了太贵之后_第三层的会话与第一层的判定都不换() -> None:
    """第二层的结论不许漏进另外两层（§3.3 那句"关掉它防御仍成立"的另一面）。

    `explain_verdict` 抬 `too_costly` 时吃的是 `EXPLAIN` 读回来的行，它既不动 `guard()` 的产物、
    也不动 `session_statements` 的字面。这一格今天必然绿——它防的是将来把裁决做成
    "在 `session_statements` 里根据上一档结论省掉某条 SET"那种"顺手优化"。
    """
    expensive = [{"rows": 10_000, "Extra": ""}]
    with pytest.raises(SqlGuardError) as caught:
        explain_verdict(expensive, max_explain_rows=100)
    assert caught.value.rule_id is GuardRuleId.too_costly
    assert session_statements("mysql", timeout_ms=1000) == _SESSION_1000
    assert guard(_LEGAL, allowed=ALLOWED, default_schema=DEMO_DB, max_rows=1000).sql_final == (
        f"{_LEGAL} LIMIT 1001"
    )


def test_拆掉第三层那条超时_set_之后第一层与第二层的产物一个字不变(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反方向的独立性：第三层被拆（删掉 `MAX_EXECUTION_TIME` 那一行），上面两层无感。

    与上一条配成一对——**独立性是双向的**，只断一个方向的话，"第三层的字面里藏着对
    第一层的补偿"（比如"守卫放行的函数就由会话兜住"这类看似合理的写法）就抓不住。
    这里替掉的是**用例内的桩**，`executor.py` 主分支一个字没动（§8.3 同族纪律）。
    """
    legal = guard(_LEGAL, allowed=ALLOWED, default_schema=DEMO_DB)
    before_text = explain_statements("mysql", as_str(legal))
    real = executor.session_statements

    def _without_timeout(kind: str, *, timeout_ms: int) -> list[str]:
        return [
            line for line in real(kind, timeout_ms=timeout_ms) if "MAX_EXECUTION_TIME" not in line
        ]

    # 这里断的是**桩的输入契约**（真函数那一侧确实有那条超时 SET），不是桩自己的产物——
    # "把桩的输出逐字写出来再断言"是同义反复，对"删掉那一行"这个变异永远绿。
    # 它同时是本条的前提：真函数里本来就没那一行的话，"拆了它而上层无感"就是一句空话，
    # 红在这里比绿过去诚实（工单 040 验收 1 那把变异刀口会红在本条与 `test_第三层的四条_set_…`）。
    assert real("mysql", timeout_ms=1000) == _SESSION_1000

    monkeypatch.setattr(executor, "session_statements", _without_timeout)
    assert guard(_LEGAL, allowed=ALLOWED, default_schema=DEMO_DB).sql_final == as_str(legal)
    assert explain_statements("mysql", as_str(legal)) == before_text


def test_钳位在第一层_丢探针行与_truncated_判定在第三层() -> None:
    """验收 3 那一发的**闸内对应物**：那个 `+1` 是两个主人之间唯一的契约。

    第一层（`guard()`）负责把顶层 LIMIT 钳成 `max_rows + 1`，第三层（`split_truncated`）
    负责"取到 `row_limit+1` 行就判还有更多、并把探针行丢掉"。两处必须同源：
    - 传进 `row_limit=1000` 而守卫按 `max_rows=1000` 钳位 → 拿到 1001 行 → 1000 行 + `True`；
    - 上限没碰到（正好 1000 行）→ 原样 1000 行 + `False`，探针根本不存在；
    - 把两处的数错开（守卫钳 1001、执行器按 `row_limit=1001` 截）→ **探针行被当成真数据**
      且 `truncated` 恒假——那正是 §9.2 ⑧ 闭掉的那个洞的形状，这里反向钉它一次。

    名字里的两半各自有断言，不许只断一半：`max_rows=1000` **显式传**进去（不跟着
    `guard()` 的默认值走，理由同 `test_dry_run_mysql_live.py:40` 那条"默认值改了要红在
    本用例而不是红在一片"），钳出来的文本就是下面 `split_truncated` 吃进去的那个 1001。
    """
    clamped = guard(
        f"SELECT id FROM {DEMO_DB}.order_item",
        allowed=ALLOWED,
        default_schema=DEMO_DB,
        max_rows=1000,
    )
    assert as_str(clamped).endswith(" LIMIT 1001"), as_str(clamped)

    rows_1001 = [{"id": i} for i in range(1001)]
    kept, truncated = split_truncated(rows_1001, row_limit=1000)
    assert truncated is True
    assert len(kept) == 1000
    assert kept[-1] == {"id": 999}, "丢的必须是探针行（最后一行），不是把真数据切掉一格"

    exact, truncated_exact = split_truncated([{"id": i} for i in range(1000)], row_limit=1000)
    assert truncated_exact is False and len(exact) == 1000

    # 错位那一发：执行器多问了一格，探针行就冒充成第 1001 行真数据、且没人报截断
    misaligned, truncated_mis = split_truncated(rows_1001, row_limit=1001)
    assert truncated_mis is False and len(misaligned) == 1001


def test_三层的判定共用同一枚词表但互不覆盖成员() -> None:
    """`GuardRuleId` 是三层共用的词表（037 之后 22 枚），共用**名字**不等于共用**判定**。

    断三件事：第一层的 `sleep_function` 与第二层的 `too_costly` 是两个成员；
    两路抬的都是同一个 `SqlGuardError` 类（所以管线的 `guard_result` 与早退形状不用另写一套，
    §9.2 那条依赖方向 `dry_run.py → sql_guard.py` 说的就是这个）；
    而 `sleep_function` 绝不从第二层出来——第二层看不到函数，只看到估行。
    """
    assert GuardRuleId.sleep_function is not GuardRuleId.too_costly
    with pytest.raises(SqlGuardError) as from_first:
        sql_guard.check(_SLEEP, allowed=ALLOWED, default_schema=DEMO_DB)
    assert from_first.value.rule_id is GuardRuleId.sleep_function
    with pytest.raises(SqlGuardError) as from_second:
        explain_verdict([{"rows": 999_999, "Extra": ""}], max_explain_rows=100)
    assert from_second.value.rule_id is GuardRuleId.too_costly


@pytest.mark.parametrize(
    "timeout_ms",
    [1, 999, 61_000],
)
def test_timeout_ms_的三处换算在取整边界与超上限档位上都对(timeout_ms: int) -> None:
    """一条 `timeout_ms` 进、三处不同单位出（§4.1 的单位口径最容易写错的那三格）。

    `MAX_EXECUTION_TIME` 与 PG 的 `statement_timeout` 是**毫秒**，MySQL 的 `wait_timeout` 是**秒**
    且要留 10 秒余量（会话不能比语句先被踢）。选 1/999 这两档是为了逼出取整边界：
    `1 // 1000 + 10 = 10`、`999 // 1000 + 10 = 10`——写成 `round()` 就会把 999 抬成 11；
    61_000 是"超过一分钟"那一档，钉的是没有隐藏的分钟级上限。
    1000 与 15000 两档**不在这里重复**：前者由 `test_第三层的四条_set_…` 逐字四行断，
    后者由 `tests/unit/test_executor_session.py::test_mysql_顺序_with_值` 断，
    同一组数字在三处各写一遍就是维护成本（本文件只补那两条都没走到的边界）。
    期望值按 §4.1 那条公式手算，不是把实现再跑一遍。
    """
    lines = session_statements("mysql", timeout_ms=timeout_ms)
    assert lines[1] == f"SET SESSION MAX_EXECUTION_TIME = {timeout_ms}"
    assert lines[2] == f"SET SESSION wait_timeout = {timeout_ms // 1000 + 10}"
    assert session_statements("postgres", timeout_ms=timeout_ms)[1] == (
        f"SET statement_timeout = '{timeout_ms}ms'"
    )
