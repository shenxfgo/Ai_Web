# P4 规格：pgvector 混合检索与索引档案原子切换

日期：2026-10-02（grill 走完全部拍板后综合，未再访谈；spec 落笔时新 surfaced 的两问已由用户同日拍板，
见 §4 表的第 14、15 行）
依据：`docs/adr/0012`~`0015`（本片四条不可逆决策——**形状与理由住在那四份 ADR 里，本文件只引用不重述**）、
`docs/architecture.md` §5.1/§5.2/§7、`docs/metadata-model.md` §2.6/§2.7/§2.3/§6、
`docs/kb-workflow.md` §6/§8、`docs/roadmap.md` §P4（六条验收）与 §10、`CONTEXT.md` 检索与索引节。
术语以 `CONTEXT.md` 为准：**索引档案 / 重建（reindex）/ 激活（activate）/ 混合检索 / 闸内替身**；
另沿用同步节的 同步作业 / worker / 进度事件 / 阶段（phase）与档（stage）/ partial，与权限节的数据源级授权。

## 1. 问题陈述

roadmap §P4 这一片要的是"向量能真跑、档案能原子切换"。今天这两件事一件都没有，而且缺口的形状
各不相同——有的是整条链路不存在，有的是**已经存在一条反向的链路**：

1. **向量列存在，向量路不存在。** `kb_card.embedding vector(1536)` 与它的 HNSW 索引早在迁移 0005
   就建好了（`app/models/kb.py:86`、`:168`），但**全仓没有任何一处往这列写过值**；`embed` 那一档
   永远端出 `_embed_skipped()`（`sync_service.py:751-768`）的 `{skipped: true, code:
   embedding_not_configured | embedding_not_implemented}`，作业照样 `success`。
   `/kb/search` 只有关键词一路（`retriever.py:448` 那句 `_RETRIEVER = LikeRetriever()`），
   响应里 `score_vec/fused/used_profile` 三格 §7 明写"P2 不给，给了就是编的"。
2. **档案没有状态机，而且库里可以并存两个 active。** `kb_index_profile` 只有 `is_active BOOLEAN` +
   `built_at`，唯一约束只有 `uq_kb_index_profile_name`（迁移 0005）；读侧
   `retriever._active_profile_id()`（`retriever.py:314`）是 `.where(is_active).limit(1)`——
   两行 active 时**安静地任选一行**。靠这两列也推不出三态："建好待切"与"已退役"在库里同形
   （都是 `NOT is_active` 且 `built_at NOT NULL`）。这是 ADR-0013 的前半。
3. **档案的出生入口长错了地方。** `kb_service.profile_row(*, is_active: bool = True)`
   （`kb_service.py:321-336`，docstring 自己写着"它一落库就是当前生效的那套"）+ `ensure_profile`
   （`:339`）挂在**同步链路**上。后果：改一行 `.env` 的 `AIWEB_EMBEDDING__DIMENSION` 再跑一次同步，
   就绕过任何切换机制换了**全库检索基准**。roadmap 验收 1 要的"切换必须走 activate"今天不但没实现，
   而且还有一条方向相反的路在真跑。这是 ADR-0013 的后半。
   > **as-built(P4-027)：这一条已经关掉了。** 那两个函数（`profile_row` / `ensure_profile`）连同
   > 它们共用的 `EMBEDDING_PROVIDER` 常量一起删除，不是改成只读——所以本节那两处行号坐标
   > 从今天起**指向不存在的代码**，别按它们定位。现行形状与两条新告警的 code 记在
   > metadata-model §2.6 末注 as-built(P4-027)。仍未落地的是后半句"出生入口归 reindex"：此刻
   > **应用侧**（`grep` 全 `app/`）没有任何代码路径往 `kb_index_profile` 插行——插入这件事只剩 ORM
   > 类定义，剩下六处 `KbIndexProfile(...)` 构造点全在测试里（用例自己摆那一行基准）。那一格要等故事 11~13（工单 029）。
4. **换维度无处可去。** pgvector 的 HNSW 只吃定维列，PG 又不允许 `ALTER TYPE` 改 `vector` 维度，
   而 §2.6 那句"换模型必须新 profile 全量重建"里没有一格说明**新旧两批向量同时住哪儿**——
   验收 5 要的"激活后旧向量仍在（可回滚）"在单列 schema 上物理不可能。这是 ADR-0012。
