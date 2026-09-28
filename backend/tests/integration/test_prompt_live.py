"""010 在真语料上的端到端：检索给的 uid → 卡片全文 + 关联边 → 完整 messages。

离线单测（`tests/unit/test_prompt_builder.py`）钉的是结构与手算预算；这里钉的是
"演示库真同步出来的卡片，塞进 prompt 之后那句话真的在场"——009 的教训是
离线全绿与真库跑通是两件事，而枚举取值这类事实只有真库能给答案。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401
from app.models.datasource import DataSource
from app.models.user import User
from app.services.nl2sql.prompt_builder import build_prompt
from app.services.nl2sql.retriever import load_schema_tables
from app.settings import get_settings
from tests.integration.conftest import Account, DbAccount
from tests.integration.test_kb_search_live import _item, _search_once
from tests.integration.test_sync_live import _sync_once

pytestmark = [pytest.mark.pg, pytest.mark.live]

Login = Callable[..., Awaitable[Account]]


async def _tables_for(
    session: AsyncSession, *, ds_id: int, items: list[dict[str, Any]]
) -> list[Any]:
    """检索候选（只有 uid）→ prompt 素材（全文 + 关联边）。"""
    src = await session.get(DataSource, ds_id)
    assert src is not None
    actor = await session.get(User, int(src.created_by))
    assert actor is not None
    return await load_schema_tables(
        session, actor=actor, table_uids=[str(item["table_uid"]) for item in items]
    )


async def _prompt_text(
    session: AsyncSession, *, ds_id: int, items: list[dict[str, Any]], question: str
) -> str:
    """把 system + user 拼成一条文本，好做"在场"断言。"""
    messages = build_prompt(
        question=question,
        tables=await _tables_for(session, ds_id=ds_id, items=items),
        token_budget=get_settings().retrieval.token_budget,
    )
    return "\n".join(str(message["content"]) for message in messages)


async def test_枚举取值与两条硬约束真的出现在送出的prompt里(
    client: AsyncClient,
    login: Login,
    monkeypatch: pytest.MonkeyPatch,
    account: DbAccount,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """工单验收"枚举取值出现在 prompt 里"+"两条硬约束在场"，在演示库真卡片上验一次。

    `order_main.status` 是真 ENUM，008 实测六个取值进卡；用户说"已完成"要能对上 `completed`，
    靠的就是这一行原样进 prompt。
    """
    run = await _sync_once(client, login, monkeypatch, account)
    question = "每个状态的订单各有多少"
    items = await _search_once(client, run, question, k=3)
    _item(items, "order_main")  # 先钉住召回：没召回的话，下面的"在场"就成了假通过
    async with session_factory() as session:
        prompt = await _prompt_text(session, ds_id=run.ds_id, items=items, question=question)

    assert "只能引用给定的表" in prompt
    assert "必须带 LIMIT" in prompt
    # 取值行要**绑在 status 这一行上**：分开断言会放过"取值行来自别的列"
    enum_lines = [line for line in prompt.splitlines() if "取值:" in line]
    status_line = next(line for line in enum_lines if "status" in line)
    assert "completed" in status_line
    assert "【输出格式】" in prompt and "【问题】" in prompt


async def test_真卡片全文远长于检索侧的二百字预览(
    client: AsyncClient,
    login: Login,
    monkeypatch: pytest.MonkeyPatch,
    account: DbAccount,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """009 的 `text_preview` 只有 200 字；prompt 拿到的必须是全文。

    演示库最宽的是 68 列的 `product_stats_wide`（008：主卡 + 2 个列切片）。
    问"金额"把它召回，然后数它进 prompt 的字符数——200 字根本装不下 68 列的字段清单。
    """
    run = await _sync_once(client, login, monkeypatch, account)
    items = await _search_once(client, run, "各商品的成交金额是多少", k=5)
    wide = _item(items, "product_stats_wide")
    async with session_factory() as session:
        tables = await _tables_for(session, ds_id=run.ds_id, items=[wide])
    full_text = "\n".join(seg for table in tables for seg in table.segments)

    assert len(full_text) > 200
    # 全文的标志：末列 `stock_qty`（第 68 列）只落在列切片里，而列切片正是 200 字预览装不下的部分
    assert "stock_qty" in full_text
