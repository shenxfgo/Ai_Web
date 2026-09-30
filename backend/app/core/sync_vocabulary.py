"""双层阶段词表：9 值细粒度 `phase` 与 5 值粗粒度 `stage` 之间**唯一的那一次翻译**。

口径出处：`docs/metadata-model.md` §2.5（phase CHECK，本轮不动）、§2.8（新表
`sync_job_event.stage` 的 CHECK）与 §6 as-built(P3) 第 2 条；ADR-0010 的两套词表并存
是有意的——细粒度给排障（"卡在 IS 查询还是卡在写库"），粗粒度给 SSE 与进度条。

这一份之所以是独立模块而不是 `sync_service` 里的一个私有函数：写侧（追加事件行）与读侧
（`core/sse.py` 的流）都要用它，而工单 017 验收 5 的判据是"grep 全仓找不到第二处翻译"。翻译
放在任何一方内部，另一方就只剩"再抄一遍 dict"这条路——010 那族"同一个门槛散在三处"
的形状就是这么长出来的。

它住在 `core/` 而不是 `services/`：读侧在 core，而 core 不许反向 import services
（`docs/architecture.md` §2 的分层）。这个模块只 import `typing`，两边都用得上而不成环。
"""

from __future__ import annotations

from typing import Final

# §2.5 的九个细值逐个归类。`connect`/`discover` 归 extract：那两步还在对着源库说话；
# 四个实体类细值归 upsert：它们说的是"正在往元数据库写哪一类实体"（§7 的批语义，
# 一批一个事务），读源库的那几条查询在 `collect()` 里已经跑完了。
_PHASE_TO_STAGE: Final[dict[str, str]] = {
    "connect": "extract",
    "discover": "extract",
    "tables": "upsert",
    "columns": "upsert",
    "indexes": "upsert",
    "fks": "upsert",
    "card_build": "card_build",
    "embed": "embed",
    "done": "done",
}


def stage_of(phase: str) -> str:
    """把细粒度 phase 翻成对外那一档；词表外的值抛 ValueError。

    不做"未知就归到 extract"那种兜底：进度条会因此永远停在第一段，而那是最像没坏的形状。
    """
    try:
        return _PHASE_TO_STAGE[phase]
    except KeyError:
        raise ValueError(f"phase={phase!r} 不在 sync_jobs.phase 的词表里") from None


#: 终局那一档的名字。读侧（`core/sse.py` 的流）要靠它判定"这条流到头了"，
#: 而粗档那五个字面只许住在①迁移的 CHECK 与它的 ORM 镜像 ②这一份词表里
#: （工单 017 验收 5），所以给读侧的是谓词而不是让它在自己的比较里再抄一个 "done"。
STAGE_DONE: Final = "done"


def is_terminal_stage(stage: str) -> bool:
    """这一档是不是流的最后一行（`run_sync` 的 finally 保证每条路都写得出它）。"""
    return stage == STAGE_DONE


__all__ = ["STAGE_DONE", "is_terminal_stage", "stage_of"]
