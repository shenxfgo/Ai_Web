"""跨方言类型归一的**唯一映射源**（工单 023）。

`docs/metadata-model.md` §9 定的口径是：`meta_column.data_type` 存**归一值**、
`raw_data_type` 存**方言原文**。归一是纯函数、不连库，两方言共用同一张值域表——否则
会出现 MySQL 存 `varchar`、PG 存 `varchar(64)` 这种"同一语义两种字面"，卡片层就得再归一次。

这一层因此把**所有归一后的规范字面**（`int`/`bool`/`varchar`/`timestamptz`…）都收在
这一个模块里：`postgres_types.py` 与 `mysql_types.py` 只做方言原料的**解析**，然后调用这里的
`map_base` / `compose`，自己不写任何一份映射字面（工单 023 验收 3 的"全仓不许出现第二份映射
字面"）。两方言的 `data_type` 值域由测试侧 `tests/unit/_type_domain.py` 那张**同一张断言表**钉住。

拍板（已写进 §9）：`tinyint(1) → bool`。理由三条——① 归一的目的是跨方言可比，PG 的
`boolean` 归一后就是 `bool`，MySQL 若保留 `tinyint(1)` 会造出"同一语义两种字面"；②
`raw_data_type` 继续存方言原文，`tinyint(1)` 没丢；③ 两方言共用同一结论是工单已定口径。

拍板（已写进 §9，工单 023 开工后补的一格）：**任意精度小数两方言都归 `numeric`**，值域里没有
`decimal` 这个基名。PG 的 `numeric`/`decimal` 与 MySQL 的 `decimal`/`numeric`/`dec`/`fixed`
本是同一家族的方言语面，各留一个名字就是"同一语义两种字面"——卡片与 prompt 跨方言比较时还得再归一次。
方言原文照旧存在 `raw_data_type`，MySQL 那边写下的仍是 `decimal(12,2)`。
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# 归一后的规范基名——值域字面在**整个后端只住在这里这一处**。
# ---------------------------------------------------------------------------
BOOL: Final = "bool"
BYTEA: Final = "bytea"
SMALLINT: Final = "smallint"
INT: Final = "int"
BIGINT: Final = "bigint"
TINYINT: Final = "tinyint"
FLOAT4: Final = "float4"
FLOAT8: Final = "float8"
NUMERIC: Final = "numeric"
CHAR: Final = "char"
VARCHAR: Final = "varchar"
TEXT: Final = "text"
DATE: Final = "date"
TIME: Final = "time"
DATETIME: Final = "datetime"
TIMESTAMP: Final = "timestamp"
TIMESTAMPTZ: Final = "timestamptz"
JSON: Final = "json"
JSONB: Final = "jsonb"
UUID: Final = "uuid"
ENUM: Final = "enum"
SET: Final = "set"
BLOB: Final = "blob"

# `data_type` 的**值域**（规范基名全集）：`_type_domain.py` 与两个方言的 normalize 都从这里取值。
VALUE_DOMAIN: Final = frozenset(
    {
        BOOL,
        BYTEA,
        SMALLINT,
        INT,
        BIGINT,
        TINYINT,
        FLOAT4,
        FLOAT8,
        NUMERIC,
        CHAR,
        VARCHAR,
        TEXT,
        DATE,
        TIME,
        DATETIME,
        TIMESTAMP,
        TIMESTAMPTZ,
        JSON,
        JSONB,
        UUID,
        ENUM,
        SET,
        BLOB,
    }
)

# 哪些规范基名**保留**长度/精度括号。不在表里的（整型的显示宽度、enum/set 的取值清单、
# text/json 这类）归一时一律把括号丢掉——显示宽度不是语义（MySQL 8.0 已废弃它）。
PARAMETERIZED: Final = frozenset({CHAR, VARCHAR, NUMERIC, DATETIME, TIME, TIMESTAMP})

# ---------------------------------------------------------------------------
# 方言原料基名 → 规范基名。两份表都在这一处，值全用上面的常量，不重造字面。
# ---------------------------------------------------------------------------
PG_SYNONYMS: Final[dict[str, str]] = {
    "int2": SMALLINT,
    "smallint": SMALLINT,
    "smallserial": SMALLINT,
    "serial2": SMALLINT,
    "int4": INT,
    "integer": INT,
    "int": INT,
    "serial": INT,
    "int8": BIGINT,
    "bigint": BIGINT,
    "bigserial": BIGINT,
    "serial8": BIGINT,
    "bool": BOOL,
    "boolean": BOOL,
    "real": FLOAT4,
    "float4": FLOAT4,
    "float": FLOAT8,  # PG 的裸 float = double precision
    "float8": FLOAT8,
    "double precision": FLOAT8,
    "numeric": NUMERIC,
    "decimal": NUMERIC,  # PG 的 decimal 是 numeric 的别名
    "character varying": VARCHAR,
    "varchar": VARCHAR,
    "character": CHAR,
    "bpchar": CHAR,
    "char": CHAR,
    "text": TEXT,
    "date": DATE,
    "time": TIME,
    "time without time zone": TIME,
    "time with time zone": TIME,
    "timestamp": TIMESTAMP,
    "timestamp without time zone": TIMESTAMP,
    "timestamptz": TIMESTAMPTZ,
    "timestamp with time zone": TIMESTAMPTZ,
    "json": JSON,
    "jsonb": JSONB,
    "uuid": UUID,
    "bytea": BYTEA,
}

MYSQL_SYNONYMS: Final[dict[str, str]] = {
    "tinyint": TINYINT,
    "smallint": SMALLINT,
    "mediumint": INT,
    "int": INT,
    "integer": INT,
    "bigint": BIGINT,
    "bool": BOOL,
    "boolean": BOOL,
    "dec": NUMERIC,
    "decimal": NUMERIC,
    "numeric": NUMERIC,
    "fixed": NUMERIC,
    "float": FLOAT4,
    "real": FLOAT8,  # MySQL 的 real = double
    "double": FLOAT8,
    "double precision": FLOAT8,
    "char": CHAR,
    "varchar": VARCHAR,
    "tinytext": TEXT,
    "text": TEXT,
    "mediumtext": TEXT,
    "longtext": TEXT,
    "binary": BLOB,
    "varbinary": BLOB,
    "tinyblob": BLOB,
    "blob": BLOB,
    "mediumblob": BLOB,
    "longblob": BLOB,
    "date": DATE,
    "datetime": DATETIME,
    "timestamp": TIMESTAMP,
    "time": TIME,
    "json": JSON,
    "enum": ENUM,
    "set": SET,
}


def map_base(synonyms: dict[str, str], token: str) -> str:
    """把一个方言基名映到规范基名；表里没有的原样（小写）返回，交给调用方的容错口径。"""
    return synonyms.get(token, token)


def compose(canonical_base: str, params: str | None, array_depth: int) -> str:
    """规范基名 + 是否保留括号 + 数组后缀 → 最终 `data_type` 字面。"""
    token = canonical_base
    if params is not None and canonical_base in PARAMETERIZED:
        token = f"{canonical_base}({params})"
    return token + "[]" * array_depth
