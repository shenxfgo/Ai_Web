"""卡片逐表提交的失败隔离（工单 019，roadmap P3 验收 5 的落点）。

一句话：注入让某几张表的卡片构建抛错，**已提交的那几张卡不许被回滚射程扫到**。
这条只能在真链路上钉（HTTP 发起 → 真入队 → worker 跑 → 落库回读）：形状测试看不见
事务边界，而事务边界正是这张切片存在的理由（metadata-model §6 as-built 拍板第 5 条：
"3 张表抛错、其余已落库"之所以成立，是因为卡片改成了**逐表提交**）。

期望值口径：
- `partial` 的定义（CONTEXT.md 术语表：部分表失败、其余已落库，`errors` 里带表名）；
- `sync_jobs.errors` 的形状 `{code, detail}`（+ 可选 `data`），与元数据侧失败分类同构
  （metadata-model §2.5 as-built P3-016）；
- `card_build_failed` 这个 code 的原文来自 metadata-model §2.8 的事件 payload 示例；
- 卡片文本以 008 的 golden 快照 `tests/fixtures/prompts/kb_card__*.expected.txt` 为
  独立真相源（§2.1）——事务边界改了，内容一个字不许变；
- `counters.cards` = 本轮写出的卡片条数（工单 019 已定口径，只改事务边界不改计数）。

注入钩子是 test-only 的（工单 019 已定口径）：由参数传入、一路穿到 `sync_cards`，
生产路径（`loop()` / `run_sync` 默认值）拿到的永远是 `None`，不存在
"如果这是测试就抛错"的分支。桩抽取器直接复用 `test_sync_pg.py` 的那只（016 交付的
真链路进入方式：POST 只回 202，作业由 `run_once` 消费——工单 016 已定口径"不起子进程"）。
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  —— 全模型注册，FK 探测才看得见 chat_*（同 test_kb_cards_pg）
from app.core.security import encrypt_secret, hash_password
from app.models.datasource import DataSource
from app.models.kb import KbCard
from app.models.meta import (
    MetaColumn,
    MetaDatabase,
    MetaIndex,
    MetaIndexColumn,
    MetaRelation,
    MetaTable,
)
from app.models.user import User
from app.services import sync_service
from app.services.kb_service import sync_cards
from tests.conftest import load_script
from tests.integration.conftest import Account
from tests.integration.test_sync_pg import StubExtractor

pytestmark = [pytest.mark.pg]

Login = Callable[..., Awaitable[Account]]

RUN_WORKER = load_script(
    "run_worker_for_card_isolation_tests",
    Path(__file__).resolve().parents[2] / "scripts" / "run_worker.py",
)

PASSWORD = "口令-不会出现在任何响应里"
DS_BODY = {
    "name": "card-iso-src",
    "kind": "mysql",
    "host": "stub.invalid",
    "port": 3306,
    "connect_user": "stub",
    "connect_password": PASSWORD,
    "include_schemas": ["shop"],
}


def boom_on(names: set[str]) -> Callable[[str], None]:
    """注入钩子：全名在 `names` 里的表，卡片构建抛错。

    用 RuntimeError（不是 AppError）：和 `StubExtractor.fail_on` 同一条理由——真要说的是
    "任何没分类过的异常都不许把已成功的那几张卡带走"，用自己分类过的异常反而是最平易的路。
    """

    def _hook(table_full_name: str) -> None:
        if table_full_name in names:
            raise RuntimeError(f"注入故障：卡片构建失败（{table_full_name}）")

    return _hook


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Callable[..., StubExtractor]:
    """与 `test_sync_pg.stub` 同一进入点：只换 `_extractor_for`，被验的是真 `run_sync`。"""

    def build(**kw: Any) -> StubExtractor:
        extractor = StubExtractor(**kw)
        monkeypatch.setattr(sync_service, "_extractor_for", lambda ds, spec: extractor)
        return extractor

    return build


async def _register(client: AsyncClient, acct: Account, **over: Any) -> int:
    resp = await client.post("/api/datasources", json={**DS_BODY, **over}, headers=acct.headers)
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


async def _run_chain(
    client: AsyncClient,
    acct: Account,
    ds_id: int,
    *,
    card_build_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """真 HTTP 发起 → 真入队 → worker 循环体跑完 → 回读库里那一行的终局。

    worker 用一条新连接（真机上它们不在同一个进程里）；NullPool + 用完 dispose，
    免得闲置连接挡住随机 schema 结尾那句 DROP SCHEMA（同 `test_sync_pg._sync`）。
    """
    resp = await client.post("/api/sync/jobs", json={"datasource_id": ds_id}, headers=acct.headers)
    assert resp.status_code == 202, resp.text
    job_id = int(resp.json()["job_id"])

    engine = create_async_engine(os.environ["AIWEB_PG_TEST_DSN"], poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await RUN_WORKER.run_once(session, card_build_hook=card_build_hook)
        schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
        async with factory() as session:
            row = (
                (
                    await session.execute(
                        text(f'select * from "{schema}".sync_jobs where id = :id'), {"id": job_id}
                    )
                )
                .mappings()
                .one()
            )
        return dict(row)
    finally:
        await engine.dispose()


async def _card_tables(
    session_factory: async_sessionmaker[AsyncSession], ds_id: int
) -> dict[str, int]:
    """库里现存的卡片按表名归堆：{表全名: 该表卡片条数}。"""
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        f"select t.schema_name || '.' || t.table_name as full_name,"
                        f" count(*) as n"
                        f' from "{schema}".kb_card c'
                        f' join "{schema}".meta_table t on t.id = c.table_id'
                        f" where c.datasource_id = :ds"
                        f" group by 1"
                    ),
                    {"ds": ds_id},
                )
            )
            .mappings()
            .all()
        )
    return {str(r["full_name"]): int(r["n"]) for r in rows}


async def test_注入一张表卡片构建抛错_终局partial_其余表卡片照常在场(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """roadmap P3 验收 5 的最小格：一张炸、三张在，终局必须说真话。

    期望值全部来自文档，不来自实现：
    ① 终局 `status='partial'`（CONTEXT.md：部分表失败、其余已落库）；
    ② `errors` 是 `[{code, detail}]` 形状、`code='card_build_failed'`
       （metadata-model §2.8 payload 示例点名的 code；§2.5 钉的形状）；
    ③ `errors[].detail` 里带表名（partial 词条原文："`errors` 里带表名"）；
    ④ `counters.cards` = 成功写出的卡片条数（工单 019 已定口径：语义仍是本轮写出的卡片条数）。
    """
    acct = await login(username="iso-a", role="member")
    ds_id = await _register(client, acct)
    stub(tables={"shop": ("t1", "t2", "t3", "t4")})

    row = await _run_chain(client, acct, ds_id, card_build_hook=boom_on({"shop.t3"}))

    assert row["status"] == "partial", row
    errors = row["errors"]
    assert len(errors) == 1, errors
    assert set(errors[0]) <= {"code", "detail", "data"}, errors[0]
    assert errors[0]["code"] == "card_build_failed", errors
    assert "shop.t3" in errors[0]["detail"], errors
    assert isinstance(errors[0]["detail"], str), "detail 一律是字符串（§2.5 as-built P3-016）"
    # t3 之外每张表一张 table 卡；t3 一张都不该在场（它构建失败）
    assert await _card_tables(session_factory, ds_id) == {
        "shop.t1": 1,
        "shop.t2": 1,
        "shop.t4": 1,
    }
    assert row["counters"]["cards"] == 3, "计数只算写出的卡片条数，失败那张不进账"


async def test_注入三张表卡片构建抛错_终局partial_errors含三张表名_其余五张在场(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """工单 019 验收 1 的字面形状：真 HTTP 发起 → 真入队 → worker 跑，3 张炸、5 张在。

    这就是 roadmap P3 验收 5 那一格（注入点从 embed 换成 card_build，P3 不落 embed）。
    `errors` 里每条都要带得出"炸的是哪张表"——三张表名逐一点齐，而不是"有一条错误记录"。
    """
    acct = await login(username="iso-b", role="member")
    ds_id = await _register(client, acct)
    stub(tables={"shop": tuple(f"t{i}" for i in range(1, 9))})  # t1..t8

    failed = {"shop.t3", "shop.t5", "shop.t8"}
    row = await _run_chain(client, acct, ds_id, card_build_hook=boom_on(failed))

    assert row["status"] == "partial", row
    errors = row["errors"]
    assert len(errors) == 3, errors
    assert [e["code"] for e in errors] == ["card_build_failed"] * 3, errors
    for name in sorted(failed):
        assert any(name in e["detail"] for e in errors), f"{name} 必须点得出名：{errors}"
    # 其余五张照常在场（每张一 table 卡），失败三张一张都不在
    assert await _card_tables(session_factory, ds_id) == {f"shop.t{i}": 1 for i in (1, 2, 4, 6, 7)}
    assert row["counters"]["cards"] == 5


async def test_排在后面的表失败_前面已提交的卡片仍查得到(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """工单 019 验收 2——这张切片存在的理由，形状测试原理上看不见它。

    卡片按表边界 commit（`sync_cards` 循环体）：失败那张表自己 rollback 时，**已提交**的
    前面几张不在回滚射程内。反证：恢复"攒够全部 rows 再一次 upsert、调用方单次提交"的
    旧形状，最后那张一炸、整体回滚，前面五张卡全查不到，这条当场变红。

    回读走 `session_factory` 的另一条连接（worker 那条已 dispose）：跨连接可见才算真提交，
    而不是同一事务里自欺。表遍历序是 `meta_table.id`（建表插入序），t6 排在 t1..t5 之后。
    """
    acct = await login(username="iso-c", role="member")
    ds_id = await _register(client, acct)
    stub(tables={"shop": tuple(f"t{i}" for i in range(1, 7))})  # t1..t6

    row = await _run_chain(client, acct, ds_id, card_build_hook=boom_on({"shop.t6"}))

    assert row["status"] == "partial", row
    assert "shop.t6" in row["errors"][0]["detail"], row["errors"]
    assert await _card_tables(session_factory, ds_id) == {f"shop.t{i}": 1 for i in range(1, 6)}, (
        "t6 失败之前，t1..t5 的卡片已各自提交落库——回滚只许作废 t6 自己"
    )


async def _meta_counts(
    session_factory: async_sessionmaker[AsyncSession], ds_id: int
) -> tuple[int, int]:
    """`(meta_table 行数, meta_column 行数)`，都限定在这个数据源上。"""
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        tables = (
            await session.execute(
                text(f'select count(*) from "{schema}".meta_table where datasource_id = :ds'),
                {"ds": ds_id},
            )
        ).scalar_one()
        columns = (
            await session.execute(
                text(
                    f'select count(*) from "{schema}".meta_column c'
                    f' join "{schema}".meta_table t on t.id = c.table_id'
                    f" where t.datasource_id = :ds"
                ),
                {"ds": ds_id},
            )
        ).scalar_one()
    return int(tables), int(columns)


async def test_卡片失败隔离不漏到元数据侧_注入前后meta行数一行不差(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., StubExtractor],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """工单 019 验收 3：逐表提交只发生在卡片侧，元数据侧"每库一事务"原样不动。

    先来一轮不注入的全成功同步数出基线行数，再来一轮同样对象、注入三张表卡片失败的同步：
    `meta_table`/`meta_column` 的行数必须**一行不少、一行不多**。反例是卡片侧的 rollback
    越界扫到元数据（比如把提交边界挪回库级、或失败处理顺手回滚了还没提交的 meta 写）——
    那会让"部分表失败、其余已落库"这句话只对卡片成立、对元数据不成立。
    """
    acct = await login(username="iso-d", role="member")
    ds_id = await _register(client, acct)
    stub(tables={"shop": tuple(f"t{i}" for i in range(1, 9))})  # t1..t8

    clean = await _run_chain(client, acct, ds_id)
    assert clean["status"] == "success", clean
    baseline = await _meta_counts(session_factory, ds_id)
    assert baseline == (8, 16), "每张表 id/name 两列——基线本身也要有独立期望值，不是拿到啥算啥"

    injected = await _run_chain(
        client, acct, ds_id, card_build_hook=boom_on({"shop.t2", "shop.t5", "shop.t7"})
    )
    assert injected["status"] == "partial", injected
    assert await _meta_counts(session_factory, ds_id) == baseline, (
        "注入只炸卡片，元数据一行不许少（metadata-model §6 as-built 拍板第 5 条）"
    )


# ============================================================ 验收 4：golden 逐字符
#
# 期望值住在 `tests/fixtures/prompts/kb_card__*.expected.txt`（docs/verification.md §2.1
# 的"卡片模板 golden | 快照"，008 手写、按 kb-workflow §5 逐行）——独立真相源。
# 喂进去的 meta_* 行按 §2.1 点名的场景摆成 golden 用例同构的输入。工单 019 改的是
# `sync_cards` 的事务边界（逐表 flush+commit），内容一个字都不许变：这条就是把
# "新边界写进库的 text_md" 与快照文件逐字符对。
# 场景选取口径：六个窄场景里注释/粒度全走人工列的（order_main 的索引在 golden 里是
# 手写序、而读料 SQL 按 is_primary DESC + index_name 排序——两条路的"顺序"本来就是两回事，
# 硬比会把渲染器的契约读歪），所以取输入能由 `meta_*` 行**原样**复现的五个场景。
# as-built(P3-023)：种子里的 `data_type` 字面跟着归一走了（`varchar` → `varchar(32)`），
# 快照那份是 023 重录过的——事务边界的断言与类型字面的断言是两回事，这里仍只比文本。

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "prompts"


def _fixture_text(name: str) -> str:
    return (FIXTURES / f"kb_card__{name}.expected.txt").read_text(encoding="utf-8").rstrip("\n")


async def _seed_db(session: AsyncSession, ds_id: int, catalog: str, schema: str) -> MetaDatabase:
    db = MetaDatabase(datasource_id=ds_id, catalog_name=catalog, schema_name=schema)
    session.add(db)
    await session.flush()
    return db


async def _seed_table(
    session: AsyncSession, db: MetaDatabase, name: str, **kw: object
) -> MetaTable:
    tbl = MetaTable(
        datasource_id=db.datasource_id,
        database_id=db.id,
        catalog_name=db.catalog_name,
        schema_name=db.schema_name,
        table_name=name,
        table_type=str(kw.pop("table_type", "BASE TABLE")),
        **kw,
    )
    session.add(tbl)
    await session.flush()
    return tbl


def _seed_column(session: AsyncSession, tbl: MetaTable, pos: int, name: str, **kw: Any) -> None:
    session.add(
        MetaColumn(
            table_id=tbl.id,
            ordinal_position=pos,
            column_name=name,
            data_type=str(kw.pop("data_type", "bigint")),
            raw_data_type=str(kw.pop("raw_data_type", "bigint")),
            **kw,
        )
    )


async def _texts_of(session: AsyncSession, table_id: int) -> list[str]:
    rows = await session.scalars(
        select(KbCard.text_md).where(KbCard.table_id == table_id).order_by(KbCard.seq)
    )
    return list(rows)


async def test_未注入时写进库的卡片文本与008golden快照逐字符相同(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """工单 019 验收 4：事务边界改成逐表提交之后，内容仍与 golden 一字不差。

    走 `sync_cards`（本片的改动点）而不是 `build_cards`（008 已钉）：这条要证的是
    "装配 → 生成 → 逐表 upsert → 提交"整条写侧管道对文本无损，读回来仍是快照那一份。
    """
    async with session_factory() as session:
        user = User(username="golden-owner", password_hash=hash_password("x"), role="admin")
        session.add(user)
        await session.flush()
        ds = DataSource(
            name="golden-src",
            kind="mysql",
            host="127.0.0.1",
            port=3306,
            connect_user="aiweb_ro",
            secret_enc=encrypt_secret("x"),
            server_version="5.7.44-log",
            created_by=user.id,
        )
        session.add(ds)
        await session.flush()

        demo = await _seed_db(session, ds.id, "", "ai_web_demo")
        # 场景 1（§2.1"无注释表"）：表/列注释、粒度、行数全部缺席，三级降级走左端
        t_nc = await _seed_table(session, demo, "t_no_comment")
        _seed_column(session, t_nc, 1, "id", nullable=False, is_primary_key=True)
        _seed_column(session, t_nc, 2, "note", data_type="text", raw_data_type="text")

        # 场景 2（"无 PK 表"）：规模行完整（行数 + 最近更新），一个主键都不许凭空标出
        t_pk = await _seed_table(
            session,
            demo,
            "t_no_pk",
            comment_zh="人工核对过的无主键表",
            granularity="一行 = 一个埋点事件",
            approx_rows=12345,
            last_analyze_at=dt.datetime(2026, 9, 20, tzinfo=dt.UTC),
        )
        _seed_column(
            session,
            t_pk,
            1,
            "event_type",
            data_type="varchar(32)",
            raw_data_type="varchar(32)",
            comment_zh="事件类型",
        )
        _seed_column(
            session,
            t_pk,
            2,
            "ts",
            data_type="datetime",
            raw_data_type="datetime",
            comment_zh="事件时间",
        )

        # 场景 3（"纯视图"）：无行数 → 【规模】整行降级，表名带（视图）
        view = await _seed_table(
            session,
            demo,
            "v_daily_sales",
            table_type="VIEW",
            comment_zh="每日销售汇总",
            granularity="一行 = 一天",
        )
        _seed_column(
            session, view, 1, "day", data_type="date", raw_data_type="date", comment_zh="统计日"
        )
        _seed_column(
            session,
            view,
            2,
            "order_count",
            data_type="bigint",
            raw_data_type="bigint",
            comment_zh="订单数",
        )
        _seed_column(
            session,
            view,
            3,
            "gmv",
            data_type="numeric(18,2)",
            raw_data_type="decimal(18,2)",
            comment_zh="成交额",
        )

        # 场景 4（60 列宽表）：主卡 + 两张切片；c60 是索引列 + 外键列，被提进主卡末尾
        wide = await _seed_table(
            session,
            demo,
            "product_stats_wide",
            comment_zh="商品统计宽表",
            granularity="一行 = 一个商品的汇总",
            approx_rows=1024,
        )
        product = await _seed_table(session, demo, "product")
        _seed_column(
            session,
            wide,
            1,
            "c1",
            data_type="int",
            raw_data_type="int",
            nullable=False,
            is_primary_key=True,
            comment_zh="主键",
        )
        for i in range(2, 61):
            _seed_column(
                session,
                wide,
                i,
                f"c{i}",
                data_type="int",
                raw_data_type="int",
                comment_zh=f"指标{i}",
            )
        idx = MetaIndex(
            table_id=wide.id,
            index_name="idx_c60",
            index_type="BTREE",
            is_unique=False,
            is_primary=False,
        )
        session.add(idx)
        await session.flush()
        session.add(MetaIndexColumn(index_id=idx.id, seq_in_index=1, column_name="c60"))
        session.add(
            MetaRelation(
                datasource_id=ds.id,
                source_kind="extracted",
                from_table_id=wide.id,
                from_column_name="c60",
                to_table_id=product.id,
                to_column_name="id",
            )
        )
        await session.flush()

        # 场景 5（"中文·空格表名"，PG 方言）：`public.订单 a b`，非 5.7 不拼方言约束句
        pub = await _seed_db(session, ds.id, "public", "")
        odd = await _seed_table(
            session, pub, "订单 a b", comment_zh="中文带空格的表名", approx_rows=1_000_000
        )
        _seed_column(session, odd, 1, "id", nullable=False, is_primary_key=True, comment_zh="主键")
        _seed_column(
            session,
            odd,
            2,
            "`折扣`",
            data_type="numeric",
            raw_data_type="numeric",
            comment_zh="折扣率",
        )
        await session.flush()

        mysql_ids = [t_nc.id, t_pk.id, view.id, wide.id]
        await sync_cards(
            session,
            datasource_id=ds.id,
            job_id=None,
            table_ids=mysql_ids,
            dialect_name="mysql",
            server_version="5.7.44-log",
        )
        await sync_cards(
            session,
            datasource_id=ds.id,
            job_id=None,
            table_ids=[odd.id],
            dialect_name="postgresql",
            # golden 那一份的【方言】行是 `postgresql 15`：`_server_major` 取前两段，
            # 单段字面 "15" 才复现得出来（"15.4" 会带出 15.4，是另一条合法形状）。
            server_version="15",
        )

        for tbl, names in [
            (t_nc, ["t_no_comment"]),
            (t_pk, ["t_no_pk"]),
            (view, ["v_daily_sales"]),
            (odd, ["odd_identifier_pg"]),
            (wide, ["product_stats_wide", "product_stats_wide__seq1", "product_stats_wide__seq2"]),
        ]:
            assert await _texts_of(session, tbl.id) == [_fixture_text(n) for n in names], (
                f"{names}：逐表提交改了事务边界，正文必须与 008 golden 快照逐字符相同"
            )
