"""同步编排的失败路径（桩抽取器 + 真元数据库）：计数诚实、锁、鉴权、规模保护。

为什么这些用例不打真 MySQL：它们要的是"抽取中途炸了"这个前提，真库给不了；
而恰恰是这些前提决定了 `partial`、409、`failed` 这三个状态是不是真话。
`test_sync_live.py` 管"成功的时候东西对不对"，这里管"失败的时候说的对不对"。

期望值口径：metadata-model §3/§5/§6 + architecture §7 + roadmap P2 验收 3
+ 工单 007 验收 5/6。桩件全部走 `extractor/base.py` 的 `Raw*`，不碰方言层。
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.errors import SCOPE_REMEDIES, AppError, ExtractScopeTooLarge
from app.extractor.base import (
    RawCatalog,
    RawColumn,
    RawForeignKey,
    RawIndex,
    RawIndexColumn,
    RawTable,
    ScopeCounts,
    ServerInfo,
    SourceManifest,
)
from app.extractor.batching import slice_names
from app.services import sync_service
from tests.conftest import load_script
from tests.integration.conftest import Account

pytestmark = [pytest.mark.pg]

Login = Callable[..., Awaitable[Account]]

# worker 的循环体按文件路径加载（`scripts/` 不是包，理由见 tests/conftest.py 的 load_script）
RUN_WORKER = load_script(
    "run_worker_for_sync_tests", Path(__file__).resolve().parents[2] / "scripts" / "run_worker.py"
)


@dataclass
class SyncSubmit:
    """一次「发起 + 消费」之后可断言的形状。

    `status_code` 是 POST 的返回码（202 或 4xx），其余字段来自**库里那一行的终局**——
    P2 时代它们来自同一个响应体，进程分离之后分成两跳，而这片要钉的东西没变。
    `.json()` / `.text` 保持 httpx 的形状，是为了让绝大多数断言本体一行不改；
    改了的只有两处，都是"落点跟着进程边界搬"的那两条（400 与 500 用例）。
    """

    status_code: int
    body: dict[str, Any]

    def json(self) -> dict[str, Any]:
        return self.body

    @property
    def text(self) -> str:
        return json.dumps(self.body, ensure_ascii=False, default=str)


PASSWORD = "口令-不会出现在任何响应里"
DS_BODY = {
    "name": "stub-src",
    "kind": "mysql",
    "host": "stub.invalid",
    "port": 3306,
    "connect_user": "stub",
    "connect_password": PASSWORD,
    "include_schemas": ["shop"],
}


def _catalog(name: str) -> RawCatalog:
    return RawCatalog(
        catalog_name="",
        schema_name=name,
        charset="utf8mb4",
        collation="utf8mb4_general_ci",
        approx_size_bytes=1,
        visible_table_count=1,
    )


def _table(schema: str, name: str, *, table_type: str = "BASE TABLE") -> RawTable:
    return RawTable(
        catalog_name="",
        schema_name=schema,
        table_name=name,
        table_type=table_type,
        comment=f"{name} 的注释",
        # 视图在 IS 里没有引擎（真库里这一列是 NULL）：桩件跟着源库的形状，否则
        # "视图卡片该走降级模板"这一条在测试里永远撞不到。
        engine="InnoDB" if table_type == "BASE TABLE" else None,
        charset="utf8mb4",
        collation="utf8mb4_general_ci",
        approx_rows=10,
        data_bytes=1024,
        index_bytes=512,
    )


def _column(schema: str, table: str, name: str, *, pk: bool = False) -> RawColumn:
    return RawColumn(
        catalog_name="",
        schema_name=schema,
        table_name=table,
        column_name=name,
        ordinal_position=1,
        data_type="bigint",
        raw_data_type="bigint(20)",
        nullable=True,
        default=None,
        generated=False,
        comment=f"{name} 注释",
        char_length=None,
        num_precision=None,
        num_scale=None,
        enum_values=None,
        is_primary_key=pk,
    )


def _index(schema: str, table: str) -> RawIndex:
    return RawIndex(
        catalog_name="",
        schema_name=schema,
        table_name=table,
        index_name="PRIMARY",
        is_unique=True,
        is_primary=True,
        index_type="BTREE",
        comment=None,
        cardinality=10,
        columns=(RawIndexColumn(column_name="id", seq_in_index=1, collation="A", sub_part=None),),
    )


def _fk(schema: str, table: str, column: str, to_table: str) -> RawForeignKey:
    return RawForeignKey(
        catalog_name="",
        schema_name=schema,
        table_name=table,
        fk_name=f"fk_{table}_{column}",
        from_column=column,
        to_catalog=None,
        to_schema=None,
        to_table=to_table,
        to_column="id",
        seq=1,
        on_delete="CASCADE",
        on_update="RESTRICT",
    )


def _manifest(
    catalog: str,
    tables: Sequence[str],
    foreign_keys: Sequence[RawForeignKey] = (),
    *,
    id_is_pk: bool = False,
    extra_column: str | None = None,
    views: Sequence[str] = (),
    batches: Sequence[Sequence[str]] = (),
) -> SourceManifest:
    names = ["id", "name"] + ([extra_column] if extra_column else [])
    return SourceManifest(
        kind="mysql",
        server_version="5.7.17",
        collected_at=dt.datetime.now(dt.UTC),
        catalogs=[_catalog(catalog)],
        tables=[_table(catalog, t) for t in tables]
        + [_table(catalog, v, table_type="VIEW") for v in views],
        columns=[_column(catalog, t, c, pk=id_is_pk and c == "id") for t in tables for c in names],
        indexes=[_index(catalog, t) for t in tables],
        foreign_keys=list(foreign_keys),
        batches=[list(b) for b in batches],
    )


class StubExtractor:
    """按需返回 manifest、在指定库上抛错的假方言；`calls` 记录它被要求抽哪些库。

    `fail_on` 用 RuntimeError 而不是 AppError：真要说的是"任何没分类过的驱动异常都不能
    把计数带进总账"，用我们自己分类过的异常反而是最容易处理的那条路。
    """

    def __init__(
        self,
        *,
        catalogs: Sequence[str] = ("shop",),
        tables: Mapping[str, Sequence[str]] | None = None,
        views: Mapping[str, Sequence[str]] | None = None,
        fks: Mapping[str, Sequence[RawForeignKey]] | None = None,
        fail_on: str | None = None,
        id_is_pk: bool = False,
        extra_column: str | None = None,
    ) -> None:
        self._catalogs = list(catalogs)
        self._tables = dict(tables or {})
        self._views = dict(views or {})
        self._fks = dict(fks or {})
        self._fail_on = fail_on
        self._id_is_pk = id_is_pk
        self._extra_column = extra_column
        self.calls: list[str] = []
        # 编排层下发的那段范围条件（`table_sql`）原样记下：桩件自己不渲染口径，
        # 所以"PG 源有没有拿到 PG 的列写法"这件事只有在这里看得见。
        self.scope_sqls: list[str | None] = []
        self.closed = 0

    @property
    def kind(self) -> str:
        return "mysql"

    async def probe(self) -> ServerInfo:
        return ServerInfo(kind="mysql", server_version="5.7.17")

    async def discover(self) -> list[RawCatalog]:
        return [_catalog(name) for name in self._catalogs]

    def _objects(self, name: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """这个库在桩件里"源库有的对象"：(表, 视图)。没配过的库默认一张 `orders`。"""
        return self._tables.get(name, ("orders",)), self._views.get(name, ())

    async def count_scope(
        self,
        catalogs: Sequence[RawCatalog],
        *,
        table_sql: str | None = None,
        table_params: Mapping[str, str] | None = None,
    ) -> ScopeCounts:
        """分母按桩件自己知道的那点事实数，与 `collect` 同源（同一个 `_objects`）。

        范围过滤（`table_sql`）不参与：真方言侧它是 SQL 的事，桩件如果在这里各算一套，
        "分母和实际落库的对象数对不上"就永远只有真库能发现——而那正是这条要防的错。
        """
        base = view = 0
        for catalog in catalogs:
            tables, views = self._objects(catalog.schema_name)
            base += len(tables)
            view += len(views)
        return ScopeCounts(total=base + view, base_table=base, view=view)

    async def collect(
        self,
        catalogs: Sequence[RawCatalog],
        *,
        table_sql: str | None = None,
        table_params: Mapping[str, str] | None = None,
        max_tables: int | None = None,
        batch_size: int,
        batch_interval_ms: int,
    ) -> SourceManifest:
        name = catalogs[0].schema_name
        self.calls.append(name)
        self.scope_sqls.append(table_sql)
        if self._fail_on == name:
            raise RuntimeError(f"抽取 {name} 时源库断了")
        tables, views = self._objects(name)
        if max_tables is not None and len(tables) > max_tables:
            raise ExtractScopeTooLarge(
                f"抽取范围里有 {len(tables)} 张表，超过上限 {max_tables}",
                # detail 的形状照 `extractor/mysql.py` 的抛出点；三条出路的原文由
                # `test_extract_mysql_client.py` 按 §6 钉，这里只验它能不能穿过编排层。
                # 原文借 `SCOPE_REMEDIES` 常量而不是另编三句话：桩件与真抛出点同形，
                # 这条才真的是"生产 detail 原样到达响应体"。
                detail={
                    "table_count": len(tables),
                    "max_tables": max_tables,
                    "remedies": list(SCOPE_REMEDIES),
                },
            )
        return _manifest(
            name,
            tables,
            self._fks.get(name, ()),
            id_is_pk=self._id_is_pk,
            extra_column=self._extra_column,
            views=views,
            # 批名单按**真方言那一份切片函数**算，而不是随手 `[list(tables)]` 交一份：
            # 编排层往下传的正是这一格，桩件与实现不同形的话，"payload 里有每批的表名"
            # 这句话就只能等真 MySQL 才验得到（同上面 `SCOPE_REMEDIES` 那条理由）。
            batches=slice_names(list(tables) + list(views), batch_size),
        )

    async def close(self) -> None:
        self.closed += 1


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Callable[..., StubExtractor]:
    """把 `_extractor_for` 换成返回桩件；返回构造器，用例各自决定桩的行为。

    打在 `_extractor_for` 而不是 `MySQLExtractor` 上：那是 `run_sync` 取方言的唯一入口，
    换掉它之后被验的就是真 `run_sync`，不是某个包装函数的转发。
    """

    def build(**kw: Any) -> StubExtractor:
        extractor = StubExtractor(**kw)
        monkeypatch.setattr(sync_service, "_extractor_for", lambda ds, spec: extractor)
        return extractor

    return build


async def _register(client: AsyncClient, acct: Account, **over: Any) -> int:
    resp = await client.post("/api/datasources", json={**DS_BODY, **over}, headers=acct.headers)
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


async def _terminal_row(factory: async_sessionmaker[AsyncSession], job_id: int) -> dict[str, Any]:
    """读回终局那一行，投影成 P2 响应体的字段名。"""
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    text(f'select * from "{schema}".sync_jobs where id = :id'), {"id": job_id}
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1, rows
    row = dict(rows[0])
    return {
        "job_id": job_id,
        "status": row["status"],
        "counters": row["counters"],
        "warnings": row["warnings"],
        "errors": row["errors"],
    }


async def _sync(client: AsyncClient, acct: Account, ds_id: int) -> SyncSubmit:
    """发起 + 消费：`POST` 只拿到 202 与 job_id，然后 await worker 的循环体把这一轮跑完。

    工单 016 的已定口径是用例**不真起子进程**，所以这里 await 的 `run_once` 就是常驻循环
    每一轮做的事。返回的 `SyncSubmit` 把库里那一行的终局投影成 P2 那套字段名，于是这片
    所有断言本体（计数诚实、错误 code、口令不外泄）一行都不用改，变的只有进入方式。

    worker 用的是一条**新连接**而不是 `client` 那条 `get_db`：真机上它们不在同一个进程里，
    测试里共用会话就会把"两条连接各自看见什么锁"这一格糊过去。NullPool + 用完 dispose，
    免得闲置连接挡住随机 schema 结尾那句 DROP SCHEMA。
    """
    resp = await client.post("/api/sync/jobs", json={"datasource_id": ds_id}, headers=acct.headers)
    if resp.status_code != 202:
        return SyncSubmit(resp.status_code, resp.json())

    job_id = int(resp.json()["job_id"])
    engine = create_async_engine(os.environ["AIWEB_PG_TEST_DSN"], poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await RUN_WORKER.run_once(session)
        return SyncSubmit(202, await _terminal_row(factory, job_id))
    finally:
        await engine.dispose()


async def _scalar(
    session_factory: async_sessionmaker[AsyncSession], sql: str, **params: Any
) -> Any:
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        result = await session.execute(text(sql.replace("{s}", schema)), params)
        return result.scalars().one_or_none()


async def test_第二个库失败时计数只算写完的那个库(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`partial` 的 counters 必须是"确实提交完了的行数"（§6）。

    第一次 live 真跑暴露的反例：一个 catalog 中途 rollback、一行都没落，总账却已经边写边涨，
    于是响应说"10 张表同步好了"而表里是空的。这一条钉住的就是那个涨法。
    """
    acct = await login(username="owner-a", role="member")
    ds_id = await _register(client, acct, include_schemas=["shop", "warehouse"])
    extractor = stub(
        catalogs=("shop", "warehouse"),
        tables={"shop": ("orders", "users")},
        fail_on="warehouse",
    )

    resp = await _sync(client, acct, ds_id)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "partial", body
    assert extractor.calls == ["shop", "warehouse"]
    assert body["counters"]["tables"] == 2, "第一个库写完 2 张，第二个库一行都没落"
    assert body["counters"]["databases"] == 1
    assert body["counters"]["tables_failed"] == 1
    stored = await _scalar(
        session_factory,
        'select count(*) from "{s}".meta_table where datasource_id = :ds',
        ds=ds_id,
    )
    assert stored == 2, "计数与库里的行数必须同源：失败的那个库不能出现在任何一边的数字里"
    assert [e["code"] for e in body["errors"]] == ["extract_failed"], (
        "驱动异常的 code 只能是我们分类过的那一个。SQLAlchemy 的异常自己也有 `.code`"
        "（文档码，实测 'cd3x'），用 getattr 兜默认值等于把内部码当错误分类回给前端"
    )
    assert PASSWORD not in resp.text, "错误详情里不能带出口令"
    assert extractor.closed == 1, "失败路径也要关连接（finally 那一支）"


