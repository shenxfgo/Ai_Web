"""`chat_sessions` / `chat_messages` 的落库真跑（列定义与"哪些列 P2 恒空"见 metadata-model §2.7）。

为什么要有这一张表：**可解释性要留痕**。012 的验收里"retrieved 有 score_kw 而无 score_vec"、
"sql_raw ≠ sql_final"、"库里没有一行业务数据"三条都只能在这张表上验，纯函数与 stdout 都盖不住。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401  # 把全部 ORM 注册进 Base.metadata
from app.core.security import encrypt_secret
from app.models.chat import ChatMessage, ChatSession
from app.models.datasource import DataSource
from app.models.user import User

pytestmark = pytest.mark.pg


async def _seed(session: AsyncSession) -> tuple[User, DataSource]:
    user = User(
        username="asker",
        display_name="问数的人",
        role="member",
        password_hash="x",
    )
    session.add(user)
    await session.flush()
    src = DataSource(
        name="demo-mysql",
        kind="mysql",
        host="127.0.0.1",
        port=3306,
        catalog_name="",
        connect_user="aiweb_ro",
        secret_enc=encrypt_secret("pw"),
        params={},
        created_by=user.id,
    )
    session.add(src)
    await session.flush()
    return user, src


async def test_一次问数留下会话与消息两行(session_factory) -> None:
    async with session_factory() as session:
        user, src = await _seed(session)
        chat = ChatSession(user_id=user.id, datasource_id=src.id, title="2024 年每个月的订单总金额")
        session.add(chat)
        await session.flush()
        message = ChatMessage(
            session_id=chat.id,
            role="assistant",
            question_text="2024 年每个月的订单总金额是多少",
            retrieved=[{"table_uid": "u1", "score_kw": 3.5, "score_vec": None, "fused": None}],
            sql_raw="SELECT dt FROM ai_web_demo.order_main",
            sql_final="SELECT dt FROM ai_web_demo.order_main LIMIT 1001",
            guard_result={"ok": True, "violations": []},
            executed=True,
            # 只有 name 没有 type：行出 executor 时已过 `serialize_cell`（Decimal→字符串、
            # datetime→ISO），源库类型在那一刻就丢了，能填进这一格的只有列名。
            result_columns=[{"name": "dt"}],
            result_stats={"row_count": 12, "truncated": False, "result_file": "run1.csv"},
            chart_spec={"type": "line", "x": "dt", "series": ["金额"], "title": None},
            conclusion="全年共 12 个月有数据",
            latency_ms=2300,
            model="deepseek-chat",
        )
        session.add(message)
        await session.commit()

        got = await session.get(ChatMessage, message.id)
        assert got is not None
        assert got.sql_raw != got.sql_final  # 落库的是守卫重生成后的版本，带 LIMIT
        assert got.chart_spec is not None and got.chart_spec["type"] == "line"


async def test_结果行绝不进元数据库(session_factory) -> None:
    """§2.7 那条"行集不在这张表里"（ADR-0004）在这里是一张**结构**闸门：没有装行的列。

    查的是 `information_schema` 而不是 ORM 的列集合：后者只能证明"模型里没写"，前者才证明
    **真建出来的那张表**写不进去——迁移和模型对不上时，只有前者会红。
    """
    async with session_factory() as session:
        columns = set(
            await session.scalars(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = :schema AND table_name = 'chat_messages'"
                ),
                {"schema": os.environ["AIWEB_PG__SCHEMA_NAME"]},
            )
        )
        assert columns, "chat_messages 没建出来"
        assert "result_rows" not in columns
        assert {"result_columns", "result_stats"} <= columns


async def test_早退的那一类也必须留行(session_factory) -> None:
    """拒答不落痕等于没拒。`executed=false` + `error_code` 是三类早退共同的形状。"""
    async with session_factory() as session:
        user, src = await _seed(session)
        chat = ChatSession(user_id=user.id, datasource_id=src.id, title="把 orders 表删了")
        session.add(chat)
        await session.flush()
        message = ChatMessage(
            session_id=chat.id,
            role="assistant",
            question_text="把 orders 表删了",
            retrieved=[],
            executed=False,
            error_code="no_schema_found",
            error_message="没有匹配到任何表：请补录表注释 / 检查授权 / 触发一次同步",
        )
        session.add(message)
        await session.commit()
        # 断的是"恰好一行"而不是"至少一行"：夹具每条用例前清过这张表，多出来的一行就是
        # 别的用例串了台——`assert count` 那种真值判断对此完全无感。
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ChatMessage)
                .where(ChatMessage.error_code == "no_schema_found")
            )
            == 1
        )
