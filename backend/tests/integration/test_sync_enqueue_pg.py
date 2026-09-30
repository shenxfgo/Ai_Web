"""进程分离后的新接缝：入队 202 + claim 的行数判定（工单 016）。

这一片的存在理由是"**请求不再等抽取跑完**"这件事只能在这一层验：
P2 的三张 sync 用例管的是"跑完之后计数对不对"，那些断言本体到本片一行不改；
这里管的是"响应返回的那一刻，库里那一行处于什么状态"，以及"同一个作业不会被两个
worker 拿到"——后者是 ADR-0011 换来的全部风险所在。

桩件与 `_register`/`_scalar`/`stub` 夹具来自 `test_sync_pg.py`（那片已经把"抽取中途炸了"
的前提搭好了，这里只是换一个目标问它）。

期望值口径：docs/adr/0011 + docs/metadata-model.md §6 + 工单 016 验收 1/2/4。
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.job_queue import PostgresJobQueue
from tests.conftest import load_script
from tests.integration.test_sync_pg import (
    StubExtractor,
    _register,
    _scalar,
    stub,  # noqa: F401 —— 夹具要 import 进来才在本模块可见
)

pytestmark = pytest.mark.pg

Login = Callable[..., Awaitable[Any]]
# `scripts/` 不是包，按文件路径加载（理由见 tests/conftest.py 的 `load_script`）
RUN_WORKER = load_script(
    "run_worker_script", Path(__file__).resolve().parents[2] / "scripts" / "run_worker.py"
)


async def _post(client: AsyncClient, acct: Any, ds_id: int) -> Any:
    return await client.post("/api/sync/jobs", json={"datasource_id": ds_id}, headers=acct.headers)


async def _job_row(
    session_factory: async_sessionmaker[AsyncSession], job_id: int
) -> dict[str, Any]:
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(f'select * from "{schema}".sync_jobs where id = :id'), {"id": job_id}
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1, f"job {job_id} 在库里应当只有一行，实得 {len(rows)}"
    return dict(rows[0])


async def test_发起同步立刻回_202_而那一轮还没开始跑(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """验收 1：202 + 只有 `{job_id}`，并且**响应返回时作业尚未跑完**。

    三条断言各挡一种假 202：`status_code` 挡"只改了返回码"；`keys()` 挡"顺手把 P2 那套
    计数塞进同一个响应体"（那是 017 的收尾帧，不是这一帧）；`started_at is None` 挡
    "入队时就替 worker 把钟点上"——钟没点上，才证明执行这一步不在请求里。
    """
    acct = await login(username="q-202", role="member")
    ds_id = await _register(client, acct)
    extractor = stub()

    resp = await _post(client, acct, ds_id)
    assert resp.status_code == 202, resp.text
    assert resp.json().keys() == {"job_id"}, resp.json()

    row = await _job_row(session_factory, int(resp.json()["job_id"]))
    assert row["status"] == "pending", row
    assert row["started_at"] is None, row
    assert row["finished_at"] is None, row
    assert extractor.calls == [], "请求内一次都没该碰源库：真碰了就是 202 写得假"
    stored = await _scalar(
        session_factory,
        'select count(*) from "{s}".meta_table where datasource_id = :ds',
        ds=ds_id,
    )
    assert stored in (0, None), stored


async def test_排队中的_pending_行也挡住新作业(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """验收 2：第二次 POST 的 409 只能来自 `ux_sync_running`，因为代码里没有那个分支。

    这条断言的全部力量在"pending"这一格：`enqueue` 不查"有没有未结束的作业"，
    它只往表里插一行——所以能把 pending 挡住的只有那个部分唯一索引（它的 WHERE 里
    写着 `status IN ('pending','running')`，见 0004_meta.py:119-126）。
    把索引的 WHERE 改成只剩 'running'，这条用例照红，而任何代码分支都替代不了它。
    """
    acct = await login(username="q-409", role="member")
    ds_id = await _register(client, acct)
    extractor = stub()

    first = await _post(client, acct, ds_id)
    assert first.status_code == 202, first.text
    second = await _post(client, acct, ds_id)
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "sync_already_running", second.text
    assert extractor.calls == [], "撞锁的这次连方言都不该拿到"

    pending = await _scalar(
        session_factory,
        'select count(*) from "{s}".sync_jobs where datasource_id = :ds',
        ds=ds_id,
    )
    assert pending == 1, "第二个 job 不能落库，否则部分唯一索引形同虚设"


async def test_claim_领走最老的_pending_并把这一轮的钟点上(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """claim 是"入队"与"开跑"之间唯一的那一步：状态、钟、领到的三样都在这条上钉。"""
    acct = await login(username="q-claim", role="member")
    ds_id = await _register(client, acct)
    stub()
    job_id = int((await _post(client, acct, ds_id)).json()["job_id"])

    async with session_factory() as session:
        claimed = await PostgresJobQueue(session).claim()
    assert claimed is not None
    assert (claimed.job_id, claimed.datasource_id) == (job_id, ds_id), claimed
    assert claimed.started_at is not None, "run_sync 的本轮起钟必须由 claim 点上"

    row = await _job_row(session_factory, job_id)
    assert row["status"] == "running", row
    assert row["phase"] == "connect", row
    assert row["finished_at"] is None, row
    # 工单 016 的口径：本片不写心跳，heartbeat_at 只在 claim 那一次赋值
    assert row["heartbeat_at"] is not None and row["heartbeat_at"] == row["started_at"], row


async def test_同一个作业不会被两个_worker_领走(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """验收 4 的"已被别的 worker claim"那一半：第二次 claim 拿到 None，且那行没被改坏。

    两个 worker 各自开一个会话（等价于两个进程的连接池），第一个领走之后第二个只能空手。
    "不抛"是必须的——常驻循环里一次空领是常态，不是异常。
    """
    acct = await login(username="q-two-workers", role="member")
    ds_id = await _register(client, acct)
    stub()
    job_id = int((await _post(client, acct, ds_id)).json()["job_id"])

    async with session_factory() as first:
        got = await PostgresJobQueue(first).claim()
    async with session_factory() as second:
        again = await PostgresJobQueue(second).claim()
    assert got is not None and got.job_id == job_id, got
    assert again is None, "已被领走的作业不许第二个 worker 再拿到"

    row = await _job_row(session_factory, job_id)
    assert row["status"] == "running" and row["phase"] == "connect", row
    assert row["counters"] == {}, row
    assert row["errors"] == [] and row["warnings"] == [], row


async def test_源被删掉之后队列里就没有这一格(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """验收 4 的"作业不存在"那一半，在进程内能观察到的形态是**队列里没有它**。

    删除是级联的（`sync_jobs.datasource_id` ON DELETE CASCADE），所以源一走、pending 行
    跟着走，`claim` 根本不会把它交给 worker。这条用例钉的两件事因此都是负的：
    `run_once` 不抛（常驻进程不能被一次空领带走），以及库里不留活口（不会有孤行锁死这条源）。
    """
    acct = await login(username="q-gone", role="member")
    ds_id = await _register(client, acct)
    stub()
    job_id = int((await _post(client, acct, ds_id)).json()["job_id"])

    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        await session.execute(
            text(f'delete from "{schema}".data_sources where id = :ds'), {"ds": ds_id}
        )
        await session.commit()

    async with session_factory() as session:
        assert await RUN_WORKER.run_once(session) is None, (
            "删掉的源不该让 worker 抛，也不该留下活口"
        )
    left = await _scalar(
        session_factory, 'select count(*) from "{s}".sync_jobs where id = :id', id=job_id
    )
    assert left == 0, "级联应当已经带走那一行"


async def test_worker_跑完之后终局那三样都对(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """验收 3：入队 → `run_once` → 那一行的 `status`/`finished_at`/`counters` 是终局。

    这一条是进程分离之后**唯一的成功判据**：请求已经不管作业了，所以"同步完成了"这件事
    只能从库里那一行读出来。计数口径原样继承 007（失败的 catalog 一行都不许进总账）。
    """
    acct = await login(username="q-terminal", role="member")
    ds_id = await _register(client, acct, include_schemas=["shop", "warehouse"])
    extractor = stub(
        catalogs=("shop", "warehouse"),
        tables={"shop": ("orders", "users")},
        fail_on="warehouse",
    )
    job_id = int((await _post(client, acct, ds_id)).json()["job_id"])

    async with session_factory() as session:
        assert await RUN_WORKER.run_once(session) == job_id

    row = await _job_row(session_factory, job_id)
    assert row["status"] == "partial", row
    assert row["phase"] == "done", row
    assert row["finished_at"] is not None, row
    assert row["counters"]["tables"] == 2, "第一个库写完 2 张，第二个库一行都没落"
    assert row["counters"]["tables_failed"] == 1, row["counters"]
    assert [e["code"] for e in row["errors"]] == ["extract_failed"], row["errors"]
    assert extractor.calls == ["shop", "warehouse"], "worker 跑的是整条 run_sync，不是半个"


async def test_claim_出来的顺序就是入队的顺序(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """三条作业按 A→B→C 入队，claim 三次必须按 A→B→C 出来。

    这一条单独看**不足以**钉住"漏了 `ORDER BY id` 会怎样"——刚插进堆表的行，PG 顺手也
    大抵按插入序返回。真正的守门人是 `tests/unit/test_job_queue.py` 里那句
    `"ORDER BY id LIMIT 1" in sql`：那里红了才是"先来后到"丢了。这里补的是另一头：
    多个源同时在排时，先到先得这件事在真库上成立。
    """
    acct = await login(username="q-fifo", role="member")
    stub()
    job_ids = []
    for i in range(3):
        ds_id = await _register(client, acct, name=f"src-{i}", host=f"h{i}.invalid")
        job_ids.append(int((await _post(client, acct, ds_id)).json()["job_id"]))

    async with session_factory() as session:
        queue = PostgresJobQueue(session)
        # 多领一次：第四次必须是 None，否则"三条作业都还在队列里"这个前提就没被否定
        claimed = [(await queue.claim()) for _ in range(4)]
    assert [c.job_id if c is not None else None for c in claimed] == [*job_ids, None], claimed
