"""超时中断的分类：工单 011 验收 ②——错误里要带"超时上限来自哪个配置项"。

口径来自 safety §4.1/§4.3：MySQL 的 `SET SESSION MAX_EXECUTION_TIME` 到点自杀报 errno 3024，
PG 的 `SET LOCAL statement_timeout` 报 SQLSTATE 57014。分类器把这两种源库原文都翻成 QueryTimeout，
detail 点名上限是数据源级 `timeout_ms`（L4）还是全局 `AIWEB_QUERY__TIMEOUT_MS`——
因为用户"该改哪一格"完全取决于这个来源。期望值来自文档点名的错误号，不是代码凑的。
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import DBAPIError

from app.core.errors import QueryTimeout
from app.services.nl2sql.executor import classify_source_error


class _Orig(Exception):
    def __init__(self, *args: object) -> None:
        super().__init__(*args)


def _dbapi(orig: BaseException) -> DBAPIError:
    return DBAPIError("SELECT ...", None, orig)  # type: ignore[arg-type]


def test_mysql_超时_3024_归_query_timeout_且点名全局配置() -> None:
    exc = _dbapi(
        _Orig(3024, "Query execution was interrupted, maximum statement execution time exceeded")
    )
    with pytest.raises(QueryTimeout) as caught:
        classify_source_error(exc, timeout_ms=15000, timeout_source="AIWEB_QUERY__TIMEOUT_MS")
    # detail 里必须出现那个键名，验收 ② 钉的就是这句话
    assert "AIWEB_QUERY__TIMEOUT_MS" in str(caught.value.detail)
    assert "15000" in str(caught.value.detail)


def test_pg_超时_57014_归_query_timeout_且点名数据源级() -> None:
    exc = _dbapi(_Orig("57014", "canceling statement due to statement timeout"))
    with pytest.raises(QueryTimeout) as caught:
        classify_source_error(exc, timeout_ms=8000, timeout_source="data_sources.timeout_ms")
    assert "data_sources.timeout_ms" in str(caught.value.detail)
    assert "8000" in str(caught.value.detail)


def test_非超时错误原样抛出不被吞() -> None:
    exc = _dbapi(_Orig(1146, "Table 'x' doesn't exist"))
    try:
        classify_source_error(exc, timeout_ms=15000, timeout_source="AIWEB_QUERY__TIMEOUT_MS")
    except QueryTimeout:
        raise AssertionError("语法/对象错误不该被误判成超时") from None
    except BaseException:
        return  # 原样抛出即达标
    raise AssertionError("非超时错误应原样抛出，不该静默返回")
