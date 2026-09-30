"""MySQL 5.7 抽取：`information_schema` 的五条批量 SQL（原文照 metadata-model §8.1）。

为什么手写 IS 查询而不用 SQLAlchemy Inspector：Inspector 逐表 `SHOW CREATE TABLE`，
2000 表就是 2000 次往返，而且会**丢掉 `SUB_PART` 与 `CARDINALITY`**——§10 末把这两列
非空当作"没退回 Inspector 方案"的验收锚点。

这一层只产 SQL 文本和参数，不碰连接：`IN (...)` 的占位符数量、漏掉 `TABLE_SCHEMA` 条件
这类错误，在 mock 连接上不会显形，所以单测直接按 §2.2 第 4 项对 SQL 文本做快照。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Final
from zoneinfo import ZoneInfo

from sqlalchemy import URL, create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from app.core.errors import SCOPE_REMEDIES, ExtractScopeTooLarge
from app.extractor.base import (
    ConnectionSpec,
    ExtractWarning,
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
)

# 系统库永远不该进元数据：它们的信息是实例级的，且会把 60+ 张表灌进卡片库（§8.1 A）
SYSTEM_SCHEMAS: Final = ("information_schema", "mysql", "performance_schema", "sys")

SQL_CATALOGS: Final = f"""
SELECT s.SCHEMA_NAME, s.DEFAULT_CHARACTER_SET_NAME, s.DEFAULT_COLLATION_NAME,
       COALESCE(SUM(t.DATA_LENGTH + t.INDEX_LENGTH),0) AS size_bytes,
       COALESCE(SUM(t.TABLE_ROWS),0)                  AS approx_rows,
       COUNT(t.TABLE_NAME)                            AS visible_table_count
FROM information_schema.SCHEMATA s
LEFT JOIN information_schema.TABLES t ON t.TABLE_SCHEMA = s.SCHEMA_NAME
WHERE s.SCHEMA_NAME NOT IN {SYSTEM_SCHEMAS}
GROUP BY s.SCHEMA_NAME, s.DEFAULT_CHARACTER_SET_NAME, s.DEFAULT_COLLATION_NAME
"""


def _in_params(tables: Sequence[str], schema: str) -> dict[str, object]:
    if not tables:
        # `IN ()` 在 MySQL 里是语法错误；空范围必须由调用方（sync_service）提前短路，
        # 而不是让这条 SQL 发到源库去换一个 1064。
        raise ValueError("抽取范围里没有表：不该发这条查询")
    params: dict[str, object] = {"schema": schema}
    params.update({f"tbl_{i}": name for i, name in enumerate(tables)})
    return params


def _in_list(tables: Sequence[str], column: str) -> str:
    return f"{column} IN ({', '.join(f':tbl_{i}' for i in range(len(tables)))})"


def _tables_from(
    schema: str, scope_sql: str | None, scope_params: Mapping[str, str]
) -> tuple[str, dict[str, object]]:
    """§8.1 B 的 FROM/WHERE 那一半：抽表清单与数表清单**共用这一份**。

    分开的代价是看得见的：数据源配了 exclude_tables 时，两处各写一套 WHERE 就会一个报
    9 表 1 视图、一个抽出 11 张——SSE 每帧的分母和实际落库的对象数从此对不上。
    """
    sql = """
FROM information_schema.TABLES t
WHERE t.TABLE_SCHEMA = :schema
  AND t.TABLE_TYPE IN ('BASE TABLE','VIEW')
""".strip()
    if scope_sql:
        sql += f"\n  AND {scope_sql}"
    return sql, {"schema": schema, **scope_params}


def build_sql_tables(
    schema: str, scope_sql: str | None, scope_params: Mapping[str, str]
) -> tuple[str, dict[str, object]]:
    """§8.1 B：一次拿完表名/类型/注释/引擎/大小。

    `scope_sql` 由 `datasource_service.table_scope_filter` 渲染（列写法传 `t.table_name`），
    和 006 的 `test_connection` 共用同一个函数——两边各写一套就会一个报 9/1、一个抽 11。
    注意这里是 LIKE 而不是 §8.1 原文的 REGEXP：数据源里那两列存的既不是正则
    （006 已实测，见工单偏差节），跟着 §10.2 的"别手写 COLLATE"一起改成了源原生 LIKE。
    """
    from_where, params = _tables_from(schema, scope_sql, scope_params)
    sql = """
