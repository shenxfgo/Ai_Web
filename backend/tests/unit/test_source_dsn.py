"""源库连接串怎么拼：kind → 驱动、口令里的保留字符。

期望值口径：
- `docs/architecture.md` §9 驱动：元数据库 asyncpg；源 MySQL 走 asyncmy、源 PG 走 psycopg3
- `docs/roadmap.md` 分组 2 踩坑预警：DSN 必须对 user/password 做 quote_plus，
  否则 `p@ss` 里的那个 `@` 会被当成"用户名与主机之间的分隔符"
- `docs/metadata-model.md` §2.2 末注：MySQL 没有 catalog_name，库在 include_schemas 里
"""

from __future__ import annotations

from sqlalchemy import make_url

from app.models.datasource import DataSource
from app.services.source_manager import source_connect_args, source_url


def _row(**overrides: object) -> DataSource:
    base: dict[str, object] = {
        "name": "demo",
        "kind": "mysql",
        "host": "127.0.0.1",
        "port": 3306,
        "catalog_name": "",
        "connect_user": "aiweb_ro",
        "secret_enc": b"x",
        "created_by": 1,
    }
    return DataSource(**{**base, **overrides})  # type: ignore[arg-type]


def _dsn(row: DataSource) -> str:
    """渲染成含明文口令的串：只在断言"能不能读回来"时才需要它可见。"""
    return source_url(row, "whatever").render_as_string(hide_password=False)


def test_mysql走asyncmy_pg走psycopg() -> None:
    """两个驱动都不是元数据库那个 asyncpg：拿错的话连不上，但报错长得像"口令错了"。"""
    assert make_url(_dsn(_row(kind="mysql"))).drivername == "mysql+asyncmy"
    assert make_url(_dsn(_row(kind="postgres", catalog_name="warehouse"))).drivername == (
        "postgresql+psycopg"
    )


def test_库名对mysql是schema对pg才是database() -> None:
    """metadata-model §2.2 末注：MySQL 的库走 include_schemas，所以串里不该带 database。"""
    assert make_url(_dsn(_row(kind="mysql"))).database is None
    assert make_url(_dsn(_row(kind="postgres", catalog_name="warehouse"))).database == "warehouse"


def test_口令里的at井号百分号不撑破连接串() -> None:
    """@ # / % 和一个汉字混进口令时，串必须还能被原样读回来。

    这条是 quote_plus 的真正落点：URL.create 会替我们转义，手搓 f-string 不会。
    断言写在"读回来相等"上而不是"字符串里有 %40"上——前者才是我们真正依赖的性质。
    """
    secret = "p@ss#词/100%"
    dsn = source_url(_row(), secret).render_as_string(hide_password=False)
    parsed = make_url(dsn)
    assert parsed.password == secret
    assert parsed.username == "aiweb_ro"
    assert parsed.host == "127.0.0.1" and parsed.port == 3306
    # 只准有一个裸 @（user 与 host 之间那个）：口令里再漏出一个就说明没转义
    assert dsn.count("@") == 1


def test_asyncmy的口令以utf8字节交给驱动() -> None:
    """asyncmy 拿到 str 口令时按 latin-1 编码，含非 ASCII 就当场 UnicodeEncodeError。

    那个异常不是 SQLAlchemyError，探测里的异常映射接不住，用户看到的是裸 500——
    而 500 的异常链里带着连接串。传 bytes 时驱动原样使用，非 ASCII 口令才可用。
    """
    args = source_connect_args(_row(kind="mysql"), "口令p@ss")
    assert args["password"] == "口令p@ss".encode()


def test_psycopg的口令不重复塞进connect_args() -> None:
    """psycopg3 自己处理 unicode；多塞一份只是给以后留个"两处口令可以不一样"的坑。"""
    assert "password" not in source_connect_args(_row(kind="postgres"), "口令")


def test_超时毫秒换算成秒且至少一秒() -> None:
    """connect_timeout 的单位是秒，而 §2.2 的 timeout_ms 是毫秒；填 100ms 会算出 0，
    0 在驱动里是"永不超时"，跟用户要的"最快砍断"正好相反。"""
    assert source_connect_args(_row(), "pw", timeout_ms=15_000)["connect_timeout"] == 15
    assert source_connect_args(_row(), "pw", timeout_ms=100)["connect_timeout"] == 1
    assert "connect_timeout" not in source_connect_args(_row(), "pw")
