"""`mysql_types.normalize()` 的归一用例 + `rows_to_columns()` 的接线（工单 023 验收 2）。

oracle 同样来自 `docs/metadata-model.md` §9：MySQL 那侧点名的原料是
`decimal unsigned zerofill` / `enum('a','b')` / `set` / `tinyint(1)`（已拍板 → `bool`）/
`datetime(3)` / 生成列 / `utf8mb4_0900_ai_ci` 容错。期望值从清单抄，不连库。

值域一致性不在此各写一份——`test_type_domain.py` 那张**同一张断言表**把两方言共用。
"""

from __future__ import annotations

import pytest

from app.extractor.mysql_types import normalize
from tests.unit.test_type_domain import assert_in_value_domain

# §9 MySQL 清单（含拍板后的 tinyint(1)→bool、decimal 一族归 numeric）+ 修饰剥离 + 排序规则容错。
MYSQL_CASES: list[tuple[str, str, str]] = [
    # §9 逐字点名的原料
    ("decimal unsigned zerofill", "decimal(10,0) unsigned zerofill", "numeric(10,0)"),
    ("decimal 无修饰", "decimal(18,2)", "numeric(18,2)"),
    ("numeric 别名", "numeric(10,2)", "numeric(10,2)"),
    ("dec 缩写", "dec(8,3)", "numeric(8,3)"),
    ("enum", "enum('a','b')", "enum"),
    ("enum 六值", "enum('pending','paid','shipped','completed','cancelled','refunding')", "enum"),
    ("set", "set('a','b')", "set"),
    ("datetime 精度", "datetime(3)", "datetime(3)"),
    ("tinyint(1)→bool 拍板", "tinyint(1)", "bool"),
    ("生成列 varchar", "varchar(200)", "varchar(200)"),
    ("8.0 排序规则容错", "varchar(64) COLLATE utf8mb4_0900_ai_ci", "varchar(64)"),
    # 整族：显示宽度不是语义，MySQL 8.0 已废弃，归一后统一剥掉
    ("int 显示宽度", "int(11)", "int"),
    ("bigint 显示宽度", "bigint(20)", "bigint"),
    ("smallint 显示宽度", "smallint(6)", "smallint"),
    # tinyint 非 (1)：窄整型，不塌成 bool，也不改基名（保留方言可辨识度，raw 仍在原文）
    ("tinyint(4)", "tinyint(4)", "tinyint"),
    ("mediumint", "mediumint(9)", "int"),
    # 变长字符保留长度——与 PG varchar(64) 同形状（值域共用）
    ("varchar 带长度", "varchar(255)", "varchar(255)"),
    ("char 带长度", "char(32)", "char(32)"),
    ("unsigned int", "int(10) unsigned", "int"),
    # 其余常见标量
    ("text", "text", "text"),
    ("date", "date", "date"),
    ("datetime 无精度", "datetime", "datetime"),
    ("timestamp", "timestamp", "timestamp"),
    ("json", "json", "json"),
    ("double", "double", "float8"),
    ("float", "float", "float4"),
    ("blob", "blob", "blob"),
]


@pytest.mark.parametrize("label,raw,expected", MYSQL_CASES, ids=[c[0] for c in MYSQL_CASES])
def test_mysql_归一映射(label: str, raw: str, expected: str) -> None:
    assert normalize(raw) == expected


@pytest.mark.parametrize("label,raw,expected", MYSQL_CASES, ids=[c[0] for c in MYSQL_CASES])
def test_mysql_归一结果落在共享值域内(label: str, raw: str, expected: str) -> None:
    assert_in_value_domain(normalize(raw))


# ---------------------------------------------------------------------------
# 接线：rows_to_columns 现在写"归一值 + 方言原文"（验收 2 的 raw 还在）
# ---------------------------------------------------------------------------


def _col_row(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "TABLE_NAME": "t",
        "COLUMN_NAME": "c",
        "ORDINAL_POSITION": 1,
        "DATA_TYPE": "varchar",
        "COLUMN_TYPE": "varchar(255)",
        "IS_NULLABLE": "YES",
        "COLUMN_DEFAULT": None,
        "EXTRA": "",
        "COLUMN_COMMENT": None,
        "CHARACTER_MAXIMUM_LENGTH": 255,
        "NUMERIC_PRECISION": None,
        "NUMERIC_SCALE": None,
        "COLLATION_NAME": "utf8mb4_general_ci",
        "enum_def": None,
    }
    base.update(over)
    return base


def test_rows_to_columns_写归一值并把方言原文留在_raw_data_type() -> None:
    from app.extractor.mysql import rows_to_columns

    (col,) = rows_to_columns([_col_row(DATA_TYPE="tinyint", COLUMN_TYPE="tinyint(1)")])
    assert col.data_type == "bool", "data_type 是归一值"
    assert col.raw_data_type == "tinyint(1)", "raw_data_type 必须仍是 COLUMN_TYPE 方言原文"


def test_rows_to_columns_的_decimal_unsigned_zerofill_归一后原文不失() -> None:
    from app.extractor.mysql import rows_to_columns

    (col,) = rows_to_columns(
        [
            _col_row(
                DATA_TYPE="decimal",
                COLUMN_TYPE="decimal(10,0) unsigned zerofill",
                NUMERIC_PRECISION=10,
                NUMERIC_SCALE=0,
            )
        ]
    )
    assert col.data_type == "numeric(10,0)", "拍板：任意精度小数归 numeric"
    # §9 的"raw 还在"那层保证不许破：修饰（unsigned/zerofill）只在 raw 里看得见
    assert col.raw_data_type == "decimal(10,0) unsigned zerofill"


def test_rows_to_columns_enum_列_归一取基名_原文带全取值() -> None:
    from app.extractor.mysql import rows_to_columns

    (col,) = rows_to_columns(
        [
            _col_row(
                DATA_TYPE="enum",
                COLUMN_TYPE="enum('a','b')",
                enum_def="enum('a','b')",
            )
        ]
    )
    assert col.data_type == "enum"
    assert col.raw_data_type == "enum('a','b')"
    assert col.enum_values == ("a", "b")
