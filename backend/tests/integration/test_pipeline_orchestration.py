"""`pipeline.ask()` 的编排（architecture §4.1 的 ②→⑨）。

接缝口径逐条对 docs/verification.md §2.1 的 `pipeline` 行：LLM 走 respx、检索走 `Retriever`
Protocol 的桩、executor 走 monkeypatch 的 spy。为什么要 pg 夹具：早退那三类的验收本体是
"留下一行 `chat_messages`"，不落库的断言只证明了返回值，没证明留痕。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from app.core.errors import NotImplementedSource, QueryTimeout, ReadonlyCapabilityMissing
from app.core.security import encrypt_secret
from app.models.chat import ChatMessage
from app.models.datasource import DataSource
from app.models.user import User
from app.services.llm_client import LlmClient
from app.services.nl2sql import executor, pipeline
from app.services.nl2sql.prompt_builder import SchemaTable
from app.services.nl2sql.retriever import CardMatch, RetrievedTable
from app.settings import LlmGroup, get_settings

pytestmark = pytest.mark.pg

READY_LLM = LlmGroup(
    base_url="https://llm.test/v1",
    api_key="sk-test",
    model="test-model",
    temperature=0,
    timeout_s=5,
)


class FakeRetriever:
    """`Retriever` Protocol 的桩：只记调用，返回预先摊好的候选。"""

    def __init__(self, found: list[RetrievedTable]) -> None:
        self.found = found
        self.calls: list[dict[str, Any]] = []

    async def search(
        self,
        session: Any,
        *,
        actor: User,
        question: str,
        datasource_ids: Any = (),
        k: int | None = None,
    ) -> list[RetrievedTable]:
        self.calls.append({"question": question, "datasource_ids": list(datasource_ids), "k": k})
        return self.found


async def _seed_user(session: Any) -> User:
    user = User(username="asker", role="member", password_hash="x")
    session.add(user)
    await session.flush()
    return user


@respx.mock
async def test_检索为空时早退并留下一行(session_factory, results_dir: Path) -> None:
    """§4.1 的 fail-fast：一个候选都没有就不该去生成 SQL（宁可不答，不让模型对着空 schema 编）。"""
    route = respx.post("https://llm.test/v1/chat/completions")
    async with session_factory() as session:
        user = await _seed_user(session)
        outcome = await pipeline.ask(
            session,
            actor=user,
            question="随便问一句",
            datasource_ids=[],
            retriever=FakeRetriever([]),
            llm=LlmClient(llm=READY_LLM, http=httpx.AsyncClient()),
        )

    assert outcome.error_code == "no_schema_found"
    assert outcome.sql_raw is None and outcome.sql_final is None
    assert not route.called  # 早退发生在第一次 LLM 往返之前
    assert not results_dir.exists() or list(results_dir.glob("*.csv")) == []
    # 工单 013 验收 ③：拒答不能只说"没有"，必须给出三条当下就能做的动作。
    # 断言打在 outcome 上而不是打在常量上——常量对了但编排把它换了位置，用户看到的照样是空话。
    assert all(word in outcome.error_message for word in ("补录表注释", "检查授权", "同步")), (
        outcome.error_message
    )
    # 服务端消息只写**动作名**，不写界面话（architecture §4.1 的 as-built(P2-013) 那条分层）。
    # "点此同步"在 P2 指不到任何东西，而这一行是要落进 chat_messages.error_message 给后人看的。
    assert "点此" not in outcome.error_message, outcome.error_message
    # 每步耗时在场：早退也要说清"卡在哪一步"
    assert [name for name, _ in outcome.steps] == ["retrieve"]
    assert outcome.message_id is not None


def _draft_route(*, sql: str | None = None, clarify: str | None = None) -> respx.Route:
    """按 system 消息分流的假端点：第一条是 SQL 生成，第二条是结论生成。

    为什么分流而不是 `side_effect=[a, b]`：调用次序本身是被测对象的一部分，用序列桩就变成
    "我按我猜的次序应答"——多叫一次或少叫一次都测不出来。
    """
    if not sql and not clarify:
        # 两个都不给 = 用例想测"畸形返回"，但那该由 `return_value=httpx.Response(...)` 自己写死。
        # 让它静默回一个空 JSON 的话，桩就成了"永远有个答案"，测出来的早退是桩造的。
        raise ValueError("_draft_route 必须至少给 sql 或 clarify 之一")
    draft = json.dumps(
        {"sql": sql or "", "explanation": "按月汇总", "clarify": clarify or ""},
        ensure_ascii=False,
    )

    def _reply(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "SQL 生成器" in body["messages"][0]["content"]:
            return httpx.Response(200, json=_chat(draft))
        return httpx.Response(200, json=_chat("全年共 3 个月有数据，合计 300.5 万元。"))

    return respx.post("https://llm.test/v1/chat/completions").mock(side_effect=_reply)


def _chat(content: str) -> dict[str, Any]:
    return {
        "id": "cmpl-1",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
    }


DRAFT_SQL = "SELECT dt, SUM(amount) AS 金额 FROM ai_web_demo.order_main GROUP BY dt"
RESULT_ROWS = [["2024-01", "100.5"], ["2024-02", "80.0"], ["2024-03", "120.0"]]


class Spies:
    """被打桩的协作者 + 它们各自收到的实参（编排顺序与接线口径全靠这些断言）。"""

    def __init__(self) -> None:
        self.executor_calls: list[dict[str, Any]] = []
        self.card_requests: list[list[str]] = []

    async def execute_readonly(self, row: Any, **kwargs: Any) -> Any:
        self.executor_calls.append({"ds_id": row.id, **kwargs})
        return executor.ExecutionResult(
            columns=["dt", "金额"],
            rows=[list(r) for r in RESULT_ROWS],
            truncated=False,
            row_count=len(RESULT_ROWS),
            run_id="20260929-abc123",
            result_file="data/results/x.csv",
        )

    async def load_cards(self, _session: Any, **kwargs: Any) -> list[SchemaTable]:
        self.card_requests.append(list(kwargs["table_uids"]))
        return [
            SchemaTable(
                full_name="ai_web_demo.order_main",
                segments=("# 表 ai_web_demo.order_main —— 订单主表\n【主键】id\n",),
                relations=(),
            )
        ]


def _found(datasource_id: int, table_id: int) -> RetrievedTable:
    return RetrievedTable(
        table_uid="u" * 32,
        table_id=table_id,
        datasource_id=datasource_id,
        title="订单主表",
        score_kw=3.5,
        matched_term_count=2,
        matched_column_count=2,
        hits=(),
        cards=(
            CardMatch(
                card_id=77,
                table_id=table_id,
                table_uid="u" * 32,
                datasource_id=datasource_id,
                kind="table",
                seq=0,
                title="订单主表",
                score=3.5,
                text_preview="# 表 ai_web_demo.order_main",
            ),
        ),
    )


@pytest.fixture
def spies(monkeypatch: pytest.MonkeyPatch) -> Spies:
    """只打两处外桩：executor（会连真 MySQL）与 load_schema_tables（要真卡片语料）。

    守卫和 chart_advisor **不打桩**：验收里"打印的是守卫重生成后的版本"这一条，靠的就是
    真守卫改真 SQL；桩出来的 sql_final 证明不了接线。权限同理不打桩——`get_authorized`
    走真判定，被测数据源由用例自己 seed 成 actor 所有。
    """
    out = Spies()
    monkeypatch.setattr(pipeline, "execute_readonly", out.execute_readonly)
    monkeypatch.setattr(pipeline, "load_schema_tables", out.load_cards)
    return out


async def _seed_user_and_source(session: Any, *, row_limit: int = 1000) -> tuple[User, DataSource]:
    user = User(username="asker", role="member", password_hash="x")
    session.add(user)
    await session.flush()
    ds = DataSource(
        name="demo-mysql",
        kind="mysql",
        host="127.0.0.1",
        port=3306,
        catalog_name="",
        connect_user="aiweb_ro",
        secret_enc=encrypt_secret("pw"),
        params={},
        created_by=user.id,
        # 显式写这两列：库里它们是 server_default，用例里这个对象是刚 add 的、
        # 没经过一次真 SELECT，pipeline 读它们会触发懒加载（async 下直接 MissingGreenlet）。
        row_limit=row_limit,
        timeout_ms=15000,
    )
    session.add(ds)
    await session.flush()
    return user, ds


@respx.mock
async def test_一次完整问数把九步串起来并留痕(
    session_factory, spies: Spies, results_dir: Path
) -> None:
    async with session_factory() as session:
        user, ds = await _seed_user_and_source(session)
        respx_mock = _draft_route(sql=DRAFT_SQL)

        outcome = await pipeline.ask(
            session,
            actor=user,
            question="2024 年每个月的订单总金额是多少",
            datasource_ids=[ds.id],
            retriever=FakeRetriever([_found(ds.id, ds.id * 10 + 1)]),
            llm=LlmClient(llm=READY_LLM, http=httpx.AsyncClient()),
        )

        assert [name for name, _ in outcome.steps] == [
            "retrieve",
            "schema",
            "prompt",
            "generate",
            "guard",
            "execute",
            "chart",
            "conclude",
        ]
        assert respx_mock.call_count == 2  # ⑤ 生成 + ⑨ 结论
        assert outcome.sql_raw == DRAFT_SQL
        # 打印与落库的都是守卫重生成后的版本：这条草稿**没写** LIMIT，守卫才补上 row_limit+1
        # （那多出来的一行是截断探针，见 safety §4.2）。模型听话按模板写了 LIMIT 时这一支不走，
        # 见文件末尾那条用例——别把这里的 1001 当成真链路的常态。
        assert outcome.sql_final is not None and outcome.sql_final != DRAFT_SQL
        assert outcome.sql_final.endswith("LIMIT 1001")
        assert outcome.model == "test-model"  # 记的是这次真问的模型，不是 .env 里那个
        assert outcome.guard_result == {"ok": True, "violations": []}
        assert outcome.executed is True
        assert outcome.chart_spec is not None and outcome.chart_spec["type"] == "line"
        assert outcome.conclusion is not None and outcome.conclusion.startswith("全年")
        assert outcome.error_code is None

        # ⑦ 的三条接线义务。键集合断得**一个不多**：多出来的那个键，就是调用方能传进来的
        # 那个入参——`row_limit`/`max_cell_chars`/`result_dir` 一旦被请求体摸到，L4/L3 的
        # 口径和"结果不落元数据库"就同时作废，而那种改动在"只断言值对不对"的测试里全绿。
        call = spies.executor_calls[0]
        assert set(call) == {
            "ds_id",
            "password",
            "sql_final",
            "row_limit",
            "max_cell_chars",
            "result_dir",
        }
        assert call["ds_id"] == ds.id
        # 口令是从 secret_enc 解出来的，不是哪儿传来的（这一格是 Fernet 接线的唯一证据）
        assert call["password"] == "pw"
        assert call["sql_final"] == outcome.sql_final  # 执行的就是守卫那句
        assert call["row_limit"] == 1000  # seed 的 L4 列就写着 1000；不跟 .env 的全局值走
        assert call["max_cell_chars"] == get_settings().result.max_cell_chars
        assert call["result_dir"] == get_settings().result.dir

        stored = await session.get(ChatMessage, outcome.message_id)
        assert stored is not None
        assert stored.sql_raw == DRAFT_SQL and stored.sql_final == outcome.sql_final
        assert stored.retrieved[0]["score_kw"] == 3.5
        assert stored.retrieved[0]["score_vec"] is None
        assert stored.result_stats is not None and stored.result_stats["row_count"] == 3
        assert stored.executed is True and stored.error_code is None


@respx.mock
async def test_守卫拒绝时一次都不进执行器(session_factory, spies: Spies, results_dir: Path) -> None:
    """verification §2.1 ① 的本体：断言的是"executor 没被调用"，不是看日志里有没有那句话。

    守卫在编排层是**可观测的一道闸**：绕过它直调 executor，011 的内存上限（守卫注入的
    `LIMIT row_limit+1`）就没了，而且"三层防御"退化成两层。
    """
    async with session_factory() as session:
        user, ds = await _seed_user_and_source(session)
        route = _draft_route(sql="SELECT * FROM ai_web_demo.secret_stuff")

        outcome = await pipeline.ask(
            session,
            actor=user,
            question="把 secret_stuff 全列出来",
            datasource_ids=[ds.id],
            retriever=FakeRetriever([_found(ds.id, ds.id * 10 + 1)]),
            llm=LlmClient(llm=READY_LLM, http=httpx.AsyncClient()),
        )

        assert spies.executor_calls == []
        assert route.call_count == 1  # 结论那一次也不该发
        assert outcome.error_code == "sql_guard_rejected"
        assert outcome.sql_final is None and outcome.executed is False
        assert outcome.guard_result is not None
        assert outcome.guard_result["ok"] is False
        assert outcome.guard_result["violations"][0]["code"] == "table_not_allowed"
        assert [name for name, _ in outcome.steps] == [
            "retrieve",
            "schema",
            "prompt",
            "generate",
            "guard",
        ]
        stored = await session.get(ChatMessage, outcome.message_id)
        assert stored is not None
        assert stored.executed is False and stored.error_code == "sql_guard_rejected"
        # sql_raw 仍然留下：拒了也要能查"模型到底写了什么"
        assert stored.sql_raw == "SELECT * FROM ai_web_demo.secret_stuff"
    assert not results_dir.exists() or list(results_dir.glob("*.csv")) == []


@respx.mock
async def test_模型硬产出_DROP_时守卫拦下且一次都不进执行器(
    session_factory, spies: Spies, results_dir: Path
) -> None:
    """工单 013 验收 ② 的字面要求：桩里**故意**放 `DROP TABLE orders`。

    与上一条 `table_not_allowed` 分开是因为它们撞的是两道不同的门：那一条是"表不在白名单"，
    这一条是"语句根本不是 SELECT"（`top_level_not_select`，语料 §8.1 第一档）。
    只测前者就等于默认"模型只会挑错表"，而 roadmap P2 验收 5 问的是删表。
    """
    async with session_factory() as session:
        user, ds = await _seed_user_and_source(session)
        route = _draft_route(sql="DROP TABLE orders")

        outcome = await pipeline.ask(
            session,
            actor=user,
            question="把 orders 表删了",
            datasource_ids=[ds.id],
            retriever=FakeRetriever([_found(ds.id, ds.id * 10 + 1)]),
            llm=LlmClient(llm=READY_LLM, http=httpx.AsyncClient()),
        )

        assert spies.executor_calls == []
        assert route.call_count == 1
        assert outcome.error_code == "sql_guard_rejected"
        assert outcome.executed is False
        # sql_final 必须是 None：没有"重写后放行的版本"这种东西，DROP 不会被改写成 SELECT 放过去
        assert outcome.sql_final is None
        assert outcome.guard_result is not None
        assert outcome.guard_result["violations"][0]["code"] == "top_level_not_select"
        stored = await session.get(ChatMessage, outcome.message_id)
        assert stored is not None
        assert stored.sql_raw == "DROP TABLE orders"  # 拒了也要留下模型原文当现场
        assert stored.executed is False
    # 验收 ⑤：拒答不写 CSV、也不留半截文件（目录压根不该被动过）
    assert not results_dir.exists() or list(results_dir.glob("*")) == []


@respx.mock
async def test_模型反问时不执行并把那句问话当结论(session_factory, spies: Spies) -> None:
    """`clarify` 非空 → 不执行（拍板 ④ 最后一档）。它是这一轮的答案，不是故障。"""
    async with session_factory() as session:
        user, ds = await _seed_user_and_source(session)
        _draft_route(clarify="你要看的是下单金额还是实付金额？")

        outcome = await pipeline.ask(
            session,
            actor=user,
            question="每个月的金额是多少",
            datasource_ids=[ds.id],
            retriever=FakeRetriever([_found(ds.id, ds.id * 10 + 1)]),
            llm=LlmClient(llm=READY_LLM, http=httpx.AsyncClient()),
        )

        assert spies.executor_calls == []
        assert outcome.error_code is None
        assert outcome.sql_raw is None
        assert outcome.clarify == "你要看的是下单金额还是实付金额？"
        assert outcome.executed is False
        stored = await session.get(ChatMessage, outcome.message_id)
        assert stored is not None
        assert stored.conclusion == outcome.clarify
        assert stored.error_code is None and stored.executed is False


@respx.mock
async def test_返回被截断时留下畸形返回那一行而不是猜(session_factory, spies: Spies) -> None:
    async with session_factory() as session:
        user, ds = await _seed_user_and_source(session)
        respx.post("https://llm.test/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json=_chat('{"sql": "SELECT dt FROM ai_web_demo.order_main GROUP BY dt LI'),
            )
        )

        outcome = await pipeline.ask(
            session,
            actor=user,
            question="每个月的日期",
            datasource_ids=[ds.id],
            retriever=FakeRetriever([_found(ds.id, ds.id * 10 + 1)]),
            llm=LlmClient(llm=READY_LLM, http=httpx.AsyncClient()),
        )

        assert spies.executor_calls == []
        assert outcome.error_code == "llm_bad_response"
        # 掉的原文要看得见（验收"哪一步掉的、掉的原文是什么"）
        assert "SELECT dt FROM ai_web_demo.order_main" in (outcome.error_message or "")
        assert [name for name, _ in outcome.steps] == ["retrieve", "schema", "prompt", "generate"]
        stored = await session.get(ChatMessage, outcome.message_id)
        assert stored is not None
        assert stored.error_code == "llm_bad_response" and stored.sql_raw is None


@respx.mock
async def test_两个预算参数是装配点接上去的不是靠默认值(
    session_factory, spies: Spies, monkeypatch: pytest.MonkeyPatch
) -> None:
    """010 的硬接线义务：`build_prompt` 的 `token_budget=None` 意为**不裁切**，漏传不会报错，
    只会静默变成"素材段不限长"，等到上下文超限那天才炸。所以这一条要单独钉。

    预算值故意设成不可能与函数默认值相撞的数（few_shot 的默认恰好也是 1500，用默认值断言
    等于没断言）。
    """
    monkeypatch.setenv("AIWEB_RETRIEVAL__TOKEN_BUDGET", "4321")
    monkeypatch.setenv("AIWEB_RETRIEVAL__FEW_SHOT_TOKEN_BUDGET", "777")
    get_settings.cache_clear()
    seen: dict[str, Any] = {}
    real = pipeline.build_prompt

    def spy(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(pipeline, "build_prompt", spy)
    try:
        async with session_factory() as session:
            user, ds = await _seed_user_and_source(session)
            _draft_route(sql=DRAFT_SQL)
            await pipeline.ask(
                session,
                actor=user,
                question="2024 年每个月的订单总金额是多少",
                datasource_ids=[ds.id],
                retriever=FakeRetriever([_found(ds.id, ds.id * 10 + 1)]),
                llm=LlmClient(llm=READY_LLM, http=httpx.AsyncClient()),
            )
    finally:
        # 缓存不清的话，4321/777 会跟着这个 settings 单例活到下一条用例——
        # 别处的预算断言就会在一个没人改过 .env 的仓库里莫名其妙地红。
        get_settings.cache_clear()

    assert seen["token_budget"] == 4321
    assert seen["few_shot_budget"] == 777
    assert seen["row_limit"] == ds.row_limit  # 模板里那句 LIMIT 与守卫/执行器同一个数


@respx.mock
async def test_行数上限走数据源的_L4_而不是全局(
    session_factory, spies: Spies, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`data_sources.row_limit` 的注释写着"覆盖全局默认"，超时那一列 011 已按此实现（L4→L3）。

    全局值用 monkeypatch 钉成另一个数，而不是断言"它不等于 7"：后者会跟着开发者 .env 变红，
    而且真正常量相同的那天它悄悄失效。
    这条用例也是那个选择的钉子——不写它，"row_limit 只从 Settings 取"这句会被下一片
    误读成"L4 那列没人用"，然后把列删掉。
    """
    monkeypatch.setenv("AIWEB_QUERY__ROW_LIMIT", "999")
    get_settings.cache_clear()
    try:
        async with session_factory() as session:
            user, ds = await _seed_user_and_source(session, row_limit=7)
            _draft_route(sql=DRAFT_SQL)

            outcome = await pipeline.ask(
                session,
                actor=user,
                question="2024 年每个月的订单总金额是多少",
                datasource_ids=[ds.id],
                retriever=FakeRetriever([_found(ds.id, ds.id * 10 + 1)]),
                llm=LlmClient(llm=READY_LLM, http=httpx.AsyncClient()),
            )

            assert outcome.sql_final is not None and outcome.sql_final.endswith("LIMIT 8")
            assert spies.executor_calls[0]["row_limit"] == 7
    finally:
        get_settings.cache_clear()


