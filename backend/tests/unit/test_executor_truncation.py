"""截断探测：工单 011 验收 ③ + safety §4.3 的"取到 row_limit+1 行即判定截断"。

口径来自 §4.3：守卫注入的 LIMIT 是 `row_limit+1`（由 `check(max_rows=row_limit)` 带入），
执行侧取到 `row_limit+1` 行就判定 `truncated=true` 并**丢弃那多出来的一行**——
那一行只是"还有没有更多"的探针，不是给用户看的数据。
"""

from __future__ import annotations

from app.services.nl2sql.executor import split_truncated


def test_取满_row_limit_加_1_行判截断并丢掉探针行() -> None:
    rows = [(i,) for i in range(1001)]  # row_limit=1000，取到了 1001 行
    kept, truncated = split_truncated(rows, row_limit=1000)
    assert truncated is True
    assert len(kept) == 1000
    assert kept[-1] == (999,)  # 第 1001 行（探针）被丢弃


def test_不足上限不算截断() -> None:
    rows = [(i,) for i in range(500)]
    kept, truncated = split_truncated(rows, row_limit=1000)
    assert truncated is False
    assert len(kept) == 500


def test_恰好_row_limit_行不算截断() -> None:
    # 恰好取到 row_limit 行——没有那第 row_limit+1 行的探针，说明结果就是这么多
    rows = [(i,) for i in range(1000)]
    kept, truncated = split_truncated(rows, row_limit=1000)
    assert truncated is False
    assert len(kept) == 1000


def test_空结果() -> None:
    kept, truncated = split_truncated([], row_limit=1000)
    assert kept == []
    assert truncated is False
