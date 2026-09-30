"""真进程边界上的进度流（工单 017 验收 7）：子进程 worker + 子进程 API + 真 HTTP 流式读。

为什么这一条**不能**用 ASGI 客户端跑：`httpx.ASGITransport` 把 body 片段攒进一个 list 才
返回 `Response`（它自己的 `handle_async_request` 就是这么写的），于是"首帧在作业结束之前
到达"这句话在那条传输上没有对应的事实——服务端怎么流都一样。而 `resp.is_closed` 在那个
客户端上更是恒真（响应早就攒完了）。这里三个进程各就各位，就是本机 `dev.ps1 dev` +
`dev.ps1 worker` 的那个形状，pytest 只当那个"盯着进度条的人"。

分工（剩下的半边在 `test_sync_events_sse_pg.py`，那里攒得到头也不需要跨进程）：
帧内容与游标补读、心跳、鉴权、叫醒 → 那一份；**跨进程的进入方式**、演示库上的真实分母
（`total=10`，不是 `SHOW FULL TABLES` 数出来的 11）→ 这一份。

期望值口径：verification §1（9 表 + 1 视图、宽表 68 列 → 12 张卡）+ metadata-model §2.8
（每档一帧、counters 那五个键）+ architecture §6.2（`retry:`、响应头、15s 心跳）+
工单 017 验收 1/2/6/7。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import Account, DbAccount
from tests.integration.test_sync_events_sse_pg import EVENT_KEYS, Frame, _frames
from tests.integration.test_sync_live import TABLES, VIEWS
from tests.integration.test_sync_pg import _register

pytestmark = [pytest.mark.pg, pytest.mark.live]

BACKEND_ROOT = Path(__file__).resolve().parents[2]
RUN_WORKER = BACKEND_ROOT / "scripts" / "run_worker.py"
DEMO_DB = "ai_web_demo"
Login = Callable[..., Awaitable[Account]]
Factory = async_sessionmaker[AsyncSession]

# 一轮真同步（单库源）的粗档序列：discover→extract、tables→upsert、card_build、embed、done。
# 每档恰好一帧是 §2.8 的写法，不是碰巧——那三个细值与粗档同名，所以这里没有重复项。
STAGES = ["extract", "upsert", "card_build", "embed", "done"]
TOTAL = TABLES + VIEWS  # 10：verification §1 的口径（下划线前缀的对象被源的 exclude 挡掉）
CARDS = TOTAL + 2  # 宽表 68 列 > 40 → 主卡 + 2 张切片（`test_kb_cards_live` 同一笔账）

_READY_TIMEOUT_S = 60.0
# 整条流的天花板：演示库一轮真同步是秒级，120s 之外就是挂住了
_STREAM_TIMEOUT_S = 120.0

# uvicorn 在 win32 上默认给 **Proactor** 事件循环（`uvicorn/loops/asyncio.py` 写明
# `if sys.platform == "win32" and not use_subprocess: ProactorEventLoop`），而 asyncpg 只认
# Selector——本机 `dev.ps1 dev` 那条命令能用，靠的正是 `--reload` 让 uvicorn 走子进程模式
# （use_subprocess=True → Selector）。CLI 的 `--loop` 四档里没有任何一档给 Selector，
# 所以这里绕开 `Server.run()`（它会把 loop_factory 显式传给 Runner，策略说了不算），
# 自己 `asyncio.run(server.serve())`：循环就是策略里那一个。
# 用例里也不要 --reload——那会多一层文件监视的进程，terminate 只杀得掉父的。
_SELECTOR = (
    "asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy());"
    if sys.platform == "win32"
    else ""
)
_API_LAUNCHER = (
    "import asyncio, uvicorn; " + _SELECTOR + "asyncio.run(uvicorn.Server(uvicorn.Config("
    "'app.main:app', host='127.0.0.1', port={port}, log_level='info')).serve())"
)


def _free_port() -> int:
    """让 OS 挑一个空闲端口：并行跑两份 pytest 时 8000 一定被人占着。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def subproc_env(pg_test_env: dict[str, str], jwt_secret: str, fernet_key: str) -> dict[str, str]:
    """两个子进程各自的环境变量：随机测试 schema + 测试密钥。

    为什么改环境变量就足以让子进程去测试库（这一点是整个用例的地基）：`app/settings.py` 里
    `settings_customise_sources` 的顺序是 (init, env, secrets, dotenv)——pydantic-settings
    按**先后**定优先级，`.env` 排最后，所以真实环境变量赢过它。`AIWEB_PG__*` 六项由
    `pg_test_env` 从 `AIWEB_PG_TEST_DSN` 摊开，schema 是那 8 位随机名（conftest 在 import
    任何 app 模块之前就写进 os.environ 的那一份），口令与 JWT 密钥同理。
    少了这条事实的话，这两个子进程会连到本机 `.env` 那套真元数据库上去。

    `fernet_key` 必须两边同一个：口令是 API 进程加密的，解它的是 worker 进程。
    """
    env = {
        **pg_test_env,
        "AIWEB_JWT__SECRET": jwt_secret,
        "AIWEB_FERNET__KEYS": fernet_key,
    }
    # 发出去之前最后看一眼：宁可这里红，也不要在真元数据库上红
    assert env["AIWEB_PG__SCHEMA_NAME"].startswith("aiweb_test_"), env["AIWEB_PG__SCHEMA_NAME"]
    return env


