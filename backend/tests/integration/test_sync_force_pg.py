"""工单 021 · `?force=true` 覆盖规模上限（把 §6 第三条出路从替代话换回真开关）。

这一片钉一条路与一对字。
**路**：请求体 `force` → `sync_jobs.force`（enqueue 写入）→ `ClaimedJob.force`（claim 带回）→
`run_sync(force=)`（真时为 `collect` 不传上限）；路断在任何一环，超限的作业都不会"照常开工"。
**字**：`SCOPE_REMEDIES` 第三条、metadata-model §6 表里 ③ 那一格、`test_extract_mysql_client.py`
的期望文本——三处必须是同一条字符串，由用例钉，不靠人眼（验收 2）。

期望值出处：docs/metadata-model.md §6 表第 5 行与 as-built(P3 开工前拍板) 第 7 条、
docs/architecture.md §7 的 POST 行、工单 021 验收 1~5。落点沿用 016 的进程分离：
POST 只入队回 202；超限且 force=false 表现为作业 `failed` +
`errors[0].code='extract_scope_too_large'`，而该错误类的 HTTP 档位仍是 400（已定口径
"仍是 400、既有形状不动、只换文案第三条"——两头都断在这里）。

桩件 `StubExtractor` 与 `_register`/`_scalar` 复用 `test_sync_pg.py`；`stub` 夹具在本文件
自建而不 import 原件（那份会在 `test_sync_enqueue_pg.py` 之外的文件再添一笔 F811 的
per-file-ignore，而 `pyproject.toml` 是共享文件，本片不动它）。
"""

from __future__ import annotations

import os
import re
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.errors import SCOPE_REMEDIES, ExtractScopeTooLarge
from app.services import sync_service
from app.services.job_queue import PostgresJobQueue
from app.settings import get_settings
from tests.conftest import load_script
from tests.integration.conftest import Account
from tests.integration.test_sync_pg import StubExtractor, _register, _scalar

pytestmark = pytest.mark.pg

Login = Callable[..., Awaitable[Account]]
RUN_WORKER = load_script(
    "run_worker_for_force_tests", Path(__file__).resolve().parents[2] / "scripts" / "run_worker.py"
)

# 三处字面的位置：常量（app/core/errors.py）、§6 表那一格（docs/metadata-model.md）、
# 用例期望文本（tests/unit/test_extract_mysql_client.py）。后两处按"文件里的原文"读，
# 而不是 import 常量来比自己——自己反推出期望值等于没钉。
BACKEND_ROOT = Path(__file__).resolve().parents[2]
DOCS_MODEL = BACKEND_ROOT.parent / "docs" / "metadata-model.md"
MYSQL_CLIENT_TEST = BACKEND_ROOT / "tests" / "unit" / "test_extract_mysql_client.py"

# §6 as-built(P3 拍板) 第 7 条点名的原文；backtick 是 markdown 排版，不进字符串内容
SIX_THIRD_REMEDY = "admin 用 ?force=true 覆盖上限"


