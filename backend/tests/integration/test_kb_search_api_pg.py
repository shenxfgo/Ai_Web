"""检索预览接口：`POST /api/kb/search`（工单 009 的接口层）。

接缝选在 HTTP 而不是 service，因为这一片真正会错的是**边界**：`datasource_ids` 里塞别人
私有源时该 403 而不是安静少一张、问句长度该有上限（它直接变成 SQL 里的 ILIKE 参数）、
响应里不该出现数据源字段。命中算法本身在 `test_kb_search_pg.py` 那一侧验。

期望值口径：
- `docs/architecture.md` §7 端点表：read 权、body `{query,datasource_ids[],k}`
- `docs/architecture.md` §5.2 (5)：权限过滤在召回这一层就做
- 工单 006 那把尺：口令与 `connect_user` 不进响应体
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401
from tests.integration.conftest import Account
from tests.integration.test_kb_cards_pg import _cards, _column, _data_source, _table

pytestmark = pytest.mark.pg

Login = Callable[..., Awaitable[Account]]


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    owner_id: int,
    name: str,
    table: str = "order_main",
    allow_global: bool = False,
) -> dict[str, Any]:
    """一条源 + 一张两列的中文注释表 + 真跑一次卡片落库，把 id/uid 带回来。"""
    async with session_factory() as session:
        src = _data_source(
            owner_id,
            name,
            allow_global=allow_global,
            # 假的哨兵口令：专门用来断言它不会出现在响应体里
            secret="p@ss-not-for-response",
        )
        session.add(src)
        await session.flush()
        tbl = await _table(session, src, table, comment_zh="订单主表")
        session.add_all(
            [
                _column(tbl, 1, "amount", comment_zh="订单金额（元，优惠前）"),
                _column(tbl, 2, "status", comment_zh="订单状态"),
            ]
        )
        await session.flush()
        await _cards(session, src, [tbl.id])
        await session.commit()
        return {"ds_id": src.id, "table_uid": tbl.table_uid}


async def test_一句问句换来一张候选表并带命中理由(
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """预览端点存在的意义就是让人肉验证命中：分数、命中词数/列数、哪个词对上哪个字段都要看得见。"""
    acct = await login(username="searcher", role="member")
    seeded = await _seed(session_factory, owner_id=acct.user_id, name="demo-search")

    resp = await client.post(
        "/api/kb/search",
        json={"query": "订单金额", "datasource_ids": [seeded["ds_id"]], "k": 5},
        headers=acct.headers,
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["table_uid"] for i in items] == [seeded["table_uid"]]
    only = items[0]
    # 问句切成 `订单金额/订单/单金/金额`：amount 的注释全对上，status 只对上 `订单`。
    assert only["matched_term_count"] == 4
    assert only["matched_column_count"] == 2
    assert {(h["column_name"], h["field"]) for h in only["hits"]} == {
        ("amount", "comment_zh"),
        ("status", "comment_zh"),
    }
    assert {"订单金额", "订单"} <= {h["term"] for h in only["hits"]}
    assert only["score_kw"] > 0


async def test_点名的源里没有我的权限时回403(
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """预览端点把越权抬成 403，而不是安静少一张。

    它的全部用途就是让人判断"为什么没命中"，把越权降级成空结果等于把这个诊断废掉。
    真检索路径（012 的 `/chat/ask`）反过来——那边按 §5.2 (5) 静默过滤，不能因为一个坏 id
    就整条问数失败。
    """
    owner = await login(username="search-owner", role="member")
    seeded = await _seed(session_factory, owner_id=owner.user_id, name="demo-private")
    stranger = await login(username="search-stranger", role="member")

    resp = await client.post(
        "/api/kb/search",
        json={"query": "订单金额", "datasource_ids": [seeded["ds_id"]], "k": 5},
        headers=stranger.headers,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "forbidden"


async def test_不点名源时在我看得见的全范围里找_global档算看得见(
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`datasource_ids` 省略 = 我的可见集合，走的是同一把 `access_of` 尺。

    global 档那条是关键：owner 判据写错成"只能搜自己的源"，这一条就会红。
    """
    owner = await login(username="global-search-owner", role="member")
    seeded = await _seed(
        session_factory, owner_id=owner.user_id, name="demo-global-search", allow_global=True
    )
    reader = await login(username="global-reader", role="member")

    resp = await client.post("/api/kb/search", json={"query": "订单金额"}, headers=reader.headers)
    assert resp.status_code == 200, resp.text
    assert [i["table_uid"] for i in resp.json()["items"]] == [seeded["table_uid"]]


async def test_零命中时items是空数组(
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """§4.1 的 fail-fast 靠这个形状：空集合是合法答案，不是 404 也不是 500。"""
    acct = await login(username="search-miss", role="member")
    await _seed(session_factory, owner_id=acct.user_id, name="demo-miss")

    resp = await client.post(
        "/api/kb/search", json={"query": "卫星云图降雨量"}, headers=acct.headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


async def test_响应里不含数据源字段与口令(
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """检索打的是元数据库，但候选表挂在源上——响应里不能顺手漏出源的任何身份。"""
    acct = await login(username="search-leak", role="member")
    await _seed(session_factory, owner_id=acct.user_id, name="demo-leak")

    resp = await client.post("/api/kb/search", json={"query": "订单金额"}, headers=acct.headers)
    assert resp.status_code == 200, resp.text
    assert "p@ss-not-for-response" not in resp.text
    assert "connect_user" not in resp.text
    assert "aiweb_ro" not in resp.text
    assert "datasource_id" not in resp.text


async def test_问句空或过长回422(
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """问句直接变成 SQL 里的 ILIKE 参数，长度必须有上限；空问句则什么都切不出来。"""
    acct = await login(username="search-422", role="member")
    await _seed(session_factory, owner_id=acct.user_id, name="demo-422")

    for bad in ("", "订" * 201):
        resp = await client.post("/api/kb/search", json={"query": bad}, headers=acct.headers)
        assert resp.status_code == 422, resp.text


async def test_传P4的检索旋钮时当场422而不是假装收下了(
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """§7 端点表里那四个向量旋钮 P2 不收，收的方式是拒。

    静默忽略会留下最难查的一种"没生效"：前端把 `mode=vector` 传上来，接口返回的还是 ILIKE
    的结果，看上去一切正常。422 直接把话说明白。
    """
    acct = await login(username="search-forbid", role="member")
    await _seed(session_factory, owner_id=acct.user_id, name="demo-forbid")

    for extra in (
        {"mode": "vector"},
        {"top_vector": 20},
        {"ef_search": 64},
        {"trgm_threshold": 0.3},
    ):
        resp = await client.post(
            "/api/kb/search", json={"query": "订单金额", **extra}, headers=acct.headers
        )
        assert resp.status_code == 422, f"{extra} → {resp.status_code} {resp.text}"


async def test_未登录回401(client: AsyncClient) -> None:
    resp = await client.post("/api/kb/search", json={"query": "订单金额"})
    assert resp.status_code == 401, resp.text