SELECT t.TABLE_SCHEMA, t.TABLE_NAME,
       CASE t.TABLE_TYPE WHEN 'BASE TABLE' THEN 'BASE TABLE'
                         WHEN 'VIEW' THEN 'VIEW' ELSE t.TABLE_TYPE END AS table_type,
       t.TABLE_COMMENT, t.ENGINE, t.ROW_FORMAT, t.TABLE_COLLATION, t.TABLE_ROWS,
       t.DATA_LENGTH, t.INDEX_LENGTH, t.CREATE_TIME, t.UPDATE_TIME
"""
    return f"{sql}{from_where}", params


def build_sql_count_scope(
    schema: str, scope_sql: str | None, scope_params: Mapping[str, str]
) -> tuple[str, dict[str, object]]:
    """同一张 FROM/WHERE 只换个投影：按 TABLE_TYPE 分组数一遍。

    昂贵的 C/D/E 一条都不发，所以这条能在作业开头跑得起（metadata-model §2.8 的
    total/base_table/view 就是它的产物）。
    """
    from_where, params = _tables_from(schema, scope_sql, scope_params)
    sql = f"SELECT t.TABLE_TYPE AS table_type, COUNT(*) AS n\n{from_where}\nGROUP BY t.TABLE_TYPE"
    return sql, params


def rows_to_scope_counts(rows: Sequence[Row]) -> ScopeCounts:
    """把 (表类型, 条数) 摊成三个分母；`n` 是 `COUNT(*)`，源库永远不会回 NULL。"""
    base = 0
    view = 0
    for row in rows:
        kind = str(row["table_type"]).upper()
        n = _int(row["n"]) or 0
        if kind == "BASE TABLE":
            base += n
        elif kind == "VIEW":
            view += n
    return ScopeCounts(total=base + view, base_table=base, view=view)


def build_sql_columns(schema: str, tables: Sequence[str]) -> tuple[str, dict[str, object]]:
    """§8.1 C：注释、枚举定义、生成列标记、长度/精度一次拿完。"""
    sql = f"""
SELECT c.TABLE_NAME, c.COLUMN_NAME, c.ORDINAL_POSITION, c.DATA_TYPE, c.COLUMN_TYPE,
       c.IS_NULLABLE, c.COLUMN_DEFAULT, c.EXTRA, c.COLUMN_COMMENT,
       c.CHARACTER_MAXIMUM_LENGTH, c.NUMERIC_PRECISION, c.NUMERIC_SCALE,
       c.COLLATION_NAME,
       CASE WHEN c.DATA_TYPE='enum' OR c.DATA_TYPE='set' THEN c.COLUMN_TYPE END AS enum_def
FROM information_schema.COLUMNS c
WHERE c.TABLE_SCHEMA = :schema AND {_in_list(tables, "c.TABLE_NAME")}
ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION
"""
    return sql, _in_params(tables, schema)


def build_sql_indexes(schema: str, tables: Sequence[str]) -> tuple[str, dict[str, object]]:
    """§8.1 D：STATISTICS 保序，`CARDINALITY` / `SUB_PART` 是验收锚点。

    那个 `LEFT JOIN STATISTICS it ... AND it.SEQ_IN_INDEX=1` 的自连接看着多余，实际是
    §8.1 特意留的：`COMMENT` 在 IS 里是**每行都带一份**的索引级属性，固定取第 1 行就不会
    让复合索引的注释随列数翻倍（本机 5.7.17 实测 STATISTICS 同时有 COMMENT 和 INDEX_COMMENT，
    所以这一列不用退回 `SHOW CREATE TABLE`）。
    """
    sql = f"""
SELECT s.TABLE_NAME, s.INDEX_NAME, (s.NON_UNIQUE=0) AS is_unique,
       (s.INDEX_NAME='PRIMARY') AS is_primary, s.INDEX_TYPE, s.NULLABLE,
       s.COLUMN_NAME, s.SEQ_IN_INDEX, s.CARDINALITY, s.SUB_PART, s.COLLATION,
       IFNULL(it.COMMENT,'') AS index_comment
FROM information_schema.STATISTICS s
LEFT JOIN information_schema.STATISTICS it
       ON it.TABLE_SCHEMA=s.TABLE_SCHEMA AND it.TABLE_NAME=s.TABLE_NAME
      AND it.INDEX_NAME=s.INDEX_NAME AND it.SEQ_IN_INDEX=1