5. **三个配置键至今只有定义点。** `AIWEB_EXTRACT__SAMPLE_ROW_LIMIT` 在 `Settings` 与 `.env.example`
   里**都不存在**（`metadata-model §6` as-built 第 3 条的原话是"这一键从未落地"），而
   `kb-workflow §8:249` 与 `roadmap` 分组 7 一直当它存在在讲；`sample_distinct` /
   `sample_distinct_max_distinct`（`settings.py:150-151`）有定义、`.env.example:72-73` 有行，
   **无读取点**；`meta_column.enum_values` 只有真 MySQL ENUM 有值、`sample_values` 恒空。
   后果照 P3 spec §6 的原话：
   "非 ENUM 的中文短枚举列（用户说'已完成'、库存 `completed`）在 P2/P3 都进不了卡片"。
   P3 收口时判的是**"键不补、三步整块推 P4"**——就是推给本片。
6. **这台机器没有 embedding 端点**：LLM 走 DeepSeek，它不提供 embedding 模型，
   `AIWEB_EMBEDDING__{BASE_URL,API_KEY,MODEL}` 三键全空。所以"向量路真跑一次"缺的不是代码而是**靶**。
   roadmap §10 早就给过许可（"P4 可先用 hash 伪向量验证 profile 切换与回填流程"）；
   ADR-0015 把那条许可具体成了一个闸内替身。

## 2. 方案概述

六件事，各自穿到底：

- **向量路的靶**：交付 `backend/scripts/fake_embedding_server.py`（ADR-0015）——真 HTTP、真
  `/v1/embeddings`、按 model 名返回定长确定性向量。`.env` 把 embedding 三键指向它，于是
  `settings.embedding.configured` 变真、维度校验与非 200 分支全部走**同一条生产代码**，
  生产侧 `EmbeddingClient` 只有一个实现、`provider` 恒 `openai_compatible` 不动。
  语义类验收仍以真端点为主靶，到位前那一格写"只有用例、没有真跑"。
- **重建 = 一颗按数据源的同步作业**，复用 P3 那一整套（`sync_jobs` / `JobQueue` / worker /
  `sync_job_event` / SSE）。`phase` 九值词表**不加值**；变化只发生在 `embed` 那一档——它从
  `{skipped: true}` 变成真计数、真写向量。激活是**另一个端点**（`POST /api/kb/profiles/{id}/activate`），
  不混进作业里，因为"跑完"与"换基准"是两件事。
- **档案三态入库 + 同步只认 active**（ADR-0013）：迁移加 `status` CHECK 与 partial unique
  `WHERE status='active'`，激活是那条单语句 CASE UPDATE；`card_build` 不再 ensure profile，
  profile 的出生入口从此唯一 = reindex。
- **换维度 = 同表第二列**（ADR-0012）：`embedding_1024 vector(1024)` 带自己的 HNSW 索引，
  "维度 → 列名"全仓只住一个纯函数（与 `sync_vocabulary.stage_of` 同一条纪律）。
- **融合吃名次**（ADR-0014）：RRF `Σ 1/(rrf_k + rank)`，两路各出名次；关键词路的名次沿用 009 的
  计数键 `(命中词数 ↓, 命中列数 ↓, table_uid ↑)`，`score_kw` 只做展示；`mode='keyword'` 的降级路径
  与 `LikeRetriever` 逐字同构。权限过滤进向量 SQL 那一条语句（§5.2 踩坑 ④，与主流程那句自相矛盾的
  话统一成前者），粒度到源不到表（ADR-0008）。
- **枚举 NDV 采样进本片**，住在**逐表提交那一路**（019 的 `card_build` 同一条），不进 `collect()`——
  因为"单表采样失败其余表照落"这个失败语义，`collect()` 的"每库一事务"表达不出来。

跨过的接缝：`Retriever` Protocol（加第二个实现，调用方一行不动）、`JobQueue`（reindex 复用，
不新增动作）、embedding 客户端（新增，唯一实现）、`kb_index_profile` 的读写面（加 `status`）。
其余全在已有接缝上：`sync_jobs` / `sync_job_event` / `_set_phase` / `stage_of` / `rank_candidates` /
`kb_service` 的卡片写入 / `prompt_builder`（010 的裁切器**只复验不重造**）。

## 3. 用户故事（每条可独立验证）

### A. 档案三态与激活（roadmap 验收 1）

1. 迁移 0008 之后 `kb_index_profile` 有 `status text CHECK IN ('draft','active','retired')`，
   外加一条 partial unique `WHERE status='active'`。用例断**第二行 active 由库拒**（IntegrityError），
   不是断某条代码分支——"只有一个 active"必须由索引背书（ADR-0013）。
2. 现存那一行（P2/P3 期间由 `ensure_profile` 落的 `no-embedding@1536@tplv1`）在迁移里有个明确的
   归置口径，且迁移后库里**恰好一行 active**、`SELECT count(*)` 断得出来。
