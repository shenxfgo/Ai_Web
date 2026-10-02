# 换 embedding 维度 = 同一张表再加一列（列名带维度），检索按 profile 选列

**状态**：accepted（P4 开工前拍板，2026-10-02）

`kb_card` 的向量列宽今天取自配置（`app/models/kb.py` 里 `Vector(get_settings().embedding.dimension)`），
而 pgvector 的 HNSW 只接受**定维**列（见 ADR-0003 补注与 `metadata-model §2.6` 的 as-built(0005)），
PG 又不支持用 `ALTER TYPE` 改一个 `vector` 列的维度。roadmap §P4 验收 5 要的却是
"换 `bge-large-zh`(1024) 重建，激活后旧向量仍在（可回滚）"——新旧两套维度必须**同时在场**。
决定的形状：`kb_card` 除 `embedding vector(1536)` 外再加一列 `embedding_1024 vector(1024)`，
列名带维度；检索读哪一列由 active profile 的 `dimensions` 决定，而这层"维度 → 列名"的映射
**全仓只住在一个纯函数里**（与 `sync_vocabulary.stage_of` 同一条纪律：出现第二处就是 bug）。

**为什么不是影子表**：把向量拆到 `kb_embedding(card_id, profile_id, embedding)`、每维度一张表，
能让"改卡片文本"与"重灌向量"解耦——但 `index_profile_id` 已经是 `kb_card` 的列、`doc_uid` 又拌了它，
所以**新 profile 的向量天然只能挂在新 profile 的卡片行上**，"只重嵌不重建"在 schema 层就不成立；
而卡片文本是纯函数产物、几乎免费，真正花钱的 embedding 调用本来就要按新行逐张重算。
拆表换不来任何东西，只把 0005 交付并被快照测试钉着的 `kb_card.embedding` 变成死列。

**为什么不是"不做跨维度共存"**：那是把 §2.6 那句"换维度必须新表/新列"永久留在文档里当装饰。
本片的验收 5 真跑这一列（闸内假端点按 model 名返回 1024 维），所以它不是预建的死列。

**后果（一条要认的翻案）**：`AIWEB_EMBEDDING__DIMENSION` 的角色从"决定列宽"降为
"声明当前用哪一维 → 选出对应那一列"。§2.6 as-built(0005) 那句"dim 不硬编码在迁移里"从此只对
**第一列**成立：库里 1536 是 0005 跑定时已经写死的事实，所谓"不硬编码"从来只到代码层。
再加第三个维度 = 再一次 DDL，这是明码标价，不是意外。
