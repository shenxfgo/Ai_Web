"""结果单元格序列化：roadmap P2 踩坑 ②③ + safety §4.3 的单元格保护。

期望值来自文档口径而不是"代码怎么写的"：
- Decimal 必须按字符串回，不能让金额变成 `0.30000000000000004`（§4.3）
- naive datetime 进 json.dumps 会 500，所以日期走 ISO 字符串（§4.3 / 工单验收 ④）
- bytes 不能直接进 JSON，回 `"<binary 1.2KB>"` 这种可读占位（§4.3）
- 超长 str 按 `ResultGroup.max_cell_chars` 截断（§4.3）
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.services.nl2sql.executor import serialize_cell


def test_decimal_按字符串回不失精() -> None:
    # 0.1+0.2 用二进制浮点会是 0.30000000000000004；Decimal 原样转字符串保精度
    assert serialize_cell(Decimal("0.30"), max_cell_chars=1000) == "0.30"
    assert serialize_cell(Decimal("12345678901234567890.123"), max_cell_chars=1000) == (
        "12345678901234567890.123"
    )


def test_datetime_走_iso_字符串() -> None:
    # naive：源库 DATETIME 出来就是无时区的
    assert serialize_cell(datetime(2024, 3, 5, 14, 30, 0), max_cell_chars=1000) == (
        "2024-03-05T14:30:00"
    )
    # aware：保留偏移
    aware = datetime(2024, 3, 5, 14, 30, 0, tzinfo=timezone(timedelta(hours=8)))
    assert serialize_cell(aware, max_cell_chars=1000) == "2024-03-05T14:30:00+08:00"
    # 纯日期
    assert serialize_cell(date(2024, 3, 5), max_cell_chars=1000) == "2024-03-05"


def test_bytes_回可读占位() -> None:
    # <binary 1.2KB>：大小按 1024 进制保留一位小数
    assert serialize_cell(b"\x00" * 1200, max_cell_chars=1000) == "<binary 1.2KB>"
    # 不足 1KB 用 B
    assert serialize_cell(b"\x01\x02\x03", max_cell_chars=1000) == "<binary 3B>"


def test_超长字符串按上限截断() -> None:
    long = "甲" * 50
    out = serialize_cell(long, max_cell_chars=10)
    assert out == "甲" * 10


def test_短字符串与整型原样回() -> None:
    assert serialize_cell("订单", max_cell_chars=1000) == "订单"
    assert serialize_cell(42, max_cell_chars=1000) == 42
    assert serialize_cell(None, max_cell_chars=1000) is None
