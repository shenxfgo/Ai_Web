"""端到端示踪弹（工单 012）：`demo_ask.py "2024 年每个月的订单总金额是多少"`。

P2 验收 4 的本体：一句问话打进去，屏幕上出现检索表、模型给出的 SQL、守卫判定、结果表格、
图表选型和结论，外加每一步耗时——"卡在哪一步"必须是事实而不是猜（roadmap §P2 踩坑 ②）。

为什么走进程内而不是 HTTP：拍板 1 把端点整块交给了 P8，这一片没有 token 可拿。
鉴权也不是绕过去了——pipeline 内部走的就是 `datasource_service.get_authorized` 那同一把尺。

口令不读也不报（verification §1.6）：源库凭据由 `data_sources.secret_enc` 解密，只活在
执行器那几行里，本脚本的 stdout 没有它们的位置。

退出码：0 跑通；1 链路失败（早退、未执行或结果为空）；2 前置条件不满足（LLM 未配置 /
没有可用数据源 / 指定了 actor 看不见的源）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path

import httpx

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

if sys.platform == "win32":  # asyncpg 与 Proactor 事件循环不兼容
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import func, select  # noqa: E402

from app.core.db import dispose_engine, get_sessionmaker  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.core.logging import reconfigure_std_streams  # noqa: E402
from app.models.chat import ChatMessage  # noqa: E402
from app.models.datasource import DataSource  # noqa: E402
from app.models.meta import MetaTable  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import datasource_service, sync_service  # noqa: E402
from app.services.llm_client import LlmClient  # noqa: E402
from app.services.nl2sql import pipeline  # noqa: E402
from app.services.nl2sql.retriever import LikeRetriever  # noqa: E402
from app.settings import get_settings  # noqa: E402


def _print_outcome(out: pipeline.AskOutcome) -> None:
    """把 ②→⑨ 每一步的产物原样端出来：跑通与否都要看得见（验收 3）。"""
    print("\n===== 端到端示踪弹（architecture §4.1 的 ②→⑨）=====")
    print(f"问句             ：{out.question}")
    print(f"模型             ：{out.model or '—（没问到 LLM）'}")
    # None = pipeline 的 finally 那一次 commit 自己失败了（它不许顶掉真故障，代价是这里没痕）
    mid = out.message_id if out.message_id is not None else "—（留痕没写成）"
    print(f"chat_messages.id ：{mid}")

    print(f"[② 检索] {len(out.retrieved)} 张候选表")
    for r in out.retrieved:
        print(f"    - {r['table_uid'][:8]}…  score_kw={r['score_kw']}")
    print(f"[③ 进 prompt 的表] {len(out.tables)} 张：" + "、".join(out.tables or ["—"]))

    print(f"[⑤ 模型给的 SQL] {out.sql_raw or '—（没产出，看下面的终局）'}")
    guard = out.guard_result or {}
    verdict = "未到此步" if "ok" not in guard else "PASS" if guard["ok"] else "REJECT"
    print(f"[⑥ 守卫判定] {verdict}")
    for v in guard.get("violations", []):
        print(f"    - [{v['code']}] {v['field']}：{v['message']}")
    # 验收 2：这一行必须是守卫**重生成后**的版本（带注入的 LIMIT），不是模型原文。
    print(f"[⑥ sql_final] {out.sql_final or '—'}")

    print(f"[⑦ 执行] executed={out.executed} 行数={out.row_count} 截断={out.truncated}")
    print(f"       结果文件：{out.result_file or '—'}")
    if out.clarify:
        print(f"[⑦ 追问] {out.clarify}")
    if out.columns:
        print("[⑦ 结果表格]（最多显示 20 行）")
        print("| " + " | ".join(out.columns) + " |")
        for row in out.rows[:20]:
            print("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
    print(f"[⑧ 图表选型] {out.chart_spec or '—'}")
    print(f"[⑨ 结论] {out.conclusion or '—'}")
    if out.error_code:
        print(f"[终局] error_code={out.error_code} executed={out.executed}")
        print(f"       {out.error_message}")
    if out.steps:
        parts = " → ".join(f"{name} {ms}ms" for name, ms in out.steps)
        print(f"[耗时] {parts} = 合计 {sum(ms for _, ms in out.steps)}ms")


async def _resolve_actor(username: str | None) -> User:
    """取一个真用户当 actor：给了用户名就按名字，否则取库里第一个 admin。

    这里不校验口令：本脚本不签 token，pipeline 只用 `actor.id` 做数据源可见性判断，
    真鉴权在 HTTP 层，而那一层归 P8（拍板 1）。
    """
    async with get_sessionmaker()() as session:
        stmt = select(User).where(User.is_active.is_(True))
        stmt = (
            stmt.where(User.username == username) if username else stmt.where(User.role == "admin")
        )
        actor = (await session.scalars(stmt.order_by(User.id).limit(1))).first()
        if actor is None:
            who = f"用户 {username}" if username else "admin 用户"
            raise SystemExit(f"[FAIL] 库里没有活跃的{who}。先跑 scripts/seed_admin.py 建一个。")
        session.expunge(actor)  # 会话关了还要读 actor.id / username
        return actor


async def _ensure_meta_extracted(ds: DataSource) -> None:
    """`meta_table` 为空就现场补一次完整同步（工单"接口层"那条），顺带把 `kb_card` 刷出来。

    判空必须按 `datasource_id` 而不是全表：一台机器登记两个源时，第一个源同步过就会让
    第二个源永远抽不到结构。
    """
    async with get_sessionmaker()() as session:
        count = await session.scalar(
            select(func.count()).select_from(MetaTable).where(MetaTable.datasource_id == ds.id)
        )
        if count:
            print(f"[准备] 源 {ds.id}（{ds.name}）已有 {count} 张表结构，跳过抽取")
            return
        row = await session.get(DataSource, ds.id)
        owner = await session.get(User, ds.created_by)
        if row is None or owner is None:
            raise SystemExit(f"[FAIL] 源 {ds.id} 或它的登记人不见了，没法替它抽取")
        # 抽取用登记人而不是脚本的 actor：`run_sync` 要的是"谁有权限动这个源的元数据"，
        # 与"谁在问数"是两个角色。这句必须打出来，否则下一片会以为示踪弹以 admin 身份
        # 跑通了同步，而真链路里同步是 007 那条 HTTP 路径的事。
        print(
            f"[准备] meta_table 为空，先对 {ds.name} 做一次完整同步…"
            f"（以登记人 {owner.username} 的身份跑，不是本脚本的 actor）"
        )
        outcome = await sync_service.run_sync(session, row, actor=owner)
        print(f"       同步终态 status={outcome.status} counters={outcome.counters}")
        for err in outcome.errors:
            print(f"       errors: {err}")
        if outcome.status != "success":
            raise SystemExit(f"[FAIL] 同步未成功（status={outcome.status}），问数没有 schema 可用")


async def run_once(question: str, *, ds_id: int | None, username: str | None) -> int:
    if not get_settings().llm.configured:
        print(
            "[FAIL] LLM 未配置：AIWEB_LLM__BASE_URL / API_KEY / MODEL 三项都要给", file=sys.stderr
        )
        return 2
    actor = await _resolve_actor(username)
    async with get_sessionmaker()() as session:
        visible = await datasource_service.list_visible(session, actor)
    if not visible:
        print(f"[FAIL] {actor.username} 名下没有可用数据源（登记走 006 的 HTTP 接口）")
        return 2
    if ds_id is None:
        ds, _access = visible[0]
        print(f"[准备] 未指定 --datasource-id，取第一个可见源：id={ds.id} {ds.name}")
    else:
        matched = [row for row, _ in visible if row.id == ds_id]
        if not matched:
            ids = "、".join(str(row.id) for row, _ in visible)
            print(f"[FAIL] 源 {ds_id} 对 {actor.username} 不可见（可见：{ids}）", file=sys.stderr)
            return 2
        ds = matched[0]

    await _ensure_meta_extracted(ds)

    async with get_sessionmaker()() as session:
        # 会话交给 pipeline 自己提交：AppError 已被它落进那一行 chat_messages 再原样抬上来，
        # 这里只负责"别把故障打成一行 traceback"（验收 3）。
        http = httpx.AsyncClient()
        llm = LlmClient(llm=get_settings().llm, http=http)
        try:
            out = await pipeline.ask(
                session,
                actor=actor,
                question=question,
                datasource_ids=[ds.id],
                retriever=LikeRetriever(),
                llm=llm,
            )
        except AppError as exc:
            print(f"[FAIL] {exc.code}: {exc.message}", file=sys.stderr)
            return 1
        except Exception as exc:  # 宽是故意的：示踪弹要端出真实故障，但不能只有一行栈
            print(f"[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr)
            # 完整栈同时打出去：非 AppError 的这一支没有稳定 code，只有一行消息的话
            # "卡在哪一步"就又变成了猜（踩坑 ②）。步骤耗时在有终局行的路上已被打过了。
            traceback.print_exc()
            return 1
        finally:
            await http.aclose()

    _print_outcome(out)
    if out.clarify:
        print("\n[结果] 模型按契约反问（§4.1 的三种终态之一）：没生成 SQL、没执行，这不算失败。")
        return 0
    if not out.executed or not out.rows:
        print("\n[结果] 未跑通：没执行或结果集为空——上面的步骤耗时与 error_code 就是答案。")
        return 1
    # message_id 为 None 意味着留痕那一次 commit 自己失败了（pipeline 的 finally 不让它顶掉
    # 业务异常，代价就是这里没法回查）。跑通但查不到痕，要单独报出来而不是假装一致。
    if out.message_id is None:
        print("\n[结果] 跑通，但 chat_messages 那一行没写成——现场只剩上面 stdout 的示踪。")
        return 1
    async with get_sessionmaker()() as session:
        row = await session.get(ChatMessage, out.message_id)
    print(f"\n[结果] 跑通：{out.row_count} 行，结果文件 {out.result_file}")
    if row is not None:
        same = "相同" if row.sql_raw == row.sql_final else "不同"
        print(f"       留痕 chat_messages#{row.id}（sql_raw 与 sql_final {same}）")
    return 0


async def _run(question: str, *, ds_id: int | None, username: str | None) -> int:
    """引擎要在同一个事件循环里释放：另起 `asyncio.run` 会开出第二个循环。"""
    try:
        return await run_once(question, ds_id=ds_id, username=username)
    finally:
        await dispose_engine()


def main(argv: Sequence[str] | None = None) -> int:
    reconfigure_std_streams()
    parser = argparse.ArgumentParser(description="问数链路端到端示踪弹（P2 验收 4）")
    parser.add_argument("question", nargs="+", help="一句中文问句")
    parser.add_argument(
        "--datasource-id", type=int, default=None, help="数据源 id，缺省取第一个可见的"
    )
    parser.add_argument("--as-user", default=None, help="以哪个用户名当 actor，缺省取第一个 admin")
    args = parser.parse_args(argv)
    return asyncio.run(
        _run(" ".join(args.question).strip(), ds_id=args.datasource_id, username=args.as_user)
    )


if __name__ == "__main__":
    raise SystemExit(main())
