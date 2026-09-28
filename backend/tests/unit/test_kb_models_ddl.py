"""kb_card / kb_index_profile 的建表 DDL 契约（schema-only 断言，不碰数据库）。

口径来源：
- `docs/metadata-model.md` §2.6（卡片表 + profile 表 + 四条索引）
- ADR-0003 + `docs/roadmap.md` §配置语义：`embedding vector(dim)` 的 dim 取自
  `AIWEB_EMBEDDING__DIMENSION`，迁移与 ORM 都不硬编码
- `docs/verification.md` §2.2 第 2 项：无 PG 时对编译出的 DDL 字符串做断言

期望值全部来自上面这些文档原文，不是从模型代码里反过来算的。
"""

from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

import app.models  # noqa: F401  # 建表前必须先把 ORM 注册进 Base.metadata
from app.core.db import Base
from app.settings import get_settings


def _ddl(table_name: str) -> str:
    table = Base.metadata.tables[f"{Base.metadata.schema}.{table_name}"]
    return str(CreateTable(table).compile(dialect=postgresql.dialect()))


def _line(ddl: str, needle: str) -> str:
    for line in ddl.splitlines():
        if needle in line:
            return line
    raise AssertionError(f"DDL 里没有含 {needle!r} 的行：\n{ddl}")


def _col(ddl: str, name: str) -> str:
    """按列名取它的 DDL 行——子句顺序（DEFAULT/NOT NULL 谁在前）是 SQLAlchemy 的实现细节，
    断言只钉在"这一列存在、类型对、可空性对"上。"""
    return _line(ddl, f"{name} ")


def _index_ddls(table_name: str) -> str:
    table = Base.metadata.tables[f"{Base.metadata.schema}.{table_name}"]
    return "\n".join(
        str(CreateIndex(ix).compile(dialect=postgresql.dialect())) for ix in table.indexes
    )


def test_卡片表与_index_profile_表都在元数据_schema_里() -> None:
    """§2.6：`kb_index_profile` 必须和 `kb_card` 同批存在——它的 `index_profile_id` 是 NOT NULL。"""
    got = {
        t.split(".", 1)[1]
        for t in Base.metadata.tables
        if not t.split(".", 1)[1].startswith("meta_")
    }
    assert {"kb_card", "kb_index_profile"} <= got
    assert all(t.startswith(f"{Base.metadata.schema}.") for t in Base.metadata.tables)


def test_embedding_列宽取自_settings_且没有向量端点时整列可空() -> None:
    """§2.6 + ADR-0003：`vector(dim)` 的 dim 是 settings 里的维度，迁移与 ORM 都不硬编码。

    可空是"未启用 embedding 端点时卡片照常生成、embedding 为 NULL"这条验收的 DDL 依据。
    SQLAlchemy 把 pgvector 类型渲染成大写 `VECTOR(...)`，而 verification §2.2 的断言字面是
    小写 `vector(1536)`，所以比对统一转大写。
    """
    dim = get_settings().embedding.dimension
    line = _line(_ddl("kb_card").upper(), "EMBEDDING VECTOR(")
    assert f"EMBEDDING VECTOR({dim})" in line
    assert dim == 1536, "conftest 把维度钉成 1536，才配得上 verification §2.2 的断言字面"
    assert "NOT NULL" not in line


def test_卡片身份列锁住_一表一卡加段号_软删不换主键() -> None:
    """§2.6：`doc_uid = md5(kind|table_uid|seq|index_profile_id)` 是重建时的幂等键。

    `index_profile_id` NOT NULL 是"profile 与卡片同批建"的硬依据——没有它，换模型后
    新旧向量混在同一个索引里就无法区分（§2.6 末）。`table_id` 可空只服务 `term` 卡。
    """
    ddl = _ddl("kb_card")
    doc_uid = _col(ddl, "doc_uid")
    assert "CHAR(32)" in doc_uid and "NOT NULL" in doc_uid
    assert "CONSTRAINT uq_kb_card_doc_uid UNIQUE (doc_uid)" in ddl.replace("\n", " ")
    assert "kind IN ('table','table_columns','term')" in ddl
    seq = _col(ddl, "seq")
    assert "INTEGER" in seq and "DEFAULT 0" in seq and "NOT NULL" in seq
    profile = _col(ddl, "index_profile_id")
    assert "BIGINT" in profile and "NOT NULL" in profile
    assert "NULL" not in _col(ddl, "table_id"), "term 卡的 table_id 为 NULL，这列必须可空"
    assert "deleted_at TIMESTAMP WITH TIME ZONE" in ddl


