from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from app.core.logging import reconfigure_std_streams

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

# 测试名是中文的，cp936 控制台下 pytest 的 ID 输出会成乱码
reconfigure_std_streams()

if sys.platform == "win32":  # asyncpg 与 Proactor 事件循环不兼容
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """真实 PG 用例默认 skip；配了 DSN 才跑；CI 里可要求不 skip 而是失败。"""
    if os.environ.get("AIWEB_PG_TEST_DSN"):
        return
    if os.environ.get("AIWEB_REQUIRE_PG_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="未设 AIWEB_PG_TEST_DSN")
    for item in items:
        if "pg" in item.keywords:
            item.add_marker(skip)