3. `POST /api/kb/reindex {datasource_id}` 返回 **202 + `{job_id}`**，同时库里多出一行
   `status='draft'`、`built_at IS NULL` 的新档案；**此时 active 那行不动**，`/kb/search`
   的结果与检索基准一字不变（"重建中不影响线上检索"是这条故事的全部用途）。
4. `POST /api/kb/profiles/{id}/activate` 是**一条语句**：新档案转 active、旧 active 同刻转 retired。
   用例断：激活前后 active 计数恒为 1，且旧档案的卡一行没删（可回滚的前提）。
5. 激活只校"卡片行数齐"（该 draft 档案下这个源的卡片行数 = 应建卡片的表数），
   **不校验 `embedding` 非空**——哪几张表 embed 失败由 `errors[]` 说，不由激活门槛说。
6. `built_at IS NULL`（还没跑完）的 draft 不允许激活 → 400 + 错误码；`retired` 的档案**可以**再激活
   ——这就是验收 5 那句"可回滚"的落点，要有用例正向证它。
7. `activate` 的 id 来源不靠新端点：**`POST /api/kb/reindex` 的 202 响应带 `{job_id, profile_id}`**，
   且这个 id 一路写进该作业的终局事件，所以"建了哪套 draft"是可回放的事实，不是要靠猜返回值。
   档案列表端点**不在本片**（见 §6）——诊断用的 `used_profile` 由 `/kb/search` 顶层那一格给。

### B. 同步只认 active（ADR-0013 后半）

8. `card_build` 的档案来源由"settings 三元组 ensure"改成"读库里 `status='active'` 那一行"，
   `index_profile_id` 取它。**profile 的唯一出生入口是 reindex**：全仓 `grep` 不到第二处
   往 `kb_index_profile` 插行的代码路径（`ensure_profile` 改只读或删）。
9. settings 三元组与 active 档案不一致时：**不写卡、不建档案**，作业仍可以 `success` 结束，但
   `sync_job_event` / 日志里有一条结构化告警，下一步文案指向 `POST /api/kb/reindex`
   （roadmap §启动自检项 7"维度三方一致"的运行时版本）。用例两个方向各一条：改 `DIMENSION`、改 `MODEL`。
10. 008/019 期间依赖 ensure 语义的用例随之重写，含 019 那条"`index_profile_id` 不进 upsert 的 `set_`"——
    **改的是来源，不是口径**。这是明示的影响面，不是回归。

### C. reindex 作业与 embed 档第一次真写

11. reindex 一颗作业**按数据源**（不是按档案、不是全库）；词表不加值，`embed` 仍是那第 8 个 phase、
    仍归 `embed` 那一档（`sync_vocabulary.py:23-34` 的映射一个字不改）。
12. `embed` 档的帧从 `{skipped: true, code, detail}` 变成**真计数**：`{stage:'embed', done, total, …}`
    逐表推进，`sync_jobs.progress` 跟着动。
    **已拍板的一条**（2026-10-02，spec 落笔时补问；它是决策 ②④ 与 `kb_service.py:295-306`
    "改文本就把 embedding 洗成 NULL"那个性质的合取）：普通同步（非 reindex）在
    三键已配**且**三元组与 active 档案一致时，同样把 `embed` 档真写完——
    否则 reindex 之后新增/变更的卡片永远没有向量，而它们在向量路里是**静默缺席**的
    （§5.2 (1) 最坏的那种失效：结果看起来正常）。三键为空时才回落到
    `{skipped: true, code: embedding_not_configured}`；`embedding_not_implemented` 那一支
    （`sync_service.py:761`）在生产路径上不再成立，**随本片删掉**——
    留一支"明明配了却跳过"的分支，等于给未来的自己留一条假说明。
    用例要正向断："配了端点的普通同步跑完 → 新建卡片的向量列非空"。
13. 每张卡的向量写入列由该档案的 `dimensions` 经"维度→列名"纯函数决定；`embedded_at` 同写。
14. **单表 embedding 调用失败**（真 HTTP 500 / 超时 / 连不上）→ 该表进 `errors[]`
    （`code` 新增一档，与既有各档同形），终局 `partial`，**其余表的向量照常在场**。
    这条决定了构造：采样与 embed 都只能在逐表那一路，不许进 `collect()`。
15. **维度不符**（端点返回数组长度 ≠ 档案 `dimensions`）→ **整颗作业 `failed`**，不截断、不补零、
    不"降级只写关键词"。失败信息里要同时出现两个数（期望维度、实收维度）。
16. 三键为空（没配端点、替身也没起）时 `POST /api/kb/reindex` → **400** 且**不建 draft 空壳**；
    出路文案说清"要么配真端点，要么起闸内替身"。

### D. 跨维度共存（roadmap 验收 5，ADR-0012）

