"""PostgreSQL 抽取：`pg_catalog` 的五条批量 SQL（原文照 metadata-model §8.2）。

与 `mysql.py` 同形：同步驱动（psycopg3）+ `asyncio.to_thread`，`_rows` 是本模块唯一的
I/O 出口，编排（发哪条、什么顺序、结果怎么归并）因此能在不连库的情况下被单测钉住。
选同步而不是 async 有一条实测理由：psycopg3 的 async 模式在 Windows 的
`ProactorEventLoop` 上直接抛 `InterfaceError`，而 API 进程用的正是它（uvicorn 的
`asyncio_loop_factory` 在 win32 返回 Proactor）；抽取跑在 worker 里（`run_worker.py:40`
已切 Selector），但同步这一路在两种环流下都不会踩到那个坑。`architecture §9` 原文那句
"源 PG 抽取用 psycopg 同步"从这一片起才是真的。

对 §8.2 的七处偏离，都写在使用点上，这里只做目录：

1. `= ANY(%s)` 换成 `= :schema` + `IN (:tbl_N)`。传数组字面量要把用户填的表名先攒成一个
   数组常量，而 020 定下的形状是"按库、按批各发一条"，具名绑定参数是那一形状的写法。
2. C 条的 `modifiers` CASE 原文引用了 `facts.numeric_scale`，而 §8.2 的 FROM 里没有 `facts`
   这个别名（文档笔误，照抄是 42P01）。判定与取值范围原样搬进 `postgres_types.modifiers()`。
3. E 条原文只按 schema 过滤。分批必须再按表名过滤，否则每一批都会把整个 schema 的外键重取
   一遍——同一批关系落库两次，计数还会对不上。
4. D 条的 `s.idx_scan` 不进 `RawIndex.cardinality`：它数的是"这个索引被扫过多少次"，不是
   "这组列有多少不同值"，§8.2 自己把它叫 `cardinality_hint`。填进去就是把错单位写进一列名字
   管得着它的数据，所以 PG 侧这一格留 NULL（真要给 PG 补基数，原料是 `pg_stats.n_distinct`）。
5. B 条的 `relkind` 收在 `('r','p','v')`，少了原文的 `'m'`/`'f'`：§2.4 的
   `meta_table` 有 `CHECK (table_type IN ('BASE TABLE','VIEW'))`，物化视图/外部表落库必撞它；
   这与 §8.1 B 的 `TABLE_TYPE IN ('BASE TABLE','VIEW')` 是同一个口径，两方言都在这两类前止步。
6. `pg_total_relation_size` 是"表 + 索引 + TOAST"的合计，而 §2.4 只有 `data_bytes` /
   `index_bytes` 两格、没有合计这一格。把总数塞进 `data_bytes` 会让跨方言求和时把索引再数一遍，
   所以两个都留 NULL。
7. B 条的 `approx_rows` 只取 `c.reltuples`：原文的 `COALESCE(s.reltuples, c.reltuples)` 里
   `pg_stat_user_tables` **没有** `reltuples` 这一列（本机 PG 18.6 实测 42703 UndefinedColumn），
   左支不存在。剩下的 `c.reltuples` 是"上次 ANALYZE/VACUUM 时的行数估算"，与 MySQL 那边
   `information_schema.TABLES.TABLE_ROWS` 的引擎侧估算同一档口径。

上面这七条逐条都能在本模块找到对应编号；§8.2 末注的目录多一条 **⑧**，说的是"拿本节原文 diff
本模块 SQL 常量"这件事从此对 C 条不成立（CASE 搬走是偏离 2，比对方式跟着变是末注的 ⑧）。
本模块只记到七，比对口径以 §8.2 末注那份为准。

还有一处不是偏离但值得留字：LIKE 里的字面 `%` 写的是**单个**而不是 §8.2 原文那种双写。
本机在 PG 18.6 上实测过两条路径：带绑定参数时 SQLAlchemy 会自己把它升成 `%%` 再交给驱动
（回显的语句里是 `'pg\\_%%'`），不带参数时原样通过——两种写法在 LIKE 里都是同一个通配符，
语义没变。`\\_` 的反斜线转义是 PG 的 LIKE 默认就认（实测 `'pgXtemp' LIKE 'pg\\_%'` 为假），
所以系统对象那道过滤按字面下划线走，与 MySQL 侧 exclude_tables 的 `\\_%` 同一口径。
别名（`AS schema_name` 这类）是原文裸列的补名，驱动回来的键名就是它，形状需要，不算口径偏离。
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Final

from sqlalchemy import URL, create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from app.extractor import postgres_types
from app.extractor.base import (
    ConnectionSpec,
    RawCatalog,
    RawColumn,
    RawForeignKey,
    RawIndex,
    RawIndexColumn,
    RawTable,
    Row,
    ScopeCounts,
    ServerInfo,
    SourceManifest,
    apply_index_flags,
    ensure_within_max_tables,
)
from app.extractor.batching import in_list, in_params, slice_names

# §8.2 A 的排除名单，原文逐字（三个名字 + 两个 `pg_temp*` / `pg_toast*` 模式）
SYSTEM_SCHEMAS: Final = ("pg_catalog", "information_schema", "pg_toast")

# `\_` 把下划线还原成字面量：不转义的 `pg_%` 里 `_` 是单字符通配符，会把 `pgXtemp` 这类
# 正常表名一起排掉（本机 PG 18.6 实测：`'pgXtemp' LIKE 'pg\_%'` 为假、`LIKE 'pg_%'` 为真），
# 跟 MySQL 侧 exclude_tables 用的 `\_%` 是同一口径。
SQL_CATALOGS: Final = f"""
SELECT current_database()                                AS catalog_name,
       n.nspname                                         AS schema_name,
       pg_catalog.obj_description(n.oid, 'pg_class')      AS schema_comment
