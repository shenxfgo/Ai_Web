# 索引档案的三态入库，且同步只认 active（profile 的唯一出生入口是 reindex）

**状态**：accepted（P4 开工前拍板，2026-10-02）

`kb_index_profile` 今天只有 `is_active BOOLEAN` + `built_at`，**没有任何唯一约束**保证只有一行 active
（`alembic/versions/0005_kb_card.py` 只有 `uq_kb_index_profile_name`），读侧
`retriever._active_profile_id()` 是 `.where(is_active).limit(1)`——两行 active 时它安静地任选一行。
而靠这两列**推导**三态也不行："建好待切"与"已退役"在库里同形（都是 `NOT is_active` 且
`built_at NOT NULL`）。决定的形状：迁移加 `status text CHECK IN ('draft','active','retired')`，
外加一条 partial unique index `WHERE status='active'` 让"只有一个 active"由库背书
（CHECK 只能限字面，限不了唯一）；激活是**一条语句**：
`UPDATE … SET status = CASE WHEN id=:new THEN 'active' WHEN status='active' THEN 'retired' ELSE status END`。

**为什么同时否掉"只加唯一索引 + 时间戳列"**：那样库里同时存在 `is_active` 与时间戳两套真相，
读侧还得自己拼三态判定——那正是 `stage_of` 那条纪律反对的"第二个翻译点"。

**第二个决定，也是本片最容易被忽略的一条**：同步（`card_build`）**只认库里 status='active' 的那一套**，
不再按 settings 三元组 ensure。理由是它是个后门：`kb_service.profile_row(is_active=True)`
（注释自己写着"它一落库就是当前生效的那套）在同步链路上，所以**改一行 `.env` 的
`AIWEB_EMBEDDING__DIMENSION` 再跑一次同步，就等于绕过 `activate` 悄悄换了全库检索基准**——
而 roadmap §P4 验收 1 要的正是"切换必须走 activate、之后只有一个 active"。
settings 与 active 不一致时的行为是：不写卡、不建 profile、发一条结构化告警
（roadmap §启动自检项 7"维度三方一致"的运行时版本），下一步文案指向 `POST /api/kb/reindex`。
profile 的出生入口从此**唯一**：reindex 端点。

**后果**：008/019 期间"每次同步 ensure 一次 profile"的语义要改，它的用例（含 019 那条
"`index_profile_id` 不进 upsert 的 `set_`"）要跟着重写。这不是回归，是把一条静默路径改成显式路径：
"改了配置却什么都检索不到"与"改了配置就换了全库基准"两种坏形状，都不如"改配置时告诉你先去重建"。