17. 迁移 0008 给 `kb_card` 加第二列 `embedding_1024 vector(1024)`，**带自己的 HNSW 索引**
    （`vector_cosine_ops`，`m/ef_construction` 沿用 0005 那两个调参数）；1536 那列与它的索引不受影响。
18. "维度 → 列名"的映射全仓只有**一个纯函数**，`grep` 不到第二处把 `1024` 翻成列名字面。
    用例穷举它：1536→`embedding`、1024→`embedding_1024`、未预置的第三维→**显式报错**（不猜、不静默回落）。
19. 走一遍真流程（闸内替身按 model 名返回 1024 维）：新 draft 档案的向量落在 `embedding_1024`，
    **旧 1536 那批一个字节都不动**；激活后检索读 1024 列；再激活回旧档案则读 1536 列。
    两半各自有用例——验收 5 要的是"共存 + 可回滚"，不是只证前者。
20. `AIWEB_EMBEDDING__DIMENSION` 的角色变更要写进文档：它从"决定列宽"降为"声明用哪一维 → 选出对应列"
    （ADR-0012 后果段），§2.6 那句"dim 不硬编码在迁移里"（`metadata-model.md:330`）此后**只对第一列成立**。

### E. 混合检索与 RRF（roadmap 验收 2、3，ADR-0014）

21. 新增 `HybridRetriever` 实现同一个 `Retriever` Protocol；签名里**不出现** ef_search、维度、列名
    （那些走配置与内部路径，不进协议），切换只改 `retriever.py:448` 那一行赋值——
    `pipeline` 与 `/kb/search` 端点一行不动。这是 009 那份协议纪律的兑现，要有一条用例真的换实现来证。
22. 向量路一条 SQL 取全局 top：`<=>` 距离算子、`ORDER BY` 距离、`LIMIT top_vector`（默认 80），
    并在**同一句里**带 `index_profile_id = active` 与 `datasource_id = ANY(:visible)`（故事 32）。
    向量路的 80 与关键词路的 `_RECALL_LIMIT = 80`（`retriever.py:234`）是两个独立的数，
    不共用常量——否则调召回窗口会同时动两路，诊断就分不清是谁的功劳。
23. `SET LOCAL hnsw.ef_search` 在事务里发（§P4 踩坑 ②：SQLAlchemy 惰性 BEGIN 会让它变成 no-op）。
    用例必须证明**它真的生效**，而不只是拼进了 SQL——办法就是故事 28 的单调性。
24. RRF 是纯函数：两路名次 → `Σ 1/(rrf_k + rank)` → 融合次序。`rrf_k` 默认 60，来自 `.env` 新键
    `AIWEB_RETRIEVAL__RRF_K`，**不进请求体**。期望值手算，不碰 HTTP、不连库。
25. 关键词路的名次沿用 009 的计数键（`retriever.py:227` 那个 sort 一字不改）；`score_kw` 只进响应做展示。
    有一条用例正向钉住这一点：把 `score_kw` 换成任意单调变换，融合名次**不变**
    （反过来说，若哪天有人按 `score_kw` 排名次，009 那次"68 列宽表打穿 `order_main`"的破验收会当场复现）。
26. `mode='keyword'` 是"向量路整体下线"时的降级路径，行为与 P2 的 `LikeRetriever` **逐字同构**：
    009 那三条 live 用例在 `HybridRetriever` 下原样通过，不重测、不放宽。
27. 请求体放开三个旋钮并各自**真生效**：`mode`（`hybrid|vector|keyword`）、`top_vector`、`ef_search`。
    `trgm_threshold` 仍只取配置值，`rrf_k` 只在 `.env`；`extra="forbid"` 与未登录 401 不动。
    `.env` 新键 `AIWEB_RETRIEVAL__HNSW_EF_SEARCH`（默认 64）是**配置那一档**，请求体的 `ef_search`
    是单次覆盖；"请求给了用请求的，没给用配置的"这层裁决只住一个地方。
    两个默认值字面在这里是**冲突的**（roadmap 验收 3 写 64，`architecture §7:742` 的请求体写
    `ef_search=100`），本片统一成"**请求体 schema 不带默认值**，缺省即取配置"，§7 那行随之订正——
    留着两个数，验收 3 的"改配置"就说不清改的是哪一个。
28. 验收 3 的单调性：`ef_search` 64 与 400 各跑一次同一句问句，断**64 的向量名次集合 ⊆ 400 的**。
    这条防的就是"参数没传下去"，所以它必须跑在真 HNSW 索引上（`pg` marker）。
