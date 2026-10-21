"""`retry_with_error`：第二层（dry-run）拒了一句 SQL 之后回喂模型，**最多一次**（工单 038）。

接缝口径沿用 `test_pipeline_orchestration.py` 那一族（verification §2.1 的 `pipeline` 行）：
LLM 走 respx、检索走 `Retriever` Protocol 的桩、executor 走 monkeypatch 的 spy。
断"叫了几次"的尺子是 `respx.Route.call_count` 与 `route.calls`，
不是 `side_effect` 计数（工单 038 开工实录里那条认错）。

为什么这里要换一个 executor 桩：本片的主角是**第二层抬上来的那枚 `SqlGuardError`**，
既有那个 `Spies.execute_readonly` 永远成功，造不出"坏 SQL"。而新桩按 **SQL 内容**裁决
（含 `nope` 就拒），不按第几次调用裁决——`EXPLAIN` 本来就是语句的函数，桩要是按次序回
"第一次坏第二次好"，那"重试了一次"就成了桩自己编的事实，而不是 pipeline 的事实。

为什么分流按"回喂段在不在 `messages` 里"：既有 `_draft_route` 按 `messages[0]` 的 system 模板分流，
而两次生成的 system 是**同一份**（safety §3.5 那条"卡片、表与硬约束一概不变"），复用它就是
"两次都坏"。次序仍不由桩代劳——叫了几次是独立断言的那一根。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError

from app.models.chat import ChatMessage
from app.services.llm_client import LlmClient
from app.services.nl2sql import executor, pipeline
from app.services.sql_guard import GuardRuleId, SqlGuardError
from tests.integration.test_pipeline_orchestration import (
    READY_LLM,
    RESULT_ROWS,
    FakeRetriever,
    Spies,
    _found,
    _seed_user_and_source,
)

pytestmark = pytest.mark.pg

#: 模型第一版草稿里那枚拼错的列名。守卫第一层**不认识列**（22 枚 `GuardRuleId` 里没有列名那一档），
#: 所以这一句能过第一层、活到第二层——这正是 038 的靶："可自纠"的那一类在 `EXPLAIN` 当场就被拒。
BAD_DRAFT_SQL = "SELECT nope FROM ai_web_demo.order_main GROUP BY dt"
GOOD_DRAFT_SQL = "SELECT dt, SUM(amount) AS 金额 FROM ai_web_demo.order_main GROUP BY dt"
#: 白名单外的一张表：第一层就死，属本片**不许进**的那一支（工单 038 验收 2）。
LAYER1_DRAFT_SQL = "SELECT id FROM ai_web_demo.secret_table"

#: 逐字抄自 `dry_run.py:138` 那一支的字面（`f"源库在执行 EXPLAIN 时就拒了这条 SQL：{detail}"`），
#: detail 用真实 MySQL 1054 的原话。桩不编措辞：回喂段要"逐字"含它，措辞是桩编的就等于自证。
ENGINE_MESSAGE = "源库在执行 EXPLAIN 时就拒了这条 SQL：1054: Unknown column 'nope' in 'field list'"
ENGINE_SENTENCE = f"[dry_run_engine_rejected] {ENGINE_MESSAGE}"

CONCLUSION_TEXT = "全年共 3 个月有订单，合计 300.5 万元。"

#: 第二次"回得更烂"那一档的正文：`max_tokens` 截断的形状（大括号不闭合），`parse_llm_draft` 认它。
TRUNCATED_SECOND_BODY = '{"sql": "SELECT dt FROM ai_web_demo.order_main GROUP BY dt LIMIT 100 LI'


def _chat(content: str) -> dict[str, Any]:
    return {
        "id": "cmpl-1",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
    }


def _draft_body(sql: str) -> str:
    return json.dumps({"sql": sql, "explanation": "按月汇总", "clarify": ""}, ensure_ascii=False)


def _retry_route(
    *, first: str, second: str | None = None, second_raw: str | None = None
) -> respx.Route:
    """假端点：生成请求里**带着回喂段**的才回第二次那一份，否则回 `first`；非生成请求回结论。

    分流键是"错误原话在不在 `messages` 里"——那正是 safety §3.5 钉的"回喂的正文逐字就是我们
    本来要说给用户的那一句"。pipeline 没把那句塞进去的话，这里永远回 `first`，"坏→好"那一档
    自然红，而不是靠桩配合。不需要重试的档里 `second` 故意填**坏** SQL：真多发了一次就当场可见。

    `second_raw` 是给"第二次回得更烂"那一档的口子：拿到的就是模型正文本身（可以是一截断 JSON），
    绕开 `_draft_body` 那层组装。两者必须只给一个——桩"永远有个答案"的话，测出来的早退是桩造的。
    """
    if (second is None) == (second_raw is None):
        raise ValueError("_retry_route 的第二次那一半只能给 second 或 second_raw 之一")

    def _reply(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        messages = body["messages"]
        if "SQL 生成器" not in messages[0]["content"]:
            return httpx.Response(200, json=_chat(CONCLUSION_TEXT))
        if any(ENGINE_SENTENCE in m["content"] for m in messages[1:]):
            content = second_raw if second_raw is not None else _draft_body(str(second))
            return httpx.Response(200, json=_chat(content))
        return httpx.Response(200, json=_chat(_draft_body(first)))

    return respx.post("https://llm.test/v1/chat/completions").mock(side_effect=_reply)


def _generate_calls(route: respx.Route) -> list[list[dict[str, Any]]]:
    """这次跑到底发了几次**生成**请求（工单字面"`llm.complete` 恰好 2 次"里那两次不含结论）。"""
    out: list[list[dict[str, Any]]] = []
    for call in route.calls:
        messages = json.loads(call.request.content)["messages"]
        if "SQL 生成器" in messages[0]["content"]:
            out.append(messages)
    return out


class Layer2Spies:
    """第二层的桩：按 SQL 内容抬 `SqlGuardError`，并记下它**看见的每一次**往返。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute_readonly(self, row: Any, **kwargs: Any) -> Any:
        sql_final = kwargs["sql_final"]
        self.calls.append(sql_final)
        if "nope" in sql_final:
            raise SqlGuardError(GuardRuleId.dry_run_engine_rejected, ENGINE_MESSAGE, sql_final[:80])
        return executor.ExecutionResult(
            columns=["dt", "金额"],
            rows=[list(r) for r in RESULT_ROWS],
            truncated=False,
            row_count=len(RESULT_ROWS),
            run_id="20261021-rt1234",
            result_file="data/results/r.csv",
        )