@respx.mock
async def test_执行侧故障留痕之后原样上抛(
    session_factory, spies: Spies, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真故障不是"问数的答案"：留一行痕迹，但异常必须继续往上，让唯一的 handler 去定 status。

    这一条钉的是两件事：① `finally` 里的留痕不会因为抬异常而丢（007 的教训）；
    ② pipeline 不自己吞错误、不改写错误分类。
    """

    async def boom(row: Any, **kwargs: Any) -> Any:
        raise QueryTimeout("源库执行超时被中断", detail="超时上限 15000ms")

    monkeypatch.setattr(pipeline, "execute_readonly", boom)
    async with session_factory() as session:
        user, ds = await _seed_user_and_source(session)
        _draft_route(sql=DRAFT_SQL)

        with pytest.raises(QueryTimeout):
            await pipeline.ask(
                session,
                actor=user,
                question="2024 年每个月的订单总金额是多少",
                datasource_ids=[ds.id],
                retriever=FakeRetriever([_found(ds.id, ds.id * 10 + 1)]),
                llm=LlmClient(llm=READY_LLM, http=httpx.AsyncClient()),
            )

        # 夹具每条用例前清空过这张表，所以"只有一行"本身就是留痕的证据
        rows = (await session.scalars(select(ChatMessage))).all()
        assert len(rows) == 1
        assert rows[0].error_code == "query_timeout"
        assert rows[0].executed is False
        assert rows[0].sql_final is not None  # 死在执行这一步，前头几步都留得住


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (ReadonlyCapabilityMissing("源账号不是只读账号"), "readonly_capability_missing"),
        (NotImplementedSource("kind=postgresql 的只读执行尚未实现"), "not_implemented"),
    ],
    ids=["只读能力缺失", "该源类型未实现"],
)
@respx.mock
async def test_执行侧的另两类异常也走同一条留痕路上抛(
    session_factory, spies: Spies, monkeypatch: pytest.MonkeyPatch, exc: Any, code: str
) -> None:
    """接线义务 ③ 要求这四类异常各自落成**一个稳定 code**——"稳定"意味着它是 `AppError.code`，
    不是 pipeline 里现编的字符串。逐类各测一遍，才不会有一类在编排层被 `except Exception`
    兜成 500、或干脆没人接（那 P8 的 status 表就对不上）。
    """

    async def boom(row: Any, **kwargs: Any) -> Any:
        raise exc

    monkeypatch.setattr(pipeline, "execute_readonly", boom)
    async with session_factory() as session:
        user, ds = await _seed_user_and_source(session)
        _draft_route(sql=DRAFT_SQL)

        with pytest.raises(type(exc)) as caught:
            await pipeline.ask(
                session,
                actor=user,
                question="2024 年每个月的订单总金额是多少",
                datasource_ids=[ds.id],
                retriever=FakeRetriever([_found(ds.id, ds.id * 10 + 1)]),
                llm=LlmClient(llm=READY_LLM, http=httpx.AsyncClient()),
            )
        assert caught.value.code == code  # 原样上抛：分类没被编排层改写
        stored = await session.scalar(select(ChatMessage).where(ChatMessage.error_code == code))
        assert stored is not None and stored.executed is False


@respx.mock
async def test_候选里混了别的源时只问一个源(session_factory, spies: Spies) -> None:
    """一条连接串连不了两个库：拍板按相关度第一名的那个源收口，其余源的表**不进 prompt**。

    为什么不是"带着跨源表一起给、让守卫去拒"：给了模型也执行不了，而"给了却不能执行"
    正是它写出跨库 JOIN 的头号原因；拒一次要用户重问一次，不如一开始就不给。
    """
    other = RetrievedTable(
        table_uid="o" * 32,
        table_id=999,
        datasource_id=4242,  # 另一个源，actor 根本没授权
        title="别家的表",
        score_kw=1.0,
        matched_term_count=1,
        matched_column_count=1,
        hits=(),
        cards=(),
    )
    async with session_factory() as session:
        user, ds = await _seed_user_and_source(session)
        _draft_route(sql=DRAFT_SQL)

        outcome = await pipeline.ask(
            session,
            actor=user,
            question="2024 年每个月的订单总金额是多少",
            datasource_ids=[ds.id],
            retriever=FakeRetriever([_found(ds.id, ds.id * 10 + 1), other]),
            llm=LlmClient(llm=READY_LLM, http=httpx.AsyncClient()),
        )

        assert outcome.datasource_id == ds.id
        # 钉子在这里：补卡片的请求只带同源那一个 uid。跨源那张表**根本没被取料**，
        # 所以它进不了 prompt ——断"prompt 里没有它"是桩自己造的假象（桩对任何 uid 都回同一张表）。
        assert spies.card_requests == [["u" * 32]]
        # retrieved 留痕仍是检索给的全部：拒的是取材，不是记录
        assert {r["table_uid"] for r in outcome.retrieved} == {"u" * 32, "o" * 32}


@respx.mock
async def test_模型照模板写了_LIMIT_时守卫把它钳回探针(session_factory, spies: Spies) -> None:
    """真链路上"模型听话"的那一支，必须有一条用例站在旁边。

    prompt 模板要求模型"必须带 LIMIT {{ row_limit }}"，所以模型写出的行数**等于**上限才是常态，
    而不是例外。这一支的期望是钳位后的形状（safety §1.2 ⑦）：`+1` 探针在、raw 与 final 不相等。
    留痕里 `sql_raw` 仍是模型原文——审计要看它说了什么，执行要跑守卫改过的版本，两列分开才有意义。

    探针到执行侧真的变成 `truncated=true` 这一跳**不在本用例的视野里**（executor 被 spy 打桩，
    它记下行数不记 truncated），钉住它的是 `test_pipeline_live.py`（live）与 2026-09-29 的
    `demo_ask.py` 实录（safety §1.2 ⑦ 末）。
    """
    limited = DRAFT_SQL + " LIMIT 1000"
    async with session_factory() as session:
        user, ds = await _seed_user_and_source(session)
        _draft_route(sql=limited)

        outcome = await pipeline.ask(
            session,
            actor=user,
            question="2024 年每个月的订单总金额是多少",
            datasource_ids=[ds.id],
            retriever=FakeRetriever([_found(ds.id, ds.id * 10 + 1)]),
            llm=LlmClient(llm=READY_LLM, http=httpx.AsyncClient()),
        )

        assert outcome.sql_raw == limited
        assert outcome.sql_final is not None and outcome.sql_final.endswith("LIMIT 1001")
        assert spies.executor_calls[0]["row_limit"] == 1000  # 上限与那句 LIMIT 是同一个数
        stored = await session.get(ChatMessage, outcome.message_id)
        assert stored is not None
        assert stored.sql_raw == limited
        assert stored.sql_final == outcome.sql_final
