"""`HybridRetriever` 的三条 mode、融合的次序与实现切换缝（工单 031 验收 3/4/5/6）。

接缝选在 `search()` 上（与 009 同一处），因为这一片真正会错的是**装配**：向量列选错、
`set_config` 没跟召回同事务、权限谓词漏一条、融合把带理由的那份对象丢掉——每一种都表现为
"结果看着合理但来路不对"，而单条 SQL 自身都是合法的。

靶是闸内替身（工单 028 / ADR-0015：真进程真 socket，不是桩）。语义那一格（"销售额该命中
哪张表"）不在这个文件里断言，替身证明的是形状不是语义（spec 故事 43）。

期望值口径（都不从实现反推）：
- **向量名次是我摆出来的**：三根卡片向量按问句自己的向量摆成 `q / one-hot / -q`，
  余弦距离因此严格是 `0 < 中等 < 2`，名次 1/2/3 由构造给定，不靠"替身大概会把哪张排前面"。
  问句向量走真客户端从替身取（同输入必同输出），取来只用于**摆位置**，不作为断言的算式。
- **关键词名次由 §5.1 的计数键给定**：`payment_record` 的列注释 `订单金额（元）` 对上
  切词切出的全部 4 个词，`order_main` 的 `订单状态` 只对上 1 个，`customer` 一个都对不上。
- **融合次序手算**：`rrf_k=60` 下 A=1/62+1/61、B=1/61+1/63、C=1/62，逐个分数写在用例里。
- 降级路径的期望就是 `LikeRetriever` 的期望（故事 26"逐字同构"），且这一族用例跑在
  **端点根本没配**的环境里（`tests/integration/conftest.py` 的 autouse 夹具）——配不上端点
  还照样出结果，才是"向量路整体下线"那句话的形状。

验收 5 在这一分两格：本文件跑的是"**旋钮真生效**"（给的值真的进了那句 SQL），而"换实现只改一行
/ 实现字面不出现在调用方"是不碰库也不碰 HTTP 的机械检查，按 spec §5 的分层住在
`tests/unit/test_retriever_seam.py`。
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from typing import NamedTuple

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.models  # noqa: F401
from app.core.errors import VectorColumnMissing
from app.core.security import hash_password
from app.models.datasource import DataSource
from app.models.kb import KbCard, KbIndexProfile
from app.models.meta import MetaTable
from app.models.user import User
from app.schemas.kb import SearchMode
from app.services import kb_service
from app.services.embedding_client import EmbeddingClient, EmbeddingNotConfigured
from app.services.nl2sql import retriever
from app.services.nl2sql.retriever import (
    HybridRetriever,
    LikeRetriever,
    RetrievedTable,
    SearchTuning,
)
from app.settings import get_settings
from tests.integration.conftest import FakeEmbedding, insert_active_profile
from tests.integration.test_kb_cards_pg import _cards, _column, _data_source, _table

pytestmark = pytest.mark.pg

_QUESTION = "订单金额"
# 与 `scripts/fake_embedding_server.py` 的 model 表同一个名字：它回 1536，`embedding` 那一列
# 也正是 1536 宽（0005 的 as-built），所以读侧映射给出的列与替身给的宽度天然对上。
_MODEL = "text-embedding-3-small"
_DIMENSION = 1536
# 融合常数取 `.env` 的缺省那一格。写死在这里而不是读 settings：用例的分数是**手算**的，
# 跟着配置漂就等于期望值由被测的实现算（工单 031 验收 1 反对的正是这个）。
_RRF_K = 60

_ORDER = "order_main"
_PAYMENT = "payment_record"
_CUSTOMER = "customer"
# 三张卡的名次摆位：`_place_by_order` 按它写向量，断言按它读次序。
_FIRST = [_ORDER, _CUSTOMER, _PAYMENT]


def _tuning(mode: SearchMode, *, top_vector: int = 80, ef_search: int = 64) -> SearchTuning:
    """手工把三个旋钮摆成"替身这一维 + 默认窗口"，不走 `resolve_tuning`。

    裁决那一层由 `tests/unit/test_retriever_rrf.py` 钉；这里绕开它，是为了让"请求给了三个值"
    这一格与"实现真的读了这三个值"那一格分开红——否则一次配置改动能同时打红两组用例。
    """
    return SearchTuning(mode=mode, top_vector=top_vector, ef_search=ef_search)


def _names(seed: Seed, found: Sequence[RetrievedTable]) -> list[str]:
    """把检索结果折成表名，红的时候一句 `['order_main', ...]` 比一串 md5 好读。

    为什么不切 `table_uid`：它是 md5（CONTEXT.md「表唯一键」：md5 over `\x1f` 连接的三段），
    本身不含表名字面。反查走 `table_id`——那张表在本次用例里是谁，夹具摆的时候就知道了。
    查不到的一律印成 `unknown:<id>` 而不是 KeyError：越权召回一张没摆过的表，要看得见它召回了，
    而不是红在断言的辅助函数里。
    """
    by_id = {tbl.id: name for name, tbl in seed.tables.items()}
    return [by_id.get(item.table_id, f"unknown:{item.table_id}") for item in found]


class Seed(NamedTuple):
    actor: User
    src: DataSource
    tables: dict[str, MetaTable]
    cards: dict[str, KbCard]


async def _prepare(session: AsyncSession) -> Seed:
    """三张窄表 + 各自的 `table` 卡（卡片正文由 008 的渲染器产，不给检索器手搓假料）。"""
    actor = User(username="Seeker", password_hash=hash_password("x"), role="member")
    session.add(actor)
    await session.flush()
    src = _data_source(actor.id, "hybrid-src")
    session.add(src)
    await session.flush()

    tables: dict[str, MetaTable] = {}
    cards_by_table: dict[str, KbCard] = {}
    # 表注释一律不含问句里的词：召回与计数都得只由**列注释**决定，否则一条断言同时管两件事。
    for name, column, comment in (
        (_PAYMENT, "amount", "订单金额（元）"),  # 4 个词全对上 → 关键词第 1 名
        (_ORDER, "status", "订单状态"),  # 只对上 `订单` → 关键词第 2 名
        (_CUSTOMER, "phone", "客户手机号"),  # 一个都对不上 → 不在关键词路里
    ):
        tbl = await _table(session, src, name)
        session.add(_column(tbl, 1, column, comment_zh=comment))
        await session.flush()
        tables[name] = tbl

    for card in await _cards(session, src, [tbl.id for tbl in tables.values()]):
        cards_by_table[next(n for n, t in tables.items() if t.id == card.table_id)] = card
    return Seed(actor, src, tables, cards_by_table)


def _pin_embedding(
    monkeypatch: pytest.MonkeyPatch,
    handle: FakeEmbedding,
    *,
    model: str,
    dimension: int,
) -> None:
    """把 settings 那四格指向替身，并报上"这一维用的是哪个 model 名"。

    为什么用例要主动改配置：读侧的**列名**取自 active 档案那一行（故事 19），而问句向量的
    **宽度**由 settings 的 model 名决定（替身按 model 名报宽）。换基准的线上动作本来就把这两样
    一起换；只换其一的话向量路抬 `embedding_dimension_mismatch`，那是验收 6 单独钉的事。
    """
    monkeypatch.setenv("AIWEB_EMBEDDING__BASE_URL", handle.base_url)
    monkeypatch.setenv("AIWEB_EMBEDDING__API_KEY", "k")
    monkeypatch.setenv("AIWEB_EMBEDDING__MODEL", model)
    monkeypatch.setenv("AIWEB_EMBEDDING__DIMENSION", str(dimension))
    get_settings.cache_clear()


async def _query_vector_at(dimension: int) -> list[float]:
    """问句自己的向量：走**真客户端 + 真 socket** 从替身取，同输入必同输出。

    宽度由调用方给定（基准那一档 1536；换到 1024 那一档时同一句问句再问一次）。取来只用于
    **摆位置**，不作为任何断言的算式。
    """
    client = EmbeddingClient(embedding=get_settings().embedding)
    (vector,) = await client.embed([_QUESTION], expected_dimension=dimension)
    return vector


async def _place_by_order(
    session: AsyncSession,
    cards: dict[str, KbCard],
    *,
    column: str,
    dimension: int,
    order: Sequence[str],
) -> None:
    """把三张卡按 `order` 摆成余弦距离 `0 < 中间 < 2`，写进 `column` 那一列。

    - 第 1 名 = 问句向量本身 → 距离 0。
    - 第 2 名 = 单位向量（`q` 的最大那个分量那一维）→ 相似度落在 (0, 1) 开区间。
      取 argmax 而不是随手挑一维，是为了**保证**它是正数（单位向量的最大分量必然 ≥ 0，
      而 `q` 不是那一维本身，所以也必然 < 1）。
    - 第 3 名 = `-q` → 相似度 −1、距离 2（余弦距离的上界）。

    名次由调用方的 `order` 给定，不由实现算出——期望值不来自被测代码。换列那一条把 `order`
    整个反过来摆：两列都按同一次序摆的话，读错列照样绿。

    写向量的语句用 029 那条生产同款 `card_vector_update(column)`：读侧要验的是"读对列"，
    那就得让写侧真的走那一列。
    """
    q = await _query_vector_at(dimension)
    head = max(range(len(q)), key=lambda index: q[index])
    one_hot = [0.0] * dimension
    one_hot[head] = 1.0
    stmt = kb_service.card_vector_update(column)
    for name, vector in zip(order, (q, one_hot, [-value for value in q]), strict=True):
        await session.execute(stmt, {"card_id": cards[name].id, "vector": vector})


@pytest.fixture
def embedding_off() -> None:
    """显式写下"这一族用例跑在端点没配的环境里"。

    真正的清空在 `tests/integration/conftest.py::embedding_endpoint_off`（autouse），这里只是
    让读代码的人一眼看见"这条用例的前提是没有端点"，而不是碰巧没配。
    """
    assert not get_settings().embedding.configured
    return None


@pytest.fixture
def embedding(
    monkeypatch: pytest.MonkeyPatch, fake_embedding: FakeEmbedding
) -> Iterator[FakeEmbedding]:
    """把四格指向闸内替身（覆盖 autouse 那份"没有端点"）。"""
    _pin_embedding(monkeypatch, fake_embedding, model=_MODEL, dimension=_DIMENSION)
    try:
        yield fake_embedding
    finally:
        get_settings.cache_clear()


async def _seeded(
    session_factory: async_sessionmaker[AsyncSession], *, with_vectors: bool
) -> tuple[AsyncSession, Seed]:
    """摆好"active 档案 + 三张表 + 卡片（± 向量）"，回一条**没提交结束**的会话。

    档案行用 `insert_active_profile` 摆（工单 027 之后同步链路不再自己长基准行）；向量走真
    HTTP 从替身取，所以 `with_vectors=True` 的用例必须自己 request `embedding` 夹具。
    """
    await insert_active_profile(
        session_factory, model=_MODEL, dimensions=_DIMENSION, card_template_version=1
    )
    session = session_factory()
    seed = await _prepare(session)
    if with_vectors:
        # 基准那一档：三张卡摆进 `embedding`，名次 `_FIRST`（向量第 1/2/3 由构造给定）。
        await _place_by_order(
            session, seed.cards, column="embedding", dimension=_DIMENSION, order=_FIRST
        )
    await session.commit()
    return session, seed


async def test_hybrid_两路名次折完之后关键词头名被向量路翻掉(
    session_factory: async_sessionmaker[AsyncSession], embedding: FakeEmbedding
) -> None:
    """工单 031 验收 3/4 的正脸：一条 SQL 都合法，只有装配对了次序才长成手算的那样。

    手算（`rrf_k=60`）：
    - `order_main`：关键词第 2 + 向量第 1 → `1/62 + 1/61` ≈ 0.032522
    - `payment_record`：关键词第 1 + 向量第 3 → `1/61 + 1/63` ≈ 0.032266
    - `customer`：只在向量第 2 → `1/62` ≈ 0.016129

    头名从关键词路的 `payment_record` 翻成 `order_main`，靠的是**名次**而不是分数（ADR-0014）：
    两路都露过面的那张，比只在一路里排第一的那张值钱。
    """
    session, seed = await _seeded(session_factory, with_vectors=True)
    async with session:
        found = await HybridRetriever(tuning=_tuning("hybrid"), rrf_k=_RRF_K).search(
            session, actor=seed.actor, question=_QUESTION, k=5
        )

    assert _names(seed, found) == [_ORDER, _PAYMENT, _CUSTOMER]
    # 两路都命中的那张带的是关键词路那份形状：命中理由不许在装配时被换成向量路的空壳。
    order, payment = found[0], found[1]
    assert (order.matched_term_count, order.matched_column_count) == (1, 1)
    assert (payment.matched_term_count, payment.matched_column_count) == (4, 1)
    assert {(h.column_name, h.term) for h in order.hits} == {("status", "订单")}
    # 只在向量路的那张：没有理由可说，也不许硬凑（口径写在 `_vector_table` 的文档串里）。
    customer = found[2]
    assert (customer.score_kw, customer.matched_term_count, customer.hits, customer.cards) == (
        0.0,
        0,
        (),
        (),
    )


async def test_mode_keyword_与_LikeRetriever_逐字段相同且不包含只有向量的表(
    session_factory: async_sessionmaker[AsyncSession], embedding_off: None
) -> None:
    """故事 26：降级路径与 P2 **逐字同构**，而"向量路整体下线"的字面意思就是 `customer` 消失。

    这一条跑在**端点没配**的环境里（`embedding_off` 当场核一遍前提）：降级不碰 embedding
    客户端，所以它既不会因端点坏了而红，也不会花一次别人的额度——这是"降级路径"四个字
    能被兑现的地方。
    """
    session, seed = await _seeded(session_factory, with_vectors=False)
    async with session:
        like = await LikeRetriever().search(session, actor=seed.actor, question=_QUESTION, k=5)
        hybrid = await HybridRetriever(tuning=_tuning("keyword"), rrf_k=_RRF_K).search(
            session, actor=seed.actor, question=_QUESTION, k=5
        )

    assert _names(seed, like) == [_PAYMENT, _ORDER]
    # `==` 打在 frozen dataclass 上 = 逐字段比（含 score_kw 与整条 hits/cards）。
    assert hybrid == like


async def test_mode_vector_在端点没配时抬一档而不是安静回落到关键词路(
    session_factory: async_sessionmaker[AsyncSession], embedding_off: None
) -> None:
    """故事 27 的反面：有人明确要向量而机器没配，**不许**改成给他关键词结果。

    静默降级的代价是响应里写了一件没发生的事——界面上"混合检索"的标记还亮着。抬
    `EmbeddingNotConfigured`（503）才把"去配四格或起替身"这句话送到人眼前。
    """
    session, seed = await _seeded(session_factory, with_vectors=False)
    async with session:
        with pytest.raises(EmbeddingNotConfigured):
            await HybridRetriever(tuning=_tuning("vector"), rrf_k=_RRF_K).search(
                session, actor=seed.actor, question=_QUESTION, k=5
            )


async def test_top_vector_收到_1_时向量路只带一张表上来(
    session_factory: async_sessionmaker[AsyncSession], embedding: FakeEmbedding
) -> None:
    """验收 5 的"旋钮真的生效"这一格：`LIMIT` 不是装饰品，`customer`（向量第 2 名）该消失。

    `top_vector=1` 之后：`order_main` = `1/62 + 1/61`、`payment_record` = `1/61`（只有关键词），
    次序仍是前者在前，但三条变两条。SQL 形状那一格已由 `test_retriever_vector_sql.py` 钉住，
    这一条钉的是"构造时给的值真的进了那一句"。
    """
    session, seed = await _seeded(session_factory, with_vectors=True)
    async with session:
        found = await HybridRetriever(tuning=_tuning("hybrid", top_vector=1), rrf_k=_RRF_K).search(
            session, actor=seed.actor, question=_QUESTION, k=5
        )

    assert _names(seed, found) == [_ORDER, _PAYMENT]


async def test_k_收口发生在融合之后而不是各自砍一刀(
    session_factory: async_sessionmaker[AsyncSession], embedding: FakeEmbedding
) -> None:
    """`k=1` 出门的是**融合后的**头名 `order_main`，不是关键词路的头名 `payment_record`。

    两条腿各自先砍到 1 再融合的话，`order_main` 在关键词路就被砍掉了（它排第 2），融合就只剩
    `payment_record` 与向量路的表——这一条正是那种写法唯一的红点。
    """
    session, seed = await _seeded(session_factory, with_vectors=True)
    async with session:
        found = await HybridRetriever(tuning=_tuning("hybrid"), rrf_k=_RRF_K).search(
            session, actor=seed.actor, question=_QUESTION, k=1
        )

    assert _names(seed, found) == [_ORDER]


async def test_向量路只召回当前生效档案且只召回看得见的源(
    session_factory: async_sessionmaker[AsyncSession], embedding: FakeEmbedding
) -> None:
    """§5.2 (1) WHERE 那三个格子在真库上的形状（单元用例只钉得了 SQL 文本）。

    两个"看起来正常"的错法各配一根反证，两根的数据形状不同，因为要躲过的谓词不同：
    - 漏 `datasource_id`：别人源里的卡摆成**与问句同向**（距离 0），漏了这一格它必出头名，
      而"少一张别人的表"这种断言根本分不出"过滤生效"和"没召回"。
    - 漏 `index_profile_id`：老档案（`retired`）下的卡放在**同一个看得见的源**里——它躲得过源那格、
      只躲得过档案那格。距离摆成中间值而非 0：与 `order_main` 并列 0 会让次序变成浮点运气，
      而"插在中间把精确次序撞歪"是确定性的红。
    """
    session, seed = await _seeded(session_factory, with_vectors=True)
    async with session:
        other = User(username="Other", password_hash=hash_password("x"), role="member")
        session.add(other)
        await session.flush()
        their_src = _data_source(other.id, "their-hybrid")
        session.add(their_src)
        await session.flush()
        theirs = await _table(session, their_src, "secret_ledger")
        seed.tables["secret_ledger"] = theirs  # 让 `_names` 认得它：越权召回时要印名字
        session.add(_column(theirs, 1, "note", comment_zh="备注"))
        await session.flush()
        # `_cards` 回的是**全库**的卡（它按 table_id 排序、不按入参过滤），所以按 table_id 挑。
        built = await _cards(session, their_src, [theirs.id])
        [their_card] = [card for card in built if card.table_id == theirs.id]
        # 与问句同向 = 余弦距离 0，即"全场最相关"，越权了必然排在最前。
        await session.execute(
            kb_service.card_vector_update("embedding"),
            {"card_id": their_card.id, "vector": await _query_vector_at(_DIMENSION)},
        )

        # 老档案那一半：retired 那行从 030 的切换链上取要多摆一次 active，这里手插一行——
        # 对读侧而言档案 id 只是卡上的一个 FK 值，它是不是"上一版"与这条断言无关。
        serving = await session.scalar(
            select(KbIndexProfile).where(KbIndexProfile.status == "active")
        )
        assert serving is not None
        retired = KbIndexProfile(
            name=f"{_MODEL}@{_DIMENSION}@1#retired",
            provider=serving.provider,
            model=serving.model,
            dimensions=serving.dimensions,
            card_template_version=serving.card_template_version,
            status="retired",
            built_at=func.now(),
        )
        session.add(retired)
        await session.flush()
        stale = await _table(session, seed.src, "legacy_ledger")
        seed.tables["legacy_ledger"] = stale
        session.add(_column(stale, 1, "amount", comment_zh="订单金额（元）"))
        await session.flush()
        await kb_service.sync_cards(
            session,
            index_profile_id=int(retired.id),
            datasource_id=seed.src.id,
            job_id=None,
            table_ids=[stale.id],
            dialect_name=seed.src.kind,
            server_version=seed.src.server_version or "",
        )
        stale_cards = (
            (
                await session.execute(
                    select(KbCard).where(
                        KbCard.index_profile_id == int(retired.id), KbCard.table_id == stale.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert stale_cards, "老档案下一张卡都没落成：那半根反证就是空的"
        q = await _query_vector_at(_DIMENSION)
        head = max(range(len(q)), key=lambda index: q[index])
        one_hot = [0.0] * _DIMENSION
        one_hot[head] = 1.0
        for card in stale_cards:
            await session.execute(
                kb_service.card_vector_update("embedding"),
                {"card_id": card.id, "vector": one_hot},
            )
        await session.commit()

        found = await HybridRetriever(tuning=_tuning("vector"), rrf_k=_RRF_K).search(
            session, actor=seed.actor, question=_QUESTION, k=5
        )

    assert _names(seed, found) == [_ORDER, _CUSTOMER, _PAYMENT]
    assert "secret_ledger" not in _names(seed, found)
    assert "legacy_ledger" not in _names(seed, found)


async def test_还没有向量的卡不进向量路(
    session_factory: async_sessionmaker[AsyncSession], embedding: FakeEmbedding
) -> None:
    """ADR-0003「卡片先于向量」的读侧一半：NULL 排不出距离，而 PG 把 NULL 排在升序**末尾**。

    少了 `<col> IS NOT NULL` 这一格，"这张卡还没灌向量"会伪装成"这张表最不相关"，并在
    `top_vector` 大于真向量卡数时来占名额——`_place_by_order` 摆的那三张之外的第四张就是这一格。
    """
    session, seed = await _seeded(session_factory, with_vectors=True)
    async with session:
        fresh = await _table(session, seed.src, "no_vector_yet")
        seed.tables["no_vector_yet"] = fresh
        session.add(_column(fresh, 1, "amount", comment_zh="订单金额（元）"))
        await session.flush()
        await _cards(session, seed.src, [fresh.id])
        await session.commit()

        found = await HybridRetriever(tuning=_tuning("vector"), rrf_k=_RRF_K).search(
            session, actor=seed.actor, question=_QUESTION, k=5
        )
        # 同一句问句走关键词路时它必须出现——否则"没进向量路"可能是"根本没这张表"。
        keyword = await HybridRetriever(tuning=_tuning("keyword"), rrf_k=_RRF_K).search(
            session, actor=seed.actor, question=_QUESTION, k=5
        )

    assert "no_vector_yet" not in _names(seed, found)
    assert "no_vector_yet" in _names(seed, keyword)


async def test_档案那一维没有预置列时读侧当场抬错而不是猜一列(
    session_factory: async_sessionmaker[AsyncSession],
    embedding: FakeEmbedding,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工单 031 验收 6：读侧复用写侧那一份"维度→列名"映射，第三维**显式报错**。

    造法是把 active 档案那一行的 `dimensions` 改成 768（列只有 1536/1024 两维，0008 迁移的
    事实），问句照旧。静默回落到"第一列"的代价是拿 1536 的列去比 768 的向量，PG 当场报维度错
    还好；两列宽度恰好兼容时，检索会返回一份"看起来正常"的错结果（§5.2 (1) 那一格）。
    """
    session, seed = await _seeded(session_factory, with_vectors=True)
    async with session:
        profile = await session.scalar(select(KbIndexProfile))
        assert profile is not None
        profile.dimensions = 768
        await session.commit()

        with pytest.raises(VectorColumnMissing) as exc:
            await HybridRetriever(tuning=_tuning("hybrid"), rrf_k=_RRF_K).search(
                session, actor=seed.actor, question=_QUESTION, k=5
            )
    detail = str(exc.value)
    assert "768" in detail, detail
    assert "1536" in detail and "1024" in detail, detail


