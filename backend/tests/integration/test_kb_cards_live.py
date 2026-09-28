"""真连演示库跑一次同步之后，卡片长什么样（工单 008 的验收全落在这里）。

为什么必须 live：这几条钉的都是**真元数据经过真抽取、真落库、再读回来生成**的文本——
"6 个枚举值在不在卡片里""视图那行降级了没有""人工补录的中文有没有进到 prompt 素材"，
每一件都跨越 抽取→落库→读料→模板 四层，任何一层的单测都只能证明自己那一段。

期望值口径：
- 工单 008 验收 ①–⑤
- `docs/verification.md` §1：10 个业务对象（9 表 + 1 视图）、`product_stats_wide` 68 列
- `docs/kb-workflow.md` §5（文本形状）、§6（切表与视图降权）、ADR-0003（向量可插拔）

007 的活体管线（登记源 → 打同步 → 读元数据库）直接复用 `test_sync_live` 的助手：
凭证只从 gitignored 的 `.setup/aiweb_ro.cnf` 读，复制一份只会多一个泄露口令的地方。
"""

from __future__ import annotations

import configparser
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.conftest import Account
from tests.integration.test_sync_live import (
    TABLES,
    VIEWS,
    WIDE_COLUMNS,
    _rows,
    _sync_once,
    _trigger,
)

pytestmark = [pytest.mark.pg, pytest.mark.live]

Login = Callable[..., Awaitable[Account]]
BACKEND = Path(__file__).resolve().parents[2]
CNF = BACKEND / ".setup" / "aiweb_ro.cnf"

# 10 个对象里只有 product_stats_wide 过 40 列 → 3 张（主卡 + 2 张切片），其余一表一卡
CARDS = TABLES + VIEWS + 2
CARD_TOKEN_LIMIT = 900  # §6 的"单块不超预算"


@pytest.fixture(scope="module")
def account() -> tuple[str, str, str, int]:
    """(user, password, host, port)——口令只活在这条调用链里，不进断言、不进日志。"""
    if not CNF.exists():
        pytest.skip(f"缺少 {CNF}：要先跑工单 002 的建库脚本")
    cp = configparser.ConfigParser()
    cp.read(CNF, encoding="utf-8")
    c = cp["client"]
    return c["user"], c["password"], c.get("host", "127.0.0.1"), int(c.get("port", "3306"))


async def _cards_of(
    session_factory: async_sessionmaker[AsyncSession], ds_id: int, table: str
) -> list[dict[str, Any]]:
    return await _rows(
        session_factory,
        "select c.kind, c.seq, c.title, c.text_md, c.search_text, c.token_count,"
        "       c.meta::text as meta, c.doc_uid, c.index_profile_id,"
        "       c.embedding is null as emb_null, c.embedded_at is null as embedded_null"
        ' from "{s}".kb_card c join "{s}".meta_table t on t.id = c.table_id'
        " where t.datasource_id = :ds and t.table_name = :tbl order by c.seq",
        ds=ds_id,
        tbl=table,
    )