29. `/kb/search` 每项**扩三列**：`vector_rank` / `keyword_rank` / `rrf_score`（名次与融合分，
    不是相似度）；**顶层扩三格**：`used_profile`（active 档案的 `id/name/dimensions`）、
    `aggregated_tables`、`hops_used`。粒度**仍是表**，009 交付的 `hits`/`cards` 字段一个不删——
    §7 那句"目标形态是卡粒度"由本片正式订正为表粒度（依据是 §5.2 (4) 的聚合语义与 009 的既有形状）。
30. `hops_used` 的原料已由 014 交付（`join_graph`），`aggregated_tables` 的形状已由 009 交付；
    本片只把这两格填上，**不重做图、不重做聚合**。
31. `chat_messages.retrieved` 里 012 留成 NULL 的 `score_vec`/`fused`（`pipeline.py:215-221`）
    开始写**名次**，**字段名不改**（改名会牵动 §2.7 的列定义与未来前端契约）。
    `mode='keyword'` 时 `score_vec` 回到 NULL——012 那句"留 null 才能把'没命中'和'没算'分开"的口径继续成立。

### F. 权限（决策 ⑨，对齐 ADR-0008）

32. 可见源集合来自 `datasource_service.list_visible`，与关键词路同一把尺；过滤条件进**向量 SQL**，
    应用层那一次保留作二次保险。用例要能证明"把应用层那刀摘掉，无权源的卡也不在结果里"。
33. 粒度**到数据源、不到表**：本片不建 `datasource_grants`（§2.3 那张表继续无主，归 P6）。
34. `/kb/search` 点名一个看不见的源仍 **403**（009 的边界口径不动）；真检索路径（`/chat/ask`）
    仍按 §5.2 (5) 静默过滤——两件事继续分开。

### G. 枚举 NDV 采样（决策 ⑩，`kb-workflow §8`）

35. `.env` 新键 `AIWEB_EXTRACT__SAMPLE_ROW_LIMIT`（默认 1000）**出生即有读取点**；
    `sample_distinct` / `sample_distinct_max_distinct` 第一次被读到（此前只有定义点）。
    `test_env_example.py` 那条"`.env.example` ↔ `Settings` 一一对应"的闸会兜住漏写字面量的那一半。
36. 三步照 §8：按类型筛候选 → `SELECT col, COUNT(*) … GROUP BY col LIMIT 40` →
    写 `meta_column.enum_values` 与 `sample_values`。`.env.example` 那条"只取值不存行数据"的
    注释口径不许破——落库的是**值域**，不是行。
37. 单表采样失败**按表进 `errors[]`** 落 `partial`，其余表已写入的 `enum_values` 照常在场
    （同故事 14 的构造：逐表那一路，不进 `collect()`）。
38. `SourceDialect.sample_distinct()` 是可选方法、默认 disabled 的形状不动；本片**MySQL 与 PG 两方言都实现**。
    超过 `max_distinct` 的高基数列**只记基数不记值域**（§8 第二步的阈值语义），用例要有这一档。
    采样开关的**默认值改成 `false`**（§4 行 14），所以这一档的闸内证据必须**显式把键打开**才拿得到
    ——代价与手测要求写在 §7 那条"默认值"注里，别以为装完 P4 枚举值域就自动在场。

### H. 测试分层与闸内替身（决策 ①、⑪，roadmap 验收 4）

39. 纯函数单测（RRF、融合次序、维度→列名、切词、名次键、`ef` 取值裁决）**不碰 HTTP、不连库**；
    `AIWEB_PG_TEST_DSN` 缺失时整文件照跑——这就是验收 4 的左半边。
40. 需要 embedding 客户端的用例挂 `live`：真起 `backend/scripts/fake_embedding_server.py` 子进程
    （随机端口、等启动、`finally` 杀干净）。`addopts` 不动（仍是 `-q --strict-markers`），
    默认闸继续整轮真跑。
41. 需要真列/真索引形状的用例（`<=>`、HNSW、`SET LOCAL`、第二列 DDL、单调性）挂 `pg`；
    无 DSN 时干净 skip，`AIWEB_REQUIRE_PG_TESTS` 把 skip 变 fail——**现有机制不动**。
42. 替身自身有一条用例：同一输入两次调用逐位相同、按 model 名分别报 1536 与 1024 维。
    （确定性是故事 28 那个超集断言的前提——随机向量会让单调性永远测不出东西。）
43. **语义类验收的证据边界**：验收 2 那句"销售额命中 `payment_record` 与 `order_main`"在闸内由替身跑
    **形状**（三列名次齐、聚合与诊断字段齐），语义那一格在收口里写**"只有用例、没有真跑"**，
    直到用户配好真端点（密钥不进对话）。这与 P3 收口那四格同形，不许含糊成"真跑过"。

### I. 验收 6 的复验与端点命名（决策 ⑥）