def _tail(log: Path, limit: int = 4000) -> str:
    """日志末尾一截；**只要出现过口令字样就整份不给**。

    不做"挑出不含口令的行"那种过滤：一行里既有原文又有别的东西时过滤会漏，而这里的失败
    诊断不需要全文，需要的是"没被写进聊天/终端/CI 日志"这个保证。
    """
    try:
        body = log.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # 文件还没建出来：连启动都没走到，日志本来就没有
        return f"<读不到日志：{exc}>"
    if any("口令" in line or "password" in line.lower() for line in body.splitlines()):
        return "<日志含口令，已隐去全文>"
    return body[-limit:]


def _spawn(argv: list[str], log: Path, env: dict[str, str]) -> subprocess.Popen[str]:
    """起子进程并把两份标准流并进 `log`：不接 PIPE 是因为没人读。

    Windows 的匿名管道缓冲只有 64KB，写满之后子进程就卡在写日志上——那副样子和"进度流挂住"
    一模一样，会把这条用例的失败解释带偏。文件没有这个上限（子进程拿到的是复制过的句柄，
    父这边关掉也不影响它写）。

    调用方一律带 `-u`：子进程的 stdout 是文件时 Python 默认**块缓冲**，而我们收尾用的是
    terminate（硬杀），缓冲区里的东西一起没了——本机第一次跑就是这么看到两份空日志。
    """
    with log.open("wb") as sink:
        return subprocess.Popen(
            argv,
            cwd=str(BACKEND_ROOT),
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
        )


def _terminate(proc: subprocess.Popen[str]) -> None:
    """收尾：活着就 terminate，10s 内不退再 kill（`terminate` 之后不许再写日志文件）。"""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


@pytest.fixture
def api_url(subproc_env: dict[str, str], tmp_path: Path) -> Iterator[str]:
    """起真 API 子进程并等它就绪，返回 `http://127.0.0.1:<port>`。"""
    port = _free_port()
    log = tmp_path / "api.log"
    proc = _spawn([sys.executable, "-u", "-c", _API_LAUNCHER.format(port=port)], log, subproc_env)
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_ready(proc, base, log)
        yield base
    finally:
        _terminate(proc)


