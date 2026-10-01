"""PG 侧类型归一（工单 023 验收 1 立项，`docs/metadata-model.md` §9 指名的落点；工单 024 接线）。

两个**纯函数**，都不连库：
- `normalize()`：吃 PG 类型原料（`format_type` 的人话名、`pg_type.typname` 的下划线数组名、
  带 `generated always` 子句的定义），吐归一后的 `data_type`。
- `modifiers()`：§8.2 C 那条 `modifiers` CASE 的 Python 版，吐
  `(char_length, num_precision, num_scale)`（原文引用的 `facts` 别名在 §8.2 里不存在，见函数注释）。

**所有映射字面都在 `type_normalize.py` 这一处**；这里只做 PG 特有的原料**解析**
（多词人话名、下划线数组、`[]` 数组、`serial`、生成列子句），解析出的基名交给共享的
`map_base` + `compose`，本模块不写一份规范字面。
"""

from __future__ import annotations

import re
from typing import Final

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


def normalize(raw: str, *, is_enum: bool = False) -> str:
    """PG 类型原料 → 归一 `data_type`。期望值全部来自 §9 / roadmap 验收 6 那张清单。

    `is_enum` 是 §8.2 C 的 `typtype='e'` 那一支（抽取器传 `enum_values IS NOT NULL` 的结果）：
    PG 的枚举是**独立的类型对象**，`format_type` 打出来的是它的名字（`order_status`），
    文本里没有"这是枚举"的痕迹——不靠这个旗标就只能把类型名当 `data_type` 落库，
    于是跨方言比较时 MySQL 的 `enum` 对上 PG 的 `order_status`，正是 §9 要避免的"同一语义两种字面"。
    """
    if is_enum:
        return tn.ENUM
    cleaned = _strip_clause(raw)
    depth, elem = _split_array(cleaned)
    base, params = _split_params(elem)
    canonical = tn.map_base(tn.PG_SYNONYMS, base)
    return tn.compose(canonical, params, depth)


# §8.2 C 的 modifiers 白名单，原文逐字：`t.typname IN ('varchar','bpchar','numeric')`。
_MODIFIER_TYPENAMES: Final = frozenset({"varchar", "bpchar", "numeric"})


def modifiers(typname: str, raw: str) -> tuple[int | None, int | None, int | None]:
    """§8.2 C 的 `modifiers` 那一格 → `(char_length, num_precision, num_scale)`。

    原文那条 CASE 写的是 `ARRAY[(a.atttypmod-4), facts.numeric_scale]`，而 **`facts` 这个别名
    在 §8.2 的 FROM 里不存在**（文档笔误，照抄会 42P01）。这里把判定和取值范围原样搬进 Python，
    原料换成同一行已经 SELECT 出来的 `format_type` 文本：

    - varchar/char：`atttypmod` 是"长度 + 4"，而 `format_type` 打出来的就是那个长度本身
      （本机 PG 18.6 实测：`format_type('character varying'::regtype, 4+64)` →
      `character varying(64)`），所以"减 4"这一步在文本口径下没有对应物，不需要做。
    - numeric：两个参数都会打全，**scale=0 也打**（实测 `numeric(10)` → `numeric(10,0)`），
      与 MySQL 侧 `decimal(10)` 归一成 `numeric(10,0)` 同一口径。
    - 判定按 `typname` 而不是按文本：数组列的 `typname` 是 `_varchar` / `_numeric`，落在白名单
      之外 → 三个值全 NULL。这不是省事，§8.2 那条 CASE 就是这个判定——`varchar(64)[]` 里的
      `(64)` 属于元素而不属于列，给它填 `char_length=64` 会让"这列能存多长"变成假话。
    """
    if typname not in _MODIFIER_TYPENAMES:
        return None, None, None
    depth, elem = _split_array(_strip_clause(raw))
    if depth:
        return None, None, None
    _, params = _split_params(elem)
    if params is None:
        return None, None, None
    parts = params.split(",")
    if typname == "numeric":
        return None, _num(parts[0]), _num(parts[1]) if len(parts) > 1 else None
    return _num(parts[0]), None, None


def _num(token: str) -> int | None:
    """括号里的数字：`format_type` 只会打非负整数，个别自建类型除外，那一路回 None 而不是猜。"""
    token = token.strip()
    return int(token) if token.isdigit() else None
