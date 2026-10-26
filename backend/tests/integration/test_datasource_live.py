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


async def test_非只读账号探得出缺失能力_但登记那道闸门仍然打开(
    client: AsyncClient, login: Login, rw_account: DbAccount
) -> None:
    """工单 040 验收 5 的**反半**：`grants_verdict` 说"这不是只读账号"那一支，第一次由真账号回答。

    上面那一发（`test_测试连接报得出表数视图数和只读结论`）用的是只读账号 `aiweb_ro`，
    它只能证 `read_only is True` / `code is None`。判决的**另一支**在本文件今天没有实物——
    此前只有 `tests/unit/test_source_probe.py:96-163` 那族喂字符串的判决定签过，
    而"safety §4.1 那句黄色警告真从端点回得出来"这件事写在文档上却没人跑过。
    凭证是工单格子 ② 用户 2026-10-20 点名授权的那把（`my_login.cnf`），
    用它之前先按工单字面跑了只读 `show grants` 核实身份，读数在
    `.scratch/p5-sql-guard-hardening/evidence/grants_raw040.txt` + `grants_verdict040.txt`。

    断三件事，少一件就不是这一格：
    - **HTTP 仍是 200**：`test_connection` 不是错误通道（§7 的 docstring 明写"阻断留给 011"）。
      做成 4xx 就变成"登记页面拒绝保存"，那是 P7/P9 的表单语义，工单"明确不做"第四条拦的就是它。
    - **码可以 switch**：`grants.code == "readonly_capability_missing"`，与执行路那把
      `ReadonlyCapabilityMissing` 同一枚码——但**两档不合并**，硬阻断仍由
      `tests/unit/test_executor_grants.py:33` 签。
    - **口令不出现在响应里**：这一发用的是本机建库账号，泄露代价比只读账号高一档。
    """
    ds_id, headers, password = await _register(
        client, login, rw_account, name="demo-not-readonly", who="probe-rw-owner"
    )

    resp = await client.post(f"/api/datasources/{ds_id}/test", json={}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # "连得上"与"只读不只读"是两件事：非只读账号照样连得上，闸门在下一格
    assert body["ok"] is True
    assert body["grants"]["read_only"] is False
    assert body["grants"]["code"] == "readonly_capability_missing", body["grants"]
    assert body["grants"]["warnings"], "只给码不给原文行，用户就没法自己核对是不是真读错了账号"
    # 不用 `assert password not in resp.text`：pytest 的重写会把左操作数的**字面值**倒进终端，
    # 而这一发用的是本机建库账号（`my_login.cnf`）——一次红测就等于把口令贴进 CI 日志。
    # 同一条理由见 conftest 的 `DbAccount.__repr__`；这一格只报"发生了"，不报"是什么"。
    if password in resp.text:
        pytest.fail("响应体里出现了连接口令（口令不外泄是 §6 的口径，这里不回显它）")