FROM pg_catalog.pg_namespace n
WHERE n.nspname NOT IN {SYSTEM_SCHEMAS}
  AND n.nspname NOT LIKE 'pg\\_temp%'
  AND n.nspname NOT LIKE 'pg\\_toast%'
  AND has_schema_privilege(n.oid, 'USAGE')
"""

# B 的 SELECT 那一半；FROM/WHERE 与计数查询共用 `_tables_from`（同 MySQL 的理由：
# 两处各写一套条件，分母和实际落库的对象数就会分叉）
SQL_TABLES_PROJECTION: Final = """
SELECT n.nspname                          AS schema_name,
       c.relname                          AS table_name,
       CASE c.relkind WHEN 'r' THEN 'BASE TABLE' WHEN 'p' THEN 'BASE TABLE'
                      WHEN 'v' THEN 'VIEW' ELSE c.relkind::text END AS table_type,
       pg_catalog.obj_description(c.oid, 'pg_class') AS comment,
       c.relam IN (SELECT oid FROM pg_am WHERE amname = 'heap') AS is_heap,
       pg_catalog.pg_total_relation_size(c.oid) AS size_bytes,
       c.reltuples::bigint                          AS approx_rows,
       s.last_analyze,
       s.last_autoanalyze
"""

SQL_COUNT_PROJECTION: Final = """
SELECT CASE c.relkind WHEN 'r' THEN 'BASE TABLE' WHEN 'p' THEN 'BASE TABLE'
                      WHEN 'v' THEN 'VIEW' ELSE c.relkind::text END AS table_type,
       COUNT(*) AS n
"""


def _tables_from(
    schema: str, scope_sql: str | None, scope_params: Mapping[str, str]
) -> tuple[str, dict[str, object]]:
    """§8.2 B 的 FROM/WHERE：抽表清单与数表清单共用这一份。"""
    sql = """
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_catalog.pg_stat_user_tables s ON s.relid = c.oid
WHERE c.relkind IN ('r', 'p', 'v')
  AND n.nspname = :schema
  AND c.relname NOT LIKE 'pg\\_%'
