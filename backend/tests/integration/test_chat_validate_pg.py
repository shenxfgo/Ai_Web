"""`POST /api/chat/validate`：第一层守卫的第一个 HTTP 入口（工单 039，roadmap §P5 验收 2）。

靶选在**真 ASGI 应用**上（`client` 夹具走 `httpx.AsyncClient(transport=ASGITransport(app))`，
不是直接调函数）：这一片要证的恰好是"守卫能不能被 HTTP 调到"——直接调 `sql_guard.check()`
的用例 003 起就有了，再加一条只是重复；而"路由挂上了、依赖接对了、信封长对了"这三件事
只有从 HTTP 那一头才看得见。

**一次都不连源库**：所有用例只动元数据库（PG）。这既是它的边界也是它的反证——
`test_validate一次都不执行也不会有人连源库` 把那两把刀都钉住（执行入口被打桩、
源库引擎装配点被换成"敢来就抛"）。

期望值口径（都不从实现反推）：
- 钳位后的 `sql_final` 来自**我插进去的那一行** `data_sources.row_limit=7`
  （`resolve_row_limit` 的 L4 覆盖 L3，所以它与 `.env` 里的全局键无关），
  `LIMIT 8` 是 `max_rows+1` 的手算；
- 拒的那几档断的是 **状态码 + `error.code` + `error.detail.rule_id`** 三格，
  不错误息文本（011 那条"文本会变，档不会"）；
- `rule_id` 的合法值来自 `GuardRuleId` 那枚封闭词表（035），这里逐条写死字面。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401
from app.core.security import encrypt_secret
from app.models.chat import ChatMessage, ChatSession
from app.models.datasource import DataSource
from app.models.meta import MetaTable
from app.models.user import User
from app.services import source_manager
from app.services.nl2sql import executor
from tests.integration.test_kb_cards_pg import _cards, _column, _table

pytestmark = pytest.mark.pg

_ENDPOINT = "/api/chat/validate"


async def _source(
    session: AsyncSession,
    owner_id: int,
    *,
    name: str = "demo-mysql",
    row_limit: int | None = 7,
    allow_global: bool = False,
) -> DataSource:
    """一条 MySQL 源 + `row_limit=7`。

    为什么把 `row_limit` 钉成 7：钳位后的 `sql_final` 是本片唯一一格里"实现算出来的字符串"，
    留成全局默认 1000 的话，`.env` 一改这条用例就红，而红的原因是配置不是契约。
    """
    src = DataSource(
        name=name,
        kind="mysql",
        host="127.0.0.1",
        port=3306,
        connect_user="aiweb_ro",
        secret_enc=encrypt_secret("x"),
        server_version="5.7.44-log",
        allow_global_access=allow_global,
        created_by=owner_id,
        row_limit=row_limit,
    )
    session.add(src)
    await session.flush()
    return src


async def _meta_table(session: AsyncSession, src: DataSource, name: str, **kw: object) -> MetaTable:
    """在 `ai_web_demo` 库里摆一行 `meta_table`（同 `_table`，只是不要求卡片）。"""
    tbl = await _table(session, src, name, **kw)
    session.add_all([_column(tbl, 1, "id"), _column(tbl, 2, "amount", comment_zh="订单金额")])
    await session.flush()
    return tbl


async def _post(client, headers: dict[str, str] | None, payload: dict[str, object]):
    if headers is None:
        return await client.post(_ENDPOINT, json=payload)
    return await client.post(_ENDPOINT, json=payload, headers=headers)


async def test_多语句被拒时守卫那一档从HTTP那一头看得见(
    client, login, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """验收 2 的字面：`SELECT 1; DROP TABLE users` → 400 `sql_guard_rejected` + `multi_statement`。

    这一条就是本片存在的理由：这具守卫此前只能被 `pipeline.ask()` 调到，"我的 SQL 为什么被拒"
    要么问模型、要么跑一次完整问数。
    """
    account = await login(username="Owner1")
    async with session_factory() as session:
        src = await _source(session, account.user_id)
        await session.commit()
        ds_id = src.id

    resp = await _post(
        client, account.headers, {"sql": "SELECT 1; DROP TABLE users", "datasource_id": ds_id}
    )

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"]["code"] == "sql_guard_rejected"
    # `rule_id` 住在 detail 里，不是顶层（safety §8.4 末的形状订正：顶层那对键是简写）
    assert body["error"]["detail"]["rule_id"] == "multi_statement"
    # message 也必须在：这一发的价值就是"不用问模型就知道错在哪"
    assert body["error"]["message"], body
    assert body["error"]["detail"]["message"], body


async def test_合法单句回钳位后的sql_final(client, login, session_factory) -> None:
    """`SELECT 1` 无表是合法只读查询（守卫 §1.2 的口径），200 的那一格里 SQL 已经被钳过。

    `LIMIT 8` = 我插的那一行 `row_limit=7` 加一（`sql_guard` 的缺省探针钳位），
    不是全局配置那 1000。
    """
    account = await login(username="Owner2")
    async with session_factory() as session:
        src = await _source(session, account.user_id)
        await session.commit()
        ds_id = src.id

    resp = await _post(client, account.headers, {"sql": "SELECT 1", "datasource_id": ds_id})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["sql_final"] == "SELECT 1 LIMIT 8", body["sql_final"]
    assert body["tables"] == []


async def test_表不属于该源时拒在table_not_allowed(client, login, session_factory) -> None:
    """白名单来自这个源的 `meta_table`——它不属于这个源，就是拒，而不是"看不见"。"""
    account = await login(username="Owner3")
    async with session_factory() as session:
        src = await _source(session, account.user_id)
        await _meta_table(session, src, "order_main")
        await session.commit()
        ds_id = src.id

    resp = await _post(
        client,
        account.headers,
        {"sql": "SELECT id FROM other_place.ledger", "datasource_id": ds_id},
    )

    assert resp.status_code == 400, resp.text
    detail = resp.json()["error"]["detail"]
    assert detail["rule_id"] == "table_not_allowed"


async def test_短表名一律拒因为default_schema是空串(client, login, session_factory) -> None:
    """工单八档之外补的一格，因为它决定这个端点**怎么用**：`SELECT id FROM order_main` 必拒。

    `default_schema=""` 是从 `pipeline.py` 那一句原样抄来的（口径"复用不复制"），不是本片新定的
    宽松度。守卫在空 `default_schema` 下补不出 `db` 那一格，短名落成的三元组是 `..order_main`，
    白名单里却是 `ai_web_demo.order_main`——于是**这张表明明属于这个源、也没陈旧**，照样拒。

    这一条单独摆而不是并进 ③：③ 测的是"不属于该源的表要拒"，这里测的是"属于该源的表也要写全名"。
    两者都落 `table_not_allowed`，混在一条用例里就分不出是哪一档漂了。
    ask 那一路看不到这一档，是因为 prompt 给模型的就是全名；validate 的直接用户会一头撞上，
    所以 safety §8.4 那一行要把它写成契约而不是让人猜。
    """
    account = await login(username="Owner3b")
    async with session_factory() as session:
        src = await _source(session, account.user_id)
        await _meta_table(session, src, "order_main")
        await session.commit()
        ds_id = src.id

    short = await _post(
        client, account.headers, {"sql": "SELECT id FROM order_main", "datasource_id": ds_id}
    )
    assert short.status_code == 400, short.text
    assert short.json()["error"]["detail"]["rule_id"] == "table_not_allowed"
    # 同一张表、同一次请求，只差那一段 schema 前缀——这才是"短名"这一档而不是"陌生表"那一档
    full = await _post(
        client,
        account.headers,
        {"sql": "SELECT id FROM ai_web_demo.order_main", "datasource_id": ds_id},
    )
    assert full.status_code == 200, full.text


async def test_陈旧表不进白名单(client, login, session_factory) -> None:
    """`is_stale=true` 的表源库里已没实体，卡片也不给它建（`required_card_count_stmt` 那把尺）。

    这一格防的是集合口径漂移：validate 的白名单只要把陈旧表收进来，同一件 SQL 就会
    validate 放行、ask 拒——而"两条路同一个判定"是本片的全部价值。

    **表名必须写全 `schema.table`**：`default_schema=""` 是 ask 那一路的原样口径（本片不放宽），
    而短表名在守卫里补不出 db，`ref.db` 落空串、白名单里是 `ai_web_demo.t_old_backup`，
    **无论陈旧与否都会拒**——那样这条用例只证明了"短名被拒"，什么都没钉住。写全名之后，
    两张表唯一的差别就只剩 `is_stale` 那一列。
    """
    account = await login(username="Owner4")
    async with session_factory() as session:
        src = await _source(session, account.user_id)
        await _meta_table(session, src, "order_main")
        await _meta_table(session, src, "t_old_backup", is_stale=True)
        await session.commit()
        ds_id = src.id

    resp = await _post(
        client,
        account.headers,
        {"sql": "SELECT id FROM ai_web_demo.t_old_backup", "datasource_id": ds_id},
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["detail"]["rule_id"] == "table_not_allowed"
    # 同一条 SQL 换成没陈旧的那张表必须放行——否则这条用例只证明了"全都拒"
    ok = await _post(
        client,
        account.headers,
        {"sql": "SELECT id FROM ai_web_demo.order_main", "datasource_id": ds_id},
    )
    assert ok.status_code == 200, ok.text


async def test_没有令牌是401而不是FastAPI那把裸403(client, login, session_factory) -> None:
    """`HTTPBearer(auto_error=False)` 的那条注释（`deps.py:56-57`）在端点上的落点。

    源是**真建好的**：拿一个不存在的 id 也回 401，但那条用例什么也没证明——它可能只是
    碰巧在鉴权之前就失败了。建好源再摘掉令牌，401 才是"鉴权这一档走在查库之前"的证据。
    """
    account = await login(username="OwnerNoToken")
    async with session_factory() as session:
        src = await _source(session, account.user_id)
        await session.commit()
        ds_id = src.id

    resp = await _post(client, None, {"sql": "SELECT 1", "datasource_id": ds_id})

    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "unauthorized"


async def test_有令牌但没有该源权限是403(client, login, session_factory) -> None:
    """档位 = 登录态 + 该源 read 权（工单 039 格子 ③），判权用的是 `get_authorized` 那把刀。"""
    owner = await login(username="Owner5")
    stranger = await login(username="Stranger5")
    async with session_factory() as session:
        src = await _source(session, owner.user_id)
        await session.commit()
        ds_id = src.id

    resp = await _post(
        client,
        stranger.headers,
        {"sql": "SELECT 1", "datasource_id": ds_id},
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "forbidden"
    # 反证这一档不是"表不在白名单"：owner 自己发同一条就过
    assert (
        await _post(client, owner.headers, {"sql": "SELECT 1", "datasource_id": ds_id})
    ).status_code == 200


async def test_软删过的源是404而不是403(client, login, session_factory) -> None:
    """`get_authorized:75-76` 已定的档：回 403 等于替调用者确认"这个 id 下曾有过一个源"。"""
    account = await login(username="Owner6")
    async with session_factory() as session:
        src = await _source(session, account.user_id)
        src.deleted_at = dt.datetime.now(dt.UTC)
        await session.commit()
        ds_id = src.id

    resp = await _post(client, account.headers, {"sql": "SELECT 1", "datasource_id": ds_id})

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "not_found"


async def test_空语句拒在empty_statement而不是422(client, login, session_factory) -> None:
    """空/超长的主人是守卫 `_preprocess`，不是 pydantic——所以是 400 `sql_guard_rejected`。

    与"请求体缺字段"那档（422 `invalid_request`，`RequestValidationError` 处理器）分得开，
    这一格才有意义：同一个"你的 SQL 不行"，两个主人。
    """
    account = await login(username="Owner7")
    async with session_factory() as session:
        src = await _source(session, account.user_id)
        await session.commit()
        ds_id = src.id

    resp = await _post(client, account.headers, {"sql": "   ", "datasource_id": ds_id})

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "sql_guard_rejected"
    assert resp.json()["error"]["detail"]["rule_id"] == "empty_statement"


async def test_超过长度上限拒在too_large(client, login, session_factory) -> None:
    """上限 8000 是 `sql_guard._MAX_SQL_LEN` 的字面（§1.2），这里从 7999 的字面量之外摆一条。"""
    account = await login(username="Owner8")
    async with session_factory() as session:
        src = await _source(session, account.user_id)
        await session.commit()
        ds_id = src.id

    # `SELECT 1` + 一段 9000 字符的注释：长度超上限，形状本身还是合法只读
    resp = await _post(
        client,
        account.headers,
        {"sql": "SELECT 1 /* " + "a" * 9000 + " */", "datasource_id": ds_id},
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["detail"]["rule_id"] == "too_large"


async def test_请求体缺datasource_id是422而不是跳过表白名单(client, login) -> None:
    """口径第 2 条：`datasource_id` 必带。缺它只有两种坏选择（跳过白名单 / 全拒），都不许。

    出 422 而不是 400：这一档的主人是 pydantic（`extra="forbid"` 同一条），不是守卫。
    """
    account = await login(username="Owner9")

    resp = await _post(client, account.headers, {"sql": "SELECT 1"})

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "invalid_request"
    # 不回整条 `exc.errors()`：那个 `input` 装的是整个请求体（errors.py 里 422 处理器的理由）
    assert {"loc", "type", "msg"} == set(resp.json()["error"]["detail"][0])

    extra = await _post(
        client, account.headers, {"sql": "SELECT 1", "datasource_id": 1, "session_id": 9}
    )
    assert extra.status_code == 422, extra.text


async def test_validate一次都不执行也不会有人连源库(
    client, login, session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反证（验收 3）：给 validate 摘掉桩之后它会不会红——答案是会，这一条就是那把桩。

    两把刀分开摆，因为"没执行"与"没连库"是两件事：
    ① `executor.execute_readonly` 被打桩计数（`pipeline` 是 `from ... import` 直接绑的，
       所以两处都换掉，否则偷渡的执行路径可以绕过其中一处）；
    ② 源库引擎的**唯一装配点** `source_manager.create_source_engine` 换成"敢来就抛"——
       dry-run（037）也走这条路，所以这一把同时证明"只跑第一层"（口径第 3 条）。
       它同样要钉两处：`executor` 第 31 行是 `from app.services.source_manager import
       create_source_engine`，只换主人那一处等于没换——这一层与 ① 是同一条陷阱，
       所以它自己也必须被这条用例反证一次（把 ② 的 executor 那一处去掉，这条用例就红）。
    """
    calls: list[str] = []

    async def _spy(*args: object, **kwargs: object) -> object:
        calls.append("execute_readonly")
        raise AssertionError("validate 不许执行 SQL")

    def _no_engine(*args: object, **kwargs: object) -> object:
        raise AssertionError("validate 不许连源库")

    monkeypatch.setattr(executor, "execute_readonly", _spy)
    monkeypatch.setattr("app.services.nl2sql.pipeline.execute_readonly", _spy)
    monkeypatch.setattr(source_manager, "create_source_engine", _no_engine)
    monkeypatch.setattr("app.services.nl2sql.executor.create_source_engine", _no_engine)

    account = await login(username="Owner10")
    async with session_factory() as session:
        src = await _source(session, account.user_id)
        await _meta_table(session, src, "order_main")
        await session.commit()
        ds_id = src.id

    resp = await _post(
        client,
        account.headers,
        {"sql": "SELECT id FROM ai_web_demo.order_main", "datasource_id": ds_id},
    )

    assert resp.status_code == 200, resp.text
    assert calls == []
    # 成功体那一格也得是真放行（不是"没跑到断言"）：`tables` 回的是守卫归一化后的那张全名，
    # 它是本片唯一一处"由 `table_full_name` 反向渲染守卫三元组"的落点，所以钉在这里。
    body = resp.json()
    assert body["tables"] == ["ai_web_demo.order_main"], body
    # 口径第 4 条：不落库、不建会话行——留痕归 P8 的问数面。
    # 打 ORM 而不是裸 SQL：schema 每条用例随机（`pg_migrated` 那套），写死 `public` 会红在
    # 与环境有关的地方。`session_factory` 在每条用例开头清空过这两张表，所以这里的 0 是
    # "这一发请求没写"而不是"库本来就是空的"——反证的对象是端点，不是夹具。
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ChatSession)) == 0
        assert await session.scalar(select(func.count()).select_from(ChatMessage)) == 0


