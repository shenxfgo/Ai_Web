"""卡片读取接口：`GET /api/kb/cards?table_uid=`（工单 008 的接口层）。

接缝选在 HTTP 而不是 service，因为这一片真正会错的是**权限档位**：architecture §7 给卡片读
的是 read 权（`global` 档也能读），而 sync / datasource 那几个端点要 owner 档——两把尺用错
一把，只有打真端点才看得出来。

期望值口径：
- `docs/architecture.md` §7 端点表：read 权、"卡片全文 + embedding 元信息（维度/模型/建索引时间）"
- `docs/kb-workflow.md` §6：一表多段，主卡 `seq=0` 在前
- `docs/metadata-model.md` §2.6：`doc_uid = md5(kind|table_uid|seq|index_profile_id)`
- `docs/verification.md` §2.2：`dimensions` 列宽 1536（tests/conftest.py 钉死）
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401
from app.core.security import encrypt_secret
from app.models.datasource import DataSource
from app.models.meta import MetaColumn, MetaDatabase, MetaTable
from app.services.kb_service import card_doc_uid, sync_cards
from tests.integration.conftest import Account

pytestmark = pytest.mark.pg

Login = Callable[..., Awaitable[Account]]

# 输出模型的白名单：多一列就等于把 data_sources 的字段顺手漏出去
_CARD_KEYS = {
    "id",
    "kind",
    "seq",
    "doc_uid",
    "title",
    "text_md",
    "token_count",
    "meta",
    "embedded_at",
    "index_profile",
}
_PROFILE_KEYS = {"id", "name", "model", "dimensions", "card_template_version"}


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    owner_id: int,
    name: str,
    columns: int = 1,
    allow_global: bool = False,
    with_cards: bool = True,
) -> dict[str, Any]:
    """建一条源 + 一张带 N 列的表（可选：真跑一次卡片落库），把 id/uid 带回来。"""
    async with session_factory() as session:
        src = DataSource(
            name=name,
            kind="mysql",
            host="127.0.0.1",
            port=3306,
            connect_user="aiweb_ro",
            secret_enc=encrypt_secret("p@ss-not-for-response"),
            server_version="5.7.44-log",
            allow_global_access=allow_global,
            created_by=owner_id,
        )
        session.add(src)
        await session.flush()
        db_row = MetaDatabase(datasource_id=src.id, catalog_name="", schema_name="ai_web_demo")
        session.add(db_row)
        await session.flush()
        tbl = MetaTable(
            datasource_id=src.id,
            database_id=db_row.id,
            catalog_name="",
            schema_name="ai_web_demo",
            table_name="order_main",
            table_type="BASE TABLE",
            comment_zh="订单主表",
        )
        session.add(tbl)
        await session.flush()
        session.add_all(
            [
                MetaColumn(
                    table_id=tbl.id,
                    ordinal_position=1,
                    column_name="id",
                    data_type="bigint",
                    raw_data_type="bigint",
                    nullable=False,
                    is_primary_key=True,
                )
            ]
            + [
                MetaColumn(
                    table_id=tbl.id,
                    ordinal_position=i + 2,
                    column_name=f"c{i}",
                    data_type="int",
                    raw_data_type="int",
                )
                for i in range(columns - 1)
            ]
        )
        await session.flush()
        uid = tbl.table_uid
        out = {"ds_id": src.id, "table_id": tbl.id, "table_uid": uid}
        if with_cards:
            await sync_cards(
                session,
                datasource_id=src.id,
                job_id=None,
                table_ids=[tbl.id],
                dialect_name=src.kind,
                server_version=src.server_version or "",
            )
        await session.commit()
        return out


async def test_按table_uid取回一张表的全部卡片段按seq升序(
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """一张宽表在库里是 2 行 `kb_card`，接口必须把**全部段**都给出去。

    只回主卡的话，前端拿到的是前 25 列，AI 会以为这张表只有 25 列——
    切片是 kb-workflow §6 定的，读侧把它截断就是凭空丢知识。
    """
    acct = await login(username="card-owner", role="member")
    seeded = await _seed(session_factory, owner_id=acct.user_id, name="demo-wide", columns=42)

    resp = await client.get(
        "/api/kb/cards", params={"table_uid": seeded["table_uid"]}, headers=acct.headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert [c["kind"] for c in body] == ["table", "table_columns"]
    assert [c["seq"] for c in body] == [0, 1]
    assert body[0]["title"] == "ai_web_demo.order_main（订单主表）"
    assert body[0]["text_md"].startswith("【表】ai_web_demo.order_main")
    assert set(body[0]) == _CARD_KEYS
    assert set(body[0]["index_profile"]) == _PROFILE_KEYS
    # §2.6 的指纹：读侧回的要能和按四个输入重算出来的一致，否则前端没法把段去重
    assert body[0]["doc_uid"] == card_doc_uid(
        "table",
        seeded["table_uid"],
        seq=body[0]["seq"],
        index_profile_id=body[0]["index_profile"]["id"],
    )
    assert body[0]["token_count"] > 0
    # §7 的"embedding 元信息"：维度/模型/建索引时间——没向量化时是 NULL，但字段必须在
    assert body[0]["embedded_at"] is None
    assert body[0]["index_profile"]["dimensions"] == 1536
    assert body[0]["index_profile"]["card_template_version"] == 1


async def test_响应里不含数据源字段与口令(
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """卡片挂在源上，读卡片的人只该看见卡片：`connect_user` 和口令都不在 DTO 里。"""
    acct = await login(username="leak-check", role="member")
    seeded = await _seed(session_factory, owner_id=acct.user_id, name="demo-leak")

    resp = await client.get(
        "/api/kb/cards", params={"table_uid": seeded["table_uid"]}, headers=acct.headers
    )
    assert resp.status_code == 200, resp.text
    assert "p@ss-not-for-response" not in resp.text
    assert "connect_user" not in resp.text
    assert "host" not in resp.text


async def test_未知table_uid回404(client: AsyncClient, login: Login) -> None:
    """不是空数组：`table_uid` 是对外标识，指着一个不存在的东西是调用方的错，要看得见。"""
    acct = await login(username="nobody-owner", role="member")
    resp = await client.get("/api/kb/cards", params={"table_uid": "0" * 32}, headers=acct.headers)
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "not_found"


async def test_表存在但还没建卡回空数组(
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """同步只落了 `meta_*` 时卡片是零条——这是"还没建卡"的真实状态，不是 404。

    回 404 会让前端把它显示成"表不存在"，而用户下一步恰恰是去点同步。
    """
    acct = await login(username="nocards", role="member")
    seeded = await _seed(
        session_factory, owner_id=acct.user_id, name="demo-nocards", with_cards=False
    )

    resp = await client.get(
        "/api/kb/cards", params={"table_uid": seeded["table_uid"]}, headers=acct.headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


async def test_无权限的人读不到卡片(
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """别人的源没开全局可见时，拿着 uid 也读不到——uid 不是能力凭证。"""
    owner = await login(username="real-owner", role="member")
    seeded = await _seed(session_factory, owner_id=owner.user_id, name="demo-private")
    stranger = await login(username="stranger", role="member")

    resp = await client.get(
        "/api/kb/cards", params={"table_uid": seeded["table_uid"]}, headers=stranger.headers
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "forbidden"


async def test_global档的人能读卡片_read权而不是owner权(
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """§7 给 `/kb/cards` 的是 read 权：`allow_global_access` 的源，非 owner 也读得到。

    这一条和上一条是一对——只测 403 的话，把档位写成 owner 也照样绿。
    """
    owner = await login(username="global-owner", role="member")
    seeded = await _seed(
        session_factory, owner_id=owner.user_id, name="demo-global", allow_global=True
    )
    reader = await login(username="reader", role="member")

    resp = await client.get(
        "/api/kb/cards", params={"table_uid": seeded["table_uid"]}, headers=reader.headers
    )
    assert resp.status_code == 200, resp.text
    assert [c["seq"] for c in resp.json()] == [0]


async def test_未登录回401(client: AsyncClient) -> None:
    resp = await client.get("/api/kb/cards", params={"table_uid": "0" * 32})
    assert resp.status_code == 401, resp.text
