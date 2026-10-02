"""`POST /api/kb/reindex` 的**端点半边 + 转手半边**（工单 029 验收 1/2/3/8/9 + 撞名口径）。

接缝两片：前五条只打真 HTTP → 回读 `kb_index_profile` 与 `sync_jobs`，被验的是"档案的出生入口"
（决策 5：全仓只有 reindex 往这张表插行）与"202 的两个键"；最后两条把 worker 的 `run_once` 拉进来，
验那颗靶能不能穿过进程边界到达 `run_sync`（021 给 `force` 立过的同一环）。真写向量的事在
`test_kb_reindex_live.py`（桩件给不了向量，它只给表结构）。

期望值口径（工单 029 字面 + spec 故事 3/7/16，不是从实现反推）：
- 验收 1：202 响应体**两个键齐**（`job_id` 与 `profile_id`）；库里那一行 `status='draft'`、
  `built_at IS NULL`；active 那行**整行逐格不变**。工单字面写的是"`updated_at` 没变"，而
  `kb_index_profile` 从来没有这一列（§2.6 只有 `created_at`/`built_at` 两个钟）——所以这里用
  整行快照，它是那句字面能落到的最强写法。差异已记进工单交付记录。
- 验收 8：三键为空 → **400 且不建空壳**（档案行数一根手指头都不许多）。
- 撞名口径（拆工时补）：档案是**全局**的（`profile_name()` 里没有 datasource），第二个源同三元组
  必然撞 `uq_kb_index_profile_name` → 复用那一行、202 回同一个 id、不建第二行、不抬 409。
- 撞到的那一行若是 **active**：**拒绝**（409）。复用它等于"重建中不影响线上检索"当场失效
  （spec 故事 3 的全部用途），而同一套三元组本来就没有"新基准"可建。这是撞名口径的必要补集。
- 权限档：与 `POST /sync/jobs` 同一把尺（owner 档）。工单涉及层那行写的是 `get_authorized`，
  但 reindex 会派 worker 拿这个源的凭据去连库、还要烧真端点的 embedding 额度，
  016 给 `/sync/jobs` 定档的理由逐字适用。**这是一处对工单字面的偏离**，写在交付记录里。
- 已定口径"reindex 用 draft 档案自己的三元组，**不再比 settings**"：027 那道守卫只管普通同步，
  这颗作业在建完 draft 之后、跑完之前被人改了 `.env` 也不改靶（工单原文："改的是配置不是这颗
  作业的靶"，因为 `kb_index_profile.dimensions` 是历史事实）。
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models.kb import KbIndexProfile
from app.services import kb_service, sync_service
from app.services.embedding_client import EMBEDDING_PROVIDER
from app.services.job_queue import PostgresJobQueue
from app.settings import get_settings
from tests.conftest import load_script
from tests.integration.conftest import Account, insert_active_profile
from tests.integration.test_sync_pg import StubExtractor

pytestmark = [pytest.mark.pg]

Login = Callable[..., Awaitable[Account]]

RUN_WORKER = load_script(
    "run_worker_for_reindex_tests",
    Path(__file__).resolve().parents[2] / "scripts" / "run_worker.py",
)

# 一条窄表在 kb-workflow §6 的形状下就是一张 `table` 卡（列数 ≤ 40 且 token ≤ 900）——
# 与 027 的守卫用例族同一个口径，不从"跑一遍数一下"反推。
CARDS_PER_NARROW_TABLE = 1
TABLES = ("shop_t1",)


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Callable[..., StubExtractor]:
    """把 `run_sync` 取方言的唯一入口换成桩：源库不参与，被验的仍是真编排（同 027 的夹具）。"""

    def build(**kw: Any) -> StubExtractor:
        extractor = StubExtractor(**kw)
        monkeypatch.setattr(sync_service, "_extractor_for", lambda ds, spec: extractor)
        return extractor

    return build


DS_BODY = {
    "name": "reindex-src",
    "kind": "mysql",
    "host": "stub.invalid",
    "port": 3306,
    "connect_user": "stub",
    "connect_password": "口令-不出现在任何响应里",
    "include_schemas": ["shop"],
}

# 这台机器"现在打算用哪套档案"钉成这一组字面（独立真相源，不从 settings 读回来）。
# 维度选 1536：那是 `kb_card.embedding` 的列宽（0005 的 as-built），也是 028 替身的默认维。
CONFIGURED: tuple[str, int, int] = ("text-embedding-3-small", 1536, 1)
# 与 CONFIGURED 不同的一套，用来摆"当前生效的是别的"——撞名口径只在两套相异时才谈得上。
OTHER: tuple[str, int, int] = ("bge-large-zh-v1.5", 1024, 1)

# "没配端点"的判据是 `EmbeddingGroup.configured` 那**四格**（BASE_URL / API_KEY / MODEL /
# DIMENSION>0），任意一格空掉就算；用例只钉一整组，不逐格试——逐格是 028 客户端用例的靶。
_EMPTY = ""


def _schema() -> str:
    return os.environ["AIWEB_PG__SCHEMA_NAME"]


def _name(triplet: tuple[str, int, int]) -> str:
    """§2.6 的档案名拼法，**手抄**在这里而不是调 `kb_service.profile_name()`。

    期望值不许由被测的那条链算出来：`profile_name()` 少拼一段（例如漏了 tplv）时，
    用它的返回值当期望值照样绿，而漏拼的那一段正是"改模板不会长出新档案"这个 bug 的形状。
    """
    model, dimension, tplv = triplet
    return f"{model}@{dimension}@tplv{tplv}"


def _pin(
    monkeypatch: pytest.MonkeyPatch,
    triplet: tuple[str, int, int] | None,
    *,
    base_url: str = "http://127.0.0.1:1/v1",
) -> None:
    """把"这台机器打算用哪套档案"钉死，含"根本没配"那一支。

    走真配置通道（setenv + `cache_clear()`）而不是桩 Settings：endpoint 与 kb_service 各读一次
    Settings，桩只能盖住其中一处（020/027 同一条纪律）。

    `base_url` 默认是**一个不存在的端口**：本文件的用例只发请求、从不跑带向量的那一轮，
    留个假地址比留个空的更诚实（`configured` 判的是"四格齐不齐"，不是"连不连得上"）。
    `test_kb_reindex_live.py` 传的是替身自己打印出来的那个地址——同一份写法，两种靶。
    """
    model, dimension, tplv = triplet or (_EMPTY, _EMPTY, 1)
    monkeypatch.setenv("AIWEB_EMBEDDING__BASE_URL", "" if triplet is None else base_url)
    monkeypatch.setenv("AIWEB_EMBEDDING__API_KEY", "" if triplet is None else "k")
    monkeypatch.setenv("AIWEB_EMBEDDING__MODEL", model)
    monkeypatch.setenv("AIWEB_EMBEDDING__DIMENSION", str(dimension) if triplet else "1536")
    monkeypatch.setenv("AIWEB_EXTRACT__CARD_TEMPLATE_VERSION", str(tplv))
    get_settings.cache_clear()


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, int, int]]:
    """端点四格里前三格（BASE_URL / API_KEY / MODEL）已配，三元组 = CONFIGURED
    （不连真端点：本文件的用例只发请求，不跑 worker；DIMENSION 由 `_pin` 跟着三元组给）。"""
    _pin(monkeypatch, CONFIGURED)
    yield CONFIGURED
    get_settings.cache_clear()


@pytest.fixture
def unconfigured(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """前三格钉成空（DIMENSION 仍由 `_pin` 给着 1536，`configured` 因此为假）——
    这是**本机 .env 的现状**，但只有钉住才不是碰巧。"""
    _pin(monkeypatch, None)
    yield None
    get_settings.cache_clear()


async def _profile_rows(factory: async_sessionmaker[AsyncSession]) -> list[dict[str, Any]]:
    """整表按 id 升序倒出来，**每格都读**。

    为什么不只点 `status`/`built_at`："`active` 那行没被动过"这句话只有逐格比才成立。
    """
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    text(f'SELECT * FROM "{_schema()}".kb_index_profile ORDER BY id')
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


async def _job_row(factory: async_sessionmaker[AsyncSession], job_id: int) -> dict[str, Any]:
    async with factory() as session:
        stmt = text(f'SELECT * FROM "{_schema()}".sync_jobs WHERE id = :id')
        row = (await session.execute(stmt, {"id": job_id})).mappings().one()
    return dict(row)


async def _reindex(client: AsyncClient, acct: Account, ds_id: int) -> Response:
    return await client.post("/api/kb/reindex", json={"datasource_id": ds_id}, headers=acct.headers)


async def _register(client: AsyncClient, acct: Account, **over: Any) -> int:
    resp = await client.post("/api/datasources", json={**DS_BODY, **over}, headers=acct.headers)
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


async def test_reindex_回_202_两个键齐_库里多出一行_draft_且_active_整行不动(
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
    configured: tuple[str, int, int],
) -> None:
    """验收 1 + 故事 3 的全部用途：新档案出生，而线上那套基准一个字都没变。

    档案行摆的是 `OTHER`（1024 那一套），settings 是 `CONFIGURED`（1536）——"重建中"的靶必须
    与线上的靶不同名，否则撞名口径会把这条用例变成"复用 active"，而那正是被拒绝的那一支。
    """
    active_id = await insert_active_profile(
        session_factory, model=OTHER[0], dimensions=OTHER[1], card_template_version=OTHER[2]
    )
    before = {r["id"]: r for r in await _profile_rows(session_factory)}
    acct = await login(username="rx-new")
    ds_id = await _register(client, acct)

    resp = await _reindex(client, acct, ds_id)

    assert resp.status_code == 202, resp.text
    # 两个键**齐**：少一个键的话 030 就没有 id 可激活，而 spec 故事 7 明确"不靠猜返回值"
    assert set(resp.json()) == {"job_id", "profile_id"}, resp.json()
    job_id, profile_id = int(resp.json()["job_id"]), int(resp.json()["profile_id"])

    rows = await _profile_rows(session_factory)
    draft = next(r for r in rows if int(r["id"]) == profile_id)
    assert draft["status"] == "draft", draft
    # built_at IS NULL 是 030 那道"未跑完不许激活"的门，出生时它必须为空
    assert draft["built_at"] is None, draft
    assert draft["name"] == _name(configured), draft
    assert (draft["model"], draft["dimensions"], draft["card_template_version"]) == configured, (
        draft
    )
    # §2.6 注④ 那一份字面。这里**不**用 `EMBEDDING_PROVIDER` 比：出生那行写的就是它，
    # 拿同一个常量比自己等于没比——只能证"两边读的是同一个符号"，证不了那一格真的是文档
    # 指定的协议名。夹具那边（`_retired_profile`）用常量是对的，那里是输入不是期望值。
    assert draft["provider"] == "openai_compatible", draft

    after = {r["id"]: r for r in rows}
    assert after[active_id] == before[active_id], "active 那行整行逐格不许动"
    # 202 是真的：那一行还排在队列里，钟（started_at）要等 worker 领走才点上
    job = await _job_row(session_factory, job_id)
    assert job["status"] == "pending", job
    assert job["started_at"] is None, job
    assert job["datasource_id"] == ds_id, job


async def test_三键为空时_reindex_回_400_并且不建_draft_空壳(
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
    unconfigured: None,
) -> None:
    """验收 8（spec 故事 16）：没端点就不许出生一行"永远不会被灌完"的档案。

    留着空壳的后果比报错重：030 会看到一行 draft 并诱导人去激活一套从来没有卡的基准，
    而 `/kb/search` 切过去就是零结果。行数不变是这条用例的全部断言。
    """
    acct = await login(username="rx-400")
    ds_id = await _register(client, acct)
    assert await _profile_rows(session_factory) == []

    resp = await _reindex(client, acct, ds_id)

    assert resp.status_code == 400, resp.text
    body = resp.json()["error"]
    assert body["code"] == "embedding_not_configured", body
    # 出路要说清两条路（故事 16 的字面）：配真端点，或者起闸内替身
    detail = str(body["message"]) + str(body["detail"])
    assert "AIWEB_EMBEDDING__BASE_URL" in detail, detail
    assert "fake_embedding_server" in detail, detail
    assert await _profile_rows(session_factory) == []


async def test_同三元组对第二个数据源_reindex_复用同一行_draft_不建第二行(
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
    configured: tuple[str, int, int],
) -> None:
    """验收 9：档案是全局的，所以两个源同三元组必然撞名——撞名是正常状态，不是错误。

    抬 409 会把"两个源共用一套基准"判成事故；建第二行则当场撞 `uq_kb_index_profile_name`。
    两条作业的 profile_id 相同，而 `built_at` 仍是 NULL（两次都还没跑）。
    """
    acct = await login(username="rx-two")
    ds_a = await _register(client, acct)
    ds_b = await _register(client, acct, name="reindex-src-b")

    first = await _reindex(client, acct, ds_a)
    second = await _reindex(client, acct, ds_b)

    assert (first.status_code, second.status_code) == (202, 202), (first.text, second.text)
    assert first.json()["profile_id"] == second.json()["profile_id"], (first.json(), second.json())
    assert first.json()["job_id"] != second.json()["job_id"], "两个源各一颗作业"
    rows = await _profile_rows(session_factory)
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "draft", rows


async def test_同一个源连发两次_reindex_第二次_409_且档案行不翻倍(
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
    configured: tuple[str, int, int],
) -> None:
    """互斥是库给的（`ux_sync_running`），不是代码里查一次再插。

    这一条同时钉两件事：409 那一档没被 reindex 绕开（同图还有未结束作业就不许再排一颗），
    以及撞名复用发生在**入队之前**也没长出第二行档案。
    """
    acct = await login(username="rx-409")
    ds_id = await _register(client, acct)

    first = await _reindex(client, acct, ds_id)
    second = await _reindex(client, acct, ds_id)

    assert first.status_code == 202, first.text
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "sync_already_running", second.text
    rows = await _profile_rows(session_factory)
    assert len(rows) == 1, rows


async def test_配置三元组与_active_一致时拒绝_reindex_不把线上基准当靶(
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
    configured: tuple[str, int, int],
) -> None:
    """撞名口径的补集：撞到的那行是 active，就当场拒绝，而不是"原地重建它"。

    复用的话后果是 spec 故事 3 的反面——那颗作业会往**线上那一套**的卡上逐张改向量，
    "重建中不影响线上检索"当场为假。而同一套三元组本来就没有"新基准"可建：
    要补向量走的是普通同步（决策 15），不是 reindex。
    """
    model, dimension, tplv = configured
    await insert_active_profile(
        session_factory, model=model, dimensions=dimension, card_template_version=tplv
    )
    acct = await login(username="rx-same")
    ds_id = await _register(client, acct)

    resp = await _reindex(client, acct, ds_id)

    assert resp.status_code == 409, resp.text
    body = resp.json()["error"]
    assert body["code"] == "reindex_matches_active_profile", body
    # 出路要说得出口袋里那两件事：换一套三元组，或者跑普通同步补向量
    assert _name(configured) in str(body["message"]), body
    rows = await _profile_rows(session_factory)
    assert len(rows) == 1 and rows[0]["status"] == "active", rows


async def test_撞名的行是_retired_时复用它_reindex_不新建第三行(
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
    configured: tuple[str, int, int],
) -> None:
    """回滚路上的一环：退役过的那一套再 reindex，是把那批卡重灌一遍，不是再造一套基准。

    `uq_kb_index_profile_name` 让同三元组不可能有第二行，所以"再激活回旧维度"只能复用
    retired 那一行。active（另一套）照样一个字不动——这才是"可回滚"的前提。
    """
    await insert_active_profile(
        session_factory, model=OTHER[0], dimensions=OTHER[1], card_template_version=OTHER[2]
    )
    retired_id = await _insert_retired_profile(session_factory, configured)
    acct = await login(username="rx-retired")
    ds_id = await _register(client, acct)

    resp = await _reindex(client, acct, ds_id)

    assert resp.status_code == 202, resp.text
    assert int(resp.json()["profile_id"]) == retired_id, resp.json()
    rows = await _profile_rows(session_factory)
    assert len(rows) == 2, rows
    assert [r["status"] for r in rows if int(r["id"]) == retired_id] == ["retired"], rows


async def test_global_档不能发_reindex_只有_owner_能做(
    client: AsyncClient,
    login: Login,
    configured: tuple[str, int, int],
) -> None:
    """权限档的正面反证：'global' 看得见这个源，但不能拿它的凭据去连库、烧 embedding 额度。

    与 `POST /sync/jobs` 同一把尺（016 定档的理由逐字适用）。403 而不是 404：
    这个源对他是**可见**的（列表里就有），只是这一档不给这个动作。
    """
    owner = await login(username="rx-owner")
    other = await login(username="rx-other")
    ds_id = await _register(client, owner, allow_global_access=True)

    resp = await _reindex(client, other, ds_id)

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "forbidden", resp.text


async def _insert_retired_profile(
    factory: async_sessionmaker[AsyncSession], triplet: tuple[str, int, int]
) -> int:
    """摆一行 retired（`insert_active_profile` 只管 active 那一族）。

    走 ORM 而不是裸 SQL：这一行的形状就是档案行的形状，字段名写错要当场 AttributeError。
    """
    model, dimension, tplv = triplet
    async with factory() as session:
        row = KbIndexProfile(
            name=_name(triplet),
            provider=EMBEDDING_PROVIDER,
            model=model,
            dimensions=dimension,
            card_template_version=tplv,
            status="retired",
            built_at=datetime(2026, 10, 1, tzinfo=UTC),
        )
        session.add(row)
        await session.commit()
        return int(row.id)


async def _run_worker_once() -> int | None:
    """换一条**新连接**跑一轮 worker 的 `run_once`，回它领到的 job_id。

    与 `test_sync_pg._sync` 同一条理由：真机上 worker 是另一个进程，测试里共用请求会话会把
    "两条连接各自看见什么"糊过去。NullPool + 用完 dispose，不留闲置连接挡住随机 schema
    结尾那句 DROP SCHEMA。
    """
    engine = create_async_engine(os.environ["AIWEB_PG_TEST_DSN"], poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            return await RUN_WORKER.run_once(session)
    finally:
        await engine.dispose()


async def _card_profile_ids(factory: async_sessionmaker[AsyncSession]) -> list[int]:
    """库里卡片行挂的是哪几套档案（去重、升序）。"""
    async with factory() as session:
        stmt = text(f'SELECT DISTINCT index_profile_id FROM "{_schema()}".kb_card ORDER BY 1')
        rows = (await session.execute(stmt)).scalars().all()
    return [int(i) for i in rows]


async def test_claim_把_reindex_profile_id_随行带回_普通同步为_none(
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
    configured: tuple[str, int, int],
) -> None:
    """验收 2 的第一跳：那颗靶要能穿过进程边界到达 worker（021 给 `force` 立过的同一环）。

    走 `PostgresJobQueue` 直接入队而不是打 HTTP：这一条要单独钉"两行两个值"——HTTP 与 claim
    同时坏的话，端到端那条分不出坏在哪一环。不带 `reindex_profile_id` 的老调用形状必须落
    NULL（普通同步的靶是"库里 active 那一套"，不是某一行档案）。
    """
    acct = await login(username="rx-claim")
    ds_reindex = await _register(client, acct, name="rx-claim-a", host="ra.invalid")
    ds_plain = await _register(client, acct, name="rx-claim-b", host="rb.invalid")

    async with session_factory() as session:
        queue = PostgresJobQueue(session)
        profile_id = await kb_service.create_draft_profile(session)
        job_reindex = await queue.enqueue(ds_reindex, acct.user_id, reindex_profile_id=profile_id)
        job_plain = await queue.enqueue(ds_plain, acct.user_id)

    async with session_factory() as session:
        queue = PostgresJobQueue(session)
        first = await queue.claim()
        second = await queue.claim()

    assert first is not None and second is not None
    assert (first.job_id, first.reindex_profile_id) == (job_reindex, profile_id), first
    assert (second.job_id, second.reindex_profile_id) == (job_plain, None), second


async def _events(factory: async_sessionmaker[AsyncSession], job_id: int) -> list[dict[str, Any]]:
    """一个作业的整条事件流（按 seq）。终局那一帧的 payload 只能从这里查——验收 2 要的就是
    "不需要靠猜 202 的返回值"，所以断言必须打在库里的行上而不是 `run_sync` 的返回值上。"""
    async with factory() as session:
        stmt = text(
            f'SELECT seq, stage, phase, counters, payload FROM "{_schema()}".sync_job_event '
            "WHERE job_id = :id ORDER BY seq"
        )
        rows = (await session.execute(stmt, {"id": job_id})).mappings().all()
    return [dict(r) for r in rows]


async def test_reindex_的卡片挂在_draft_那一行_哪怕跑之前_配置_又改了(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
    configured: tuple[str, int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已定口径"reindex 用 draft 档案自己的三元组，不再比 settings"的执行半边。

    中间改配置才是这条口径的字面场景：工单原文写的是"建 draft 之后、跑完之前有人改了 `.env`，
    改的是配置不是这颗作业的靶"。这里改的方向是**把端点配空**（`_pin(None)`：BASE_URL /
    API_KEY / MODEL 三格一起清，比换个 model 名更强：settings
    连一套三元组都拿不出来了，而卡片照样落在入队那一刻的那一行上）。worker 在本进程里跑
    （016 已定口径"不真起子进程"），所以 setenv 对它可见——与 027 的守卫用例族同一个注入点。

    断言的三样缺一都算没钉住：卡片挂在 **draft** 那一行（不是 active、也不是新长出第三行）、
    `warnings` 为空（027 那道守卫**没**插手这颗作业）、档案行只有出生时那两行。

    终局是 `failed` 而不是 success，这是本片补的一条口径（工单只写了普通同步的回落）：
    reindex 这颗作业的存在理由就是把向量灌进那一套新档案，这台机器此刻没端点可问，
    它就没完成使命——静默 `skipped` 会留下一行 `built_at` 已写的 draft，030 那道"卡片行数齐"
    拦不住它，于是"零向量"能被激活成线上基准。所以这里 `built_at` 必须仍是 NULL。
    端点清空之后客户端在"未配置"那一档就抬，**连 socket 都不碰**（028 的 `_require_configured`），
    所以这条 pg 用例不发真 HTTP；配了端点的成功与 partial 两路在 `test_kb_reindex_live.py`。
    """
    active_id = await insert_active_profile(
        session_factory, model=OTHER[0], dimensions=OTHER[1], card_template_version=OTHER[2]
    )
    before = {r["id"]: r for r in await _profile_rows(session_factory)}
    acct = await login(username="rx-cards")
    ds_id = await _register(client, acct)
    stub(tables={"shop": TABLES})

    resp = await _reindex(client, acct, ds_id)
    assert resp.status_code == 202, resp.text
    job_id, draft_id = int(resp.json()["job_id"]), int(resp.json()["profile_id"])
    assert draft_id != active_id, "靶与线上那套必须不同名，否则这条在验'复用 active'"

    _pin(monkeypatch, None)

    assert await _run_worker_once() == job_id, "worker 必须领到并跑完这一轮"

    row = await _job_row(session_factory, job_id)
    # 靶在 card_build 之前就已定下：卡片真挂在 draft 那一行，一张都没落到 active 上
    assert await _card_profile_ids(session_factory) == [draft_id]
    assert row["counters"]["cards"] == len(TABLES) * CARDS_PER_NARROW_TABLE, row
    assert row["warnings"] == [], f"027 那道守卫不许插手 reindex：{row['warnings']}"
    assert row["reindex_profile_id"] == draft_id, row
    assert row["status"] == "failed", row
    assert [e["code"] for e in row["errors"]] == ["embedding_not_configured"], row["errors"]

    after = {r["id"]: r for r in await _profile_rows(session_factory)}
    assert len(after) == 2, "跑一轮 reindex 不许多出第三行档案"
    assert after[draft_id]["built_at"] is None, after[draft_id]
    assert after[active_id] == before[active_id], "active 那行整行逐格不许动（含 built_at）"

    # 验收 2：profile_id 一路写进终局事件，重放事件流就查得到，不必猜 202 的返回值
    done = (await _events(session_factory, job_id))[-1]
    assert done["stage"] == "done", done
    assert done["payload"] == {"profile_id": draft_id}, done