""".strip()
    if scope_sql:
        sql += f"\n  AND {scope_sql}"
    return sql, {"schema": schema, **scope_params}


def build_sql_tables(
    schema: str, scope_sql: str | None, scope_params: Mapping[str, str]
) -> tuple[str, dict[str, object]]:
    """§8.2 B：一次拿完表名/类型/注释/尺寸/统计新鲜度。

    `scope_sql` 由调用方用 `datasource_service.table_scope_filter(ds, column="c.relname")`
    渲染——列名是 PG 的那一个，别名与 MySQL 的 `t.table_name` 不同，所以口径留在编排层。
    """
    from_where, params = _tables_from(schema, scope_sql, scope_params)
    return f"{SQL_TABLES_PROJECTION}{from_where}", params


def build_sql_count_scope(
    schema: str, scope_sql: str | None, scope_params: Mapping[str, str]
) -> tuple[str, dict[str, object]]:
    """同一张 FROM/WHERE 只换个投影：按 `relkind` 归一后的表类型分组数一遍（§2.8 的分母）。"""
    from_where, params = _tables_from(schema, scope_sql, scope_params)
    return f"{SQL_COUNT_PROJECTION}{from_where}\nGROUP BY 1", params


def rows_to_scope_counts(rows: Sequence[Row]) -> ScopeCounts:
    """把 (表类型, 条数) 摊成三个分母。

    只认 'BASE TABLE' 与 'VIEW' 两档：B 条已经把它们过滤到只剩这两类（偏离 5），
    所以这里不存在"第三类被丢掉"的漏口——写清楚是为了以后放宽 B 时记得同步这里。
    """
    base = 0
    view = 0
    for row in rows:
        kind = str(row["table_type"]).upper()
        n = int(str(row["n"]))
        if kind == "BASE TABLE":
            base += n
        elif kind == "VIEW":
            view += n
    return ScopeCounts(total=base + view, base_table=base, view=view)


def build_sql_columns(schema: str, tables: Sequence[str]) -> tuple[str, dict[str, object]]:
    """§8.2 C：`format_type` 的人话名、列注释、默认值、生成列旗标、枚举标签一次拿完。

    `modifiers` 那一格不在这里（原文那条 CASE 引用了不存在的别名，见模块头的偏离 2）；
    `t.typname` 仍然 SELECT 出来，因为它就是那条 CASE 的判定依据，也是"这一列是不是数组"的
    最短路径（数组的元素类型名带下划线前缀：`_varchar`）。
    """
    sql = f"""
SELECT n.nspname AS schema_name, c.relname AS table_name,
       a.attnum AS ordinal_position, a.attname AS column_name,
       pg_catalog.format_type(a.atttypid, a.atttypmod) AS raw_data_type,
       t.typname AS data_type, (NOT a.attnotnull) AS nullable,
       pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS default_value,
       a.attgenerated <> '' AS is_generated,
       pg_catalog.col_description(a.attrelid, a.attnum) AS comment,
       CASE WHEN t.typtype='e' THEN (SELECT jsonb_agg(e.enumlabel ORDER BY e.enumsortorder)
                                     FROM pg_catalog.pg_enum e
                                     WHERE e.enumtypid=t.oid) END AS enum_values
FROM pg_catalog.pg_attribute a
JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE a.attnum > 0 AND NOT a.attisdropped
  AND n.nspname = :schema AND {in_list(tables, "c.relname")}
ORDER BY c.relname, a.attnum
"""
    return sql, in_params(tables, schema)


def build_sql_indexes(schema: str, tables: Sequence[str]) -> tuple[str, dict[str, object]]:
    """§8.2 D：`unnest(i.indkey) WITH ORDINALITY` 保索引列序。

    表达式索引那一位的 `x.attnum` 是 0，`LEFT JOIN pg_attribute` 因此接不上 →
    `column_name` 回 NULL（演示夹具的 `ix_product_name_lower` 钉的就是这一支）。表达式本体在
    `pg_get_indexdef` 里，但 `RawIndexColumn` 没有放它的格子（`meta_index.funcdef` 那一格
    今天没有人写），所以这一片只留 NULL。
    """
    sql = f"""
