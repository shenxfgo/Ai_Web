"""进度流的**读侧**（工单 017 的 SSE 半边）：帧、游标补读、心跳、叫醒。

写侧（事件行的形状、seq、与元数据同事务、embed skipped）由 `test_sync_events_pg.py` 钉，
这里一行都不重复；本片钉的是"一个 HTTP 客户端能不能把那些行按顺序、不丢不重地读出来"。

一条决定本片形状的事实：`httpx.ASGITransport` 是**整响应缓冲**的（它把 body 片段攒进 list
才返回 Response，见其 `handle_async_request`），所以从这里发出去的"流式"请求拿不到中间态。
于是分工是——

- 帧的内容、顺序、响应头、游标补读、权限：在这里断（这些流都会自然结束，攒得到头）；
- 心跳：只能**直接驱动生成器**来断（作业一直 pending 的话流不会结束，攒不到头）；
- "首帧早于作业结束"的跨进程版本：只能打真 HTTP 服务，归 `test_sync_events_live.py`。

期望值口径：`docs/metadata-model.md` §2.8（表与游标语义）+ `docs/architecture.md` §6.2
（帧格式、15s 心跳、响应头）与 §7 那行 `GET /sync/jobs/{id}/events` + spec 故事 7/8/9
+ 工单 017 验收 2/3/4。桩件的一轮是 2 表 + 1 视图，所以 `total` 手算就是 **3**。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import deps
from app.core.sse import PING_INTERVAL_S, stream_job_events
from app.deps import make_job_queue
from app.services.job_queue import SYNC_EVENT_CHANNEL
from tests.integration.conftest import Account
from tests.integration.test_sync_pg import (
    StubExtractor,
    _register,
    _sync,
    stub,  # noqa: F401 —— 夹具要 import 进来才在本模块可见
)

pytestmark = pytest.mark.pg

Login = Callable[..., Awaitable[Account]]
Maker = Callable[..., StubExtractor]
Factory = async_sessionmaker[AsyncSession]

STUB_TABLES = {"shop": ("orders", "users")}
STUB_VIEWS = {"shop": ("v_report",)}
EVENT_KEYS = ("stage", "done", "total", "base_table", "view")


@pytest.fixture(autouse=True)
def stream_overridden(application: FastAPI, session_factory: Factory) -> None:
    """把 SSE 自己开的那条会话也指到测试库。

    必须单独 override：流式响应的会话**不是**请求级会话——FastAPI 在端点函数一返回就把请求级
    依赖的退出栈关掉了，而 `StreamingResponse` 的体是在那之后才被迭代的（见
    `deps.get_stream_sessionmaker`）。只 override `get_db` 的话，这条流会静默连到本机 `.env`
    那套真元数据库上去。
    """
    application.dependency_overrides[deps.get_stream_sessionmaker] = lambda: session_factory


@dataclass(frozen=True)
class Frame:
    event: str
    data: dict[str, Any]
    id: int | None


def _frames(body: str) -> list[Frame]:
    """按 SSE 的分帧规则切响应体；注释帧（`: ping`）与 `retry:` 不当帧返回。

    解析器是用例**自己**搓的，于是"帧能被一个独立解析器读出来"这件事本身也被验了一遍。
    它只认本项目会发出的那几种行，多一个字符都不猜。
    """
    out: list[Frame] = []
    for block in body.split("\n\n"):
        if not block.strip() or block.startswith(":") or block.startswith("retry:"):
            continue
        event: str | None = None
        event_id: int | None = None
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data_lines.append(line.removeprefix("data: "))
            elif line.startswith("id: "):
                event_id = int(line.removeprefix("id: "))
        assert event is not None, f"这一帧没有事件名：{block!r}"
        out.append(Frame(event=event, data=json.loads("\n".join(data_lines)), id=event_id))
    return out


def _seqs(frames: list[Frame]) -> list[int | None]:
    """progress 帧的游标序列（`done` 帧没有游标，它不是事件表的一行）。"""
    return [f.id for f in frames if f.event == "progress"]


async def _finished_run(
    client: AsyncClient, login: Login, maker: Maker, username: str
) -> tuple[Account, int]:
    """登记一个源、把一轮跑完（2 表 + 1 视图），返回（身份，job_id）。"""
    acct = await login(username=username)
    ds_id = await _register(client, acct)
    maker(tables=STUB_TABLES, views=STUB_VIEWS)
    run = await _sync(client, acct, ds_id)
    assert run.status_code == 202, run.body
    assert run.body["status"] == "success", run.body
    return acct, int(run.body["job_id"])


async def _enqueued_only(client: AsyncClient, login: Login, username: str) -> tuple[Account, int]:
    """只入队、不消费：那一行停在 `pending`，事件表里一行都没有。

    心跳与叫醒两条都要一个"还没人跑"的作业，而它同时也是验收 1 的另一半证据：
    202 回来之后事件表确实是空的。
    """
    acct = await login(username=username)
    ds_id = await _register(client, acct)
    resp = await client.post("/api/sync/jobs", json={"datasource_id": ds_id}, headers=acct.headers)
    assert resp.status_code == 202, resp.text
    return acct, int(resp.json()["job_id"])


async def _events_of(session_factory: Factory, job_id: int) -> list[dict[str, Any]]:
    """这个作业的事件行，按 seq 升序，只取帧要投影的那几列。

    读它用的是裸 SQL 而不是 `core.sse.read_events`：帧要和**库里的事实**逐条对上，
    拿被验的那条读路径当期望值就是同义反复。
    """
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        f'select seq, stage, phase, counters from "{schema}".sync_job_event '
                        "where job_id = :j order by seq"
                    ),
                    {"j": job_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


async def test_事件流把每一行推成一帧_五键齐在场_收尾是_done(
    client: AsyncClient, login: Login, stub: Maker, session_factory: Factory
) -> None:
    """验收 2 + spec 故事 7：五帧 progress + 一帧 done，顺序与库里逐条对上。

    `total` 是首帧就有分母的那一个（作业开始时那一次带 scope 的计数）：3 = 2 张表 + 1 个视图，
    这个数是用例自己手算的，不是从代码里读出来的。
    """
    acct, job_id = await _finished_run(client, login, stub, "sse-full")
    events = await _events_of(session_factory, job_id)
    assert len(events) == 5, events

    resp = await client.get(f"/api/sync/jobs/{job_id}/events", headers=acct.headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-cache, no-transform"
    assert resp.headers["x-accel-buffering"] == "no"
    # 重连提示排在最前：浏览器只在**断连之后**才用它，所以它必须早于任何事件帧
    assert resp.text.startswith("retry: "), resp.text

    frames = _frames(resp.text)
    assert [f.event for f in frames] == ["progress"] * 5 + ["done"], resp.text
    assert _seqs(frames) == [e["seq"] for e in events]
    assert [f.data["stage"] for f in frames[:5]] == [e["stage"] for e in events]
    assert [f.data["phase"] for f in frames[:5]] == [e["phase"] for e in events]
    for frame, event in zip(frames[:5], events, strict=True):
        assert set(frame.data) >= set(EVENT_KEYS), frame.data
        for key in ("done", "total", "base_table", "view"):
            assert frame.data[key] == event["counters"][key], (frame, event)

    first = frames[0].data
    assert (first["total"], first["base_table"], first["view"], first["done"]) == (3, 2, 1, 0)

    done = frames[-1].data
    assert done["job_id"] == job_id
    assert done["status"] == "success"
    assert isinstance(done["duration_ms"], int) and done["duration_ms"] >= 0, done
    assert frames[-1].id is None, "收尾帧不带游标：它是这条流的结尾，不是事件表的一行"


async def test_重连按游标续读_一条不丢也不重复(
    client: AsyncClient, login: Login, stub: Maker
) -> None:
    """验收 3 + spec 故事 8：游标是客户端带来的，服务端不记任何状态。

    "不丢"与"不重"在这里一次断完：全量是 [1..5]，从 2 续是 [3,4,5]，前面拿到的 [1,2] 加上
    补读的 [3,4,5] 正好是不重不漏的全集。这是选事件表而不是纯推 NOTIFY 的全部理由——
    NOTIFY 丢了就是永远丢了，表里那一行却还在。

    最后两格定的是**优先级与兜底**：查询参数赢过 `Last-Event-ID`（前端重连时两个都带是合法
    写法，次序必须说得出一个），而认不出来的游标一律归 0 从头补读（宁可重发也不漏，
    事件表是 append-only 的，前端按 seq 去重就行）。
    """
    acct, job_id = await _finished_run(client, login, stub, "sse-cursor")
    url = f"/api/sync/jobs/{job_id}/events"

    full = _frames((await client.get(url, headers=acct.headers)).text)
    assert _seqs(full) == [1, 2, 3, 4, 5]

    tail = _frames((await client.get(f"{url}?cursor=2", headers=acct.headers)).text)
    assert _seqs(tail) == [3, 4, 5], "从游标之后续，那一帧本身不重发"
    assert sorted([*_seqs(full[:2]), *_seqs(tail)]) == [1, 2, 3, 4, 5]

    by_header = _frames(
        (await client.get(url, headers={**acct.headers, "Last-Event-ID": "2"})).text
    )
    assert _seqs(by_header) == [3, 4, 5], "EventSource 只会自动带这个头，必须同样认"

    query_wins = _frames(
        (await client.get(f"{url}?cursor=4", headers={**acct.headers, "Last-Event-ID": "2"})).text
    )
    assert _seqs(query_wins) == [5]

    garbage = _frames(
        (await client.get(url, headers={**acct.headers, "Last-Event-ID": "not-a-seq"})).text
    )
    assert _seqs(garbage) == [1, 2, 3, 4, 5], "认不出来就从头，而不是报错或当成一个很大的数"


async def test_客户端已经追平时给一个干净的结尾_不挂在心跳上(
    client: AsyncClient, login: Login, stub: Maker
) -> None:
    """重连带上"我已经读到最后一条"时，这条流必须**当场结束**。

    少这一条的话表现是最难查的那种：连接活着、帧也在发（每 15s 一个 ping），但永远等不到
    `event: done`，前端的进度条就停在 100% 转圈。判据用 `sync_jobs.finished_at`（它和终局
    那条事件行是同一个事务写下的），而不是在 `status` 那三个值上再抄一份词表。
    """
    acct, job_id = await _finished_run(client, login, stub, "sse-caught")
    resp = await client.get(f"/api/sync/jobs/{job_id}/events?cursor=5", headers=acct.headers)
    assert resp.status_code == 200, resp.text
    frames = _frames(resp.text)
    assert [f.event for f in frames] == ["done"], resp.text
    assert frames[0].data["status"] == "success"


async def test_鉴权与发起同一把尺_别人的作业_没令牌_没这个作业(
    client: AsyncClient, login: Login, stub: Maker
) -> None:
    """工单 017 的"同一把尺"落到读侧：这条流会把 `errors[].detail` 的原文推给客户端，
    能读进度就能读出错提示里的源库信息，所以它不能因为"只是个只读 GET"就放松。

    三种进不来都在**返回响应之前**判掉：状态码在流开始之后就没法补发了，那是这类端点
    最容易写错的一处（先开流、再判权，权限就只剩装饰器的样子）。
    """
    acct, job_id = await _finished_run(client, login, stub, "sse-owner")
    stranger = await login(username="sse-stranger")
    url = f"/api/sync/jobs/{job_id}/events"

    resp = await client.get(url, headers=stranger.headers)
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "forbidden"

    resp = await client.get(url)
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "unauthorized"

    resp = await client.get("/api/sync/jobs/999999/events", headers=acct.headers)
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "not_found"


async def test_没有新事件时每窗吐一帧_ping(
    client: AsyncClient, login: Login, session_factory: Factory
) -> None:
    """验收 4 + spec 故事 9：心跳靠的是注释帧，不是"总会有新事件"。

    窗口设到 20ms（工单原话是"不靠真等 15s"），而 15 这个数**只断不出等**：它是 §6.2 的
    口径，写成断言才会在有人改成 5s 或 60s 时留下一次红。

    这里不走 HTTP 而是直接驱动生成器：`ASGITransport` 要攒完整响应才返回，而一条只入队、
    没有 worker 来跑的作业永远不收尾，用它发 HTTP 是把用例挂死而不是验东西。跨进程时序
    归 `test_sync_events_live.py`，但**心跳不在那一条里**——live 跑的是有推进的作业，
    15s 一帧的注释行在那条流上不会出现，要断它就得让一条流真空等 15s。
    """
    assert PING_INTERVAL_S == 15.0, "§6.2：远端链路 idle 超时之前至少要有一帧"
    _acct, job_id = await _enqueued_only(client, login, "sse-ping")
    async with session_factory() as session:
        wake = await make_job_queue(session).subscribe(job_id)
        agen = stream_job_events(session, wake, job_id, 0, ping_after_s=0.02)
        frames: list[str] = []
        try:
            for _ in range(3):
                frames.append(await asyncio.wait_for(agen.__anext__(), 5))
        finally:
            await agen.aclose()
            await wake.aclose()
    # 字面而不是 `sse_ping()`：期望值要从独立真相来，否则实现改成吐什么都不算红
    assert frames == [": ping\n\n"] * 3


async def test_NOTIFY_叫醒不必等心跳就把那一帧送出来(
    client: AsyncClient, login: Login, session_factory: Factory
) -> None:
    """叫醒这条链是"进度条为什么会动"的答案：写侧那一声与读侧这一听必须对上同一个通道名。

    窗口给 60s、等待只给 10s：两侧通道名写错一个字母时，这里不会红在断言上，而是红在超时上
    ——这正是它必须存在的理由（`SYNC_EVENT_CHANNEL` 住在缝上，但缝也可以被两侧各自绕过）。
    同理，NOTIFY 与插入必须**同事务**：先提交后 notify 之间有个窗口，那段窗口里的读到的
    还是上一帧。

    事件行是手写裸 SQL 而不是走 `_set_phase`：这一条要钉的是那一声，事件行的形状与写侧入口
    另有 `test_sync_events_pg.py` 的五条用例。
    """
    _acct, job_id = await _enqueued_only(client, login, "sse-wake")
    async with session_factory() as session:
        wake = await make_job_queue(session).subscribe(job_id)
        agen = stream_job_events(session, wake, job_id, 0, ping_after_s=60.0)
        pull = asyncio.create_task(agen.__anext__())
        try:
            await asyncio.sleep(0.05)  # 让生成器先进到 wait()，再去写那一行
            await _write_event_and_notify(session_factory, job_id)
            frame = await asyncio.wait_for(pull, 10)
        finally:
            await agen.aclose()
            await wake.aclose()

    (got,) = _frames(frame)
    assert got.event == "progress" and got.id == 1, frame
    assert got.data["stage"] == "extract" and got.data["phase"] == "discover"
    assert (got.data["total"], got.data["done"]) == (3, 0)


async def _write_event_and_notify(factory: Factory, job_id: int) -> None:
    """在**另一条连接**上追加一行事件并同事务发一声 NOTIFY（写侧的最小替身）。"""
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    counters = {"done": 0, "total": 3, "base_table": 2, "view": 1, "cards": 0}
    async with factory() as session:
        await session.execute(
            text(
                f'insert into "{schema}".sync_job_event '
                "(job_id, seq, stage, phase, counters, payload) "
                "values (:j, 1, :stage, :phase, cast(:c as jsonb), '{}'::jsonb)"
            ),
            {"j": job_id, "stage": "extract", "phase": "discover", "c": json.dumps(counters)},
        )
        await session.execute(
            text("select pg_notify(:chan, :pid)"),
            {"chan": SYNC_EVENT_CHANNEL, "pid": str(job_id)},
        )
        await session.commit()
