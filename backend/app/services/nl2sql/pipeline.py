"""问数链路的编排（architecture §4.1 的 ②→⑨）与 LLM 草稿的兜底解析。

本片不开 HTTP 端点：SSE 与 `POST /api/chat/ask` 整块归 P8，理由见 architecture §4.1 ⑦ 的
as-built(P2-012)。鉴权照旧走 `datasource_service.get_authorized`——绕过 HTTP 不等于绕过权限面。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.security import decrypt_secret
from app.models.chat import ChatMessage, ChatSession
from app.models.user import User
from app.services import datasource_service, sql_guard
from app.services.chart_advisor import advise_chart
from app.services.llm_client import LlmBadResponse, LlmClient
from app.services.nl2sql.executor import execute_readonly, resolve_row_limit
from app.services.nl2sql.prompt_builder import build_conclusion_prompt, build_prompt
from app.services.nl2sql.retriever import Retriever, load_schema_tables
from app.services.sql_guard import QualifiedTable, SqlGuardError
from app.settings import get_settings

logger = logging.getLogger(__name__)

# 围栏是"输入卫生"而不是安全规则：剥掉它不给模型任何额外权力，守卫才是裁决者（safety §1）。
_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*[ \t]*\n?(?P<body>.*)\n?[ \t]*```$", re.DOTALL)
_BARE_SQL = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


@dataclass(frozen=True)
class SqlDraft:
    """模型正文解析出来的三选一：一条 SQL、一次追问、或者解析失败（抛）。

    `sql` 与 `clarify` 互斥——`clarify` 非空时 `sql` 恒 `None`，编排层据此走追问而不是执行。
    """

    sql: str | None
    explanation: str | None
    clarify: str | None


def parse_llm_draft(text: str) -> SqlDraft:
    """把模型正文解析成 `SqlDraft`；分档只有两条线（§4.1 ⑦ 的 as-built ④）。

    **没补全任何东西的算恢复**：代码围栏、JSON 前后粘的客套话/解释文字，都只是外壳。
    **要猜模型本来想说什么的算不可恢复**：`max_tokens` 截断（大括号不闭合）、纯散文里
    根本没有 JSON、空响应——一律 `llm_bad_response` 早退。补全半截 SQL 之后执行它的失败成本
    是"跑了一条没人授权过的语句"，这比拒答重得多。
    """
    raw = (text or "").strip()
    if not raw:
        _reject("模型没有返回正文", raw)

    fenced = _FENCE.match(raw)
    body = fenced.group("body").strip() if fenced else raw

    start, end = body.find("{"), body.rfind("}")
    if start != -1:
        if end <= start:
            # 开了 JSON 却没关：正是 max_tokens 截断的形状。
            _reject("模型返回的 JSON 被截断（大括号不闭合），不做补全", body)
        try:
            obj = json.loads(body[start : end + 1])
        except json.JSONDecodeError as exc:
            _reject(f"模型返回的不是合法 JSON 对象：{exc.msg}", body)
        if not isinstance(obj, dict):
            _reject("模型返回的 JSON 不是对象", body)
        return _draft_from_obj(obj)

    if _BARE_SQL.match(body):
        # 没按契约回 JSON，但围栏里那条语句是完整的——一个字都没补，照收。
        return SqlDraft(sql=body, explanation=None, clarify=None)

    _reject("模型返回里没有 SQL", body)


def _draft_from_obj(obj: dict[str, object]) -> SqlDraft:
    sql = str(obj.get("sql") or "").strip()
    clarify = str(obj.get("clarify") or "").strip()
    explanation = str(obj.get("explanation") or "").strip()
    if clarify:
        # 本轮是追问，不是草稿：带着 clarify 里的 SQL 一起执行等于替用户猜一个前提。
        return SqlDraft(sql=None, explanation=explanation or None, clarify=clarify)
    if sql:
        return SqlDraft(sql=sql, explanation=explanation or None, clarify=None)
    _reject("模型既没给 SQL 也没给追问", str(obj))


def _reject(message: str, preview: str) -> NoReturn:
    # detail 只留前 200 字：工单验收要"掉的原文是什么"可见，但整段模型输出进日志/响应体没意义。
    raise LlmBadResponse(f"模型返回无法解析：{message}", detail=preview[:200])


# ============================================================ 编排（§4.1 的 ②→⑨）


@dataclass
class AskOutcome:
    """一次问数的全部中间产物：`demo_ask.py` 打印的就是它，P8 的接口层回吐的也是它。

    为什么不是"成功返回结果、失败抛异常"：拍板 ④ 的三档（检索为空 / 畸形返回 / 守卫拒绝）是
    这条链路的**答案**而不是故障——它们各自带着用户下一步要用的东西（补录知识 / 原文残形 /
    违规规则）。执行侧的异常（超时、只读能力缺失、驱动报错）不在这里，原样上抛给全局 handler，
    映射成 HTTP status 的落点只有 P8 一处。
    """

    question: str
    chat_session_id: int
    # 留痕失败时为 None（见 `finally` 那一段）：0 会被读成"有一行 id=0 的记录"，而它不存在。
    message_id: int | None = None
    steps: list[tuple[str, int]] = field(default_factory=list)
    retrieved: list[dict[str, Any]] = field(default_factory=list)
    datasource_id: int | None = None
    tables: list[str] = field(default_factory=list)
    sql_raw: str | None = None
    sql_final: str | None = None
    guard_result: dict[str, Any] | None = None
    executed: bool = False
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    run_id: str | None = None
    result_file: str | None = None
    chart_spec: dict[str, Any] | None = None
    conclusion: str | None = None
    clarify: str | None = None
    model: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class _Stopwatch:
    """每步耗时的记步器（roadmap P2 踩坑 ②："卡在哪一步"必须是事实而不是猜）。"""

    def __init__(self) -> None:
        self.steps: list[tuple[str, int]] = []
        self._mark = time.perf_counter()

    def lap(self, name: str) -> None:
        now = time.perf_counter()
        self.steps.append((name, round((now - self._mark) * 1000)))
        self._mark = now

    @property
    def last_ms(self) -> int:
        """刚 `lap()` 完那一步的耗时；一步都没有时是 0。"""
        return self.steps[-1][1] if self.steps else 0


async def ask(
    session: AsyncSession,
    *,
    actor: User,
    question: str,
    datasource_ids: Sequence[int] = (),
    retriever: Retriever,
    llm: LlmClient,
) -> AskOutcome:
    """跑一遍 ②→⑨，返回结构化中间结果；无论走到哪一步退出，库里都留下一行 `chat_messages`。

    `retriever` 与 `llm` 由调用方注入（接缝口径见 verification §2.1 的 pipeline 行）：
    检索实现 P4 要换 Hybrid，LLM 客户端在测试里走 respx——两处都不该在 pipeline 里 new。
    """
    settings = get_settings()
    chat = ChatSession(user_id=actor.id, title=question[:200])
    msg = ChatMessage(role="assistant", question_text=question)
    session.add(chat)
    await session.flush()
    msg.session_id = chat.id

    # message_id 留空：它只在 `finally` 的 commit 成功后才被填上。
    # 初始写成 0 的话，"留痕失败"和"有一行 id=0 的记录"在调用方眼里就长一样了。
    out = AskOutcome(question=question, chat_session_id=chat.id)
    watch = _Stopwatch()
    try:
        found = await retriever.search(
            session,
            actor=actor,
            question=question,
            datasource_ids=datasource_ids,
            k=settings.retrieval.final_tables_k,
        )
        watch.lap("retrieve")
        out.retrieved = [
            {
                # §2.7 的五键：score_vec/fused 恒 null（向量与 RRF 是 P4），留 null 才能把
                # "没命中"和"没算"分开。
                "card_id": r.cards[0].card_id if r.cards else None,
                "table_uid": r.table_uid,
                "score_kw": round(r.score_kw, 4),
                "score_vec": None,
                "fused": None,
            }
            for r in found
        ]
        msg.retrieved = out.retrieved
        if not found:
            return _exit_early(msg, out, "no_schema_found", NO_SCHEMA_HINT)

        # ③ 一次问数只打一个数据源：一条连接串连不了两个库，跨源需求今天就是拒答。
        # 候选里混了别的源时按相关度取第一名那个源，其余源的表**不进 prompt**——
        # 给了模型也执行不了，而"给了却不能执行"正是模型写出守卫必拒 SQL 的头号原因。
        primary_id = found[0].datasource_id
        same_source = [r for r in found if r.datasource_id == primary_id]
        ds, _access = await datasource_service.get_authorized(session, actor, primary_id)
        chat.datasource_id = ds.id
        out.datasource_id = ds.id

        tables = await load_schema_tables(
            session, actor=actor, table_uids=[r.table_uid for r in same_source]
        )
        watch.lap("schema")
        out.tables = [t.full_name for t in tables]
        if not tables:
            # 检索报了 uid、这里却一张卡都补不出来：只可能是同步在两步之间把表 prune 掉了。
            return _exit_early(msg, out, "no_schema_found", NO_SCHEMA_HINT)

        # L4 覆盖 L3，与 011 的 `resolve_timeout` 共用一条判定（`resolve_row_limit`）。
        # 不写成 `ds.row_limit or settings.query.row_limit`：`or` 兜不住手插行的负数，而负数会
        # 一路进 `split_truncated` 渲染成 `rows[:-n]`——真结果从尾部被吃掉且不报截断。
        row_limit = resolve_row_limit(ds)
        messages = build_prompt(
            question=question,
            tables=tables,
            terms=(),
            examples=(),
            row_limit=row_limit,
            # 010 的硬接线义务：漏传这两个就是静默"素材段不限长"，报错推迟到上下文超限那天。
            token_budget=settings.retrieval.token_budget,
            few_shot_budget=settings.retrieval.few_shot_token_budget,
        )
        watch.lap("prompt")

        raw = await llm.complete(messages)
        out.model = msg.model = llm.model
        watch.lap("generate")
        try:
            draft = parse_llm_draft(raw)
        except LlmBadResponse as exc:
            # 畸形返回是拍板 ④ 的三档之一：原文片段必须留痕，否则没人知道模型到底回了什么。
            return _exit_early(msg, out, exc.code, f"{exc.message}（原文片段：{exc.detail}）")
        out.sql_raw = msg.sql_raw = draft.sql
        if draft.clarify:
            # 追问不是故障：本轮的答案就是那句 `clarify`，落成 conclusion 而不是 error。
            # 这里**不补 chart/conclude 两个 0ms 的假步**：耗时清单的用途是"卡在哪一步"（踩坑 ②），
            # 把没跑的步报成 0ms 等于把"跑完了但没画图"当成事实端出来。步数少两格就是少两格。
            out.clarify = draft.clarify
            out.conclusion = draft.clarify
            msg.conclusion = draft.clarify
            return out

        allowed = frozenset(QualifiedTable.parse(name) for name in out.tables)
        try:
            guarded = sql_guard.check(
                draft.sql or "",
                allowed=allowed,
                dialect=ds.kind,  # type: ignore[arg-type]
                default_schema="",
                max_rows=row_limit,
            )
        except SqlGuardError as exc:
            msg.guard_result = out.guard_result = _guard_outcome(
                ok=False, violations=[exc.violation]
            )
            early = _exit_early(
                msg,
                out,
                "sql_guard_rejected",
                f"[{exc.rule_id}] {exc.message}",
            )
            watch.lap("guard")
            return early
        watch.lap("guard")
        out.sql_final = msg.sql_final = guarded.sql_final
        msg.guard_result = out.guard_result = _guard_outcome(ok=True, violations=[])

        result = await execute_readonly(
            ds,
            password=decrypt_secret(ds.secret_enc),
            sql_final=guarded.sql_final or "",
            row_limit=row_limit,
            max_cell_chars=settings.result.max_cell_chars,
            result_dir=settings.result.dir,
        )
        watch.lap("execute")
        out.executed = msg.executed = True
        out.columns, out.rows = result.columns, result.rows
        out.row_count, out.truncated = result.row_count, result.truncated
        out.run_id, out.result_file = result.run_id, result.result_file
        # §2.7 说这一列是"列定义"，但定义里只剩名字：行出 executor 时已过 `serialize_cell`，
        # `Decimal`/`datetime` 的源库类型那一刻就丢了。填不进列的东西不在列里写。
        msg.result_columns = [{"name": c} for c in result.columns]
        msg.result_stats = {
            "row_count": result.row_count,
            "truncated": result.truncated,
            # §2.7 那一格的 `elapsed_ms` 是**源库执行**那一步的耗时，不是整条链的合计
            # （合计在 `latency_ms` 列）。两列分开才答得出"慢在模型还是在源库"。
            "elapsed_ms": watch.last_ms,
            "run_id": result.run_id,
            # 只存引用不存路径：result_file 是服务端绝对路径，进库就等于把部署拓扑写进数据
            "result_file": Path(result.result_file).name,
            "dialect": ds.kind,
        }

        spec = advise_chart(columns=result.columns, rows=result.rows)
        out.chart_spec = msg.chart_spec = asdict(spec)
        watch.lap("chart")

        conclusion = (
            await llm.complete(
                build_conclusion_prompt(
                    question=question,
                    columns=result.columns,
                    rows=result.rows,
                    row_count=result.row_count,
                    truncated=result.truncated,
                    # 与 ④ 同一条硬接线义务（010 拍的）：`token_budget=None` 意为**不裁切**，
                    # 漏传不会报错，只会让 ⑨ 的素材段不限长——宽结果下照样顶爆上下文。
                    token_budget=settings.retrieval.token_budget,
                )
            )
        ).strip()
        watch.lap("conclude")
        out.conclusion = msg.conclusion = conclusion
    except AppError as exc:
        # 上游/执行侧的真故障（LLM 不可达、源库超时、只读能力缺失…）留痕后原样上抛：
        # 映射成 HTTP status 的落点只有全局 handler 一处，这里再判一遍就会有两套口径。
        # 只写 `msg` 不写 `out`：异常抬走时调用方拿不到返回值，往一个要被丢弃的对象上
        # 抄错误码属于"看起来对称、实际上没人读"。留痕那一格才是现场。
        msg.error_code, msg.error_message = exc.code, exc.message
        raise
    finally:
        out.steps = watch.steps
        msg.latency_ms = _total_ms(watch)
        # 终局留痕写在 finally：中途抬异常也要有这一行（007 的教训——状态停在半路最难查）。
        # 但这次 commit 自己**不能让原异常消失**：元数据库正是故障源时（超时、连接断、schema 被
        # 删），失败的 commit 会抛 PendingRollbackError 顶掉 QueryTimeout/DBAPIError，调用方
        # 拿到的是一个跟现场无关的错误码。留痕丢了要能看见，但不能为此把真故障换成假故障。
        session.add(msg)
        try:
            await session.commit()
            out.message_id = msg.id
        except Exception:  # 宽是故意的：任何留痕失败都不许顶掉上面那条真故障
            logger.exception("问数留痕写入失败（question=%s）", question[:80])

    return out


NO_SCHEMA_HINT = "没有匹配到任何表：请补录表注释 / 检查授权 / 点此同步"


def _total_ms(watch: _Stopwatch) -> int:
    return sum(ms for _, ms in watch.steps)


def _guard_outcome(*, ok: bool, violations: Sequence[Any]) -> dict[str, Any]:
    """`chat_messages.guard_result` 的形状（§2.7）：`{ok, violations:[{code,field,message}]}`。

    放这里而不是 sql_guard 里，是因为 sql_guard 的 `GuardResult` 是**裁决原文**，
    而这一列是给人看的审计快照——两者字段名不同（`node`→`field`）是刻意的。

    **拒绝那一支的快照只有第一条违规**：链路走的是 `sql_guard.check()`，它命中首条即抛
    （003 的口径），拿不到剩下的。要"一次看全所有违规"得改走 `guard()`，那是接口层的活
    （P8 的展示形态决定值不值这一趟），今天先按"报一条最要命的"落库。
    """
    return {
        "ok": ok,
        "violations": [{"code": v.code, "field": v.node, "message": v.message} for v in violations],
    }


def _exit_early(msg: ChatMessage, out: AskOutcome, code: str, message: str) -> AskOutcome:
    """三类早退的共同形状：`executed=false` + 稳定 code + 一句下一步。"""
    out.error_code, out.error_message = code, message
    msg.error_code, msg.error_message = code, message
    msg.executed = False
    return out
