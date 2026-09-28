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
from tests.integration.conftest import Account, DbAccount

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
    headers: dict[str, str]
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
    """登记一条指向演示库的源 → 打 `POST /api/sync/jobs`，两边的响应都带回来。

    走端点而不是直接调 `run_sync`：这是示踪弹，要的是"一次 HTTP 调用之后库里就有元数据"，
    顺手把 owner 档鉴权和 `get_db` 覆盖那条链也验一遍。
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
        headers=acct.headers,
        body={},
        ro_password=password,
        sent=sent,
    )
    await _trigger(client, run)
    return run


async def _trigger(client: AsyncClient, run: SyncRun) -> dict[str, Any]:
    """对同一条源再打一次同步——幂等断言要的就是"同一个目标跑第二遍"。"""
    resp = await client.post(
        "/api/sync/jobs", json={"datasource_id": run.ds_id}, headers=run.headers
    )
    assert resp.status_code == 200, resp.text
    run.body = resp.json()
    assert run.ro_password not in resp.text, "响应体里出现了源库口令"
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
    assert float(inferred[0]["confidence"]) == 0.7, "0.7 出自 roadmap P4，改它要连文档一起改"
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
