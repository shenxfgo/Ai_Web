"""Chat 面的接口层。**今天只有 `POST /chat/validate` 一支**（工单 039）。

`chat` 这个 tag 在 `router.py` 里是第一次出现，它**不等于** `/chat/ask` 已经存在：问数本体、
SSE 流式、结果下载端点全部归 P8（roadmap §P8），本片交付的只是"第一层守卫能不能被 HTTP 调到"
这一条缝。这里不放空壳——一个返回 501 的 `/chat/ask` 只会让前端以为功能已经在了。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.chat import ChatValidateOut, ChatValidateRequest
from app.services.nl2sql import validate as validate_service

router = APIRouter()


@router.post("/chat/validate")
async def validate_sql(
    payload: ChatValidateRequest,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatValidateOut:
    """校验一句话但不跑它：过守卫就回钳位后的那句话，不过就回它踩了哪条规则。

    roadmap §P5 验收 2 的字面落点。档位 = **登录态 + 该源 read 权**（口径格子 ③，2026-10-21 拍板）：
    判权用的是 `datasource_service.get_authorized` 那把刀，与 `/kb/cards`、`/kb/search` 同一档，
    不是 `require_admin`（那是 030 的 activate，全库基准才按角色判，这里判的是"这个源你能不能读"）。

    它**不执行、不连源库、不落 `chat_*`**（口径第 3、4 条）：dry-run 是第二层的事，点一次"校验"
    就多一次源库往返不是 roadmap 要的；留痕归 P8 的问数面。这一格有反证用例钉着
    （`test_validate一次都不执行也不会有人连源库`）。

    白名单比 ask 那一路**宽**（该源全部未陈旧表，而不是检索出的 k 张）——这是"必带
    `datasource_id`、没有问句可检索"的直接后果，写在 safety §8.4 与本端点的响应契约上；
    把它收到 grants 是 P6 验收 2 的活。
    """
    return await validate_service.validate_sql(db, actor=actor, payload=payload)