SELECT pn.nspname AS schema_name, tc.relname AS table_name, ic.relname AS index_name,
       i.indisunique, i.indisprimary, am.amname AS index_type,
       pg_catalog.obj_description(ic.oid, 'pg_class') AS comment,
       s.idx_scan AS cardinality_hint,
       pg_catalog.pg_get_indexdef(ic.oid) AS def,
       a.attname AS column_name, (x.ord)::int AS seq_in_index
FROM pg_catalog.pg_index i
JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid
JOIN pg_catalog.pg_am am ON am.oid = ic.relam
JOIN pg_catalog.pg_class tc ON tc.oid = i.indrelid
JOIN pg_catalog.pg_namespace pn ON pn.oid = tc.relnamespace
CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS x(attnum, ord)
LEFT JOIN pg_catalog.pg_attribute a ON a.attrelid = tc.oid AND a.attnum = x.attnum
LEFT JOIN pg_catalog.pg_stat_user_indexes s ON s.indexrelid = ic.oid
WHERE pn.nspname = :schema AND {in_list(tables, "tc.relname")}
ORDER BY tc.relname, ic.relname, x.ord
"""
    return sql, in_params(tables, schema)


def build_sql_foreign_keys(schema: str, tables: Sequence[str]) -> tuple[str, dict[str, object]]:
    """§8.2 E：`unnest(con.conkey, con.confkey)` 保复合外键的列序。

    `confupdtype` 原文只写了"同理解析"，这里按 `confdeltype` 那条 CASE 的同一张字母表补全。
    """
    sql = f"""
SELECT sn.nspname AS schema_name, rc.relname AS table_name, con.conname AS constraint_name,
       att.attname AS from_column, (x.ord)::int AS seq,
       rn.nspname AS to_schema, rt.relname AS to_table, ta.attname AS to_column,
       CASE con.confdeltype WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT'
                            WHEN 'c' THEN 'CASCADE' WHEN 'n' THEN 'SET NULL'
                            WHEN 'd' THEN 'SET DEFAULT' END AS on_delete,
       CASE con.confupdtype WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT'
                            WHEN 'c' THEN 'CASCADE' WHEN 'n' THEN 'SET NULL'
                            WHEN 'd' THEN 'SET DEFAULT' END AS on_update
