"""MySQL 侧类型归一（工单 023 验收 2；与 §9 指名的 `postgres_types.normalize()` 对称的落点）。

`normalize()` 是**纯函数**：吃 `information_schema.COLUMNS.COLUMN_TYPE` 那种带修饰的原文
（`decimal(10,0) unsigned zerofill`、`varchar(64) collate utf8mb4_0900_ai_ci`、`enum('a','b')`、
`tinyint(1)`、`datetime(3)`），吐归一后的 `data_type`。方言原文由 `rows_to_columns()` 原样留在
`raw_data_type`（§9 的"raw 还在"那层保证不许破）。

映射字面同样只在 `type_normalize.py` 这一处；本模块只做 MySQL 特有的**解析**（修饰剥离、
排序规则容错、`tinyint(1)→bool` 这条已拍板结论），规范字面一律走共享 `map_base`/`compose`。
"""

from __future__ import annotations

import re

from app.extractor import type_normalize as tn

# 修饰词与排序规则后缀：`unsigned`/`zerofill`/`binary` 是数值/字符修饰，
# `character set x`/`collate x` 是字符序（8.0 的 `utf8mb4_0900_ai_ci` 就在这儿被容错掉）。
_MODIFIER_RE = re.compile(r"\s+(unsigned|zerofill|binary)\b")
_CHARSET_RE = re.compile(r"\s+character\s+set\s+\S+")
_COLLATE_RE = re.compile(r"\s+collate\s+\S+")

# 已拍板（§9）：`tinyint(1) → bool`。显示宽度 1 是 MySQL 表示布尔的唯一写法。
_BOOL_WIDTH: str = "1"


def _strip_modifiers(raw: str) -> str:
    s = " ".join(raw.lower().split())
    s = _CHARSET_RE.sub("", s)
    s = _COLLATE_RE.sub("", s)
    s = _MODIFIER_RE.sub("", s)
    return s.strip()


def _split_params(token: str) -> tuple[str, str | None]:
    """`decimal(10,0)` → (`decimal`, `10,0`)；`varchar` 这类无括号则参数为 `None`。"""
    open_idx = token.find("(")
    if open_idx < 0:
        return token.strip(), None
    base = token[:open_idx].strip()
    params = token[open_idx + 1 :].partition(")")[0].strip()
    return base, (params or None)


def normalize(raw: str) -> str:
    """MySQL `COLUMN_TYPE` 原文 → 归一 `data_type`。期望值全部来自 §9 那张清单。"""
    cleaned = _strip_modifiers(raw)
    base, params = _split_params(cleaned)
    canonical = tn.map_base(tn.MYSQL_SYNONYMS, base)
    # 拍板结论：tinyint(1) 归成 bool——但只认宽度 1，tinyint(4) 仍是窄整型 tinyint。
    if canonical == tn.TINYINT and params == _BOOL_WIDTH:
        canonical = tn.BOOL
    # MySQL 无数组类型，层级恒 0。
    return tn.compose(canonical, params, 0)
