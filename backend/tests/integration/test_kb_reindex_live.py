"""embed 档**真写向量的那半边**（工单 029 验收 3/4/5/6/7/10）。

与 `test_kb_reindex_pg.py` 的分工是"桩件给得了表结构、给不了向量"：那一片验端点形状、档案
出生与靶的转手，这一片起 028 的闸内替身真发 HTTP，验向量真的落进 `kb_card` 的那一根列。
所以本文件每条用例都要一份替身、一次真往返（ADR-0015：只 subprocess 一层，客户端没有
transport 注入点，这里连"打个桩"的口子都不存在）。

期望值口径（五条不是从实现反推的）：
- 验收 5 的"旧那批一个字节都不动"取的是**库里该列的文本形态**逐行比对，不是"行数没变"：
  行数不变而值被覆盖，正是 ADR-0012 要防的那件事（两根列不在同一个语义空间里）。
- "挂对卡"比的是"这张卡存的就是**它自己正文**哈希出来的那个点"（替身的 `pseudo_vector`，
  它的确定性由 spec 故事 42 与 `test_embedding_server_live.py` 钉着）。只断非空的话，
  客户端按下标归位那种错法（卡片 A 挂上卡片 B 的向量）照样绿。
- 验收 6 除了 `partial` 这个字符串，还查库断言**其余表的向量真在场**（工单假绿防线 ③）。
- 验收 3 比的是 `/kb/search` 的 `items`。顶层的 `took_ms` 不参与：它是耗时不是名次，
  两次调用之间它必然不同，把它比进去只会得到一条永远红的用例。
- "三元组跟着档案走"里 `model` 那一格独立成条：替身按 model 名回话、未知名字当场 400，
  所以"客户端从 settings 拿模型名"这个错法在这里表现为 `partial`，不是静默写进一批
  另一个语义空间的向量（见那条用例自己的说明）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.vector_columns import vector_column_for
from app.services import sync_service
from tests.conftest import BACKEND_ROOT, load_script
from tests.integration.conftest import FakeEmbedding, insert_active_profile
from tests.integration.test_kb_reindex_pg import (
    CARDS_PER_NARROW_TABLE,
    CONFIGURED,
    OTHER,
    Login,
    _card_profile_ids,
    _events,
    _job_row,
    _pin,
    _profile_rows,
    _register,
    _reindex,
    _run_worker_once,
    _schema,
)
from tests.integration.test_sync_pg import StubExtractor, _sync

pytestmark = [pytest.mark.pg]

# 替身那份哈希在测试进程里再算一遍当期望值。它住在 `scripts/`（给子进程跑的那一份），
# 不是生产代码，所以"卡自己的正文 → 卡自己那一格的向量"这句话有独立出处。
FAKE_SERVER = load_script(
    "fake_embedding_server_for_reindex_tests",
    BACKEND_ROOT / "scripts" / "fake_embedding_server.py",
)

# 三张表是这条链的最小形状：逐表失败要"炸一张、另外两张还在"，两张表分不出"其余"是一首
# 还是两首。窄表 → 一张 `table` 卡（kb-workflow §6，与 pg 那一片同一个口径）。
TABLES = ("shop_t1", "shop_t2", "shop_t3")
FULL_NAMES = tuple(f"shop.{t}" for t in TABLES)
CARD_TOTAL = len(TABLES) * CARDS_PER_NARROW_TABLE

# 问句里带表名与"注释"两个词：命中集非空是"两次一样"这句话的前提——两份空列表也算一样。
_QUERY = "shop_t1 shop_t2 注释"

# pgvector 的 `vector` 是 **float4**（单精度），不是 float8：替身算出的双精度点存进来会先
# 被削成约 7 位有效数字（实测 `0.07753724297456155` 落库回读成 `0.077537246`）。
# 所以"挂对卡"的比对口径是 1e-6 相对误差——它是 float4 的表示精度，不是"差不多就行"：
# 挂错的向量与正确点在每个非零桶上都差在 1e-1 量级，差四个数量级。
# 这条精度事实 031 的 `<=>` 也要用（名次比较在 float4 上做），故写在这里而不是埋在断言里。
_VECTOR_RTOL = 1e-6


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Callable[..., StubExtractor]:
    """把 `run_sync` 取方言的唯一入口换成桩：源库不参与，被验的仍是真编排（同 pg 那一片）。

    桩件只替掉"结构从哪来"，向量那一路一个字都不替：客户端、HTTP、替身、UPDATE 全是真的。
    """

    def build(**kw: Any) -> StubExtractor:
        extractor = StubExtractor(**kw)
        monkeypatch.setattr(sync_service, "_extractor_for", lambda ds, spec: extractor)
        return extractor

    return build


def _sql_column(dimension: int) -> str:
    """用例只写**维度**，列名一律由全仓唯一那份映射（`core/vector_columns.py`）翻出来。

    工单验收 11 要的就是这条纪律：`1024 → embedding_1024` 的字面翻译只准出现在那一处。
    测试如果为了拼 SQL 自己记一份列名，它就成了第二份映射，而这份映射写错时的红是
    "向量没写进去"，真原因却是查错了列。

    （`app/` 与 `alembic/versions/` 之外不在 allowlist 用例的射程内，所以这条靠的是这里的
    写法本身：三根读库函数都收维度，两根列谁是谁由调用点那一份 `CONFIGURED`/`OTHER` 说话。）
    """
    return vector_column_for(dimension)


async def _vectors(
    factory: async_sessionmaker[AsyncSession], *, profile_id: int, dimension: int
) -> dict[int, str | None]:
    """某档案下每张卡在**那一维对应那根列**上的文本形态，按卡 id 收。

    取 `::text` 而不是解成 float：验收 5 说的是"一个字节都不动"，文本是最接近"字节"的可比
    形状；真要比数值的是 `_vector_pairs`，那是另一句话。
    """
    safe = _sql_column(dimension)
    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    f'SELECT id, {safe}::text FROM "{_schema()}".kb_card '
                    "WHERE index_profile_id = :p ORDER BY id"
                ),
                {"p": profile_id},
            )
        ).all()
    return {int(card_id): (None if value is None else str(value)) for card_id, value in rows}


async def _vector_pairs(
    factory: async_sessionmaker[AsyncSession], *, profile_id: int, dimension: int
) -> list[tuple[str, list[float]]]:
    """`[(卡片正文, 该卡的向量)]`：验"挂对卡"要的两格都从**同一行**读出来。

    向量按文本解 JSON——pgvector 的输出形状就是 `[1,2,3]`，测试连接不必装它的编解码器
    （那是 031 读侧第一次要的事）。
    """
    safe = _sql_column(dimension)
    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    f'SELECT text_md, {safe}::text FROM "{_schema()}".kb_card '
                    "WHERE index_profile_id = :p ORDER BY id"
                ),
                {"p": profile_id},
            )
        ).all()
    return [(str(md), json.loads(str(vec))) for md, vec in rows if vec is not None]


async def _presence_by_table(
    factory: async_sessionmaker[AsyncSession], *, profile_id: int, dimension: int
) -> dict[str, tuple[int, int]]:
    """每张表"该档案下有几张卡、那一根列上非空的有几张"：`{schema.table: (总数, 非空数)}`。

    两个数都要：只看非空数的话，一张表一张卡都没建出来也能"全部非空"（0 对 0），而验收 6
    要的恰恰是"那张表在、它的向量不在"与"这张表在、它的向量也在"分得开。
    """
    safe = _sql_column(dimension)
    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT t.schema_name || '.' || t.table_name AS tbl, "
                    f"count(*) AS total, count(c.{safe}) AS filled "
                    f'FROM "{_schema()}".kb_card c '
                    f'JOIN "{_schema()}".meta_table t ON t.id = c.table_id '
                    "WHERE c.index_profile_id = :p AND c.deleted_at IS NULL "
                    "GROUP BY 1 ORDER BY 1"
                ),
                {"p": profile_id},
            )
        ).all()
    return {str(tbl): (int(total), int(filled)) for tbl, total, filled in rows}


async def _search_items(client: AsyncClient, acct: Login, ds_id: int) -> list[dict[str, Any]]:
    """打一次 `/kb/search`，只回 `items`（`took_ms` 不参与比对，见文件头口径）。"""
    resp = await client.post(
        "/api/kb/search",
        json={"query": _QUERY, "datasource_ids": [ds_id], "k": 5},
        headers=acct.headers,
    )
    assert resp.status_code == 200, resp.text
    items: list[dict[str, Any]] = resp.json()["items"]
    return items


async def test_普通同步配了端点时向量真落且embed帧是真计数(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    fake_embedding: FakeEmbedding,
) -> None:
    """验收 10（决策 15 的正向断言）+ 验收 4（那一帧不再有 `skipped`）。

    为什么这条挂 live 而不是 pg：027 之后"配了端点的普通同步也真灌"是决策 15 的合取结果，
    它的反面（端点没配 → `{skipped:true}`）在 pg 那一片和 027 的守卫用例族里已经钉过；
    正向这一支必须有真 HTTP 才算证据。

    档案行摆 `CONFIGURED`、settings 也钉成 `CONFIGURED`：027 那道守卫要求两侧一致，否则一轮
    普通同步一张卡都不写，这条就在验一个空集合。
    """
    active_id = await insert_active_profile(
        session_factory,
        model=CONFIGURED[0],
        dimensions=CONFIGURED[1],
        card_template_version=CONFIGURED[2],
    )
    _pin(monkeypatch, CONFIGURED, base_url=fake_embedding.base_url)
    acct = await login(username="lv-plain")
    ds_id = await _register(client, acct)
    stub(tables={"shop": TABLES})

    resp = await _sync(client, acct, ds_id)

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "success", body
    assert body["warnings"] == [], body
    job_id = int(body["job_id"])

    # 验收 10：三根格——每张表一张卡、卡上那一列非空、非空数等于卡数
    presence = await _presence_by_table(
        session_factory, profile_id=active_id, dimension=CONFIGURED[1]
    )
    expected = dict.fromkeys(FULL_NAMES, (CARDS_PER_NARROW_TABLE, CARDS_PER_NARROW_TABLE))
    assert presence == expected, presence
    # 另一维那一列一个字不写（ADR-0012）：碰了它就说明列名不是从档案那一维来的
    other = await _presence_by_table(session_factory, profile_id=active_id, dimension=OTHER[1])
    assert {t: f for t, (_, f) in other.items()} == dict.fromkeys(FULL_NAMES, 0), other
    # 挂对卡：存的向量 == 这张卡自己的正文哈希出来的那个点
    for text_md, vector in await _vector_pairs(
        session_factory, profile_id=active_id, dimension=CONFIGURED[1]
    ):
        assert vector == pytest.approx(
            FAKE_SERVER.pseudo_vector(text_md, dimension=CONFIGURED[1]),
            rel=_VECTOR_RTOL,
            abs=1e-12,
        ), f"卡片正文与向量不配对：{text_md[:60]}"

    # 验收 4：embed 一档逐表一帧，`embedded` 从 1 涨到卡数，且**没有** `skipped` 键
    events = await _events(session_factory, job_id)
    embeds = [e for e in events if e["stage"] == "embed"]
    assert len(embeds) == len(TABLES), [e["payload"] for e in embeds]
    assert all("skipped" not in e["payload"] for e in embeds), [e["payload"] for e in embeds]
    assert [e["payload"]["embedded"] for e in embeds] == list(range(1, CARD_TOTAL + 1)), embeds
    assert {e["payload"]["table"] for e in embeds} == set(FULL_NAMES), embeds
    # `done` 全程单调不降、到 embed 时等于 total。工单字面是"单调上涨到 total"，而 embed 段内
    # 它是平的：`_Progress` 的分母是"本轮抽成的对象数"，灌向量不新增对象。这条差异记进
    # 工单交付记录，不在用例里偷偷把"上涨"改成"不降"就算了。
    dones = [e["counters"]["done"] for e in events]
    assert dones == sorted(dones), dones
    assert dones[-1] == len(TABLES) == events[0]["counters"]["total"], (dones, events[0])
    assert all(e["counters"]["done"] == e["counters"]["total"] for e in embeds), embeds
    assert float((await _job_row(session_factory, job_id))["progress"]) == 100.0

    # 普通同步不许给线上那套基准盖 `built_at`：`mark_profile_built` 只吃 reindex 的靶
    rows = {int(r["id"]): r for r in await _profile_rows(session_factory)}
    assert rows[active_id]["status"] == "active", rows[active_id]
    assert rows[active_id]["built_at"] is None, rows[active_id]


async def test_reindex到另一维时新列真写旧列一字不动且检索结果相同(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    fake_embedding: FakeEmbedding,
) -> None:
    """验收 5（假绿防线 ②）+ 验收 3（这颗示踪弹的全部用途）。

    两个维度同时在场才谈得上"换维度是同表第二列"（ADR-0012）：先用 1536 那套跑一轮真同步
    让 `embedding` 有值，再 reindex 出 1024 那一套，比的就是**逐行的文本**。
    替身按 model 名回宽度，所以 `_pin` 换成 `OTHER` 之后端点真的回 1024——不是用例假装它回。

    `/kb/search` 前后各问一次比 `items`：它是表粒度、且只认 active 档案，所以"重建中不影响
    线上检索"应当是**逐字节**相同，而不是"名次看起来差不多"。
    """
    active_id = await insert_active_profile(
        session_factory,
        model=CONFIGURED[0],
        dimensions=CONFIGURED[1],
        card_template_version=CONFIGURED[2],
    )
    _pin(monkeypatch, CONFIGURED, base_url=fake_embedding.base_url)
    acct = await login(username="lv-reindex")
    ds_id = await _register(client, acct)
    stub(tables={"shop": TABLES})
    first = await _sync(client, acct, ds_id)
    assert first.json()["status"] == "success", first.text

    old_before = await _vectors(session_factory, profile_id=active_id, dimension=CONFIGURED[1])
    assert all(value is not None for value in old_before.values()), old_before
    items_before = await _search_items(client, acct, ds_id)
    assert items_before, "命中集为空的话'两次一样'是在比两份空，什么也没验"

    _pin(monkeypatch, OTHER, base_url=fake_embedding.base_url)
    resp = await _reindex(client, acct, ds_id)
    assert resp.status_code == 202, resp.text
    job_id, draft_id = int(resp.json()["job_id"]), int(resp.json()["profile_id"])
    assert draft_id != active_id, "靶与线上那套同名就谈不上'旧列不动'"

    assert await _run_worker_once() == job_id

    row = await _job_row(session_factory, job_id)
    assert row["status"] == "success", row
    # 两套基准各一批行：新的挂在 draft 上，旧的一行都没被搬走
    assert await _card_profile_ids(session_factory) == [active_id, draft_id]
    new_presence = await _presence_by_table(
        session_factory, profile_id=draft_id, dimension=OTHER[1]
    )
    assert new_presence == dict.fromkeys(
        FULL_NAMES, (CARDS_PER_NARROW_TABLE, CARDS_PER_NARROW_TABLE)
    ), new_presence
    old_on_new = await _presence_by_table(
        session_factory, profile_id=draft_id, dimension=CONFIGURED[1]
    )
    assert {t: f for t, (_, f) in old_on_new.items()} == dict.fromkeys(FULL_NAMES, 0), old_on_new
    for text_md, vector in await _vector_pairs(
        session_factory, profile_id=draft_id, dimension=OTHER[1]
    ):
        assert vector == pytest.approx(
            FAKE_SERVER.pseudo_vector(text_md, dimension=OTHER[1]), rel=_VECTOR_RTOL, abs=1e-12
        ), f"卡片正文与向量不配对：{text_md[:60]}"

    # 假绿防线 ②：旧那批逐行、逐字节比（"行数没变"不够——覆盖就发生在行数不变的前提下）
    assert (
        await _vectors(session_factory, profile_id=active_id, dimension=CONFIGURED[1]) == old_before
    )

    # 验收 3：一字不变
    assert await _search_items(client, acct, ds_id) == items_before

    # 终局盖 built_at 的只有靶；active 整行逐格不动
    rows = {int(r["id"]): r for r in await _profile_rows(session_factory)}
    assert rows[draft_id]["status"] == "draft", rows[draft_id]
    assert rows[draft_id]["built_at"] is not None, rows[draft_id]
    assert rows[active_id]["built_at"] is None, rows[active_id]


async def test_单表embed失败时落partial其余表的向量真在场(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    fake_embedding_with_switches: Callable[..., FakeEmbedding],
) -> None:
    """验收 6（spec 故事 14 / 决策 9 第一档）：一张表倒下，不许把整轮打成 failed。

    注入点在**替身侧**（`--fail-on`）而不是给 `run_sync` 加第二个 test-only 形参——工单已定
    口径，理由是 019 的 `card_build_hook` 已经是签名上的代价。子串够用是因为卡片正文里带着
    `schema.table`（kb-workflow §5 的表头），替身因此只认"文本"这一层。

    "假绿防线 ③"要的是**查库**：`partial` 这个字符串加一条 errors 文案，配不上"其余表的
    向量根本没写"这件事。
    """
    fake = fake_embedding_with_switches(fail_on=["shop_t2"])
    await insert_active_profile(
        session_factory,
        model=OTHER[0],
        dimensions=OTHER[1],
        card_template_version=OTHER[2],
    )
    _pin(monkeypatch, CONFIGURED, base_url=fake.base_url)
    acct = await login(username="lv-partial")
    ds_id = await _register(client, acct)
    stub(tables={"shop": TABLES})

    resp = await _reindex(client, acct, ds_id)
    assert resp.status_code == 202, resp.text
    job_id, draft_id = int(resp.json()["job_id"]), int(resp.json()["profile_id"])

    assert await _run_worker_once() == job_id

    row = await _job_row(session_factory, job_id)
    assert row["status"] == "partial", row
    # 500 是可重试那一档，退避重试用尽后客户端翻成 `EmbeddingUpstreamError`。这里断的是
    # **端点自己坏掉**那一族，不是新发明的 `embed_failed` 兜底档。
    assert [e["code"] for e in row["errors"]] == ["embedding_upstream_error"], row["errors"]
    assert "shop.shop_t2" in str(row["errors"][0]["detail"]), row["errors"]

    presence = await _presence_by_table(
        session_factory, profile_id=draft_id, dimension=CONFIGURED[1]
    )
    assert presence == {
        "shop.shop_t1": (1, 1),
        "shop.shop_t2": (1, 0),
        "shop.shop_t3": (1, 1),
    }, f"卡片行在、向量缺失，这才是故事 14：{presence}"

    # partial 也盖 built_at（已定口径），否则 030 的"未跑完不许激活"永远为真
    rows = {int(r["id"]): r for r in await _profile_rows(session_factory)}
    assert rows[draft_id]["status"] == "draft", rows[draft_id]
    assert rows[draft_id]["built_at"] is not None, rows[draft_id]

    # 帧只发给活下来的那两张表，`embedded` 停在 2：炸掉那张不占计数（故事 14 的账要能对上）
    embeds = [e for e in await _events(session_factory, job_id) if e["stage"] == "embed"]
    assert {e["payload"]["table"] for e in embeds} == {"shop.shop_t1", "shop.shop_t3"}, embeds
    assert [e["payload"]["embedded"] for e in embeds] == [1, 2], embeds


async def test_端点回的宽度与档案不符时整颗作业failed且不盖built_at(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    fake_embedding: FakeEmbedding,
) -> None:
    """验收 7（spec 故事 15）：维度不符不是"某一表倒下"，是整颗作业作废。

    靶的形状是"配置写 1536、端点按 model 名回 999"：`mismatched-width` 回 999 维，而 999 不在
    `core/vector_columns.py` 的允许集合里，所以档案行本身仍是合法的 1536（列名取得到），
    只有**响应**对不上。这正是"不截断、不补零、不降级只写关键词"那句话唯一的闸内证据。

    两个数同时出现在失败信息里是工单 028 验收 5 的文案契约：只说"维度不符"的那条日志，
    读的人分不清该改 `.env` 还是该建新档案。
    """
    await insert_active_profile(
        session_factory,
        model=OTHER[0],
        dimensions=OTHER[1],
        card_template_version=OTHER[2],
    )
    _pin(monkeypatch, ("mismatched-width", 1536, 1), base_url=fake_embedding.base_url)
    acct = await login(username="lv-dim")
    ds_id = await _register(client, acct)
    stub(tables={"shop": TABLES})

    resp = await _reindex(client, acct, ds_id)
    assert resp.status_code == 202, resp.text
    job_id, draft_id = int(resp.json()["job_id"]), int(resp.json()["profile_id"])

    assert await _run_worker_once() == job_id

    row = await _job_row(session_factory, job_id)
    assert row["status"] == "failed", row
    assert [e["code"] for e in row["errors"]] == ["embedding_dimension_mismatch"], row["errors"]
    detail = str(row["errors"][0]["detail"])
    assert "999" in detail and "1536" in detail, detail

    # "整颗作废"是真的作废：一套向量都没留下，`built_at` 也没盖
    after = await _vectors(session_factory, profile_id=draft_id, dimension=CONFIGURED[1])
    assert after and all(value is None for value in after.values()), after
    rows = {int(r["id"]): r for r in await _profile_rows(session_factory)}
    assert rows[draft_id]["built_at"] is None, rows[draft_id]


async def test_reindex跑的时候三键被清空也不留下零向量的draft(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    fake_embedding: FakeEmbedding,
) -> None:
    """本片补的那条口径在**替身真起过**的情况下的反证。

    pg 那一片已经钉过同样的终局（`failed` + `built_at IS NULL`），但那一轮客户端是在"未配置"
    那一档抬的、一个 socket 都没碰。这一条要的是另一件事：**替身在跑，而配置在入队之后被
    清空**——`run_sync` 读的是 `get_settings()`，它必须读到清空后的那一份。

    写它的动机是一个具体的假绿：如果 embed 档在"有卡片、没端点、又是 reindex"时回落到
    `skipped`，库里就躺着一行 `built_at` 已盖、零向量的 draft，等 030 把它激活成线上基准，
    而 `/kb/search` 切过去就是零结果。
    """
    await insert_active_profile(
        session_factory,
        model=OTHER[0],
        dimensions=OTHER[1],
        card_template_version=OTHER[2],
    )
    _pin(monkeypatch, CONFIGURED, base_url=fake_embedding.base_url)
    acct = await login(username="lv-cleared")
    ds_id = await _register(client, acct)
    stub(tables={"shop": TABLES})

    resp = await _reindex(client, acct, ds_id)
    assert resp.status_code == 202, resp.text
    job_id, draft_id = int(resp.json()["job_id"]), int(resp.json()["profile_id"])

    _pin(monkeypatch, None)

    assert await _run_worker_once() == job_id
    row = await _job_row(session_factory, job_id)
    assert row["status"] == "failed", row
    assert [e["code"] for e in row["errors"]] == ["embedding_not_configured"], row["errors"]
    assert row["warnings"] == [], row
    rows = {int(r["id"]): r for r in await _profile_rows(session_factory)}
    assert rows[draft_id]["built_at"] is None, rows[draft_id]


async def test_请求体问的是档案那一行的模型名而不是配置当前那个(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    fake_embedding: FakeEmbedding,
) -> None:
    """已定口径"reindex 用 draft 档案**自己的三元组**"里 `model` 那一格的真跑证据。

    为什么必须挂在 live：全仓只有替身会让"模型名"改变行为——它按 model 名回话，未知名字当场
    400（`embedding_bad_request`），桩件给不出这个差别。

    靶只把 `MODEL` 换成替身不认的名字、`DIMENSION` 留在与档案同宽：宽度那一路**看不出**两侧
    谁是谁（`expected_dimension` 本来就从档案读，与改过的配置恰好同值），唯一分得开的是请求体
    里那个名字。所以这条的形态只有两种——`success`（读的是档案），或三张表逐表进 `errors[]`
    后终局 `partial`（读的是 settings）。没有第三种。

    动机的具体形状：三元组里只 `dimensions` 走档案、`model` 仍从 settings 拼的话，
    "改了模型名再索引一轮"会问新模型、按旧档案的宽度校验，两边恰好同宽时静默灌进一批来自
    另一个语义空间的向量，而库里看不出任何异常（工单 029 双轴审查·规格轴）。
    """
    await insert_active_profile(
        session_factory,
        model=OTHER[0],
        dimensions=OTHER[1],
        card_template_version=OTHER[2],
    )
    _pin(monkeypatch, CONFIGURED, base_url=fake_embedding.base_url)
    acct = await login(username="lv-model")
    ds_id = await _register(client, acct)
    stub(tables={"shop": TABLES})

    resp = await _reindex(client, acct, ds_id)
    assert resp.status_code == 202, resp.text
    job_id, draft_id = int(resp.json()["job_id"]), int(resp.json()["profile_id"])
    born = {int(r["id"]): r for r in await _profile_rows(session_factory)}
    assert born[draft_id]["model"] == CONFIGURED[0], born[draft_id]

    _pin(
        monkeypatch,
        ("settings-only-model", CONFIGURED[1], CONFIGURED[2]),
        base_url=fake_embedding.base_url,
    )

    assert await _run_worker_once() == job_id
    row = await _job_row(session_factory, job_id)
    assert row["status"] == "success", row
    assert row["errors"] == [], row
    presence = await _presence_by_table(
        session_factory, profile_id=draft_id, dimension=CONFIGURED[1]
    )
    assert presence == dict.fromkeys(
        FULL_NAMES, (CARDS_PER_NARROW_TABLE, CARDS_PER_NARROW_TABLE)
    ), presence