@pytest.fixture
def layer2(monkeypatch: pytest.MonkeyPatch) -> Layer2Spies:
    """两处外桩：卡片走 `Spies.load_cards`（与那一族同一个桩），executor 换成会拒第二层那一枚。

    不复用 `spies` 那一颗 fixture：它把 `execute_readonly` 打成"永远成功"，本片要的恰是
    "会按内容拒"——两处各打各的，才有桩真正看见第二次往返。
    """
    cards = Spies()
    monkeypatch.setattr(pipeline, "load_schema_tables", cards.load_cards)
    out = Layer2Spies()
    monkeypatch.setattr(pipeline, "execute_readonly", out.execute_readonly)
    return out


async def _ask(session: Any, user: Any, ds: Any) -> Any:
    return await pipeline.ask(
        session,
        actor=user,
        question="2024 年每个月的订单总金额是多少",
        datasource_ids=[ds.id],
        retriever=FakeRetriever([_found(ds.id, ds.id * 10 + 1)]),
        llm=LlmClient(llm=READY_LLM, http=httpx.AsyncClient()),
    )


@respx.mock
async def test_第二次生成时回喂段带着那句源库原话(session_factory, layer2: Layer2Spies) -> None:
    """钉 pipeline 与 `build_retry_messages` 之间那道接线：第二次请求体**真的**带着回喂段。

    纯函数那边已经测过"回喂段含 SQL 与错误原话"（`tests/unit/test_prompt_retry.py`），编排这里
    如果不调它，那边全绿也照样等于零实现。
    """
    route = _retry_route(first=BAD_DRAFT_SQL, second=GOOD_DRAFT_SQL)
    async with session_factory() as session:
        user, ds = await _seed_user_and_source(session)
        await _ask(session, user, ds)

    gens = _generate_calls(route)
    assert len(gens) == 2, f"生成请求应恰好两次，实测 {len(gens)}"
    first, second = gens
    # ① 原 messages 一根不少：两次请求的 system 是同一份，第二次只在尾部追加
    assert second[: len(first)] == first
    assert len(second) == len(first) + 2
    # ② 追加的两段是"上一轮原文"与"回喂段"，次序不能倒（倒过来模型看不见自己写过什么）
    assert second[-2] == {"role": "assistant", "content": _draft_body(BAD_DRAFT_SQL)}
    assert second[-1]["role"] == "user"
    fed_back = second[-1]["content"]
    # ③ 逐字含第一层重生成后、本来要端给源库的那句 SQL，与那句源库原话
    assert layer2.calls[0] in fed_back
    assert ENGINE_SENTENCE in fed_back


