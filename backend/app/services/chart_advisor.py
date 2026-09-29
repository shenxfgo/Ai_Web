"""结果集 → 图表选型（architecture §4.2 的确定性规则，不用 LLM）。

**输入是 executor 序列化之后的行**（safety §4.3：`Decimal`→字符串、`datetime`→ISO 字符串），
不是数据库原始值。所以数值列/时间列的判定必须在字符串形状上做。

输出是 metadata-model §2.7 那个**自有形状**，不是 `EChartsOption`：选型是领域判断，
option 结构是渲染细节，绑在一起的话换图表库就要动选型函数和它的单测。翻译成 ECharts
的那一层归 P8（口径见 architecture §6.1 的 as-built）。
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChartSpec:
    """图表建议。`fallback` 恒为 `table`——类型判断永远会错，逃生门必须留（§4.2 规则 6）。"""

    type: str
    x: str | None
    series: tuple[str, ...]
    title: str | None = None
    # 类别过多时的收敛参数：§4.2 规则 3 的"top10 + 其他"、pie 的"series ≤6 其余归其他"。
    # 由渲染层执行折叠——本模块不碰数据行，只说"该折成几段"。
    top_n: int | None = None
    other_label: str | None = None
    fallback: str = "table"


_MAX_CHART_ROWS = 200
"""超过这个行数不画图（verification §2.1）：点太密时图比表更难读。"""

_WIDE_COLUMNS = 8
"""§4.2 规则 5 的"列数 ≥8 → table"。"""

_CATEGORY_NDV_MAX = 12
"""类别轴的上限（§4.2 规则 3 的 NDV≤12，2026-09-29 拍板以此为准，architecture 原文的 20 作废）。"""

_TOP_N_WHEN_MANY = 10
"""NDV>12 时仍画 bar，但只取 top10 + "其他"（§4.2 的 as-built 补齐那条）。"""

_PIE_SERIES_MAX = 6
"""§4.2 规则 3 的 pie 分支：series ≤6，其余归"其他"。"""

_RATIO_HINT = re.compile(r"ratio|pct|percent|share|rate", re.IGNORECASE)
"""§4.2 规则 3 的列名线索。"""

_SCATTER_MIN_ROWS = 30
"""§4.2 规则 4：散点要 ≥30 行才谈得上相关性。"""

_DISTINCT_RATIO_MIN_ROWS = 20
"""规则 5 的 `distinct/rows > 0.9` 参与判定的最小样本量（012 拍板，理由见使用处注释）。"""

_DISTINCT_RATIO_MAX = 0.9
"""§4.2 规则 5 的比值上限：首列几乎每行都唯一 → 那不是可聚合的类别轴。"""


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def is_number(value: Any) -> bool:
    """序列化后的数值长什么样：`Decimal` 成了字符串（safety §4.3），所以字符串也要认。

    两支不认：`bool` 是 `int` 的子类，但真/假列不是数值轴；**非有限值**（`NaN`/`Inf`，
    MySQL 的 `0/0` 与 `POW(10,400)` 会真吐出来，而 `float()` 认这两个字面量）也不认——
    一格 `NaN` 就能把整列的合计毒成 `NaN`，让"合计≈100 判饼图"和摘要里的 sum/avg 全废。
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return False
        return math.isfinite(parsed)
    return False


def numeric_column_slots(
    columns: Sequence[str], rows: Sequence[Sequence[Any]]
) -> list[tuple[int, str]]:
    """非空值**全部**像数值的列，返回 `(下标, 列名)`。混进一个 `'线上'` 就不算数值列。

    带下标是因为 `SELECT a, a FROM t` 是合法 SQL，而 `columns.index("a")` 只会回到第一次
    出现的那一列——按名字取值会把"a 的第二种算法"当成第一种，摘要表里那一行的 sum/min/max
    全是错值，而且错得看不出来。
    """
    out: list[tuple[int, str]] = []
    for i, name in enumerate(columns):
        present = [row[i] for row in rows if i < len(row) and not _is_blank(row[i])]
        if present and all(is_number(v) for v in present):
            out.append((i, name))
    return out


def has_duplicate_columns(columns: Sequence[str]) -> bool:
    """列名有没有重复。`ChartSpec` 的 `x`/`series` 存的是**列名**（metadata-model §2.7），
    重名时这个形状表达不出"哪一列"——渲染层拿到的 `x="a"` 是第几个 `a`？没有答案。
    所以这不是"保守起见不画图"，而是"该形状在此输入下无定义"。
    """
    return len(set(columns)) != len(columns)


# 序列化后的时间形态：date 是 "2024-01-31"，datetime 是 "2024-01-31T09:00:00"
# （safety §4.3 走 `isoformat()`）。`YYYY-MM` 也算——月度聚合出来的列常是这种。
_TEMPORAL = re.compile(r"^\d{4}-\d{2}(-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?)?$")


def _is_temporal(value: Any) -> bool:
    return isinstance(value, str) and bool(_TEMPORAL.match(value.strip()))


