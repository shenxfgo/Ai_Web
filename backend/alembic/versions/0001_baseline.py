"""baseline：扩展与 schema 占位（业务表从 0002 起）

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pg_trgm 是 trusted 扩展（PG13+），非超管也能建；关键词检索路依赖它
    try:
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    except Exception as exc:
        raise RuntimeError(
            "建 pg_trgm 失败。请让 DBA 执行：CREATE EXTENSION IF NOT EXISTS pg_trgm; "
            f"（原始错误：{exc}）"
        ) from exc


def downgrade() -> None:
    # 不删扩展：同一实例上的其他对象可能也在用
    pass