async def test_validate构造的白名单与检索那一路逐枚相等(
    login, session_factory: async_sessionmaker[AsyncSession], active_profile
) -> None:
    """验收 5 的那条附加用例：`allowed` 只能有一份拼法（工单口径"提成纯函数"的钉子）。

    两条路的白名单出生地不同——ask 那一路是 `load_schema_tables` 给的 `full_name`
    （`pipeline.py:301` 再 `QualifiedTable.parse`），validate 这一路直接读 `meta_table`。
    这里**同时跑两边**：同一批表、同一次会话，两个集合必须逐枚相等。
    只断言"validate 自己能拼对"是同义反复——漂移发生在两路之间，就必须在两路之间测。
    """
    from app.services.nl2sql import validate as validate_service
    from app.services.nl2sql.retriever import load_schema_tables
    from app.services.sql_guard import QualifiedTable

    account = await login(username="Owner11")
    async with session_factory() as session:
        src = await _source(session, account.user_id)
        orders = await _meta_table(session, src, "order_main")
        payments = await _meta_table(session, src, "payment_record")
        await _cards(session, src, [orders.id, payments.id])
        await session.commit()
        ds_id = src.id
        uids = [orders.table_uid, payments.table_uid]
        actor = await session.get(User, account.user_id)
        assert actor is not None

        mine = await validate_service.allowed_from_source(session, datasource_id=ds_id)
        # 检索那一路的原样形状：先拼名（`retriever`），再 parse 回三元组（`pipeline`）
        tables = await load_schema_tables(session, actor=actor, table_uids=uids)
        theirs = frozenset(QualifiedTable.parse(t.full_name) for t in tables)

    assert tables, "检索那一路一张表都没补出来，这条用例什么都没比"
    assert mine == theirs
    # 期望值不来自实现：这三枚三元组是按 `meta_table` 里我摆的那两行手算的
    assert mine == frozenset(
        {
            QualifiedTable("", "ai_web_demo", "order_main"),
            QualifiedTable("", "ai_web_demo", "payment_record"),
        }
    )