def test_卡片正文列非空_与_build_cards_的产出字段一一对应() -> None:
    """§2.6 + kb-workflow §5：`text_md` 是送 embedding 的完整文档，`search_text` 是关键词路的入口。

    `token_count` NOT NULL = §6 的预算闸在写库前真的算过，不是事后补的字段。
    """
    ddl = _ddl("kb_card")
    for name in ("text_md", "search_text", "token_count"):
        assert "NOT NULL" in _col(ddl, name), f"{name} 是卡片构建的必填产出"
    assert "TEXT" in _col(ddl, "text_md")
    assert "TEXT" in _col(ddl, "search_text")
    assert "INTEGER" in _col(ddl, "token_count")
    assert "meta JSONB" in ddl
    assert "title TEXT" in ddl, "title 允许为空：源库没表注释时卡片只有全名，占位符不算标题"


def test_index_profile_把模型_维度_模板版本钉成一行() -> None:
    """§2.6：换模型/改模板必须新建 profile 全量重建，所以这三样是列，不是散在 settings 的隐式约定。

    `name` 唯一（形如 `text-embedding-3-small@1536@tplv1`）= 同一配置不会重复建 profile；
    `is_active` 是检索侧唯一的过滤位——激活切换的机制在 P4，但列必须在 P2 就存在，
    否则 `kb_card.index_profile_id` 那根 NOT NULL 外键无处可指。
    """
    ddl = _ddl("kb_index_profile")
    oneline = ddl.replace("\n", " ")
    assert "CONSTRAINT uq_kb_index_profile_name UNIQUE (name)" in oneline
    for name in ("provider", "model", "dimensions", "card_template_version", "is_active"):
        assert "NOT NULL" in _col(ddl, name), f"{name} 是 profile 身份的组成部分"
    assert "INTEGER" in _col(ddl, "dimensions")
    assert "INTEGER" in _col(ddl, "card_template_version")
    distance = _col(ddl, "distance_fn")
    assert "'cosine'" in distance, "§2.6：distance_fn 有库侧默认值"
    assert "built_at TIMESTAMP WITH TIME ZONE" in ddl


def test_卡片带着同步与向量化的时间戳() -> None:
    """§2.6 末行 `sync_job_id / updated_at / deleted_at`：卡片是同步产物，必须能被追溯到那一次 job。

    `embedded_at` 可空就是"未配 embedding 端点"的库侧证据——验收项"卡片照常生成、embedding
    为 NULL"要能在库里被看出来，而不是只靠测试内存里的对象。
    """
    ddl = _ddl("kb_card")
    for name in ("sync_job_id", "embedded_at"):
        line = _col(ddl, name)
        assert "NOT NULL" not in line, f"{name} 可空：没跑那一步/没向量化时它就该是空"
    assert "BIGINT" in _col(ddl, "sync_job_id")
    assert "TIMESTAMP WITH TIME ZONE" in _col(ddl, "embedded_at")
    updated = _col(ddl, "updated_at")
    assert "TIMESTAMP WITH TIME ZONE" in updated and "NOT NULL" in updated


def test_四条索引各自守着一条检索路() -> None:
    """§2.6 的 SQL 块 + verification §2.2 的 DDL 字面：向量、trgm、FTS、软删后的源内过滤。

    HNSW 的 `WITH (m=16, ef_construction=64)` 是文档给的固定调参，不是"随便建的默认值"；
    FTS 必须走 `to_tsvector('simple', …)`——默认配置会把中文按词干规则切碎。
    """
    ddl = _index_ddls("kb_card")
    # 索引名是 §2.6 给的，schema 前缀随测试会话变，所以按名字取行再比片段
    emb = _line(ddl, "ix_kb_card_emb")
    assert "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)" in emb
    trgm = _line(ddl, "ix_kb_card_search_trgm")
    assert "USING gin (search_text gin_trgm_ops)" in trgm
    fts = _line(ddl, "ix_kb_card_search_fts")
    assert "USING gin (to_tsvector('simple', search_text))" in fts
    ds_kind = _line(ddl, "ix_kb_card_ds_kind")
    assert "(datasource_id, kind) WHERE deleted_at IS NULL" in ds_kind
    assert "UNIQUE" not in ds_kind
