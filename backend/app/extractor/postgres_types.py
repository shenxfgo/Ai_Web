"""PG 侧类型归一（工单 023 验收 1，`docs/metadata-model.md` §9 指名的落点）。

`normalize()` 是**纯函数**：吃 PG 类型原料（`format_type` 的人话名、`pg_type.typname` 的
下划线数组名、带 `generated always` 子句的定义），吐归一后的 `data_type`。它不连库——
PG 抽取器本身是工单 024 的活，本片只交付这个函数 + 用例。

**所有映射字面都在 `type_normalize.py` 这一处**；这里只做 PG 特有的原料**解析**
（多词人话名、下划线数组、`[]` 数组、`serial`、生成列子句），解析出的基名交给共享的
`map_base` + `compose`，本模块不写一份规范字面。
"""

from __future__ import annotations

import re

from app.extractor import type_normalize as tn

# `numeric(10,2) generated always as (a*b)` / `int generated always as identity`：
# 归一只认类型那半截，generated 子句是列属性（`generated` 标志在抽取器里另落）。
_GENERATED_RE = re.compile(r"\s+generated\s+always\b.*$", re.S)
_COLLATE_RE = re.compile(r"\s+collate\s+\S+")


def _strip_clause(raw: str) -> str:
    s = " ".join(raw.lower().split())
    s = _GENERATED_RE.sub("", s)
    s = _COLLATE_RE.sub("", s)
    return s.strip()


def _split_array(elem: str) -> tuple[int, str]:
    """拆出数组层级与元素部分。

    两种原料形态都要认：人话的 `integer[]` / `character varying(64)[]`（按尾部 `[]` 计数），
    和 `pg_type.typname` 的下划线名 `_int4` / `_text`（一层数组）。
    """
    if elem.startswith("_"):
        return 1, elem[1:]
    depth = 0
    while elem.endswith("[]"):
        elem = elem[: -len("[]")]
        depth += 1
    return depth, elem.strip()


def _split_params(token: str) -> tuple[str, str | None]:
    """`character varying(64)` → (`character varying`, `64`)；无括号则参数为 `None`。"""
    open_idx = token.find("(")
    if open_idx < 0:
        return token.strip(), None
    base = token[:open_idx].strip()
    params = token[open_idx + 1 :].partition(")")[0].strip()
    return base, (params or None)


def normalize(raw: str) -> str:
    """PG 类型原料 → 归一 `data_type`。期望值全部来自 §9 / roadmap 验收 6 那张清单。"""
    cleaned = _strip_clause(raw)
    depth, elem = _split_array(cleaned)
    base, params = _split_params(elem)
    canonical = tn.map_base(tn.PG_SYNONYMS, base)
    return tn.compose(canonical, params, depth)
