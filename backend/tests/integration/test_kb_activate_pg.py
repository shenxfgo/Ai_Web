"""`activate_profile()`：换基准（工单 030 / ADR-0013 / spec 故事 5、6、19）。

接缝：`kb_service.activate_profile(session, *, profile_id)` 收一个 session 与要上线的那行档案 id，
回"切换前后的两行摘要"。它是 `kb_index_profile.status` 在应用侧**唯一**的写入点——027 关掉了同步
链路自己长基准那条后门，029 把出生入口收进 reindex，这一片把"上线"这一步也收进同一个门。

期望值口径（文档字面 + 真库实测，不是从实现反推）：
- ADR-0013 的字面是"激活 = **一条** `UPDATE … CASE`，三支各改一行
  （`WHEN id=:new THEN 'active'` / `WHEN status='active' THEN 'retired'` / `ELSE status`）"。
  **这一条在真库上被本轮证伪了**（见 `test_draft_行物理上排在_active_之前时切换仍然成功`）：
  partial unique 的键是表达式，PG 在语句内部逐行写索引项，行序反过来就撞约束。实物是同一个事务里
  固定顺序的两条（先降级旧 active、再升级新那行），ADR 要的那件事（不引入第二套真相、外部读者只
  看见切换前或切换后）没丢。不引入"唯一索引 + 时间戳两真相"那半句仍然照 ADR 生效。
- 工单 030 验收 1：切换前后 `count(*) WHERE status='active'` **恒为 1**（只断返回码不算数），
  且旧那行落在 `retired`。026 当时还要求 `is_active` 跟着镜像一次，那一列在 031 的收缩迁移
  （0009）里已经删掉，所以这一格现在只由 `status` 一列说话。
- 工单 030 已定口径：draft 且 `built_at IS NULL` → 400；已 active 的自己激活自己 → 幂等，
  不产生第二行、也不把任何一行改成 retired。
- spec 故事 6 的后半句：`retired` 可以再激活，**而且这一格不许附加 `built_at` 条件**——
  0008 归置出来的历史行是 retired 且 `built_at IS NULL`，附加条件等于取消回滚。
- 工单 030 验收 2 的"激活后 `/kb/search` 读的向量列跟着维度变"要等 031 的读侧才验得全，本片按
  工单给的降级口径验"库里两根向量列的内容各自未变"（`test_上线与回滚都不改两根向量列里的内容`），
  降级理由写在交付记录里。

前置分两格，各有一段用例：`built_at` 那一格只看档案自己那一行；决策 9 的"整档案卡片行数齐"
（验收 2/3/5）要真 `meta_table` 与真 `kb_card`，它住在本文件后半截的那把尺里。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy import Text, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.security import encrypt_secret, hash_password
from app.models.datasource import DataSource
from app.models.kb import KbCard, KbIndexProfile
from app.models.meta import MetaDatabase, MetaTable
from app.models.user import User
from app.services.kb_service import activate_profile

pytestmark = pytest.mark.pg


def _profile(name: str, **kw: Any) -> KbIndexProfile:
    """按 §2.6 的拼法（`model@dim@tplv`）从名字里**拆出**那三格。

    不写死 `model="text-embedding-3-small", dimensions=1536`：那会让 `NEW` 那行的名字说是 1024、
    列里存的是 1536。名字与列不一致时，"上线 1024 档案"那类断言就验在了一个假档案上——
    而 031 的读侧正是拿 `dimensions` 这一格决定读哪张向量列的。
    """
    model, dimensions, template_version = name.split("@")
    return KbIndexProfile(
        name=name,
        provider="openai_compatible",
        model=model,
        dimensions=int(dimensions),
        card_template_version=int(template_version.removeprefix("tplv")),
        **kw,
    )


async def _rows(session: AsyncSession) -> list[tuple[str, str]]:
    """库里每一行档案的 `(name, status)`，按 name 排序（顺序不参与判定）。

    这里曾经还带第三格 `is_active`，用来钉 026 那条"双写"口径。0009 把那一列从库里删了以后
    它就没有可取的东西了——尺子只剩一把，本来的两列形状也随之出清。
    """
    rows = (
        await session.execute(
            select(KbIndexProfile.name, KbIndexProfile.status).order_by(KbIndexProfile.name)
        )
    ).all()
    return [(str(r[0]), str(r[1])) for r in rows]


async def _active_count(session: AsyncSession) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(KbIndexProfile.__table__)
                .where(KbIndexProfile.status == "active")
            )
        ).scalar_one()
    )


async def _insert(session: AsyncSession, *profiles: KbIndexProfile) -> dict[str, int]:
    for profile in profiles:
        session.add(profile)
    await session.flush()
    return {str(p.name): int(p.id) for p in profiles}


async def test_激活一行_built_at_齐了的_draft_旧_active_同刻变_retired(
    session_factory: Any,
) -> None:
    """工单 030 目标那句"一条语句换基准"——实物是两条，见文件头的口径。"""
    async with session_factory() as session:
        ids = await _insert(
            session,
            _profile("text-embedding-3-small@1536@tplv1", status="active"),
            _profile("bge-large-zh-v1.5@1024@tplv1", status="draft", built_at=func.now()),
        )
        await session.commit()
        draft_id = ids["bge-large-zh-v1.5@1024@tplv1"]

        outcome = await activate_profile(session, profile_id=draft_id)

        assert await _active_count(session) == 1, "切换以后 active 计数必须恰好是 1"
        assert await _rows(session) == [
            ("bge-large-zh-v1.5@1024@tplv1", "active"),
            ("text-embedding-3-small@1536@tplv1", "retired"),
        ]
        assert outcome.activated.id == draft_id
        assert outcome.retired is not None
        assert outcome.retired.name == "text-embedding-3-small@1536@tplv1"


async def test_draft_行物理上排在_active_之前时切换仍然成功(session_factory: Any) -> None:
    """这一条就是"一条语句"那个字面的反证，它先是红的。

    heap 扫描按插入序走：先插的那行 tid 小、先被更新。上一行用例是"旧 active 先插"，那种顺序下
    旧行先被改出 partial unique 的命中范围，新行后进不撞；这一行故意反过来——draft 先插、要变成
    active 的那行先被更新，此时旧 active 的索引项**还在**。真库实测（本轮在 `aiweb_test` 里另起
    一张同形状表探过四种形状）：单条 CASE 在 draft 先插时 `UniqueViolationError:
    ux_kb_index_profile_active`，在旧 active 先插时成功；两条（先降级再升级）两种行序都成功，
    两条反序（先升级）撞。所以单条语句的生死取决于行序，而它红的那一格正是 ADR 字面靠不住的那格。
    """
    async with session_factory() as session:
        ids = await _insert(
            session,
            _profile("bge-large-zh-v1.5@1024@tplv1", status="draft", built_at=func.now()),
            _profile("text-embedding-3-small@1536@tplv1", status="active"),
        )
        await session.commit()

        await activate_profile(session, profile_id=ids["bge-large-zh-v1.5@1024@tplv1"])

        assert await _active_count(session) == 1
        assert await _rows(session) == [
            ("bge-large-zh-v1.5@1024@tplv1", "active"),
            ("text-embedding-3-small@1536@tplv1", "retired"),
        ]


async def test_retired_可以再激活回去而且库里仍是恰好一行_active(session_factory: Any) -> None:
    """roadmap P4 验收 5 的"可回滚"那一格（工单 030 验收 6）。

    这里回滚到的那行 `built_at` 是 NULL——它插进来的时候就是 active 且没盖钟，正是 0008 归置
    出来的历史行形状（迁移只写 status，不碰 built_at）。回滚这一格不许附加 built_at 条件：
    第一次上线新档案之后立刻想退回去，退的那套往往就是这套从没跑过作业的基准。
    """
    async with session_factory() as session:
        ids = await _insert(
            session,
            _profile("text-embedding-3-small@1536@tplv1", status="active"),
            _profile("bge-large-zh-v1.5@1024@tplv1", status="draft", built_at=func.now()),
        )
        await session.commit()
        old_id = ids["text-embedding-3-small@1536@tplv1"]

        await activate_profile(session, profile_id=ids["bge-large-zh-v1.5@1024@tplv1"])
        back = await activate_profile(session, profile_id=old_id)

        assert await _active_count(session) == 1
        assert back.activated.id == old_id
        assert back.retired is not None and back.retired.name == "bge-large-zh-v1.5@1024@tplv1"
        # 回滚没有顺手给那行盖钟：`built_at` 是"作业跑完"的凭证，不是"被激活过"的凭证。
        assert (
            await session.scalar(select(KbIndexProfile.built_at).where(KbIndexProfile.id == old_id))
            is None
        )
        assert await _rows(session) == [
            ("bge-large-zh-v1.5@1024@tplv1", "retired"),
            ("text-embedding-3-small@1536@tplv1", "active"),
        ]


async def test_built_at_为空时拒绝激活而且三态一个字没动(session_factory: Any) -> None:
    """工单 030 验收 4：作业没跑完的 draft 不许上线（spec 故事 6）。"""
    async with session_factory() as session:
        ids = await _insert(
            session,
            _profile("text-embedding-3-small@1536@tplv1", status="active"),
            _profile("bge-large-zh-v1.5@1024@tplv1", status="draft"),
        )
        await session.commit()
        before = await _rows(session)

        with pytest.raises(AppError) as excinfo:
            await activate_profile(session, profile_id=ids["bge-large-zh-v1.5@1024@tplv1"])

        assert excinfo.value.code == "profile_not_built"
        assert excinfo.value.status_code == 400
        assert await _rows(session) == before, "拒绝之外不许留下任何改动"
        assert await _active_count(session) == 1


async def test_激活已经_active_的那一行是幂等它没有改变任何东西(session_factory: Any) -> None:
    """工单 030 已定口径：幂等 200，不是 400。"""
    async with session_factory() as session:
        ids = await _insert(
            session,
            _profile("text-embedding-3-small@1536@tplv1", status="active"),
        )
        await session.commit()
        active_id = ids["text-embedding-3-small@1536@tplv1"]

        outcome = await activate_profile(session, profile_id=active_id)

        assert outcome.retired is None, "自己激活自己不该把谁改成 retired"
        assert outcome.activated.id == active_id
        assert await _rows(session) == [("text-embedding-3-small@1536@tplv1", "active")]


async def test_库里一行_active_都没有时激活把_draft_顶上去(session_factory: Any) -> None:
    """第一次上线（P4 之前库里只有回填出来的 retired/没有 active）走的是同一条语句。"""
    async with session_factory() as session:
        ids = await _insert(
            session,
            _profile("bge-large-zh-v1.5@1024@tplv1", status="draft", built_at=func.now()),
        )
        await session.commit()

        outcome = await activate_profile(session, profile_id=ids["bge-large-zh-v1.5@1024@tplv1"])

        assert await _active_count(session) == 1
        assert outcome.retired is None
        assert await _rows(session) == [("bge-large-zh-v1.5@1024@tplv1", "active")]


async def test_不存在的_profile_id_回_404_而且库里没有任何变化(session_factory: Any) -> None:
    """工单没写这一格，但 UPDATE 命中 0 行不能报成功。

    404 而不是 400，且 code 用仓库既有那只通用的 `not_found`（`sync.py:135` 的"没有这个同步作业"、
    `datasource_service` 的"数据源 N 不存在"同一个档）：400 的主语是"请求本身能成立但不该受理"
    （`built_at` 为空、行数不齐才是那一档）。不为这一格新造 code 是因为它不携带新信息——
    030 的四种失败里只有"没建完"和"行数不齐"需要人去做不同的事。
    """
    async with session_factory() as session:
        ids = await _insert(
            session,
            _profile("text-embedding-3-small@1536@tplv1", status="active"),
        )
        await session.commit()
        before = await _rows(session)

        with pytest.raises(AppError) as excinfo:
            await activate_profile(
                session, profile_id=int(ids["text-embedding-3-small@1536@tplv1"]) + 99999
            )

        assert excinfo.value.code == "not_found"
        assert excinfo.value.status_code == 404
        assert await _rows(session) == before


# ================================================== 卡片行数那道前置（验收 2/3/5 / 决策 9）
#
# 这把尺的三格都来自工单 030 的"已定口径"，不是从实现反推：
# - 应建 = 该源 `meta_table` 里 `is_stale = false` 的表数（陈旧表源库已无实体，本轮同步不给它建卡，
#   `sync_service.py` 那句"只给本轮真的落成了的表建卡"是同一件事）。
# - 实有 = 该档案下 `kind IN ('table','table_columns')` 且未软删的卡**按表去重**的行数——
#   一张宽表会切成主卡 + 多张 `table_columns` 切片（§6 的分块），拿卡片行数比就会绿在
#   "只重建了一个源"的档案上。
# - 比较的源集合 = **当前 active 档案有卡片的源集合**（整档案激活）：全局档案切过去以后，
#   没重建的那个源会在向量路里静默缺席，那是 §5.2 (1) 列的最坏失效形状。
# `term` 卡（人工术语）不挂在表上（`kb_card.table_id` 可空），所以它既不该算实有、也不该进尺。

OLD = "text-embedding-3-small@1536@tplv1"
NEW = "bge-large-zh-v1.5@1024@tplv1"
# `test_同一张表的切片卡不重复计数` 那一族共用的表集合：一张宽表 + 两张窄表。
WIDE_AND_NARROW = ("wide_t", "narrow_a", "narrow_b")


async def _source(
    session: AsyncSession, name: str, tables: Sequence[str], *, stale: Sequence[str] = ()
) -> int:
    """造一个源 + `ai_web_demo` 那一库 + 若干表，回它的 id。

    `stale` 里那几个名字必须是 `tables` 的子集，落 `is_stale=true`——它们是"源库已经没了"的那些表。
    `secret_enc` 走真加密（同 `test_kb_cards_pg.py` 的同名夹具）：这一列是"永远不该出现在响应里"
    的那一列，明文字面进库就等于给 detail 泄口令留一条路。
    """
    user = User(username=f"owner-{name}", password_hash=hash_password("x"), role="admin")
    session.add(user)
    await session.flush()
    src = DataSource(
        name=name,
        kind="mysql",
        host="127.0.0.1",
        port=3306,
        connect_user="aiweb_ro",
        secret_enc=encrypt_secret("x"),
        created_by=int(user.id),
    )
    session.add(src)
    await session.flush()
    db_row = MetaDatabase(datasource_id=int(src.id), catalog_name="", schema_name="ai_web_demo")
    session.add(db_row)
    await session.flush()
    for table in tables:
        session.add(
            MetaTable(
                datasource_id=int(src.id),
                database_id=int(db_row.id),
                catalog_name="",
                schema_name="ai_web_demo",
                table_name=table,
                table_type="BASE TABLE",
                is_stale=table in stale,
            )
        )
    await session.flush()
    return int(src.id)


async def _table_ids(session: AsyncSession, datasource_id: int) -> dict[str, int]:
    rows = (
        await session.execute(
            select(MetaTable.id, MetaTable.table_name).where(
                MetaTable.datasource_id == datasource_id
            )
        )
    ).all()
    return {str(r[1]): int(r[0]) for r in rows}


async def _card(
    session: AsyncSession,
    *,
    profile_id: int,
    datasource_id: int,
    table: str | None,
    kind: str = "table",
    seq: int = 0,
    deleted: bool = False,
    tables: dict[str, int] | None = None,
) -> None:
    """往某套档案下插一张卡；`table=None` 插的是 `term` 卡（`table_id` 可空那一格）。

    `doc_uid` 这里是**用例自己拼的 md5**，不调 `kb_service.card_doc_uid()`：那是被测链路上的函数，
    拿它的返回值当期望值就等于允许它少拌一段（比如漏了 `seq`）而用例照样绿——而"多段卡叠成
    同一个 doc_uid"恰好是这张表唯一约束要拦的事。拼法本身（拌齐 profile/源/表/kind/seq）只要保证
    **每行互不相同**就够用，唯一约束守的是"别越建越多"，不是某个具体哈希。
    """
    table_id = None if table is None else (tables or {})[table]
    digest = hashlib.md5(
        f"030|{profile_id}|{datasource_id}|{table_id}|{kind}|{seq}".encode()
    ).hexdigest()
    session.add(
        KbCard(
            datasource_id=datasource_id,
            kind=kind,
            table_id=table_id,
            seq=seq,
            doc_uid=digest,
            index_profile_id=profile_id,
            title="t",
            text_md="t",
            search_text="t",
            token_count=1,
            deleted_at=func.now() if deleted else None,
        )
    )
    await session.flush()


async def _table_cards(
    session: AsyncSession, *, profile_id: int, datasource_id: int, names: Sequence[str]
) -> dict[str, int]:
    """给这些表各插一张主卡，回 `表名 → meta_table.id`（后面补切片卡要用同一个 id）。"""
    tables = await _table_ids(session, datasource_id)
    for name in names:
        await _card(
            session, profile_id=profile_id, datasource_id=datasource_id, table=name, tables=tables
        )
    return tables


async def _card_count(session: AsyncSession, profile_id: int) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(KbCard.__table__)
                .where(KbCard.index_profile_id == profile_id)
            )
        ).scalar_one()
    )


async def test_逐源行数齐的_draft_能上线(session_factory: Any) -> None:
    """验收 2 的通过那一半：一个源、两张表、两边都齐。"""
    async with session_factory() as session:
        ids = await _insert(
            session,
            _profile(OLD, status="active"),
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

        outcome = await activate_profile(session, profile_id=ids[NEW])

        assert outcome.activated.id == ids[NEW]
        assert await _active_count(session) == 1


async def test_少一个源的_draft_被拒_detail_点名那个源与两个数(session_factory: Any) -> None:
    """验收 5：整档案激活——缺的那个源必须点得出**全名**，还得给出应有/实有。

    只回"行数不齐"是不够的：多源场景下人要做的动作是"给 shop_wh 那个源再跑一次 reindex"，
    点名不到源，这条 400 就只是把静默缺席换成了一条要人去查库的报错。
    """
    async with session_factory() as session:
        ids = await _insert(
            session,
            _profile(OLD, status="active"),
            _profile(NEW, status="draft", built_at=func.now()),
        )
        keep = await _source(session, "shop_main", ("shop_order",))
        gone = await _source(session, "shop_wh", ("shop_stock",))
        await _table_cards(session, profile_id=ids[OLD], datasource_id=keep, names=("shop_order",))
        await _table_cards(session, profile_id=ids[OLD], datasource_id=gone, names=("shop_stock",))
        await _table_cards(session, profile_id=ids[NEW], datasource_id=keep, names=("shop_order",))
        await session.commit()
        before = await _rows(session)

        with pytest.raises(AppError) as excinfo:
            await activate_profile(session, profile_id=ids[NEW])

        assert excinfo.value.code == "profile_cards_incomplete"
        assert excinfo.value.status_code == 400
        text = f"{excinfo.value.message} {excinfo.value.detail}"
        assert "shop_wh" in text, f"detail 要点名那个源：{text}"
        assert "shop_main" not in text, "齐的那个源不许被一起点名"
        # "应有 1 / 实有 0"——两个数都在，否则看的人不知道该补几张。
        assert "应有 1" in text and "实有 0" in text, f"detail 要给出应有与实有两个数：{text}"
        assert await _rows(session) == before, "拒绝之外不许留下任何改动"
        assert await _active_count(session) == 1


async def test_同一张表的切片卡不重复计数(session_factory: Any) -> None:
    """尺是**按表去重**的行数，不是卡片行数（工单已定口径那句"一张表可能有多段卡"）。

    形状：宽表 `wide_t` 一张主卡 + 两张 `table_columns` 切片，另两张窄表各一张主卡，而
    `stale_t` 那来源库已经没了。于是应建 = 3、实有 = 3 而卡片行数 = 5。按卡片行数比就会
    在"只重建了 wide_t 一张表（3 段卡）"的档案上放行，而那是同一件事的另一种写法。
    """
    async with session_factory() as session:
        ids = await _insert(
            session,
            _profile(OLD, status="active"),
            _profile(NEW, status="draft", built_at=func.now()),
        )
        src = await _source(
            session, "demo-mysql", ("wide_t", "narrow_a", "narrow_b", "stale_t"), stale=("stale_t",)
        )
        # 旧基准：三张非陈旧表各一张主卡（陈旧那张本轮同步不给它建卡，库里本来就没有它的卡）。
        await _table_cards(session, profile_id=ids[OLD], datasource_id=src, names=WIDE_AND_NARROW)
        # 新档案：wide_t 切成三段、另两张窄表各一段 —— 卡片行数 5，按表去重 3。
        tables = await _table_ids(session, src)
        await _card(session, profile_id=ids[NEW], datasource_id=src, table="wide_t", tables=tables)
        for seq in (1, 2):
            await _card(
                session,
                profile_id=ids[NEW],
                datasource_id=src,
                table="wide_t",
                kind="table_columns",
                seq=seq,
                tables=tables,
            )
        for name in ("narrow_a", "narrow_b"):
            await _card(session, profile_id=ids[NEW], datasource_id=src, table=name, tables=tables)
        await session.commit()

        outcome = await activate_profile(session, profile_id=ids[NEW])

        assert outcome.activated.id == ids[NEW]
        assert await _card_count(session, ids[NEW]) == 5, "前提：卡行数确实是 5（按表去重才是 3）"


async def test_只重建了宽表那一段的_draft_按表去重算缺(session_factory: Any) -> None:
    """上一条的反面：卡片行数够（3 张）而表数不够（1 张）时不许放行。

    这一条才是"按卡片行数比"那个错法的反证——如果实现里漏了 `distinct`，这里回的是 3 ≥ 3 就绿了。
    """
    async with session_factory() as session:
        ids = await _insert(
            session,
            _profile(OLD, status="active"),
            _profile(NEW, status="draft", built_at=func.now()),
        )
        src = await _source(session, "demo-mysql", WIDE_AND_NARROW)
        await _table_cards(session, profile_id=ids[OLD], datasource_id=src, names=WIDE_AND_NARROW)
        tables = await _table_ids(session, src)
        await _card(session, profile_id=ids[NEW], datasource_id=src, table="wide_t", tables=tables)
        for seq in (1, 2):
            await _card(
                session,
                profile_id=ids[NEW],
                datasource_id=src,
                table="wide_t",
                kind="table_columns",
                seq=seq,
                tables=tables,
            )
        await session.commit()

        with pytest.raises(AppError) as excinfo:
            await activate_profile(session, profile_id=ids[NEW])

        text = f"{excinfo.value.message} {excinfo.value.detail}"
        assert excinfo.value.code == "profile_cards_incomplete"
        assert "应有 3" in text and "实有 1" in text, f"两个数要按表去重：{text}"


async def test_软删的卡不算实有(session_factory: Any) -> None:
    """`deleted_at IS NULL` 是这把尺的一部分：那张卡对检索已经不存在了，不能拿它凑数。"""
    async with session_factory() as session:
        ids = await _insert(
            session,
            _profile(OLD, status="active"),
            _profile(NEW, status="draft", built_at=func.now()),
        )
        src = await _source(session, "demo-mysql", ("shop_t1", "shop_t2"))
        await _table_cards(
            session, profile_id=ids[OLD], datasource_id=src, names=("shop_t1", "shop_t2")
        )
        tables = await _table_ids(session, src)
        await _card(session, profile_id=ids[NEW], datasource_id=src, table="shop_t1", tables=tables)
        await _card(
            session,
            profile_id=ids[NEW],
            datasource_id=src,
            table="shop_t2",
            deleted=True,
            tables=tables,
        )
        await session.commit()

        with pytest.raises(AppError) as excinfo:
            await activate_profile(session, profile_id=ids[NEW])

        text = f"{excinfo.value.message} {excinfo.value.detail}"
        assert excinfo.value.code == "profile_cards_incomplete"
        assert "应有 2" in text and "实有 1" in text, f"软删那张不许计入实有：{text}"


async def test_term_卡不进尺(session_factory: Any) -> None:
    """人工术语卡的 `table_id` 是 NULL：它既不该被算成"这个源重建过了"，也不该拉低别的表。

    两半各挡一种错法：只有前半，`kind` 那条过滤漏了也不会红（多一张卡照样放行）；只有后半，
    "term 卡也算一张表"就会把本来齐了的那套挡在门外。
    """
    async with session_factory() as session:
        ids = await _insert(
            session,
            _profile(OLD, status="active"),
            _profile(NEW, status="draft", built_at=func.now()),
        )
        src = await _source(session, "demo-mysql", ("shop_t1",))
        tables = await _table_ids(session, src)
        await _table_cards(session, profile_id=ids[OLD], datasource_id=src, names=("shop_t1",))
        await _card(session, profile_id=ids[NEW], datasource_id=src, table=None, kind="term")
        await _card(session, profile_id=ids[NEW], datasource_id=src, table="shop_t1", tables=tables)
        await session.commit()

        outcome = await activate_profile(session, profile_id=ids[NEW])

        assert outcome.activated.id == ids[NEW], "term 卡多一张不该把齐了的那套挡在门外"

        # 后半：只有 term 卡的那套档案 = 这个源一张表都没重建，必须算缺，且实有要写 0。
        only_term = "third@1536@tplv1"
        third = await _insert(session, _profile(only_term, status="draft", built_at=func.now()))
        await _card(
            session, profile_id=third[only_term], datasource_id=src, table=None, kind="term"
        )
        await session.commit()

        with pytest.raises(AppError) as excinfo:
            await activate_profile(session, profile_id=third[only_term])

        text = f"{excinfo.value.message} {excinfo.value.detail}"
        assert excinfo.value.code == "profile_cards_incomplete"
        assert "应有 1" in text and "实有 0" in text, f"term 卡不许顶替表卡凑数：{text}"


async def test_切换不删旧档案下的任何一张卡(session_factory: Any) -> None:
    """验收 3：旧向量与旧卡片都还在，那才是"可回滚"的物理前提（029 验收 5 的卡片半边）。"""
    async with session_factory() as session:
        ids = await _insert(
            session,
            _profile(OLD, status="active"),
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
        old_cards = await _card_count(session, ids[OLD])
        total_before = await _card_count(session, ids[NEW])

        await activate_profile(session, profile_id=ids[NEW])

        assert old_cards == 2
        assert await _card_count(session, ids[OLD]) == old_cards, "切换不许动旧档案的任何一张卡"
        assert await _card_count(session, ids[NEW]) == total_before
        # 再退回去：那批卡还在，所以回滚不需要重跑作业（工单已定口径）。
        back = await activate_profile(session, profile_id=ids[OLD])
        assert back.activated.id == ids[OLD]
        assert await _active_count(session) == 1


async def test_陈旧表不进尺(session_factory: Any) -> None:
    """`is_stale = true` 那张表源库已无实体，本轮同步不给它建卡，所以它不该算"缺"。"""
    async with session_factory() as session:
        ids = await _insert(
            session,
            _profile(OLD, status="active"),
            _profile(NEW, status="draft", built_at=func.now()),
        )
        src = await _source(session, "demo-mysql", ("shop_t1", "shop_t2"), stale=("shop_t2",))
        await _table_cards(session, profile_id=ids[OLD], datasource_id=src, names=("shop_t1",))
        await _table_cards(session, profile_id=ids[NEW], datasource_id=src, names=("shop_t1",))
        await session.commit()

        outcome = await activate_profile(session, profile_id=ids[NEW])

        assert outcome.activated.id == ids[NEW], "应建那把尺里陈旧表要不算，否则永远补不齐"
        assert await _active_count(session) == 1


# ================================= 验收 2 在本片能验的那一半（降级理由写在交付记录）
#
# 工单 030 验收 2 的原句是"激活 1024 档案 → `/kb/search` 走 `embedding_1024`"，那半句的靶子是
# 031 的读侧，本片还没有它。工单给了降级口径："库里两列内容各自未变"。这里断的是**同一件事**
# 里更硬的那一面：切换这件事只改 `kb_index_profile.status`，不改 `kb_card` 的任何一格。


async def _fill_vectors(session: AsyncSession, profile_id: int) -> None:
    """给那套档案下的每张卡把**两根向量列都**灌上值，且逐卡不同。

    宽度取列自己声明的那一格（pgvector 的 `.dim`）：写错长度 PG 当场拒，这正好说明两列是两列。
    逐卡不同是为了让"整列被换成了别的批次"这种错不被"每卡同一个常量"掩盖过去。
    """
    emb_dim = int(KbCard.__table__.c.embedding.type.dim)
    narrow_dim = int(KbCard.__table__.c.embedding_1024.type.dim)
    card_ids = (
        (
            await session.execute(
                select(KbCard.id).where(KbCard.index_profile_id == profile_id).order_by(KbCard.id)
            )
        )
        .scalars()
        .all()
    )
    for card_id in card_ids:
        await session.execute(
            update(KbCard.__table__)
            .where(KbCard.__table__.c.id == card_id)
            .values(
                embedding=[float(card_id)] * emb_dim,
                embedding_1024=[float(-card_id)] * narrow_dim,
            )
        )


async def _vector_snapshot(session: AsyncSession) -> list[tuple[int, str | None, str | None]]:
    """每张卡的 `(id, embedding::text, embedding_1024::text)`，按 id 排序。

    比的是 PG 自己渲染出来那段文本，不是 Python 里那份 `list[float]`：拿 ORM 读回来的对象比
    就等于允许驱动层做一次转换，而"回滚后向量还在"这件事的真相只在库里那一格。
    """
    rows = (
        await session.execute(
            select(
                KbCard.id,
                cast(KbCard.embedding, Text),
                cast(KbCard.embedding_1024, Text),
            ).order_by(KbCard.id)
        )
    ).all()
    return [
        (int(r[0]), None if r[1] is None else str(r[1]), None if r[2] is None else str(r[2]))
        for r in rows
    ]


async def test_上线与回滚都不改两根向量列里的内容(session_factory: Any) -> None:
    """验收 2 降级那一半：切换前后，每张卡的两列逐字节相同。

    回滚（`retired → active`）读回去的正是这批没被动过的向量——所以它不需要重跑作业（工单已定
    口径），前提是"向量没被切走"。这一条就是那个前提的证据；`test_切换不删旧档案下的任何一张卡`
    证的是行还在，这一条证的是列里的值还是那批值。
    """
    async with session_factory() as session:
        ids = await _insert(
            session,
            _profile(OLD, status="active"),
            _profile(NEW, status="draft", built_at=func.now()),
        )
        src = await _source(session, "demo-mysql", ("shop_t1", "shop_t2"))
        await _table_cards(
            session, profile_id=ids[OLD], datasource_id=src, names=("shop_t1", "shop_t2")
        )
        await _table_cards(
            session, profile_id=ids[NEW], datasource_id=src, names=("shop_t1", "shop_t2")
        )
        await _fill_vectors(session, ids[OLD])
        await _fill_vectors(session, ids[NEW])
        await session.commit()

        before = await _vector_snapshot(session)
        assert len(before) == 4, "夹具该留下四张卡（两套档案 × 两张表）"
        assert all(r[1] is not None and r[2] is not None for r in before), (
            "两列都得有值，否则这条断言断在空上"
        )

        await activate_profile(session, profile_id=ids[NEW])

        assert await _vector_snapshot(session) == before, (
            "切换只该改 status，不该碰任何一张卡的向量"
        )

        back = await activate_profile(session, profile_id=ids[OLD])

        assert back.activated.id == ids[OLD]
        assert await _vector_snapshot(session) == before, "回滚读的就是这批还没被动的向量"
        assert await _rows(session) == [(NEW, "retired"), (OLD, "active")]
