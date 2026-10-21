"""第二层住进执行链路之后的**接线**（工单 037 验收 1/2/3 的桩件半边）。

为什么要有这一族而不只靠 live：验收 1 要断的是"**没走到取行那一步**"，验收 2 要断的是
"源库**一条 EXPLAIN 都没收到**"。真库上这两句话都没法直接观察——只能看见结果不对，
看不见"那一步有没有发生"。桩件把连接上收到过的语句**按顺序**记下来，"发过 EXPLAIN"与
"发过真 SQL"第一次成为两个可区分的事实（工单 037 验收 1 明写了这个要求）。

live 那一半证的是桩件证不了的：源库原话真的长那样、`EXPLAIN` 真的不执行、估行真的读得回来。
两边不互相替代，也不许拿一边冒充另一边。

期望值口径：
- 会话序列引用被测模块自己的 `session_statements`——本文件要断的是**次序**（第二层落在那四条
  之后、真 SQL 之前），不是那四条的字面；那四条的字面由 `test_executor_session.py` 钉。
- `EXPLAIN` 那一行的期望值是 `"EXPLAIN " + sql_final`，来自 safety §3.1 第一条。
- `1054 Unknown column` 那个 errno 与原文抄自实测
  `.scratch/p5-sql-guard-hardening/evidence/probe_explain037.txt`。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError

from app.models.datasource import DataSource
from app.services.nl2sql import executor
from app.services.nl2sql.executor import session_statements
from app.services.sql_guard import GuardRuleId, SqlGuardError
from app.settings import get_settings

SQL_FINAL = "SELECT id, name FROM customer LIMIT 5"
# 002 给演示账号的那两条。`USAGE ON *.*` 是豁免过的（grants_verdict 里唯一不报越界的 *.* 形状）
_GRANTS = (
    r"GRANT USAGE ON *.* TO `aiweb_ro`@`%`",
    "GRANT SELECT ON `ai_web_demo`.* TO `aiweb_ro`@`%`",
)
# 一条"引擎给了数、且没超默认阈值"的普通计划（`order_main` 全表扫描的实测估行）
_PLAN_WITH_ROWS: tuple[dict[str, Any], ...] = ({"rows": 30226, "Extra": ""},)


def _engine_error(errno: int, text: str) -> DBAPIError:
    """按驱动的形状造一发源库错误：errno 长在 `orig.args[0]` 上，`root_cause` 读的就是这一层。"""
    return DBAPIError("EXPLAIN …", None, Exception(errno, text))  # type: ignore[arg-type]


class _Result:
    """最小结果桩：只实现执行链路真读的那几个方法。"""

    def __init__(
        self,
        *,
        columns: tuple[str, ...] = (),
        rows: tuple[tuple[Any, ...], ...] = (),
        scalars: tuple[Any, ...] = (),
        mappings: tuple[dict[str, Any], ...] = (),
    ) -> None:
        self._columns = columns
        self._rows = rows
        self._scalars = scalars
        self._mappings = mappings

    def keys(self) -> list[str]:
        return list(self._columns)

    def fetchall(self) -> list[Any]:
        return list(self._rows)

    def scalars(self) -> list[Any]:
        return list(self._scalars)

    def mappings(self) -> list[dict[str, Any]]:
        return list(self._mappings)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._scalars)


class _Connection:
    """记语句的连接桩。收到什么就按计划回什么，计划外的一条都不许出现。"""

    def __init__(
        self,
        *,
        mappings: tuple[dict[str, Any], ...] = _PLAN_WITH_ROWS,
        explain_error: Exception | None = None,
    ) -> None:
        self.sent: list[str] = []
        self._mappings = mappings
        self._explain_error = explain_error

    async def __aenter__(self) -> _Connection:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execution_options(self, **_: Any) -> _Connection:
        return self

    async def execute(self, stmt: Any) -> _Result:
        sql = str(stmt)
        self.sent.append(sql)
        if sql == "show grants":
            return _Result(scalars=_GRANTS)
        if sql.startswith("SET "):
            return _Result()
        if sql.startswith("EXPLAIN "):
            if self._explain_error is not None:
                raise self._explain_error
            return _Result(mappings=self._mappings)
        if sql == SQL_FINAL:
            return _Result(columns=("id", "name"), rows=((7, "沈晓"),))
        raise AssertionError(f"连接收到了计划外的语句：{sql!r}")


class _Engine:
    def __init__(self, conn: _Connection) -> None:
        self._conn = conn
        self.disposed = 0

    def connect(self) -> _Connection:
        return self._conn

    async def dispose(self) -> None:
        self.disposed += 1


def _datasource() -> DataSource:
    return DataSource(
        id=1,
        name="stub",
        kind="mysql",
        host="127.0.0.1",
        port=3306,
        catalog_name="",
        connect_user="aiweb_ro",
        secret_enc=b"",
        row_limit=1000,
        timeout_ms=15000,
    )


async def _run(conn: _Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """把执行器的"怎么连"换成桩连接，其余（守卫后的 sql_final、会话序列、落盘）全是真的。"""
    engine = _Engine(conn)
    monkeypatch.setattr(executor, "create_source_engine", lambda *a, **k: engine)
    return await executor.execute_readonly(
        _datasource(),
        password="x",
        sql_final=SQL_FINAL,
        row_limit=1000,
        max_cell_chars=1000,
        result_dir=tmp_path,
    )


@pytest.fixture(autouse=True)
def query_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """把本片读的两格钉成测试值并走真实配置通道（同 conftest 的 jwt_secret：桩属性盖不住读次数）。

    默认值本身由 `test_settings.py` 那条断言管，这里要的是**测试不跟着开发者机器上的 .env 走**。
    """
    monkeypatch.setenv("AIWEB_QUERY__DRY_RUN", "true")
    monkeypatch.setenv("AIWEB_QUERY__MAX_EXPLAIN_ROWS", "50000000")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def _set_query(monkeypatch: pytest.MonkeyPatch, key: str, value: str) -> None:
    """改一格 query 配置并让 `get_settings` 的 `lru_cache` 失效——不失效就等于没改。"""
    monkeypatch.setenv(f"AIWEB_QUERY__{key}", value)
    get_settings.cache_clear()


async def test_第二层跑在只读会话建好之后_取行之前(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """safety §3.1 第二条的落点：`executor` 在第三层那四条 SET 之后、发真 SQL 之前调用第二层。

    断的是**整条序列**，不是"发过 EXPLAIN"这一句——发在只读会话之前（那时 `show grants` 已把事务
    开了）与发在取行之后（钱已经花了）都能满足"发过 EXPLAIN"，而两者都不是这一层的位置。
    """
    conn = _Connection()
    out = await _run(conn, tmp_path, monkeypatch)
    assert conn.sent == [
        "show grants",
        *session_statements("mysql", timeout_ms=15000),
        f"EXPLAIN {SQL_FINAL}",
        SQL_FINAL,
    ]
    assert out.row_count == 1


async def test_explain_自己被源库拒时真_sql_一个字都没发(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验收 1 的两半：被第二层拒 + **没走到取行那一步**（实测 errno 1054）。

    桩件这里能给出 live 给不出的那半句：`sql_final` 不在 `sent` 里。
    错误正文必须带源库原话，而且**带错误号**（`1054` + `Unknown column …`）——038 回喂模型要的是
    引擎那一整串，不是我们的转述。
    """
    conn = _Connection(explain_error=_engine_error(1054, "Unknown column 'nope' in 'field list'"))
    with pytest.raises(SqlGuardError) as caught:
        await _run(conn, tmp_path, monkeypatch)
    assert caught.value.rule_id == GuardRuleId.dry_run_engine_rejected
    text = str(caught.value)
    assert "1054" in text and "Unknown column 'nope' in 'field list'" in text, text
    assert SQL_FINAL not in conn.sent
    assert conn.sent[-1] == f"EXPLAIN {SQL_FINAL}"


