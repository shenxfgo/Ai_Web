"""worker 心跳与回收的**行为**（工单 018）：独立连接可见性、一轮回收、保留期、不误杀边界。

三条语句的**形状**已由 `tests/unit/test_worker_reclaim.py` 钉住；这一份钉的是它们在真库上
跑起来的样子——PG 的时钟、独立连接的可见性、`RETURNING` 定胜负、那条 DELETE 真的只碰事件表。
"跑一轮循环"在这里就是 await 一次 `heartbeat_tick`（工单 016 的已定口径：用例不真起子进程），
**不走重启路径**——回收必须由"扫到过"证明，而不是"重启时顺手改的"（工单 018 验收 2 原话）。

期望值口径：docs/metadata-model.md §6（回收那一行的 SQL 原文、实现约束 1、as-built 第 4/9 条）
+ 工单 018「已定口径」与验收 1/2/4/5 + 工单 017「未证明的一格」（NOTIFY 丢失的自愈，转 018 补）。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.sse import stream_job_events
from app.deps import make_job_queue
from tests.conftest import load_script
from tests.integration.test_sync_pg import _register

pytestmark = pytest.mark.pg

Login = Callable[..., Awaitable[Any]]
Factory = async_sessionmaker[AsyncSession]

RUN_WORKER = load_script(
    "run_worker_for_heartbeat_tests",
    Path(__file__).resolve().parents[2] / "scripts" / "run_worker.py",
)


def _worker_factory() -> tuple[async_sessionmaker[AsyncSession], Any]:
    """worker 侧的独立连接工厂（NullPool）；返回（工厂，engine）供用例自己 dispose。

    独立成一条缝而不是复用 `session_factory`：验收 1 要证的正是"心跳走另一条连接"，
    两侧共用引擎的话，NullPool 下确实是两条连接、真池子下可能就是同一条——用例不该赌池子的
    行为（与 `test_sync_pg._sync` 给 worker 单开 engine 同一条理由）。
    """
    engine = create_async_engine(os.environ["AIWEB_PG_TEST_DSN"], poolclass=NullPool)
    return async_sessionmaker(engine, expire_on_commit=False), engine


async def _mk_job(
    session_factory: Factory, ds_id: int, *, status: str = "running", heartbeat: str = "now()"
) -> int:
    """手工造一行未结束的 job，心跳钟点给成 SQL 表达式（如 `now() - interval '20 minutes'`）。

    走裸 SQL 而不是 API：要摆的姿势（心跳是 20 分钟前）不是任何公开接口做得出的动作，
    而工单验收 2 明说"手工把某行的 `heartbeat_at` 改成 20 分钟前"。
    """
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        job_id = (
            await session.execute(
                text(
                    f'insert into "{schema}".sync_jobs (datasource_id, status, phase,'
                    f" started_at, heartbeat_at) values (:ds, :st, 'tables', now(), {heartbeat})"
                    " returning id"
                ),
                {"ds": ds_id, "st": status},
            )
        ).scalar_one()
        await session.commit()
    return int(job_id)


async def _job_row(session_factory: Factory, job_id: int) -> dict[str, Any]:
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(f'select * from "{schema}".sync_jobs where id = :j'), {"j": job_id}
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1, rows
    return dict(rows[0])


async def _events_of(session_factory: Factory, job_id: int) -> list[dict[str, Any]]:
    """这个作业的事件行，按 seq 升序（裸 SQL 读库里的事实，不复用被验的写路径）。"""
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


async def test_一轮循环把心跳超时的_running_改判_failed_并补上终局事件(
    client: AsyncClient, login: Login, session_factory: Factory
) -> None:
    """验收 2：手工把 `heartbeat_at` 摆到 20 分钟前、`status` 保持 `running`，跑**一轮** →
    该行 `failed` + `errors` 里出现带 `reclaimed` 码的一条。不走重启路径（工单原话：
    否则证明的是"重启顺手改的"而不是"扫到过"）。

    终局事件那一条是工单 018 目标 2 的话——"让正在读流的前端能收尾"：补的是 017 的形状
    （粗档 `done`、seq 在该作业现有事件之后连续），counters 沿用该作业最后一帧的账
    （回收者不知道中途进度，抄上一帧是唯一不撒谎的取值；一帧都没有才归零）。
    """
    acct = await login(username="reclaim-run")
    ds_id = await _register(client, acct)
    job_id = await _mk_job(session_factory, ds_id, heartbeat="now() - interval '20 minutes'")

    hb_factory, hb_engine = _worker_factory()
    try:
        report = await RUN_WORKER.heartbeat_tick(
            hb_factory, own_job_id=None, stale_seconds=180, retention_days=30
        )
    finally:
        await hb_engine.dispose()

    assert report.reclaimed == [job_id], report
    row = await _job_row(session_factory, job_id)
    assert row["status"] == "failed", row
    assert row["errors"] == [{"code": "reclaimed", "detail": "heartbeat 超时"}], row
    assert row["finished_at"] is not None, row
    events = await _events_of(session_factory, job_id)
    assert [(e["seq"], e["stage"], e["phase"]) for e in events] == [(1, "done", "done")], events
    assert events[0]["payload"] == {"code": "reclaimed", "detail": "heartbeat 超时"}, events


def _results_snapshot() -> list[tuple[str, int, int]]:
    """`data/results/` 的清单快照：(文件名, 字节数, mtime_ns)。不读内容、不写任何东西。

    验收 4 要的是"回收前后逐字节相同"——名字+大小+修改时间三样全同，就是"一个字节都不动"
    在不打开文件前提下的最强证据（打开读反而可能碰 atime）。快照取的是**这个工作树自己的**
    `data/results/`：用例只 stat 不读写，那个目录里 P2 验收 4/5 的现场证据一个字节都不会少。
    """
    root = Path(__file__).resolve().parents[3] / "data" / "results"
    if not root.exists():
        return []
    entries: list[tuple[str, int, int]] = []
    for p in root.iterdir():
        st = p.stat()
        entries.append((p.name, st.st_size, st.st_mtime_ns))
    return sorted(entries)


async def test_NOTIFY_丢失时一条流最迟一个心跳周期仍读到那一行(
    client: AsyncClient, login: Login, session_factory: Factory
) -> None:
    """工单 017「未证明的一格」转给 018 的那笔账：直接往 `sync_job_event` 插一行而**不**
    `pg_notify`，断言一条流仍在一条心跳周期内读到它——"推理成立但没人真把叫醒打掉"到此闭环。

    它与 017 的 `test_NOTIFY_叫醒不必等心跳就把那一帧送出来` 互为反面：那一条把心跳路堵死
    只留叫醒，这一条把叫醒打掉只留心跳（订阅是真的 `subscribe`，只是写侧不发那一声，
    这一觉永远等不来）。读侧代码本片一个字没改——这格要验的本来就是 017 交付的自愈路径。
    窗口 50ms，不真等 15s（与 017 心跳用例同一个手法）。
    """
    acct = await login(username="notify-lost")
    ds_id = await _register(client, acct)
    resp = await client.post("/api/sync/jobs", json={"datasource_id": ds_id}, headers=acct.headers)
    assert resp.status_code == 202, resp.text
    job_id = int(resp.json()["job_id"])

    async with session_factory() as session:
        wake = await make_job_queue(session).subscribe(job_id)
        agen = stream_job_events(session, wake, job_id, 0, ping_after_s=0.05)
        try:
            pull = asyncio.create_task(agen.__anext__())
            await asyncio.sleep(0.02)  # 让生成器先进到 wait()，再去写那一行
            await _write_event_without_notify(session_factory, job_id)
            first = await asyncio.wait_for(pull, 10)
            assert first == ": ping\n\n", "叫醒确实被打掉了：第一帧只能是心跳"
            second = await asyncio.wait_for(agen.__anext__(), 10)
        finally:
            await agen.aclose()
            await wake.aclose()
        assert second.startswith("id: 1\nevent: progress\n"), second
        assert '"stage": "extract"' in second, second


async def _write_event_without_notify(session_factory: Factory, job_id: int) -> None:
    """在另一条连接上追加一行事件、**不发** `pg_notify`——"那一声丢了"的最小构造。

    与 017 的 `_write_event_and_notify` 只差那一声：形状（五键 counters、stage/phase 字面）
    照抄它的独立真相来源 `test_sync_events_pg.py` 那一份，事件行本身不是本条要验的东西。
    """
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    counters = {"done": 0, "total": 3, "base_table": 2, "view": 1, "cards": 0}
    async with session_factory() as session:
        await session.execute(
            text(
                f'insert into "{schema}".sync_job_event '
                "(job_id, seq, stage, phase, counters, payload) "
                "values (:j, 1, :stage, :phase, cast(:c as jsonb), '{}'::jsonb)"
            ),
            {"j": job_id, "stage": "extract", "phase": "discover", "c": json.dumps(counters)},
        )
        await session.commit()


async def test_保留期删过期事件行_未过期不删_结果csv逐字节不动(
    client: AsyncClient, login: Login, session_factory: Factory
) -> None:
    """验收 4：插一行 `created_at` 早于 31 天的事件 → 被同一轮循环删掉；未过期的不删。

    31 天与 30 天（`retention_days` 默认值）留了一天余量，判据是 `created_at` 而不是
    作业终局时间——事件表没有别的钟可用，而已定口径把删除范围限死在这一张表：
    `data/results/` 的 csv 是 P2 验收 4/5 的现场证据，删文件不可逆（§6 as-built 第 9 条）。
    """
    acct = await login(username="retention")
    ds_id = await _register(client, acct)
    job_id = await _mk_job(session_factory, ds_id, heartbeat="now() - interval '5 seconds'")
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        await session.execute(
            text(
                f'insert into "{schema}".sync_job_event'
                " (job_id, seq, stage, phase, counters, payload, created_at)"
                " values (:j, 1, 'extract', 'connect', '{}'::jsonb, '{}'::jsonb,"
                " now() - interval '31 days')"
            ),
            {"j": job_id},
        )
        await session.execute(
            text(
                f'insert into "{schema}".sync_job_event'
                " (job_id, seq, stage, phase, counters, payload)"
                " values (:j, 2, 'upsert', 'tables', '{}'::jsonb, '{}'::jsonb)"
            ),
            {"j": job_id},
        )
        await session.commit()

    before = _results_snapshot()
    hb_factory, hb_engine = _worker_factory()
    try:
        report = await RUN_WORKER.heartbeat_tick(
            hb_factory, own_job_id=None, stale_seconds=180, retention_days=30
        )
    finally:
        await hb_engine.dispose()

    assert report.pruned == 1, report
    events = await _events_of(session_factory, job_id)
    assert [e["seq"] for e in events] == [2], "过期的删了，未过期的一行不许少"
    assert _results_snapshot() == before, "回收动了 data/results/ —— 已定口径不许碰"


async def test_边界_179秒不动_181秒判失败_pending_也算僵尸候选(
    client: AsyncClient, login: Login, session_factory: Factory
) -> None:
    """验收 5 + 已定口径第 3 条：阈值边界两侧各一行，`pending` 与 `running` 同判。

    五行同一条用例、同一次 tick，为的是"不误杀"与"要杀"由**同一轮扫描**当场分判：

    - `running` @179s：阈值外 1 秒，不许动（边界必须严：`<` 写成 `<=` 这里还是绿的，
      但 181s 那一行保证阈值内侧照样杀，两侧合起来把比较符钉死）。
    - `running` @now：新鲜心跳，正是"作业还在动"的样子，误杀它等于制造假 failed。
    - `pending` @181s：已定口径"`pending` 行也算僵尸候选"。真实路径上 pending 行没有钟
      （claim 时才点上），所以这一行只能手工摆——摆出来才验得到谓词真的带上了 `pending`。
    - `pending` @NULL：入队后从没被领取的行。它**不该**被回收：比较匹配不上 NULL，
      判据是"心跳超时"而不是"在队列里躺久了"——躺久的 pending 说明没有 worker，
      而起 worker 是运维动作，不是把别人的待办改判 failed 的理由。
    - `running` @181s：对照组，同轮必须被收。
    """
    acct = await login(username="reclaim-edge")
    # 数据源名全库唯一（§2.3），五个源要五个名字；一行 job 一个源，互不挡 `ux_sync_running`
    ids = [await _register(client, acct, name=f"edge-{i}") for i in range(5)]
    keep_179 = await _mk_job(session_factory, ids[0], heartbeat="now() - interval '179 seconds'")
    keep_fresh = await _mk_job(session_factory, ids[1], heartbeat="now()")
    kill_pending = await _mk_job(
        session_factory, ids[2], status="pending", heartbeat="now() - interval '181 seconds'"
    )
    keep_pending_null = await _mk_job(session_factory, ids[3], status="pending", heartbeat="NULL")
    kill_running = await _mk_job(
        session_factory, ids[4], heartbeat="now() - interval '181 seconds'"
    )

    hb_factory, hb_engine = _worker_factory()
    try:
        report = await RUN_WORKER.heartbeat_tick(
            hb_factory, own_job_id=None, stale_seconds=180, retention_days=30
        )
    finally:
        await hb_engine.dispose()

    assert sorted(report.reclaimed) == sorted([kill_pending, kill_running]), report
    for job_id in (keep_179, keep_fresh, keep_pending_null):
        row = await _job_row(session_factory, job_id)
        assert row["status"] in ("pending", "running"), f"误杀 {job_id}：{row}"
    for job_id in (kill_pending, kill_running):
        row = await _job_row(session_factory, job_id)
        assert row["status"] == "failed", row
        assert [e["code"] for e in row["errors"]] == ["reclaimed"], row
        # 终局事件按 017 的形状补齐：被回收的 pending 一行旧事件都没有，seq 从 1 起
        events = await _events_of(session_factory, job_id)
        assert [(e["seq"], e["stage"]) for e in events] == [(1, "done")], events
    # 回收释放 `ux_sync_running`：僵尸行占住的部分唯一索引从此挡不住新作业——
    # 这正是"存盘打断同步会自愈"的那一格（不用手工进库删行）。
    resp = await client.post("/api/sync/jobs", json={"datasource_id": ids[4]}, headers=acct.headers)
    assert resp.status_code == 202, resp.text


async def test_主事务未提交时心跳换个连接就已可见(
    client: AsyncClient, login: Login, session_factory: Factory
) -> None:
    """验收 1（工单 018 + metadata-model §6 实现约束 1）：独立连接是这句话的全部内容。

    "主事务"在这里用**另一条会话上的一个未提交事务**替身——它写的是事件表的一行，而不是
    `sync_jobs` 自己：`run_sync` 每库一个事务时锁的是 meta_* 的行，job 行的钟不归它管，
    所以心跳 UPDATE 不该被它挡住（被挡住的话这条用例就会挂死，那也是要暴露的错）。
    两轮 tick 之间 `heartbeat_at` 必须变——"每约 10s 变化一次"的"变化"由循环每一轮负责，
    间隔本身由 `Settings.extract.heartbeat_interval_s` 说话（验收 6 的 grep 证据 + 循环本体）。
    """
    acct = await login(username="hb-visible")
    ds_id = await _register(client, acct)
    job_id = await _mk_job(session_factory, ds_id)

    hb_factory, hb_engine = _worker_factory()
    try:
        main = session_factory()
        async with main.begin():
            # "主事务"开着，里面躺着一条还没提交的事件行
            await main.execute(
                text(
                    f'insert into "{os.environ["AIWEB_PG__SCHEMA_NAME"]}".sync_job_event'
                    " (job_id, seq, stage, phase, counters, payload)"
                    " values (:j, 1, 'upsert', 'tables', '{}'::jsonb, '{}'::jsonb)"
                ),
                {"j": job_id},
            )
            before = (await _job_row(session_factory, job_id))["heartbeat_at"]
            report = await RUN_WORKER.heartbeat_tick(
                hb_factory, own_job_id=job_id, stale_seconds=180, retention_days=30
            )
            mid = (await _job_row(session_factory, job_id))["heartbeat_at"]
            assert report.own_beaten == 1, report
            assert mid > before, "主事务未提交时，换一个连接就该读到刷新后的钟"
            report2 = await RUN_WORKER.heartbeat_tick(
                hb_factory, own_job_id=job_id, stale_seconds=180, retention_days=30
            )
            after = (await _job_row(session_factory, job_id))["heartbeat_at"]
            assert report2.own_beaten == 1 and after > mid, "每轮都要把钟推到'现在'，不是一次性的"
            # 反向证据：主事务真的还没提交——它写的那行事件对读侧不可见
            schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
            async with session_factory() as reader:
                visible = (
                    await reader.execute(
                        text(f'select count(*) from "{schema}".sync_job_event where job_id = :j'),
                        {"j": job_id},
                    )
                ).scalar_one()
            assert visible == 0, "上面读到的心跳必须来自未提交主事务**之外**的连接"
        await main.rollback()
    finally:
        await hb_engine.dispose()
        await main.close()
