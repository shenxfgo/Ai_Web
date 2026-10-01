"""同步这一路的源方言驱动表：按 kind 分发，以及 kind 绑定的那两样东西成不成对。

期望值出处：
- 工单 024 验收 2 原文——"501 那一条的判定从'非 mysql 就抛'改成'未知 kind 才抛'，用例钉住
  `kind='postgres'` 不再命中 `NotImplementedSource`，且未知 kind 仍抛（不许顺手放宽成不抛）"。
- `docs/metadata-model.md` §2.2 的 `kind` CHECK 只认 mysql/postgres（006 的
  `test_kind_只认_mysql_与_postgres` 钉过），所以 §9 驱动表该有的两行就是这两行。
- 两处口径必须一致那条（`docs/verification.md` §1.2 末注 as-built(006)）：范围过滤住在源配置里，
  006 探测与 007 抽取共用同一个 `table_scope_filter`，而它渲染出的列写法必须认得下游那条查询。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import pytest

from app.core.errors import NotImplementedSource
from app.extractor import mysql as mysql_extractor
from app.extractor import postgres as pg_extractor
from app.extractor.base import ConnectionSpec
from app.extractor.postgres import PostgresExtractor
from app.models.datasource import DataSource
from app.services.datasource_service import table_scope_filter
from app.services.sync_service import _DIALECTS, _dialect_for, _extractor_for

SPEC = ConnectionSpec(host="127.0.0.1", port=3306, user="aiweb_ro", password="p")


def _ds(**overrides: object) -> DataSource:
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


# ------------------------------------------------------------------ 验收 2：只丢未知 kind


def test_postgres_有方言可用_不再命中_not_implemented() -> None:
    """P2 那句"PG 登记得了、同步必炸 501"的缺口在这一格关掉。

    只构造不连接：`__init__` 是惰性的（引擎到第一次 `_rows` 才建），所以这一条不需要 PG 在场，
    也就不会因为演示源没建起来而 skip 掉——skip 掉的正好是验收 2 要钉的那一句。
    """
    extractor = _extractor_for(_ds(kind="postgres"), SPEC)
    assert isinstance(extractor, PostgresExtractor)


def test_mysql_仍然走_mysql_方言() -> None:
    """回归面：改判定之前 mysql 是唯一能过的分支，改之后它不能变成别的东西。"""
    assert type(_extractor_for(_ds(kind="mysql"), SPEC)).__name__ == "MySQLExtractor"


@pytest.mark.parametrize("kind", ["oracle", "mssql", "MySQL", ""])
def test_未知_kind_照旧抛_不许放宽成不抛(kind: str) -> None:
    """§2.2 的 CHECK 只认两个值，所以这里的四种都到不了库；但服务层不能依赖那道 CHECK。

    大小写那条（`"MySQL"`）是故意放的：`kind` 到了这一层是没被规范过的文本，放宽成
    "认不出就当 mysql"会把一条登记错误的源抽成另一个方言的查询。
    抛的必须是 `NotImplementedSource`（HTTP 501 + `not_implemented`），不是 `KeyError`（500）。
    """
    with pytest.raises(NotImplementedSource) as caught:
        _extractor_for(_ds(kind=kind), SPEC)
    assert caught.value.code == "not_implemented"


def test_未知_kind_的文案带着那个_kind() -> None:
    """501 是唯一能自救的入口：用户看到"kind=oracle"才知道是登记页选错了源类型。"""
    with pytest.raises(NotImplementedSource, match="kind=oracle"):
        _dialect_for("oracle")


# ------------------------------------------------------------- kind 绑定的两样东西成不成对

# 各条 B 查询的构造器：测试自己按 kind 查表，不从 `_DIALECTS` 里拿——从被测那张表里读出的
# 构造器去验同一张表，红了也只是"两格一起改错了"。
TABLES_SQL: dict[
    str, Callable[[str, str | None, Mapping[str, str]], tuple[str, dict[str, object]]]
] = {
    "mysql": mysql_extractor.build_sql_tables,
    "postgres": pg_extractor.build_sql_tables,
}


def test_驱动表覆盖了_kind_check_认的两种源() -> None:
    """§2.2 的 CHECK（006 钉过）与 §9 的驱动表必须是同一套名字，少一个就是 500 或 501。"""
    assert set(_DIALECTS) == {"mysql", "postgres"}


@pytest.mark.parametrize("kind", sorted(_DIALECTS))
def test_范围条件的列写法在这个方言的_b_查询里认得(kind: str) -> None:
    """`scope_column` 必须是该方言 B 条自己声明的别名，否则同步一开就撞 42703 / 1054。

    这是"表名列写法"与"方言"之间的唯一耦合点：`table_scope_filter` 拿 `column` 参数渲染，
    真 PG 抽取器用的是 `c.relname`（`pg_class` 的别名），MySQL 用 `t.table_name`。
    把 PG 那一格写成 `t.table_name`，`run_sync` 照发不误，报错的却是源库——
    所以这里断的是**渲染出来的片段逐字出现在那条 B 查询里**，不是两处字符串相等。
    """
    dialect = _dialect_for(kind)
    scope_sql, scope_params = table_scope_filter(
        _ds(kind=kind, include_tables=["order_main", "v_daily%"], exclude_tables=["\\_%"]),
        column=dialect.scope_column,
    )
    assert scope_sql, "没有条件就没有可断的对象，这条用例就空转了"
    built, params = TABLES_SQL[kind]("ai_web_demo", scope_sql, scope_params)
    assert scope_sql in built, f"{kind} 的 B 查询里没有这个别名：{scope_sql}"
    # 用户填的表名一个都不许出现在语句文本里（006 的注入面口径，只走绑定参数）
    assert params == {"schema": "ai_web_demo", **scope_params}
    for name in ("order_main", "v_daily"):
        assert name not in built
