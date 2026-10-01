"""进度事件落库（工单 017 的写侧）：一行行追加、seq 单调、与它所描述的那次提交同生共死。

为什么不打真 MySQL：这一片要的是"事件行说的每句话都能和库里的事实对上"，其中
**失败隔离**那条真库给不了前提（要的是"某个库整批真抛错"）。真演示库上的
`total == 10` 归 `test_sync_events_live.py`，读侧（SSE 帧、游标补读、ping）归
`test_sync_events_sse_pg.py`。

桩件与 `_register`/`_sync`/`stub` 来自 `test_sync_pg.py`：016 之后"发起 + 消费"已经是
一条现成的缝（POST 拿 202，再 await worker 的循环体把那一轮跑完）。

期望值口径：`docs/metadata-model.md` §2.8（表、counters 与 payload 形状）+ §6 as-built(P3)
第 2/3 条 + 工单 017 验收 1/2/6 + spec 故事 6/11。序列与计数都是手写的，不是从代码反推。
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.settings import get_settings
from tests.integration.test_sync_pg import (
    StubExtractor,
    _register,
    _sync,
    stub,  # noqa: F401 —— 夹具要 import 进来才在本模块可见
)

pytestmark = pytest.mark.pg

Login = Callable[..., Awaitable[Any]]
Maker = Callable[..., StubExtractor]
Factory = async_sessionmaker[AsyncSession]


async def _events(session_factory: Factory, job_id: int) -> list[dict[str, Any]]:
    """按 seq 升序读回这个作业的全部事件行。

    走裸 SQL 而不是 ORM：读事件的路径不该复用写事件的那份映射，否则列名写错两边一起错
    （与 `test_sync_pg._terminal_row` 同一个理由）。
    """
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(f'select * from "{schema}".sync_job_event where job_id = :j order by seq'),
                    {"j": job_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


async def _one_run(
    client: AsyncClient,
    login: Login,
    username: str,
    session_factory: Factory,
    maker: Maker,
    ds_over: dict[str, Any] | None = None,
    **stub_kw: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """登记一个源、按给定桩件跑完一轮，返回（终局行，事件行）。

    `ds_over` 是给用例改数据源配置的口子（`include_schemas` 那一类）：桩件"有两个库"和
    数据源"允许抽两个库"是两件事，只写前者会被 `run_sync` 的 schema 过滤悄悄裁掉一个。
    """
    acct = await login(username=username)
    ds_id = await _register(client, acct, **(ds_over or {}))
    maker(**stub_kw)
    run = await _sync(client, acct, ds_id)
    assert run.status_code == 202, run.body
    job_id = int(run.body["job_id"])
    return run.body, await _events(session_factory, job_id)


@pytest.fixture
def embedding_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """把 embedding 钉成"没配置"：本机 `.env` 三键为空是**现状**，不是用例的前提。

    不钉住的话，换一台配了端点的机器上 skipped 那条断言就凭空红。
    """
    for key in ("AIWEB_EMBEDDING__BASE_URL", "AIWEB_EMBEDDING__API_KEY", "AIWEB_EMBEDDING__MODEL"):
        monkeypatch.setenv(key, "")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


async def test_一次同步把每一步落成事件行_stage_序列单调不倒退(
    client: AsyncClient,
    login: Login,
    stub: Maker,
    session_factory: Factory,
    embedding_off: None,
) -> None:
    """验收 1：粗档序列是 extract → upsert → card_build → embed → done，seq 从 1 起连续 +1。

    整个列表逐个写死而不是断"没有倒退"：倒退、缺档、多一档都要能分得出来。
    `embed` 那一格在本机永远是 skipped（见下一条），但它照样占一档——前端的进度条
    要知道有这一段存在，否则它会在 card_build 之后直接跳到 done。
    """
    body, events = await _one_run(
        client, login, "ev-seq", session_factory, stub, tables={"shop": ("orders", "users")}
    )
    assert body["status"] == "success", body
    assert [e["seq"] for e in events] == [1, 2, 3, 4, 5], events
    assert [e["stage"] for e in events] == [
        "extract",
        "upsert",
        "card_build",
        "embed",
        "done",
    ], events
    # 细值与粗档同时在场：排障要的是细的那一个，它不许被翻译覆盖掉
    assert [e["phase"] for e in events] == [
        "discover",
        "tables",
        "card_build",
        "embed",
        "done",
    ], events
    assert [e["job_id"] for e in events] == [int(body["job_id"])] * 5


async def test_upsert_事件与元数据同事务_失败的库一条事件都不留(
    client: AsyncClient,
    login: Login,
    stub: Maker,
    session_factory: Factory,
    embedding_off: None,
) -> None:
    """§2.8 的"只追加"要能被撤销：事件行和它所宣布的那次提交在**同一个事务**里。

    两个库、第二个整批抛错：`shop` 提交成功 → 一条 upsert 事件；`crm` rollback →
    它的事件也跟着消失（不是"写了一条再说失败"）。终局 partial，而事件流里那条 upsert
    的 done 恰好是 shop 的对象数——这就是 `run_sync` 不变量 2（"counters 只报确实提交完的
    行数"）在事件层的形状。
    """
    body, events = await _one_run(
        client,
        login,
        "ev-atomic",
        session_factory,
        stub,
        ds_over={"include_schemas": ["shop", "crm"]},
        catalogs=("shop", "crm"),
        tables={"shop": ("orders", "users"), "crm": ("leads",)},
        fail_on="crm",
    )
    assert body["status"] == "partial", body
    upserts = [e for e in events if e["stage"] == "upsert"]
    assert len(upserts) == 1, events
    assert upserts[0]["payload"]["schema"] == "shop", upserts
    assert upserts[0]["counters"]["done"] == 2, upserts
    assert upserts[0]["counters"]["total"] == 3, "分母是整个作业的范围（两个库），不是单库的数"
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1)), "回滚不许留下空洞"


async def test_事件_counters_带全五键_分母来自带_scope_的那次计数(
    client: AsyncClient,
    login: Login,
    stub: Maker,
    session_factory: Factory,
    embedding_off: None,
) -> None:
    """验收 2 的写侧一半：帧负载那五个键在**落库那一刻**就得齐，端点只是把它们投影出去。

    `total`/`base_table`/`view` 是作业开始时那一次带 scope 过滤的计数（2026-09-30 拍板），
    所以第一帧就有分母；`done` 是已提交对象数，从 0 涨到 3。
    """
    _, events = await _one_run(
        client,
        login,
        "ev-counters",
        session_factory,
        stub,
        tables={"shop": ("orders", "users")},
        views={"shop": ("v_report",)},
    )
    first = events[0]["counters"]
    assert set(first) == {"done", "total", "base_table", "view", "cards"}, first
    assert first == {"done": 0, "total": 3, "base_table": 2, "view": 1, "cards": 0}, first
    assert [e["counters"]["done"] for e in events] == [0, 3, 3, 3, 3], events
    # `cards` 在 card_build 那一帧还是 0：那一帧说的是"进了这一步"，卡片还没开始刷。
    # 进度条拿到的是进入时的账，不是预测——把预测写进事实表是这类流最典型的腐烂方式。
    assert events[2]["counters"]["cards"] == 0, events[2]
    assert events[3]["counters"]["cards"] == 3, "两张表 + 一个视图出三张卡"
    assert events[-1]["counters"]["cards"] == 3, events[-1]
    assert events[-1]["stage"] == "done" and events[-1]["counters"]["total"] == 3


@pytest.fixture
def batch_size_three(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """把 `AIWEB_EXTRACT__BATCH_SIZE` 钉成 3——走真配置通道，而不是桩掉 `get_settings`（021 同款）。

    这一格在 020 之前**从来没有读取点**，所以"用例把它设成 3"这句话本身就是被测对象：
    桩掉 `get_settings` 只换掉抽取那一次的读取，`run_sync` 有没有真把配置往下传就永远验不到。
    """
    monkeypatch.setenv("AIWEB_EXTRACT__BATCH_SIZE", "3")
    get_settings.cache_clear()
    try:
        yield
    finally:
        # 不清回去的话，同会话后头的用例会拿着 batch_size=3 继续跑
        get_settings.cache_clear()


async def test_分批的两个配置键真有读取点_每批的表名随_tables_帧交回(
    client: AsyncClient,
    login: Login,
    stub: Maker,
    session_factory: Factory,
    embedding_off: None,
    batch_size_three: None,
) -> None:
    """工单 020 验收 6 的编排层一半：4 张表在 `BATCH_SIZE=3` 下是**两批**，名单进事件。

    期望值手算（4 ÷ 3 = 2 批，切刀 `orders/users/items` + `payments`）。这一条同时钉三件事：
    ① 键有读取点（读不到的话按默认 200 只有 1 批）；② 值穿过 `run_sync` 到达方言；
    ③ 每批的表名进了 `tables` 帧的 `payload`，而**不是**每批一帧——分批不改提交边界（§2.8
    的"事件与元数据同事务"），多出来的帧只会在同一次提交后一起可见，把流拉长却不让进度条提前。
    """
    body, events = await _one_run(
        client,
        login,
        "ev-batch",
        session_factory,
        stub,
        tables={"shop": ("orders", "users", "items", "payments")},
    )
    tables_frame = next(e for e in events if e["phase"] == "tables")
    assert tables_frame["payload"]["batches"] == [
        ["orders", "users", "items"],
        ["payments"],
    ], tables_frame
    assert body["counters"]["batches"] == 2, body
    assert len(events) == 5, "两批仍然只有五帧：一批一帧的话这里是 6"


async def test_embed_档未配置时以_skipped_出现_作业终局仍是_success(
    client: AsyncClient,
    login: Login,
    stub: Maker,
    session_factory: Factory,
    embedding_off: None,
) -> None:
    """验收 6 + spec 故事 11：跳过要**说得出来**，而不是把这一档悄悄删掉。

    `payload` 的形状照 §2.8：`{skipped, code, detail}`。作业终局仍是 success，不是
    partial——"这台机器没配向量端点"不是失败（roadmap 明确 embedding 归 P4）。
    """
    body, events = await _one_run(client, login, "ev-embed", session_factory, stub)
    embed = next(e for e in events if e["stage"] == "embed")
    assert embed["payload"]["skipped"] is True, embed
    assert embed["payload"]["code"] == "embedding_not_configured", embed
    assert embed["payload"]["detail"], "skip 要给一句人话，否则前端只能显示一个 code"
    assert body["status"] == "success", body
    assert [e["stage"] for e in events][-1] == "done"


async def test_作业行被删掉时事件行跟着消失_不留孤儿(
    client: AsyncClient,
    login: Login,
    stub: Maker,
    session_factory: Factory,
    embedding_off: None,
) -> None:
    """§2.8 第 ④ 条的 CASCADE 要在**已经应用的 DDL** 里成立，这一条只能打真库。

    ORM 上写 `ondelete="CASCADE"` 对建表语句没有任何作用，真正带出那句 `ON DELETE CASCADE`
    的是迁移；迁移有没有生效，单测永远看不见。留孤儿的后果是 SSE 端点拿到一个
    "有事件但没有作业"的 job_id，权限那一关就无从判起。

    这里手工删作业行而不是 `DELETE /api/datasources/{id}`：后者是**软删**（§7），
    作业行照样在场，用它试不出这条 CASCADE。
    """
    body, events = await _one_run(
        client, login, "ev-cascade", session_factory, stub, tables={"shop": ("orders", "users")}
    )
    assert body["status"] == "success", body
    assert len(events) == 5, events

    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        await session.execute(
            text(f'delete from "{schema}".sync_jobs where id = :j'), {"j": int(body["job_id"])}
        )
        await session.commit()
    assert await _events(session_factory, int(body["job_id"])) == []
