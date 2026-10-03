"""真连演示库跑一次同步：工单 007 的六条验收逐条对。

标记 live：要真 MySQL 在线，凭据从 gitignored 的 `.setup/aiweb_ro.cnf` 读（同 006），
缺文件就 skip——换台机器不该把整个闸口卡红。

为什么这些断言非 live 不可：`sub_part` / `cardinality` 是不是**真有值**、宽表的中文注释
是不是**真到达**、推断边是不是**真的一条不多**，全都不是语句形状能证明的。
语句形状由 `tests/unit/test_sync_upsert_sql.py` 钉，这里钉的是它跑在真元数据 + 真源库上
的结果。

期望值口径：
- 工单 007 验收 1–6（本文件与之一一对应，函数名里就写着是哪一条）
- `docs/verification.md` §1：9 张 BASE TABLE + 1 张 VIEW，`product_stats_wide` 68 列全带中文注释
- `docs/metadata-model.md` §3/§4/§5：三段式写入语义、`extracted` vs `inferred`、软删除
- 最后一张用例（工单 023 验收 5）：`docs/metadata-model.md` §9 的归一清单 +
  `backend/scripts/init_demo_mysql.sql` 里逐字写着的列类型
- 最后一条（工单 033 验收 2）：`docs/kb-workflow.md` §8 的类型清单与阈值语义 +
  `init_demo_mysql.sql` 里那几处 `ELT(1 + x % n, ...)` 的**字面清单**（品牌 12、标签 5×4、
  退款原因 5×3、事件类型 6、城市 13 项里 `% 12` 够得着的 12）；窗口组数在写用例前用只读探针
  在演示库上数过一遍，数的是"该有几个值"这件事，不是拿落库结果反推期望
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.extractor.mysql import MySQLExtractor
from app.services.kb_service import ActiveProfile
from app.settings import get_settings
from tests.integration.conftest import Account, DbAccount
from tests.integration.test_sync_pg import _sync
from tests.unit.test_type_domain import assert_in_value_domain

pytestmark = [pytest.mark.pg, pytest.mark.live]

Login = Callable[..., Awaitable[Account]]

DEMO_DB = "ai_web_demo"

# verification.md §1 的对象与列数口径
TABLES = 9
VIEWS = 1
WIDE_COLUMNS = 68

_CJK = re.compile(r"[一-鿿]")


@dataclass
class SyncRun:
    ds_id: int
    #: 发起这一轮同步的账号（工单 016 之后 `_trigger` 直接复用 `test_sync_pg._sync`，
    #: 它要的是 headers 而不是一个裸字典）
    acct: Account
    body: dict[str, Any]
    #: `repr=False`：这条口令是真账号的，用例一失败 pytest 就把帧局部变量倒进终端，
    #: 默认 dataclass repr 会连着它一起倒出来。断言用 `run.ro_password` 照旧拿得到。
    ro_password: str = field(repr=False)
    #: 到这里为止真的发到源库的语句原文（验收 6 的靶子）
    sent: list[str]


async def _sync_once(
    client: AsyncClient,
    login: Login,
    monkeypatch: pytest.MonkeyPatch,
    account: DbAccount,
) -> SyncRun:
    """登记一条指向演示库的源 → 发起同步并等 worker 跑完，终局那一行带回来。

    走端点而不是直接调 `run_sync`：这是示踪弹，要的是"一次 HTTP 调用之后库里就有元数据"，
    顺手把 owner 档鉴权和 `get_db` 覆盖那条链也验一遍。工单 016 之后这一次调用是两跳
    （POST 拿 202 + await worker 的循环体），见 `_trigger`。
    """
    user, password, host, port = account
    acct = await login(username="sync-owner", role="member")

    sent: list[str] = []
    real_rows = MySQLExtractor._rows

    def record(self: MySQLExtractor, sql: str, params: dict[str, object] | None = None) -> Any:
        sent.append(sql)
        return real_rows(self, sql, params)

    # `_rows` 是方言层唯一的 I/O 出口（probe/discover/collect 都从它走），
    # 所以"发出去的是什么"这一事实只有在这里拦才拦得住全部。
    monkeypatch.setattr(MySQLExtractor, "_rows", record)

    register = await client.post(
        "/api/datasources",
        json={
            "name": "demo-sync",
            "kind": "mysql",
            "host": host,
            "port": port,
            "connect_user": user,
            "connect_password": password,
            "include_schemas": [DEMO_DB],
            # verification §1.2：下划线前缀的是建库脚本的内部对象，不是业务表
            "exclude_tables": ["\\_%"],
        },
        headers=acct.headers,
    )
    assert register.status_code == 201, register.text
    run = SyncRun(
        ds_id=int(register.json()["id"]),
        acct=acct,
        body={},
        ro_password=password,
        sent=sent,
    )
    await _trigger(client, run)
    return run


async def _trigger(client: AsyncClient, run: SyncRun) -> dict[str, Any]:
    """对同一条源再发起一次同步，并等 worker 把它跑完——幂等断言要的就是"同一个目标跑第二遍"。

    工单 016 之后这里是**两跳**：POST 只换回 202 与 job_id，活儿在 worker 那条连接上跑。
    所以这一句直接复用 `test_sync_pg._sync`（它已经把两跳和终局投影都封好了），
    `run.body` 于是是 `sync_jobs` 那一行的终局——字段名与 P2 的响应体一致，
    验收 1/4 那些 `run.body["counters"][...]` 的断言本体因此一行不改。
    """
    submit = await _sync(client, run.acct, run.ds_id)
    assert submit.status_code == 202, submit.text
    run.body = submit.json()
    assert run.ro_password not in submit.text, "同步任务的记录里出现了源库口令"
    return run.body


async def _rows(
    session_factory: async_sessionmaker[AsyncSession], sql: str, **params: Any
) -> list[dict[str, Any]]:
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        result = await session.execute(text(sql.replace("{s}", schema)), params)
        return [dict(row) for row in result.mappings()]


async def _one(session_factory: async_sessionmaker[AsyncSession], sql: str, **params: Any) -> Any:
    rows = await _rows(session_factory, sql, **params)
    assert len(rows) == 1, rows
    return next(iter(rows[0].values()))


async def _value_domain(
    session_factory: async_sessionmaker[AsyncSession], ds_id: int
) -> dict[str, tuple[str, list[str] | None, list[str] | None]]:
    """`{表.列: (data_type, enum_values, sample_values)}`——§8 那两格在本轮落库后的样子。

    键用 `表.列` 而不是 `库.表.列`：这一份要跨 MySQL/PG 两个 live 文件共用（PG 那一份从这里
    import，与 `_counts` / `_relation_edges` 同一先例），而两边都只登记一个库一个 schema，
    `datasource_id` 已经把范围钉住了。
    带着 `data_type` 回来只有一个用途：那两条"凡是被采过的列，类型必须在 §8 清单里"的
    全列级断言——只点名几列的话，采样把清单外的列也写了会看不见。
    """
    return {
        f"{row['tbl']}.{row['col']}": (row["norm"], row["ev"], row["sv"])
        for row in await _rows(
            session_factory,
            "select t.table_name as tbl, c.column_name as col, c.data_type as norm,"
            "       c.enum_values as ev, c.sample_values as sv"
            ' from "{s}".meta_column c join "{s}".meta_table t on t.id = c.table_id'
            " where t.datasource_id = :ds",
            ds=ds_id,
        )
    }


async def _relation_edges(
    session_factory: async_sessionmaker[AsyncSession], ds_id: int
) -> list[dict[str, Any]]:
    return await _rows(
        session_factory,
        "select r.source_kind, r.fk_name, r.confidence, r.on_delete,"
        "       f.table_name as from_table, r.from_column_name as from_column,"
        "       t.table_name as to_table, r.to_column_name as to_column"
        ' from "{s}".meta_relation r'
        ' join "{s}".meta_table f on f.id = r.from_table_id'
        ' join "{s}".meta_table t on t.id = r.to_table_id'
        " where r.datasource_id = :ds order by from_table, from_column",
        ds=ds_id,
    )


async def _counts(session_factory: async_sessionmaker[AsyncSession], ds_id: int) -> dict[str, int]:
    """五张 meta_* 的行数快照——幂等断言比对的就是这个字典。"""
    return {
        "meta_table": await _one(
            session_factory,
            'select count(*) from "{s}".meta_table where datasource_id = :ds',
            ds=ds_id,
        ),
        "meta_column": await _one(
            session_factory,
            'select count(*) from "{s}".meta_column c join "{s}".meta_table t on t.id = c.table_id'
            " where t.datasource_id = :ds",
            ds=ds_id,
        ),
        "meta_index": await _one(
            session_factory,
            'select count(*) from "{s}".meta_index i join "{s}".meta_table t on t.id = i.table_id'
            " where t.datasource_id = :ds",
            ds=ds_id,
        ),
        "meta_index_column": await _one(
            session_factory,
            'select count(*) from "{s}".meta_index_column ic'
            ' join "{s}".meta_index i on i.id = ic.index_id'
            ' join "{s}".meta_table t on t.id = i.table_id'
            " where t.datasource_id = :ds",
            ds=ds_id,
        ),
        "meta_relation": await _one(
            session_factory,
            'select count(*) from "{s}".meta_relation where datasource_id = :ds',
            ds=ds_id,
        ),
    }


async def test_验收1_十个对象落进_meta_table_且宽表_68_列带中文注释(
    client: AsyncClient,
    login: Login,
    monkeypatch: pytest.MonkeyPatch,
    account: DbAccount,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run = await _sync_once(client, login, monkeypatch, account)

    assert run.body["status"] == "success", json.dumps(run.body, ensure_ascii=False)
    assert run.body["errors"] == []
    assert run.body["counters"]["databases"] == 1
    assert run.body["counters"]["tables"] == TABLES + VIEWS
    assert run.body["counters"]["tables_failed"] == 0

    kinds = {
        row["table_type"]: row["n"]
        for row in await _rows(
            session_factory,
            'select table_type, count(*) as n from "{s}".meta_table'
            " where datasource_id = :ds group by table_type",
            ds=run.ds_id,
        )
    }
    assert kinds == {"BASE TABLE": TABLES, "VIEW": VIEWS}

    names = {
        row["table_name"]
        for row in await _rows(
            session_factory,
            'select table_name from "{s}".meta_table where datasource_id = :ds',
            ds=run.ds_id,
        )
    }
    # 建库脚本的内部对象被源的 exclude_tables 挡掉了：算进元数据就是污染表卡片和 prompt
    assert not [n for n in names if n.startswith("_")], names
    assert {"product_stats_wide", "v_daily_sales", "category"} <= names

    wide = await _rows(
        session_factory,
        "select c.column_name, c.comment_raw, c.ordinal_position"
        ' from "{s}".meta_column c join "{s}".meta_table t on t.id = c.table_id'
        " where t.table_name = 'product_stats_wide' and t.datasource_id = :ds",
        ds=run.ds_id,
    )
    assert len(wide) == WIDE_COLUMNS, f"宽表抽出 {len(wide)} 列，文档要求 {WIDE_COLUMNS}"
    assert sorted(row["ordinal_position"] for row in wide) == list(range(1, WIDE_COLUMNS + 1))
    missing = [row["column_name"] for row in wide if not _CJK.search(str(row["comment_raw"]))]
    assert not missing, f"这些列没有中文注释（IS 里的注释没到达或被覆盖）：{missing[:5]}"

    # §2.2：server_version 这一列存在的唯一理由就是"探测/同步时写入"，只在响应里回不算写入
    version = await _one(
        session_factory,
        'select server_version from "{s}".data_sources where id = :ds',
        ds=run.ds_id,
    )
    assert str(version).startswith("5.7"), version


async def test_验收2_cardinality_与_sub_part_真的有非空值(
    client: AsyncClient,
    login: Login,
    monkeypatch: pytest.MonkeyPatch,
    account: DbAccount,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """§10 末锚点：这两列非空 = 没退回逐表 `SHOW CREATE TABLE`。

    `sub_part` 只有**前缀索引**才会给非空值，所以它同时是夹具考点（verification §1 的
    `idx_product_name(name(32))`）。期望值写死 32：断言成"非空就行"的话，长度漂移
    （name(32) 改成 name(20)）就没人发现了。
    """
    run = await _sync_once(client, login, monkeypatch, account)

    idx = await _rows(
        session_factory,
        "select count(*) as total, count(i.cardinality) as with_card"
        ' from "{s}".meta_index i join "{s}".meta_table t on t.id = i.table_id'
        " where t.datasource_id = :ds",
        ds=run.ds_id,
    )
    total_idx, card = idx[0]["total"], idx[0]["with_card"]
    assert total_idx >= 15, total_idx
    assert card == total_idx, f"cardinality 非空 {card}/{total_idx}——Inspector 方案就是这么死的"

    prefix = await _rows(
        session_factory,
        "select ic.column_name, ic.sub_part, i.index_name"
        ' from "{s}".meta_index_column ic join "{s}".meta_index i on i.id = ic.index_id'
        ' join "{s}".meta_table t on t.id = i.table_id'
        " where t.datasource_id = :ds and ic.sub_part is not null",
        ds=run.ds_id,
    )
    assert [(r["index_name"], r["column_name"], r["sub_part"]) for r in prefix] == [
        ("idx_product_name", "name", 32)
    ]

    # PRIMARY 也在 STATISTICS 里，抽取层不该丢掉它：主键是 prompt 里最值钱的一条信息
    pk = await _rows(
        session_factory,
        "select t.table_name, ic.column_name, ic.seq_in_index"
        ' from "{s}".meta_index i'
        ' join "{s}".meta_index_column ic on ic.index_id = i.id'
        ' join "{s}".meta_table t on t.id = i.table_id'
        " where t.datasource_id = :ds and i.is_primary order by t.table_name",
        ds=run.ds_id,
    )
    assert len(pk) == TABLES, f"9 张表都该有 PRIMARY，实到 {len(pk)}"
    assert all(r["column_name"] == "id" and r["seq_in_index"] == 1 for r in pk), pk


async def test_验收3_真外键落成_extracted_且自引用不报错(
    active_profile: ActiveProfile,
    client: AsyncClient,
    login: Login,
    monkeypatch: pytest.MonkeyPatch,
    account: DbAccount,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run = await _sync_once(client, login, monkeypatch, account)
    edges = await _relation_edges(session_factory, run.ds_id)
    by_key = {(r["from_table"], r["from_column"], r["to_table"], r["to_column"]): r for r in edges}

    for key in (
        ("order_item", "order_id", "order_main", "id"),
        ("payment_record", "order_id", "order_main", "id"),
    ):
        assert key in by_key, f"真外键没落库：{key}；实到 {sorted(by_key)}"
        row = by_key[key]
        assert row["source_kind"] == "extracted", row
        # confidence 默认 1.0 出自 §2.4：真约束不需要打分
        assert float(row["confidence"]) == 1.0, row
        assert row["fk_name"], row
        assert row["on_delete"], row

    # 自引用：category.parent_id → category.id。它一旦报错或被静默丢掉，JOIN 图的自环分支
    # 就永远没被测过（工单 007 目标里点名的场景）。
    self_ref = by_key.get(("category", "parent_id", "category", "id"))
    assert self_ref is not None, "自引用外键没了"
    assert self_ref["source_kind"] == "extracted"
    assert run.body["errors"] == []
    assert run.body["warnings"] == []
    assert run.body["counters"]["relations_extracted"] == sum(
        1 for r in edges if r["source_kind"] == "extracted"
    )


async def test_验收4_埋点表按命名约定推出_inferred_且与_extracted_可区分(
    client: AsyncClient,
    login: Login,
    monkeypatch: pytest.MonkeyPatch,
    account: DbAccount,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """库里没建约束的 `product_id` 要推出来，库里没有的 `user` 表不许编。

    这两半边都是"宁缺毋滥"（§4）：推断边进了 prompt 就是 AI 的 JOIN 依据，
    错一条比少一条贵得多。
    """
    run = await _sync_once(client, login, monkeypatch, account)
    edges = await _relation_edges(session_factory, run.ds_id)
    inferred = [r for r in edges if r["source_kind"] == "inferred"]
    extracted = [r for r in edges if r["source_kind"] == "extracted"]

    assert extracted and inferred, "两档必须同时存在才谈得上可区分"
    assert {(r["from_table"], r["from_column"], r["to_table"]) for r in inferred} == {
        ("user_activity_log", "product_id", "product")
    }, f"推断边集合漂移：{inferred}"
    # §5.3 加权公式（010 拍板，取代 007 的常数 0.7）：`user_activity_log.product_id → product.id`
    # 两侧同为 INT，四项加成全中 → 0.35+0.25+0.15+0.15+0.10 = **1.00**。
    # 这一条是真库上的实测值，不是手算：能顺带证明抽取层给的 `data_type` 两边确实都是 `int`。
    assert float(inferred[0]["confidence"]) == 1.0, (
        "confidence 出自 architecture §5.3 公式，改它要连文档一起改"
    )
    assert all(r["fk_name"] is None for r in inferred), "推断边不该带着约束名"
    # user_id：演示库里既没有 user 也没有 users，推出来就是一条指向不存在对象的边
    assert "user_id" not in {r["from_column"] for r in inferred}
    # 已有真约束的列不再重复推断一遍（同一条边挂两种 source_kind 会让 JOIN 图算重）
    enforced = {(r["from_table"], r["from_column"]) for r in extracted}
    assert not enforced & {(r["from_table"], r["from_column"]) for r in inferred}, (
        "真外键覆盖的列又推断了一遍"
    )
    assert run.body["counters"]["relations_inferred"] == len(inferred)


async def test_验收5_同步两次行数不变且人工列不被覆盖(
    client: AsyncClient,
    login: Login,
    monkeypatch: pytest.MonkeyPatch,
    account: DbAccount,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """幂等是这一层的**全部价值**：点两次"同步"不该抹掉任何人录过的东西。"""
    run = await _sync_once(client, login, monkeypatch, account)
    before = await _counts(session_factory, run.ds_id)

    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        await session.execute(
            text(
                f'update "{schema}".meta_table set comment_zh = :zh, business_desc = :bd'
                " where datasource_id = :ds and table_name = 'order_main'"
            ),
            {"zh": "订单主表（人工核对过）", "bd": "一行 = 一笔订单", "ds": run.ds_id},
        )
        await session.execute(
            text(
                f'update "{schema}".meta_column set comment_zh = :zh where column_name = :col'
                f' and table_id = (select id from "{schema}".meta_table'
                " where datasource_id = :ds and table_name = 'order_main')"
            ),
            {"zh": "订单号（人工）", "col": "order_no", "ds": run.ds_id},
        )
        await session.commit()

    await _trigger(client, run)
    assert run.body["status"] == "success", json.dumps(run.body, ensure_ascii=False)

    after = await _counts(session_factory, run.ds_id)
    assert after == before, f"两次同步行数变了：{before} → {after}"

    human = await _rows(
        session_factory,
        "select t.comment_zh, t.business_desc, t.comment_raw, c.comment_zh as col_zh"
        ' from "{s}".meta_table t'
        ' join "{s}".meta_column c on c.table_id = t.id and c.column_name = :col'
        " where t.datasource_id = :ds and t.table_name = :tbl",
        col="order_no",
        ds=run.ds_id,
        tbl="order_main",
    )
    assert len(human) == 1
    assert human[0]["comment_zh"] == "订单主表（人工核对过）", "表级人工列被同步覆盖了"
    assert human[0]["business_desc"] == "一行 = 一笔订单"
    assert human[0]["col_zh"] == "订单号（人工）", "列级人工列被同步覆盖了"
    # 反向也要成立：COALESCE 的方向写成"新值优先"，人工列第二轮就会被同步抹掉；
    # 而 comment_raw 是同步列，它必须跟着源库走，不能被同一条 COALESCE 顺手保护掉。
    assert "人工" not in str(human[0]["comment_raw"])
    assert str(human[0]["comment_raw"]), "同步列没写进去"

    # 陈旧标记：这一轮表都在，不该有任何一张被标 stale（§5 只标"这一轮没出现"的）
    stale = await _one(
        session_factory,
        'select count(*) from "{s}".meta_table where datasource_id = :ds and is_stale',
        ds=run.ds_id,
    )
    assert stale == 0


async def test_验收6_抽取路径只发_select(
    client: AsyncClient,
    login: Login,
    monkeypatch: pytest.MonkeyPatch,
    account: DbAccount,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """源库账号是只读的，但这不是放松代码的理由：写语句发过去就是 1142 权限错误。

    拦在 `_rows`（方言层唯一 I/O 出口）上，验的是**实际发出去**的语句，
    不是 SQL 常量的文本——后者单测已经钉过一遍了。
    """
    run = await _sync_once(client, login, monkeypatch, account)
    assert run.sent, "一条都没发，说明这个断言在空跑"
    non_select = [s.strip()[:60] for s in run.sent if not s.strip().upper().startswith("SELECT")]
    assert not non_select, f"抽取路径发出了非只读语句：{non_select[:2]}"
    hits = [
        (word, sql[:60])
        for sql in run.sent
        for word in (
            "insert ",
            "update ",
            "delete ",
            "alter ",
            "drop ",
            "create ",
            "truncate ",
            "call ",
            "grant ",
        )
        if re.search(rf"\b{word}", sql, re.I)
    ]
    assert not hits, hits
    # 只读账号真跑通了：上面那 10 个对象的元数据就是这条断言的证据
    assert (await _counts(session_factory, run.ds_id))["meta_table"] == TABLES + VIEWS


async def test_工单023_data_type_落库后全是归一值_方言原文仍留在_raw(
    client: AsyncClient,
    login: Login,
    monkeypatch: pytest.MonkeyPatch,
    account: DbAccount,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """工单 023 验收 5：真跑一次演示库同步之后，`meta_column.data_type` 的取值全落在归一值域内。

    期望值口径：列名与方言原料来自 `backend/scripts/init_demo_mysql.sql`（建库脚本，独立真相源），
    归一结论来自 `docs/metadata-model.md` §9 的清单与 as-built(023) 的两条拍板
    （`tinyint(1)→bool`、任意精度小数两方言都归 `numeric`）。视图 `v_daily_sales` 的聚合列
    精度是引擎自己算出来的（`SUM(DECIMAL(12,2))` 的标度不在建库脚本里），所以那一类只断
    "落在值域内 + 不含空格"，不断具体数字。
    """
    run = await _sync_once(client, login, monkeypatch, account)
    assert run.body["status"] == "success", json.dumps(run.body, ensure_ascii=False)

    cols = await _rows(
        session_factory,
        "select t.table_name as t, c.column_name as c, c.data_type, c.raw_data_type"
        ' from "{s}".meta_column c join "{s}".meta_table t on t.id = c.table_id'
        " where t.datasource_id = :ds",
        ds=run.ds_id,
    )
    assert cols, "一列都没读到，这条断言在空跑"
    for row in cols:
        # 归一值域 = 规范基名 (+ 可选括号 + 可选 [])：出现 `int(11) unsigned` 这类带修饰的
        # 原文，就是抽取层没接上（§9 的口径是 data_type 存归一值、raw_data_type 存原文）。
        assert_in_value_domain(str(row["data_type"]))
    spaced = [
        f"{row['t']}.{row['c']}={row['data_type']!r}"
        for row in cols
        if " " in str(row["data_type"])
    ]
    assert not spaced, f"data_type 里出现了带修饰的方言原文：{spaced[:3]}"

    by_column = {f"{row['t']}.{row['c']}": row for row in cols}
    expected: dict[str, tuple[str, str]] = {
        # 两条拍板的现场证据：tinyint(1) 塌成 bool、decimal 一族写成 numeric
        "order_item.is_gift": ("bool", "tinyint(1)"),
        "order_main.amount": ("numeric(12,2)", "decimal(12,2)"),
        "order_item.unit_price": ("numeric(10,2)", "decimal(10,2)"),
        # 显示宽度不是语义：5.7 写出的 tinyint(4) 归一后是裸基名
        "category.level": ("tinyint", "tinyint(4)"),
        # 本来就规范的字面：归一不该动它们
        "customer.phone": ("char(11)", "char(11)"),
        "payment_record.trade_no": ("varchar(40)", "varchar(40)"),
        "user_activity_log.extra": ("json", "json"),
        # enum 塌成基名，取值清单住在 enum_values，原文照旧完整留在 raw
        "order_main.status": (
            "enum",
            "enum('pending','paid','shipped','completed','cancelled','refunding')",
        ),
    }
    got = {
        name: (str(by_column[name]["data_type"]), str(by_column[name]["raw_data_type"]))
        for name in expected
    }
    assert got == expected, json.dumps(got, ensure_ascii=False)


# ---------------------------------------- 工单 033（kb-workflow §8）live 半边：期望值的原料清单
#
# 下面几份清单逐条读自 `backend/scripts/init_demo_mysql.sql` 的 `INSERT ... SELECT` 表达式本身
# （行号注在每一个名字上），**不是**从 `meta_column` dump 回来的：这一条要断的是"值域该是那些值"，
# 拿落库结果当期望就是同义反复。`brand`/`tags`/`reason` 三份都写成 `ELT(1 + x % n, ...)`，
# 而 `n` 就是那份清单的长度——所以"组合数 = 各清单长度之积"这一条事实也住在脚本里。
BRANDS = frozenset(
    {
        "云帆",
        "木白",
        "青麦",
        "拾光",
        "物研社",
        "南屿",
        "常喜",
        "禾风",
        "初语",
        "山海",
        "良物",
        "潮汐",
    }
)  # :127-128，`ELT(1 + p.x2 % 12, ...)`
TAG_HEADS = ("热销", "新品", "促销", "自营", "包邮")  # :131，`ELT(1 + p.x3 % 5, ...)`
TAG_TAILS = ("正品保障", "七天无理由", "破损包退", "会员专享")  # :132，`ELT(1 + p.x4 % 4, ...)`
REFUND_REASONS = ("拍错/多拍", "商品与描述不符", "质量问题", "物流太慢", "七天无理由")  # :330
REFUND_STATES = ("已上传凭证", "客服已电话沟通", "等待买家退货")  # :331
EVENT_TYPES = ("view", "search", "cart_add", "fav", "order_submit", "pay_success")  # :503
# :167-168 那份城市清单写着 13 个名字，但取模是 `% 12` → `ELT` 最多走到第 12 项，
# '重庆' 在这张表里一次都没出现过。全表 distinct 因此是 12，而**这一列的值域永远缺那一个名字**——
# 它是 §8 那句"只取值不存行数据"之外另一条容易被读错的口径：脚本作者以为是 13。
CITIES = frozenset(
    {
        "上海",
        "北京",
        "广州",
        "深圳",
        "杭州",
        "成都",
        "武汉",
        "西安",
        "南京",
        "苏州",
        "长沙",
        "重庆",
    }
)

# 本轮**被采样写进值域**的 7 列（键是 `表.列`）。逐列的来历：§8 第一步的类型清单放行、
# 第二步的阈值 30 放行，两边都对着实跑前用只读探针在演示库上数过的那一份窗口组数：
#   category.level 2 / customer.city 4 / order_item.is_gift 2 / product.brand 12 /
#   product.tags 20 / refund_record.reason 15 / user_activity_log.event_type 6
SAMPLED_COLUMNS = frozenset(
    {
        "category.level",
        "customer.city",
        "order_item.is_gift",
        "product.brand",
        "product.tags",
        "refund_record.reason",
        "user_activity_log.event_type",
    }
)
# 本轮**有 `enum_values` 却没有 `sample_values`** 的 8 列：它们是 MySQL 真 ENUM，值域由
# `information_schema` 的定义给（§8.2 那条老通路），而 §8 第一步的 `has_declared_enum` 那一格
# 把它们挡在采样之外——定义比观测准（工单 033 的已定口径）。
DECLARED_ENUM_COLUMNS = frozenset(
    {
        "customer.gender",
        "customer.level",
        "order_main.pay_type",
        "order_main.status",
        "payment_record.channel",
        "payment_record.status",
        "product.status",
        "refund_record.status",
    }
)
# §8 第一步放行去问的列数：演示库业务表里 `varchar`/`char`/`tinyint` 共 17 列，减去被
# `exclude_tables: ["\\\\_%"]` 整表排除的 `_aiweb_demo_marker.version`；8 列真 ENUM 根本不进门。
# 16 条问、7 列拿到值域——"发了多少条"与"写了多少列"分别是两道闸的实物，合成一条就说不清谁在筛。
SENT_SAMPLE_QUERIES = 16


async def test_工单033_开采样跑真MySQL_短文本列首次出现值域_关掉那一轮又抹回NULL(
    client: AsyncClient,
    login: Login,
    monkeypatch: pytest.MonkeyPatch,
    account: DbAccount,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """工单 033 验收 2 的 MySQL 半边：开关打开跑一次真同步，那两格在非真 ENUM 列上**第一次**有值。

    必须 live 的理由有三条，闸内一条都给不出：
    - 采样语句是 `build_sql_sample` 现拼的文本带两个绑定参数，它在真 MySQL 8 上跑不跑得起来
      （派生表那一层 `LIMIT` 会不会被优化器合并掉、`GROUP BY` 认不认 derived 列）只有真库能答；
      跑不起来的话终局是 `partial` + `sample_failed`，而这一条断的是 `success`。
    - 值域的**内容**来自真数据的哈希分布（12 个品牌、5×4 个标签组合、15 条退款原因），
      不是桩里摆好的那两组字符串。
    - 关掉开关的第二轮把两格一起抹回 NULL：`sample_values` 是随本片才进 `_COLUMN_SYNC_COLS` 的，
      "一批两格同生同灭"这件事只在真 upsert 上看得见（033 交付记录里那条口径的实物）。

    期望值出处：上面那份原料注释块（全部来自 `init_demo_mysql.sql` 的表达式与行号）、
    `docs/kb-workflow.md` §8 的类型清单与阈值语义、`docs/metadata-model.md` §9 的归一值域
    （`tinyint(4)→tinyint`、`tinyint(1)→bool`，两条都注在 as-built(023) 那里，且本文件
    工单 023 那一条已经逐字钉过一遍——这里复用同一对字面，不再另立一套）。

    这一条**故意不要求** `active_profile`：033 把采样从 `if decision.profile_id is not None:`
    里面提到了档案分支**之前**（"采样不属于档案"），所以"没有 active 档案、一张卡都没建的那一轮，
    值域照样在场"正是那句注释的实物——`counters["cards"] == 0` 与 7 列值域同轮成立才算钉住。

    一句自我限定：**`is_generated` 那一支在两处演示库里都没有实物**（MySQL 0 个生成列、PG 0 个），
    所以这一条证不到"生成列不采"；那颗分辨力只住在 `tests/unit/test_extract_sample_distinct.py`。
    """
    # 三个键逐条钉死：期望的列名单与值集合是按"阈值 30 + 窗口 1000"数出来的，本机 `.env` 改了
    # 那两个数时这一条要跟着重数，而不是悄悄换一份期望值。
    monkeypatch.setenv("AIWEB_EXTRACT__SAMPLE_DISTINCT", "true")
    monkeypatch.setenv("AIWEB_EXTRACT__SAMPLE_DISTINCT_MAX_DISTINCT", "30")
    monkeypatch.setenv("AIWEB_EXTRACT__SAMPLE_ROW_LIMIT", "1000")
    get_settings.cache_clear()

    run = await _sync_once(client, login, monkeypatch, account)
    body = run.body
    assert body["status"] == "success", json.dumps(body, ensure_ascii=False)
    assert body["errors"] == [], "采样那一步在真 MySQL 上失败了：见 §8 第二步的语句形状"

    sample_sqls = [sql for sql in run.sent if "AS sample_value" in sql]
    assert len(sample_sqls) == SENT_SAMPLE_QUERIES, len(sample_sqls)
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in sample_sqls), sample_sqls[:1]

    domain = await _value_domain(session_factory, run.ds_id)

    def values_of(key: str) -> tuple[str, list[str] | None, list[str] | None]:
        assert key in domain, f"{key} 没落进本轮的 meta_column：{sorted(domain)[:5]}…"
        return domain[key]

    # ---- 正量 ①：中文短枚举列第一次有了值域（P2/P3 期间这两格在非真 ENUM 列上恒空）
    norm, brands, brands_sample = values_of("product.brand")
    assert norm == "varchar(64)"
    assert brands is not None and set(brands) == BRANDS
    assert len(brands) == len(BRANDS) == 12
    assert brands == brands_sample, "§8 第三步：两格同批写入、同一份内容"

    # ---- 正量 ②：组合值（`CONCAT_WS` 出来的 5×4）整串进值域，不拆开
    _norm, tags, tags_sample = values_of("product.tags")
    assert tags is not None and set(tags) == {
        f"{head},{tail}" for head in TAG_HEADS for tail in TAG_TAILS
    }
    assert len(tags) == len(TAG_HEADS) * len(TAG_TAILS) == 20
    assert tags == tags_sample

    # ---- 正量 ③：自由文本形状的原因列（5×3 = 15 种组合，全角括号是脚本原文）
    _norm, reasons, _sample = values_of("refund_record.reason")
    assert reasons is not None and set(reasons) == {
        f"{reason}（{state}）" for reason in REFUND_REASONS for state in REFUND_STATES
    }
    assert len(reasons) == len(REFUND_REASONS) * len(REFUND_STATES) == 15

    # ---- 正量 ④：次序在真数据上的样子。六个值的次数互不相等（窗口=前 1000 行：pay_success 188、
    # search 179、fav 165、cart_add 162、order_submit 157、view 149，探针在演示库上数过），
    # 所以"频次降序"这一支被唯一决定；同频次按字面升序那一支要等次数相等才出场，住在纯函数层。
    _norm, events, _sample = values_of("user_activity_log.event_type")
    assert events == [
        "pay_success",
        "search",
        "fav",
        "cart_add",
        "order_submit",
        "view",
    ], events
    assert set(events or []) == set(EVENT_TYPES)

    # ---- 正量 ⑤：窗口 ≠ 全表。`customer` 全表 distinct 城市是 12（那份 13 项清单里 '重庆' 被
    # `% 12` 挡在门外），而这一列拿到的值域只有 4 个：`(SELECT city ... LIMIT 1000)` 走的是覆盖
    # 索引 `idx_customer_city`（:157），前 1000 行按索引序回来只摊到 4 个城市。
    # 这一格是 §8 那条"扫描上限"的**代价**的实物：省掉排序，换来"少见的取值可能没进窗口"。
    # 真要变红的是执行计划换了，那时候重数的是这个 4 与下面这份名单，不是断言的写法。
    _norm, cities, _sample = values_of("customer.city")
    assert cities is not None and set(cities) <= CITIES
    assert set(cities) == {"上海", "北京", "南京", "广州"}
    assert len(cities) == 4 < 12

    # ---- 正量 ⑥：窄整型与布尔写法。MySQL 的布尔是 `tinyint(1)`，驱动回来是 int，
    # 所以值域字面是 "0"/"1"；PG 的 `boolean` 回来是 Python bool，同一归一值 `bool` 下字面是
    # "false"/"true"（`test_extract_pg_live.py` 的对应那一条）。两方言在 `data_type` 上一致、
    # 在值域字面上不一致——这一点只有各跑一次真库才看得见，而卡片把这两行都端给模型。
    assert values_of("category.level")[1] == ["2", "1"]
    assert values_of("order_item.is_gift")[:1] == ("bool",)
    assert values_of("order_item.is_gift")[1] == ["0", "1"]

    # ---- 反证 ①：真 ENUM 列一律不采，值域仍是**定义**那一份，且 `sample_values` 恒空。
    # `sample_values is None` 才是这一支的判据：`on_sale/off_sale/draft` 三个值的观测顺序
    # 与定义顺序恰好也排得一样（窗口里 on_sale 约占 2/3），所以次序分不出"定义"与"观测"，
    # 而"定义那一路从来只写 `enum_values`"这条分得开。
    assert values_of("product.status") == ("enum", ["on_sale", "off_sale", "draft"], None)

    # ---- 全列级：谁被写了、谁没被写，一列都不许多（`_value_domain` 带 `data_type` 就是为这两条）
    assert {key for key, (_n, _e, sv) in domain.items() if sv is not None} == SAMPLED_COLUMNS
    assert {key for key, (_n, ev, _s) in domain.items() if ev is not None} == (
        SAMPLED_COLUMNS | DECLARED_ENUM_COLUMNS
    )
    # 被采到的列的类型基名全落在 §8 第一行那份清单里；冒出别的基名就是候选筛子坏了。
    assert {n.partition("(")[0] for n, _e, sv in domain.values() if sv is not None} == {
        "varchar",
        "tinyint",
        "bool",
    }

    # ---- 反证 ②：高基数列什么都不写（§8 第二步"超过阈值即放弃"，放弃的形状是 NULL 不是空列表）
    for key in ("customer.nickname", "product.sku", "user_activity_log.session_id"):
        assert values_of(key)[1] is None and values_of(key)[2] is None, key
    # `category.name` 是最要紧的那一列：表只有 40 行，窗口 1000 行把它扫全了，40 > 30 → 放弃。
    # 外层 `LIMIT` 若写成阈值本身（`LIMIT 30`），这一列只会回 30 组，`adjudicate` 看不出"超了"，
    # 于是 40 个取值的自由文本列会被当成枚举列写进卡片。
    # `ndv_query_limit` 那 10 行余量买的就是这一格。
    assert values_of("category.name")[1] is None

    # ---- 采样不属于档案：这一轮没有 active 档案（用例没要那副夹具），卡片一张都没建
    assert body["counters"]["cards"] == 0

    # =============================== 第二轮：把开关关掉，看这两格的命运（同一个源、同一个账号）
    monkeypatch.setenv("AIWEB_EXTRACT__SAMPLE_DISTINCT", "false")
    get_settings.cache_clear()
    second = await _trigger(client, run)
    assert second["status"] == "success", json.dumps(second, ensure_ascii=False)
    assert second["errors"] == []

    after = await _value_domain(session_factory, run.ds_id)
    assert [sql for sql in run.sent if "AS sample_value" in sql] == sample_sqls, (
        "开关关掉之后源库又收到了采样语句：这一轮该一步都不发（工单 033 验收 1 的 live 那一半）"
    )
    # 两格一起回 NULL：`sample_values` 随本片进 `_COLUMN_SYNC_COLS`，与 `enum_values` 同一条命运。
    # 少了这一格的话，一轮不采样的同步会把 `enum_values` 抹成 NULL 而让隔壁那格留着上一轮的值域——
    # 卡片少一行「取值:」而界面下拉里还摆着那两个值，两处读同一列的说法当场分家。
    assert {key for key, (_n, _e, sv) in after.items() if sv is not None} == set()
    for key in SAMPLED_COLUMNS:
        assert after[key][1] is None and after[key][2] is None, key
    # 真 ENUM 那 8 列不受影响：值域每轮由抽取层从定义重写，`sample_values` 则始终是空的
    # （定义那一路不写它，采样那一路不许碰它们）。逐个点名而不是比一个字典推导式：这一格
    # 想说的话是"8 列全在、全带着定义"，写成推导式就把"少了一列"这种红洗成了"等于空"。
    for key in sorted(DECLARED_ENUM_COLUMNS):
        assert after[key][0] == "enum", key
        assert after[key][1], f"{key} 的定义值域第二轮不见了"
        assert after[key][2] is None, key
