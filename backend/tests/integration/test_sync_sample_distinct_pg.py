"""枚举 NDV 采样在同步链路上的那一格（工单 033；`docs/kb-workflow.md` §8 第一、三步）。

这一片要钉的是**编排**：候选怎么筛、逐表怎么发、值域怎么落库、单表失败怎么隔离。
方言侧那条语句长什么样不在这里（`tests/unit/test_extract_sample_distinct.py` 不连库钉），
真 MySQL/真 PG 演示源上值域进不进得了卡片也不在这里（live 那一份）。进入方式与
`test_sync_pg.py` 同源：POST 只回 202，作业由 worker 的循环体消费（工单 016 已定口径
"用例不起子进程"），被验的是真 `run_sync`，桩件只替换 `_extractor_for` 那一个入口。

期望值口径（全部来自文档字面，不来自实现）：
- §8 第一步：候选按**列类型**筛，`id`（`bigint`）不在清单里 → 不许被问；
- §8 第二步：`enum_values` 与 `sample_values` 两格同批写入，超阈值那一列"放弃"= 什么都不写；
- 工单 033 已定口径：单表失败 → `errors[]` + 终局 `partial`，`code` 是**新增的一档**
  `sample_failed`（与 029 的 `embed_failed` 分开，别合成一个"回填失败"了事）；
- metadata-model §2.5 的 `errors` 形状 `{code, detail}`、表名进 `detail`（019/029 同族）；
- 顺序硬要求（工单 033 已定口径）：采样在 card_build **之前**且自己的事务已提交——
  `kb_service` 读的是库里那一格，所以"同一轮内卡片文本里就有值域"是这一条的可观测形状。

桩件为什么自带一整套列类型：`StubExtractor` 的列全是 `bigint`（`test_sync_pg._column`），
而 `bigint` 不是 §8 第一步的候选——采样用例需要一个"有短文本列"的源形状，
所以这里只改类型不改结构，`id` 那一列**故意留成 `bigint`** 当反证（它不许被问）。
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.extractor.base import ColumnSample, RawColumn, SampleTarget, SourceManifest
from app.services import sync_service
from app.settings import get_settings
from tests.conftest import load_script
from tests.integration.conftest import Account, ActiveProfile
from tests.integration.test_sync_card_isolation_pg import _meta_counts, boom_on
from tests.integration.test_sync_pg import StubExtractor, SyncSubmit, _register, _terminal_row

pytestmark = [pytest.mark.pg]

Login = Callable[..., Awaitable[Account]]

RUN_WORKER = load_script(
    "run_worker_for_sample_tests",
    Path(__file__).resolve().parents[2] / "scripts" / "run_worker.py",
)

# §8 第二步回的那一组：两个不同值、频次降序在桩件里就摆好（编排层不该重排，
# `adjudicate` 才重排），默认阈值 30 之内。
DEFAULT_GROUPS: tuple[tuple[str | None, int], ...] = (("已支付", 3), ("已完成", 2))


class SampleStub(StubExtractor):
    """带 §8 采样加档的桩：记下每一次被要求采样的靶，按预置的值域回组。

    `samples` 按**列名**给（不给的列回 `DEFAULT_GROUPS`）；`fail_on_tables` 按表名点名。
    失败用 RuntimeError 而不是 AppError：与 `StubExtractor.fail_on` 同一条理由——要证的是
    "任何没分类过的异常都不许把别的表的值域带走"，用自己分类过的异常反而是最平易的路。
    """

    def __init__(
        self,
        *,
        samples: Mapping[str, Sequence[tuple[str | None, int]]] | None = None,
        fail_on_tables: Sequence[str] = (),
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self._samples = dict(samples or {})
        self._fail_on_tables = set(fail_on_tables)
        # 每次被要求采样时记下的 `(靶, max_distinct, row_limit)`：三个键里那两个
        # 只有在这里才看得见（落库的值域对"阈值 2 与阈值 30"回的是同一份桩数据），
        # 所以"这一键真被读到"的证据形状是"下发的那一份跟着 .env 变"。
        self.sample_calls: list[tuple[SampleTarget, int, int]] = []

    async def collect(self, *args: Any, **kw: Any) -> SourceManifest:
        manifest = await super().collect(*args, **kw)
        return replace(manifest, columns=[_retype(c) for c in manifest.columns])

    async def sample_distinct(
        self, target: SampleTarget, *, max_distinct: int, row_limit: int
    ) -> list[ColumnSample]:
        self.sample_calls.append((target, max_distinct, row_limit))
        if target.table_name in self._fail_on_tables:
            raise RuntimeError(f"注入故障：{target.schema_name}.{target.table_name} 采样时源库断了")
        return [
            ColumnSample(
                column_name=name,
                groups=tuple(self._samples.get(name, DEFAULT_GROUPS)),
            )
            for name in target.columns
        ]


def _retype(column: RawColumn) -> RawColumn:
    """把非主键列摆成 §8 清单里的那一类（`varchar(32)`），`id` 原样留 `bigint`。

    留着 `id` 是为了让"候选筛选"这一步有用例看得见：筛子坏了的话 `id` 也会出现在
    `sample_calls` 的靶里，而不是安静地少问一列。
    """
    if column.column_name == "id":
        return column
    return replace(column, data_type="varchar(32)", raw_data_type="varchar(32)")


@pytest.fixture
def sample_stub(monkeypatch: pytest.MonkeyPatch) -> Callable[..., SampleStub]:
    """与 `test_sync_pg.stub` 同一个进入点：只换 `_extractor_for`，被验的是真 `run_sync`。"""

    def build(**kw: Any) -> SampleStub:
        extractor = SampleStub(**kw)
        monkeypatch.setattr(sync_service, "_extractor_for", lambda ds, spec: extractor)
        return extractor

    return build


@pytest.fixture
def sample_settings(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """按用例点名的键走**真实配置通道**注入（环境变量 + 清缓存）。

    与 `conftest.embedding_endpoint_off` 同一纪律：`run_sync` 在 worker 那条会话上读
    `get_settings()`，给模块属性打桩只能盖住一个调用点；走环境变量才盖得住整条链路。
    用例没点名的键因此是"整条目录的默认"，不是字面的"什么都不设"——那一条
    （工单 033 验收 8）由 `test_默认配置下一次采样都不发_作业照样success` 显式把开关
    钉回 `false` 来复现代码默认值，本机 `.env` 上那行 P2 时代留下的 `true` 盖不住它。
    """

    def apply(**kw: str) -> None:
        for key, value in kw.items():
            monkeypatch.setenv(f"AIWEB_EXTRACT__{key.upper()}", value)
        get_settings.cache_clear()

    try:
        yield apply
    finally:
        get_settings.cache_clear()


async def _run_chain(
    client: AsyncClient,
    acct: Account,
    ds_id: int,
    *,
    card_build_hook: Callable[[str], None] | None = None,
) -> tuple[SyncSubmit, dict[str, Any]]:
    """发起 + 消费 + 回读终局那一行（同 `test_sync_pg._sync`，这里多回一份原始行）。

    `card_build_hook` 一路穿到 `sync_cards`（工单 019 的 test-only 注入缝）：本片要摆
    "卡片那一步失败"这一格时，用的必须是那一条既有通路而不是新造一个钩子。
    """
    resp = await client.post("/api/sync/jobs", json={"datasource_id": ds_id}, headers=acct.headers)
    assert resp.status_code == 202, resp.text
    job_id = int(resp.json()["job_id"])

    engine = create_async_engine(os.environ["AIWEB_PG_TEST_DSN"], poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await RUN_WORKER.run_once(session, card_build_hook=card_build_hook)
        row = await _terminal_row(factory, job_id)
        return SyncSubmit(202, row), row
    finally:
        await engine.dispose()


async def _values_by_column(
    session_factory: async_sessionmaker[AsyncSession], ds_id: int
) -> dict[str, tuple[list[str] | None, list[str] | None]]:
    """{`schema.table.column`: (`enum_values`, `sample_values`)}，两格一起回读。

    一起回是必须的：§8 第三步说的"写入 `enum_values` 与 `sample_values`"是一批两格，
    只断 `enum_values` 的话，"另一格从来没被写过"这条会安静地留在那儿。
    """
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "select t.schema_name || '.' || t.table_name || '.' || c.column_name"
                        " as key,"
                        " c.enum_values, c.sample_values"
                        f' from "{schema}".meta_column c'
                        f' join "{schema}".meta_table t on t.id = c.table_id'
                        " where t.datasource_id = :ds"
                    ),
                    {"ds": ds_id},
                )
            )
            .mappings()
            .all()
        )
    return {str(r["key"]): (r["enum_values"], r["sample_values"]) for r in rows}


async def test_开关打开后_候选列的值域第一次有了写入点(
    sample_settings: Callable[..., None],
    sample_stub: Callable[..., SampleStub],
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """工单 033 验收 2 的闸内那一半：`sample_distinct=true` 时两格都落成值域。

    P2/P3 期间非真 ENUM 列的这两格**恒空**（`enum_values` 只由 `information_schema` 的
    enum 定义写、`sample_values` 从来没有写入点），所以这条的正向断言写"等于桩回的那一份"
    而不是"非空"——空列表与 NULL 都不是这一轮该出现的形状。

    `id` 那一列同时是反证：它是 `bigint`，不在 §8 第一步的清单里，不许被问、更不许被写。
    """
    sample_settings(sample_distinct="true")
    acct = await login(username="smp-a", role="member")
    ds_id = await _register(client, acct)
    extractor = sample_stub(tables={"shop": ("orders",)})

    submitted, _row = await _run_chain(client, acct, ds_id)
    assert submitted.json()["status"] == "success", submitted.json()

    # 一张表一靶，靶里只有候选列
    assert [target.table_name for target, _, _ in extractor.sample_calls] == ["orders"]
    assert extractor.sample_calls[0][0].columns == ("name",), (
        "§8 第一步：`bigint` 的 `id` 不该出现在被问的列里"
    )
    assert await _values_by_column(session_factory, ds_id) == {
        "shop.orders.id": (None, None),
        "shop.orders.name": (["已支付", "已完成"], ["已支付", "已完成"]),
    }


async def test_默认配置下一次采样都不发_作业照样success(
    sample_settings: Callable[..., None],
    sample_stub: Callable[..., SampleStub],
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """工单 033 验收 8："决定权交回用户"那条口径的证据。

    断的是**没发过**（`sample_calls == []`），不是"结果为空"——后者在"发了但值域超阈值"
    那条路上同样成立，而那恰恰是相反的那件事。两格留 NULL 也一并钉住：默认路径上
    §8 的三步一步都不跑，卡片因此继续少一行「取值:」（§1 问题 5 那一格在默认路径没闭环，
    spec §7 的认下的代价）。
    """
    sample_settings(sample_distinct="false")
    acct = await login(username="smp-b", role="member")
    ds_id = await _register(client, acct)
    extractor = sample_stub(tables={"shop": ("orders", "users")})

    submitted, _row = await _run_chain(client, acct, ds_id)
    assert submitted.json()["status"] == "success", submitted.json()
    assert extractor.sample_calls == [], "开关关着却发了采样 SQL：这一步的真假只有这里看得见"
    assert await _values_by_column(session_factory, ds_id) == {
        "shop.orders.id": (None, None),
        "shop.orders.name": (None, None),
        "shop.users.id": (None, None),
        "shop.users.name": (None, None),
    }


async def test_阈值与行数上限两键真的下发_改配置下发的那一份跟着变(
    sample_settings: Callable[..., None],
    sample_stub: Callable[..., SampleStub],
    client: AsyncClient,
    login: Login,
) -> None:
    """工单 033 验收 1 的后两键：`SAMPLE_DISTINCT_MAX_DISTINCT` / `SAMPLE_ROW_LIMIT` 有读取点。

    `sample_distinct` 那一键的证据是上一条（关 → 一次不发），这一条补另外两键。
    断的是**下发的那一份**而不是落库结果：桩件对"阈值 7 与阈值 30"回的是同一组值，
    库里那一格长得一模一样——"配置改了不生效"这类错（P2 那本账的最后两条之一，020 同族）
    在结果侧是看不见的，只有靶上那两个关键字能说出"这一键真被读到了"。
    3000 与 7 都不是代码默认值（1000 与 30），也不与默认值成倍数关系，抄错默认值红。
    """
    sample_settings(
        sample_distinct="true", sample_row_limit="3000", sample_distinct_max_distinct="7"
    )
    acct = await login(username="smp-c", role="member")
    ds_id = await _register(client, acct)
    extractor = sample_stub(tables={"shop": ("orders",)})

    submitted, _row = await _run_chain(client, acct, ds_id)
    assert submitted.json()["status"] == "success", submitted.json()
    assert len(extractor.sample_calls) == 1, extractor.sample_calls
    _target, max_distinct, row_limit = extractor.sample_calls[0]
    assert (max_distinct, row_limit) == (7, 3000), (
        "settings 的两个数字没有原样到达方言层：要么没读，要么在中途被写死了一份"
    )


async def test_超阈值那一列什么都不写_阈值两侧各一列(
    sample_settings: Callable[..., None],
    sample_stub: Callable[..., SampleStub],
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """工单 033 验收 4：高基数列（NDV > 阈值）不写值域，阈值两侧各摆一列。

    阈值钉成 2，一列回 1 个值（阈值 -1）、一列回 3 个值（阈值 +1），两侧在同一次运行里
    各自过——"超了不写"与"没超就写"必须来自同一轮，否则一条绿只说明另一条没跑到。
    NDV 恰好等于阈值那一格（`adjudicate` 的 `>` 而非 `>=`）住在纯函数层
    `tests/unit/test_extract_sample_distinct.py`，这里不重复摆一遍输入。

    放弃的形状是 **NULL 而不是空列表**：§8 那句"只记基数不记值域"在 `meta_column` 上
    没有落点（那格列不存在，本片不动迁移），所以编排层能给出的诚实形状是"什么都不写"，
    而不是把 `[]` 写成"这列的取值有零个"。
    """
    sample_settings(sample_distinct="true", sample_distinct_max_distinct="2")
    acct = await login(username="smp-d", role="member")
    ds_id = await _register(client, acct)
    sample_stub(
        tables={"shop": ("orders",)},
        extra_column="status",
        samples={
            "name": (("已支付", 5),),
            "status": (("new", 1), ("paid", 2), ("done", 3)),
        },
    )

    submitted, _row = await _run_chain(client, acct, ds_id)
    assert submitted.json()["status"] == "success", submitted.json()
    assert await _values_by_column(session_factory, ds_id) == {
        "shop.orders.id": (None, None),
        "shop.orders.name": (["已支付"], ["已支付"]),
        "shop.orders.status": (None, None),
    }


async def test_单表采样失败_其余表已写入的值域照常在场(
    sample_settings: Callable[..., None],
    sample_stub: Callable[..., SampleStub],
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """工单 033 验收 5：单表失败 → `partial` + 点得出表名，其余表的值域查库断在场。

    跑两轮是必须的：只跑注入那一轮的话，"其余表在场"这句话没有反证——它可能从来就没被写过，
    也可能只是第一轮留下的旧行。第二轮的 `collect` 会按既有 upsert 把两列的 `enum_values`
    一起抹成 NULL（metadata-model §6：本轮同步列以本轮事实为准），所以第二轮查到的那一份
    **只可能来自第二轮的采样**，隔离才是真的隔离。

    `code` 是新增的一档 `sample_failed`，不与 029 的 `embed_failed` 合档（工单 033 已定口径）；
    表名进 `detail` 而不是新造一格（§2.5 的 `{code, detail}` 形状，019/029 同族）。
    """
    sample_settings(sample_distinct="true")
    acct = await login(username="smp-e", role="member")
    ds_id = await _register(client, acct)

    sample_stub(tables={"shop": ("orders", "users")})
    first, _row = await _run_chain(client, acct, ds_id)
    assert first.json()["status"] == "success", first.json()
    assert await _values_by_column(session_factory, ds_id) == {
        "shop.orders.id": (None, None),
        "shop.orders.name": (["已支付", "已完成"], ["已支付", "已完成"]),
        "shop.users.id": (None, None),
        "shop.users.name": (["已支付", "已完成"], ["已支付", "已完成"]),
    }, "注入前的基线本身也要有独立期望值，不是拿到啥算啥"

    sample_stub(tables={"shop": ("orders", "users")}, fail_on_tables=("users",))
    second, _row = await _run_chain(client, acct, ds_id)
    body = second.json()
    assert body["status"] == "partial", body
    errors = body["errors"]
    assert [e["code"] for e in errors] == ["sample_failed"], errors
    assert "shop.users" in errors[0]["detail"], errors
    assert await _values_by_column(session_factory, ds_id) == {
        "shop.orders.id": (None, None),
        "shop.orders.name": (["已支付", "已完成"], ["已支付", "已完成"]),
        "shop.users.id": (None, None),
        "shop.users.name": (None, None),
    }, "一张表断了把隔壁表的值域一起带走：逐表事务名存实亡"


async def test_采样失败不碰元数据行数_注入前后同一份(
    sample_settings: Callable[..., None],
    sample_stub: Callable[..., SampleStub],
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """工单 033 验收 6：注入前后 `meta_table` / `meta_column` 的行数相同（019 验收 3 同形）。

    上一条钉的是"值域没被隔壁表带走"，这一条钉的是"行没被带走"——两件事在 `partial`
    字符串上长得一样，查库才是唯一的分法。基线 `(2, 4)` 是独立期望值（两张表 × `id`/`name`
    两列），不是"先抓一份再跟它比"：抓到什么比什么是同义反复，红了自己也不知道为什么。
    """
    sample_settings(sample_distinct="true")
    acct = await login(username="smp-f", role="member")
    ds_id = await _register(client, acct)

    sample_stub(tables={"shop": ("orders", "users")})
    first, _row = await _run_chain(client, acct, ds_id)
    assert first.json()["status"] == "success", first.json()
    baseline = await _meta_counts(session_factory, ds_id)
    assert baseline == (2, 4), "每张表 id/name 两列——基线本身也要有独立期望值"

    sample_stub(tables={"shop": ("orders", "users")}, fail_on_tables=("users",))
    second, _row = await _run_chain(client, acct, ds_id)
    assert second.json()["status"] == "partial", second.json()
    assert await _meta_counts(session_factory, ds_id) == baseline, (
        "采样这一步只该动 `meta_column` 的值域两格，行数一格里都不许变"
    )


async def _card_lines(
    session_factory: async_sessionmaker[AsyncSession], ds_id: int, needle: str
) -> list[str]:
    """本轮卡片文本里含 `needle` 的那些行（跨该数据源全部卡片）。

    回**整行**而不是回"有没有"：工单 012 那条老教训——「取值」与列名分开断会放过
    "取值行来自别的列"，所以这里把整行交给调用点逐字对。
    """
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(f'select text_md from "{schema}".kb_card where datasource_id = :ds'),
                    {"ds": ds_id},
                )
            )
            .scalars()
            .all()
        )
    return [line for card in rows for line in str(card).splitlines() if needle in line]


async def test_同一轮内新采的值域就出现在卡片取值段(
    sample_settings: Callable[..., None],
    sample_stub: Callable[..., SampleStub],
    active_profile: ActiveProfile,
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """工单 033 验收 3（顺序硬要求的那条）：采样在 card_build 之前且已提交 → 同轮就进卡片。

    这一条钉的是**轮内**：`kb_service` 读的是库里那一格 `enum_values`，采样若把自己的事务
    留到卡片之后才提交，同轮卡片就会少一行「取值:」——而作业是 `success`，一次看起来完全
    正常的同步悄悄少了那一行。这一条红了就是"假成功"，比红在断言里糟得多。

    整行逐字对（模板 `card_template.j2:11` 的字段行形状：`- 名 类型 可空: 注释 取值: a / b`）：
    「取值」与列名绑在同一行，分开断言会放过"这一行其实来自隔壁的表"。
    """
    sample_settings(sample_distinct="true")
    acct = await login(username="smp-h", role="member")
    ds_id = await _register(client, acct)
    sample_stub(tables={"shop": ("orders",)})

    submitted, _row = await _run_chain(client, acct, ds_id)
    assert submitted.json()["status"] == "success", submitted.json()
    assert await _card_lines(session_factory, ds_id, "取值:") == [
        "- name varchar(32) 可空: name 注释 取值: 已支付 / 已完成"
    ]


async def test_卡片那一步失败也带不走已提交的值域(
    sample_settings: Callable[..., None],
    sample_stub: Callable[..., SampleStub],
    active_profile: ActiveProfile,
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """顺序硬要求落地后的另一半：卡片那一步的失败射程里没有值域（033 已定口径 × 019 的边界）。

    这两步在同一条会话上前后脚跑：采样逐表提交，卡片逐表提交、单表失败 rollback（019）。
    这一条钉的是**两档失败不许互相污染**——注入的是卡片失败，`errors[]` 里就只能出现
    `card_build_failed`，不许混进 `sample_failed`，也不许把两张表的值域一起回滚掉。
    合体的形状（把采样搬进 `sync_cards` 的逐表循环里）在这条上会红：那张失败表的值域
    跟着它的 rollback 一起消失。

    实测变异记在这里而不是留白：把采样那一步的 `commit` 换成 `flush`，**红的不是这一条**
    （卡片那一步自己的逐表提交会顺手把它挂着的写一起收口），红的是上一条
    `test_单表采样失败_其余表已写入的值域照常在场`——"自己的事务已提交"那一半的证据在那里。
    """
    sample_settings(sample_distinct="true")
    acct = await login(username="smp-i", role="member")
    ds_id = await _register(client, acct)
    sample_stub(tables={"shop": ("orders", "users")})

    submitted, _row = await _run_chain(client, acct, ds_id, card_build_hook=boom_on({"shop.users"}))
    body = submitted.json()
    assert body["status"] == "partial", body
    assert [e["code"] for e in body["errors"]] == ["card_build_failed"], body
    assert await _values_by_column(session_factory, ds_id) == {
        "shop.orders.id": (None, None),
        "shop.orders.name": (["已支付", "已完成"], ["已支付", "已完成"]),
        "shop.users.id": (None, None),
        "shop.users.name": (["已支付", "已完成"], ["已支付", "已完成"]),
    }


async def test_方言没有这档能力时整步跳过_不报错也不采样(
    sample_settings: Callable[..., None],
    sample_stub: Callable[..., SampleStub],
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """工单 033 验收 7 末条：`can_sample_distinct=False` 的方言 → 整步跳过，作业照样 success。

    开关是开着的（`sample_distinct=true`），所以这一步的假只可能来自能力位那一格——
    两条闸各钉各的，合成一条就分不出"用户没让扫"与"这一家方言还不会扫"。
    注册表用 `dataclasses.replace` 换一份而不是改类定义：那张表是 `_DIALECTS` 里的数据，
    而编排层读的正是数据（024 拆掉 `isinstance` 那条边之后的形状）。

    跳过是**静默的**，这是工单 033 已定口径而不是省事：源类型支不支持采样不是用户的错，
    把"这一家还不会"报成 `partial` 会让每一次 MySQL→新方言的过渡期同步都带上一条假故障。
    """
    sample_settings(sample_distinct="true")
    monkeypatch.setitem(
        sync_service._DIALECTS,
        "mysql",
        replace(sync_service._DIALECTS["mysql"], can_sample_distinct=False),
    )
    acct = await login(username="smp-g", role="member")
    ds_id = await _register(client, acct)
    extractor = sample_stub(tables={"shop": ("orders",)})

    submitted, body = await _run_chain(client, acct, ds_id)
    assert submitted.json()["status"] == "success", submitted.json()
    assert extractor.sample_calls == []
    assert body["errors"] in ([], None), body
    assert await _values_by_column(session_factory, ds_id) == {
        "shop.orders.id": (None, None),
        "shop.orders.name": (None, None),
    }
