"""工单 023 验收 3 的那张**断言表**——两方言共用的归一值域，住在这一个文件里。

为什么单独成模块（而不是各测试文件各写一份）：`docs/metadata-model.md` §9 的 oracle
要"两方言的 `data_type` 值域由同一张断言表钉住"，全仓不许出现第二份映射字面。这张表就是
那个"同一处"：`test_pg_type_normalize.py` 与 `test_mysql_type_normalize.py` 都从这里取
`assert_in_value_domain`，而下面的参数化用例把跨方言等价族逐族跑一遍。
（文件名以 `test_` 开头是有意的：这张表本身必须被收集执行，只当共享模块放就会被 pytest 漏掉。）

表里的期望值一律**从 §9 那张清单抄**（`_text/numeric(10,2)/varchar(64)/timestamptz/jsonb/
serial/generated always` + MySQL `decimal unsigned zerofill/enum/set/tinyint(1)/datetime(3)`），
不是照着实现回写的。
"""

from __future__ import annotations

import pytest

from app.extractor.mysql_types import normalize as my_normalize
from app.extractor.postgres_types import normalize as pg_normalize
from app.extractor.type_normalize import VALUE_DOMAIN

# §9 点名的、跨方言"同一语义必须归成同一字面"的等价族：
# 每一行是 (概念, PG 原料, PG 期望, MySQL 原料, MySQL 期望)。
# PG 与 MySQL 两列期望相等的那些族，就是"两方言共用同一结论"的可执行版本
# （tinyint(1)/boolean→bool 是工单已拍板、逐字落进 §9 的那条）。
CROSS_DIALECT_EQUIVALENCE: list[tuple[str, str, str, str, str]] = [
    ("布尔", "boolean", "bool", "tinyint(1)", "bool"),
    ("整型", "integer", "int", "int(11)", "int"),
    ("整型(内部名)", "int4", "int", "int", "int"),
    ("大整型", "bigint", "bigint", "bigint(20)", "bigint"),
    ("小整型", "smallint", "smallint", "smallint(6)", "smallint"),
    ("变长字符", "character varying(64)", "varchar(64)", "varchar(64)", "varchar(64)"),
    ("定长字符", "character(5)", "char(5)", "char(5)", "char(5)"),
    ("文本", "text", "text", "text", "text"),
    ("双精度", "double precision", "float8", "double", "float8"),
    ("单精度", "real", "float4", "float", "float4"),
    ("日期", "date", "date", "date", "date"),
    ("带时区时间戳", "timestamp with time zone", "timestamptz", "datetime(3)", "datetime(3)"),
    # 拍板（§9 as-built 023 补的一格）：任意精度小数两方言都归 numeric，
    # 括号跟各自的声明走（PG 写 numeric(10,2)、MySQL 写 decimal(12,2) → 同一基名）。
    ("任意精度小数", "numeric(10,2)", "numeric(10,2)", "decimal(12,2)", "numeric(12,2)"),
    ("任意精度小数(别名)", "decimal", "numeric", "numeric(10,2)", "numeric(10,2)"),
]


def assert_in_value_domain(normalized: str) -> None:
    """归一后的 `data_type` 必须落在共享值域内（§9 / roadmap 验收 6 的口径）。

    允许带长度/精度的参数化字面（`varchar(64)`）与数组后缀（`int[]`）：先剥掉这两层，
    剩下必须是某个规范基名，才算"落在值域内"。出现 `varchar(255) unsigned zerofill`
    这种带修饰的原文，就是归一没接上。
    """
    token = normalized
    depth = 0
    while token.endswith("[]"):
        token = token[:-2]
        depth += 1
    base = token.split("(", 1)[0] if "(" in token else token
    assert base in VALUE_DOMAIN, f"{normalized!r} 的基名 {base!r} 不在归一值域内"


@pytest.mark.parametrize(
    "concept,pg_raw,pg_expected,mysql_raw,mysql_expected",
    CROSS_DIALECT_EQUIVALENCE,
    ids=[row[0] for row in CROSS_DIALECT_EQUIVALENCE],
)
def test_两方言把同一语义归成同一结论(
    concept: str, pg_raw: str, pg_expected: str, mysql_raw: str, mysql_expected: str
) -> None:
    """这张表就是"值域一致"的可执行断言：同族跨方言的期望值相等。"""
    assert pg_normalize(pg_raw) == pg_expected, concept
    assert my_normalize(mysql_raw) == mysql_expected, concept
    # 两方言共用同一结论的那些族，字面必须一模一样
    if pg_expected == mysql_expected:
        assert pg_normalize(pg_raw) == my_normalize(mysql_raw), concept
    assert_in_value_domain(pg_normalize(pg_raw))
    assert_in_value_domain(my_normalize(mysql_raw))


def test_decimal_不是归一值域的成员() -> None:
    """§9 as-built(023) 的拍板：任意精度小数两方言都归 `numeric`，值域里没有 `decimal`。

    这条钉的是"塌成同一个字面"这个**决定**本身：只靠上面等价族那两行，把 `DECIMAL`
    重新加回值域也不会红（两行都仍然自洽）——这一行会。
    """
    assert "decimal" not in VALUE_DOMAIN
    assert my_normalize("decimal(12,2)") == "numeric(12,2)"
    assert pg_normalize("decimal") == "numeric"
