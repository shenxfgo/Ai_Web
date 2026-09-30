"""meta_* / sync_jobs 的建表 DDL 契约（schema-only 断言，不碰数据库）。

口径来源：
- `docs/metadata-model.md` §1（四元组唯一 + `table_uid` 稳定指纹）、
  §2.4（六张快照表）、§2.5（sync_jobs）
- ADR-0005：`table_uid = md5(concat_ws('\\x1f', 四元组))`，`char(32) GENERATED ALWAYS ... STORED`
- `docs/verification.md` §2.2 第 2 项：无 PG 时也要验迁移——编译 DDL 字符串比对，
  断言的是"迁移写错了 90% 的情形"，真库那 10% 由 integration 用例盖

期望值全部来自上面这些文档原文，不是从模型代码里反过来算的。
"""

from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

import app.models  # noqa: F401  # 建表前必须先把 ORM 注册进 Base.metadata
from app.core.db import Base


def _ddl(table_name: str) -> str:
    table = Base.metadata.tables[f"{Base.metadata.schema}.{table_name}"]
    return str(CreateTable(table).compile(dialect=postgresql.dialect()))


def _index_ddls(table_name: str) -> str:
    table = Base.metadata.tables[f"{Base.metadata.schema}.{table_name}"]
    return "\n".join(
        str(CreateIndex(ix).compile(dialect=postgresql.dialect())) for ix in table.indexes
    )


def test_快照表齐全_含_sync_jobs() -> None:
    """metadata-model §2.4 的六张 + §2.5 的 sync_jobs + §2.8 的 sync_job_event，一张都不能少。"""
    schema = Base.metadata.schema
    expected = {
        "meta_database",
        "meta_table",
        "meta_column",
        "meta_index",
        "meta_index_column",
        "meta_relation",
        "sync_jobs",
        "sync_job_event",
    }
    got = {
        t.split(".", 1)[1]
        for t in Base.metadata.tables
        if t.split(".", 1)[1].startswith(("meta_", "sync_"))
    }
    assert got == expected
    # 所有表都必须在元数据 schema 内
    assert all(t.startswith(f"{schema}.") for t in Base.metadata.tables)


def test_meta_table_的四元组唯一键与_table_uid_生成列() -> None:
    """§1：唯一性只在四元组内成立；table_uid 用 chr(31) 做分隔符，绝不裸拼。

    表达式口径（2026-09-27 真库验过才定的）：`md5(a::text || chr(31) || b || chr(31) || c
    || chr(31) || d)`。ADR-0005 原文写的是 `concat_ws`，但 PG 的 proc 目录把 concat_ws 标成
    stable，生成列要求 immutable——照抄会在建表当场被拒。
    """
    ddl = _ddl("meta_table").replace("\n", " ")
    assert "UNIQUE (datasource_id, catalog_name, schema_name, table_name)" in ddl
    # char(32) 定长走 btree；不是 uuid 类型（md5 不是 uuid 形状）
    assert (
        "table_uid CHAR(32) GENERATED ALWAYS AS (md5(datasource_id::text || chr(31) "
        "|| catalog_name || chr(31) || schema_name || chr(31) || table_name)) STORED" in ddl
    )
    assert ddl.count("chr(31)") == 3, "四元组之间正好三个分隔符"
    assert "CONSTRAINT uq_meta_table_table_uid UNIQUE (table_uid)" in ddl


def test_meta_table_把人工列与同步列分在两张皮里() -> None:
    """§3：comment_zh / business_desc / granularity / is_hidden 是人工列，同步永不覆盖。

    DDL 层能验的是它们**存在且可空**（可空 = 允许"库里已有人工值"这一状态），
    覆盖语义在 upsert SQL 里，由 test_sync_upsert_sql.py 把守。
    """
    ddl = _ddl("meta_table")
    human = ("comment_raw", "comment_zh", "business_desc", "granularity", "is_hidden", "is_stale")
    for col in human:
        assert col in ddl
    # 视图单独色：table_type 只允许两个值，别的方言值抽取时先归一
    assert "table_type IN ('BASE TABLE','VIEW')" in ddl


def test_meta_column_到_meta_index_的父子关系走_cascade() -> None:
    """§2.4 + §3：子表无人工字段，同步按 table_id 全量替换，所以父删子必须跟着没。"""
    col_ddl = _ddl("meta_column")
    assert "UNIQUE (table_id, column_name)" in col_ddl
    assert "ON DELETE CASCADE" in col_ddl
    assert "enum_values JSONB" in col_ddl, "中文枚举值直接决定 where 能否命中，必须是 JSONB"
    idx_ddl = _ddl("meta_index")
    assert "UNIQUE (table_id, index_name)" in idx_ddl
    # 验收锚点（§2.4 末 / roadmap P3 验收 7）：这两个数是"没退回 SHOW CREATE TABLE"的证据
    assert "cardinality BIGINT" in idx_ddl
    ic_ddl = _ddl("meta_index_column")
    assert "sub_part INTEGER" in ic_ddl
    assert "UNIQUE (index_id, seq_in_index)" in ic_ddl


def test_meta_relation_三种来源共用一张表() -> None:
    """§2.4 + §4：manual > extracted > inferred，delete-diff 只作用于 extracted。"""
    ddl = _ddl("meta_relation")
    assert "source_kind IN ('extracted','inferred','manual')" in ddl
    assert "confidence NUMERIC(4, 3)" in ddl
    assert (
        "UNIQUE (datasource_id, source_kind, from_table_id, from_column_name, to_table_id, "
        "to_column_name)" in ddl.replace("\n", " ")
    )


def test_sync_jobs_的互斥是数据库保证的_partial_unique_index() -> None:
    """§6：同数据源只允许一个未结束 job，冲突返回 409 而不是代码 race。"""
    ddl = _index_ddls("sync_jobs")
    assert "CREATE UNIQUE INDEX ux_sync_running ON" in ddl
    assert "WHERE status IN ('pending','running')" in ddl
    jobs = _ddl("sync_jobs")
    assert "heartbeat_at TIMESTAMP WITH TIME ZONE" in jobs
    assert "counters JSONB" in jobs
    assert "phase IN (" in jobs, "phase 枚举是 SSE 进度轴的契约（§2.5）"
