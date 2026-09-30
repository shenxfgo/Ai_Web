"""一次同步：把抽取层的 `Raw*` 落进 `aiweb.meta_*`（列定义对 §2.4，写入语义对 §3–§5）。

这一层的全部价值在于**幂等**：同一份抽取结果跑两遍，行数不变、人工列不变。
§3 把这件事拆成三段不同的写入策略，每段对应一个 `*_upsert()` / `*_delete_*()` 构造器——
语句在这里造好，执行在 `run_sync()` 里，测试则直接编译语句钉它的形状（`test_sync_upsert_sql.py`）。

方言层（`app/extractor/`）不认识 ORM，这一层不认识 `information_schema`；两边只通过
`extractor/base.py` 的 `Raw*` 说话。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from time import monotonic
from typing import Any, cast

from sqlalchemy import Table, bindparam, delete, func, insert, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    AppError,
    ExtractScopeTooLarge,
    NotImplementedSource,
    SourceUnreachable,
)
from app.core.security import decrypt_secret
from app.core.sync_vocabulary import stage_of
from app.extractor.base import (
    ConnectionSpec,
    Extractor,
    InferredRelation,
    RawCatalog,
    RawColumn,
    RawForeignKey,
    RawIndex,
    RawTable,
    SourceManifest,
)
from app.extractor.mysql import MySQLExtractor
from app.models.datasource import DataSource
from app.models.meta import (
    MetaColumn,
    MetaDatabase,
    MetaIndex,
    MetaIndexColumn,
    MetaRelation,
    MetaTable,
    SyncJob,
    SyncJobEvent,
)
from app.services.datasource_service import (
    describe_source_error,
    root_cause,
    table_scope_filter,
)
from app.services.job_queue import SYNC_EVENT_CHANNEL
from app.services.kb_service import sync_cards
from app.services.nl2sql import join_graph
from app.services.relation_infer import high_degree_fields, infer_relations
from app.settings import get_settings

# 没启用 sqlalchemy 的 mypy 插件时，`DeclarativeBase.__table__` 被标成 `FromClause`，
# 于是 `insert(_TABLE)` 全都要报错。这里逐个 cast 而不是引插件：插件会顺带把 32 个文件
# 的 `Mapped[...]` 判定也换一套规则，不是这一片该付的账。
_TABLE = cast("Table", MetaTable.__table__)
_COLUMN = cast("Table", MetaColumn.__table__)
_RELATION = cast("Table", MetaRelation.__table__)
_INDEX = cast("Table", MetaIndex.__table__)
_INDEX_COLUMN = cast("Table", MetaIndexColumn.__table__)
_JOB = cast("Table", SyncJob.__table__)
_EVENT = cast("Table", SyncJobEvent.__table__)

# extracted 关系的属性全部来自源库（§8.1 E），每轮重算；created_by/confidence 不在这里，
# 前者属 manual、后者属 inferred，都不该被同步写。
# `deferability` 有意不进这份名单：§7 的 RawForeignKey 没有这一列、§8.1 E 也不 SELECT 它，
# 于是它永远进不了 rows——写进 set_ 就等于每轮把 EXCLUDED 的 NULL 同步进来，
# 将来谁从别的路子（比如手工编辑）写入的值会被下一轮同步清掉。列留着，等真有来源再接。
_RELATION_SYNC_COLS = ("fk_name", "on_delete", "on_update", "is_authors_enforced")

# §3 的两类列：同步列每轮吃 `EXCLUDED`（新值），人工列吃 `COALESCE(现有值, 新值)`。
# 名单在这里列全，是为了让"给 ORM 加了个人工列"必须同时改这里——漏改的后果是
# 那个字段每轮同步被清空，而这是 §3 承诺永远不会发生的事。
_TABLE_SYNC_COLS = (
    "database_id",
    "table_type",
    "comment_raw",
    "engine",
    "row_format",
    "charset",
    "collation",
    "approx_rows",
    "data_bytes",
    "index_bytes",
    "last_analyze_at",
)
_TABLE_HUMAN_COLS = ("comment_zh", "business_desc", "granularity")
_COLUMN_SYNC_COLS = (
    "ordinal_position",
    "data_type",
    "raw_data_type",
    "nullable",
    "default_value",
    "is_generated",
    "comment_raw",
    "is_primary_key",
    "is_unique",
    "is_indexed",
    "enum_values",
    "char_length",
    "numeric_precision",
    "numeric_scale",
)
_COLUMN_HUMAN_COLS = ("comment_zh", "business_desc")


# 库级没有人工列（§2.4：这一张的列全部来自 discover()，UI 上也没有可编辑项）
_DATABASE_SYNC_COLS = (
    "raw_collation",
    "raw_engine",
    "table_count",
    "approx_rows",
    "approx_size_bytes",
    "is_visible",
    "sync_job_id",
)


def _protect_human(table: Any, stmt: Any, names: tuple[str, ...]) -> dict[str, Any]:
    return {name: func.coalesce(table.c[name], getattr(stmt.excluded, name)) for name in names}


def _take_new(stmt: Any, names: tuple[str, ...]) -> dict[str, Any]:
    return {name: getattr(stmt.excluded, name) for name in names}


def meta_table_upsert() -> Any:
    """§3 第 1 段：自然键 upsert，人工列永不被同步覆盖。

    构造时不给 `values()`：执行时传 `list[dict]`，SQLAlchemy 按第一行的键渲染列，
    于是"这批行带哪些列"只由 `rows` 决定，语句模板保持一份。
    """
    stmt = pg_insert(_TABLE)
    return stmt.on_conflict_do_update(
        index_elements=[
            _TABLE.c.datasource_id,
            _TABLE.c.catalog_name,
            _TABLE.c.schema_name,
            _TABLE.c.table_name,
        ],
        set_={
            # 同步列：EXCLUDED = 本批新值
            **_take_new(stmt, _TABLE_SYNC_COLS),
            "is_stale": False,  # 本轮出现了，上一轮的陈旧标记作废
            # 人工列：COALESCE(现有值, 新值) = 库里已有人工值就保留，只有为空时才接受新值。
            # 方向不能反——`coalesce(excluded.x, 现有值)` 变成"新值优先"，第二次同步就赢了。
            **_protect_human(_TABLE, stmt, _TABLE_HUMAN_COLS),
            # 人工开关，原样保留（§3：连新值都不接受）
            "is_hidden": _TABLE.c.is_hidden,
            # 本轮同步时刻由语句自己的 now() 生成，不是批参数（§5 的陈旧判定拿它当钟）
            "synced_at": func.now(),
        },
    )


def meta_column_upsert() -> Any:
    """§3 第 2a 段：字段表与主表**同构**走 upsert。

    原文这里写的是 delete-then-insert，理由是"子表无人工字段"——不成立，见 §3 的
    as-built(0007) 注：`meta_column` 有 `comment_zh`/`business_desc`，全量替换会每轮清空它们。
    "源库删掉的列要消失"改由 `meta_column_sweep()` 补。
    """
    stmt = pg_insert(_COLUMN)
    return stmt.on_conflict_do_update(
        index_elements=[_COLUMN.c.table_id, _COLUMN.c.column_name],
        set_={
            **_take_new(stmt, _COLUMN_SYNC_COLS),
            **_protect_human(_COLUMN, stmt, _COLUMN_HUMAN_COLS),
            "synced_at": func.now(),
        },
    )


def meta_column_sweep() -> Any:
    """§3 as-built(0007)：删掉"本轮没碰过"的列，把 delete-then-insert 换回来的语义补上。

    `:synced_before` 由调用方传**本轮同步开始前沿库取到的时刻**（用 `sync_jobs.started_at`），
    不能传 `now()`：PG 的 `now()` 是事务时间戳，本轮 upsert 写的 `synced_at` 与它相等，
    于是刚写进去的列会被自己扫掉——或者反过来，同事务里永远扫不掉任何东西。
    """
    return delete(_COLUMN).where(
        _COLUMN.c.table_id.in_(bindparam("table_ids", expanding=True)),
        _COLUMN.c.synced_at < bindparam("synced_before"),
    )


def _extracted_scope() -> tuple[Any, ...]:
    """extracted 差分的作用域谓词：本数据源、只打 extracted、只打"本轮写完的那些库"。

    库级作用域是这次 review 抓出来的真 bug 的修复：DELETE 原来只按 datasource_id 收口，
    而 `keep` 只装**本 catalog** 刚 upsert 的 id——多 schema 时后写的库会把先写的库的
    extracted 边全删光（演示库只有一个 schema，所以 live 用例看不见）。与 `meta_table_mark_stale`
    同一条规矩（§5/§6 末条）：只有本轮确认完整枚举过的库参与删除差分。
    """
    return (
        _RELATION.c.datasource_id == bindparam("datasource_id"),
        # literal_column 而不是传字符串：字符串会编译成绑定参数，于是"这一刀只打
        # extracted"这个 §4 的关键作用域在语句文本里读不出来。这个值是代码里的常量、
        # 不是外部输入，内联进 SQL 才让单测能钉住它（review 时看语句就该看见）。
        _RELATION.c.source_kind == literal_column("'extracted'"),
        _RELATION.c.from_table_id.in_(
            select(_TABLE.c.id).where(
                _TABLE.c.database_id.in_(bindparam("database_ids", expanding=True))
            )
        ),
    )


def meta_relation_replace(rows: Sequence[Mapping[str, Any]]) -> Any:
    """§3 第 3 段：upsert 本轮的 extracted 关系，并顺手删掉"上一轮有、这一轮没有"的。

    形状是 §3 给的一条语句（`with keep as (insert ... returning id) delete ...`），不是两条：
    拆成两条的话，中间崩了就留下一张"旧关系已删、新关系没写"的关系表，而 §4 的
    delete-diff 恰恰只能作用于 `source_kind='extracted'`——manual/inferred 一行都不能碰。

    `rows` 不可为空：PG 的 executemany 空列表会整体不执行，连 DELETE 那半边也没了；
    空列表走 `meta_relation_prune()`。调用方（`_write_catalog`）二选一，**没有"跳过"这条路**——
    跳过就等于源库删光外键时上一轮的 extracted 边永远残留。
    """
    ins = pg_insert(_RELATION).values(list(rows))
    upsert = ins.on_conflict_do_update(
        index_elements=[_RELATION.c[name] for name in _RELATION_KEY],
        set_={
            **_take_new(ins, _RELATION_SYNC_COLS),
            "updated_at": func.now(),
        },
    ).returning(_RELATION.c.id)
    keep = upsert.cte("keep")
    return (
        delete(_RELATION)
        .add_cte(keep)
        .where(*_extracted_scope(), _RELATION.c.id.not_in(select(keep.c.id)))
    )


def meta_relation_prune() -> Any:
    """本轮这个库一条外键都没有：上一轮的 extracted 边整批收走（§4 delete-diff 的另一半）。"""
    return delete(_RELATION).where(*_extracted_scope())


def meta_index_purge() -> Any:
    """§3 第 2b 段：本轮涉及的表，索引整批删掉再插。

    只删父表这一张：`meta_index_column.index_id` 是 `ON DELETE CASCADE`，子行跟着走。
    """
    return delete(_INDEX).where(_INDEX.c.table_id.in_(bindparam("table_ids", expanding=True)))


_RELATION_KEY = (
    "datasource_id",
    "source_kind",
    "from_table_id",
    "from_column_name",
    "to_table_id",
    "to_column_name",
)


def meta_relation_inferred_upsert() -> Any:
    """§4：命名约定推断的边只 upsert，不参与删除差分。

    自然键里含 `source_kind`，所以一条边的 extracted 与 inferred 是两个行——
    验收 4 要"两者可区分"靠的就是这个，不是靠 confidence 空不空。
    """
    stmt = pg_insert(_RELATION)
    return stmt.on_conflict_do_update(
        index_elements=[_RELATION.c[name] for name in _RELATION_KEY],
        set_={"confidence": stmt.excluded.confidence, "updated_at": func.now()},
    )


def meta_table_mark_stale() -> Any:
    """§5：本轮没出现的表打陈旧标记，不物理删（`chat_messages` 还引用着它）。

    `:database_ids` 只准放 `known_complete=True` 的库（§6 末条）：权限看不到的库不进这个
    集合，"看不到"就永远不会变成"被标陈旧"。
    """
    return (
        update(_TABLE)
        .where(
            _TABLE.c.database_id.in_(bindparam("database_ids", expanding=True)),
            _TABLE.c.synced_at < bindparam("synced_before"),
        )
        # 同 §4 那个谓词：策略常量内联进 SQL，才能在语句文本里被读出来（绑定参数不可见）
        .values(is_stale=literal_column("true"))
    )


_DATABASE = cast("Table", MetaDatabase.__table__)


def meta_database_upsert() -> Any:
    """§2.4 的库级 upsert，RETURNING id 给 `meta_table.database_id` 用。

    MySQL 侧的三元组是 `('' , <db>)`、PG 侧是 `(<db>, <schema>)`（§1 的规范化列），
    冲突键必须是全三列：少一列就会让两个库里同名的 schema 互相覆盖。
    """
    stmt = pg_insert(_DATABASE)
    return stmt.on_conflict_do_update(
        index_elements=[
            _DATABASE.c.datasource_id,
            _DATABASE.c.catalog_name,
            _DATABASE.c.schema_name,
        ],
        set_={
            **_take_new(stmt, _DATABASE_SYNC_COLS),
            "updated_at": func.now(),
        },
    ).returning(_DATABASE.c.id)


# ============================================================ Raw* → 行字典
#
# 这一段是纯函数：抽取层的 dataclass 进，`meta_*` 的列名字典出。
# 全部规则都是"怎么把外键/推断边落到 table_id 上"，出错的样子是整批同步炸或静默丢关系，
# 所以它们比编排更值得被单独钉住。


def _key(catalog_name: str, schema_name: str, table_name: str) -> tuple[str, str, str]:
    return (catalog_name, schema_name, table_name)


def database_row(datasource_id: int, job_id: int, catalog: RawCatalog) -> dict[str, Any]:
    return {
        "datasource_id": datasource_id,
        "catalog_name": catalog.catalog_name,
        "schema_name": catalog.schema_name,
        "raw_collation": catalog.collation,
        "raw_engine": None,  # MySQL 没有"库级引擎"这一说；PG 侧才填
        "table_count": catalog.visible_table_count,
        "approx_rows": catalog.approx_rows,
        "approx_size_bytes": catalog.approx_size_bytes,
        # §6：这个字段是"权限不可见"的告警依据，P2 只有真枚举到的库才写行，恒 true
        "is_visible": not catalog.grant_limited,
        "sync_job_id": job_id,
    }


def table_rows(
    datasource_id: int, database_id: int, tables: Sequence[RawTable]
) -> list[dict[str, Any]]:
    """人工列（comment_zh/business_desc/granularity/is_hidden）一律不进这个字典。

    upsert 的 INSERT 分支因此用 server_default，DO UPDATE 分支用 COALESCE/原样保留——
    两条路径都不会拿"本轮没提供"当成"该清空"。`table_uid` 是生成列，写进去会被 PG 拒。
    """
    return [
        {
            "datasource_id": datasource_id,
            "database_id": database_id,
            "catalog_name": t.catalog_name,
            "schema_name": t.schema_name,
            "table_name": t.table_name,
            "table_type": t.table_type,
            "comment_raw": t.comment,
            "engine": t.engine,
            "row_format": t.row_format,
            "charset": t.charset,
            "collation": t.collation,
            "approx_rows": t.approx_rows,
            "data_bytes": t.data_bytes,
            "index_bytes": t.index_bytes,
            "last_analyze_at": t.last_analyze_at,
        }
        for t in tables
    ]


def column_rows(table_id: dict[tuple[str, str, str], int], columns: Sequence[RawColumn]) -> Any:
    out: list[dict[str, Any]] = []
    for c in columns:
        tid = table_id.get(_key(c.catalog_name, c.schema_name, c.table_name))
        if tid is None:
            continue  # 表本身不在本轮范围内（范围外键带进来的邻居表），整列跳过
        out.append(
            {
                "table_id": tid,
                "ordinal_position": c.ordinal_position,
                "column_name": c.column_name,
                "data_type": c.data_type,
                "raw_data_type": c.raw_data_type,
                "nullable": c.nullable,
                "default_value": c.default,
                "is_generated": c.generated,
                "comment_raw": c.comment,
                "is_primary_key": c.is_primary_key,
                "is_unique": c.is_unique,
                "is_indexed": c.is_indexed,
                "enum_values": list(c.enum_values) if c.enum_values is not None else None,
                "char_length": c.char_length,
                "numeric_precision": c.num_precision,
                "numeric_scale": c.num_scale,
            }
        )
    return out


def index_rows(
    table_id: dict[tuple[str, str, str], int], indexes: Sequence[RawIndex]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 (meta_index 行, meta_index_column 的"半成品"行)。

    子行的 `index_id` 此刻还不存在——父行刚删重插，而 `executemany + RETURNING` 在
    PG 上不保证回吐顺序，所以不指望它。子行先带 `(table_id, index_name)` 这个自然键，
    由 `resolve_index_ids()` 在父行插完后照真表回填。
    """
    parents: list[dict[str, Any]] = []
    children: list[dict[str, Any]] = []
    for idx in indexes:
        tid = table_id.get(_key(idx.catalog_name, idx.schema_name, idx.table_name))
        if tid is None:
            continue
        parents.append(
            {
                "table_id": tid,
                "index_name": idx.index_name,
                "is_unique": idx.is_unique,
                "is_primary": idx.is_primary,
                "index_type": idx.index_type,
                "comment": idx.comment,
                "cardinality": idx.cardinality,
            }
        )
        for col in idx.columns:
            children.append(
                {
                    "table_id": tid,
                    "index_name": idx.index_name,
                    "seq_in_index": col.seq_in_index,
                    "column_name": col.column_name,
                    "collation": col.collation,
                    "sub_part": col.sub_part,
                }
            )
    return parents, children


