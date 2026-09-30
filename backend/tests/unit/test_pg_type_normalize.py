"""`postgres_types.normalize()` 的归一用例——oracle 是 `docs/metadata-model.md` §9 那张清单。

roadmap §P3 验收 6 点名的七类原料：`_text / _int4 / _numeric / _timestamptz / _varchar / [] /
_jsonb`。§9 的映射样例（`character varying(64)→varchar(64)`、`timestamp with time zone→
timestamptz`、`double precision→float8`、`integer[]→int[]`）逐条抄成期望值，
`serial` 与 `generated always` 各有用例。纯函数，不连库。

值域一致性不在这里各写一份：`test_type_domain.py` 那张**同一张断言表**已经把两方言共用，
本文件只补 PG 独有的形状（数组归一后的 `[]`、`_typname`、serial、生成列）。
"""

from __future__ import annotations

import pytest

from app.extractor.postgres_types import normalize
from tests.unit.test_type_domain import assert_in_value_domain

# §9 PG 清单 + roadmap 验收 6 的七类原料 + §9 那四条映射样例。
# 期望值全部从文档抄，数组断出归一后的形状（integer[] → int[]）。
PG_CASES: list[tuple[str, str, str]] = [
    # §9 逐字点名的映射样例
    ("character varying", "character varying(64)", "varchar(64)"),
    ("timestamptz 人话", "timestamp with time zone", "timestamptz"),
    ("double precision", "double precision", "float8"),
    ("整型数组", "integer[]", "int[]"),
    # roadmap 验收 6 的七类原料（typname 形态的下划线数组名）
    ("_text", "_text", "text[]"),
    ("_int4", "_int4", "int[]"),
    ("_numeric", "_numeric", "numeric[]"),
    ("_timestamptz", "_timestamptz", "timestamptz[]"),
    ("_varchar", "_varchar", "varchar[]"),
    ("_jsonb", "_jsonb", "jsonb[]"),
    # 方括号[]形态（人话数组）——与下划线形态归一到同一个形状
    ("[] 方括号", "text[]", "text[]"),
    ("[] 变长带长度", "character varying(64)[]", "varchar(64)[]"),
    ("[] 双精度", "double precision[]", "float8[]"),
    # §9 点名的标量原料
    ("numeric 带精度", "numeric(10,2)", "numeric(10,2)"),
    ("jsonb", "jsonb", "jsonb"),
    ("timestamptz 简写", "timestamptz", "timestamptz"),
    # §9 的两样：serial / generated always
    ("serial", "serial", "int"),
    ("bigserial", "bigserial", "bigint"),
    ("smallserial", "smallserial", "smallint"),
    ("生成列整型", "int GENERATED ALWAYS AS IDENTITY", "int"),
    ("生成列 numeric", "numeric(10,2) GENERATED ALWAYS AS (a * b)", "numeric(10,2)"),
    ("生成列 timestamptz", "timestamptz GENERATED ALWAYS AS (now())", "timestamptz"),
    # 常见标量补全（跨方言值域共用的基名）
    ("integer", "integer", "int"),
    ("int4", "int4", "int"),
    ("bigint", "bigint", "bigint"),
    ("int8", "int8", "bigint"),
    ("smallint", "smallint", "smallint"),
    ("int2", "int2", "smallint"),
    ("boolean", "boolean", "bool"),
    ("bool", "bool", "bool"),
    ("real", "real", "float4"),
    ("text", "text", "text"),
    ("date", "date", "date"),
    ("json", "json", "json"),
    ("uuid", "uuid", "uuid"),
    ("timestamp 无时区", "timestamp without time zone", "timestamp"),
    ("bpchar", "character(5)", "char(5)"),
]


@pytest.mark.parametrize("label,raw,expected", PG_CASES, ids=[c[0] for c in PG_CASES])
def test_pg_归一映射(label: str, raw: str, expected: str) -> None:
    assert normalize(raw) == expected


@pytest.mark.parametrize("label,raw,expected", PG_CASES, ids=[c[0] for c in PG_CASES])
def test_pg_归一结果落在共享值域内(label: str, raw: str, expected: str) -> None:
    assert_in_value_domain(normalize(raw))


def test_pg_数组归一断出形状而非只去下划线() -> None:
    """验收 1：`integer[] → int[]` 这类——基名与数组形状**同时**归一。"""
    assert normalize("integer[]") == "int[]"
    # 二维数组保留层级
    assert normalize("integer[][]") == "int[][]"


def test_pg_归一不吞掉长度与精度() -> None:
    """§9：varchar(64)/numeric(10,2) 是值域里的参数化字面，归一不许把它们抹成裸名。"""
    assert normalize("character varying(64)") == "varchar(64)"
    assert normalize("numeric(10,2)") == "numeric(10,2)"


def test_pg_serial_与_generated_always_各是一条独立原料() -> None:
    """验收 1：serial / generated always 各有用例——这里再钉一遍它们的语义。"""
    # serial 是伪类型，落库物理上是 integer；归一取物理基名
    assert normalize("serial") == "int"
    # generated always 不是类型，是列属性；归一要从原料里剥出类型部分
    assert normalize("bigint GENERATED ALWAYS AS IDENTITY") == "bigint"
