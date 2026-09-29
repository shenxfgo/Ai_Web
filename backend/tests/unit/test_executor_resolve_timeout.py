"""超时上限与来源的判定：工单 011 验收 ② 的"来源"半边。

审查抓出的口径：工单说"数据源级优先、缺省落全局"，如果来源判定留给调用方传字符串，
012 一撒谎验收 ② 就假过。所以判定收进 `resolve_timeout(row)` 纯函数——
`DataSource.timeout_ms` 是 NOT NULL 列（server_default=15000），正常登记行走数据源档；
列上缺值（手插行/旧行）才落全局，期望值口径来自 schema 而不是代码凑的。
"""

from __future__ import annotations

from app.models.datasource import DataSource
from app.services.nl2sql.executor import resolve_timeout


def _row(**kw: object) -> DataSource:
    base: dict[str, object] = {
        "id": 1,
        "name": "t",
        "kind": "mysql",
        "host": "h",
        "port": 3306,
        "connect_user": "u",
        "secret_enc": b"",
    }
    base.update(kw)
    return DataSource(**base)  # type: ignore[arg-type]


def test_列上有值走数据源档且点名这一格() -> None:
    ms, source = resolve_timeout(_row(timeout_ms=300))
    assert ms == 300
    assert source == "data_sources.timeout_ms"


def test_列上缺值落全局缺省且点名配置键() -> None:
    # timeout_ms 没设（手插行/未 flush 的内存对象）→ 落 Settings.query.timeout_ms
    ms, source = resolve_timeout(_row())
    assert source == "AIWEB_QUERY__TIMEOUT_MS"
    # 值必须是正数：落到全局后仍然要能进 SET 语句，0 或负数会让 MAX_EXECUTION_TIME 语义反转
    assert ms > 0
