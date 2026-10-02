"""档案三态入库 + 第二向量列 + reindex 的载体列（口径见 ADR-0012、ADR-0013、工单 026）

Revision ID: 0008
Revises: 0007

一句话口径：这一版把两件原本只存在于代码约定里的事**交给库背书**——
"全表至多一个 active 档案"由一条 partial unique 索引背书，"两套维度同时在场"由
`kb_card` 的第二列背书。没有它们，激活与回滚就只是应用层的礼貌。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

from app.settings import get_settings

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = get_settings().pg.schema_name

# 第二套维度的列宽，1024 是**字面量**：roadmap §P4 验收 5 点名的就是 bge-large-zh(1024)，
# 而 pgvector 的 HNSW 只吃定维列、PG 又不允许 ALTER 改 vector 维度（ADR-0012）。
# 这是那条明写的翻案——`AIWEB_EMBEDDING__DIMENSION` 不再决定列宽，只声明当前读哪一维。
# 全仓第二处出现这个字面的是 app/models/kb.py 的同名列；"维度→列名"的映射归 031 的纯函数。
SECOND_DIM = 1024

# 三态词表的库内落点。字面来自 ADR-0013，与 app/models/kb.py 的 CheckConstraint 逐字一致。
STATUS_WORDS = "('draft','active','retired')"


def require_vector_extension(bind: sa.Connection) -> None:
    """加第二向量列前重新查一次扩展注册表——0005 建过不代表这一版还在。

    形状照 0005 的同名函数：`vector` 不是 trusted 扩展（pgvector 0.8.6），应用账号
    `CREATE EXTENSION` 必失败并脏掉迁移链，所以迁移**不尝试建**，只把人领到 DBA 那里。
    hint 里点名 0008：只说"缺扩展"的话，人不知道该重跑哪一条、更不知道是加列这条路缺的。
    """
    installed = bind.execute(
        sa.text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
    ).scalar()
    if not installed:
        raise RuntimeError(
            "0008 加 kb_card.embedding_1024 需要 vector 扩展，但它不是 trusted 扩展，"
            "应用账号建不了。请超级用户/DBA 在元数据库执行：CREATE EXTENSION vector;"
            "（0.4+ 才支持 HNSW），然后重跑 alembic upgrade head。"
        )


def backfill_status_sql(schema: str) -> str:
    """现存档案行的归置口径，写在迁移最前面是为了让它能被读出来、也能被单独测。

    `is_active=true` → `active`，其余历史行 → `retired`，**不是 `draft`**：`draft` 的含义是
    "建好了等着被激活"，而 P2/P3 期间由 ensure_profile 落下的那一行是正在服务的那一套。
    把它们落成 draft，激活端点下一步就会 retire 掉现状——那是把现状当待办（ADR-0013）。
    """
    return (
        f'UPDATE "{schema}".kb_index_profile '
        f"SET status = CASE WHEN is_active THEN 'active' ELSE 'retired' END"
    )


def upgrade() -> None:
    # ── ① 档案三态：列 → 归置 → CHECK → partial unique ─────────────────────
    # 默认 'draft' 只服务"新插入而调用方没写 status"这一种情形；存量行紧接着被上面那条
    # 归置口径改写，所以库里不会留下"历史行是 draft"的中间态。
    op.add_column(
        "kb_index_profile",
        sa.Column("status", sa.Text(), server_default=sa.text("'draft'"), nullable=False),
        schema=SCHEMA,
    )
    op.execute(backfill_status_sql(SCHEMA))
    # CHECK 在回填之后加：词表里三个值都是回填可能产出的，反过来加也不会红，但"先有合法数据
    # 再有约束"读起来不需要犹豫。
    # 名字传的是**裸名**：alembic 在这里会套上 metadata 的 naming_convention（`ck_%(table_name)s_
    # %(constraint_name)s`），传全名就落进库里变成双前缀。0005 的 `kind_allowed` 是这么对的，
    # 0006/0007 传了全名、库里现在是 `ck_sync_job_event_ck_sync_job_event_stage_allowed`（已记进
    # 工单 026 的偏离节，本片不动它们）。约束名要能被反查，见 ADR-0013。
    op.create_check_constraint(
        "status_allowed",
        "kb_index_profile",
        f"status IN {STATUS_WORDS}",
        schema=SCHEMA,
    )
    # "全表至多一行 active"没有自然键列，所以键是表达式本身：命中谓词的行全取同一个值 true，
    # 第二行 active 当场撞唯一约束。这就是 spec 故事 1 要的"由库拒，不由代码分支拦"。
    op.create_index(
        "ux_kb_index_profile_active",
        "kb_index_profile",
        [sa.text("(status = 'active')")],
        schema=SCHEMA,
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    # ── ② 第二向量列：新列 + 自己的 HNSW，1536 那列与它的索引一个字不动 ───────
    require_vector_extension(op.get_bind())
    op.add_column(
        "kb_card",
        sa.Column("embedding_1024", Vector(SECOND_DIM), nullable=True),
        schema=SCHEMA,
    )
    # 调参与 ix_kb_card_emb 逐字同调：两批向量共存时，检索侧只该在"读哪一列"上分岔。
    # HNSW 空表能建（同步是先建空卡再灌），所以随加列同批建，不等第一张 1024 的卡出现。
    op.create_index(
        "ix_kb_card_emb_1024",
        "kb_card",
        ["embedding_1024"],
        schema=SCHEMA,
        postgresql_using="hnsw",
        postgresql_ops={"embedding_1024": "vector_cosine_ops"},
        postgresql_with={"m": 16, "ef_construction": 64},
    )

    # ── ③ reindex 的载体列：执行体在另一个进程里读不到请求（ADR-0011）──────────
    # 与 0007 的 force 同一个理由：入队那一刻"这颗作业是给哪套 draft 建卡"只能落到行上。
    # 可空 = 普通同步不换档案；SET NULL = 档案行被删时作业行不该跟着消失，它是发生过的事实。
    op.add_column(
        "sync_jobs",
        sa.Column("reindex_profile_id", sa.BigInteger(), nullable=True),
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_sync_jobs_reindex_profile_id",
        "sync_jobs",
        "kb_index_profile",
        ["reindex_profile_id"],
        ["id"],
        ondelete="SET NULL",
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_sync_jobs_reindex_profile_id", "sync_jobs", type_="foreignkey", schema=SCHEMA
    )
    op.drop_column("sync_jobs", "reindex_profile_id", schema=SCHEMA)
    op.drop_index("ix_kb_card_emb_1024", table_name="kb_card", schema=SCHEMA)
    op.drop_column("kb_card", "embedding_1024", schema=SCHEMA)
    op.drop_index("ux_kb_index_profile_active", table_name="kb_index_profile", schema=SCHEMA)
    op.drop_constraint(
        "ck_kb_index_profile_status_allowed", "kb_index_profile", type_="check", schema=SCHEMA
    )
    op.drop_column("kb_index_profile", "status", schema=SCHEMA)