def _is_temporal_column(rows: Sequence[Sequence[Any]], index: int) -> bool:
    present = [row[index] for row in rows if index < len(row) and not _is_blank(row[index])]
    return bool(present) and all(_is_temporal(v) for v in present)


def advise_chart(*, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> ChartSpec:
    """按 §4.2 的决策表选一种图表；纯函数，不碰库也不改写行。"""
    if len(rows) == 1 and len(columns) == 1:
        return ChartSpec(type="kpi", x=None, series=(columns[0],), title=None)

    # 五条"不画图"的下线条件：§4.2 规则 5（列数≥8、首列几乎每行唯一）+ verification §2.1 的
    # "行数>200""全 NULL 列"，外加"单行多列"和"列名重复"这两条形状本身无定义的。
    # 顺序在选型之前：图比表难读的时候，选错类型的代价是"看不清"，不选只是"没图"。
    if len(rows) < 2:
        # 单行多列没有 x 轴可画，而"一行两列"里挑一列当系列纯属猜。
        return _table_only(columns)
    if len(rows) > _MAX_CHART_ROWS:
        return _table_only(columns)
    if len(columns) >= _WIDE_COLUMNS:
        return _table_only(columns)
    if has_duplicate_columns(columns):
        return _table_only(columns)
    numeric = [name for _idx, name in numeric_column_slots(columns, rows)]
    if not numeric:
        # 一列数值都凑不出来（全 NULL、或全是文本），任何图都会画成"把文本当坐标"。
        return _table_only(columns)

    first = columns[0]
    if _is_temporal_column(rows, 0):
        # §4.2 规则 2 的时间那一支。多数值列直接出 multi-line（多条系列），
        # 而**不做转长表**——stacked bar 要的是重塑数据，那是渲染层的活（口径见工单 012）。
        series = tuple(c for c in numeric if c != first)
        if series:
            return ChartSpec(type="line", x=first, series=series, title=None)
    elif first not in numeric:
        # §4.2 规则 3：首列是类别轴。
        values = [row[0] for row in rows if not _is_blank(row[0])]
        ndv = len(set(values))
        series = tuple(c for c in numeric if c != first)
        if series:
            if _is_share(series[0], rows, columns.index(series[0])):
                return ChartSpec(
                    type="pie",
                    x=first,
                    series=(series[0],),
                    top_n=_PIE_SERIES_MAX,
                    other_label="其他",
                )
            return ChartSpec(
                type="bar",
                x=first,
                series=series,
                top_n=_TOP_N_WHEN_MANY if ndv > _CATEGORY_NDV_MAX else None,
                other_label="其他" if ndv > _CATEGORY_NDV_MAX else None,
            )
    else:
        # 首列是数值：要么是两个数值列的散点（规则 4），要么它是"整数序数"当轴（规则 2）。
        others = tuple(c for c in numeric if c != first)
        has_category_axis = any(
            c != first and c not in numeric and not _is_temporal_column(rows, columns.index(c))
            for c in columns
        )
        if others and not has_category_axis and len(rows) >= _SCATTER_MIN_ROWS:
            return ChartSpec(type="scatter", x=first, series=others, title=None)
        # 规则 5 的 `distinct/rows > 0.9`：**只在行数 ≥20 时参与判定**。
        # 小样本的比值天然接近 1（12 行 12 个月就是 1.0），拿它当下线条件会把
        # "全年 12 个月各多少"这种最典型的问句判成"不画图"。这条阈值语义是 012 拍的，
        # 因为 §4.2 原文只给了比值没给样本量前提。
        distinct = len({row[0] for row in rows if not _is_blank(row[0])})
        if len(rows) >= _DISTINCT_RATIO_MIN_ROWS and distinct / len(rows) > _DISTINCT_RATIO_MAX:
            return _table_only(columns)
        if others:
            ndv = distinct
            return ChartSpec(
                type="bar",
                x=first,
                series=others,
                top_n=_TOP_N_WHEN_MANY if ndv > _CATEGORY_NDV_MAX else None,
                other_label="其他" if ndv > _CATEGORY_NDV_MAX else None,
            )

    return _table_only(columns)


def _column_values(rows: Sequence[Sequence[Any]], index: int) -> list[Any]:
    return [row[index] for row in rows if index < len(row) and not _is_blank(row[index])]


def _is_share(name: str, rows: Sequence[Sequence[Any]], index: int) -> bool:
    """这一列是不是"份额"：列名带 ratio/pct/share/rate，或者非空值合计≈100（§4.2 规则 3）。

    合计的容差取 ±1：百分比按两位小数存出来的列，加总常是 99.99 或 100.01，
    差一分就说"这不是占比"会在真语料上随机翻车。
    """
    if _RATIO_HINT.search(name):
        return True
    values = [float(v) for v in _column_values(rows, index) if is_number(v)]
    return bool(values) and abs(sum(values) - 100) <= 1


def _table_only(columns: Sequence[str]) -> ChartSpec:
    return ChartSpec(type="table", x=None, series=tuple(columns), title=None)