# ============================================================ 换基准：读侧跟着档案的维度走

# 替身那张 model→宽度 表里的第二个名字（`scripts/fake_embedding_server.py`，故事 42）：
# 报 1024 维，落的就是 0008 给的第二列。
_MODEL_1024 = "bge-large-zh-v1.5"
_DIMENSION_1024 = 1024


async def test_激活换到另一维之后向量路读的就是那一维的那一列(
    session_factory: async_sessionmaker[AsyncSession],
    fake_embedding: FakeEmbedding,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """spec 故事 19 后半句（"激活后检索读 1024 列；再激活回旧档案则读 1536 列"）的正身。

    工单 030 的验收 2 当时按降级口径验的是"两根向量列内容各自未变"，理由就是读侧还不存在
    （metadata-model §2.6 末注 as-built(P4-030) 第 6 条），并把这一格写给本片。三个方向各走一次：

    1. A（1536）在服务，`embedding` 里摆成 `_FIRST`；
    2. 给同一批表在 B（1024）下再建一批卡，`embedding_1024` 里摆成**反过来**的次序，激活 B；
    3. 再激活回 A（故事 6 那句"可回滚"在检索侧的样子）。

    B 的卡由 `sync_cards(index_profile_id=B)` 直接建（027 之后建卡不自己长档案，reindex 那颗
    作业也是这一条形）；两张档案同时在场是 partial unique 允许的形状，`_cards` 那一个 helper
    只认 active 那一行，所以这里不能复用它。
    """
    _pin_embedding(monkeypatch, fake_embedding, model=_MODEL, dimension=_DIMENSION)
    session, seed = await _seeded(session_factory, with_vectors=True)
    async with session:
        serving = await session.scalar(
            select(KbIndexProfile).where(KbIndexProfile.status == "active")
        )
        assert serving is not None
        second = KbIndexProfile(
            name=kb_service.profile_name(
                _MODEL_1024, dimension=_DIMENSION_1024, card_template_version=1
            ),
            provider="openai_compatible",
            model=_MODEL_1024,
            dimensions=_DIMENSION_1024,
            card_template_version=1,
            status="draft",
            built_at=func.now(),
        )
        session.add(second)
        await session.flush()
        second_id = int(second.id)

        await kb_service.sync_cards(
            session,
            index_profile_id=second_id,
            datasource_id=seed.src.id,
            job_id=None,
            table_ids=[tbl.id for tbl in seed.tables.values()],
            dialect_name=seed.src.kind,
            server_version=seed.src.server_version or "",
        )
        by_table = {tbl.id: name for name, tbl in seed.tables.items()}
        built = (
            (
                await session.execute(
                    select(KbCard)
                    .where(KbCard.index_profile_id == second_id)
                    .order_by(KbCard.table_id, KbCard.seq)
                )
            )
            .scalars()
            .all()
        )
        second_cards: dict[str, KbCard] = {}
        for card in built:
            second_cards.setdefault(by_table[int(card.table_id)], card)  # 主卡 seq=0 先来
        assert set(second_cards) == set(seed.tables), second_cards

        _pin_embedding(monkeypatch, fake_embedding, model=_MODEL_1024, dimension=_DIMENSION_1024)
        await _place_by_order(
            session,
            second_cards,
            column="embedding_1024",
            dimension=_DIMENSION_1024,
            order=list(reversed(_FIRST)),
        )
        await session.commit()

        await kb_service.activate_profile(session, profile_id=second_id)
        switched = await HybridRetriever(tuning=_tuning("vector"), rrf_k=_RRF_K).search(
            session, actor=seed.actor, question=_QUESTION, k=5
        )

        await kb_service.activate_profile(session, profile_id=int(serving.id))
        _pin_embedding(monkeypatch, fake_embedding, model=_MODEL, dimension=_DIMENSION)
        back = await HybridRetriever(tuning=_tuning("vector"), rrf_k=_RRF_K).search(
            session, actor=seed.actor, question=_QUESTION, k=5
        )

    assert _names(seed, switched) == list(reversed(_FIRST)), (
        "激活 1024 档案以后读的还是 1536 那一列"
    )
    assert _names(seed, back) == _FIRST, "回滚以后读不回到老那一列——故事 6 的'可回滚'在检索侧断了"


# ============================================================ ef_search：下发、取值合法、单调性

# 探测值：既不是配置的缺省 64、也不是服务端的缺省 40，"下发了"和"没下发"因此一眼可分。
_PROBE_EF = 333
# `iterative_scan` 的两个拼写：单数 `relaxed_order` 是 pgvector 0.8.6 认的取值之一
# （合法表：`off | relaxed_order | strict_order`），复数是 §5.2 原文里那个错拼写。
_SCAN_OK = "relaxed_order"
_SCAN_BAD = "relaxed_orders"
# 弧上摆多少张表、相邻两名的余弦距离差多少。两个数一起决定"64 那一发真的被 ef 卡住了"：
# 200 > 80(`top_vector` 上限) > 64(小 ef) > 40(服务端缺省)，而 0.004 的间距比浮点误差大五个量级。
_ARC_COUNT = 200
_ARC_STEP = 0.004


async def test_ef_search_下发到的就是请求给的那个值且只活到本事务结束(
    session_factory: async_sessionmaker[AsyncSession], embedding: FakeEmbedding
) -> None:
    """故事 23（§P4 踩坑 ②）的直接证据：那句 `set_config` 落在召回所在的**同一个事务**里。

    两次 `SHOW` 中间夹的就是那次 `search()`，而且四次读全在**同一条连接**上——这条前提是
    `engine.connect()` + `AsyncSession(bind=那条连接)` 换来的（`await session.connection()`
    不行：事务一结束，它返回的那个 Connection 就 closed 了）。为什么必须钉住：池子是 FIFO 的，
    还回去再取多半是另一条连接，而那条上连 `hnsw.ef_search` 这个名字都不认识（实测 `SHOW`
    直接抬 `unrecognized configuration parameter`）——不钉的话后半句会绿得毫无意义。
    `pg_backend_pid()` 那两格就是把"当真钉住了"这件事本身也断言一遍。

    跨边界用的是 `conn.rollback()` 而不是 `session.commit()`：`Session(bind=连接)` 默认
    只在 SAVEPOINT 里提交，`commit()` 根本不会结束真事务，那样读回来的仍是本事务的值，
    红和绿都说明不了什么。而 `SET LOCAL` 写的是"到本事务结束"，提交与回滚是同一道边界。

    三种错法分别红在哪一格：

    - 事务内不是 333：`hnsw_tuning_statement` 压根没发，或者发的不是请求给的那个值。
      反证：注释掉 `retriever.py:711` 那一行 `session.execute(hnsw_tuning_statement(...))`，
      这里读回服务端的 40。
    - 跨边界仍是 333：写成了 `set_config(..., false)` 或裸 `SET`（不带 LOCAL）。SQL 一模一样、
      检索照跑出结果，代价是这个值留在池子里那条连接上，毒到下一个拿到它的请求——
      也就是"上一个请求的 ef 决定下一个请求的召回宽度"。
      这里断 `!= 333` 而不是 `== 40`：前者测的是"LOCAL 的语义"，后者测的是服务端的缺省配置。
    """
    session, seed = await _seeded(session_factory, with_vectors=True)
    await session.close()
    engine: AsyncEngine = session_factory.kw["bind"]
    async with engine.connect() as conn:
        async with AsyncSession(bind=conn) as pinned:
            await HybridRetriever(
                tuning=_tuning("vector", ef_search=_PROBE_EF), rrf_k=_RRF_K
            ).search(pinned, actor=seed.actor, question=_QUESTION, k=5)
            inside = await conn.scalar(text("SHOW hnsw.ef_search"))
            pid_inside = await conn.scalar(text("SELECT pg_backend_pid()"))
        await conn.rollback()
        outside = await conn.scalar(text("SHOW hnsw.ef_search"))
        pid_outside = await conn.scalar(text("SELECT pg_backend_pid()"))

    assert pid_inside == pid_outside, "两次读落在两条连接上：跨边界那一格没有可证的内容"
    assert inside == str(_PROBE_EF)
    assert outside != str(_PROBE_EF)


async def test_iterative_scan_的合法拼写要到扩展装载之后才开始被校验(
    session_factory: async_sessionmaker[AsyncSession], embedding: FakeEmbedding
) -> None:
    """生产下发的 `_SCAN_OK` 在**真库真会话**上被认，而 `_SCAN_BAD` 不被认。

    为什么必须先跑一次真检索：`hnsw.*` 在 vector 扩展装载进本会话之前只是**占位符**，
    `set_config` 对任何拼写都照收不误——裸发一句 `SET` 再读回来，绿的是"什么都没校验"。
    召回那句 `<=>` 会把扩展装进来，装载那一刻 PG 才把先前那份占位值拿去对枚举表：
    生产用的拼写若错（§5.2 原文的复数就是错的），红点落在 `search()` 而不是落在 `SHOW` 上，
    这条用例才叫真跑过。**不**反过来断"装载前什么拼写都收"：连接是池化复用的，取到的那条多半
    早已装载过扩展，那一格在这里不可复现（那个事实本身记在 `hnsw_tuning_statement` 的文档串里）。

    反证那一半（复数被拒）放在最后：它必然中止当前事务，而前半的读已经完成了。
    """
    session, seed = await _seeded(session_factory, with_vectors=True)
    async with session:
        await HybridRetriever(tuning=_tuning("vector"), rrf_k=_RRF_K).search(
            session, actor=seed.actor, question=_QUESTION, k=5
        )
        assert await session.scalar(text("SHOW hnsw.iterative_scan")) == _SCAN_OK

        with pytest.raises(DBAPIError, match="invalid value for parameter"):
            await session.execute(
                text("SELECT set_config('hnsw.iterative_scan', :value, true)"),
                {"value": _SCAN_BAD},
            )


def _unit_orthogonal(vector: Sequence[float]) -> list[float]:
    """一根与 `vector` **严格正交**的单位向量——弧就在这个方向上走。

    取绝对值最大的两格 `i`、`j`，令 `w[i]=v[j]`、`w[j]=-v[i]`、其余为 0：点积是
    `v[i]·v[j] − v[j]·v[i]`，恒等于 0，不需要任何数值补偿。挑最大的两格只为了让
    `‖w‖ = hypot(v[i], v[j])` 离 0 远一些（替身是 char-ngram 哈希向量，非零格本就有限）。
    """
    head, second = sorted(range(len(vector)), key=lambda index: -abs(vector[index]))[:2]
    norm = math.hypot(vector[head], vector[second])
    assert norm > 0.0, "替身回的向量全为零，正交方向造不出来"
    out = [0.0] * len(vector)
    out[head], out[second] = vector[second] / norm, -vector[head] / norm
    return out


def _arc_vectors(query: Sequence[float]) -> list[list[float]]:
    """第 `k` 根向量与问句的**余弦距离严格 = `_ARC_STEP * k`**，于是名次由构造给定。

    在 `q̂` 与 `û` 张成的平面里走：`v_k = cosθ_k · q̂ + sinθ_k · û`，则 `cos(q, v_k) = cosθ_k`
    （`‖v_k‖=1`、`û` 那一项与 `q` 正交而消失）。取 `cosθ_k = 1 − step·k`，`k < 250` 时恒在
    `[-1, 1]` 内，所以 `sinθ_k = √(1−cos²)` 不开负数。期望名次因此是 `arc_000 … arc_199`
    **这个顺序本身**，不是实现算出来的。
    """
    norm = math.sqrt(sum(value * value for value in query))
    forward = [value / norm for value in query]
    sideways = _unit_orthogonal(query)
    arc: list[list[float]] = []
    for step in range(_ARC_COUNT):
        cos_theta = 1.0 - _ARC_STEP * step
        sin_theta = math.sqrt(1.0 - cos_theta * cos_theta)
        arc.append([cos_theta * a + sin_theta * b for a, b in zip(forward, sideways, strict=True)])
    return arc


async def _seed_arc(session: AsyncSession, seed: Seed) -> list[str]:
    """摆 `_ARC_COUNT` 张"各有一张卡、各有一根弧向量"的表，回表名（升序即向量名次）。

    为什么不复用 `_cards()`：那 200 张表的正文内容在这一条里不参与检索（只走 `mode='vector'`），
    而向量必须**由本文件按距离摆**——渲染器不产向量，`sync_cards` 之后还得逐张 UPDATE，
    两百次往返换不来任何断言面。`doc_uid` 与 `table_uid` 仍走生产的算法（一个是 md5 生成列、
    一个是 `card_doc_uid`），因为向量路那句 WHERE 认的就是档案 id 与源 id。
    """
    active = await kb_service.read_active_profile(session)
    assert active is not None, "用例没摆 active 档案"
    anchor = seed.tables[_ORDER]
    names = [f"arc_{step:03d}" for step in range(_ARC_COUNT)]
    tables = [
        MetaTable(
            datasource_id=seed.src.id,
            database_id=anchor.database_id,
            catalog_name="",
            schema_name=anchor.schema_name,
            table_name=name,
            table_type="BASE TABLE",
        )
        for name in names
    ]
    session.add_all(tables)
    await session.flush()
    uid_of = {
        str(row["table_name"]): str(row["table_uid"])
        for row in (
            await session.execute(
                select(MetaTable.table_name, MetaTable.table_uid).where(
                    MetaTable.id.in_([tbl.id for tbl in tables])
                )
            )
        ).mappings()
    }
    by_name = {tbl.table_name: tbl for tbl in tables}
    # `names` 本身就是步号序（`arc_000 … arc_199` 定宽），与 `_arc_vectors` 逐格对齐。
    for name, vector in zip(names, _arc_vectors(await _query_vector_at(_DIMENSION)), strict=True):
        table = by_name[name]
        seed.tables[name] = table
        session.add(
            KbCard(
                datasource_id=seed.src.id,
                kind="table",
                table_id=table.id,
                seq=0,
                doc_uid=kb_service.card_doc_uid(
                    "table", uid_of[name], seq=0, index_profile_id=active.index_profile_id
                ),
                index_profile_id=active.index_profile_id,
                title=name,
                text_md=name,
                # 正文里没有问句的任何切词：这条用例不走向关键词路，但留下"即使走也不该出现"的形状。
                search_text=name,
                token_count=1,
                embedding=vector,
            )
        )
    await session.flush()
    return names


async def test_ef_search_从_64_调到_400_时召回的名次集合真的变大(
    session_factory: async_sessionmaker[AsyncSession],
    embedding: FakeEmbedding,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工单 031 验收 4（roadmap 验收 3 / 假绿防线 ①）：`ef_search` 不是一件装饰品。

    两格**用例前提**都得摆上，因为它们各自对应真库上量出来的一种"参数下发了也没用"：

    1. `SET LOCAL enable_sort = off`。200 行的表，优化器本来更愿意"顺序扫全表 + 排序"，
       那条路上 `<=>` 是精确算的，`ef_search` 根本不参与，两次检索交回的是一模一样的 80 张。
       关掉排序之后，能交付这个次序的路径只剩那条 HNSW 索引，ef 才第一次有上界效应。
       卡片上万时优化器自己会选索引，这一格只是"小表也要把计划钉在索引上"——这条用例证的
       从来不是"我们一定用索引"，而是"我下发的值进了那个会用索引的查询"。
    2. 把 `iterative_scan` 临时换成 `off`（生产值是 `relaxed_order`，由上面那条用例钉着）。
       `relaxed_order` 的语义就是"第一批不够就接着扫到 LIMIT 够为止"，所以它**故意**让结果集
       对 ef 不敏感——实测同一格计划（`enable_sort=off`）下 `ef=64`：`relaxed_order` 交回 80 张、
       `off` 交回 64 张。要露出 ef 这个窗口，只能先把"接着扫"那条路关掉。两条用例一条钉生产
       取值、一条钉 ef 的效应，不重复也不互相削弱。

    两种假绿各有一根钉，缺一根这条用例就退化成"SQL 能跑"（第一格是真跑过的反证：把
    `retriever.py` 里那句 `hnsw_tuning_statement` 注释掉再跑）：

    - **`SET LOCAL` 没下发** → 两次检索都用服务端缺省 40，两边都是 40 张，红在
      `ranks_400 == expected[:80]`（40 != 80）；最后那句 `len(ranks_64) < len(ranks_400)` 也是红的，
      只是排不到它。
    - **计划没走索引**（前提 1 没摆上）→ 两条腿都是"扫全表 + 精确算距离 + 排序"，交回同样的
      80 张，`ranks_400 == expected[:80]` 反而过得去；钉住这一格的是 `len(ranks_64) <= 64`
      （实测 80 > 64），它盯的是"小 ef 那一发真的被窗口截住了"。

    期望值全部来自构造（`_arc_vectors` 给的距离是 `0.004k`，严格递增、无并列）：
    `ranks_400` 应当正好是弧上前 80 张、按摆出去的顺序排列——`ef=400 > _ARC_COUNT` 时 HNSW
    把全图都走了一遍，这一格因此同时是"索引确实在工作"的阳性对照。
    """
    monkeypatch.setattr(retriever, "_ITERATIVE_SCAN", "off")
    session, seed = await _seeded(session_factory, with_vectors=False)
    async with session:
        expected = await _seed_arc(session, seed)
        await session.commit()

        await session.execute(text("SET LOCAL enable_sort = off"))
        found_64 = await HybridRetriever(
            tuning=_tuning("vector", ef_search=64), rrf_k=_RRF_K
        ).search(session, actor=seed.actor, question=_QUESTION, k=_ARC_COUNT)
        found_400 = await HybridRetriever(
            tuning=_tuning("vector", ef_search=400), rrf_k=_RRF_K
        ).search(session, actor=seed.actor, question=_QUESTION, k=_ARC_COUNT)

    ranks_64, ranks_400 = _names(seed, found_64), _names(seed, found_400)
    assert ranks_400 == expected[:80]
    assert len(ranks_64) <= 64, "比小 ef 允许的还多：那次检索没用上 HNSW 索引"
    assert set(ranks_64) <= set(ranks_400)
    assert len(ranks_64) < len(ranks_400), "大 ef 没有多召回任何东西：SET LOCAL 没生效"