44. `TOKEN_BUDGET=1000` **只做复验**：把预算调到 1000 跑一次真检索 → prompt 组装，断送进 prompt 的
    表数显著变少、**不报错**、日志有被裁表名。裁切器**不许重造**（010 已交付，断言在
    `tests/unit/test_prompt_builder.py`）。
45. `/kb/rebuild` 旧名全部回写（`architecture.md:741`、`:106` 那行 `reembed.py 归 P4`、
    `kb-workflow.md:230`、`:234`、`endpoints/kb.py:3` 的注释），统一 **`POST /api/kb/reindex`**；
    `/kb/reembed`（`kb-workflow.md:184`、`roadmap.md:491`）**留 P9**，不建空壳端点、不建 `reembed.py`。
46. roadmap §P4 六条验收逐条对上：1→故事 1/3/4，2→22/29/43，3→27/28，4→39/41，5→17~19，6→44。

## 4. 已定决策（不要翻案）

| # | 决策 | 依据 |
|---|---|---|
| 1 | 向量路的靶 = 闸内伪向量替身（真 HTTP、OpenAI 兼容、确定性 n-gram 哈希）+ 真端点为语义类主靶 | ADR-0015；roadmap §10 的许可 |
| 2 | 生产 embedding 客户端只有一个实现；`provider` 恒 `openai_compatible` 不动 | ADR-0015（否掉"第二实现 + 新 provider 键"） |
| 3 | 测试拓扑**只 subprocess 一层**，需要客户端的用例全走真进程；`addopts` 不动 | 用户拍板（否掉双层 transport 注入），代价记在 ADR-0015 |
| 4 | reindex 按数据源一颗作业，复用 `sync_jobs`/`JobQueue`/worker/`sync_job_event`/SSE；`phase` 九值不加值 | 本轮拍板；ADR-0010/0011 |
| 5 | 激活是**独立端点**，不混进作业；profile 唯一出生入口 = reindex | ADR-0013 |
| 6 | 换维度 = 同表第二列 `embedding_1024`；"维度→列名"唯一住在纯函数 | ADR-0012（否掉影子表） |
| 7 | `status(draft\|active\|retired)` CHECK + partial unique `WHERE status='active'`；激活=那条 CASE UPDATE | ADR-0013（否掉"唯一索引 + 时间戳两真相"） |
| 8 | 同步只认 active；三元组不一致 → 告警且**不写卡** | ADR-0013（关掉 `profile_row(is_active=True)` 后门） |
| 9 | 单表 embed 失败 → `errors[]` + `partial`；维度不符 → 整颗 `failed`；activate 只校卡片行数 | 用户拍板（覆盖我推荐的 warnings-only） |
| 10 | 融合吃**名次**不吃分数；关键词路名次沿用 009 计数键；`mode=keyword` 与 `LikeRetriever` 逐字同构 | ADR-0014 |
| 11 | `/kb/search` 保持表粒度 + 三列名次 + 顶层三格；请求体开 `mode/top_vector/ef_search`；`rrf_k` 只进 `.env` | 本轮拍板；订正 §7 的卡粒度"目标形态" |
| 12 | 权限过滤进向量 SQL、`datasource_id = ANY(可见源)`、粒度到源；`datasource_grants` 归 P6 | ADR-0008；§P4 踩坑 ④ 优先于 §5.2 主流程那句 |
| 13 | 枚举 NDV 采样进 P4（三键一起出生），失败语义住在逐表那一路 | 本轮拍板 + P3 spec §6 那条"整块推 P4" |
| 14 | 采样开关默认 **`false`**（`settings.py:150` 与 `.env.example:72` 的 `true` 随本片改口） | 用户 2026-10-02 拍板；代价见 §7（默认路径下问题 5 那一格不闭） |
| 15 | 普通同步在**三键已配且三元组与 active 档案一致**时也真写 embed；`embedding_not_implemented` 那支删掉 | 用户 2026-10-02 拍板；性质来自 `kb_service.py:295-306` |

## 5. 测试决策

- **最高接缝仍是 HTTP**：向量/混合类验收从 `POST /api/kb/reindex` 与 `POST /api/kb/search` 进，
  worker 循环体照 P3 的做法 await 而不是真起子进程（可调试堆栈，且 P3 已经付过一次
  "进程内/子进程环境变量口径不一致"的学费）。
- **期望值来自独立真相**：RRF 名次手算、维度→列名按 §2.6 的字面列名、`total` 按演示库口径
  （排除下划线前缀 = 10）、partial 的表名清单来自注入点而不是被测代码。
  唯一允许的"由实现算"是纯函数自己的输出进纯函数（例如融合次序由名次函数喂），那叫组合不叫自证。
