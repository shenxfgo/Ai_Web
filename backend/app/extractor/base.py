"""`SourceDialect` 的中间结构（逐字段对 `docs/metadata-model.md` §7）。

这些 dataclass 是**抽取层与落库层之间唯一的耦合点**：方言只负责把源库的东西变成 `Raw*`，
`sync_service` 只负责把 `Raw*` 变成 `meta_*` 行。加第三种源库不动 service，改卡片模板不动方言。

冻结（`frozen=True`）+ `slots=True`：抽取一批可能有几千条，路径上没人应该偷偷改上游对象。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# 源库返回的一行：列名 → 值。抽取层只按这个形状取数，不认识任何 ORM。
Row = Mapping[str, object]


@dataclass(slots=True, frozen=True)
class ConnectionSpec:
    """连一个源库需要的东西（§7 的 `conn_spec`，文档引用了它但没定义，这里补上）。

    刻意不认识 `DataSource`：ORM 行、Fernet 密文和 Settings 都留在 sync_service 那一侧，
    方言层拿到的是已经解好密的明文参数，换一种源库不用改 service。
    """

    host: str
    port: int
    user: str
    password: str
    # MySQL 恒 None（它的库在 scope 里表达，§2.2 末注）；PG 是目标库名
    database: str | None = None
    # §10.1：中文注释能不能信取决于连接字符集，必须显式 utf8mb4
    charset: str = "utf8mb4"
    connect_timeout_s: int = 10


@dataclass(slots=True, frozen=True)
class ExtractWarning:
    code: str
    detail: str


@dataclass(slots=True, frozen=True)
class ServerInfo:
    """`probe()` 的返回：版本决定支持不支持，字符集决定中文注释能不能信。"""

    kind: str
    server_version: str
    supports_max_execution_time: bool = False
    charset: str | None = None


@dataclass(slots=True, frozen=True)
class RawCatalog:
    catalog_name: str
    schema_name: str
    charset: str | None
    collation: str | None
    approx_size_bytes: int | None
    visible_table_count: int | None
    # §8.1 A 的 SUM(TABLE_ROWS)，落到 meta_database.approx_rows（§2.4）；默认 None 免得
    # 尚未算它的方言（测试桩）也要跟着改构造调用
    approx_rows: int | None = None
    # 该 schema 疑似因权限被 information_schema 隐藏（§6 的 SCHEMA_PARTIALLY_VISIBLE）
    grant_limited: bool = False


@dataclass(slots=True, frozen=True)
class RawTable:
    catalog_name: str
    schema_name: str
    table_name: str
    # 已归一：'BASE TABLE' | 'VIEW'
    table_type: str
    comment: str | None
    engine: str | None
    charset: str | None
    collation: str | None
    approx_rows: int | None
    data_bytes: int | None
    index_bytes: int | None
    # 供 UI "查看建表语句"，也是中文注释拿不到时的兜底来源（§8.1 末注）
    create_sql: str | None = None
    row_format: str | None = None
    last_analyze_at: dt.datetime | None = None


@dataclass(slots=True, frozen=True)
class RawColumn:
    catalog_name: str
    schema_name: str
    table_name: str
    column_name: str
    ordinal_position: int
    data_type: str
    raw_data_type: str
    nullable: bool
    default: str | None
    generated: bool
    comment: str | None
    char_length: int | None
    num_precision: int | None
    num_scale: int | None
    enum_values: tuple[str, ...] | None
    is_primary_key: bool
    indexed_columns: tuple[str, ...] = ()
    is_unique: bool = False
    is_indexed: bool = False
    collation: str | None = None


@dataclass(slots=True, frozen=True)
class RawIndexColumn:
    column_name: str | None
    seq_in_index: int
    collation: str | None
    sub_part: int | None


@dataclass(slots=True, frozen=True)
class RawIndex:
    catalog_name: str
    schema_name: str
    table_name: str
    index_name: str
    is_unique: bool
    is_primary: bool
    index_type: str
    comment: str | None
    cardinality: int | None
    columns: tuple[RawIndexColumn, ...]


@dataclass(slots=True, frozen=True)
class RawForeignKey:
    catalog_name: str
    schema_name: str
    table_name: str
    fk_name: str
    from_column: str
    to_catalog: str | None
    to_schema: str | None
    to_table: str
    to_column: str
    seq: int
    on_delete: str | None
    on_update: str | None


@dataclass(slots=True, frozen=True)
class InferredRelation:
    """按命名约定推出来的候选关系（§4：`source_kind='inferred'`，只有打分没有真 FK）。"""

    schema_name: str
    table_name: str
    column_name: str
    to_table_name: str
    to_column_name: str
    confidence: float


@dataclass(slots=True, frozen=True)
class ManifestBatch:
    """一批 = 一个 schema（MySQL 一个 db / PG 一个 schema），service 一批一个事务。"""

    catalog: RawCatalog
    tables: Sequence[RawTable]
    columns: Sequence[RawColumn] = ()
    indexes: Sequence[RawIndex] = ()
    foreign_keys: Sequence[RawForeignKey] = ()


@dataclass(slots=True)
class SourceManifest:
    kind: str
    server_version: str
    collected_at: dt.datetime
    catalogs: list[RawCatalog] = field(default_factory=list)
    tables: list[RawTable] = field(default_factory=list)
    columns: list[RawColumn] = field(default_factory=list)
    indexes: list[RawIndex] = field(default_factory=list)
    foreign_keys: list[RawForeignKey] = field(default_factory=list)
    warnings: list[ExtractWarning] = field(default_factory=list)
    # 触发规模保护（§6 的 MAX_TABLES）时为 True
    truncated: bool = False


@runtime_checkable
class Extractor(Protocol):
    """P2 用到的方言子集。§7 的 `stream_manifest` / `sample_distinct` 属 P3，先不假装实现。

    `collect` 的抽取范围由调用方渲染成 SQL 片段传进来（006/007 共用
    `datasource_service.table_scope_filter`），方言层因此不认识 ORM 行也不认识正则口径；
    `max_tables` 是 §6 的规模保护，超限在跑昂贵的列/索引查询之前就拒绝。
    """

    # `kind` 只要求**可读**：方言类上是 `kind: Final = "mysql"`，声明成裸属性会让
    # Protocol 要求"可写"，mypy 于是判 MySQLExtractor 不满足协议（read-only attribute）。
    @property
    def kind(self) -> str: ...

    async def probe(self) -> ServerInfo: ...

    async def discover(self) -> list[RawCatalog]: ...

    async def collect(
        self,
        catalogs: Sequence[RawCatalog],
        *,
        table_sql: str | None = None,
        table_params: Mapping[str, str] | None = None,
        max_tables: int | None = None,
    ) -> SourceManifest: ...

    async def close(self) -> None: ...
