"""知识库卡片接口：只有一条按表取段文本的读路。

`/kb/search`、`/kb/rebuild`、`/kb/status` 都在 architecture §7 的端点表里，但分属 P3（检索）
与 P4（重建/术语卡），这里不放空壳——一个返回 501 的端点只会让前端以为功能存在。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.kb import KbCardOut
from app.services import kb_service

router = APIRouter()


@router.get("/kb/cards")
async def list_cards_by_table(
    table_uid: str,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[KbCardOut]:
    """一张表的全部卡片段（宽表是主卡 + 若干 `table_columns`），按 `seq` 升序。

    取 `?table_uid=` 而不是 architecture §7 那行写的 `/kb/cards/{id}`：工单 008 的验收要的是
    "把这张表的素材一次取全"，而按单张卡 id 取的话，前端得先知道有几段——段数只有服务
    端算得出来（§6 的切表策略）。判权按 §7 的 read 权：owner 与 `global` 档都能读，
    没授权的 403（`datasource_service.get_authorized` 那一把尺）。
    """
    return await kb_service.cards_of_table(db, actor=actor, table_uid=table_uid)