def _wait_ready(proc: subprocess.Popen[str], base: str, log: Path) -> None:
    """等 `/api/healthz` 答 200；子进程中途死了就直接把日志原文红出来。

    60s 是**没起来**的天花板，不是同步的耗时——一个 FastAPI 进程 import 到能答请求在本机是
    秒级，给到 60s 是留给冷启动（杀软扫描、磁盘缓存没热）的时间。

    捕获的必须是 `httpx.TransportError` 这个**父类**而不是几个子类：端口还没 bind 时，本机
    Windows 的栈给的既有拒连（`ConnectError`）也有**不回应的 SYN**（`ConnectTimeout`）——
    第一次跑就是那份 `ConnectTimeout` 穿出了窄捕获，把"进程还在冷启动"报成了用例失败，
    而 `returncode: 1` 是随后 `terminate()` 盖上去的（Windows 硬杀的退出码就是 1），
    看起来倒像是 uvicorn 自己崩了。
    """
    deadline = time.monotonic() + _READY_TIMEOUT_S
    while True:
        if proc.poll() is not None:
            raise AssertionError(
                f"API 子进程没起来（退出码 {proc.returncode}）：\n{_tail(log)}",
            )
        try:
            if httpx.get(f"{base}/api/healthz", timeout=2.0).status_code == 200:
                return
        except httpx.TransportError:
            pass
        if time.monotonic() > deadline:
            raise AssertionError(f"等 {base}/api/healthz 就绪超时：\n{_tail(log)}")
        time.sleep(0.25)


async def _finished(session_factory: Factory, job_id: int) -> bool:
    """那一行的钟点没点——期望值不取自 `core.sse.job_is_finished`，那是被验的一方。"""
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        value = (
            await session.execute(
                text(f'select finished_at is not null from "{schema}".sync_jobs where id = :job'),
                {"job": job_id},
            )
        ).scalar_one()
    return bool(value)


async def _event_rows(session_factory: Factory, job_id: int) -> list[dict[str, object]]:
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        f'select seq, stage, phase from "{schema}".sync_job_event '
                        "where job_id = :job order by seq"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


def _bodies(frames: list[Frame]) -> str:
    """失败时给人看的东西：帧的 event/id/负载压成几行，别把整包 data: 原文倒进终端。"""
    return " | ".join(f"{f.event}#{f.id} {json.dumps(f.data, ensure_ascii=False)}" for f in frames)


