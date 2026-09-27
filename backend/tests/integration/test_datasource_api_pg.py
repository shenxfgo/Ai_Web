"""数据源登记接口：口令进库即成密文，而且永不回头。

打真库 + 真 ASGI 应用（夹具见 integration/conftest.py）。这里盯三件事：密文真的落在
bytea 里、响应体里搜不到口令、owner 判定只看 created_by。

期望值口径：
- `docs/roadmap.md` P2 验收 3：`SELECT left(convert_from(secret_enc,'UTF8'),12)` 得 `gAAAAAB` 开头
- `docs/architecture.md` §7：POST 201「密码即刻 Fernet 加密」；`connect_password` 永不回传，
  只回 `password_masked:'••••'` + `has_secret:true`
- `docs/metadata-model.md` §2.2 + ADR-0008：数据源级授权，owner 依据 created_by
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import Account

pytestmark = pytest.mark.pg

Login = Callable[..., Awaitable[Account]]

DB = {
    "name": "demo-mysql",
    "kind": "mysql",
    "host": "127.0.0.1",
    "port": 3306,
    "connect_user": "aiweb_ro",
}


async def test_登记数据源时口令即刻加密落库且响应体里没有它(
    client: AsyncClient, login: Login, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    secret = "Demo-口令-9"
    resp = await client.post(
        "/api/datasources",
        json={**DB, "connect_password": secret},
        headers=(await login(username="owner1", role="member")).headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["password_masked"] == "••••"
    assert body["has_secret"] is True
    # 白名单式输出模型的价值就在这三条：以后有人往模型里加个 secret_enc，它当场变红
    assert secret not in resp.text
    assert "connect_password" not in resp.text
    assert "secret_enc" not in resp.text

    # 验收原文那句在库里核对：前 12 字节是 gAAAAAB（Fernet 的版本+时间戳头）。
    # 必须是 convert_from 之后再截：PG 的 left() 只有 text 版本，直接 left(secret_enc,12)
    # 会报 UndefinedFunction——roadmap 那句最初就是这么写的，跑第一遍才发现，已更正。
    # 在 Python 里切 bytes 不算数：验收要核对的是"库里那一列"长什么样。
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        prefix = (
            await session.execute(
                text(
                    f"select left(convert_from(secret_enc,'UTF8'),12) "
                    f'from "{schema}".data_sources'
                )
            )
        ).scalar_one()
    assert prefix.startswith("gAAAAAB")


async def test_列表只返回有权看见的源(client: AsyncClient, login: Login) -> None:
    """`access` 三档同时决定"看不看得见"：owner 看自己的，global 谁都看得见，其余不出现。

    列表按 architecture §7 是"只返回有权的"。漏了这一条的话，任何人都能靠一次 GET
    枚举出全公司接了哪些库、什么地址——这本身就是一份攻击面清单。
    """
    mine = await login(username="alice", role="member")
    other = await login(username="bob", role="member")
    admin = await login(username="root", role="admin")

    async def add(name: str, headers: dict[str, str], **extra: object) -> int:
        resp = await client.post(
            "/api/datasources",
            json={**DB, **extra, "name": name, "connect_password": "p-1"},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        return int(resp.json()["id"])

    own_id = await add("alice-own", mine.headers)
    global_id = await add("shared", other.headers, allow_global_access=True)
    hidden_id = await add("bob-private", other.headers)

    listing = await client.get("/api/datasources", headers=mine.headers)
    assert listing.status_code == 200
    # §7 给这个端点的是裸数组（无 cursor），不是 {items,next_cursor}
    rows = listing.json()
    assert isinstance(rows, list)
    items = {row["id"]: row["access"] for row in rows}
    assert items[own_id] == "owner"
    assert items[global_id] == "global"
    assert hidden_id not in items, "别人的私有源出现在了列表里"
    # 列表里同样不许出现口令的任何形态（明文、掩码字段名、密文列名）
    assert "connect_password" not in listing.text and "secret_enc" not in listing.text
    assert "p-1" not in listing.text, "三条源的明文口令出现在了列表里"

    # admin 一档：不是 owner 也全看得见（他要能管授权）
    as_admin = await client.get("/api/datasources", headers=admin.headers)
    assert {hidden_id, global_id, own_id} <= {row["id"] for row in as_admin.json()}


async def test_非owner取详情被拒而全局源放行(client: AsyncClient, login: Login) -> None:
    """403 而不是 404：roadmap P2 验收点名的就是"未授权用户访问该数据源 → 403"。

    选 403 有个代价——它承认了"这个 id 存在"。这里接受，因为数据源 id 只能从创建接口
    拿到连续整数，枚举本来就可行；换成 404 只是把泄露换成"用户以为源被删了"的困惑。
    """
    owner = await login(username="carol", role="member")
    stranger = await login(username="dave", role="member")
    resp = await client.post(
        "/api/datasources",
        json={**DB, "name": "carol-private", "connect_password": "p-1"},
        headers=owner.headers,
    )
    ds_id = resp.json()["id"]

    denied = await client.get(f"/api/datasources/{ds_id}", headers=stranger.headers)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "forbidden"
    assert "p-1" not in denied.text

    allowed = await client.get(f"/api/datasources/{ds_id}", headers=owner.headers)
    assert allowed.status_code == 200
    body = allowed.json()
    assert body["password_masked"] == "••••" and body["has_secret"] is True
    assert "connect_password" not in allowed.text and "secret_enc" not in allowed.text

    # 不存在的 id 才是 404，别和 403 混成一档
    missing = await client.get("/api/datasources/999999", headers=owner.headers)
    assert missing.status_code == 404


async def test_删除是软删且删完就读不到了(
    client: AsyncClient, login: Login, session_factory
) -> None:
    """§7 的 DELETE 写的是"软删 deleted_at"，所以行必须还在库里。

    行还在 ≠ 到处还能读到：列表与详情两条路都要当它已经没了，否则一个被删掉的源会
    继续出现在别人面前，或者更糟——继续被拿去连。（name 是另一回事：那一列仍是全局
    UNIQUE，见 `test_软删之后同名仍然不可复用`。）
    """
    mine = await login(username="erin", role="member")
    resp = await client.post(
        "/api/datasources",
        json={**DB, "name": "to-purge", "connect_password": "p-1"},
        headers=mine.headers,
    )
    ds_id = resp.json()["id"]

    gone = await client.delete(f"/api/datasources/{ds_id}", headers=mine.headers)
    assert gone.status_code == 204

    assert (await client.get("/api/datasources", headers=mine.headers)).json() == []
    assert (await client.get(f"/api/datasources/{ds_id}", headers=mine.headers)).status_code == 404

    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        deleted_at = (
            await session.execute(
                text(f'select deleted_at from "{schema}".data_sources where id = :i'),
                {"i": ds_id},
            )
        ).scalar_one()
    assert deleted_at is not None
    assert deleted_at.utcoffset() is not None  # timestamptz 往返后仍带时区


async def test_全局源对非owner可读不可删(client: AsyncClient, login: Login) -> None:
    """'global' 只买到读权。让 DELETE 只认 owner，是 §7 那一列"owner/admin"的字面要求。"""
    sharer = await login(username="frank", role="member")
    borrower = await login(username="gina", role="member")
    resp = await client.post(
        "/api/datasources",
        json={
            **DB,
            "name": "shared-read-only",
            "connect_password": "p-1",
            "allow_global_access": True,
        },
        headers=sharer.headers,
    )
    ds_id = resp.json()["id"]

    got = await client.get(f"/api/datasources/{ds_id}", headers=borrower.headers)
    assert got.status_code == 200 and got.json()["access"] == "global"

    denied = await client.delete(f"/api/datasources/{ds_id}", headers=borrower.headers)
    assert denied.status_code == 403
    still = await client.get(f"/api/datasources/{ds_id}", headers=sharer.headers)
    assert still.status_code == 200, "别人的 403 不该把源删掉"


async def test_重名冲突报409而不是500(client: AsyncClient, login: Login) -> None:
    """name 上有全局 UNIQUE，而它同时是知识库目录名（kb-workflow）。

    不拦的话 IntegrityError 会被统一处理器收成 500 database_error：前端读不出"这个名字
    已被占用"，用户就以为服务坏了。
    """
    a = await login(username="henry", role="member")
    b = await login(username="iris", role="member")
    first = await client.post(
        "/api/datasources",
        json={**DB, "name": "dup", "connect_password": "p-1"},
        headers=a.headers,
    )
    assert first.status_code == 201
    second = await client.post(
        "/api/datasources",
        json={**DB, "name": "dup", "connect_password": "p-2"},
        headers=b.headers,
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"
    assert "p-2" not in second.text


async def test_软删之后同名仍然不可复用(client: AsyncClient, login: Login) -> None:
    """§2.2 的 `name` 是无条件 UNIQUE，不是 `WHERE deleted_at IS NULL` 的部分索引。

    钉住而不是修：软删的行还占着名字，这是**当前实现的既定行为**，而两种选择各有代价
    （放行=同名重建能走；拦住=历史源的知识卡目录不会被复用）。选哪种要改 §2.2，
    不是改一处 service，所以这里只把行为钉死，口径歧义记在工单 006 的偏差清单里。
    """
    mine = await login(username="jack", role="member")
    first = await client.post(
        "/api/datasources",
        json={**DB, "name": "reused", "connect_password": "p-1"},
        headers=mine.headers,
    )
    await client.delete(f"/api/datasources/{first.json()['id']}", headers=mine.headers)

    again = await client.post(
        "/api/datasources",
        json={**DB, "name": "reused", "connect_password": "p-1"},
        headers=mine.headers,
    )
    assert again.status_code == 409, "软删的行不再占名字的话，这条要一起改 §2.2"


async def test_请求校验失败时不回显请求体里的口令(client: AsyncClient, login: Login) -> None:
    """422 的 detail 里带 pydantic 的 `input`，而 input 是**整个**请求体。

    也就是说哪怕报错的是 port 那一格，响应里也会把同一份 body 的 connect_password
    原样吐回去。这是"口令永不回传"最容易被绕过的一条路：它绕过了输出模型白名单，
    因为白名单只管成功路径。
    """
    secret = "422-leak-口令"
    resp = await client.post(
        "/api/datasources",
        json={**DB, "connect_password": secret, "port": 99999},  # port 越界 → 422
        headers=(await login(username="kate", role="member")).headers,
    )
    assert resp.status_code == 422, resp.text
    assert secret not in resp.text, "校验错误的 detail 回显了请求体"
    # detail 仍然要能定位到那一格，否则前端没法把红字挂回表单项
    assert "port" in resp.text


async def test_临时口令传空串不会被库里已存的顶替(client: AsyncClient, login: Login) -> None:
    """空串和"没填"是两件事：没填=用库里那份，空串=用户把框清空了。

    `payload.connect_password or 已存` 这种写法会把前者静默当成后者，用户看到的是
    "清空口令还能连上"。给字段加 min_length=1，让空串在门口就变 422。
    """
    acct = await login(username="kate2", role="member")
    resp = await client.post(
        "/api/datasources",
        json={**DB, "name": "empty-temp", "connect_password": "p-1"},
        headers=acct.headers,
    )
    ds_id = resp.json()["id"]

    empty = await client.post(
        f"/api/datasources/{ds_id}/test",
        json={"connect_password": ""},
        headers=acct.headers,
    )
    assert empty.status_code == 422, empty.text


async def test_没做的kind明确说未实现而不是假装成功(client: AsyncClient, login: Login) -> None:
    """PG 源能登记（§9 的驱动映射已备好），但探测还没写。

    沉默的 200 ok:true 比 501 危险得多：用户会以为"连上了"，然后 007 的同步才发现不行。
    """
    acct = await login(username="kate3", role="member")
    resp = await client.post(
        "/api/datasources",
        json={
            **DB,
            "name": "pg-source",
            "kind": "postgres",
            "port": 5432,
            "catalog_name": "warehouse",
            "connect_password": "p-1",
        },
        headers=acct.headers,
    )
    assert resp.status_code == 201, resp.text
    ds_id = resp.json()["id"]

    tested = await client.post(f"/api/datasources/{ds_id}/test", json={}, headers=acct.headers)
    assert tested.status_code == 501, tested.text
    assert tested.json()["error"]["code"] == "not_implemented"