async def test_同一数据源同时只准一个同步在跑(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """409 由 `ux_sync_running` 这个部分唯一索引保证，不是代码里的 race。

    口径出自 metadata-model §6 与 roadmap P2 验收 3。
    """
    acct = await login(username="owner-b", role="member")
    ds_id = await _register(client, acct)
    extractor = stub()

    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        # 手工留一行"正在跑"的 job，模拟另一个人（或另一次请求）已经占了这把锁
        await session.execute(
            text(
                f'insert into "{schema}".sync_jobs'
                " (datasource_id, triggered_by, status, phase, started_at, heartbeat_at)"
                f' values (:ds, (select id from "{schema}".users where username = :who),'
                " 'running', 'connect', now(), now())"
            ),
            {"ds": ds_id, "who": "owner-b"},
        )
        await session.commit()

    resp = await _sync(client, acct, ds_id)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "sync_already_running"
    # 撞锁的这次连方言都不该拿到：锁要在"要不要开始干活"这一步就回答，而不是抽完再报错
    assert extractor.calls == []
    running = await _scalar(
        session_factory,
        "select count(*) from \"{s}\".sync_jobs where datasource_id = :ds and status = 'running'",
        ds=ds_id,
    )
    assert running == 1, "第二个 job 不能落库，否则部分唯一索引形同虚设"


async def test_失败之后这条源还能再同步(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """异常路径必须写终局，否则这条源被 `ux_sync_running` 永久锁死。

    修掉它的回归测试：上一版 `run_sync` 只在 try 的末尾收尾，没分类过的异常一路冒到端点变
    500，而那行 job 永远停在 'running'——部分唯一索引从此每次同步都 409，用户侧表现为
    "这个源再也点不动了"，只能手工进库删行才能救。
    """
    acct = await login(username="owner-f", role="member")
    ds_id = await _register(client, acct)
    stub(fail_on="shop")

    first = await _sync(client, acct, ds_id)
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "partial"

    second = await _sync(client, acct, ds_id)
    assert second.status_code == 202, "第二次不该被上一次留下的僵尸 job 挡住"

    stuck = await _scalar(
        session_factory,
        'select count(*) from "{s}".sync_jobs where datasource_id = :ds'
        " and status in ('pending', 'running')",
        ds=ds_id,
    )
    assert stuck == 0, "每条 job 都要有终局，否则部分唯一索引下次一定撞锁"


async def test_非_owner_不能触发同步(
    client: AsyncClient, login: Login, stub: Callable[..., StubExtractor]
) -> None:
    """同步会拿这个源的凭据去连库，能触发它就等于能试探它的口令（architecture §7 权限档）。"""
    owner = await login(username="owner-c", role="member")
    stranger = await login(username="stranger", role="member")
    ds_id = await _register(client, owner)
    extractor = stub()

    resp = await _sync(client, stranger, ds_id)
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "forbidden"
    assert extractor.calls == [], "鉴权失败时连源库都不该被连"

    admin = await login(username="boss", role="admin")
    assert (await _sync(client, admin, ds_id)).status_code == 202, "admin 档可以替别人触发"


async def test_超过_max_tables_时整个作业失败且三条出路原样到达(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """§6 的规模保护：这是 007 认领的活（此前 `AIWEB_EXTRACT__MAX_TABLES` 全仓库无读取点）。

    拒绝必须发生在**昂贵的列/索引查询之前**，而且一行都不落——不是 partial、不是"0 张表成功"。

    工单 016 改的是这件事的**落点**而不是它的实质：请求侧只入队，POST 一律 202（上限要真连库
    数过表才量得出来，而那时响应早就发出去了），所以那三条出路如今住在 `sync_jobs.errors[]`
    里而不是错误 envelope 里。逐字相等的断言照旧——它钉的就是"穿过编排层不许换成任意三句话"。
    """
    acct = await login(username="owner-d", role="member")
    ds_id = await _register(client, acct)
    stub(tables={"shop": ("a", "b", "c")})
    monkeypatch.setattr(
        sync_service,
        "get_settings",
        lambda: SimpleNamespace(
            # 三个键一起给：`run_sync` 现在从同一份 Settings 读 max_tables（007）、
            # batch_size 与 batch_interval_ms（020）。少给后两个的话这条用例会以
            # AttributeError 失败，而那看起来像"编排层写错了"而不是"桩件没跟上"。
            extract=SimpleNamespace(max_tables=2, batch_size=200, batch_interval_ms=0)
        ),
    )

    resp = await _sync(client, acct, ds_id)
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "failed", "拒绝开工不是部分成功"
    err = resp.json()["errors"][0]
    assert err["code"] == "extract_scope_too_large"
    assert err["data"]["remedies"] == list(SCOPE_REMEDIES), (
        "§6 点名要结构化返回三条出路，且要**原样**到达。这里验的是它能不能穿过编排层："
        "run_sync 若在 rollback + 重新抛出的路上把 detail 换成一句人话（或只剩长度对得上的"
        "三句话），前端就只剩一个 code 能看——所以断言是逐字相等，不是数一下有三条"
    )
    stored = await _scalar(
        session_factory,
        'select count(*) from "{s}".meta_table where datasource_id = :ds',
        ds=ds_id,
    )
    assert stored in (0, None), "拒绝开工不该留下半截元数据"


async def test_口令不进响应也不进同步任务记录(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """006 立过这条规矩，同步这条路更容易漏——它回 counters、还往 sync_jobs 里写 errors。"""
    acct = await login(username="owner-e", role="member")
    ds_id = await _register(client, acct)
    stub(fail_on="shop")

    resp = await _sync(client, acct, ds_id)
    assert resp.status_code == 202, resp.text
    assert PASSWORD not in resp.text
    listing = await client.get("/api/datasources", headers=acct.headers)
    assert PASSWORD not in listing.text

    jobs = await _scalar(
        session_factory,
        'select count(*) from "{s}".sync_jobs where datasource_id = :ds',
        ds=ds_id,
    )
    assert jobs == 1, "一次请求一行 job：失败也要留下痕迹（P3 的进度条靠它）"
    blob = await _scalar(
        session_factory,
        'select to_json(errors)::text from "{s}".sync_jobs where datasource_id = :ds',
        ds=ds_id,
    )
    assert blob is not None and PASSWORD not in blob


async def _extracted_edges(
    session_factory: async_sessionmaker[AsyncSession], ds_id: int, schema_name: str
) -> Any:
    return await _scalar(
        session_factory,
        'select count(*) from "{s}".meta_relation r'
        ' join "{s}".meta_table t on t.id = r.from_table_id'
        ' join "{s}".meta_database d on d.id = t.database_id'
        " where r.datasource_id = :ds and r.source_kind = 'extracted'"
        " and d.schema_name = :schema",
        ds=ds_id,
        schema=schema_name,
    )


async def test_多库同步时后写的库不能删光先写的库的边(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """§4 的 delete-diff 只作用于"本轮写完的那些库"，不是整个数据源。

    演示库只有一个 schema，这条缺陷在里面永远看不见：`meta_relation_replace` 的 DELETE
    若以 datasource 为范围，同一个事务里后写的库会把先写的库刚落的 extracted 边整批
    收走——单库用例照样绿，多 schema 的库里边只剩最后一个库的。
    """
    acct = await login(username="owner-h", role="member")
    ds_id = await _register(client, acct, include_schemas=["shop", "warehouse"])
    tables = {"shop": ("orders", "users"), "warehouse": ("shipments", "orders")}
    stub(
        catalogs=("shop", "warehouse"),
        tables=tables,
        fks={
            "shop": [_fk("shop", "orders", "user_id", "users")],
            "warehouse": [_fk("warehouse", "shipments", "order_id", "orders")],
        },
    )

    first = await _sync(client, acct, ds_id)
    assert first.status_code == 202, first.text
    assert first.json()["counters"]["relations_extracted"] == 2, "两个库各一条真外键"
    assert await _extracted_edges(session_factory, ds_id, "shop") == 1
    assert await _extracted_edges(session_factory, ds_id, "warehouse") == 1, (
        "后写的 warehouse 不能删光先写的 shop 刚落的边"
    )

    # 第二轮：源库里 warehouse 的外键被删掉了，shop 的还在
    stub(
        catalogs=("shop", "warehouse"),
        tables=tables,
        fks={"shop": [_fk("shop", "orders", "user_id", "users")]},
    )
    second = await _sync(client, acct, ds_id)
    assert second.status_code == 202, second.text
    assert second.json()["counters"]["relations_extracted"] == 1
    assert await _extracted_edges(session_factory, ds_id, "shop") == 1, "还在的外键不许被差分收走"
    assert await _extracted_edges(session_factory, ds_id, "warehouse") == 0, (
        "源库删掉的外键必须被 delete-diff 收走——外键为 0 也得走 prune 那半边"
    )


async def test_还没连上源库就失败也要给_job_写终局(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """解密口令/建 spec/取方言这三步在抽取开始之前；它们炸了也必须给 job 写终局。

    上一版这三步在 try 外面：密钥对不上时 job 永远停在 'running'，`ux_sync_running`
    从此把这条源锁死（此后每次同步都 409）。终局收尾在 `finally`（metadata-model §6
    as-built 1），这条钉的是"抽取一步都没发生也要走到那句收尾"。
    """
    acct = await login(username="owner-g", role="member")
    ds_id = await _register(client, acct)
    stub()
    real_decrypt = sync_service.decrypt_secret

    def _bad_key(blob: bytes) -> str:
        raise AppError("源库口令解密失败：当前 AIWEB_FERNET__KEYS 里没有能解开它的密钥")

    monkeypatch.setattr(sync_service, "decrypt_secret", _bad_key)
    first = await _sync(client, acct, ds_id)
    assert first.status_code == 202, first.text
    assert first.json()["errors"][0]["code"] == "internal_error"

    monkeypatch.setattr(sync_service, "decrypt_secret", real_decrypt)
    second = await _sync(client, acct, ds_id)
    assert second.status_code == 202, "密钥修好后这条源必须还能同步——上一次的 job 不许挡路"
    assert second.json()["status"] == "success"

    failed = await _scalar(
        session_factory,
        "select count(*) from \"{s}\".sync_jobs where datasource_id = :ds and status = 'failed'",
        ds=ds_id,
    )
    assert failed == 1, "炸在抽取之前的那次也要留下 failed 终局的痕迹"
    stuck = await _scalar(
        session_factory,
        'select count(*) from "{s}".sync_jobs where datasource_id = :ds'
        " and status in ('pending', 'running')",
        ds=ds_id,
    )
    assert stuck == 0, "每条 job 都要有终局，否则部分唯一索引下次一定撞锁"


async def test_泛列只出告警不丢边(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
) -> None:
    """§5.3 末"候选度 > 8"在写侧只做告警（工单 014 拍板）——两条断言各钉一半。

    告警要真的穿过编排落到同步响应里（否则用户在界面上看不见"这库同名列太泛"）；
    边要一条不少（"需另一端有唯一约束，否则丢弃"那半句在现行减法规则下不可达，
    谁把它实现成过滤，用户看到的就是静默少边、跨表问答无声降级成单表）。
    """
    acct = await login(username="owner-generic-col", role="member")
    ds_id = await _register(client, acct)
    # 9 张事实表 + 1 张 org 维度表，每张都带一个 org_id → 候选度 10 > 8。
    stub(
        tables={"shop": (*tuple(f"t{i}" for i in range(9)), "org")},
        id_is_pk=True,
        extra_column="org_id",
    )

    resp = await _sync(client, acct, ds_id)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    generic = [w for w in body["warnings"] if w["code"] == "join_field_too_generic"]
    assert len(generic) == 1, body["warnings"]
    assert "`shop.org_id` 出现在 10 张表" in generic[0]["detail"], generic

    # org 自己那条是自环，被减法规则收走（008 之前就钉过），所以是 9 而不是 10。
    assert body["counters"]["relations_inferred"] == 9, body["counters"]


async def test_postgres_源的范围条件按_pg_的列写法下发(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
) -> None:
    """真 `run_sync` 的范围条件列写法跟着 `ds.kind` 走，不再是代码记忆里的那一个（验收 2）。

    `tests/unit/test_sync_dialect_dispatch.py` 钉的是驱动表里"工厂 + 列写法"成不成对；这一条钉的
    是**编排层真的去取了它**——桩件把收到的 `table_sql` 原样记下（它自己不渲染口径），红了就是
    "PG 源带着 MySQL 的别名出发了"，而那一句要等源库报 42703/1054 才看得见。
    这里用形状桩件而不是真 PG 抽取器：被验的是编排那一次分发，方言自己的 SQL 在单测与 live 里。
    """
    acct = await login(username="owner-pg-col", role="member")
    ds_id = await _register(
        client,
        acct,
        kind="postgres",
        port=5432,
        catalog_name="ai_web_demo_pg",
        include_schemas=["demo"],
        exclude_tables=["\\_%"],
    )
    extractor = stub(catalogs=("demo",), tables={"demo": ("orders",)})

    resp = await _sync(client, acct, ds_id)
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "success", resp.text
    assert extractor.scope_sqls, "桩件一次都没被问到范围条件"
    for sql in extractor.scope_sqls:
        assert sql is not None and "c.relname" in sql, sql
        assert "t.table_name" not in sql, sql


async def test_progress_成功收尾时那一格是100(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """025 验收 3：`sync_jobs.progress` 第一次有写入点，而且写的是**事件帧里那两个数**的比值。

    分工口径（metadata-model §2.8 决策 3）：序列真相在 `sync_job_event`，这一格只是同一份
    `done`/`total` 折出来的派生百分比。所以断言是两半：这一格等于 100，**且**它与最后一帧
    事件里的 counters 自洽——只断前一半的话，写死 100 也能过。
    """
    acct = await login(username="owner-prog-ok", role="member")
    ds_id = await _register(client, acct)
    stub()  # 单库单表：分母 1、跑完 done 也是 1

    resp = await _sync(client, acct, ds_id)
    assert resp.status_code == 202, resp.text
    job_id = int(resp.json()["job_id"])
    assert resp.json()["status"] == "success", resp.text

    stored = await _scalar(
        session_factory, 'select progress from "{s}".sync_jobs where id = :id', id=job_id
    )
    assert float(stored) == 100.0, stored
    last = await _scalar(
        session_factory,
        'select counters from "{s}".sync_job_event where job_id = :id order by seq desc limit 1',
        id=job_id,
    )
    assert last is not None and int(last["done"]) == int(last["total"]) == 1, last


async def test_progress_中途有库失败时停在已提交的比例(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`partial` 收尾的百分比是 2/3 而不是 100 也不是 0：分母在 discover 就定死了。

    手算口径来自桩件自己那份事实：shop 2 张 + warehouse 1 张 → `count_scope` 给 total=3，
    warehouse 整库 rollback → `done` 只涨到 2，2/3 = 66.666… → `numeric(5,2)` 存 66.67。
    这一条同时挡住两种写法：一是"全跑完才写"（partial 时永远停在 0，进度条说不上话），
    二是"按提交过的库数折算"（2 个库里成了 1 个 → 50，而 §2.8 的进度是**对象级**的）。
    """
    acct = await login(username="owner-prog-partial", role="member")
    ds_id = await _register(client, acct, include_schemas=["shop", "warehouse"])
    stub(
        catalogs=("shop", "warehouse"),
        tables={"shop": ("orders", "users")},
        fail_on="warehouse",
    )

    resp = await _sync(client, acct, ds_id)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "partial", body
    stored = await _scalar(
        session_factory,
        'select progress from "{s}".sync_jobs where id = :id',
        id=int(body["job_id"]),
    )
    assert float(stored) == 66.67, stored


async def test_progress_分母还没定出来时留在默认0(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """炸在 discover 之前的作业，`progress` 必须是 `DEFAULT 0` 那个 0，不是有人写进去的数。

    和 `tests/unit/test_sync_progress.py::test_分母还没定出来时不写这一格` 各管一半：那边管
    `progress_of` 在 `total<=0` 时回 None（一个数的产生规则），这里管**从没产生过任何一帧**时
    读侧看见什么。注入点在 `decrypt_secret`（`sync_service.py:1009`），比 `count_scope` 早，
    所以带 counters 的那一路一次都没走到，`progress` 全程没进过 `values`——这里的 0.0 是建表
    默认值，不是谁写进去的 0.0。写成 0.0 的话，读侧看不出"抽了一半失败"和"连上都没连上"的区别。
    """
    acct = await login(username="owner-prog-zero", role="member")
    ds_id = await _register(client, acct)
    stub()
    monkeypatch.setattr(
        sync_service,
        "decrypt_secret",
        lambda blob: (_ for _ in ()).throw(AppError("源库口令解密失败：用例注入")),
    )

    resp = await _sync(client, acct, ds_id)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "failed", body
    stored = await _scalar(
        session_factory,
        'select progress from "{s}".sync_jobs where id = :id',
        id=int(body["job_id"]),
    )
    assert float(stored) == 0.0, stored
    frames = await _scalar(
        session_factory,
        'select count(*) from "{s}".sync_job_event where job_id = :id',
        id=int(body["job_id"]),
    )
    assert frames == 1, "只有收尾那一帧：注入点比第一帧还早，全程没有一帧带过 counters"
