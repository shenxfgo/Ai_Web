"""Chat 面的 DTO（architecture §7 的 `/chat/*`）。

**这一份文件今天只有 validate 一支**。`chat` 这个 tag 在 `router.py` 里第一次出现（工单 039），
它不等于 `/chat/ask` 已经存在——问数本体、SSE、结果下载都还归 P8，这里不预留空壳字段，
也不给 `/chat/ask` 建请求模型（建了就是"文档说有了"的另一种写法）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ChatValidateRequest(BaseModel):
    """`POST /api/chat/validate` 的 body（roadmap §P5 验收 2，工单 039）。

    `datasource_id` **必带**（口径第 2 条）：守卫的表白名单必须有个来源，而 validate 没有问句、
    因而没有检索结果可当来源。缺它只剩两种坏选择——跳过表白名单（对外暴露一支**残缺守卫**，
    而本片的全部价值是"和真实问数走同一支判定"），或者把所有表都拒。所以这一格不是可选参数，
    缺省路径根本不存在：`extra="forbid"` 之外再加 pydantic 的必填，出 422 而不是 400。

    `sql` 的长度与空否**不在这里判**：主人是守卫的 `_preprocess`（`empty_statement` /
    `too_large`），所以超限的语句出的是 400 `sql_guard_rejected`，与"请求体写错"那档
    （422 `invalid_request`）分属两个主人。这里再加一层 `min_length`/`max_length`
    就会把守卫的那两枚 rule_id 永远遮住。
    """

    model_config = ConfigDict(extra="forbid")

    sql: str
    datasource_id: int


class ChatValidateOut(BaseModel):
    """200 那一格：放行 + 钳位后的那句话 + 它引用了哪些表。

    `ok` 在这一档恒为 `true`（拒的那一路走 `AppError` 信封，不进响应模型）。留它而不是省它：
    仓库已有先例 `datasource.py` 的 `ConnectionTestOut.ok`，而且"200 但没通过"这种形状一旦出现，
    前端就得为它写第二条分支——成功体只表达成功。

    `sql_final` 是**钳位后**的那句话（`LIMIT max_rows+1` 已经加上了），与 `chat_messages.sql_final`
    同一格语义：校验完拿去跑的应该是它，不是用户发来的原文。
    `tables` 是守卫归一化后引用的那些表（小写、`catalog.schema.table` 里空的那几格不占位），
    供人核对"我以为我查的是这张表"。
    """

    ok: bool
    sql_final: str
    tables: list[str]
