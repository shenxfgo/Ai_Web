"""真连一次演示库 ai_web_demo：POST /datasources/{id}/test。

标记 live：这条要真 MySQL 在线。凭据从 gitignored 的 `.setup/aiweb_ro.cnf` 读，
不写进 .env、不进仓库、不进任何打印。缺文件就 skip——换台机器不该把整个闸口卡红。

期望值口径：
- 工单 006 验收 3：连接成功 / 9 表 1 视图 / 只读能力 OK / MySQL 5.7.x / max_execution_time 支持
- `docs/architecture.md` §7：`{ok, server_version, visible_schemas, est_table_count,
  grants:{read_only,warnings}, latency_ms}`，body 里可带未保存的临时口令
- 验收 4：口令错误要回结构化 AppError，说清是哪个字段错了，且不抛栈
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import Account, DbAccount

pytestmark = [pytest.mark.pg, pytest.mark.live]

Login = Callable[..., Awaitable[Account]]

DEMO_DB = "ai_web_demo"

# 建库脚本 scripts/init_demo_mysql.sql 的自检口径：9 张 BASE TABLE + 1 个 VIEW
TABLES = 9
VIEWS = 1


async def _register(
    client: AsyncClient, login: Login, account: DbAccount, **over: Any
) -> tuple[int, dict[str, str], str]:
    """登记一个指向演示库的源，返回 (id, 认证头, 口令)。"""
    user, password, host, port = account
    acct = await login(username=str(over.get("who", "probe-owner")), role="member")
    body = {
        "name": str(over.get("name", "demo-live")),
        "kind": "mysql",
        "host": host,
        "port": port,
        "connect_user": user,
        "connect_password": password,
        "include_schemas": [DEMO_DB],
        # 建库脚本自检的口径：下划线开头的是脚本自己的辅助表，不是业务表。
        # 这里不 hard-code 进代码——它是**这条源**的范围配置，登记时由用户填。
        "exclude_tables": ["\\_%"],
    }
    resp = await client.post("/api/datasources", json=body, headers=acct.headers)
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"]), acct.headers, password


async def test_测试连接报得出表数视图数和只读结论(
    client: AsyncClient,
    login: Login,
    account: DbAccount,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ds_id, headers, password = await _register(client, login, account)

    # 探测端点也要鉴权：它能连到内网源库，无鉴权就等于给外网开了个端口探测器
    anon = await client.post(f"/api/datasources/{ds_id}/test", json={})
    assert anon.status_code == 401

    resp = await client.post(f"/api/datasources/{ds_id}/test", json={}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["server_version"].startswith("5.7"), body["server_version"]
    assert DEMO_DB in body["visible_schemas"]
    assert body["table_count"] == TABLES and body["view_count"] == VIEWS
    assert body["est_table_count"] == TABLES + VIEWS
    assert body["grants"]["read_only"] is True, body["grants"]
    assert body["grants"]["code"] is None, "只读账号却报了能力缺失码——判定太宽就是假绿的反面"
    assert body["supports_max_execution_time"] is True
    # 这个按钮的意义是"几秒内给人一个答复"，它自己不能变成一次慢查询
    assert body["latency_ms"] < 5000, body["latency_ms"]
    assert password not in resp.text

    # metadata-model §2.2：`server_version text NULL -- test_connection 时探测写入`。
    # 只在响应里回一份不算写入——这一列存在的唯一理由就是它，不写就等于白建。
    async with session_factory() as session:
        stored = (
            await session.execute(
                text(
                    f'select server_version from "{os.environ["AIWEB_PG__SCHEMA_NAME"]}"'
                    ".data_sources where id = :i"
                ),
                {"i": ds_id},
            )
        ).scalar_one()
    assert stored is not None and stored.startswith("5.7"), stored


async def test_口令错时回结构化错误并点出是哪个字段(
    client: AsyncClient, login: Login, account: DbAccount
) -> None:
    """驱动抛的是 1045，我们得翻成"connect_user 或 connect_password 不对"。

    原样抛栈的话：一是 500，二是异常链里带着连接串（asyncmy 的 OperationalError 会带上
    用户名与主机），三是用户完全不知道该改哪一格。
    """
    user, _, host, port = account
    acct = await login(username="probe-2", role="member")
    resp = await client.post(
        "/api/datasources",
        json={
            "name": "demo-wrong-pwd",
            "kind": "mysql",
            "host": host,
            "port": port,
            "connect_user": user,
            # 未保存的临时口令：§7 允许 body 带 connect_password，用来"先试再存"
            "connect_password": "显然是错的-口令",
            "include_schemas": [DEMO_DB],
        },
        headers=acct.headers,
    )
    ds_id = resp.json()["id"]

    probe = await client.post(
        f"/api/datasources/{ds_id}/test",
        json={"connect_password": "显然是错的-口令"},
        headers=acct.headers,
    )
    assert probe.status_code == 400, probe.text
    err = probe.json()["error"]
    assert err["code"] == "source_unreachable"
    assert "connect_password" in err["message"]
    assert "Traceback" not in probe.text
