"""同步终局必须清掉 JOIN 图缓存（工单 014 拍板 2 的接线那一半）。

单开一个文件是因为这里要钉的不是"同步说了什么"，而是**同步之外那份进程内缓存**：
`join_graph._CACHE` 的键由 `(datasource_id, database_id)` 决定，而三条不同的终局
（成功 / 部分失败 / 一个库都没写成）各自该清掉哪些键，"部分失败"那一条只有配合缓存
才看得出代价——清多了，下一次问数要为一次没人动过结构的库重新查库建图；清少了，
用户点完"同步"看到的还是旧边。

桩件、`_register`/`_sync`/`_scalar` 与 `stub` 夹具都来自 `test_sync_pg.py`：那片已经把
"抽取中途炸了"这个前提搭好了（真 MySQL 给不了这个前提），这里只是换一个问题问它。

期望值口径：architecture §5.3「缓存与失效」+ 工单 014「开工前拍板 2」。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.nl2sql import join_graph
from tests.integration.test_sync_pg import (
    _fk,
    _register,
    _scalar,
    _sync,
    stub,  # noqa: F401 —— 夹具要 import 进来才在本模块可见
)

pytestmark = pytest.mark.pg

Login = Callable[..., Awaitable[Any]]


@pytest.fixture(autouse=True)
def _clean_cache() -> Any:
    """缓存是模块级全局：进出各清一次，别把上一条用例的键带进来、也别把这里的带出去。"""
    join_graph._CACHE.clear()
    yield
    join_graph._CACHE.clear()


async def _warm(session_factory: async_sessionmaker[AsyncSession], ds_id: int, schema: str) -> int:
    """把某个库的图放进缓存，返回缓存键的后半（`database_id`）。

    走真 `graph_for` 而不是手写进 `_CACHE`：那样钉的是"真建过一次图之后会不会被清掉"，
    而键的形状由 `graph_for` 自己决定，用例替它编一个键就测不到形状变了。
    """
    out = await _scalar(
        session_factory,
        "select id from {s}.meta_database where datasource_id = :ds and schema_name = :schema",
        ds=ds_id,
        schema=schema,
    )
    assert out is not None, f"{schema} 库还没落库，缓存的前提就是空的"
    database_id = int(out)
    async with session_factory() as session:
        await join_graph.graph_for(
            session,
            datasource_id=ds_id,
            database_id=database_id,
        )
    assert (ds_id, database_id) in join_graph._CACHE, "预热没成功，后面的断言都是空的"
    return database_id


async def test_同步成功要清掉本轮那个库的图(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """用户点完"同步"就期待新结构生效——不清的话这条源要等进程重启才看得见新边。"""
    acct = await login(username="owner-cache-ok", role="member")
    ds_id = await _register(client, acct)
    stub(
        tables={"shop": ("orders", "users")},
        fks={"shop": [_fk("shop", "orders", "user_id", "users")]},
    )
    assert (await _sync(client, acct, ds_id)).status_code == 200

    shop = await _warm(session_factory, ds_id, "shop")

    assert (await _sync(client, acct, ds_id)).status_code == 200
    assert (ds_id, shop) not in join_graph._CACHE


async def test_部分失败只清提交完的那个库(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`run_sync` 把清缓存放在 finally 而不是成功分支的末尾，两条半边都得成立。

    - **提交完的那个要清**：一个 catalog 一个事务，后面的库失败不会把前面那次提交退回去；
      不清就是拿旧图回答一个已经改过结构的库。
    - **失败的那个不许清**：它那一次 `_write_catalog` 整段 rollback，`meta_relation` 一行没变，
      清它是让下一次问数白跑一趟查库建图。这一格同时也是"清在 finally 里"的证据——
      整次请求的状态是 `partial`，成功分支末尾那段收尾根本没执行。
    """
    acct = await login(username="owner-cache-partial", role="member")
    ds_id = await _register(client, acct, include_schemas=["shop", "warehouse"])
    tables = {"shop": ("orders", "users"), "warehouse": ("shipments", "orders")}
    fks = {
        "shop": [_fk("shop", "orders", "user_id", "users")],
        "warehouse": [_fk("warehouse", "shipments", "order_id", "orders")],
    }
    stub(catalogs=("shop", "warehouse"), tables=tables, fks=fks)
    assert (await _sync(client, acct, ds_id)).status_code == 200
    shop = await _warm(session_factory, ds_id, "shop")
    warehouse = await _warm(session_factory, ds_id, "warehouse")

    second = stub(catalogs=("shop", "warehouse"), tables=tables, fks=fks, fail_on="warehouse")
    resp = await _sync(client, acct, ds_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "partial", resp.json()
    assert second.calls == ["shop", "warehouse"], "桩没走到失败那一步，上面两条断言就是空的"

    assert (ds_id, shop) not in join_graph._CACHE, "提交完的库还留着旧图"
    assert (ds_id, warehouse) in join_graph._CACHE, "回滚过的库结构没变，清它是白工"


async def test_一个库都没写成时一个键都不清(
    client: AsyncClient,
    login: Login,
    stub: Callable[..., Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """唯一的 catalog 就失败 → 本轮提交过的库是空集，这时清缓存等于把"没同步成"说成"结构变了"。

    与上一条是一对：清的对象是"本轮真提交过的库"这个集合，集合为空就是什么都不清。
    缓存里留着的那份仍然与库里的行一致——上一次成功同步才是它的来源。
    """
    acct = await login(username="owner-cache-none", role="member")
    ds_id = await _register(client, acct)
    stub(tables={"shop": ("orders", "users")})
    assert (await _sync(client, acct, ds_id)).status_code == 200
    shop = await _warm(session_factory, ds_id, "shop")

    stub(tables={"shop": ("orders", "users")}, fail_on="shop")
    resp = await _sync(client, acct, ds_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "partial", resp.json()
    assert (ds_id, shop) in join_graph._CACHE
