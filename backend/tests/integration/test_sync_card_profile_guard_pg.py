"""同步链路上的两处守卫：没有 active 档案 / 三元组对不上，都**不写卡但作业仍 success**
（工单 027-C，spec 故事 9 与决策 8 的落点）。

接缝：真 HTTP 发起 → 真入队 → worker 跑完 `run_sync` → 回读库里那一行。为什么必须走整条链：
被验的是"卡片这一步在编排里的位置"，而 `card_build_guard` 自己（`tests/unit/
test_sync_card_build_guard.py` 那五条）只答得出判决，答不出"判决有没有被执行、告警有没有落进
`sync_jobs.warnings`"。桩抽取器复用 `test_sync_pg.StubExtractor`，注入点在 `_extractor_for`。

期望值口径（工单 027 字面，不是从实现反推）：
- **不写卡 ≠ 作业失败**（已定口径）：终局 `status='success'`，`counters.cards == 0`，
  告警住在 `sync_jobs.warnings` 与事件流里；判成 `failed` 会把"配置改了一半"这种可恢复状态
  伪装成事故。
- 验收 3：没有 active 档案时**没有新建档案行**（`SELECT count(*)` 前后相同）——那正是本工单
  要关掉的后门，所以断的是行数而不是返回值。
- 验收 2：detail 点得出**两个数**（配置的与档案的），两个方向各一条（维度 / 模型）。
- 验收 4：一致时卡片照常落，`index_profile_id` 等于那一行的 id。
- 新键一律走真配置通道（`monkeypatch.setenv` + `get_settings.cache_clear()`，同 020 那两条），
  否则"守卫读的是不是这台机器的配置"这句话永远盖不住。
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  —— 全模型注册，FK 探测才看得见 kb_card（同 test_kb_cards_pg）
from app.services import sync_service
from app.settings import get_settings
from tests.conftest import load_script
from tests.integration.conftest import Account, insert_active_profile
from tests.integration.test_sync_pg import StubExtractor

pytestmark = [pytest.mark.pg]

Login = Callable[..., Awaitable[Account]]

RUN_WORKER = load_script(
    "run_worker_for_profile_guard_tests",
    Path(__file__).resolve().parents[2] / "scripts" / "run_worker.py",
)

PASSWORD = "口令-不会出现在任何响应里"
DS_BODY = {
    "name": "profile-guard-src",
    "kind": "mysql",
    "host": "stub.invalid",
    "port": 3306,
    "connect_user": "stub",
    "connect_password": PASSWORD,
    "include_schemas": ["shop"],
}

# 一条窄表在 §6 的形状下就是一张 `table` 卡（列数 ≤ 40 且 token ≤ 900）——
# 期望值来自 kb-workflow §6，不是"跑一遍看落了几张"。
CARDS_PER_NARROW_TABLE = 1
TABLES = ("shop_t1",)


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Callable[..., StubExtractor]:
    """把 `run_sync` 取方言的唯一入口换成桩：源库不参与，被验的仍是真编排。"""

    def build(**kw: Any) -> StubExtractor:
        extractor = StubExtractor(**kw)
        monkeypatch.setattr(sync_service, "_extractor_for", lambda ds, spec: extractor)
        return extractor

    return build


@pytest.fixture
def triplet(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, int, int]]:
    """把"这台机器现在打算用哪套档案"钉成一条字面三元组。

    不钉的话它跟着开发者机器上的 `backend/.env` 变（`AIWEB_EMBEDDING__MODEL` 那一段），
    于是"只改维度"那条用例会连带模型那一格一起漂，detail 里的两个数就说不清是谁跟谁比。
    """
    model, dimension, tplv = "text-embedding-3-small", 1536, 1
    monkeypatch.setenv("AIWEB_EMBEDDING__MODEL", model)
    monkeypatch.setenv("AIWEB_EMBEDDING__DIMENSION", str(dimension))
    monkeypatch.setenv("AIWEB_EXTRACT__CARD_TEMPLATE_VERSION", str(tplv))
    get_settings.cache_clear()
    try:
        yield (model, dimension, tplv)
    finally:
        get_settings.cache_clear()


def _schema() -> str:
    return os.environ["AIWEB_PG__SCHEMA_NAME"]


async def _profile_rows(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session:
        return int(
            (
                await session.execute(text(f'SELECT count(*) FROM "{_schema()}".kb_index_profile'))
            ).scalar_one()
        )


async def _card_rows(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session:
        return int(
            (
                await session.execute(text(f'SELECT count(*) FROM "{_schema()}".kb_card'))
            ).scalar_one()
        )


async def _register(client: AsyncClient, acct: Account, **over: Any) -> int:
    resp = await client.post("/api/datasources", json={**DS_BODY, **over}, headers=acct.headers)
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


async def _run_chain(client: AsyncClient, acct: Account, ds_id: int) -> tuple[dict[str, Any], int]:
    """真发起 → 真 worker → 回读终局那一行；同时把 job_id 交回（事件流要按它查）。"""
    resp = await client.post("/api/sync/jobs", json={"datasource_id": ds_id}, headers=acct.headers)
    assert resp.status_code == 202, resp.text
    job_id = int(resp.json()["job_id"])

    engine = create_async_engine(os.environ["AIWEB_PG_TEST_DSN"], poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await RUN_WORKER.run_once(session)
        async with factory() as session:
            row = (
                (
                    await session.execute(
                        text(f'SELECT * FROM "{_schema()}".sync_jobs WHERE id = :id'),
                        {"id": job_id},
                    )
                )
                .mappings()
                .one()
            )
        return dict(row), job_id
    finally:
        await engine.dispose()


async def _card_build_frames(job_id: int) -> list[dict[str, Any]]:
    """事件流里 card_build 那几帧（含 payload）——告警必须**同时**住在库里和流里。"""
    engine = create_async_engine(os.environ["AIWEB_PG_TEST_DSN"], poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            f'SELECT phase, counters, payload FROM "{_schema()}".sync_job_event '
                            "WHERE job_id = :id AND stage = 'card_build' ORDER BY seq"
                        ),
                        {"id": job_id},
                    )
                )
                .mappings()
                .all()
            )
    finally:
        await engine.dispose()
    return [dict(r) for r in rows]


async def test_没有_active_档案时同步成功且零卡片_档案表一行都没多(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
    triplet: tuple[str, int, int],
) -> None:
    """工单 027 验收 3：新建库第一次同步"success 但零卡片"是**预期行为**，出路只有 reindex。

    三样一起断才够：终局状态、卡片行数、档案行数。少断最后一项，"读不到就顺手建一套"
    那条兜底照样能过——而它正是本工单要关掉的东西本身。
    """
    acct = await login(username="guard-none", role="member")
    ds_id = await _register(client, acct)
    stub(tables={"shop": TABLES})
    assert await _profile_rows(session_factory) == 0

    row, job_id = await _run_chain(client, acct, ds_id)

    assert row["status"] == "success", row
    assert row["counters"]["cards"] == 0, row
    assert await _card_rows(session_factory) == 0
    assert await _profile_rows(session_factory) == 0, "同步链路上不许长出档案行"
    codes = [w["code"] for w in row["warnings"]]
    assert codes == ["NO_ACTIVE_PROFILE"], row["warnings"]
    assert "POST /api/kb/reindex" in row["warnings"][0]["detail"], row["warnings"]
    frames = await _card_build_frames(job_id)
    assert len(frames) == 1, frames
    assert frames[0]["payload"]["skipped"] is True, frames
    assert frames[0]["payload"]["code"] == "NO_ACTIVE_PROFILE", frames


async def test_active_与配置一致时卡片照常落_挂在那一行上(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
    triplet: tuple[str, int, int],
) -> None:
    """工单 027 验收 4 的正面：一致时不但不写卡失败，而且 `index_profile_id` 就是那一行。

    反证写明在验收里：若来源仍走 `ensure_profile`，这一条会当场变红——档案行是 0，
    `ensure` 会新建一套并让卡片挂到**另一个** id 上。
    """
    model, dimension, tplv = triplet
    profile_id = await insert_active_profile(
        session_factory, model=model, dimensions=dimension, card_template_version=tplv
    )
    acct = await login(username="guard-ok", role="member")
    ds_id = await _register(client, acct)
    stub(tables={"shop": TABLES})

    row, job_id = await _run_chain(client, acct, ds_id)

    assert row["status"] == "success", row
    assert row["counters"]["cards"] == len(TABLES) * CARDS_PER_NARROW_TABLE, row
    assert row["warnings"] == [], row["warnings"]
    engine = create_async_engine(os.environ["AIWEB_PG_TEST_DSN"], poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            ids = (
                (
                    await conn.execute(
                        text(
                            f'SELECT DISTINCT index_profile_id FROM "{_schema()}".kb_card '
                            "ORDER BY 1"
                        )
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()
    assert [int(i) for i in ids] == [profile_id], ids
    assert await _profile_rows(session_factory) == 1, "写卡这一轮也不许多出档案行"
    frames = await _card_build_frames(job_id)
    assert frames[0]["payload"] == {}, frames[0]


@pytest.mark.parametrize(
    ("drift_env", "drift_value", "active_field"),
    [
        ("AIWEB_EMBEDDING__DIMENSION", "1024", "dimensions"),
        ("AIWEB_EMBEDDING__MODEL", "bge-large-zh-v1.5", "model"),
    ],
    ids=["改维度", "改模型"],
)
async def test_配置与active差一格就不写卡_告警点得出两个数(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
    triplet: tuple[str, int, int],
    monkeypatch: pytest.MonkeyPatch,
    drift_env: str,
    drift_value: str,
    active_field: str,
) -> None:
    """工单 027 验收 2：两个方向各一条（spec 故事 9）——换维度与换模型是两种不同的重建理由。

    **被改的一格在配置这一侧**（`monkeypatch.setenv` + `get_settings.cache_clear()`，同 020
    那两条），档案那一行仍是 `triplet` 钉住的那一套——这才是验收 2 的字面动作："改
    `AIWEB_EMBEDDING__DIMENSION` 再跑一次同步"。把漂移造在档案行上虽然也走同一个比对函数，
    但那样钉不到"这台机器改了 `.env` 之后同步到底看不看它"，而那正是后门原来藏身的地方。
    worker 是在本进程里跑的（`RUN_WORKER.run_once`），所以 env 改动对它可见；换成真子进程就不成立，
    那时要改的是子进程的环境变量。

    detail 里必须**两个值都在**：只说"不一致"的告警等于让运维再跑一次 SQL 去查是哪个键歪了。
    换模型不换维度时列宽没变、"看起来什么都不用重建"，但新旧向量不在同一个语义空间（ADR-0012），
    这一格正是最容易被漏的那一格。
    """
    model, dimension, tplv = triplet
    await insert_active_profile(
        session_factory, model=model, dimensions=dimension, card_template_version=tplv
    )
    active_value = dimension if active_field == "dimensions" else model
    monkeypatch.setenv(drift_env, drift_value)
    get_settings.cache_clear()

    acct = await login(username=f"guard-{active_field}", role="member")
    ds_id = await _register(client, acct)
    stub(tables={"shop": TABLES})

    row, _ = await _run_chain(client, acct, ds_id)

    assert row["status"] == "success", "不写卡 ≠ 作业失败（决策 8）"
    assert row["counters"]["cards"] == 0, row
    assert await _card_rows(session_factory) == 0
    codes = [w["code"] for w in row["warnings"]]
    assert codes == ["PROFILE_TRIPLET_MISMATCH"], row["warnings"]
    detail = row["warnings"][0]["detail"]
    assert drift_value in detail, detail
    assert str(active_value) in detail, detail
    assert "POST /api/kb/reindex" in detail, detail


async def test_守卫只长在card_build那一步_元数据这一轮照常落库(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
    triplet: tuple[str, int, int],
) -> None:
    """ "不写卡"划在哪一步为止：元数据落库、陈旧判定、embed 那一帧都不受它影响（决策 8 的射程）。

    如果实现把守卫挂在整段收尾之前（或者干脆判成 `failed`），这里就是 0 张表 / `status='failed'`。
    而"这一轮没有 active 基准"恰恰最容易被顺手写成事故——它是一条可以靠 reindex 恢复的状态。
    """
    acct = await login(username="guard-scope", role="member")
    ds_id = await _register(client, acct)
    stub(tables={"shop": TABLES})

    row, job_id = await _run_chain(client, acct, ds_id)

    counters = row["counters"]
    assert counters["tables"] == len(TABLES), counters
    assert counters["columns"] > 0, counters
    assert counters["cards"] == 0, counters
    schema = _schema()
    engine = create_async_engine(os.environ["AIWEB_PG_TEST_DSN"], poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            tables = (
                await conn.execute(
                    text(f'SELECT count(*) FROM "{schema}".meta_table WHERE datasource_id = :ds'),
                    {"ds": ds_id},
                )
            ).scalar_one()
            phases = [
                str(row["phase"])
                for row in (
                    await conn.execute(
                        text(
                            f'SELECT phase FROM "{schema}".sync_job_event '
                            "WHERE job_id = :id ORDER BY seq"
                        ),
                        {"id": job_id},
                    )
                )
                .mappings()
                .all()
            ]
    finally:
        await engine.dispose()
    assert int(tables) == len(TABLES), tables
    assert "card_build" in phases and "embed" in phases and "done" in phases, phases
