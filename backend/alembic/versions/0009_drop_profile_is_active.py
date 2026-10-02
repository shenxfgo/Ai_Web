"""收缩那一版：把 `kb_index_profile.is_active` 从库里摘掉（口径见 ADR-0013、工单 031 验收 7/8）

Revision ID: 0009
Revises: 0008

一句话口径：0008 让 `status` 出生、0009 让 `is_active` 退场——026 定的扩展-收缩第三段就是这一版。
留着一列没人读的布尔，等于把 ADR-0013 反对的那件"库里两套真相"原地保留。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.settings import get_settings

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = get_settings().pg.schema_name


def restore_is_active_sql(schema: str) -> str:
    """downgrade 的第二步：那一列按 `status` 补回来，而不是加一列全 `false`。

    写成 `ADD COLUMN … DEFAULT false` 就收工的话，列是回来了、内容全空：0008 那一世的代码
    只认 `is_active`，它于是读到"没有任何档案在服务"。回滚不是把 DDL 反着敲一遍就完事，
    它得把当时的真相带回去。
    `status` 在这一步还在（0008 的 downgrade 才删它，排在 0009 之后），所以镜像有唯一来源。
    口径与 026 的双写一致：`is_active` 跟着 `status` 的字面走，不引第二个真相。
    """
    return f"UPDATE \"{schema}\".kb_index_profile SET is_active = (status = 'active')"


def upgrade() -> None:
    # 删列前没有任何数据要归置：027 起写侧不生产它、031 起读侧不消费它，它在这两版之间
    # 就已经是一个没人读的布尔。真正的前置是**代码**那一侧（工单 031 验收 7 的 grep 零命中），
    # 不是库里的一次 UPDATE。
    op.drop_column("kb_index_profile", "is_active", schema=SCHEMA)


def downgrade() -> None:
    # server_default 与 0005 当年那一行逐字同调（`false`），否则回滚出来的列形状和历史上不是同一个。
    op.add_column(
        "kb_index_profile",
        sa.Column("is_active", sa.Boolean(), server_default=sa.false(), nullable=False),
        schema=SCHEMA,
    )
    op.execute(restore_is_active_sql(SCHEMA))
