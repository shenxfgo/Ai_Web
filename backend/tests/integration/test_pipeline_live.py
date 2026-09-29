"""真演示库跑全链：检索 → prompt → 守卫 → **真 MySQL 执行** → 落盘 → 图表 → 结论。

工单 012 的"接口层"验收本体是 `scripts/demo_ask.py` 那一次真模型手测（拍板：桩跑出来的
"合法 SQL"是假通过）。这条用例补的是它**不能常驻**的那一半回归：每次闸口都真发一次
LLM 往返，等于把 CI 绑在一个第三方端点的可用性与一次随机输出上（结论步实测 8.9 秒，
且措辞会变），所以这里 LLM 走 respx、其余全真——真卡片、真守卫、真只读连接、真 csv。

期望值口径：
- `docs/verification.md` §1：演示库 2024 年 12 个月各有订单，月度聚合必然是 12 行
- 工单 012 验收 ①：合法 `SELECT`、`guard=pass`、非空结果行
- 工单 012 验收 ②（拍板 2 带进来的那条）：`chart_spec` 对"月份 + 金额"是 `line`
- `docs/architecture.md` §4.1 ⑥：模型自带的 LIMIT 不动，**没有** LIMIT 才注入
  `row_limit+1`——所以这里刻意喂一条不带 LIMIT 的草稿，让 raw≠final 在真执行上成立一次
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.datasource import DataSource
from app.models.user import User
from app.services.llm_client import LlmClient
from app.services.nl2sql import pipeline
from app.services.nl2sql.retriever import LikeRetriever
from tests.integration.conftest import Account, DbAccount
from tests.integration.test_pipeline_orchestration import READY_LLM, _chat
from tests.integration.test_sync_live import _sync_once

pytestmark = [pytest.mark.pg, pytest.mark.live]

Login = Callable[..., Awaitable[Account]]

QUESTION = "2024 年每个月的订单总金额是多少"
#: 不带 LIMIT 的草稿：注入分支才是这一片要验的那条路（真库上 raw==final 说明守卫没参与）
DRAFT = (
    "SELECT DATE_FORMAT(created_at, '%Y-%m') AS ym, SUM(amount) AS amt "
    "FROM ai_web_demo.order_main "
    "WHERE created_at >= '2024-01-01' AND created_at < '2025-01-01' "
    "GROUP BY ym ORDER BY ym"
)


def _reply(request: httpx.Request) -> httpx.Response:
    """按 system 消息分流（次序是被测对象的一部分，序列桩测不出"多叫了一次"）。"""
    body = json.loads(request.content)
    if "SQL 生成器" in body["messages"][0]["content"]:
        return httpx.Response(200, json=_chat(DRAFT))
    return httpx.Response(200, json=_chat("2024 年共 12 个月有订单，各月金额见结果表。"))


@respx.mock
async def test_真库全链跑通并且守卫注入的那条LIMIT真的生效(
    client: AsyncClient,
    login: Login,
    monkeypatch: pytest.MonkeyPatch,
    account: DbAccount,
    session_factory: async_sessionmaker[AsyncSession],
    results_dir: Path,
) -> None:
    run = await _sync_once(client, login, monkeypatch, account)
    route = respx.post("https://llm.test/v1/chat/completions").mock(side_effect=_reply)

    async with session_factory() as session:
        src = await session.get(DataSource, run.ds_id)
        assert src is not None
        actor = await session.get(User, int(src.created_by))
        assert actor is not None
        outcome = await pipeline.ask(
            session,
            actor=actor,
            question=QUESTION,
            datasource_ids=[run.ds_id],
            retriever=LikeRetriever(),
            llm=LlmClient(llm=READY_LLM, http=httpx.AsyncClient()),
        )

    assert outcome.error_code is None, outcome.error_message
    assert route.call_count == 2  # ⑤ 生成 + ⑨ 结论，一次都不许多

    # ⑥：注入发生在守卫重生成的那句上，模型原文不动
    assert outcome.sql_raw == DRAFT
    assert outcome.sql_final is not None and outcome.sql_final.upper().endswith("LIMIT 1001")
    assert outcome.guard_result == {"ok": True, "violations": []}

    # ⑦：真库真执行，12 个月一行不缺；金额列是 decimal 字符串而不是 float（safety §4.3）
    assert outcome.executed is True
    assert outcome.row_count == 12
    months = sorted(str(row[0]) for row in outcome.rows)
    assert months == [f"2024-{m:02d}" for m in range(1, 13)]
    assert all(isinstance(row[1], str) for row in outcome.rows)
    assert outcome.truncated is False

    # 结果只进文件，不进元数据库（ADR-0004）
    assert outcome.result_file is not None
    csv_path = Path(outcome.result_file)
    assert csv_path.exists() and csv_path.suffix == ".csv"
    assert len(csv_path.read_text(encoding="utf-8-sig").splitlines()) == 13

    # ⑧⑨：月份 + 金额这一问句必须是折线，结论必须有字
    assert outcome.chart_spec is not None
    assert outcome.chart_spec["type"] == "line"
    assert outcome.chart_spec["x"] == "ym"
    assert outcome.conclusion