async def test_验收1_十个对象各一条table卡_宽表切成三张(
    client: AsyncClient,
    login: Login,
    monkeypatch: pytest.MonkeyPatch,
    account: tuple[str, str, str, int],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """§6：一表一卡；>40 列 → 主卡 + 每 30 列一张切片。计数进 `sync_jobs.counters.cards`。

    12 张（10 个对象 + 宽表多出的 2 张）而不是 10 张：切片卡也是独立文档，
    合成一条就把 §6 的"单块不超 token 预算"白做了。
    """
    run = await _sync_once(client, login, monkeypatch, account)
    assert run.body["status"] == "success", json.dumps(run.body, ensure_ascii=False)
    assert run.body["counters"]["cards"] == CARDS, run.body["counters"]

    kinds = {
        row["kind"]: row["n"]
        for row in await _rows(
            session_factory, 'select kind, count(*) as n from "{s}".kb_card group by kind'
        )
    }
    assert kinds == {"table": TABLES + VIEWS, "table_columns": 2}

    wide = await _cards_of(session_factory, run.ds_id, "product_stats_wide")
    assert [row["seq"] for row in wide] == [0, 1, 2]
    assert all(int(row["token_count"]) <= CARD_TOKEN_LIMIT for row in wide), [
        (row["seq"], row["token_count"]) for row in wide
    ]
    # 切片卡重复表头块，否则它自己没判别力（§6 原文理由）
    assert "【表】ai_web_demo.product_stats_wide" in wide[1]["text_md"]
    assert f"【字段】共 {WIDE_COLUMNS} 个（本卡仅列出第 26-55 个" in wide[1]["text_md"]
    # §2.6 的 doc_uid 拌了 seq：三张卡必须互不相同
    assert len({row["doc_uid"] for row in wide}) == 3


async def test_验收2_枚举六值进卡片_varchar_的tags_不出取值段(
    client: AsyncClient,
    login: Login,
    monkeypatch: pytest.MonkeyPatch,
    account: tuple[str, str, str, int],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """工单验收 ②③：`已完成订单 → status='completed'` 的唯一来源就是【取值】那一段。

    反向那半边同样贵：`tags` 是 varchar(255)，看着像"逗号分隔的枚举"，但它不是——
    渲染侧一旦按类型猜枚举，AI 就会拿 `tags='VIP'` 去等值匹配一列 CSV。
    """

    run = await _sync_once(client, login, monkeypatch, account)

    async def field_lines(table: str) -> dict[str, str]:
        """按 §5 的字段行形状（`- <列名> <类型> ...`）把卡片拆成 {列名: 整行}。"""
        card = (await _cards_of(session_factory, run.ds_id, table))[0]["text_md"]
        return {
            line.split()[1]: line
            for line in card.splitlines()
            if line.startswith("- ") and len(line.split()) > 1
        }

    assert (
        "取值: pending / paid / shipped / completed / cancelled / refunding"
        in ((await field_lines("order_main"))["status"])
    )
    # `tags` 在 product 上（verification §1：逗号分隔的非规范化字段）
    product = await field_lines("product")
    assert "取值" not in product["tags"], product["tags"]


async def test_验收3_视图卡片降级不报错且带降权系数(
    client: AsyncClient,
    login: Login,
    monkeypatch: pytest.MonkeyPatch,
    account: tuple[str, str, str, int],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """verification §1 的考点：视图没有 TABLE_ROWS，【规模】要整行降级。

    半截话（"约  行"）会进 prompt，模型据此判断这张表大不大；
    `meta.weight=0.6` 只给检索排序读，不给模型读（§6 末行）。
    """
    run = await _sync_once(client, login, monkeypatch, account)
    rows = await _cards_of(session_factory, run.ds_id, "v_daily_sales")

    assert [r["kind"] for r in rows] == ["table"]
    text_md = rows[0]["text_md"]
    assert "【表】ai_web_demo.v_daily_sales（视图）" in text_md
    assert "【规模】行数未知（视图或未分析）" in text_md
    assert "约  行" not in text_md
    assert json.loads(rows[0]["meta"])["weight"] == 0.6


async def test_验收4_未配向量端点时卡片照落_embedding_为_NULL(
    client: AsyncClient,
    login: Login,
    monkeypatch: pytest.MonkeyPatch,
    account: tuple[str, str, str, int],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ADR-0003 的"可插拔"：没有 embedding 端点，链路照样产出可检索的卡片。

    `DIMENSION` 是列宽（建库期硬依赖），`embedding.configured` 才是向量路的开关；
    这些卡靠 `search_text` + 关键词索引就能被检索到。
    """
    await _sync_once(client, login, monkeypatch, account)
    row = (
        await _rows(
            session_factory,
            "select count(*) as total, count(*) filter (where embedding is null) as nulls"
            ' from "{s}".kb_card',
        )
    )[0]
    assert row["total"] == row["nulls"] == CARDS

    profiles = await _rows(
        session_factory,
        'select count(*) as n from "{s}".kb_index_profile where is_active',
    )
    assert profiles[0]["n"] == 1, "P2 只落一条 active profile（draft→切 active 属 P4）"


async def test_验收5_两次同步卡片不翻倍且人工注释进卡片(
    client: AsyncClient,
    login: Login,
    monkeypatch: pytest.MonkeyPatch,
    account: tuple[str, str, str, int],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """§3 的人工列保护一路走到卡片：用户补录的中文要成为下一轮 prompt 的素材。

    同一条源跑第二遍同时是幂等锚点——`kb_card` 不该翻倍（§2.6 的 doc_uid UNIQUE）。
    """
    run = await _sync_once(client, login, monkeypatch, account)
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        await session.execute(
            text(
                f'update "{schema}".meta_table set comment_zh = :zh'
                " where datasource_id = :ds and table_name = 'order_main'"
            ),
            {"zh": "订单主表（人工核对过）", "ds": run.ds_id},
        )
        await session.commit()

    await _trigger(client, run)
    assert run.body["status"] == "success", json.dumps(run.body, ensure_ascii=False)

    rows = await _cards_of(session_factory, run.ds_id, "order_main")
    assert len(rows) == 1, f"第二轮同步把卡片翻到了 {len(rows)} 张"
    assert "订单主表（人工核对过）" in rows[0]["text_md"], "人工注释没走到卡片"
    total = await _rows(session_factory, 'select count(*) as n from "{s}".kb_card')
    assert total[0]["n"] == CARDS