async def test_真子进程跑作业时进度帧就已经在流上了(
    api_url: str,
    subproc_env: dict[str, str],
    tmp_path: Path,
    client: AsyncClient,
    login: Login,
    account: DbAccount,
    session_factory: Factory,
) -> None:
    """验收 7 + 验收 1/2/6 的真库版本。

    顺序是这条用例的全部意义：**先入队 → 再开流 → 最后才放 worker 出门**。这样第一帧只可能
    来自那个真子进程写下的事件行，而"首帧早于作业结束"是**当场**断的（拿到首帧那一刻去库里
    看 `finished_at` 还是 NULL），不是事后从时间戳倒推的。

    流式读到的是逐行喂进来的块：缓冲到底在哪一层（uvicorn 每 yield 一个块就是一次 socket
    写，还是攒够了才写）这条用例都替我们说清楚了——红在首帧那一步就是"一次性吐完"。
    """
    acct = await login(username="live-sse-owner")
    user, password, host, port = account
    ds_id = await _register(
        client,
        acct,
        name="live-sse",
        host=host,
        port=port,
        connect_user=user,
        connect_password=password,
        include_schemas=[DEMO_DB],
        # verification §1.2：下划线前缀的是建库脚本的内部对象，算进分母就是假进度
        exclude_tables=["\\_%"],
    )

    submit = await client.post(
        "/api/sync/jobs", json={"datasource_id": ds_id}, headers=acct.headers
    )
    assert submit.status_code == 202, submit.text
    job_id = int(submit.json()["job_id"])

    worker_log = tmp_path / "worker.log"
    worker = _spawn([sys.executable, "-u", str(RUN_WORKER)], worker_log, subproc_env)
    blocks: list[str] = []
    finished_at_first_frame: bool | None = None
    try:
        async with (
            asyncio.timeout(_STREAM_TIMEOUT_S),
            httpx.AsyncClient(
                base_url=api_url, timeout=httpx.Timeout(_STREAM_TIMEOUT_S, connect=10.0)
            ) as real,
            real.stream("GET", f"/api/sync/jobs/{job_id}/events", headers=acct.headers) as resp,
        ):
            assert resp.status_code == 200, (await resp.aread()).decode(errors="replace")
            assert resp.headers["content-type"].startswith("text/event-stream")
            assert resp.headers["cache-control"] == "no-cache, no-transform"
            assert resp.headers["x-accel-buffering"] == "no"

            pending: list[str] = []
            async for line in resp.aiter_lines():
                if line:
                    pending.append(line)
                    continue
                if not pending:
                    continue
                block = "\n".join(pending)
                pending.clear()
                blocks.append(block)
                # 只认第一个 progress 帧：那一刻连接还开着，而库里那一行还没有终局。
                # 判据是**解出来的帧**而不是 `startswith("event: ")`——progress 帧的第一行
                # 是 `id: <seq>`（§6.2 的形状），按行首认的话这一辈子也认不出来。
                if finished_at_first_frame is None:
                    got = _frames(block)
                    if got and got[0].event == "progress":
                        assert not resp.is_closed, "响应已经关了，那就不是流式"
                        finished_at_first_frame = await _finished(session_factory, job_id)
            assert not pending, f"响应结束时还攒着半帧：{pending}"
    finally:
        _terminate(worker)

    assert finished_at_first_frame is False, (
        "首帧到达时作业已经收尾——这一轮又跑成了「跑完再一次性吐」那个形状；"
        f"worker 日志：\n{_tail(worker_log)}"
    )
    assert blocks and blocks[0].startswith("retry: "), blocks[:2]

    frames = _frames("\n\n".join(blocks) + "\n\n")
    progress = [f for f in frames if f.event == "progress"]
    assert [f.data["stage"] for f in progress] == STAGES, _bodies(frames)
    assert [f.id for f in progress] == list(range(1, len(STAGES) + 1)), "游标必须连续无洞"
    assert frames[-1].event == "done" and frames[-1].id is None, _bodies(frames)

    first = progress[0].data
    assert set(first) >= set(EVENT_KEYS), first
    assert (first["total"], first["base_table"], first["view"], first["done"]) == (
        TOTAL,
        TABLES,
        VIEWS,
        0,
    ), "演示库的分母是 10（9 表 + 1 视图），不是 SHOW FULL TABLES 数出来的 11"

    embed = [f for f in progress if f.data["stage"] == "embed"]
    assert len(embed) == 1, "embed 档只该有一帧（spec 故事 11）"
    assert embed[0].data["payload"]["skipped"] is True, embed[0].data
    assert embed[0].data["payload"]["code"] == "embedding_not_configured", (
        "本机 .env 的 embedding 三键为空（工单 017 已定口径）"
    )

    # §2.8 的口径是「该事件时刻的累计账」：card_build 那一帧发在 `sync_cards` 之前，那一刻
    # 一张卡都还没落成，卡片数从 embed 帧起才涨上来。拿 12 去断 card_build 帧会把这条口径记反。
    assert [f.data["cards"] for f in progress] == [0, 0, 0, CARDS, CARDS], _bodies(frames)
    assert progress[-1].data["done"] == TOTAL, "收尾前两档的对象数都该走完"

    done = frames[-1].data
    assert done["status"] == "success", json.dumps(done, ensure_ascii=False)[:2000]
    assert done["errors"] == [], done["errors"]
    assert isinstance(done["duration_ms"], int) and done["duration_ms"] > 0, done
    assert done["counters"]["cards"] == CARDS, done["counters"]

    # 帧与库里的事实逐条对上：这条流不多报一帧、也不少一帧
    rows = await _event_rows(session_factory, job_id)
    assert [(r["seq"], r["stage"], r["phase"]) for r in rows] == [
        (f.id, f.data["stage"], f.data["phase"]) for f in progress
    ]
    assert password not in "\n\n".join(blocks), "进度流里出现了源库口令"
