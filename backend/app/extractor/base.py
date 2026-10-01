"""`SourceDialect` 的中间结构（逐字段对 `docs/metadata-model.md` §7）。

这些 dataclass 是**抽取层与落库层之间唯一的耦合点**：方言只负责把源库的东西变成 `Raw*`，
`sync_service` 只负责把 `Raw*` 变成 `meta_*` 行。加第三种源库不动 service，改卡片模板不动方言。

冻结（`frozen=True`）+ `slots=True`：抽取一批可能有几千条，路径上没人应该偷偷改上游对象。

唯一的例外是文件末尾的 `apply_index_flags`：它只吃这些 dataclass、不碰任何源库方言，
两个抽取器都要调它。放这里而不是留在 `mysql.py`，是为了不让 PG 那一侧出现
"方言模块之间互相 import"这条边——`batching.py` 消掉的是同一条边。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable

from app.core.errors import SCOPE_REMEDIES, ExtractScopeTooLarge

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
    """按命名约定推出来的候选关系（§4：`source_kind='inferred'`，只有打分没有真 FK）。

    `catalog_name` 跟着**发起侧那一列**走，而不是留空：§1 的规范化列里 MySQL 的 catalog
    恒空串、PG 的是目标库名，落库时两边都要靠它拼出 `meta_table` 的三元组查找键。
    """

    catalog_name: str
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
    # 工单 020：本轮读源库实际分了哪几批、每批哪些表名（顺序即发出顺序）。
    # 交回的是**名单**而不是批数：事件负载要说得出"这一批抽了谁"，而方言层不写事件。
    # 不分批的方言留空列表，`len(...)` 因此在 services 层只当"这一库没批处理"。
    batches: list[list[str]] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class ScopeCounts:
    """抽取范围内的对象数：SSE 那一帧的分母（§2.8 的 `total`/`base_table`/`view`）。

    必须是**带 scope 过滤**的计数。`RawCatalog.visible_table_count` 顶不上这个位置：
    它是 information_schema 的裸计数，演示库上数出来是 11（含建库脚本留下的下划线前缀
    对象），而 2026-09-27 拍板的口径是 10（9 张 BASE TABLE + 1 张 VIEW）。
    """

    total: int = 0
    base_table: int = 0
    view: int = 0


def ensure_within_max_tables(table_count: int, max_tables: int | None) -> None:
    """范围上限只在**昂贵查询之前**判一次：两方言这条门槛是同一条，报错的字面也因此同字。

    020 的批形状把 B 条按库发、C/D/E 按批发，判定若挪到后面就等于先把最贵的那几条发出去、
    再告诉调用方"范围太大"。`max_tables is None` 是"这道门槛关掉"，不是"上限为零"。
    """
    if max_tables is not None and table_count > max_tables:
        raise ExtractScopeTooLarge(
            f"抽取范围里有 {table_count} 张表，超过上限 {max_tables}",
            detail={
                "table_count": table_count,
                "max_tables": max_tables,
                "remedies": list(SCOPE_REMEDIES),
            },
        )


def apply_index_flags(columns: Sequence[RawColumn], indexes: Sequence[RawIndex]) -> list[RawColumn]:
    """把索引的结论回填到列上（§7：`is_primary_key` 来自索引，不是列上的旗标）。

    两方言共用：MySQL 读 `information_schema.STATISTICS`（§8.1 D），PG 读 `pg_index`
    （§8.2 D），归一后的 `RawIndex` 形状一样，回填规则因此也一样。表达式索引那一位的
    `column_name` 是 None，没有列可以打标，所以跳过。
    """
    # (表名, 列名) → (主键, 唯一, 被索引)；一个列同时出现在主键和唯一索引时取"或"
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


@runtime_checkable
class Extractor(Protocol):
    """P2 用到的方言子集 + 017 的范围计数。§7 的 `stream_manifest` / `sample_distinct`
    属 P3，先不假装实现。

    `collect` 的抽取范围由调用方渲染成 SQL 片段传进来（006/007 共用
    `datasource_service.table_scope_filter`），方言层因此不认识 ORM 行也不认识正则口径；
    `max_tables` 是 §6 的规模保护，超限在跑昂贵的列/索引查询之前就拒绝；
    `count_scope` 吃同一段范围片段，它存在的理由就是"每帧都得有分母"（§2.8）。
    `batch_size` / `batch_interval_ms` 是工单 020 加的第二对必填参数：**故意不给默认值**——
    这两个数的口径住在 `Settings.extract`（200 / 100），方言侧再写一遍就出现第二个定义点，
    而"改了配置不生效"正是这一片要闭掉的那类错。
    """

    # `kind` 只要求**可读**：方言类上是 `kind: Final = "mysql"`，声明成裸属性会让
    # Protocol 要求"可写"，mypy 于是判 MySQLExtractor 不满足协议（read-only attribute）。
    @property
    def kind(self) -> str: ...

    async def probe(self) -> ServerInfo: ...

    async def discover(self) -> list[RawCatalog]: ...

    async def count_scope(
        self,
        catalogs: Sequence[RawCatalog],
        *,
        table_sql: str | None = None,
        table_params: Mapping[str, str] | None = None,
    ) -> ScopeCounts: ...

    async def collect(
        self,
        catalogs: Sequence[RawCatalog],
        *,
        table_sql: str | None = None,
        table_params: Mapping[str, str] | None = None,
        max_tables: int | None = None,
        batch_size: int,
        batch_interval_ms: int,
    ) -> SourceManifest: ...

    async def close(self) -> None: ...