@respx.mock
async def test_第二次成功时执行的是重生成的sql且库里两次都在(
    session_factory, layer2: Layer2Spies
) -> None:
    """工单 038 验收 3：一次"坏→好"跑完，从**那一行** `chat_messages` 能读出四件事。

    查库不查日志（验收原话），也不只查 `AskOutcome`——返回值对了但库里没留下，运维看到的
    还是"这轮为什么慢了一倍"的空格。
    """
    route = _retry_route(first=BAD_DRAFT_SQL, second=GOOD_DRAFT_SQL)
    async with session_factory() as session:
        user, ds = await _seed_user_and_source(session)
        outcome = await _ask(session, user, ds)
        stored = await session.get(ChatMessage, outcome.message_id)

        assert stored is not None
        assert outcome.executed is True and stored.executed is True  # ④ 终局成功
        assert len(_generate_calls(route)) == 2
        # 第二层的拒只发生一次：第二次那一句真的又走了一遍 EXPLAIN（`calls` 是桩看见的往返）
        assert len(layer2.calls) == 2 and "nope" not in layer2.calls[1]
        assert outcome.conclusion == CONCLUSION_TEXT
        assert stored.error_code is None and stored.error_message is None

        guard_result = stored.guard_result
        assert guard_result is not None
        assert guard_result["ok"] is True and guard_result["violations"] == []
        retries = guard_result["retries"]
        # ① 第一次的 SQL + ② 第一次的错误原文
        assert len(retries) == 1
        assert retries[0]["seq"] == 1
        assert retries[0]["sql_final"] == layer2.calls[0]  # 记的是**发给源库**的那一句
        assert "nope" in retries[0]["sql_final"]
        assert retries[0]["rule_id"] == "dry_run_engine_rejected"
        assert retries[0]["message"] == ENGINE_MESSAGE  # 源库原话，没被改写成通用话术
        # ③ 第二次的 SQL：`sql_final` 那一列只装得住最后一次（§2.7 第 ⑦ 条），所以它必须不是第一句
        assert stored.sql_final == outcome.sql_final == layer2.calls[1]
        assert stored.sql_raw == GOOD_DRAFT_SQL  # 模型原文同样被第二次覆盖——重试不靠这两列


@respx.mock
async def test_两次都坏时恰好两次生成且第三次绝不发(
    session_factory, layer2: Layer2Spies, results_dir: Any
) -> None:
    """封顶那一格：断的是**次数 == 2**，不是"最终失败"。

    只断失败的话，重试三次、四次照样全绿——而"最多 1 次"是 roadmap 要点 (c) 的原话。
    结论那一次也不许发（这一轮没有答案可总结）。
    """
    route = _retry_route(first=BAD_DRAFT_SQL, second=BAD_DRAFT_SQL)
    async with session_factory() as session:
        user, ds = await _seed_user_and_source(session)
        outcome = await _ask(session, user, ds)
        stored = await session.get(ChatMessage, outcome.message_id)

        assert len(_generate_calls(route)) == 2
        assert route.call_count == 2  # 一次结论都不发
        assert len(layer2.calls) == 2
        assert outcome.executed is False and stored is not None and stored.executed is False
        assert outcome.error_code == "sql_guard_rejected"
        assert outcome.error_message == ENGINE_SENTENCE
        assert stored is not None
        assert stored.guard_result is not None
        # 终局那一格装的是**最后一次的裁决**，被丢弃的那次住在同一块的 retries 键里
        assert stored.guard_result["ok"] is False
        assert [v["code"] for v in stored.guard_result["violations"]] == ["dry_run_engine_rejected"]
        retries = stored.guard_result["retries"]
        assert [r["seq"] for r in retries] == [1]  # 只有被丢弃的那一次，不含最后一次
    assert not results_dir.exists() or list(results_dir.glob("*")) == []


@respx.mock
async def test_第一次就好时一次都不多发且retries键不出现(
    session_factory, layer2: Layer2Spies
) -> None:
    """验收 5 后半"重试路径不跑时必须零开销"——这一条同时护住既有断言。

    `retries` 是**缺席**而不是 `[]`：`test_pipeline_orchestration.py:288` 那条
    `guard_result == {"ok": True, "violations": []}` 是全等断言，多一个键就红（§2.7 第 ⑦.2 条）。
    """
    route = _retry_route(first=GOOD_DRAFT_SQL, second=BAD_DRAFT_SQL)
    async with session_factory() as session:
        user, ds = await _seed_user_and_source(session)
        outcome = await _ask(session, user, ds)
        stored = await session.get(ChatMessage, outcome.message_id)

        assert len(_generate_calls(route)) == 1
        assert route.call_count == 2  # 生成 + 结论，与 038 之前一字不差
        assert len(layer2.calls) == 1
        assert outcome.guard_result == {"ok": True, "violations": []}
        assert stored is not None and stored.guard_result == {"ok": True, "violations": []}
        assert [name for name, _ in outcome.steps] == [
            "retrieve",
            "join_graph",
            "schema",
            "prompt",
            "generate",
            "guard",
            "execute",
            "chart",
            "conclude",
        ]