def resolve_index_ids(
    children: Sequence[dict[str, Any]], existing: Sequence[tuple[int, int, str]]
) -> list[dict[str, Any]]:
    """把 `(table_id, index_name)` 换成真的 `index_id`（§2.4 的自然键 → 代理键）。

    `existing` 是刚从库里读回来的 `(id, table_id, index_name)`。查不到的那条直接丢：
    它意味着父行没插进去，而父行插不进去的原因（比如唯一键撞了）不该由子行再报一次。
    """
    by_name: dict[tuple[int, str], int] = {(tid, name): idx_id for idx_id, tid, name in existing}
    out: list[dict[str, Any]] = []
    for child in children:
        index_id = by_name.get((child["table_id"], str(child["index_name"])))
        if index_id is None:
            continue
        out.append(
            {
                "index_id": index_id,
                "seq_in_index": child["seq_in_index"],
                "column_name": child["column_name"],
                "collation": child["collation"],
                "sub_part": child["sub_part"],
            }
        )
    return out


def relation_rows(
    datasource_id: int,
    table_id: dict[tuple[str, str, str], int],
    foreign_keys: Sequence[RawForeignKey],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """真外键 → extracted 行；两端任一头不在本轮范围里就跳过并 warning，不硬留着插。

    留下的代价是当场外键约束violation（`from_table_id` 是 NOT NULL 的 FK），
    一条悬空引用会把**整个同步**打成 failed，而不是少一条边。
    """
    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for fk in foreign_keys:
        from_id = table_id.get(_key(fk.catalog_name, fk.schema_name, fk.table_name))
        # MySQL 的 REFERENCED_TABLE_SCHEMA 为 NULL 时意思是"同库"，不是"没有目标"
        to_schema = fk.to_schema or fk.schema_name
        to_catalog = fk.to_catalog if fk.to_catalog is not None else fk.catalog_name
        to_id = table_id.get(_key(to_catalog, to_schema, fk.to_table))
        if from_id is None or to_id is None:
            skipped.append(
                {
                    "code": "RELATION_OUT_OF_SCOPE",
                    "detail": (
                        f"{fk.schema_name}.{fk.table_name}.{fk.from_column} → "
                        f"{to_schema}.{fk.to_table}：有一端不在本轮抽取范围内"
                    ),
                }
            )
            continue
        rows.append(
            {
                "datasource_id": datasource_id,
                "source_kind": "extracted",
                "fk_name": fk.fk_name,
                "from_table_id": from_id,
                "from_column_name": fk.from_column,
                "to_table_id": to_id,
                "to_column_name": fk.to_column,
                "on_delete": fk.on_delete,
                "on_update": fk.on_update,
                "is_authors_enforced": True,
            }
        )
    return rows, skipped


def inferred_rows(
    datasource_id: int,
    table_id: dict[tuple[str, str, str], int],
    edges: Sequence[InferredRelation],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for edge in edges:
        from_id = table_id.get(_key("", edge.schema_name, edge.table_name))
        to_id = table_id.get(_key("", edge.schema_name, edge.to_table_name))
        if from_id is None or to_id is None:
            continue
        rows.append(
            {
                "datasource_id": datasource_id,
                "source_kind": "inferred",
                "from_table_id": from_id,
                "from_column_name": edge.column_name,
                "to_table_id": to_id,
                "to_column_name": edge.to_column_name,
                "confidence": edge.confidence,
                "is_authors_enforced": False,
            }
        )
    return rows


# ================================================================== 编排


@dataclass(slots=True)
class _Tally:
    """写进 `sync_jobs.counters` 的那几个数（§2.5）。"""

    databases: int = 0
    tables: int = 0
    columns: int = 0
    indexes: int = 0
    relations_extracted: int = 0
    relations_inferred: int = 0
    tables_stale: int = 0
    tables_failed: int = 0
    # 工单 008：卡片条数（不是表条数——宽表一张表出多张卡，§6）
    cards: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)

    def merge(self, other: _Tally) -> None:
        """把"这一轮确实提交完了"的那个分账并进总账（只有提交成功的 catalog 才走这里）。"""
        for name in self.__dataclass_fields__:
            if name != "tables_failed":
                # tables_failed 由编排层单独加，合并时不该被"成功的那一批"抵掉
                setattr(self, name, getattr(self, name) + getattr(other, name))


@dataclass(slots=True)
class SyncOutcome:
    """`run_sync` 的返回值：这一轮跑成了什么样。

    工单 016 之后它**不是** HTTP 响应体——端点只回 `{job_id}`，这一份的去处是 worker 的
    stdout 和用例的断言面（同样的内容已经写进 `sync_jobs` 那一行）。
    """

    job_id: int
    status: str
    counters: dict[str, int]
    warnings: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    duration_ms: int


@dataclass(slots=True)
class _Progress:
    """SSE 每帧的那五个键（§2.8 的 `counters`）：分母一次算定，分子跟着提交往上涨。

    它和 `_Tally` 是两个东西，所以不合并：`_Tally` 是 `sync_jobs.counters` 那本**行级**总账
    （列数、索引数、关系数…），这一份是"进度条走到哪了"的**对象级**账（多少个库内对象落成了）。
    把两者并成一份的话，前端要么拿到行数列数去画百分比，要么丢掉排障要的那本明细。
    """

    total: int = 0
    base_table: int = 0
    view: int = 0
    done: int = 0
    cards: int = 0

    def as_dict(self, *, done: int | None = None) -> dict[str, int]:
        """`done` 允许传"这一帧之后"的值：事件行宣布的是**这一步落成**的对象数，
        而总账要等它所在的那个事务提交才涨（`_write_catalog` 里就是这种情况）。"""
        return {
            "done": self.done if done is None else done,
            "total": self.total,
            "base_table": self.base_table,
            "view": self.view,
            "cards": self.cards,
        }


async def _add_event(
    session: AsyncSession,
    job_id: int,
    phase: str,
    counters: dict[str, int],
    payload: dict[str, Any] | None = None,
) -> None:
    """追加一行事件并叫醒监听者：**不开新事务**，跟着调用方那次 commit 一起落。

    这是全仓写 `sync_job_event` 的唯一入口（工单 017 的"唯一"两处之一，另一处是
    `stage_of` 的翻译），粗档 `stage` 就是在这里由细值 `phase` 现算的——第二处翻译会让
    两套词表各自漂移，而漂移在进度条上表现为"某一档永远不出现"。

    `seq` 从库里现算（`max(seq)+1`）而不是在内存里递增：某个库回滚时它占掉的那个号要被
    下一个事件复用，否则事件流留空洞，而空洞在 SSE 那边看起来就是"丢了一条进度"。
    `UNIQUE (job_id, seq)` 是"一个作业只有一个写事件的人"的兜底——claim 保证单写，
    索引保证真来了第二个写者时是报错而不是两行同 seq。

    `pg_notify` 与插入同事务：没提交的事不许叫醒读的人（否则读到的还是上一条），
    提交了的事不许没人被叫醒（NOTIFY 在无监听者那一刻永久丢失，所以它只当闹钟，
    真相在事件行里 —— ADR-0010）。
    """
    seq = (
        await session.execute(
            select(func.coalesce(func.max(_EVENT.c.seq), 0) + 1).where(_EVENT.c.job_id == job_id)
        )
    ).scalar_one()
    await session.execute(
        insert(_EVENT).values(
            job_id=job_id,
            seq=int(seq),
            stage=stage_of(phase),
            phase=phase,
            counters=counters,
            payload=payload or {},
        )
    )
    await session.execute(select(func.pg_notify(SYNC_EVENT_CHANNEL, str(job_id))))


async def _set_phase(
    session: AsyncSession,
    job_id: int,
    phase: str,
    *,
    status: str | None = None,
    counters: dict[str, int] | None = None,
    warnings: list[dict[str, Any]] | None = None,
    errors: list[dict[str, Any]] | None = None,
    finished: bool = False,
    commit: bool = True,
    event_counters: dict[str, int] | None = None,
    event_payload: dict[str, Any] | None = None,
) -> None:
    """`sync_jobs` 的推进（§6 的锁与心跳都靠这一处）；给了 `event_counters` 就顺带追一帧事件。

    `commit=False` 只为一个场景存在：事件要和它所描述的那次元数据写入**同生共死**
    （§2.8 的"只追加"要靠同一个事务才撤销得了）。那时 phase 的写、事件的写、meta_* 的写
    并进同一次 commit，而 phase 仍然只有一处写、stage 仍然只有一处翻译。
    """
    values: dict[str, Any] = {"phase": phase}
    if status is not None:
        values["status"] = status
    if counters is not None:
        values["counters"] = counters
    if warnings is not None:
        values["warnings"] = warnings
    if errors is not None:
        values["errors"] = errors
    if finished:
        values["finished_at"] = func.now()
    await session.execute(update(_JOB).where(_JOB.c.id == job_id).values(**values))
    if event_counters is not None:
        await _add_event(session, job_id, phase, event_counters, event_payload)
    if commit:
        await session.commit()


def _embed_skipped() -> dict[str, Any]:
    """embed 那一帧的理由（§2.8 的 payload：`{skipped, code, detail}`）。

    两支分开写：「这台机器没配向量端点」和「配了但 P3 不实现向量化」是两件不同的事，
    只说一支的话，另一支的读者会拿到一句假话——而这条 detail 是前端唯一能显示的说明。
    """
    if get_settings().embedding.configured:
        return {
            "skipped": True,
            "code": "embedding_not_implemented",
            "detail": "P3 只建元数据与卡片，向量化归 P4",
        }
    return {
        "skipped": True,
        "code": "embedding_not_configured",
        "detail": "embedding 三键（base_url / api_key / model）为空，向量化归 P4",
    }


def _why(exc: BaseException) -> str:
    """把异常收成一句能进 `sync_jobs.errors[]` 的话：不带驱动原文，不截出半截 SQL。"""
    if isinstance(exc, AppError):
        return str(exc)[:500]
    return describe_source_error(root_cause(exc))


def _error_entry(exc: BaseException, *, fallback_code: str = "sync_failed") -> dict[str, Any]:
    """终局那一格的形状：`{code, detail}`，分类过的 AppError 再多带一个 `data`。

    `data` 存在的理由是进程分离之后换来的：P2 时代 `ExtractScopeTooLarge.detail` 里那三条出路
    （§6 点名要结构化返回的东西）是走 HTTP 错误 envelope 到前端的，而 202 的响应体里只有
    `job_id`——这一列成了它到达前端的唯一一跳。只留 `_why` 那句人话，前端就只剩一个 code 能看。

    `detail` 一律是字符串（结构留在 `data`），否则读这一列的人要为同一个键写两种分支。

    `fallback_code` 是"没分类过的异常算哪一档"：同一个异常在"整库抽取失败"那一档是
    `extract_failed`，在"整个作业失败"这一档是 `sync_failed`——分档由**抛出点**决定，
    不由异常类型决定，所以它是入参而不是从 `exc` 上推。
    """
    entry: dict[str, Any] = {
        # 只认我们自己分类过的 code。SQLAlchemy 的异常**也**有 `.code`（文档码，真库里实测到的是
        # "cd3x" 这种），拿 getattr 兜默认值等于把内部码当错误分类回给前端。
        "code": exc.code if isinstance(exc, AppError) else fallback_code,
        "detail": _why(exc),
    }
    if isinstance(exc, AppError) and exc.detail is not None:
        entry["data"] = exc.detail
    return entry


def _extractor_for(ds: DataSource, spec: ConnectionSpec) -> Extractor:
    """§9 的驱动表：抽取这一路 MySQL 用 pymysql 同步方言，PG 侧还没做。

    没实现的一种要回 501 而不是 500——"这个源类型我们还不支持"是可自救的信息，
    而 `KeyError` 到了前端只是一个"数据访问失败"。
    """
    if ds.kind == "mysql":
        return MySQLExtractor(spec)
    raise NotImplementedSource(f"kind={ds.kind} 的元数据抽取尚未实现")


async def _table_ids(
    session: AsyncSession, database_id: int, synced_before: dt.datetime
) -> dict[tuple[str, str, str], int]:
    """把刚 upsert 的表读回成 {四元组 → id}。

    不用 `INSERT ... RETURNING`：executemany 带 RETURNING 在 PG 上不保证与入参同序，
    一旦错位，后面所有列/索引/关系都会挂到**别的表**上——那是要等到有人打开表详情页才发现的错。
    以 `synced_at >= 本轮起钟` 反查本轮触达的行，天然只覆盖这一批。
    """
    rows = await session.execute(
        select(
            _TABLE.c.id,
            _TABLE.c.catalog_name,
            _TABLE.c.schema_name,
            _TABLE.c.table_name,
        ).where(_TABLE.c.database_id == database_id, _TABLE.c.synced_at >= synced_before)
    )
    return {_key(catalog, schema, table): int(rid) for rid, catalog, schema, table in rows}


async def _write_catalog(
    session: AsyncSession,
    ds: DataSource,
    job_id: int,
    manifest: SourceManifest,
    synced_before: dt.datetime,
    warnings: list[dict[str, Any]],
    progress: _Progress,
) -> tuple[int, _Tally]:
    """§3 的三段式：库 → 表 → 列/索引 → 关系，一个 schema 一个事务（§3"每个 batch 一个事务"，
    P2 不分批，所以一个 catalog 就是一个 batch）。

    计数**只在这一个事务提交成功之后**才交回调用方合并（返回 `part`，不在这里碰总账）：
    失败的 catalog 已经 rollback，行一条都没落，但本函数的 `part` 是边写边涨的——
    当场涨进总账就等于对前端宣布"10 张表同步好了"，而表里其实一张都没有。
    `partial` 的计数必须是"确实写完了的那些"，否则这个状态就是假绿的出口。
    """
    part = _Tally()
    catalog = manifest.catalogs[0]
    database_id = int(
        (
            await session.execute(meta_database_upsert(), [database_row(ds.id, job_id, catalog)])
        ).scalar_one()
    )
    await session.execute(
        meta_table_upsert(),
        table_rows(ds.id, database_id, manifest.tables),
    )
    table_id = await _table_ids(session, database_id, synced_before)
    part.databases += 1
    part.tables += len(table_id)

    await session.execute(meta_column_upsert(), column_rows(table_id, manifest.columns))
    await session.execute(
        meta_column_sweep(),
        {
            "table_ids": sorted(table_id.values()),
            "synced_before": synced_before,
        },
    )
    parents, children = index_rows(table_id, manifest.indexes)
    await session.execute(meta_index_purge(), {"table_ids": sorted(table_id.values())})
    if parents:
        await session.execute(pg_insert(_INDEX), parents)
    existing = await session.execute(
        select(_INDEX.c.id, _INDEX.c.table_id, _INDEX.c.index_name).where(
            _INDEX.c.table_id.in_(sorted(table_id.values()))
        )
    )
    resolved = resolve_index_ids(children, [(int(i), int(t), str(n)) for i, t, n in existing])
    if resolved:
        await session.execute(pg_insert(_INDEX_COLUMN), resolved)
    part.columns += len(manifest.columns)
    part.indexes += len(parents)

    rows, skipped = relation_rows(ds.id, table_id, manifest.foreign_keys)
    warnings.extend(skipped)
    # 这里没有"跳过"这条路：外键为 0 也必须走 prune，否则源库删光外键后上一轮的
    # extracted 边永远残留（§4 的 delete-diff 是"本轮没有就收走"，不是"本轮有才维护"）。
    await session.execute(
        meta_relation_replace(rows) if rows else meta_relation_prune(),
        {"datasource_id": ds.id, "database_ids": [database_id]},
    )
    part.relations_extracted += len(rows)

    edges = infer_relations(manifest.columns, manifest.foreign_keys)
    inferred = inferred_rows(ds.id, table_id, edges)
    if inferred:
        await session.execute(meta_relation_inferred_upsert(), inferred)
    part.relations_inferred += len(inferred)

    for warn in manifest.warnings:
        warnings.append({"code": warn.code, "detail": warn.detail})
    # §5.3 末"候选度 > 8"的写侧一半：只记警告，上面的边已经写完、一条都不回退（工单 014 拍板）。
    for warn in high_degree_fields(
        manifest.columns, max_degree=get_settings().retrieval.max_join_degree
    ):
        warnings.append({"code": warn.code, "detail": warn.detail})
    # 进度事件与它宣布的那次元数据写入在同一个事务里（工单 017 的拍板）：这个库失败回滚时
    # 这一行也一起消失，事件流里就不会出现"宣布了一个没落成的 upsert"。
    # `done` 传的是"这一步之后"的数——总账要等这次 commit 才涨，而事件说的是这一帧。
    await _set_phase(
        session,
        job_id,
        "tables",
        commit=False,
        event_counters=progress.as_dict(done=progress.done + part.tables),
        event_payload={"schema": catalog.schema_name},
    )
    await session.commit()
    return database_id, part


async def run_sync(
    session: AsyncSession,
    ds: DataSource,
    *,
    job_id: int,
    synced_before: dt.datetime,
    force: bool = False,
    card_build_hook: Callable[[str], None] | None = None,
) -> SyncOutcome:
    """一次完整同步：连源库 → 抽 → 落库 → 收尾写 `sync_jobs`。

    **作业已经不是这里开的**（工单 016）：那一行在 `job_queue.enqueue` 落成 pending，
    由 worker 的 `claim` 打成 running，这里拿到的 `job_id` 一定是 running 的，
    而 `synced_before` 就是 claim 那一刻库里生成的 `started_at`——本轮所有"这行是不是
    本轮写的"的判断都以它为钟。

    `force` 是 §6 给 admin 的"知道大还是要点"的口子（工单 021 已接：请求体 `{"force":true}`
    → `sync_jobs.force` → `ClaimedJob.force` → 这里）。它**只**关掉 `MAX_TABLES` 一档——
    scope 过滤（include/exclude）、删除差分、规模之外的行为一律不变，不是跳过所有保护的
    万能钥匙。

    `card_build_hook` 是工单 019 的 **test-only** 注入点：由 worker 的进入点按参数传进来、
    原样交给 `sync_cards`，生产路径（`loop()`）恒为 `None`。单表构建失败在卡片侧逐表收口
    （提交/回滚各归各表），这里只负责把失败记进 `errors` 并把终局判成 `partial`。

    本函数的三条不变量（都被 `tests/integration/test_sync_pg.py` 钉着）：

    1. **`sync_jobs` 那行一定有终局状态**，包括异常路径。`ux_sync_running` 是部分唯一索引，
       一行留在 'running' 就等于把这条源永久锁死——此后每次同步都 409，只能手工进库删行。
    2. **`counters` 只报确实提交完的行数**：失败的 catalog 走 rollback，一行都没落，
       它的分账就不许并进总账（所以 `_write_catalog` 把 `part` 交回来由这里合并）。
    3. **本轮真提交过的每个库都要清一次 JOIN 图缓存**（工单 014 拍板 2），也在这条 finally 里。
       放 finally 而不是 try 末尾是因为"一个 catalog 一个事务"：后面的库失败时，前面已经提交的
       那些库的 `meta_relation` 已经变了，缓存在那次失败之后照样是脏的。

    不变量 3 与另外两条同住的理由也一样朴素：三条都是"抛出去的那条路径上也要成立"的事。
    """
    started = monotonic()
    tally = _Tally()
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    status = "success"
    # 在 try 之前声明：终局那一次清缓存要看得见它，而循环之前的任何一步
    # （解密、probe、discover）都可能直接抛出去，那时这个名字必须已经绑到一个空列表上。
    # `progress` 同一条理由：收尾那一帧事件也要看得见它，没数过就是全 0，不能是 NameError。
    completed_database_ids: list[int] = []
    progress = _Progress()

    extractor: Extractor | None = None
    try:
        # 解密/建 spec/取方言都必须在 try 里：这三步一炸（密钥坏掉、kind 还没实现的 501…）
        # 也要给 job 写终局，否则那行 'running' 会让 ux_sync_running 把这条源永久锁死。
        password = decrypt_secret(ds.secret_enc)  # 口令的明文只活在本函数这几行里
        spec = ConnectionSpec(
            host=ds.host,
            port=ds.port,
            user=ds.connect_user,
            password=password,
            database=ds.catalog_name or None,
        )
        extractor = _extractor_for(ds, spec)
        server = await extractor.probe()
        ds.server_version = server.server_version
        catalogs = await extractor.discover()
        wanted = set(ds.include_schemas or [])
        catalogs = [c for c in catalogs if not wanted or c.schema_name in wanted]
        scope_sql, scope_params = table_scope_filter(ds, column="t.table_name")
        # 分母先于第一帧（2026-09-30 拍板）：SSE 每一帧都要能说出"x/y"，而 `collect` 的
        # manifest 要到第一个库抽完才拿到手。这一条只发 B 那条的分组计数，昂贵的 C/D/E 一条不发。
        counts = await extractor.count_scope(
            catalogs, table_sql=scope_sql, table_params=scope_params
        )
        progress = _Progress(total=counts.total, base_table=counts.base_table, view=counts.view)
        await _set_phase(
            session,
            job_id,
            "discover",
            counters=tally.as_dict(),
            event_counters=progress.as_dict(),
        )

        max_tables = get_settings().extract.max_tables
        for catalog in catalogs:
            try:
                manifest = await extractor.collect(
                    [catalog],
                    table_sql=scope_sql,
                    table_params=scope_params,
                    max_tables=None if force else max_tables,
                )
                database_id, part = await _write_catalog(
                    session, ds, job_id, manifest, synced_before, warnings, progress
                )
                completed_database_ids.append(database_id)
                tally.merge(part)
                progress.done += part.tables
            except ExtractScopeTooLarge:
                raise  # 这是"拒绝开工"，不是"某一批失败"：不许记成 partial 继续下一个库
            except Exception as exc:  # 宽是故意的：见下面的注释
                # 一个库失败不该把整次请求打成 500，尤其当后面的库还有得抽、前面的库已经提交完。
                # 驱动在这里能抛的异常种类没法穷举（pymysql 的 "Already closed"、IS 查询超时…），
                # 穷举 `except (A, B, C)` 的结果就是漏掉的那个变成 500 + 一行卡在 running 的 job。
                await session.rollback()
                status = "partial"
                tally.tables_failed += 1
                entry = _error_entry(exc, fallback_code="extract_failed")
                # 库名前缀：一个作业可以抽多个库，只说"索引查询失败"分不出是哪个库失败。
                entry["detail"] = f"{catalog.schema_name}: {entry['detail']}"
                errors.append(entry)
        # §5 + §6 末条：只有"本轮确认完整枚举过"的库参与陈旧判定。一个都没成功就整段跳过——
        # 空集合会让 expanding bindparam 渲染成 `IN ()`（语法错误），而"什么都没同步到"
        # 恰恰最不该被理解成"上次同步的东西全过期了"。
        if completed_database_ids:
            # 工单 008（kb-workflow §7）：card_build 是同步作业里第二个落库 phase。
            # 放在整段抽取之后而不是每个 catalog 内部，是因为它的输入是**本轮确认完整的库的集合**
            # （`completed_database_ids`），循环里任何一个都还没拿到这份名单。
            await _set_phase(
                session,
                job_id,
                "card_build",
                counters=tally.as_dict(),
                event_counters=progress.as_dict(),
            )
            synced = await session.execute(
                select(_TABLE.c.id).where(
                    _TABLE.c.database_id.in_(completed_database_ids),
                    _TABLE.c.synced_at >= synced_before,
                )
            )
            # 只给"本轮真的落成了"的表建卡：陈旧表源库已无实体，给它刷一张新卡等于
            # 继续向 AI 保证一张不存在的表。
            card_result = await sync_cards(
                session,
                datasource_id=ds.id,
                job_id=job_id,
                table_ids=[int(i) for i in synced.scalars()],
                dialect_name=ds.kind,
                server_version=ds.server_version or "",
                card_build_hook=card_build_hook,
            )
            tally.cards = card_result.cards
            # 逐表提交（工单 019）之后这里**没有**统一 commit：每张表的成功或失败都在
            # `sync_cards` 里各自收口了。单表失败不中断——与上面 per-catalog 的兜底同构：
            # 错误按 `sync_jobs.errors` 的既有形状 `{code, detail}` 追加（§2.5 as-built P3-016），
            # 表名进 `detail`（§2.8 的 payload 示例点名 `card_build_failed`），终局判 `partial`。
            # 为什么不塞进 `_write_catalog`：`_set_phase` 自己会 commit，卡片提交与 phase
            # 提交不许并进同一个事务——那等于把"每库一事务"当场切两半（§6 已定口径）。
            for failure in card_result.failures:
                status = "partial"
                entry = _error_entry(failure.error, fallback_code="card_build_failed")
                # 表名前缀：与库级失败那条同一条理由——一轮里几十张表，只说"注入故障"分不出炸了谁。
                entry["detail"] = f"{failure.table_full_name}: {entry['detail']}"
                errors.append(entry)
            progress.cards = tally.cards

            stale = await session.execute(
                meta_table_mark_stale(),
                {
                    "database_ids": completed_database_ids,
                    "synced_before": synced_before,
                },
            )
            await session.commit()
            tally.tables_stale = int(stale.rowcount) if isinstance(stale, CursorResult) else 0
            # embed 档在 P3 什么都不做（向量检索归 P4），但**这一帧照样发**：少了它，进度条会
            # 从 card_build 直接跳到 done，"这台机器没配向量端点"这件事就永远说不出来（故事 11）。
            # 它跟卡片那一步同进同退：一个库都没落成时没有卡片，也就没有"本来要向量化的东西"。
            await _set_phase(
                session,
                job_id,
                "embed",
                counters=tally.as_dict(),
                event_counters=progress.as_dict(),
                event_payload=_embed_skipped(),
            )
        # 只有走完全程才更新它：失败的同步不该把"最近一次同步"往前推（UI 拿它判断新鲜度）
        ds.last_sync_at = dt.datetime.now(dt.UTC)
    except (AppError, SQLAlchemyError) as exc:
        await session.rollback()
        status = "failed"
        errors.append(_error_entry(exc))
        if isinstance(exc, SQLAlchemyError):
            raise SourceUnreachable(describe_source_error(root_cause(exc))) from exc
        raise
    except Exception as exc:
        # 没分类过的异常也要先落终局再抛：让裸异常直接冒出去，`sync_jobs` 那行会永远停在
        # 'running'，而 `ux_sync_running` 是**部分唯一索引**——于是这条源此后每次同步都 409，
        # 用户侧的表现是"这个源再也点不动了"，只能手工进库删那行才能救。
        await session.rollback()
        status = "failed"
        errors.append(_error_entry(exc))
        raise
    finally:
        # 构造方言之前的失败（密钥坏、501）没有 extractor 可关，但终局状态照样要写。
        if extractor is not None:
            await extractor.close()
        # 不变量 3：本轮提交过的每个库各清一次，键精确到 `(源, 库)` 而不是整源——
        # 一次只同步一个源的作业，把别的源的图一起清掉是白工（它们没人动过）。
        for database_id in completed_database_ids:
            join_graph.invalidate(ds.id, database_id)
        # 收尾写在 finally 里而不是 try 的末尾：上面三条 raise 路径都必须有终局状态。
        duration_ms = round((monotonic() - started) * 1000)
        await _set_phase(
            session,
            job_id,
            "done",
            status=status,
            counters=tally.as_dict(),
            warnings=warnings,
            errors=errors,
            finished=True,
            event_counters=progress.as_dict(),
        )

    return SyncOutcome(
        job_id=job_id,
        status=status,
        counters=tally.as_dict(),
        warnings=warnings,
        errors=errors,
        duration_ms=duration_ms,
    )
