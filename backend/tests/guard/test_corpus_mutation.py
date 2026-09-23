"""§8.3 变异测试：对每条放行用例施加 5 种攻击变异，变异后必须被拒。

这是防"规则只挡顶层"的探针——放行用例本身是绿的，不代表它的每个变体都被挡住。
变异一律另起一行施加，免得被用例结尾的行注释吃掉（那会让攻击文本变成注释，什么也测不出）。
"""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from app.services.sql_guard import SqlGuardError
from tests.guard.corpus import ALLOW_CASES, DEMO_TABLES, run

# 表名替换的靶子：白名单里真实存在的表，按长度降序，免得短名吃掉长名
_TABLE_NAMES = sorted((t.name for t in DEMO_TABLES), key=len, reverse=True)

_Mutator = Callable[[str], str]


def _append(fragment: str) -> _Mutator:
    return lambda sql: f"{sql}\n{fragment}"


def _swap_table(sql: str) -> str:
    for name in _TABLE_NAMES:
        swapped, n = re.subn(rf"\b{name}\b", "mysql.user", sql, count=1, flags=re.IGNORECASE)
        if n:
            return swapped
    return sql


def _to_outfile(sql: str) -> str:
    return re.sub(r"\bFROM\b", "INTO OUTFILE '/tmp/a.csv' FROM", sql, count=1, flags=re.IGNORECASE)


MUTATIONS: tuple[tuple[str, _Mutator, str], ...] = (
    ("追加第二条语句", _append("; DROP TABLE x"), "multi_statement"),
    ("表名换成系统表", _swap_table, "table_not_allowed"),
    ("追加锁定子句", _append("FOR UPDATE"), "locking_clause"),
    ("改成 INTO OUTFILE", _to_outfile, "into_outfile"),
    ("塞可执行注释", _append("/*!50100 DROP TABLE x */"), "executable_comment"),
)

# 靶子不存在的变异对该用例无意义（例如无表查询换不了表名），跳过而不是假装通过
_APPLICABLE: dict[_Mutator, re.Pattern[str] | None] = {
    _swap_table: re.compile(r"|".join(rf"\b{n}\b" for n in _TABLE_NAMES), re.IGNORECASE),
    _to_outfile: re.compile(r"\bFROM\b", re.IGNORECASE),
}


@pytest.mark.parametrize("sql", ALLOW_CASES, ids=[s[:38] for s in ALLOW_CASES])
@pytest.mark.parametrize("label,mutate,rule", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_放行用例的每种攻击变异都必须被拒(
    sql: str, label: str, mutate: _Mutator, rule: str
) -> None:
    probe = _APPLICABLE.get(mutate)
    if probe is not None and not probe.search(sql):
        pytest.skip(f"该用例没有 {label} 的靶子")
    mutated = mutate(sql)
    assert mutated != sql, f"{label} 没有改变语料，这条测试是空的"
    with pytest.raises(SqlGuardError) as ei:
        run(mutated)
    assert ei.value.rule_id == rule, f"{mutated!r} 被拒了，但原因不是 {label}"
