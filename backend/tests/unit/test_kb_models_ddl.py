"""kb_card / kb_index_profile 的建表 DDL 契约（schema-only 断言，不碰数据库）。

口径来源：
- `docs/metadata-model.md` §2.6（卡片表 + profile 表 + 五条索引）
- ADR-0003 + `docs/roadmap.md` §配置语义：**第一列** `embedding vector(dim)` 的 dim 取自
  `AIWEB_EMBEDDING__DIMENSION`。0008 起"不硬编码"只对这一列成立——第二列按 ADR-0012
  把维度写进列名，宽度是字面量（那条 ADR 明码标价的一次翻案）。
- ADR-0013 + 工单 026：profile 的三态词表与"至多一行 active"的库里背书
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


def _index_ddl(table_name: str, index_name: str) -> str:
    """按索引名取那条 DDL。

    不能用 `_line(_index_ddls(...), "ix_kb_card_emb")`：`table.indexes` 是无序集合，而且
    `ix_kb_card_emb` 是 `ix_kb_card_emb_1024` 的前缀，按子串取行会随会话随机拿到另一条。
    """
    table = Base.metadata.tables[f"{Base.metadata.schema}.{table_name}"]
    for ix in table.indexes:
        if ix.name == index_name:
            return str(CreateIndex(ix).compile(dialect=postgresql.dialect()))
    raise AssertionError(f"{table_name} 没有名为 {index_name!r} 的索引")


def _constraint_names(table_name: str) -> list[str]:
    table = Base.metadata.tables[f"{Base.metadata.schema}.{table_name}"]
    return sorted(str(c.name or "") for c in table.constraints)


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

    `name` 唯一（形如 `text-embedding-3-small@1536@tplv1`）= 同一配置不会重复建 profile。
    那条 `is_active` 曾经在这里排第一（P2 时它是检索侧唯一的过滤位），0008 让 `status` 接手、
    027 关掉写侧、031 关掉读侧，0009 把列删了——所以这一格现在钉的是**它不在**：一个没人读的
    布尔留在模型里，下一版就会有人以为它是第二真相（ADR-0013 反对的就是这件事）。
    """
    ddl = _ddl("kb_index_profile")
    oneline = ddl.replace("\n", " ")
    assert "CONSTRAINT uq_kb_index_profile_name UNIQUE (name)" in oneline
    for name in ("provider", "model", "dimensions", "card_template_version"):
        assert "NOT NULL" in _col(ddl, name), f"{name} 是 profile 身份的组成部分"
    assert "is_active" not in ddl, "收缩那一版没删干净：模型里还留着那条镜像列"
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


def test_五条索引各自守着一条检索路() -> None:
    """§2.6 的 SQL 块 + ADR-0012 的第二向量列 + verification §2.2 的 DDL 字面。

    覆盖：两套维度的向量、trgm 模糊、FTS、软删后的源内过滤。

    HNSW 的 `WITH (m=16, ef_construction=64)` 是文档给的固定调参，不是"随便建的默认值"；
    FTS 必须走 `to_tsvector('simple', …)`——默认配置会把中文按词干规则切碎。
    第二列的索引调参与第一列**逐字同调**：两批向量共存时，检索侧只该在"读哪一列"上分岔。
    """
    emb = _index_ddl("kb_card", "ix_kb_card_emb")
    assert "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)" in emb
    emb1024 = _index_ddl("kb_card", "ix_kb_card_emb_1024")
    assert (
        "USING hnsw (embedding_1024 vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
        in emb1024
    )
    trgm = _index_ddl("kb_card", "ix_kb_card_search_trgm")
    assert "USING gin (search_text gin_trgm_ops)" in trgm
    fts = _index_ddl("kb_card", "ix_kb_card_search_fts")
    assert "USING gin (to_tsvector('simple', search_text))" in fts
    ds_kind = _index_ddl("kb_card", "ix_kb_card_ds_kind")
    assert "(datasource_id, kind) WHERE deleted_at IS NULL" in ds_kind
    assert "UNIQUE" not in ds_kind


def test_第二向量列是_embedding_1024_与第一列一样可空() -> None:
    """ADR-0012：换维度 = 同一张表再加一列，列名带维度。

    "旧向量仍在（可回滚）"是 roadmap §P4 验收 5 的原话，它的物理前提就是两列**同时在场**；
    而两列同时在场又要求都可空——只有 1536 批向量的卡片，那一行的 1024 就是 NULL，反之亦然。
    列宽 1024 在这里是**字面量**，这是 ADR-0012 明写的那条翻案：
    `AIWEB_EMBEDDING__DIMENSION` 的角色从"决定列宽"降为"声明当前用哪一维"。
    """
    line = _line(_ddl("kb_card").upper(), "EMBEDDING_1024 VECTOR(")
    assert "EMBEDDING_1024 VECTOR(1024)" in line, line
    assert "NOT NULL" not in line, "两套维度共存期，另一套的卡在这一列上就是空"


def test_index_profile_的_status_是三态_CHECK_就是那份词表() -> None:
    """ADR-0013：`status TEXT CHECK IN ('draft','active','retired')` NOT NULL。

    为什么不能靠 `is_active` + `built_at` 推导三态：「建好待切」与「已退役」在库里同形
    （都是 `NOT is_active` 且 `built_at NOT NULL`），推不出来的那一态只能是一列。
    字面来自 ADR-0013 与 spec 故事 1，不是从模型代码反推的。
    """
    ddl = _ddl("kb_index_profile")
    status = _col(ddl, "status")
    assert "TEXT" in status, status
    assert "NOT NULL" in status, "可空的 status 会让「没写过」变成第四态"
    assert "CHECK (status IN ('draft','active','retired'))" in ddl.replace("\n", " "), ddl
    assert "ck_kb_index_profile_status_allowed" in _constraint_names("kb_index_profile"), (
        "约束名要能被反查（与 ck_sync_job_event_stage_allowed 同纪律）"
    )


def test_只有一个_active_由库里那条_partial_unique_索引背书() -> None:
    """ADR-0013：`CREATE UNIQUE INDEX … WHERE status = 'active'`。

    为什么键是一列表达式而不是某个真列：这条不变式是「全表至多一行 active」，它没有自然键列。
    PG 的 unique 索引至少要一个键，于是拿 `(status = 'active')` 这个表达式当键——命中谓词的行
    全取同一个值 `true`，第二行 active 因此撞唯一约束。判据「只有一个 active」住在库里，
    不在代码的 `WHERE … LIMIT 1` 里（那是 spec §1 问题 2 那条安静任选的老路）。
    """
    ddl = _index_ddls("kb_index_profile")
    line = _index_ddl("kb_index_profile", "ux_kb_index_profile_active")
    assert "UNIQUE" in line, line
    assert "WHERE" in line and "status = 'active'" in line, line
    # 表里不能有第二条同谓词的索引：两条索引只会让"第二行 active 撞哪一条"变成实现细节。
    # 数的是**谓词**而不是索引名——第二条同谓词的索引换个名字照样在场，按名字数等于没数。
    assert ddl.count("WHERE status = 'active'") == 1, ddl