- **假绿防线**（P2/P3 数过的模式都是"形状测试原理上看不见"）：
  ① `ef_search` 必须有**单调性**用例，否则"参数没传下去"永远绿；
  ② 第二列向量必须有**旧列未被改动**的用例，否则"重建覆盖了旧向量"这种事故看起来像成功；
  ③ 单表 embed 失败必须断"其余表的向量真在场"（查库），不能只断 `partial` 这个字符串；
  ④ 激活必须断**库里的 active 计数**，不能只断返回码；
  ⑤ 权限过滤必须有一条"摘掉应用层那一刀"的用例，否则向量 SQL 里那个条件写错也测不出。
- **替身不是 mock**：跨进程真调用挂 `live`（`CONTEXT.md` 那条 _避免_ 就是这个分别）。
  单测里那个不占端口的对象继续叫 mock，别把两件事混称。
- 闸是 `scripts/dev.ps1 check`（ruff + vue-tsc + pytest 含 live，基线 980 passed / 2 skipped）
  + 手动的 `ruff format --check` 与 `python -m mypy app`。跑闸前确认本机 MySQL 演示库、PG 与
  （向量类用例）假端点在线。

## 6. 明确不做

- **`/kb/status` 端点与档案列表端点**：`/kb/status` 是 §7 承诺项、无工单认领（P3 收口已记进无主账）；
  `GET /kb/profiles` 这类列表端点**也不建**（008 期间就定下的口径：`endpoints/kb.py` 里不放空壳）。
  本片需要的那几格（active 档案、维度、卡片数、已嵌数）由 `/kb/search` 顶层的 `used_profile`
  与故事 7 的 202 响应给，缺了就记进无主账，不顺手补。
- **`/kb/reembed`**（单表编辑后重嵌）：归 P9，不建空壳端点、不建 `scripts/reembed.py`。
- **LLM 改写 (0) 与 simple FTS (2c)**：仍不接。`AIWEB_RETRIEVAL__ENABLE_REWRITE` 键保留但要在注释里
  **写清归属**；(2c) 不接的理由照 009 那条（PG 无中文分词器，`simple` 对 L1 等于零贡献）。
- **`tiktoken` 与 `AIWEB_RETRIEVAL__TOKENIZER`**：不引（010 的裁切沿用 `estimate_tokens` heuristic）。
- **`pypinyin` 拼音首字母增强**：不接，原料与开关键都不建。
- **`app_settings` 那层 L3 运行参数**：不建表、不做热改。roadmap §P9 验收 3 今天要的是
  "配置改一次、重启才生效"那一层，L3 地基仍无主（P3 收口已记）。所以 `rrf_k`/`ef_search` 默认值
  走 `.env`，不走库内热改。
- **`datasource_grants` 表与 GET/PUT `/datasources/{id}/grants`**：归 P6。
- **任何前端**（调参页 `/knowledge/search`、档案列表 UI）：本片只出后端契约。
- **cancel / retry / `GET /sync/jobs` 列表与详情**：继续挂账，`cancelled` 仍无写入点。
- **版本闸门、`SCHEMA_PARTIALLY_VISIBLE`、`manifest_digest`、结果 csv 保留期**：P3 那批无主项
  不因本片自动消失。
- **不引入第二个 embedding provider、不引入向量数据库、不做 IVFFlat、不开多 worker**。

## 7. 补充说明

- **迁移只有一条**：`0008_embedding_columns`（`kb_index_profile.status` CHECK + partial unique、
  `kb_card.embedding_1024 vector(1024)` + 其 HNSW 索引）。`alembic check`（ORM ↔ 迁移漂移）
  **仍没接线**，所以这条要人工与 `app/models/kb.py` 比对一次。编号接现有最新的迁移文件（0007）之后；
  **真库 `alembic_version` 停在哪儿不算已知**——P3 spec 写过"停在 0002"，而 0007 的
  `sync_job_event` 又在 025 的真跑里被用过，这两句话有一句是过期的，开工第一件事是 `alembic current`
  查一次，别照文档推断。元数据库**只由 alembic 动结构，不手敲 DDL**。
- **环境前置（唯一可能卡住验收 2 语义那一格的一条）**：真 embedding 端点由**用户自行**配进
  `.env` 的三个 `AIWEB_EMBEDDING__*` 键（密钥不进对话、不进 git）。到位前语义类验收按故事 43 的口径
  签"只有用例、没有真跑"；闸内的形状/切换/回填由替身承担。
