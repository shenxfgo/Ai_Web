"""知识库接口：卡片读取 + 检索预览 + 重建基准（reindex）+ 上线基准（activate）。

`/kb/reindex` 是工单 029 交付的，`/kb/profiles/{id}/activate` 是工单 030 交付的。
architecture §7 原写的旧名 `/kb/rebuild` 已被 P4 决策 ⑥ 废弃，不留别名。
`/kb/status` 也是 §7 承诺项，但至今无工单认领（P3 收口记进无主账，P4 spec 不接手）。
这里都不放空壳——一个返回 501 的端点只会让前端以为功能存在。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db, get_job_queue, require_admin
from app.models.user import User
from app.schemas.kb import (
    KbActivateOut,
    KbCardOut,
    KbCardProfileOut,
    KbReindexAccepted,
    KbReindexRequest,
    KbSearchOut,
    KbSearchRequest,
)
from app.services import datasource_service, kb_service
from app.services.job_queue import JobQueue
from app.services.nl2sql import retriever

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


@router.post("/kb/search")
async def search_preview(
    payload: KbSearchRequest,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KbSearchOut:
    """检索预览：一句问句 → 候选表 + 命中理由。工单 009 的接口层。

    它存在的理由是"人肉验证命中"：012 的 `/chat/ask` 会把检索结果拌进 prompt 里看不见，
    而阶梯 L1 对不对，只有把命中理由摊开才判得出来。权限同 `/kb/cards` 的 read 权。
    """
    return await retriever.search_preview(db, actor=actor, payload=payload)


@router.post("/kb/reindex", status_code=status.HTTP_202_ACCEPTED)
async def reindex(
    payload: KbReindexRequest,
    actor: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    queue: JobQueue = Depends(get_job_queue),
) -> KbReindexAccepted:
    """重建检索基准：出生一套 draft 档案 + 入一颗按数据源的作业（工单 029，spec 故事 3/7）。

    权档与 `POST /sync/jobs` 同一把尺（owner 档，`get_owned`）：reindex 会派 worker 拿这个源的
    凭据去连库，还要烧真端点的 embedding 额度，'global' 档的人不该有这个能力。工单涉及层那行
    写的是 `get_authorized`，这是**一处对工单字面的偏离**，理由与差异都记在工单交付记录里。

    顺序是"先生档案、后入队"，两个方向都有代价，而这个方向代价小：
    - 反过来（先入队）的话，档案那一步一旦 400/409，队列里已经躺着一颗**没有靶**的作业，
      worker 领到它就会按普通同步跑（`reindex_profile_id` 为 NULL），把"重建"悄悄做成"同步"。
    - 现在这个方向，409（同源已有未结束作业）时会留下一行已提交的 draft。它不是空壳——
      下一次 reindex 同三元组会复用同一行（撞名口径），而 030 的"未跑完不许激活"看的是
      `built_at`，一行没跑完的 draft 只是躺在列表里等。

    响应体只有两颗 id：作业还在队列里，任何计数都是编的（016 的口径）。
    """
    ds = await datasource_service.get_owned(db, actor, payload.datasource_id)
    profile_id = await kb_service.create_draft_profile(db)
    job_id = await queue.enqueue(ds.id, actor.id, reindex_profile_id=profile_id)
    return KbReindexAccepted(job_id=job_id, profile_id=profile_id)


def _profile_out(summary: kb_service.ProfileSummary) -> KbCardProfileOut:
    """`ProfileSummary` → 响应模型：一格一格列出来，不 `model_validate` 整行。

    ORM 行里还有 `provider`/`distance_fn`/`created_at`/`built_at`，整行倒出来就等于让
    "上线一套档案"顺带漏出它的建档案时钟（`KbCardOut` 不整行倒出 `kb_card` 同一条理由）。
    """
    return KbCardProfileOut(
        id=summary.id,
        name=summary.name,
        model=summary.model,
        dimensions=summary.dimensions,
        card_template_version=summary.card_template_version,
    )


@router.post("/kb/profiles/{profile_id}/activate")
async def activate_profile(
    profile_id: int,
    actor: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> KbActivateOut:
    """把这套档案上线，同刻把原先生效的那套退役（工单 030 / spec 故事 5、6、19）。

    鉴权 **admin-only**（`docs/architecture.md` §7 端点表那一行的"admin"列）。这是仓库第一个
    按角色判的写端点，理由是它没有资源可判：`/sync/jobs` 与 `/kb/reindex` 判的是"这个源是不是
    你登记的"，而 activate 换的是**全库**的检索基准——它一改，所有人问出来的结果都变。
    工单 030 把这一格列为"待认的一格"并写了默认 admin-only，本轮按默认落，差异记进交付记录。

    成功与失败都在 service 那一层（`kb_service.activate_profile`）：状态机、`built_at` 那道门、
    卡片行数那把尺。这一层只做两件事——把身份换成 403、把 `AppError` 交给全局 envelope
    （404 `not_found` / 400 `profile_not_built` / 400 `profile_cards_incomplete`）。
    不在这里再判一次权限或再包一层 try：那会把"库里只有一行 active"这件事的守门人变成端点自己。

    **不发事件、不入队**（工单已定口径 / ADR-0010）：`sync_job_event` 是作业进度的真相，
    一次切换不是作业进度。前端要"哪套在服务"就读这个响应体的 `activated`，别去订阅一条不存在的事件。
    """
    outcome = await kb_service.activate_profile(db, profile_id=profile_id)
    return KbActivateOut(
        activated=_profile_out(outcome.activated),
        retired=None if outcome.retired is None else _profile_out(outcome.retired),
    )