FROM pg_catalog.pg_constraint con
JOIN pg_catalog.pg_class rc ON rc.oid = con.conrelid
JOIN pg_catalog.pg_namespace sn ON sn.oid = rc.relnamespace
JOIN pg_catalog.pg_class rt ON rt.oid = con.confrelid
JOIN pg_catalog.pg_namespace rn ON rn.oid = rt.relnamespace
CROSS JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY AS x(colid, refid, ord)
JOIN pg_catalog.pg_attribute att ON att.attrelid = rc.oid AND att.attnum = x.colid
JOIN pg_catalog.pg_attribute ta ON ta.attrelid = rt.oid AND ta.attnum = x.refid
WHERE con.contype = 'f' AND sn.nspname = :schema AND {in_list(tables, "rc.relname")}
ORDER BY rc.relname, con.conname, x.ord
"""
    return sql, in_params(tables, schema)


# ---------------------------------------------------------------------------
# pg_catalog 行 → Raw*（§7 的跨方言归一形状）
# ---------------------------------------------------------------------------


def _opt_text(value: object) -> str | None:
    """PG 的"没有注释"就是 NULL，不需要 MySQL 那一步"空串也当没有"。

    空串仍然留着：`COMMENT ON TABLE x IS ''` 是合法语句，把它读成"有注释，内容是空"
    与源库给的一致，而卡片那边按 `None` 判缺注释——所以要归成 None。
    """
    if value is None:
        return None
    return str(value) or None


def _enum_values(value: object) -> tuple[str, ...] | None:
    """`jsonb_agg(...)` 到 psycopg 手里已经是 list（JSONB 被解码），MySQL 那边才是字符串。"""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    # 个别驱动/服务器组合会把 jsonb 原样给回文本；中文标签里带引号，不能按 MySQL 那套正则拆
    raise ValueError(f"enum_values 的形状不是预期的列表：{type(value).__name__}")


def _later(a: object, b: object) -> dt.datetime | None:
    """022 拍板的 PG 档（metadata-model §2.4 注 ④）：`last_analyze` 与 `last_autoanalyze` 取较晚者。

    两个都为 NULL 就是 NULL。这里不补时区：PG 的这两列本身是 `timestamp with time zone`，
    驱动回来的就是 aware datetime——MySQL 那套 `_stamp_to_aware` 在 PG 这一路没有对象。
    """
    stamps = [value for value in (a, b) if isinstance(value, dt.datetime)]
    return max(stamps) if stamps else None


def _int(value: object) -> int | None:
    return None if value is None else int(str(value))


def rows_to_tables(rows: Sequence[Row], *, catalog_name: str) -> list[RawTable]:
    """§8.2 B 的行。

    PG 的 `catalog_name` 是真库名，不许照抄 MySQL 那个空串（工单 024 的已定口径）：
    它来自 A 条的 `current_database()`，由调用方按库传进来。
    """
    out: list[RawTable] = []
    for row in rows:
        table_type = str(row["table_type"])
        # 视图不给造新鲜度（§2.4 注 ② 的口径是方言无关的）；PG 里视图本来就不在
        # `pg_stat_user_tables` 里，那两列天然是 NULL，这一支只是把承诺写在代码上。
        freshness = (
            None if table_type == "VIEW" else _later(row["last_analyze"], row["last_autoanalyze"])
        )
        out.append(
            RawTable(
                catalog_name=catalog_name,
                schema_name=str(row["schema_name"]),
                table_name=str(row["table_name"]),
                table_type=table_type,
                comment=_opt_text(row["comment"]),
                # B 条的 `is_heap` 是个布尔，而 `RawTable.engine` 要的是名字。PG 的堆表
                # 访问方法就叫 `heap`（§8.2 自己那条子查询写的就是 `amname='heap'`），
                # 所以 True→'heap'、False→None 不丢信息；真出现第三种表 AM 时这一格会是 None，
                # 而不是把布尔硬编成名字。
                engine="heap" if row["is_heap"] else None,
                charset=None,
                collation=None,
                approx_rows=_int(row["approx_rows"]),
                data_bytes=None,
                index_bytes=None,
                row_format=None,
                last_analyze_at=freshness,
            )
        )
    return out


def rows_to_columns(rows: Sequence[Row], *, catalog_name: str) -> list[RawColumn]:
    """§8.2 C 的行：`data_type` 走 023 的归一、`raw_data_type` 存 `format_type` 原文。

    `serial` 与 `identity` 在 PG 里都是 `integer`/`bigint`，分开靠 `default_value`：
    serial 的真身是 `pg_attrdef` 里的 `nextval(...)`，identity 列不走那条路（015 的自检
    就是按这个把它们分开数的）。
    """
    out: list[RawColumn] = []
    for row in rows:
        raw = str(row["raw_data_type"])
        enum_values = _enum_values(row["enum_values"])
        char_length, num_precision, num_scale = postgres_types.modifiers(str(row["data_type"]), raw)
        out.append(
            RawColumn(
                catalog_name=catalog_name,
                schema_name=str(row["schema_name"]),
                table_name=str(row["table_name"]),
                column_name=str(row["column_name"]),
                ordinal_position=int(str(row["ordinal_position"])),
                data_type=postgres_types.normalize(raw, is_enum=enum_values is not None),
                raw_data_type=raw,
                nullable=bool(row["nullable"]),
                default=_opt_text(row["default_value"]),
                generated=bool(row["is_generated"]),
                comment=_opt_text(row["comment"]),
                char_length=char_length,
                num_precision=num_precision,
                num_scale=num_scale,
                enum_values=enum_values,
                is_primary_key=False,
                # §8.2 C 没取列级 collation（`attcollation`），所以这一格在 PG 侧是 NULL
                collation=None,
            )
        )
    return out


def rows_to_indexes(rows: Sequence[Row], *, catalog_name: str) -> list[RawIndex]:
    """§8.2 D 的行：一条索引 = 若干行，按 `(table, index, seq)` 排序回来。"""
    out: list[RawIndex] = []
    grouped: dict[tuple[str, str], list[Row]] = {}
    for row in rows:
        grouped.setdefault((str(row["table_name"]), str(row["index_name"])), []).append(row)
    for (table_name, index_name), group in grouped.items():
        first = group[0]
        out.append(
            RawIndex(
                catalog_name=catalog_name,
                schema_name=str(first["schema_name"]),
                table_name=table_name,
                index_name=index_name,
                is_unique=bool(first["indisunique"]),
                is_primary=bool(first["indisprimary"]),
                index_type=str(first["index_type"]),
                comment=_opt_text(first["comment"]),
                # 偏离 4：`idx_scan` 是被扫描次数，不是不同值个数，所以这里给 NULL
                cardinality=None,
                columns=tuple(
                    RawIndexColumn(
                        column_name=_opt_text(row["column_name"]),
                        seq_in_index=int(str(row["seq_in_index"])),
                        # PG 的升降序在 `pg_index.indoption` 里，§8.2 D 没取
                        collation=None,
                        # 前缀索引是 MySQL 特有的形状
                        sub_part=None,
                    )
                    for row in group
                ),
            )
        )
    return out


def rows_to_fks(rows: Sequence[Row], *, catalog_name: str) -> list[RawForeignKey]:
    """§8.2 E 的行。PG 的外键跨不了库，所以 `to_catalog` 就是本库名（不是 MySQL 的空串）。"""
    return [
        RawForeignKey(
            catalog_name=catalog_name,
            schema_name=str(row["schema_name"]),
            table_name=str(row["table_name"]),
            fk_name=str(row["constraint_name"]),
            from_column=str(row["from_column"]),
            to_catalog=catalog_name,
            to_schema=_opt_text(row["to_schema"]),
            to_table=str(row["to_table"]),
            to_column=str(row["to_column"]),
            seq=int(str(row["seq"])),
            on_delete=_opt_text(row["on_delete"]),
            on_update=_opt_text(row["on_update"]),
        )
        for row in rows
    ]


class PostgresExtractor:
    """按 §8.2 的五条 catalog 查询抽一个 PG 库（与 `MySQLExtractor` 对称的第五个动作）。"""

    kind: Final = "postgres"

    def __init__(self, spec: ConnectionSpec) -> None:
        self._spec = spec
        self._engine: Engine | None = None
        self._conn: Connection | None = None
        self._server_info: ServerInfo | None = None

    # ----------------------------------------------------------- I/O 边界

    def _rows(self, sql: str, params: dict[str, object] | None = None) -> list[Row]:
        # §6 末："单连接串行，不开并发打源库"
        if self._conn is None:
            self._conn = self._make_engine().connect()
        result = self._conn.execute(text(sql), params or {})
        return [dict(row) for row in result.mappings()]

    def _make_engine(self) -> Engine:
        spec = self._spec
        engine = create_engine(
            URL.create(
                "postgresql+psycopg",
                username=spec.user,
                password=spec.password,
                host=spec.host,
                port=spec.port,
                # PG 的 catalog_name 就是目标库（与 MySQL 恒空串不同），所以这里必须有值
                database=spec.database,
            ),
            poolclass=NullPool,
            connect_args={
                "connect_timeout": spec.connect_timeout_s,
                # 中文注释的保真靠这一条：libpq 只做 UTF8 ↔ 客户端编码的转换。
                # `spec.charset` 是 MySQL 的 `utf8mb4` 字面，交给 libpq 会被判成未知编码。
                "client_encoding": "UTF8",
            },
        )
        self._engine = engine
        return engine

    async def _fetch(self, sql: str, params: dict[str, object] | None = None) -> list[Row]:
        return await asyncio.to_thread(self._rows, sql, params)

    # ----------------------------------------------------------- §7 协议

    async def probe(self) -> ServerInfo:
        """版本 + 到达客户端的编码 + 会话级超时变量在不在。"""
        rows = await self._fetch("SELECT current_setting('server_version') AS v")
        version = rows[0]["v"]
        charset = (await self._fetch("SHOW client_encoding"))[0]["client_encoding"]
        supports_timeout = True
        try:
            # MySQL 那一侧的对应物是 `@@max_execution_time`（5.7.8 才有）；PG 的对应物叫
            # `statement_timeout`，从 8.2 起就在。仍然读一次而不是硬编码 True，是因为这个
            # 标志的语义是"这个源上真的能用"，而源库可能是别人架的老版本。
            await self._fetch("SHOW statement_timeout")
        except DBAPIError as exc:
            # 只把"这个变量不存在"降级成不支持；连接类错误必须继续抛，否则一次网络抖动
            # 会被报成"这个源不支持超时控制"，而 011 的超时策略正是拿这个布尔值决定的
            # （与 MySQL 侧 `_errno != 1193` 那一条同一个理由）。
            if _sqlstate(exc) != "42704":  # undefined_object，本机 PG 18.6 实测
                raise
            supports_timeout = False
        self._server_info = ServerInfo(
            kind=self.kind,
            server_version=str(version),
            supports_max_execution_time=supports_timeout,
            charset=str(charset) if charset is not None else None,
        )
        return self._server_info

    async def discover(self) -> list[RawCatalog]:
        rows = await self._fetch(SQL_CATALOGS)
        return [
            RawCatalog(
                # 与 MySQL 相反：PG 的 catalog 就是 current_database()，不是空串
                catalog_name=str(row["catalog_name"]),
                schema_name=str(row["schema_name"]),
                # §8.2 A 没取 schema 级编码/排序规则：PG 的编码在**库**级、collation 在**列**级，
                # 没有"MySQL 那种每库一个 DEFAULT_CHARSET"的对应物。
                charset=None,
                collation=None,
                # 偏离 6 的另一半：A 条不 JOIN pg_class，所以库级尺寸/表数在 PG 侧是 NULL。
                # 填它就得在 discover 阶段对全库每张表跑一次 `pg_total_relation_size`——
                # MySQL 那边读的是 information_schema 里现成的缓存计数，代价完全不同。
                approx_size_bytes=None,
                visible_table_count=None,
                approx_rows=None,
            )
            for row in rows
        ]

    async def count_scope(
        self,
        catalogs: Sequence[RawCatalog],
        *,
        table_sql: str | None = None,
        table_params: Mapping[str, str] | None = None,
    ) -> ScopeCounts:
        """每库一条分组计数（SSE 每帧的分母）。与 MySQL 同形：昂贵的 C/D/E 一条都不发。"""
        total = base = view = 0
        scope = dict(table_params or {})
        for catalog in catalogs:
            sql, params = build_sql_count_scope(catalog.schema_name, table_sql, scope)
            counts = rows_to_scope_counts(await self._fetch(sql, params))
            total += counts.total
            base += counts.base_table
            view += counts.view
        return ScopeCounts(total=total, base_table=base, view=view)

    async def collect(
        self,
        catalogs: Sequence[RawCatalog],
        *,
        table_sql: str | None = None,
        table_params: Mapping[str, str] | None = None,
        max_tables: int | None = None,
        batch_size: int,
        batch_interval_ms: int,
    ) -> SourceManifest:
        server = self._server_info or await self.probe()
        scope = dict(table_params or {})

        # 第一段：每个 schema 一次 B。先把表全拿到，才能既判断"范围里真没表"，
        # 又能在昂贵的 C/D/E 之前拒绝超限的库（§6 的 MAX_TABLES 是给源库省力的）。
        tables: list[RawTable] = []
        names_by_schema: dict[str, list[str]] = {}
        catalog_names: dict[str, str] = {}
        for catalog in catalogs:
            sql, params = build_sql_tables(catalog.schema_name, table_sql, scope)
            found = rows_to_tables(
                await self._fetch(sql, params), catalog_name=catalog.catalog_name
            )
            tables.extend(found)
            catalog_names[catalog.schema_name] = catalog.catalog_name
            if found:
                names_by_schema[catalog.schema_name] = [t.table_name for t in found]
        ensure_within_max_tables(len(tables), max_tables)

        columns: list[RawColumn] = []
        indexes: list[RawIndex] = []
        foreign_keys: list[RawForeignKey] = []
        batches: list[list[str]] = []
        for schema, names in names_by_schema.items():
            catalog_name = catalog_names[schema]
            # 第二段：C/D/E 按批各发一次（工单 020 的形状，两方言共用）
            for batch in slice_names(names, batch_size):
                if batches:
                    # 气只发生在批与批**之间**：第一批之前睡一觉等于"作业已开始却什么都没发"，
                    # 那一刻 018 的心跳正好刚开始计时。
                    await asyncio.sleep(batch_interval_ms / 1000)
                batches.append(batch)

                sql, params = build_sql_indexes(schema, batch)
                batch_indexes = rows_to_indexes(
                    await self._fetch(sql, params), catalog_name=catalog_name
                )
                indexes.extend(batch_indexes)

                sql, params = build_sql_columns(schema, batch)
                batch_columns = rows_to_columns(
                    await self._fetch(sql, params), catalog_name=catalog_name
                )
                columns.extend(apply_index_flags(batch_columns, batch_indexes))

                sql, params = build_sql_foreign_keys(schema, batch)
                foreign_keys.extend(
                    rows_to_fks(await self._fetch(sql, params), catalog_name=catalog_name)
                )

        # PG 侧不打 CHARSET_SUSPECT：libpq 转不动时会**报错**而不是把中文换成问号，
        # §10.1 那个"整片变 ?"的形态在这里不成立；而演示库的编码由 015 的建库脚本
        # 用 `ENCODING 'UTF8'` + 一道 `pg_encoding_to_char` 守卫钉住（verification §1.5.2）。
        return SourceManifest(
            kind=self.kind,
            server_version=server.server_version,
            collected_at=dt.datetime.now(dt.UTC),
            catalogs=list(catalogs),
            tables=tables,
            columns=columns,
            indexes=indexes,
            foreign_keys=foreign_keys,
            warnings=[],
            batches=batches,
        )

    async def close(self) -> None:
        def _close() -> None:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            if self._engine is not None:
                self._engine.dispose()
                self._engine = None

        await asyncio.to_thread(_close)


def _sqlstate(exc: BaseException) -> str | None:
    """SQLSTATE 在 SQLAlchemy 包起来的那颗驱动异常身上（psycopg3 的属性叫 `sqlstate`）。

    与 `mysql._errno` 同一件事、不同的名字：psycopg2 那边叫 `pgcode`，psycopg3 叫
    `sqlstate`，取错名字会得到 None，于是"只降级 42704"退化成"什么都不降级"。
    """
    orig = getattr(exc, "orig", None)
    value = getattr(orig, "sqlstate", None)
    return str(value) if isinstance(value, str) else None