@respx.mock
async def test_第一层拒绝绝不回喂(session_factory, layer2: Layer2Spies) -> None:
    """验收 2：一条 `table_not_allowed` 的 SQL 走完，生成请求必须**恰好 1 次**。

    这一条是本片的边界证据（工单原话："少它就是越界"）。越权表、多语句、DML 重试一百次还是错的，
    更硬的一条理由是：回喂它们等于把系统的白名单告诉模型再骗它绕一次（safety §3.5）。
    """
    route = _retry_route(first=LAYER1_DRAFT_SQL, second=GOOD_DRAFT_SQL)
    async with session_factory() as session:
        user, ds = await _seed_user_and_source(session)
        outcome = await _ask(session, user, ds)
        stored = await session.get(ChatMessage, outcome.message_id)

        assert route.call_count == 1  # 连结论都不发，更没有第二次生成
        assert len(layer2.calls) == 0  # 第一层就死了，第二层压根没被问到
        assert outcome.error_code == "sql_guard_rejected"
        assert stored is not None and stored.guard_result is not None
        assert [v["code"] for v in stored.guard_result["violations"]] == ["table_not_allowed"]
        assert "retries" not in stored.guard_result
        # 步梯停在一个 `guard`，而不是 `guard, dry_run, ...`：卡在哪一步必须是事实
        assert [name for name, _ in outcome.steps][-1] == "guard"


@respx.mock
async def test_第二次那一句也现算裁决且封顶跨层仍然只两趟(
    session_factory, layer2: Layer2Spies
) -> None:
    """回喂后再来的一条**过不了第一层**：这一条是"绝不复用第一次中间物"的证据。

    编排要是把上一轮的 `GuardResult`（或那句放行后的 `sql_final`）带进第二轮，这里就会
    看到 `layer2.calls` 变成 2——因为复用的那条 SQL 是好的。实测它只有 1 次：第二次的草稿是
    **现过守卫**的，而守卫拒了它，于是这一轮停在第一层。封顶那一格也在这里生效：
    跨层的失败加起来也只有两趟。
    """
    route = _retry_route(first=BAD_DRAFT_SQL, second=LAYER1_DRAFT_SQL)
    async with session_factory() as session:
        user, ds = await _seed_user_and_source(session)
        outcome = await _ask(session, user, ds)
        stored = await session.get(ChatMessage, outcome.message_id)

        assert len(_generate_calls(route)) == 2
        assert len(layer2.calls) == 1  # 第二次那句连源库都没见到
        assert route.call_count == 2
        assert outcome.error_code == "sql_guard_rejected"
        assert stored is not None and stored.guard_result is not None
        assert stored.guard_result["ok"] is False
        assert [v["code"] for v in stored.guard_result["violations"]] == ["table_not_allowed"]
        # 被丢弃的第一次仍在这一格里：终局那条裁决是第二次的，历史不该被覆盖掉
        assert [r["seq"] for r in stored.guard_result["retries"]] == [1]
        assert [name for name, _ in outcome.steps][-2:] == ["generate", "guard"]


@respx.mock
async def test_第二次回得更烂时不再回喂但重试历史仍在(session_factory, layer2: Layer2Spies) -> None:
    """模型第二次连 JSON 都没闭合：走 `llm_bad_response` 那一档，**再来第三趟没有**。

    这一条钉两件事：① 回喂那一路的入口只有"第二层拒"那一格，畸形返回不在里面（safety §3.5）；
    ② `retries` 键的口径是"这轮有没有白烧过一趟"，所以它对**所有**后续出口都成立——不然这一行
    读起来就像"模型只回了一次，还回坏了"，而真实成本是两次 LLM 加一次 EXPLAIN。
    """
    route = _retry_route(first=BAD_DRAFT_SQL, second_raw=TRUNCATED_SECOND_BODY)
    async with session_factory() as session:
        user, ds = await _seed_user_and_source(session)
        outcome = await _ask(session, user, ds)
        stored = await session.get(ChatMessage, outcome.message_id)

        assert len(_generate_calls(route)) == 2  # 第三趟没有
        assert len(layer2.calls) == 1
        assert outcome.error_code == "llm_bad_response"
        assert stored is not None and stored.guard_result is not None
        # 终局那一格记的是**第一层**的裁决（第二次的草稿连守卫都没到），重试历史另开一键
        assert stored.guard_result["ok"] is True and stored.guard_result["violations"] == []
        assert [r["seq"] for r in stored.guard_result["retries"]] == [1]