WHERE s.TABLE_SCHEMA = :schema AND {_in_list(tables, "s.TABLE_NAME")}
ORDER BY s.TABLE_NAME, s.INDEX_NAME, s.SEQ_IN_INDEX
"""
    return sql, _in_params(tables, schema)


def build_sql_foreign_keys(schema: str, tables: Sequence[str]) -> tuple[str, dict[str, object]]:
    """§8.1 E：`REFERENCED_TABLE_NAME IS NOT NULL` 把普通索引排掉。"""
    sql = f"""
SELECT k.TABLE_NAME, k.CONSTRAINT_NAME, k.COLUMN_NAME, k.ORDINAL_POSITION,
       k.REFERENCED_TABLE_SCHEMA, k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME,
       r.DELETE_RULE, r.UPDATE_RULE
FROM information_schema.KEY_COLUMN_USAGE k
JOIN information_schema.REFERENTIAL_CONSTRAINTS r
  ON r.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA AND r.CONSTRAINT_NAME = k.CONSTRAINT_NAME
WHERE k.TABLE_SCHEMA = :schema AND k.REFERENCED_TABLE_NAME IS NOT NULL
  AND {_in_list(tables, "k.TABLE_NAME")}
"""
    return sql, _in_params(tables, schema)


# ---------------------------------------------------------------------------
# IS 行 → Raw*（§7 的跨方言归一形状）
# ---------------------------------------------------------------------------


def _text(value: object) -> str | None:
    """IS 的"没有注释"在 MySQL 里是空串，而 §7/008 的卡片模板用 `None` 判缺注释。"""
    if value is None:
        return None
    text = str(value)
    return text or None


def _int(value: object) -> int | None:
    """IS 的数值列到驱动手里可能是 int、Decimal 或数字串（`SUM()` 回 Decimal）。"""
    return None if value is None else int(str(value))


def _counter(value: object) -> int:
    """ORDINAL_POSITION / SEQ_IN_INDEX 这类 NOT NULL 整数列：源库不会给空。"""
    return int(str(value))


def _flag(value: object) -> bool:
    # 5.7 的 `(NON_UNIQUE=0)` 这类布尔表达式经驱动回来是 int 0/1，不是 bool
    return bool(value)


def _charset_from_collation(collation: str | None) -> str | None:
    # §8.1 B 只 SELECT 了 TABLE_COLLATION，字符集是排序规则的下划线前缀（§10.2）
    return None if collation is None else collation.partition("_")[0]


def _stamp_to_aware(value: object, zone: dt.tzinfo) -> dt.datetime | None:
    """P3-022 时区口径：MySQL 的 `datetime` 无时区，目标列是 `timestamptz`——转换显式做，
    不许靠驱动隐式转换（011 那批 `serialize_cell` 的时区教训同源）。

    驱动解出的 naive `datetime` 按**源库所在机器时区**显式 attach；本身已带时区的值原样透传
    （今天 pymysql 对 IS 这两列给的就是 naive，走到后者属于防御）。
    """
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=zone)
        return value
    # IS 的时间列个别构建/驱动组合下回字符串：显式解析再挂时区，同样不走驱动的隐式转换
    parsed = dt.datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=zone)


def _source_machine_zone() -> dt.tzinfo:
    """源库所在机器的时区（P3-022 拍板：本机演示库 = 与 API 同区）。

    首选 `zoneinfo`：`TZ` 环境变量给了 IANA 键且平台有 tzdata 时拿到的就是 ZoneInfo。
    平台取不到本地 IANA 键时（Windows 的 `zoneinfo.TZPATH` 是空的，系统时区也没有公开的
    反查接口）退回机器当前 UTC 偏移的**显式**固定偏移——演示区（中国标准时间）全年无夏令时，
    这一替换不损失信息；真部署到带 DST 的区时由 024 一并升级为可配置键。
    """
    key = os.environ.get("TZ")
    if key:
        try:
            return ZoneInfo(key)
        except (KeyError, ValueError):
            pass  # tzdata 缺失的平台上 TZ 也可能是 Windows 本地化名，落回偏移档
    offset = dt.datetime.now().astimezone().utcoffset()
    return dt.timezone(offset) if offset is not None else dt.UTC


def rows_to_tables(rows: Sequence[Row], *, source_tz: dt.tzinfo | None = None) -> list[RawTable]:
    zone = source_tz if source_tz is not None else _source_machine_zone()
    out: list[RawTable] = []
    for row in rows:
        collation = _text(row["TABLE_COLLATION"])
        table_type = str(row["table_type"])
        # P3-022 拍板（metadata-model §2.4）：视图**不给造新鲜度**——MySQL 里视图行的
        # CREATE_TIME 是定义时间、UPDATE_TIME 常为 NULL 且语义都不是"数据被更新"，
        # 所以这一支不读那两列，恒 NULL；"没有"在这里不是 bug。
        freshness: dt.datetime | None = None
        if table_type != "VIEW":
            # 已定口径：UPDATE_TIME 优先；UPDATE_TIME 为空则退回 CREATE_TIME；两者都空则 NULL
            freshness = _stamp_to_aware(row["UPDATE_TIME"], zone) or _stamp_to_aware(
                row["CREATE_TIME"], zone
            )
        out.append(
            RawTable(
                catalog_name="",  # §1：MySQL 的 catalog 恒空串，不塞 IS 里那个 'def'
                schema_name=str(row["TABLE_SCHEMA"]),
                table_name=str(row["TABLE_NAME"]),
                table_type=table_type,
                comment=_text(row["TABLE_COMMENT"]),
                engine=_text(row["ENGINE"]),
                charset=_charset_from_collation(collation),
                collation=collation,
                approx_rows=_int(row["TABLE_ROWS"]),
                data_bytes=_int(row["DATA_LENGTH"]),
                index_bytes=_int(row["INDEX_LENGTH"]),
                row_format=_text(row["ROW_FORMAT"]),
                last_analyze_at=freshness,
            )
        )
    return out


def _enum_values(enum_def: object) -> tuple[str, ...] | None:
    if enum_def is None:
        return None
    return tuple(re.findall(r"'([^']*)'", str(enum_def)))


def rows_to_columns(rows: Sequence[Row], *, schema_name: str = "") -> list[RawColumn]:
    """§8.1 C 的行。列查询按 `:schema` 过滤、不 SELECT TABLE_SCHEMA，所以 schema 由调用方补。"""
    out: list[RawColumn] = []
    for row in rows:
        extra = str(row["EXTRA"] or "")
        out.append(
            RawColumn(
                catalog_name="",
                schema_name=schema_name,
                table_name=str(row["TABLE_NAME"]),
                column_name=str(row["COLUMN_NAME"]),
                ordinal_position=_counter(row["ORDINAL_POSITION"]),
                data_type=str(row["DATA_TYPE"]),
                raw_data_type=str(row["COLUMN_TYPE"]),
                nullable=str(row["IS_NULLABLE"]) == "YES",
                default=_text(row["COLUMN_DEFAULT"]),
                # auto_increment 也在 EXTRA 里，但它不是生成列
                generated="GENERATED" in extra.upper(),
                comment=_text(row["COLUMN_COMMENT"]),
                char_length=_int(row["CHARACTER_MAXIMUM_LENGTH"]),
                num_precision=_int(row["NUMERIC_PRECISION"]),
                num_scale=_int(row["NUMERIC_SCALE"]),
                enum_values=_enum_values(row["enum_def"]),
                is_primary_key=False,
                collation=_text(row["COLLATION_NAME"]),
            )
        )
    return out


def rows_to_indexes(rows: Sequence[Row], *, schema_name: str = "") -> list[RawIndex]:
    out: list[RawIndex] = []
    grouped: dict[tuple[str, str], list[Row]] = {}
    for row in rows:
        grouped.setdefault((str(row["TABLE_NAME"]), str(row["INDEX_NAME"])), []).append(row)
    # 查询按 (table, index, seq) 排序，dict 保序 → 这里出来的索引顺序是确定的
    for (table_name, index_name), group in grouped.items():
        first = group[0]
        out.append(
            RawIndex(
                catalog_name="",
                schema_name=schema_name,
                table_name=table_name,
                index_name=index_name,
                is_unique=_flag(first["is_unique"]),
                is_primary=_flag(first["is_primary"]),
                index_type=str(first["INDEX_TYPE"]),
                comment=_text(first["index_comment"]),
                # 索引级属性每行都带一份，取 SEQ_IN_INDEX=1 那行（§8.1 D 的自连接同理）
                cardinality=_int(first["CARDINALITY"]),
                columns=tuple(
                    RawIndexColumn(
                        column_name=_text(row["COLUMN_NAME"]),
                        seq_in_index=_counter(row["SEQ_IN_INDEX"]),
                        collation=_text(row["COLLATION"]),
                        sub_part=_int(row["SUB_PART"]),
                    )
                    for row in group
                ),
            )
        )
    return out


def apply_index_flags(columns: Sequence[RawColumn], indexes: Sequence[RawIndex]) -> list[RawColumn]:
    """把 STATISTICS 的结论回填到列上（§7：`is_primary_key` 来自索引，不是 COLUMN_KEY）。"""
    # (表名, 列名) → (主键, 唯一, 被索引)；一个列同时出现在 PRIMARY 和 uk_x 时取"或"
    flags: dict[tuple[str, str], tuple[bool, bool, bool]] = {}
    for index in indexes:
        for key_column in index.columns:
            if key_column.column_name is None:
                continue
            key = (index.table_name, key_column.column_name)
            primary, unique, _ = flags.get(key, (False, False, False))
            flags[key] = (primary or index.is_primary, unique or index.is_unique, True)

    def flags_of(column: RawColumn) -> tuple[bool, bool, bool]:
        return flags.get((column.table_name, column.column_name), (False, False, False))

    return [
        replace(
            column,
            is_primary_key=flags_of(column)[0],
            is_unique=flags_of(column)[1],
            is_indexed=flags_of(column)[2],
        )
        for column in columns
    ]


def rows_to_fks(rows: Sequence[Row], *, schema_name: str = "") -> list[RawForeignKey]:
    return [
        RawForeignKey(
            catalog_name="",
            schema_name=schema_name,
            table_name=str(row["TABLE_NAME"]),
            fk_name=str(row["CONSTRAINT_NAME"]),
            from_column=str(row["COLUMN_NAME"]),
            to_catalog="",
            to_schema=_text(row["REFERENCED_TABLE_SCHEMA"]),
            to_table=str(row["REFERENCED_TABLE_NAME"]),
            to_column=str(row["REFERENCED_COLUMN_NAME"]),
            seq=_counter(row["ORDINAL_POSITION"]),
            on_delete=_text(row["DELETE_RULE"]),
            on_update=_text(row["UPDATE_RULE"]),
        )
        for row in rows
    ]


def comment_charset_suspect(comments: Sequence[str | None]) -> bool:
    """§10.1：5.7 的 IS 列走 `character_set_system_variables`，中文注释可能整片回 `?`。

    阈值 0.3 和"`?` 占全部注释字符的比例"这个口径都是文档给的数，不是这里拍的。
    """
    joined = "".join(comment for comment in comments if comment)
    if not joined:
        return False
    return joined.count("?") / len(joined) > 0.3


class MySQLExtractor:
    """按 §8.1 的五条 IS 查询抽一个 MySQL 实例（architecture §9：pymysql 同步 + to_thread）。

    连接的建/断都在这个类里，`_rows` 是本模块唯一的 I/O 出口——编排（发哪条 SQL、什么顺序、
    结果怎么归并）因此可以在不连库的情况下被单测钉住，真库的形状交给 live 用例。
    """

    kind: Final = "mysql"

    def __init__(self, spec: ConnectionSpec) -> None:
        self._spec = spec
        self._engine: Engine | None = None
        self._conn: Connection | None = None
        self._server_info: ServerInfo | None = None

    # ----------------------------------------------------------- I/O 边界

    def _rows(self, sql: str, params: dict[str, object] | None = None) -> list[Row]:
        # §6 末："单连接串行，不开并发打源库"——连接建一次、留到 close()
        if self._conn is None:
            self._conn = self._make_engine().connect()
        result = self._conn.execute(text(sql), params or {})
        # 键名就是 SQL 里写的那个样子（本机 5.7.17 实测：`SELECT c.TABLE_NAME` 回
        # `TABLE_NAME`，`AS enum_def` 回 `enum_def`），所以下面的映射按 §8.1 原文的大小写取值
        return [dict(row) for row in result.mappings()]

    def _make_engine(self) -> Engine:
        spec = self._spec
        engine = create_engine(
            URL.create(
                "mysql+pymysql",
                username=spec.user,
                # pymysql 对 str 口令做 UTF-8 编码，非 ASCII 不会像 asyncmy 那样在客户端就炸
                password=spec.password,
                host=spec.host,
                port=spec.port,
                database=spec.database,
            ),
            # 一次同步一条连接，用完就断，不进任何池
            poolclass=NullPool,
            connect_args={"charset": spec.charset, "connect_timeout": spec.connect_timeout_s},
        )
        self._engine = engine
        return engine

    async def _fetch(self, sql: str, params: dict[str, object] | None = None) -> list[Row]:
        return await asyncio.to_thread(self._rows, sql, params)

    # ----------------------------------------------------------- §7 协议

    async def probe(self) -> ServerInfo:
        """版本 + 字符集 + `max_execution_time` 支持情况（§6 的 probe 阶段）。"""
        version = (await self._fetch("SELECT VERSION() AS v"))[0]["v"]
        charset = (await self._fetch("SELECT @@character_set_results AS cs"))[0]["cs"]
        supports_timeout = True
        try:
            # 5.7.8 才有这个会话变量；不支持时源库回 1193（006 的探测走 asyncmy，同一套判定）
            await self._fetch("SELECT @@max_execution_time")
        except DBAPIError as exc:
            if _errno(exc) != 1193:
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
                catalog_name="",
                schema_name=str(row["SCHEMA_NAME"]),
                charset=_text(row["DEFAULT_CHARACTER_SET_NAME"]),
                collation=_text(row["DEFAULT_COLLATION_NAME"]),
                approx_size_bytes=_int(row["size_bytes"]),
                visible_table_count=_int(row["visible_table_count"]),
                # §8.1 A 的 SUM(TABLE_ROWS) 到 §2.4 meta_database.approx_rows 的通路；
                # 漏掉它这条链就断在发现层（上一版 database_row 只能写 None）
                approx_rows=_int(row["approx_rows"]),
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
        """每个 schema 一条分组计数，凑齐 SSE 每帧的分母（§2.8 的 total/base_table/view）。

        每个库一条而不是并成一条 `IN`：范围条件是按 schema 渲染的，合并会把 `:schema`
        这个绑定参数拆成列表，而 B 那条用的是同一个写法。
        """
        total = base = view = 0
        for catalog in catalogs:
            sql, params = build_sql_count_scope(
                catalog.schema_name, table_sql, dict(table_params or {})
            )
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
    ) -> SourceManifest:
        server = self._server_info or await self.probe()
        scope = dict(table_params or {})

        # 第一段：每个 schema 一次 §8.1 B。先把表全部拿到手，才能既做出"范围里真没表"
        # 的判断，又能在昂贵的 C/D/E 之前拒绝超限的库（§6 的 MAX_TABLES 是给源库省力的）。
        tables: list[RawTable] = []
        names_by_schema: dict[str, list[str]] = {}
        for catalog in catalogs:
            sql, params = build_sql_tables(catalog.schema_name, table_sql, scope)
            rows = await self._fetch(sql, params)
            batch = rows_to_tables(rows)
            tables.extend(batch)
            if batch:
                names_by_schema[catalog.schema_name] = [t.table_name for t in batch]
        if max_tables is not None and len(tables) > max_tables:
            raise ExtractScopeTooLarge(
                f"抽取范围里有 {len(tables)} 张表，超过上限 {max_tables}",
                detail={
                    "table_count": len(tables),
                    "max_tables": max_tables,
                    "remedies": list(SCOPE_REMEDIES),
                },
            )

        columns: list[RawColumn] = []
        indexes: list[RawIndex] = []
        foreign_keys: list[RawForeignKey] = []
        for schema, names in names_by_schema.items():
            # 第二段：C/D/E 各一次，与表数量无关（§8.1 的整段前提）
            sql, params = build_sql_indexes(schema, names)
            batch_indexes = rows_to_indexes(await self._fetch(sql, params), schema_name=schema)
            indexes.extend(batch_indexes)

            sql, params = build_sql_columns(schema, names)
            batch_columns = rows_to_columns(await self._fetch(sql, params), schema_name=schema)
            columns.extend(apply_index_flags(batch_columns, batch_indexes))

            sql, params = build_sql_foreign_keys(schema, names)
            foreign_keys.extend(rows_to_fks(await self._fetch(sql, params), schema_name=schema))

        warnings: list[ExtractWarning] = []
        comments = [t.comment for t in tables] + [c.comment for c in columns]
        comments += [i.comment for i in indexes]
        if comment_charset_suspect(comments):
            warnings.append(
                ExtractWarning(
                    code="CHARSET_SUSPECT",
                    detail=(
                        "注释里的 ? 占比超过 30%：源库的 information_schema 可能把中文截成了问号"
                    ),
                )
            )
        return SourceManifest(
            kind=self.kind,
            server_version=server.server_version,
            collected_at=dt.datetime.now(dt.UTC),
            catalogs=list(catalogs),
            tables=tables,
            columns=columns,
            indexes=indexes,
            foreign_keys=foreign_keys,
            warnings=warnings,
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


def _errno(exc: BaseException) -> int | None:
    """SQLAlchemy 包了一层，错误号在被包的驱动异常身上。"""
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", ())
    return args[0] if args and isinstance(args[0], int) else None
