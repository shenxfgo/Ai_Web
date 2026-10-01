"""真 PG 演示源跑通全程（工单 024 验收 1/3/4/5）：`ai_web_demo_pg` → 元数据库 → 卡片。

为什么必须 live：这几条钉的都是**真 catalog 查询经真驱动、真映射、真落库之后再读回来**的东西——
"索引列序对不对""`format_type` 的人话名有没有被归一搬坏""新鲜度那一格有没有被搬成 aware
`datetime` 原值"，每一件都跨 抽取→映射→落库→读回 四层，任何一层的单测只能证明自己那一段。
不连库那半边（`tests/unit/test_extract_pg_map.py` / `test_extract_pg_client.py`）钉的是形状与分支，
这一份钉的是它跑在真 `pg_catalog` 上时结果仍然是那些形状。
一句自我限定：**"两枚时间戳都在场时取较晚的那一枚"这一支 live 证不到**——源库实测 0 张表两枚
同时非空（见下面验收 5 那条用例的 docstring），分辨力仍在那颗两枚都摆出来的桩上。

期望值出处（**独立真相源，不是从实现 dump**）：
- `backend/scripts/init_demo_pg.sql` —— 建库脚本自己：10 个业务对象的名单与各表列数、
  14 棵索引与各自的列序、6 条真外键的名字与两端、7 类类型原料（`:535` 的 `typ` CTE 与 `:635`
  那张行数期望表同一份清单）、列注释原文。**工单 024 验收 3 那句"与 015 那条自检查询逐字一致"
  在这里只能对上一半**：§1.5.4 那条自检吐的全是**计数**（10/9/1/11 四个对象数、8/2 两处注释数、
  七类类型各一条 `>0`/`=1`），给不了"哪一位排第几、注释原文是什么、FK 两端是谁"这类形状事实，
  所以形状那半的真相源换成逐条读 `CREATE TABLE`/`CREATE INDEX`/`COMMENT ON` 语句本身。
  这是记录在案的偏离，不是把验收改了。
- `docs/metadata-model.md` §8.2（五条 SQL 的分工、`modifiers` 的判定按 `typname`）、
  §2.4 注 ④（022 拍板的 PG 档：两列取较晚者、视图不给造新鲜度）、§9（归一值域）、
  §2.2 末注（PG 的 `catalog_name` 就是目标库，不是 MySQL 那个空串）。
- `docs/verification.md` §1.5.5（登记参数逐字）与 §1.5（对象数口径）。

一条用例一次真跑，所以每条的体量大得远超同类（`test_sync_live.py` 每条 ~67 行，这里最长的
一条 ~180 行）：`_sync_pg_once` 每次都要登记一个新数据源 + 跑一整轮真同步（真连源库、真发
§8.2 五条 SQL、真落 82 列 14 索引 10 卡）。拆成"一题一条用例"会把 4 轮真跑变成 12 轮以上，
而那些题本来就共享同一轮跑出来的那一套行——拆开只是让闸更慢，不会让判定更严。
凭证只从 gitignored 的 `.setup/demo_pg_ro.env` 读（工单 015 外层脚本产出的那一份）：缺文件就
skip，换台机器不该把整个闸口卡红。口令不参与 repr、不进任何断言文本。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import Account, DbAccount
from tests.integration.test_sync_events_pg import _events
from tests.integration.test_sync_live import _counts, _one, _relation_edges, _rows
from tests.integration.test_sync_pg import _sync
from tests.unit.test_type_domain import assert_in_value_domain

pytestmark = [pytest.mark.pg, pytest.mark.live]

Login = Callable[..., Awaitable[Account]]
Factory = async_sessionmaker[AsyncSession]

ENV = Path(__file__).resolve().parents[2] / ".setup" / "demo_pg_ro.env"

# verification.md §1.5.5：登记的就是这一套参数（库名走 catalog_name，schema 走 include_schemas）
PG_DEMO_DB = "ai_web_demo_pg"
SOURCE_SCHEMA = "demo"

# init_demo_pg.sql 排除内部对象（`_aiweb_demo_marker`）之后的业务对象名单，按字典序
BUSINESS = (
    "category",
    "customer",
    "order_item",
    "order_main",
    "payment_record",
    "product",
    "product_stats_wide",
    "t_no_comment",
    "user_activity_log",
    "v_daily_sales",
)
BASE_TABLES = tuple(n for n in BUSINESS if n != "v_daily_sales")
TABLES = len(BASE_TABLES)
VIEWS = 1
# 各表列数逐张数过 init_demo_pg.sql 的 CREATE TABLE：7+3+8+9+6+6+10+25+4+4
COLUMNS = 82
# 14 棵 = 8 张有主键的表（`t_no_comment` 无主键、视图无索引各出 0 棵）+ uq_customer_phone +
# order_main_order_no_key + payment_record_trade_no_key + ix_product_name_lower +
# ix_product_on_sale + ix_order_main_customer_created
INDEXES = 14
# 15 位 = 14 棵各 1 位，其中 ix_order_main_customer_created 占 2 位
INDEX_COLUMNS = 15
FK_CONSTRAINTS = 6
# §5.3 的减法规则在 PG 夹具上该推出的两条边：`user_activity_log.product_id` 与
# `product_stats_wide.product_id` 都没有真约束，而 `product.id` 是单列主键；
# `user_id` 指向的表在这个库里不存在，不许编。
# （MySQL 演示库把 `product_stats_wide.product_id` 建成了真外键，所以那边只有一条——
# 差异来自两份夹具的 DDL，不是抽取层的方言分支。）
INFERRED_EDGES = (
    ("user_activity_log", "product_id", "product"),
    ("product_stats_wide", "product_id", "product"),
)
# 一表一卡：PG 侧最宽的 product_stats_wide 只有 25 列，过不了 kb-workflow §6 的 40 列切片线
CARDS = TABLES + VIEWS


@pytest.fixture(scope="module")
def pg_source() -> DbAccount:
    """`demo_pg_ro` 的连接四件套，只从 gitignored 那份 env 读。

    凭证类型复用 `conftest.DbAccount`——它的 `__repr__` 把口令那一格换成 `<redacted>`，
    这一条保护只该有一份实现：抄第二份的话，漂移的那一份会把真口令倒进终端。
    读法用 `dotenv_values` 而不是自己切行，与 `tests/conftest.py` 读 `.env` 同一棵树里的做法。
    """
    if not ENV.exists():
        pytest.skip(f"缺少 {ENV}：PG 演示源还没开通（工单 015 的 scripts/demo_pg.ps1）")
    values = {k: v for k, v in dotenv_values(ENV).items() if v}
    missing = [k for k in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD") if not values.get(k)]
    if missing:
        pytest.skip(f"{ENV} 里 {missing} 是空的：PG 演示源没开通完整")
    if values["PGUSER"] != "demo_pg_ro":
        # 拿超管跑这一轮的话，"权限没给宽"那几条会全绿成"竟然读得到"，把判定读反
        pytest.skip(f"{ENV} 的 PGUSER 是 {values['PGUSER']!r}，不是 demo_pg_ro：不复验只读口径")
    return DbAccount(
        values["PGUSER"], values["PGPASSWORD"], values["PGHOST"], int(values["PGPORT"])
    )


async def _register(client: AsyncClient, acct: Account, source: DbAccount, name: str) -> int:
    """按 §1.5.5 那份参数真登记一个 PG 源，返回新的 `data_sources.id`。

    没有复用 `test_sync_pg._register(client, acct, **over)`：那张底是 MySQL 的 `DS_BODY`
    （`kind='mysql'`、`catalog_name=""`），覆盖出来的请求体里"PG 的 catalog 是真库名"这一格
    正好是本片「已定口径」不许照抄空串的那一条，请求体要**逐字看得见**才有意义。
    三份 `_register` 的重复是记在案的技术债，归 025 一起收（工单 024 交付记录）。
    """
    resp = await client.post(
        "/api/datasources",
        json={
            "name": name,
            "kind": "postgres",
            "host": source.host,
            "port": source.port,
            # PG 的 catalog_name 就是目标库（metadata-model §2.2 末注），与 MySQL 恒空串不同
            "catalog_name": PG_DEMO_DB,
            "connect_user": source.user,
            "connect_password": source.password,
            "include_schemas": [SOURCE_SCHEMA],
            # init_demo_pg.sql:79 特意给标记表带了下划线前缀：这一条就是"排除规则在 PG 侧也生效"
            "exclude_tables": ["\\_%"],
        },
        headers=acct.headers,
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


async def _sync_pg_once(
    client: AsyncClient, login: Login, source: DbAccount, *, username: str, ds_name: str
) -> tuple[int, dict[str, Any]]:
    """登记 → `POST /api/sync/jobs`（202）→ 进程内 await worker 的循环体 → 带回终局那一行。

    走端点而不是直接调 `run_sync`：这一片要证的正是"一次 HTTP 调用之后 PG 源的东西进了库"，
    顺手把 016 的两跳（入队 + worker）和 024 改过的那次按 kind 分发一起走过——`kind='postgres'`
    在这里不再命中 501，就是这条链的第一格。
    """
    acct = await login(username=username, role="member")
    ds_id = await _register(client, acct, source, ds_name)
    submit = await _sync(client, acct, ds_id)
    assert submit.status_code == 202, submit.text
    body = submit.json()
    assert source.password not in submit.text, "同步作业的终局记录里出现了源库口令"
    assert body["status"] == "success", json.dumps(body, ensure_ascii=False)
    return ds_id, body


async def _source_stat_stamps(source: DbAccount) -> dict[str, tuple[Any, Any]]:
    """直接问一次源库的 `pg_stat_user_tables`（一条只读 SELECT）。

    验收 5 要断的是"落库那一格是不是源库两个时间戳里较晚的那个"，而源库那两列本身才是真相——
    从抽取器读回来的值再和它自己比就是同义反复。同步只读不写源库，所以这一条读到的还是同一对值。
    """

    def _query() -> dict[str, tuple[Any, Any]]:
        import psycopg

        with (
            psycopg.connect(
                host=source.host,
                port=source.port,
                user=source.user,
                password=source.password,
                dbname=PG_DEMO_DB,
                connect_timeout=10,
            ) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT relname, last_analyze, last_autoanalyze "
                "FROM pg_stat_user_tables WHERE schemaname = %s",
                (SOURCE_SCHEMA,),
            )
            return {row[0]: (row[1], row[2]) for row in cur}

    return await asyncio.to_thread(_query)


async def test_验收1_登记PG源跑通四段_终局success_事件可回放(
    client: AsyncClient,
    login: Login,
    pg_source: DbAccount,
    session_factory: Factory,
) -> None:
    """验收 1 + 验收 8 的 live 那一格：登记 → 入队 → worker 真跑 → 终局行与事件表都回读自库里。

    期望值出处：`init_demo_pg.sql` 的名单与 §1.5.5 的登记参数、§1.6.1 那份 counters 清单。
    必须 live 的理由：这一条走的每一步都跨层（HTTP → 队列 → 真 psycopg → 元数据库），
    桩件里"注册的数据源"这件事本身就不存在。
    """
    ds_id, body = await _sync_pg_once(
        client, login, pg_source, username="pg-live-owner", ds_name="pg-live"
    )

    assert body["errors"] == []
    # PG 侧不该有告警：最高字段度是 3（`product_id` 出现在 3 张表，门槛 8），
    # 而编码那一格由 015 的建库守卫钉住，mysql 侧的 CHARSET_SUSPECT 在 PG 不成立
    assert body["warnings"] == []
    counters = body["counters"]
    assert counters["databases"] == 1
    assert counters["tables"] == TABLES + VIEWS
    assert counters["tables_failed"] == 0
    # 演示库刚被 015 的建库脚本灌过数据、又被本轮之前的同步反复读过，`tables_stale` 该是 0
    # （非 0 意味着范围或新鲜度判定漂了），而 §1.6.1 那份清单逐字写着这个数
    assert counters["tables_stale"] == 0
    assert counters["columns"] == COLUMNS
    assert counters["indexes"] == INDEXES
    assert counters["cards"] == CARDS
    assert counters["relations_extracted"] == FK_CONSTRAINTS
    # 这一格是缺陷 1 的指标：catalog 查找键写死空串时它报 0，而库里真的少两行
    assert counters["relations_inferred"] == len(INFERRED_EDGES)
    # 默认批大小 200（工单 020 口径）在 10 个对象上就是一批
    assert counters["batches"] == 1
    # §1.6.1 声称那份清单是"逐字"抄实跑输出的，所以键集合本身也要钉：
    # 多一格（后来的片子顺手加的）或少一格（这一格其实没实现）都算文档与实跑分叉
    assert set(counters) == {
        "databases",
        "tables",
        "columns",
        "indexes",
        "relations_extracted",
        "relations_inferred",
        "tables_stale",
        "tables_failed",
        "cards",
        "batches",
    }, counters

    # 落库的分母与那本总账对得上（不是"计数自己说自己对"）
    assert await _counts(session_factory, ds_id) == {
        "meta_table": TABLES + VIEWS,
        "meta_column": COLUMNS,
        "meta_index": INDEXES,
        "meta_index_column": INDEX_COLUMNS,
        # 6 条真外键 + 2 条按命名约定推出的边
        "meta_relation": FK_CONSTRAINTS + len(INFERRED_EDGES),
    }
    # `counters["cards"]` 单独一行不落地数一遍：卡片住在 `kb_card` 而不是 `meta_*`，
    # `_counts` 那本账（MySQL 侧的兄弟）覆盖不到它，而验收 1 说的"建卡片"要的是行在场
    assert (
        await _one(
            session_factory,
            'select count(*) from "{s}".kb_card c'
            ' join "{s}".meta_table t on t.id = c.table_id'
            " where t.datasource_id = :ds",
            ds=ds_id,
        )
        == CARDS
    )

    kinds = {
        row["table_type"]: row["n"]
        for row in await _rows(
            session_factory,
            'select table_type, count(*) as n from "{s}".meta_table'
            " where datasource_id = :ds group by table_type",
            ds=ds_id,
        )
    }
    assert kinds == {"BASE TABLE": TABLES, "VIEW": VIEWS}

    names = {
        row["table_name"]
        for row in await _rows(
            session_factory,
            'select table_name from "{s}".meta_table where datasource_id = :ds',
            ds=ds_id,
        )
    }
    assert names == set(BUSINESS), f"`\\_%` 排除规则在 PG 侧没生效或名单漂了：{sorted(names)}"

    # §2.2 末注 + 工单 024 已定口径：PG 侧的 catalog_name 是真库名，不许照抄 MySQL 的空串
    for catalog_value, want in ((PG_DEMO_DB, TABLES + VIEWS), ("", 0)):
        assert (
            await _one(
                session_factory,
                'select count(*) from "{s}".meta_table'
                " where datasource_id = :ds and catalog_name = :cat",
                ds=ds_id,
                cat=catalog_value,
            )
            == want
        )

    # 017 的事件表：这一轮真跑留下的帧必须能按 seq 回放
    events = await _events(session_factory, int(body["job_id"]))
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1)), "seq 不连续"
    stages = {e["stage"] for e in events}
    assert stages <= {"extract", "embed", "upsert", "card_build", "done"}, stages
    assert events[-1]["stage"] == "done", events[-1]
    # 020 的形状在 PG 侧同样成立：批名单随 `tables` 那一帧的 payload 交回
    tables_frame = next(e for e in events if e["phase"] == "tables")
    batches = tables_frame["payload"]["batches"]
    assert [len(b) for b in batches] == [10], batches
    assert sorted(next(iter(batches))) == sorted(BUSINESS)


async def test_验收3_索引列序_列注释_外键_视图落库结果与建库脚本逐字一致(
    client: AsyncClient,
    login: Login,
    pg_source: DbAccount,
    session_factory: Factory,
) -> None:
    """验收 3 + 验收 7 的 PG 半边：落库的形状逐位对，而逐位的期望值一条都不从实现取。

    真相源是 `init_demo_pg.sql` 里那 14 条 `CREATE INDEX`/`PRIMARY KEY`、逐条 `COMMENT ON`
    与 6 条 `ADD CONSTRAINT`；§1.5.4 的自检只给计数（见模块 docstring 那条偏离）。
    必须 live：列序来自 `unnest(indkey) WITH ORDINALITY` 经真目录、真驱动之后的顺序，
    中文注释经的是 libpq 的编码转换——这两件在桩里都只能"我自己排一个序再比我自己"。
    """
    ds_id, _ = await _sync_pg_once(
        client, login, pg_source, username="pg-shape-owner", ds_name="pg-shape"
    )

    # ---- 索引：谁、第几位、列名（表达式那一位在 PG 侧就是 NULL）
    shape = await _rows(
        session_factory,
        "select t.table_name as tbl, i.index_name as idx, i.index_type as type,"
        "       i.cardinality as card, ic.seq_in_index as seq, ic.column_name as col,"
        "       ic.sub_part as sub_part"
        ' from "{s}".meta_index i'
        ' join "{s}".meta_index_column ic on ic.index_id = i.id'
        ' join "{s}".meta_table t on t.id = i.table_id'
        " where t.datasource_id = :ds"
        " order by t.table_name, i.index_name, ic.seq_in_index",
        ds=ds_id,
    )
    assert len(shape) == INDEX_COLUMNS, shape
    # 全库没有一条 `USING gin/gist`（init_demo_pg.sql 里 0 处 USING），而 D 条取的是 `am.amname`
    assert {r["type"] for r in shape} == {"btree"}
    # 偏离 4：`idx_scan` 是被扫描次数而不是不同值个数，抽取器因此不给 cardinality 造假值
    assert all(r["card"] is None for r in shape)
    # 前缀长度是 MySQL 特有的形状
    assert all(r["sub_part"] is None for r in shape)
    assert [(r["tbl"], r["idx"], r["seq"], r["col"]) for r in shape] == [
        ("category", "category_pkey", 1, "id"),
        ("customer", "customer_pkey", 1, "id"),
        ("customer", "uq_customer_phone", 1, "phone"),
        ("order_item", "order_item_pkey", 1, "id"),
        ("order_main", "ix_order_main_customer_created", 1, "customer_id"),
        # 复合索引的第二位：列序来自 `unnest(i.indkey) WITH ORDINALITY`，写错就换不了位
        ("order_main", "ix_order_main_customer_created", 2, "created_at"),
        ("order_main", "order_main_order_no_key", 1, "order_no"),
        ("order_main", "order_main_pkey", 1, "id"),
        ("payment_record", "payment_record_pkey", 1, "id"),
        ("payment_record", "payment_record_trade_no_key", 1, "trade_no"),
        # 表达式索引：`indkey` 那一位是 0，LEFT JOIN pg_attribute 接不上 → 列名 NULL
        ("product", "ix_product_name_lower", 1, None),
        ("product", "ix_product_on_sale", 1, "price"),
        ("product", "product_pkey", 1, "id"),
        ("product_stats_wide", "product_stats_wide_pkey", 1, "product_id"),
        ("user_activity_log", "user_activity_log_pkey", 1, "id"),
    ]
    # 表达式本体今天落在**空**里：列是有的（§2.4 的 `meta_index.funcdef`，`models/meta.py:199`，
    # 而同文件 :213 那句"名字落在 funcdef 里"说的就是它），但 `RawIndexColumn` 没有搬运它的字段，
    # D 条取到的 `pg_get_indexdef(...) AS def` 因此在映射处被丢弃。这一条钉的是当前状态，
    # 不是"这格永远该空"——补它的人要让 D 那条的 def 走到落库，连卡片模板一起看。
    assert (
        await _one(
            session_factory,
            'select i.funcdef from "{s}".meta_index i'
            ' join "{s}".meta_table t on t.id = i.table_id'
            " where t.datasource_id = :ds and i.index_name = 'ix_product_name_lower'",
            ds=ds_id,
        )
        is None
    )

    # ---- 列注释：中文原文逐字（编码是 PG 侧唯一"变了也会静默"的风险点——libpq 转不动会报错
    # 而不是把中文换成问号，所以这里必须靠真字符来证）
    comments = {
        (r["tbl"], r["col"]): r["cmt"]
        for r in await _rows(
            session_factory,
            "select t.table_name as tbl, c.column_name as col, c.comment_raw as cmt"
            ' from "{s}".meta_column c join "{s}".meta_table t on t.id = c.table_id'
            " where t.datasource_id = :ds",
            ds=ds_id,
        )
    }
    assert comments[("product", "tags")] == "标签数组（text[]，归一化考点）"
    assert comments[("order_main", "status")] == (
        "订单状态：pending/paid/shipped/completed/cancelled/refunding"
    )
    assert comments[("user_activity_log", "occurred_at")] == "事件时刻数组（timestamptz[]）"
    # 表注释走 §8.2 B 的 obj_description 那一列
    assert (
        await _one(
            session_factory,
            'select comment_raw from "{s}".meta_table'
            " where datasource_id = :ds and table_name = 'product_stats_wide'",
            ds=ds_id,
        )
        == "商品运营统计宽表（按天累计口径）"
    )
    # 无注释的表读成 None 而不是空串（`_opt_text` 那一支）
    assert (
        await _one(
            session_factory,
            'select comment_raw from "{s}".meta_table'
            " where datasource_id = :ds and table_name = 't_no_comment'",
            ds=ds_id,
        )
        is None
    )

    # ---- 外键：6 条 extracted，名字、两端与 on_delete 全按建库脚本
    edges = await _relation_edges(session_factory, ds_id)
    extracted = [r for r in edges if r["source_kind"] == "extracted"]
    assert {
        (r["from_table"], r["from_column"], r["to_table"], r["to_column"]) for r in extracted
    } == {
        ("category", "parent_id", "category", "id"),
        ("product", "category_id", "category", "id"),
        ("order_main", "customer_id", "customer", "id"),
        ("order_item", "order_id", "order_main", "id"),
        ("order_item", "product_id", "product", "id"),
        ("payment_record", "order_id", "order_main", "id"),
    }
    assert {r["fk_name"] for r in extracted} == {
        "fk_category_parent",
        "fk_product_category",
        "fk_order_main_customer",
        "fk_order_item_order",
        "fk_order_item_product",
        "fk_payment_order",
    }
    # 建库脚本一条 ON DELETE 子句都没写 → confdeltype/confupdtype 都是 'a' → NO ACTION
    assert {r["on_delete"] for r in extracted} == {"NO ACTION"}
    # 真约束不需要打分（§2.4）
    assert all(float(r["confidence"]) == 1.0 for r in extracted)

    # 推断边按 §5.3 该推出的那两条（名单与理由在文件头的 `INFERRED_EDGES`）
    inferred = [r for r in edges if r["source_kind"] == "inferred"]
    assert {(r["from_table"], r["from_column"], r["to_table"]) for r in inferred} == set(
        INFERRED_EDGES
    )
    assert all(float(r["confidence"]) == 1.0 for r in inferred), "两侧同为 int，四项加成应全中"
    assert all(r["fk_name"] is None for r in inferred)

    # ---- 视图：table_type=VIEW、四列、无注释（夹具刻意不写），engine 不被硬造
    view_cols = await _rows(
        session_factory,
        "select c.column_name as col, c.ordinal_position as ord, c.comment_raw as cmt"
        ' from "{s}".meta_column c join "{s}".meta_table t on t.id = c.table_id'
        " where t.datasource_id = :ds and t.table_name = 'v_daily_sales'"
        " order by c.ordinal_position",
        ds=ds_id,
    )
    assert [r["col"] for r in view_cols] == ["sale_day", "order_cnt", "total_amount", "avg_amount"]
    assert all(r["cmt"] is None for r in view_cols), "视图列注释缺失是夹具故意的，抽取器不该补造"
    assert (
        await _one(
            session_factory,
            'select engine from "{s}".meta_table'
            " where datasource_id = :ds and table_name = 'v_daily_sales'",
            ds=ds_id,
        )
        is None
    )
    # 堆表那一格：B 条的 `is_heap` 是个布尔，映射成访问方法的名字
    assert (
        await _one(
            session_factory,
            "select count(*) from \"{s}\".meta_table where datasource_id = :ds and engine = 'heap'",
            ds=ds_id,
        )
        == TABLES
    )
    # 偏离 6：A 条不 JOIN pg_class，所以库级尺寸/表数在 PG 侧是 NULL
    assert (
        await _one(
            session_factory,
            'select approx_size_bytes from "{s}".meta_database'
            " where datasource_id = :ds and schema_name = :sch",
            ds=ds_id,
            sch=SOURCE_SCHEMA,
        )
        is None
    )


async def test_验收4_七类类型原料各至少一列在场_data_type归一_raw存PG原文(
    client: AsyncClient,
    login: Login,
    pg_source: DbAccount,
    session_factory: Factory,
) -> None:
    """验收 4：015 铺的七类原料各至少一列在场，`data_type` 是 §9 归一值而 `raw_data_type` 留原文。

    原料名单是 `init_demo_pg.sql:536` 的 `typ` CTE（与它 `:635` 的行数断言同一份），
    成对的期望值按 metadata-model §9 手算。必须 live：`format_type(...)` 的原文只有真 PG 给得出，
    而"归一有没有把原文搬坏"要看两列同时落库后的样子。
    """
    ds_id, _ = await _sync_pg_once(
        client, login, pg_source, username="pg-type-owner", ds_name="pg-type"
    )

    landed = {
        (r["tbl"], r["col"]): r
        for r in await _rows(
            session_factory,
            "select t.table_name as tbl, c.column_name as col, c.data_type as norm,"
            "       c.raw_data_type as raw, c.char_length as len,"
            "       c.numeric_precision as prec, c.numeric_scale as scale,"
            "       c.default_value as dflt, c.is_generated as gen,"
            "       c.is_primary_key as pk, c.is_indexed as indexed"
            ' from "{s}".meta_column c join "{s}".meta_table t on t.id = c.table_id'
            " where t.datasource_id = :ds",
            ds=ds_id,
        )
    }
    assert len(landed) == COLUMNS

    # 015 铺的七类原料（init_demo_pg.sql 的 `typ` CTE 同一份清单），每类点名列一个：
    # raw 是 `format_type` 的人话名原文，norm 是 §9 的归一值
    for tbl, col, raw, norm in (
        ("customer", "remark", "text", "text"),
        ("product", "attrs", "jsonb", "jsonb"),
        ("product", "tags", "text[]", "text[]"),
        ("user_activity_log", "hit_ids", "integer[]", "int[]"),
        ("user_activity_log", "scores", "numeric(10,2)[]", "numeric(10,2)[]"),
        ("user_activity_log", "occurred_at", "timestamp with time zone[]", "timestamptz[]"),
        ("product_stats_wide", "top_keywords", "character varying(64)[]", "varchar(64)[]"),
    ):
        row = landed[(tbl, col)]
        assert row["raw"] == raw, (tbl, col, row["raw"])
        assert row["norm"] == norm, (tbl, col, row["norm"])

    # 每一列的归一值都必须在 §9 的共享值域里：越界字面会让跨方言比较退化成字符串相等
    for value in {r["norm"] for r in landed.values()}:
        assert_in_value_domain(value)

    # §8.2 C 的 modifiers 判定按 `typname` 走：varchar/numeric 取括号，数组列三个值全空
    # （`character varying(64)[]` 里那个 64 属于元素而不属于列，填上就是假话）
    name = landed[("customer", "name")]
    assert (name["raw"], name["norm"], name["len"]) == ("character varying(64)", "varchar(64)", 64)
    assert (name["prec"], name["scale"]) == (None, None)
    price = landed[("product", "price")]
    assert (price["raw"], price["norm"], price["len"]) == ("numeric(10,2)", "numeric(10,2)", None)
    assert (price["prec"], price["scale"]) == (10, 2)
    for tbl, col in (("product", "tags"), ("product_stats_wide", "top_keywords")):
        row = landed[(tbl, col)]
        assert (row["len"], row["prec"], row["scale"]) == (None, None, None), (tbl, col, row)

    # serial 与 identity 在 PG 里都是 integer，分开靠 default_value（015 自检同口径）：
    # serial 的真身是 pg_attrdef 里的 nextval，identity 列不走那条路；两者都不是生成列
    serial = landed[("customer", "id")]
    assert (serial["raw"], serial["norm"]) == ("integer", "int")
    assert str(serial["dflt"]).startswith("nextval("), serial["dflt"]
    assert serial["gen"] is False
    for tbl in ("order_main", "payment_record"):
        ident = landed[(tbl, "id")]
        assert ident["dflt"] is None and ident["gen"] is False, (tbl, ident)

    # 视图列的类型也是 format_type 出来的：`date_trunc('day', timestamptz)` 的结果**仍带时区**
    # （`order_main.created_at` 在夹具里是 timestamptz），与 MySQL 版那个 DATETIME 的
    # `DATE_FORMAT` 结果不同档
    sale_day = landed[("v_daily_sales", "sale_day")]
    assert (sale_day["raw"], sale_day["norm"]) == (
        "timestamp with time zone",
        "timestamptz",
    )
    # 主键旗标那一格靠 `apply_index_flags`（C 条自己不读 indkey，§7）：每表恰好一列被打标，
    # 而表达式索引那一位没有列可打（`column_name` 是 None 因此跳过，不该把整张表标脏）
    pk_by_table: dict[str, list[str]] = {}
    for row in landed.values():
        if row["pk"]:
            pk_by_table.setdefault(row["tbl"], []).append(row["col"])
    assert pk_by_table == {
        "category": ["id"],
        "customer": ["id"],
        "order_item": ["id"],
        "order_main": ["id"],
        "payment_record": ["id"],
        "product": ["id"],
        "product_stats_wide": ["product_id"],
        "user_activity_log": ["id"],
    }, pk_by_table
    assert "t_no_comment" not in pk_by_table, "无主键表被打上了主键列"
    assert "v_daily_sales" not in pk_by_table, "视图没有索引，不该有主键列"
    # `product` 上被索引的列只有 `id`（主键）与 `price`（`ix_product_on_sale` 的索引列，
    # 它的 `WHERE status='on_sale'` 只是谓词，不改索引列集合）。`name` 不在这张清单里：
    # 夹具给它建的是表达式索引 `ix_product_name_lower ((lower(name)))`，PG 的 `indkey` 那一位
    # 是 0，D 条接不到 `pg_attribute` 因此 `column_name` 回 NULL（验收 3 已按 NULL 钉过），
    # 于是也没有列可被打标。MySQL 版那边 `ix_product_name` 是真列索引，差异来自两份 DDL。
    assert sorted(r["col"] for r in landed.values() if r["tbl"] == "product" and r["indexed"]) == [
        "id",
        "price",
    ]

    # 演示库一个真枚举类型都没有（`typtype='e'` 实测 0 行，见 verification §1.5.4 末注），
    # 所以 §8.2 C 那条 `jsonb_agg(...)` 的形状在 live 这一路仍然没被证过。这里断的是
    # "抽取器不凭空造枚举值"，不是"枚举读对了"。
    # 两个数一起看才是这句话的意思：SQL NULL 那一格必须是全 82，因为 SQLAlchemy 的 JSON
    # 类型默认把 Python None 序列成 JSON 字面 `null`（`IS NOT NULL` 成立），而 §2.4 的
    # "没有值域"是 SQL NULL —— live 第一次跑就是被这一格打成 82。
    null_shape = (
        await _rows(
            session_factory,
            "select count(*) filter (where c.enum_values is null) as sql_null,"
            '       count(*) as total from "{s}".meta_column c'
            ' join "{s}".meta_table t on t.id = c.table_id'
            " where t.datasource_id = :ds",
            ds=ds_id,
        )
    )[0]
    assert (int(null_shape["sql_null"]), int(null_shape["total"])) == (COLUMNS, COLUMNS), null_shape


async def test_验收5_last_analyze_at_按两列取较晚者_视图与未分析过的表留空(
    client: AsyncClient,
    login: Login,
    pg_source: DbAccount,
    session_factory: Factory,
) -> None:
    """验收 5：每张表的 `last_analyze_at` 逐张等于源库那两枚时间戳里较晚的一枚，视图留空。

    期望值不是写死的常量而是**当场从源库读**（`_source_stat_stamps` 直连 `ai_web_demo_pg` 的
    `pg_stat_user_tables`）——这两个数由 autovacuum 决定，任何字面量都会在看机器的一天变红。
    必须 live：§2.4 注 ④ 那条"两列取较晚者、视图不给造新鲜度"要验的是真时间戳经真驱动
    （aware datetime）落进 `timestamptz` 再读回来还在不在。**注意分辨力**：演示库里没有任何
    一张表同时带着两枚非空时间戳，所以"较晚者"这个分支的判定力在 `test_extract_pg_map.py`
    那颗两枚都摆出来的桩上，这里证的是"搬运没走样 + 视图那支真的留 NULL"。
    """
    ds_id, _ = await _sync_pg_once(
        client, login, pg_source, username="pg-fresh-owner", ds_name="pg-fresh"
    )
    stamps = await _source_stat_stamps(pg_source)
    landed = {
        r["table_name"]: r["at"]
        for r in await _rows(
            session_factory,
            "select table_name, last_analyze_at as at"
            ' from "{s}".meta_table where datasource_id = :ds',
            ds=ds_id,
        )
    }

    for table in BASE_TABLES:
        pair = stamps.get(table, (None, None))
        want = max([value for value in pair if value], default=None)
        assert landed[table] == want, (table, landed[table], pair)

    # 视图不在 pg_stat_user_tables 里 → 天然是 NULL；而 §2.4 注 ② 那句"不给造新鲜度"是
    # rows_to_tables 里显式的那一支。两头都断，才挡住"拿 synced_at 填这一格"那种偷懒。
    assert "v_daily_sales" not in stamps
    assert landed["v_daily_sales"] is None

    # 024 实测过的那条形状账：PG 这两列是 timestamptz，驱动回来就是 aware datetime
    analyzed = [table for table in BASE_TABLES if landed[table] is not None]
    assert analyzed, "本机 autovacuum 一个表都没分析过的话，这一格其实什么都没证到（见交付记录）"
    assert all(landed[table].utcoffset() is not None for table in analyzed), analyzed
    # 小表（category 40 行、t_no_comment 50 行）够不着 autovacuum 的分析门槛 → 应当留空。
    # 这一条只在源库确实留空时才断，避免把"机器上刚被人手工 ANALYZE 过"变成用例红。
    for table in ("category", "t_no_comment"):
        if all(value is None for value in stamps.get(table, (None, None))):
            assert landed[table] is None, (table, landed[table])