@respx.mock
async def test_第二层关掉时坏sql不会触发回喂(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """与 037 那颗开关正交（工单涉及层"四档"的最后一档）。

    实物链条：`executor.py:332` 的 `if query.dry_run:` 关掉时**一条 EXPLAIN 都不发**，那句 1054
    于是从 `conn.execute` 那里以 `SQLAlchemyError` 抬出来，走 `classify_source_error` 的
    "非超时绝不吞、原样上抛"（011 那一档）。它不是 `SqlGuardError`，所以本片这一路**不该**接住它。
    这里用同一枚异常形状（真 1054 的 `ProgrammingError`）替"开关关掉"那一档说话——
    开关本身那一半归 `tests/unit/test_dry_run_wiring.py`。
    """

    async def source_rejects(row: Any, **kwargs: Any) -> Any:
        raise ProgrammingError("SELECT nope", {}, Exception((1054, "Unknown column 'nope'")))

    monkeypatch.setattr(pipeline, "load_schema_tables", Spies().load_cards)
    monkeypatch.setattr(pipeline, "execute_readonly", source_rejects)
    route = _retry_route(first=BAD_DRAFT_SQL, second=GOOD_DRAFT_SQL)
    async with session_factory() as session:
        user, ds = await _seed_user_and_source(session)
        with pytest.raises(ProgrammingError):
            await _ask(session, user, ds)
        rows = (await session.execute(select(ChatMessage))).scalars().all()

        assert len(_generate_calls(route)) == 1  # 没有第二次：触发器只有第二层那一枚异常
        # 真故障照旧上抛（011 的"不上抛"语义没被本片改成"早退 + 回喂"），但留痕那一行仍在
        assert len(rows) == 1
        assert rows[0].guard_result == {"ok": True, "violations": []}


@respx.mock
async def test_重试那一次在步梯上有自己的三段毫秒数(
    session_factory, layer2: Layer2Spies, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验收 4：第二次要有自己的 `generate`/`guard`/`dry_run` 段，毫秒数来自钉死的桩时钟。

    为什么不打全局 `time.perf_counter`：那颗时钟是进程共用的，httpx/respx 也在读它，
    钉死会把网络层耗时一起改写成假数字。这里换掉的是 `_Stopwatch` 这一颗（pipeline 唯一的
    计时入口），它按 lap 的次序发已知值——名字与值配错、少一次 lap，都会在这条全等上红。

    as-built 的形状：`dry_run` 那一格记的是**第一次**被丢弃的往返成本，不是第二次成功的那一次
    （成功时 dry-run 住在 `execute` 里面，与 037 之前同一个形状）。不记它，那笔钱就会被算到
    第二次 `generate` 头上，"重试救回来了"在耗时面上就变成"第二次生成慢了一截"。
    """
    ticks = [11, 22, 33, 44, 55, 66, 77, 88, 99, 111, 222, 333, 444, 555, 666, 777]

    class _PinnedWatch:
        def __init__(self) -> None:
            self.steps: list[tuple[str, int]] = []

        def lap(self, name: str) -> None:
            self.steps.append((name, ticks[len(self.steps)]))

        @property
        def last_ms(self) -> int:
            return self.steps[-1][1] if self.steps else 0

    monkeypatch.setattr(pipeline, "_Stopwatch", _PinnedWatch)
    _retry_route(first=BAD_DRAFT_SQL, second=GOOD_DRAFT_SQL)
    async with session_factory() as session:
        user, ds = await _seed_user_and_source(session)
        outcome = await _ask(session, user, ds)
        stored = await session.get(ChatMessage, outcome.message_id)

        assert outcome.steps == [
            ("retrieve", 11),
            ("join_graph", 22),
            ("schema", 33),
            ("prompt", 44),
            ("generate", 55),
            ("guard", 66),
            ("dry_run", 77),  # 第一次的往返：被丢弃，但钱已经花了，必须记账
            ("generate", 88),  # 第二次自己的三段
            ("guard", 99),
            ("execute", 111),
            ("chart", 222),
            ("conclude", 333),
        ]
        assert stored is not None and stored.latency_ms == sum(ms for _, ms in outcome.steps)
        # `result_stats.elapsed_ms` 记的是 execute 那一段（037 起含成功那一次的 EXPLAIN 往返）
        assert stored.result_stats is not None and stored.result_stats["elapsed_ms"] == 111