@pytest.fixture
def max_tables_one(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """把 `AIWEB_EXTRACT__MAX_TABLES` 钉成 1——走真配置通道而不是桩掉 `get_settings`。

    工单验收 1 的原话是"用例把 MAX_TABLES 设成 1"；桩 SimpleNamespace 只盖住抽取那一次读取，
    而 force=true 的成功路径还会读到 `retrieval`（卡片泛列告警），真键真值把这条路走全。
    """
    monkeypatch.setenv("AIWEB_EXTRACT__MAX_TABLES", "1")
    get_settings.cache_clear()
    try:
        yield
    finally:
        # 缓存不清回去，同会话后头的用例会拿着 max_tables=1 继续跑
        get_settings.cache_clear()


@pytest.fixture
def stub_extractor(monkeypatch: pytest.MonkeyPatch) -> Callable[..., StubExtractor]:
    """与 `test_sync_pg.stub` 同一颗缝：换掉 `run_sync` 取方言的唯一入口 `_extractor_for`。

    自建而不是 import 原件：import 夹具要触发 pyproject 的 per-file F811 例外，
    那是一份共享文件，本片不动（文件头有一段记着这件事）。
    """

    def build(**kw: Any) -> StubExtractor:
        extractor = StubExtractor(**kw)
        monkeypatch.setattr(sync_service, "_extractor_for", lambda ds, spec: extractor)
        return extractor

    return build


async def _post(client: AsyncClient, acct: Account, ds_id: int, **extra: Any) -> Any:
    return await client.post(
        "/api/sync/jobs", json={"datasource_id": ds_id, **extra}, headers=acct.headers
    )


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


async def _drain_one(job_id: int) -> dict[str, Any]:
    """换一条**新连接**跑一轮 worker 的 `run_once`，返回终局的作业整行。

    与 `test_sync_pg._sync` 同一条理由：真机上 worker 是另一个进程，测试里共用请求会话
    会把"两条连接各自看见什么"糊过去。NullPool + 用完 dispose，不留闲置连接挡住
    随机 schema 结尾的 DROP SCHEMA。
    """
    engine = create_async_engine(os.environ["AIWEB_PG_TEST_DSN"], poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            assert await RUN_WORKER.run_once(session) == job_id, "worker 必须领到并跑完这一轮"
        return await _job_row(factory, job_id)
    finally:
        await engine.dispose()


async def test_超限且不带force_作业failed且三条出路原样到达(
    client: AsyncClient,
    login: Login,
    stub_extractor: Callable[..., StubExtractor],
    max_tables_one: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """验收 1 的前半（016 之后的落点）+ 已定口径"超限而 force=false 仍是 400 档的形状"。

    MAX_TABLES=1、源里有 2 张表：POST 仍旧只 202 + `{job_id}`（既有调用形状不变，验收 4），
    终局那一行是 `failed`，`errors[0]` 的形状穿过编排层一字不改；而"400"这一档由错误类
    自己钉住——它就是那条错误在 HTTP 上的名字（§6 as-built(P3-016) 第 4 条：钉子从请求 400
    改成 202 + 作业 failed，错误分类本身不动）。
    """
    acct = await login(username="f-400", role="member")
    ds_id = await _register(client, acct)
    stub_extractor(tables={"shop": ("a", "b")})

    resp = await _post(client, acct, ds_id)
    assert resp.status_code == 202, resp.text
    assert resp.json().keys() == {"job_id"}, resp.json()
    job_id = int(resp.json()["job_id"])

    row = await _drain_one(job_id)
    assert row["status"] == "failed", row
    assert row["force"] is False, "没说覆盖就落 false——默认值不是'曾被强制覆盖过'"
    err = row["errors"][0]
    assert err["code"] == "extract_scope_too_large", row["errors"]
    assert err["data"]["max_tables"] == 1, err["data"]
    assert err["data"]["remedies"] == list(SCOPE_REMEDIES), "三条出路要原样到达（§6 as-built 4）"
    assert len(SCOPE_REMEDIES) == 3, SCOPE_REMEDIES
    # "仍是 400"的字面一半：错误类的 HTTP 档不许因为接了 force 而漂移
    assert ExtractScopeTooLarge.status_code == 400

    stored = await _scalar(
        session_factory,
        'select count(*) from "{s}".meta_table where datasource_id = :ds',
        ds=ds_id,
    )
    assert stored in (0, None), "拒绝开工不该留下半截元数据"


async def test_超限且force为true_照常跑完且counters看得见真实抽取数(
    client: AsyncClient,
    login: Login,
    stub_extractor: Callable[..., StubExtractor],
    max_tables_one: None,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """验收 1 的后半：force=true → 202 且作业真跑完，`counters` 里实际抽取对象数 > 1。

    "覆盖了上限不等于瞒过上限"（工单 021 已定口径）：上限那一格不再传给 collect，
    但每一张真抽的表照常进 counters、照常落 `meta_table`——事后翻这一行能看出
    这次实际抽了多少，force 不是把数字也按下去的哑巴开关。
    """
    acct = await login(username="f-202", role="member")
    ds_id = await _register(client, acct)
    extractor = stub_extractor(tables={"shop": ("a", "b")})

    resp = await _post(client, acct, ds_id, force=True)
    assert resp.status_code == 202, resp.text
    assert resp.json().keys() == {"job_id"}, "响应形状不因 force 而多一个字（验收 4）"
    job_id = int(resp.json()["job_id"])

    row = await _drain_one(job_id)
    assert row["force"] is True, row
    assert row["status"] == "success", row
    assert row["errors"] == [], row["errors"]
    assert row["counters"]["tables"] == 2 > 1, "MAX_TABLES=1 而实际抽了 2 张：账要说真话"
    assert extractor.calls == ["shop"], "worker 必须真跑了抽取，不是入队时演了一遍"

    stored = await _scalar(
        session_factory,
        'select count(*) from "{s}".meta_table where datasource_id = :ds',
        ds=ds_id,
    )
    assert stored == 2, "counters 与库里的行数同源（沿用 §6 as-built 3 的口径）"


async def test_force传非布尔时422且不落到入队(
    client: AsyncClient,
    login: Login,
    stub_extractor: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """验收 3：`force` 传字符串 / 传对象 → 422，不落到入队。

    字符串走的是 strict 判定：lax 的 pydantic 会把 "true" 收成真——那是拿"admin 明说过
    覆盖"这种确认性动作去猜输入，必须 422 而不是替他做决定。
    """
    acct = await login(username="f-422", role="member")
    ds_id = await _register(client, acct)
    stub_extractor()

    for bad in ("true", "false", {"v": True}):
        resp = await _post(client, acct, ds_id, force=bad)
        assert resp.status_code == 422, f"force={bad!r} 竟然 {resp.status_code}：{resp.text}"
        assert resp.json()["error"]["code"] == "invalid_request", resp.text

    jobs = await _scalar(
        session_factory,
        'select count(*) from "{s}".sync_jobs where datasource_id = :ds',
        ds=ds_id,
    )
    assert jobs == 0, "422 的请求不能留下一行 pending"


async def test_claim_把force随行带回_不带时默认false(
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """缝的中间一环：enqueue 写进去的 force，claim 必须原样带出来给 worker。

    走 `PostgresJobQueue` 直接入队而不是 HTTP：这一条要单独钉"两行两个值"——
    若 HTTP 与 claim 同时坏，端到端那条（前两条用例）分不出坏在哪一环。
    不带 force 的调用形状（016 之前的两个位置参）必须照旧能入队，且落 false。
    """
    acct = await login(username="f-claim", role="member")
    ds_forced = await _register(client, acct, name="src-forced", host="f1.invalid")
    ds_plain = await _register(client, acct, name="src-plain", host="f2.invalid")

    async with session_factory() as session:
        queue = PostgresJobQueue(session)
        id_forced = await queue.enqueue(ds_forced, acct.user_id, force=True)
        id_plain = await queue.enqueue(ds_plain, acct.user_id)

    async with session_factory() as session:
        queue = PostgresJobQueue(session)
        first = await queue.claim()
        second = await queue.claim()
        third = await queue.claim()

    assert first is not None and second is not None and third is None
    assert (first.job_id, first.force) == (id_forced, True), first
    assert (second.job_id, second.force) == (id_plain, False), second

    row_plain = await _job_row(session_factory, id_plain)
    assert row_plain["force"] is False, "老调用形状入队：加列不许把历史语义翻成'曾覆盖过'"


def test_第三条出路的三处字面是同一条字符串() -> None:
    """验收 2：常量、§6 表 ③ 那一格、`test_extract_mysql_client.py` 的期望文本，三处同一句话。

    读法各有讲究：§6 表那一格是 markdown，反引号是排版不是内容，剥掉之后比；
    用例文件里取的是 `"remedies": [...]` 块内的**字符串字面量**——不从 `SCOPE_REMEDIES`
    反推（import 常量来比自己，"文档改了常量没改"那一类漂移就永远测不出来）。
    """
    docs_text = DOCS_MODEL.read_text(encoding="utf-8")
    lines = [ln for ln in docs_text.splitlines() if ln.startswith("| 超大库")]
    assert len(lines) == 1, "§6 表里那一行必须存在且唯一，否则这一条钉子失去对象"
    docs_third = lines[0].split("③", 1)[1].rsplit("|", 1)[0].replace("`", "").strip()

    test_src = MYSQL_CLIENT_TEST.read_text(encoding="utf-8")
    block = re.search(r'"remedies":\s*\[(.*?)\]', test_src, re.S)
    assert block is not None, "test_extract_mysql_client.py 里的 remedies 期望块不见了"
    literals = re.findall(r'"([^"]*)"', block.group(1))
    assert len(literals) == 3, literals

    assert SCOPE_REMEDIES[2] == docs_third, (SCOPE_REMEDIES[2], docs_third)
    assert SCOPE_REMEDIES[2] == literals[2], (SCOPE_REMEDIES[2], literals[2])
    # 三处同错也是错：§6 as-built(P3 拍板) 第 7 条点名换回的原文，当场锚一次
    assert SCOPE_REMEDIES[2] == SIX_THIRD_REMEDY