- **Windows 约束**（替身子进程叠三条本机事实）：`psycopg` 3.3.6 async 在默认 `ProactorEventLoop`
  上抛 `InterfaceError`，子进程要 `SelectorEventLoop`（024 已实测，`run_worker.py:40` 已切）；
  控制台 cp936，子进程要 `PYTHONIOENCODING=utf-8`；本机没有 `pwsh`，文档里的
  `powershell -File` 字面照 Makefile 走。
- **`.env` 新键三个**：`AIWEB_RETRIEVAL__RRF_K`（60）、`AIWEB_RETRIEVAL__HNSW_EF_SEARCH`（64）、
  `AIWEB_EXTRACT__SAMPLE_ROW_LIMIT`（1000）。`test_env_example.py` 的一一对应闸兜漏写；
  `.env.example` 的注释里要给 `SAMPLE_ROW_LIMIT` 写上 §8 那句"只取值不存行数据"。
- **采样默认值 = `false`（2026-10-02 拍板，代价一并认下）**：`sample_distinct` 今天在
  `settings.py:150` 与 `.env.example:72` 都是 `true`，而 `roadmap` 分组 7 写的是 `false`
  并注着"扫源库数据，默认关"。键一有读取点，"默认 true"就变成**每次同步都真去源库跑 `GROUP BY`**，
  而抽取层至今没有 `SET statement_timeout` 那一格保护（P3 无主账 ①）。所以两处改成 `false`，
  roadmap 那句注释是唯一说对的。**认下的后果**：默认配置下 §8 三步不跑，非 ENUM 的中文枚举列
  （用户说"已完成"、库存 `completed`）**仍进不了卡片**——§1 问题 5 那一格在默认路径上没有闭环，
  证据只来自"显式开 `SAMPLE_DISTINCT=true`"的那条用例 + verification 里一份同口径的手测记录。
  这不是把 P4 的验收打折，是把"扫谁的数据、什么时候扫"这条决定权交回用户。
- **需订正的旧文档字面（写侧先行，施工前一次改完，别留到收口）**：
  ① `architecture §5.1` as-built(P2-0009) 注 1"P4 的 RRF 会把分数重新变成次序本身"→ 按 ADR-0014 订正；
  ② `§5.2` 主流程"全局 top-80 → 权限过滤"与踩坑 ④"过滤要在 SQL 里"自相矛盾 → 统一成后者；
  ③ `§5.2 (3)` 的 RRF 那句补"两路名次各自的来源"；
  ④ `§7:741` `/kb/rebuild` → `POST /api/kb/reindex`（+ `POST /api/kb/profiles/{id}/activate`），
  `§7:742` 的卡粒度"目标形态"→ 表粒度扩三列；**同一行**请求体里那两个默认值
  （`top_vector=80`、`ef_search=100`）改成"缺省取配置"——64 与 100 的冲突由故事 27 收口；
  ⑤ `architecture:106` 那行"`reembed.py` 归 P4，尚未落地"→ `fake_embedding_server.py` 归 P4、`reembed` 归 P9；
  ⑥ `kb-workflow:184/:230/:234` 三处旧名与 `endpoints/kb.py:3` 的注释；
  ⑦ `metadata-model §2.6:330` "dim 不硬编码在迁移里"→ 只对第一列成立（ADR-0012 后果段）；
  ⑧ `§2.6` as-built(0008) ①"P2 只落一条行、直接 `is_active=true`"→ 改口指向 ADR-0013 的三态与唯一出生入口；
  ⑨ `§2.7` `retrieved` 五键里 `score_vec/fused` 的口径从"恒 null"改成"P4 起写名次，`mode=keyword` 时 null"；
  ⑩ `backend/.env.example:72` 的 `AIWEB_EXTRACT__SAMPLE_DISTINCT=true` 与 `settings.py:150` 的默认值
  一起改成 `false`（`roadmap` 分组 7 那句注释保持不动，它是对的），`metadata-model §6` as-built
  里"`sample_distinct` 无读取点"那条改判为**本片有读取点、默认关**。
  `roadmap §P3` 无主账里"NDV 采样推 P4"那条改判为**本片交付**。
- **本片会顺带闭掉的旧账**（仅这四条，其余无主项不因此消失）：
  `SAMPLE_ROW_LIMIT`/`sample_distinct*` 三键第一次有读取点；`meta_column.enum_values`（非 ENUM 列）与
  `sample_values` 第一次有写入点；`kb_index_profile` 三态与唯一 active 入库；`/kb/rebuild` 与
  卡粒度两处文档矛盾。
- **024 遗留的那条待追认**仍开着：本轮为拿证据对元数据库那台 PG 发过 `metadata-model §8.2` 的
  只读 `SELECT`（不落行、不发 DDL）。它没越"不许拿 `aiweb`/`aiweb_test` 当源库抽取"的界，
  但**记法要用户拍板**，本片不代答。
