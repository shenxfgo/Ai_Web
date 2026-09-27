"""源库引擎工厂：把一个 DataSource 行 + 明文口令变成一个只读 async engine。

这里只管"怎么连"，不管"连上之后查什么"（那是 extractor/ 和 executor 的事）。
按 datasource 缓存连接池的部分等 011（只读执行切片）再上：今天的每个调用点
（test_connection）要的都是一次性连接，缓存它反而是泄漏。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.models.datasource import DataSource

# §9 的驱动表：源 MySQL 用 asyncmy，源 PG 用 psycopg3——都不是元数据库那个 asyncpg。
# 拿错 drivername 的报错长得像"口令不对"，所以这张表是显式的，不靠猜；新增 kind 必须先补这里
# （kind 由 §2.2 的 CHECK 与 DataSourceCreate 的 Literal 双重收口，漏补就是 KeyError → 500）。
_DRIVER_FOR_KIND = {"mysql": "mysql+asyncmy", "postgres": "postgresql+psycopg"}


def source_url(row: DataSource, password: str) -> URL:
    """连接串走 URL.create，不走 f-string 拼接。

    手搓 `f"{user}:{pw}@{host}"` 在口令含 @ # / % 时会把串撑破（roadmap 分组 2 的踩坑预警），
    URL.create 替我们做 quote，而且解回来是同一个口令——单测钉的就是这个往返。
    """
    return URL.create(
        drivername=_DRIVER_FOR_KIND[row.kind],
        username=row.connect_user,
        password=password,
        host=row.host,
        port=row.port,
        # MySQL 恒空串→不带 database（它的库在 include_schemas 里，metadata-model §2.2 末注）；
        # PG 的 catalog_name 就是目标库
        database=row.catalog_name or None,
    )


def source_connect_args(
    row: DataSource, password: str, *, timeout_ms: int | None = None
) -> dict[str, Any]:
    """交给 DBAPI connect() 的关键字参数。

    MySQL 这一路要显式覆盖口令：asyncmy 对 str 口令做 latin-1 编码，非 ASCII 会在客户端
    就抛 UnicodeEncodeError（不是 SQLAlchemyError，异常映射接不住 → 裸 500）。传 bytes
    时驱动原样使用，口令按 UTF-8 到达源库。
    """
    args: dict[str, Any] = {}
    if row.kind == "mysql":
        args["password"] = password.encode("utf-8")
    if timeout_ms is not None:
        # 驱动这一侧的 connect_timeout 单位是秒；不足 1 秒按 1 秒收，0 会被解释成"永不超时"
        args["connect_timeout"] = max(1, round(timeout_ms / 1000))
    return args


def create_source_engine(
    row: DataSource, password: str, *, timeout_ms: int | None = None
) -> AsyncEngine:
    """一次性 engine（NullPool：用完 dispose 就断，不留后台连接）。

    timeout_ms 落到 connect_timeout 上——不传的话 MySQL 握手卡住会一直挂着这个请求，
    而"测试连接"这个按钮的全部意义就是几秒内给人一个答复。
    """
    return create_async_engine(
        source_url(row, password),
        poolclass=NullPool,
        connect_args=source_connect_args(row, password, timeout_ms=timeout_ms),
    )
