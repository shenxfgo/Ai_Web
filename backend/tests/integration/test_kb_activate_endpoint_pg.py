"""`POST /api/kb/profiles/{profile_id}/activate`：上线那道门的**端点半边**（工单 030）。

接缝：真 HTTP → 回响应体，再回读 `kb_index_profile`。状态机与卡片行数那把尺本身的用例在
`test_kb_activate_pg.py`（它们打的是 service 那条缝），这里只验三件只有端点才有的事：
**鉴权档**、**错误码到 HTTP status 的映射**、**响应体的形状**。

期望值口径：
- 鉴权 **admin-only**。依据是 `docs/architecture.md` §7 端点表那一行的"admin"列，不是我的偏好：
  切换基准这件事不挂在任何单个资源上（不像 `/sync/jobs` 判"这个源是不是你登记的"），
  它换的是**全库**的检索基准，所以只能按角色判。工单 030 把这一格列为"待认的一格"并写了
  "默认按 admin-only 落"，本轮按默认落，差异记在交付记录里。
- 200 的响应体是 `{activated, retired}` 两格摘要。`retired` 可为 `null`（第一次上线），
  这一格在 `test_库里本来没有_active_时上线回_retired_为_null` 里单独钉，因为它是
  "响应里不许编造库里没有的东西"那条口径的落点（与 029 的 202 只回两颗 id 同源）。
- 404 / 400 / 403 三档各一条，且**库里的三态一个字都不许动**——拒绝不是半途而废的写。
- 验收 7 的字面是"同一条 activate 连发两次不产生第二行 active"，走的是幂等那格（不是真并发：
  真并发那一格由 partial unique 在库里兜，本轮已在 service 侧探过四种语句形状）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func

from tests.integration.conftest import Account
from tests.integration.test_kb_activate_pg import (
    NEW,
    OLD,
    _active_count,
    _insert,
    _profile,
    _rows,
    _source,
    _table_cards,
)

pytestmark = pytest.mark.pg

Login = Callable[..., Awaitable[Account]]


async def _complete_draft(session_factory: Any) -> dict[str, int]:
    """摆出"旧基准在线上、新档案已建齐"这一整幅图，回两行档案的 id。"""
    async with session_factory() as session:
        ids = await _insert(
            session,
            _profile(OLD, status="active", is_active=True),
            _profile(NEW, status="draft", built_at=func.now()),
        )
        src = await _source(session, "demo-mysql", ("shop_t1", "shop_t2"))
        await _table_cards(
            session, profile_id=ids[OLD], datasource_id=src, names=("shop_t1", "shop_t2")
        )
        await _table_cards(
            session, profile_id=ids[NEW], datasource_id=src, names=("shop_t1", "shop_t2")
        )
        await session.commit()
        return {"new": ids[NEW], "old": ids[OLD], "src": src}


def _url(profile_id: int) -> str:
    return f"/api/kb/profiles/{profile_id}/activate"


async def test_admin_上线一行建齐的_draft_回_200_两格摘要(
    client: AsyncClient, login: Login, session_factory: Any
) -> None:
    """§7 那行的"200 档案行"落成 `{activated, retired}`：切换前后的两套都点名。"""
    ids = await _complete_draft(session_factory)
    admin = await login(username="Admin-030", role="admin")

    resp = await client.post(_url(ids["new"]), headers=admin.headers)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "activated": {
            "id": ids["new"],
            "name": NEW,
            "model": "bge-large-zh-v1.5",
            "dimensions": 1024,
            "card_template_version": 1,
        },
        "retired": {
            "id": ids["old"],
            "name": OLD,
            "model": "text-embedding-3-small",
            "dimensions": 1536,
            "card_template_version": 1,
        },
    }
    async with session_factory() as session:
        assert await _active_count(session) == 1
        assert await _rows(session) == [
            (NEW, "active", True),
            (OLD, "retired", False),
        ]


async def test_member_上线被拒_403_而且库里三态一个字没动(
    client: AsyncClient, login: Login, session_factory: Any
) -> None:
    """admin-only 这一档要有正向反证：登录了、但不是管理员。

    403 而不是 401：登录态是好的，缺的是权限（与仓库 `Forbidden` 那档同一条理由）。
    断"三态没动"是因为鉴权必须发生在任何写之前——先切了再 403 是最坏的那种"半件坏事"。
    """
    ids = await _complete_draft(session_factory)
    member = await login(username="Member-030", role="member")
    async with session_factory() as session:
        before = await _rows(session)

    resp = await client.post(_url(ids["new"]), headers=member.headers)

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "forbidden"
    async with session_factory() as session:
        assert await _rows(session) == before
        assert await _active_count(session) == 1


async def test_没登录_401_不到那道门(
    client: AsyncClient, login: Login, session_factory: Any
) -> None:
    """与全站同一条：缺令牌是 401（`_bearer` 关掉了 FastAPI 自己的 403）。

    这一条与上一条合起来才说得出"鉴权那一档是角色而不是登录态"。
    """
    ids = await _complete_draft(session_factory)
    await login(username="Admin-030b", role="admin")

    resp = await client.post(_url(ids["new"]))

    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "unauthorized"


async def test_不存在的档案回_404(client: AsyncClient, login: Login) -> None:
    """路径参数上的 id 不做存在性预检：service 那一格已经给了 404，这里只验映射。"""
    admin = await login(username="Admin-030c", role="admin")

    resp = await client.post(_url(999999), headers=admin.headers)

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "not_found"


async def test_没跑完的_draft_回_400_profile_not_built(
    client: AsyncClient, login: Login, session_factory: Any
) -> None:
    await _complete_draft(session_factory)
    admin = await login(username="Admin-030d", role="admin")
    async with session_factory() as session:
        pending = await _insert(session, _profile("pending@1536@tplv1", status="draft"))
        await session.commit()
        pending_id = pending["pending@1536@tplv1"]

    resp = await client.post(_url(pending_id), headers=admin.headers)

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "profile_not_built"


async def test_行数不齐的_draft_回_400_并且_body_点得出那个源(
    client: AsyncClient, login: Login, session_factory: Any
) -> None:
    """验收 5 的端点那一半：400 的 message 里要看得见源名。

    尺本身在 `test_kb_activate_pg.py` 验，这里验的是它抬起来之后**能不能到达人**——
    错误 envelope 把 message/detail 摊平之前，"点名的源"很容易在映射那一层被吞成一句"请求无效"。
    """
    await _complete_draft(session_factory)
    admin = await login(username="Admin-030e", role="admin")
    async with session_factory() as session:
        partial_name = "partial@1536@tplv1"
        partial = await _insert(
            session, _profile(partial_name, status="draft", built_at=func.now())
        )
        # 第二个源只是登记过、从没同步进生效档案：它不在比的那批源里（决策 9 的键集是
        # "当前 active 有卡片的源集合"），所以要被点名的只有第一个源，而且是一整源缺席。
        await _source(session, "shop_wh", ("shop_stock",))
        await session.commit()
        partial_id = partial[partial_name]

    resp = await client.post(_url(partial_id), headers=admin.headers)

    assert resp.status_code == 400, resp.text
    body = resp.json()["error"]
    assert body["code"] == "profile_cards_incomplete"
    text = f"{body['message']}{body['detail']}"
    assert "demo-mysql" in text, f"要点名那个正在服务检索的源：{text}"
    assert "shop_wh" not in text, f"没进过检索面的源不该被一起点名：{text}"
    assert "应有 2" in text and "实有 0" in text, f"两个数都要给：{text}"


async def test_库里本来没有_active_时上线回_retired_为_null(
    client: AsyncClient, login: Login, session_factory: Any
) -> None:
    """第一次上线（0008 归置之后库里那行 active 本来就是空的）不许编出第二格。"""
    async with session_factory() as session:
        ids = await _insert(session, _profile(NEW, status="draft", built_at=func.now()))
        await session.commit()
        new_id = ids[NEW]
    admin = await login(username="Admin-030f", role="admin")

    resp = await client.post(_url(new_id), headers=admin.headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["retired"] is None


async def test_同一条_activate_连发两次不产生第二行_active(
    client: AsyncClient, login: Login, session_factory: Any
) -> None:
    """工单 030 验收 7 的字面：重放那条请求，幂等那格 + partial unique 都没被绕。"""
    ids = await _complete_draft(session_factory)
    admin = await login(username="Admin-030g", role="admin")

    first = await client.post(_url(ids["new"]), headers=admin.headers)
    second = await client.post(_url(ids["new"]), headers=admin.headers)

    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert second.json() == {
        "activated": first.json()["activated"],
        "retired": None,
    }, "重放时旧 active 已经是自己，retired 只能是 null"
    async with session_factory() as session:
        assert await _active_count(session) == 1
        assert await _rows(session) == [(NEW, "active", True), (OLD, "retired", False)]