async def test_估行读不出来时拒而不取行(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§3.2 的 `dry_run_unreadable` 在执行链路上的那一发：拒了，真 SQL 就没发出去。"""
    conn = _Connection(mappings=({"rows": None, "Extra": ""},))
    with pytest.raises(SqlGuardError) as caught:
        await _run(conn, tmp_path, monkeypatch)
    assert caught.value.rule_id == GuardRuleId.dry_run_unreadable
    assert SQL_FINAL not in conn.sent


async def test_max_explain_rows_被真读_同一条_sql_从放行翻成拒(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验收 3：这一格不是"定义点在场"，它真的被读——把阈值钉成一个很小的数，同一条 SQL 翻面。

    `30226` 是实测的普通全表扫描估行。默认阈值 5e7 下放行、`100` 下拒 `too_costly`，
    两次跑的是同一个桩、同一句 `sql_final`，中间只换了那一个键。
    """
    ok = await _run(_Connection(), tmp_path, monkeypatch)
    assert ok.row_count == 1

    _set_query(monkeypatch, "MAX_EXPLAIN_ROWS", "100")
    strict = _Connection()
    with pytest.raises(SqlGuardError) as caught:
        await _run(strict, tmp_path, monkeypatch)
    assert caught.value.rule_id == GuardRuleId.too_costly
    assert SQL_FINAL not in strict.sent


async def test_dry_run_关掉时源库一条_explain_都没收到而真_sql_照跑(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验收 2：正向断"没发"，不是断"结果为空"（033 验收 1 同一条纪律）。

    这一条同时是"分层独立有效"（roadmap §P5 验收 4）的**半边**证据：关掉第二层，
    第三层那四条 SET 一个字不少、顺序不动——第二层住不进第三层，也不是靠这一条临时通的。
    """
    _set_query(monkeypatch, "DRY_RUN", "false")
    conn = _Connection()
    out = await _run(conn, tmp_path, monkeypatch)
    assert not [sql for sql in conn.sent if sql.startswith("EXPLAIN")]
    assert conn.sent == ["show grants", *session_statements("mysql", timeout_ms=15000), SQL_FINAL]
    assert out.row_count == 1
