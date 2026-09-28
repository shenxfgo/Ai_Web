# Ai_Web 架构说明

> 本文是长期维护的架构参考。内容取自实施方案（架构正文 + 附录 §8–§11）与仓库已落地代码，
> 规划过程中的讨论性文字已剔除。安全模型细节见 [nl2sql-safety.md](./nl2sql-safety.md)，
> 表结构细节见 [metadata-model.md](./metadata-model.md)。

---

## 1. 系统上下文

Ai_Web 是一个「AI 问数」平台，面向**只读分析查询**场景：

```
业务源库 (MySQL 5.7 / PostgreSQL)
        │ ① 元数据抽取（information_schema / pg_catalog，批量 SQL）
        ▼
元数据库（远端 PostgreSQL + schema aiweb）
        │ ② 表级知识卡片 + 关键词索引 +（可选）pgvector 向量
        ▼
自然语言问数链路
   问题 → 改写/术语对齐 → 检索候选表 → 组装 prompt → LLM 生成 SQL
        → SQL 守卫（三层只读防御）→ 源库只读执行 → 结果集 + 图表 + 结论
        ▼
        SSE 流式返回前端（Vue3 + Element Plus + ECharts）
```

三条不可动摇的边界：

- **源库永远是只读的**：抽取和问数执行都不写源库（详见 nl2sql-safety.md）。
- **元数据库不存业务数据行**：`aiweb` schema 只存"关于表的知识"与应用自身数据（用户、任务、会话）。
- **业务结果集不落元数据库**：查询结果落文件目录 `data/results/`，库里只留摘要与统计。

Monorepo 布局：根目录 `backend/`（FastAPI + Python 3.12 + uv）与 `frontend/`（Vue3 + TS + Vite + Element Plus），
另有 `scripts/`、`Makefile`、`docs/`、`knowledge/`、`data/`。理由：solo dev + MVP 场景下一次 commit 能覆盖
"加字段 → 迁移 → API → 前端类型"的垂直切片，`uv.lock` 与 `package-lock.json` 各自独立、互不污染。

---

## 2. 组件拆分

### 2.1 后端目录

```
backend/
├── pyproject.toml            # uv 管理；[dependency-groups].dev
├── .env.example              # 配置契约来源（键清单见 roadmap.md）
├── alembic.ini               # sqlalchemy.url 留空，由 env.py 从 settings 注入
├── app/
│   ├── main.py               # create_app() 工厂 + lifespan（配置自检/连接探测）
│   ├── settings.py           # pydantic-settings，唯一配置入口
│   ├── deps.py               # get_db / get_current_user / require_admin / ds_scope
│   ├── api/
│   │   ├── router.py         # 汇总 /api，挂各 endpoints
│   │   └── endpoints/
│   │       ├── health.py     # /healthz（无鉴权）· /health（带 PG/扩展/LLM 探测）
│   │       ├── auth.py       # login/refresh/me/password
│   │       ├── datasources.py
│   │       ├── grants.py     # 数据源授权（admin）
│   │       ├── metadata.py   # 库/表/字段/索引/关系 只读浏览 + 人工补录
│   │       ├── sync.py       # 触发/列表/详情/取消/SSE 进度
│   │       ├── kb.py         # 检索预览、卡片查看、术语补录、reindex
│   │       ├── chat.py       # 会话/消息/SSE 问数/SQL 重跑/反馈
│   │       └── admin.py      # 用户管理、索引 profile、系统统计
│   ├── core/
│   │   ├── db.py             # async engine + sessionmaker + Base（显式 schema）+ 命名约定
│   │   ├── security.py       # JWT encode/decode、密码哈希、Fernet encrypt/decrypt
│   │   ├── sse.py            # sse_format() / 进度通道（PG 表 + 进程内 asyncio.Queue）
│   │   ├── errors.py         # AppError 层级 + 统一 envelope {error:{code,message,detail}}
│   │   ├── pagination.py     # 游标/页码统一
│   │   └── logging.py        # 结构化 JSON 日志 + request_id contextvar
│   ├── models/               # SQLAlchemy 2.0 typed ORM，一文件一聚合
│   ├── schemas/              # Pydantic v2 DTO（与 models 解耦）
│   ├── services/
│   │   ├── datasource_service.py   # CRUD + Fernet + test_connection
│   │   ├── sync_service.py         # 同步编排、幂等 upsert、软删除标记
│   │   ├── kb_service.py           # 卡片生成（纯函数）+ 建索引 + 检索
│   │   ├── embedding_client.py     # OpenAI 兼容 /embeddings（httpx，可注入便于 mock）
│   │   ├── llm_client.py           # OpenAI 兼容 /chat/completions（含流式）
│   │   ├── chart_advisor.py        # 结果集 → 图表选型（纯函数）
│   │   ├── sql_guard.py            # ★ sqlglot AST 只读白名单
│   │   ├── source_manager.py       # 按 datasource 缓存只读 async engine/pool
│   │   └── nl2sql/
│   │       ├── pipeline.py         # 编排 + SSE 事件产出
│   │       ├── retriever.py        # Protocol 接缝：LikeRetriever → HybridRetriever
│   │       ├── join_graph.py       # 关系图 & JOIN 路径 BFS（纯函数）
│   │       ├── prompt_builder.py   # Jinja2 → system/user messages
│   │       ├── few_shot.py         # 示例库检索（成功问答回流）
│   │       └── executor.py         # 会话只读设置 / 超时 / 流式取数 / 截断
│   ├── extractor/
│   │   ├── base.py           # SourceDialect Protocol + Raw* 中间结构 + SourceManifest
│   │   ├── registry.py       # kind → dialect 工厂；未来加 mssql/oracle/ch
│   │   ├── mysql.py          # 手写 information_schema 批量 SQL
│   │   └── postgres.py       # 手写 pg_catalog 批量 SQL
│   └── prompts/              # nl2sql_system.j2 / nl2sql_user.j2 / rewrite.j2 / summarize.j2 / card_template.j2
├── alembic/versions/         # 0001_baseline（扩展）→ 0002_users → 0003_datasource → …
├── scripts/                  # init_demo_mysql.sql / seed_admin.py / check_env.py / demo_ask.py / reembed.py
└── tests/                    # conftest.py + guard/ + unit/ + integration/ + fixtures/
```

关键接缝：`retriever.search()` 定义成 `Protocol`，最小链路阶段提供 `LikeRetriever`（元数据列注释
ILIKE + 命中列数排序），完整版换 `HybridRetriever`，`pipeline` 只依赖 Protocol。这样"向量检索不可用时
降级回结构化路径"是生产可用的容错分支，而不是抛弃代码。

### 2.2 前端目录

```
frontend/src/
├── api/http.ts          # axios 实例：token 注入 / 401 刷新单飞 / 错误 → ElMessage
├── api/sse.ts           # ★ fetch + ReadableStream 的 POST-SSE 解析器（不用 EventSource，见 §6）
├── api/modules/         # auth / datasource / metadata / sync / kb / chat
├── types/               # 与后端 schemas 对齐（可用 openapi-typescript 生成）
├── router/              # 懒加载 + meta{requiresAuth, roles, title} + guards
├── stores/              # auth（双 token）· datasource · chat（流式状态机）· ui
├── layouts/             # DefaultLayout（侧栏 + 头部）· BlankLayout（登录）
├── components/
│   ├── sql/SqlEditor.vue        # CodeMirror 6 + 只读模式
│   ├── sql/SqlGuardReport.vue   # 校验结果可视化（拒绝原因）
│   ├── table/ResultTable.vue    # 虚拟滚动 + 类型格式化 + 导出 CSV
│   ├── chart/ChartBox.vue       # ECharts 封装 + 类型手动切换
│   └── sse/ProgressBar.vue
└── views/               # Login / Dashboard / datasource / metadata / sync / knowledge / chat / history / admin
```

版本必须成组锁：`typescript ~5.9`（7.x 与 `vue-tsc`、`@typescript-eslint` 的 peer range 不兼容）、
`vite ^6.3.5`（必须能设 `server.compress=false`，否则 SSE 被缓冲）、`vue ^3.5`、
`dompurify`（结论走 markdown → `v-html`，模型可控内容必须净化）。

---

## 3. 三种存储

### 3.1 元数据库（远端 PostgreSQL，schema `aiweb`）

存什么：用户/授权、数据源定义（口令 Fernet 加密后存 `bytea`）、元数据快照（库/表/列/索引/关系）、
同步任务、知识卡片与索引 profile、问答会话与消息（含 `retrieved`、`sql_raw`/`sql_final`、`guard_result`、
`result_stats`、`chart_spec`、`conclusion`）。

不存什么：**业务数据行**。查询结果的行集走文件（§3.3）；库里只留列定义、行数、是否截断、耗时等统计。
完整 DDL 与语义见 [metadata-model.md](./metadata-model.md)。

扩展依赖：`pg_trgm`（trusted，应用自己能建）、`btree_gin`（可选）、`vector`（pgvector，**可插拔**，见 §5）。
`alembic_version` 也落在 `aiweb` schema 内（`version_table_schema` 必须显式传，否则会落到 `public`
并表现为"看起来没迁移"）。

### 3.2 `knowledge/` — 人工维护的 markdown 覆盖层

仓库根的 `knowledge/` 目录是**版本库内容**（`.gitignore` 明确注释：
`# knowledge/ 是人工维护的知识文件，属于版本库内容，不忽略`），
用于把人工补录的业务语义（中英文名、粒度、口径、枚举含义、关系确认）以 markdown 形式纳入 git 审阅流。
目录根与配置项 `AIWEB_KB_DOCS__DIR=knowledge` 对应。

与元数据库的关系是**单向回写**：`AIWEB_KB_DOCS__AUTO_IMPORT=true` 时启动把各文件的
`## 人工维护` 区块回写进元数据库的人工列（`comment_zh` / `business_desc` / `granularity` / `is_hidden`），
同步与自动生成**永不**反向覆盖 markdown 里的人工区块。规则细节见 [kb-workflow.md](./kb-workflow.md)。

### 3.3 `data/results/` — 结果集文件

`AIWEB_RESULT__DIR=data/results`，每次问数执行产生的行集落在这里，配合：

| 键 | 默认 | 含义 |
|---|---|---|
| `AIWEB_RESULT__RETENTION_DAYS` | 30 | 落盘结果保留天数 |
| `AIWEB_RESULT__PREVIEW_ROWS` | 200 | 首屏预览行数（SSE 首批） |
| `AIWEB_RESULT__MAX_CELL_CHARS` | 1000 | 单元格截断，防结论 prompt 爆 |
| `AIWEB_RESULT__MAX_PAYLOAD_MB` | 2 | 单页 JSON payload 上限 |

`.gitignore` 里 `data/*` 被忽略、只保留 `!data/README.md`，即**结果集内容不进版本库**；
`make clean` 的 recipes 也刻意不动 `data/` 与 `knowledge/`（"只清构建产物与缓存，绝不动数据库、结果集和 knowledge/"）。
落盘目录与 `knowledge/` 一样在启动体检里做"建目录 + 写探针文件 + 删除"的可写性检查（`scripts/check_env.py::check_dirs`）。

结果文件的下载接口必须做目录穿越防护，见 [nl2sql-safety.md](./nl2sql-safety.md#7-结果文件下载与目录穿越防护)。

---

## 4. NL2SQL 流水线

### 4.1 步骤

```
POST /api/chat/ask  {session_id?, datasource_id, question, history_ids?[], options{enable_rewrite,temperature}}
  ① 改写（可选，AIWEB_RETRIEVAL__ENABLE_REWRITE=false 时跳过，省一次 LLM 往返）
     指代消解（"上个季度" → 绝对日期）+ 术语对齐（"GMV" → 口径）
  ② 检索候选表（§5 阶梯）
  ③ JOIN 路径推导（meta_relation + 命名约定推断，桥表扩展）
  ④ 组装 prompt（角色 → 硬约束 → schema 卡片 → JOIN → 术语 → few-shot → 问题 → 输出格式）
  ⑤ LLM 生成 SQL（流式，逐 token 出）
  ⑥ SQL 守卫（三层防御，见 nl2sql-safety.md）
  ⑦ 源库只读执行（会话只读 + 超时 + 行数上限 + 截断探测）
  ⑧ 结果摘要 + 图表选型（chart_advisor 纯函数，不用 LLM）
  ⑨ 结论生成（≤50 行喂 markdown 表；更多行只喂 sum/avg/min/max + top/bottom 5 摘要）
```

**失败即早退（fail-fast）**：检索为空 → **不生成 SQL**，直接返回 `NO_SCHEMA_FOUND`，
并给"请补录表注释 / 检查权限 / 点此同步"的具体下一步。宁可拒答，不要让模型对着空 schema 编 SQL。

### 4.2 图表选型规则（确定性，可单测）

1. 1 行 1 列 → `kpi`（大数字卡）。
2. 首列是日期/时间/整数序数（distinct 占比 > 0.6）+ ≥1 数值列 → 单数值列用 **line**（时间）/ **bar**（离散类别）；
   多数值列且列名同族（`2024-01`、`2024-02`… 或 `_sum` 后缀）→ 转长表后 **stacked bar** / **multi-line**。
3. 首列低基数文本（distinct ≤ 20 且非时间）+ 1 数值 → **bar**；合计≈100 或列名含 ratio/pct/share/rate → **pie**（series ≤6，其余归"其他"）。
4. 2 个数值列、无类别列、行数 ≥ 30 → **scatter**。
5. 列数 ≥ 8 或 distinct/rows > 0.9 → **table**（不画图）。
6. 一律给 `fallback='table'`，UI 提供手动切换 tab —— 图表类型判断永远会错，逃生门必须留。

---

## 5. 检索阶梯与可插拔的向量增强

### 5.1 四层阶梯（主链路）

问数链路对"找到相关表"这件事是**分层降级**的，任何一层拿不准就落到下一层：

| 层 | 手段 | 依赖 | 说明 |
|---|---|---|---|
| **L1 结构化关键词** | 在元数据库上对表名/列名/`comment_raw`/`comment_zh`/业务描述做精确、前缀、ILIKE、pg_trgm 匹配并按命中列数排序 | 只需 `pg_trgm`（trusted，应用可自建） | 永不下线的主链路；`LikeRetriever` 即其最小实现 |
| **L2 LLM 目录摘要选表** | 把候选表目录（表名 + 一行注释）压成 digest 交给 LLM 直接挑表 | 只需 LLM | 超过 `AIWEB_RETRIEVAL__CATALOG_DIGEST_MAX_TABLES`（默认 1000）时，**先按关键词预筛**再交给模型选表，避免目录本身打爆 prompt |
| **L3 完整卡片抓取** | 命中的表取其**表级知识卡片全文**（字段、枚举取值、索引、关系、方言约束）进 prompt | 元数据快照 + 卡片构建 | 卡片模板见 kb-workflow.md；宽表按列切片 |
| **L4 `NO_SCHEMA_FOUND`** | 前三层都拿不到足够证据时明确拒答 | — | 早退，给补录/授权/同步的可执行下一步 |

**pgvector / embedding 是可插拔增强，不在主链路上。** 语义：

- 未配置向量端点（base_url/key/model 不齐，或 `AIWEB_EMBEDDING__DIMENSION=0`）时，
  `settings.embedding.configured` 为 `False`，检索直接走 L1/L2 结构化路径，**链路不断**。
  `/api/healthz` 用 `retrieval_mode` 明说当前形态：`"keyword"` 或 `"vector+keyword"`。
  `DIMENSION` 从 0005 起兼任 `kb_card.embedding` 的**列宽**（建库期硬依赖 `vector` 类型），
  见 kb-workflow.md §7 末条与 ADR-0003 补注。
- 启动体检在未启用向量时把 `embedding` 判为 `ok`（"未启用，检索走结构化路径"），只有启用了才探活并核对维度。
  as-built(P2-0005)：但 `vector` **扩展**在 `DIMENSION>0` 时就要检（fatal），判据从"启用向量路"改为列宽已定。
- 启用向量后走的是下面 §5.2 的混合检索（向量路 + 关键词路 RRF 融合），它提升召回质量，但移除后系统仍可问数。

### 5.2 混合检索完整流程（向量增强启用后）

```
query
 ├─(0) 改写与术语对齐（LLM，可选）→ query_vec_text, query_kw_terms[]
 ├─(1) 向量路：SELECT id,table_id,kind, 1-(embedding <=> $1::vector(1536)) AS score
 │       FROM aiweb.kb_card
 │       WHERE deleted_at IS NULL AND index_profile_id = $active
 │       ORDER BY embedding <=> $1::vector LIMIT 80
 │       （SET LOCAL hnsw.ef_search = 100; iterative_scan='relaxed_orders'）
 ├─(2) 关键词路：terms = query_kw_terms + 从问题里正则抽出的英文标识符/驼峰拆词
 │       a) 精确/前缀：search_text ILIKE '%'+term+'%'      → score 1.0  （标识符权重最高）
 │       b) 模糊：similarity(search_text, term)            → gin_trgm_ops
 │       c) simple FTS：to_tsvector('simple',search_text) @@ plainto_tsquery('simple',term)
 │       三路 max() 融合，LIMIT 80
 ├─(3) RRF 融合： score = Σ 1/(60 + rank_i)   （k=60，纯函数单测）
 ├─(4) 去重/聚合到表：group by table_id → best_card_score + Σ 该表被命中卡片的 boost(0.05×(n-1))
 │       同 table_id 的 table_columns 卡不单独占位（避免碎片挤占）
 ├─(5) 权限过滤：JOIN data_sources × (allow_global OR grant OR owner)，只保留有权表
 ├─(6) 图扩展：对 top-K 表补 JOIN 桥表，桥表以"关系卡"身份进 prompt，不占 K 名额
 ├─(7) 裁切：按表分数降序装填 K 张表 + token 预算；超预算优先保留主卡前 25 列 + PK/FK 列
 └─(8) 落 chat_messages.retrieved（每卡带 score_vec / score_kw / fused，UI 展示可解释性）
```

相关约束（都已在计划期核实，属硬事实）：

- **PG 内置 FTS 没有中文分词器**（`to_tsvector('english','订单金额')` 只出 1 个 lexeme），
  所以主关键词路是 pg_trgm + 精确标识符命中，FTS 仅用 `simple` 配置作辅助。
  `AIWEB_RETRIEVAL__TRGM_THRESHOLD=0.08`——中文短串在 trgm 上分数极低，别指望它做语义。
- **带 `WHERE datasource_id=?` 的向量检索会退化**（HNSW 过滤已知短板）→
  先取全局 top-80 再按权限过滤，并在向量 SQL 里就 `JOIN grant` 过滤（应用层过滤只作二次保险）。
- 换 embedding 模型 / 改卡片模板 = 新建 `kb_index_profile` → 全量重建 → 原子切 active；
  检索永远只命中 active 的卡片，否则新旧向量混在一个索引里，结果不可解释且无法回滚。

### 5.3 JOIN 路径推导

- 边权重：`manual` = 0.1、`extracted` = 0.5、`inferred` = 1/confidence；两两 targets 求最短简单路径后取并集。
- 无外键的分析库（现实常态）：按命名约定推断 `t1.c → t2.c'`（列名同、`t2` 的 PK 是该列、类型族兼容），
  打分 `0.35 + 0.25[一侧是PK] + 0.15[类型完全相同] + 0.15[后缀 _id/_no/_code 且前缀与 t2 主名词匹配] + 0.10[t2 表名是 c 去后缀的单/复数变体]`，
  写入 `meta_relation(source_kind='inferred', confidence)`，**只有 ≥0.8 才进 prompt**，且带 `[推断,置信 0.87]` 标签。
  候选度 > 8 的字段（如 `org_id` 出现在 40 张表）需另一端有唯一约束，否则丢弃并 warning（抑制组合爆炸）。

  > **未解决的口径冲突（0007 施工中发现，留给 010/P4 拍板）**：本节的加权打分与
  > roadmap §P4、verification §2.1 的"**命名约定 = 0.7**"（一个常数）不是同一件事。
  > 更要紧的是两者**互斥**：常数 0.7 配上面的"≥0.8 才进 prompt"，等于推断出的边永远进不了
  > prompt——整条无外键 JOIN 推导链在参数上就死了，且不会报错，只会静默降级成单表问答。
  > 0007 按工单备忘落的是常数 `0.7`（它只负责写库，不负责门槛），**门槛与打分的统一必须在
  > 010 开工前完成**：要么 010 改用打分公式（演示库里 `user_activity_log.user_id → user.id`
  > 按公式是 `0.35+0.25+0.15+0.15+0.10=1.00`，能过门槛），要么把门槛降到 0.7 以下。
- 环与自关联（`parent_id → id`）：`max_hops` 截断，并注明"自引用层级，注意递归在 MySQL 5.7 不可用"。
- 多路径歧义（同对表 ≥2 条等长路径，或 1↔N 二义）→ 标 `ambiguous` 并在结论里请用户澄清。
- **完全推不出路径时降级为单表问答**，在结论里明说"未能确定跨表关联，建议补充表关系后重问"，
  而不是让 AI 瞎 JOIN。

---

## 6. SSE 事件契约

### 6.1 `/chat/ask` 事件序列（前后端定死再联调）

```
meta{conversation_id,message_id}
  → retrieval{tables[],scores[]}
  → sql{raw}
  → guard{verdict,rule_id?,final_sql,limit_applied}
  → executing{}
  → result{columns[],rows[],row_count,truncated}
  → chart{spec: EChartsOption}
  → conclusion{delta}*
  → done{latency_ms,tokens{prompt,completion}}
异常路径：error{code,message,retryable}
全程穿插 `: ping` 注释帧
```

流水线视角的更细粒度帧（含阶段进度与逐 token/分批输出）：

```
event: stage         {"step":"rewrite"}
event: stage         {"step":"retrieve"}
event: retrieval     {cards:[{uid,title,score_vec,score_kw,fused}]}   ← 前端"参考资料"抽屉
event: stage         {"step":"draft"}
event: sql_token     {"text":"SEL"} …                                  ← 逐 token 出 SQL（打字机）
event: sql           {sql_raw, sql_final, guard:{ok, violations[]}}
event: stage         {"step":"validate"}                                → sql_guard（含 EXPLAIN dry-run）
event: stage         {"step":"execute"}
event: result_meta   {columns, row_count, truncated, elapsed_ms}
event: result_batch  {rows:[...]}                                       ← 分批出，首屏不等全量
event: stage         {"step":"conclude"}
event: conclusion    {text, chart_spec:{type,x,series,...}}
event: done          {message_id, usage:{prompt_tokens,...}}
event: error         {code, message, retryable}
```

顺序上的硬要求：`guard` 帧必须出现在 `executing` 之前，最后一帧必须是 `done`。

### 6.2 帧格式与传输要求

- 帧体严格 `event: <name>\ndata: <json>\n\n`；每 15s 发一次 `: ping\n\n` 注释帧，
  否则远端链路 idle 超时会静默断流，前端表现为"卡住"。
- 响应头：`media_type="text/event-stream"` + `Cache-Control: no-cache, no-transform`
  + `Connection: keep-alive` + `X-Accel-Buffering: no`。
- **不能对 SSE 挂 gzip**。`StreamingResponse` 里抛出的异常必须在 generator 内 `try/except`
  转成 `error` 帧，否则前端只看到连接断开。
- 前端**不用 `EventSource`**（无法带 `Authorization` 头），用 `fetch` + `ReadableStream` 手解帧。
- 本地开发反缓冲三件套（少一个就"卡住"）：
  ① Vite `server.compress: false`（dev 默认启用的 compression 中间件会把 SSE 攒满缓冲块才吐）
  ② 后端对 `text/event-stream` 显式排除 gzip
  ③ 15s ping。Vite proxy 还要在 `proxyRes` 上改写 `cache-control` / `x-accel-buffering` / `content-encoding`。
- 断连语义：MVP 下客户端断开 = 请求作废（不落 `chat_message`），`asyncio.CancelledError` 必须正确传导，
  不留 running 的执行、不泄漏连接。同步 job 不受影响（它在服务端跑，进度以 PG 表为准，
  重连 SSE 从 `Last-Event-ID` = job 当前 `progress` 续）。

---

## 7. API 端点表

Base：`/api/v1`（当前落地前缀为 `/api`）。鉴权：`Authorization: Bearer <access_jwt>`。
错误：HTTP 状态码 + `{"error":{"code":"DATA_SOURCE_NOT_FOUND","message":"…","detail":{}}}`，
`request_id` 走响应头 `X-Request-Id`。

| 方法 | 路径 | 鉴权 | 请求要点 | 响应要点 |
|---|---|---|---|---|
| GET | `/healthz` | 无 | — | `{status:"ok"}`（进程存活，不碰数据库） |
| GET | `/health` | admin | — | `{pg:{ok,version}, extensions:{vector:{present,creatable},pg_trgm:{...}}, llm:{ok,model}, embedding:{ok,dim}}` **含降级说明文案** |
| POST | `/auth/login` | 无 | `{username,password}` | `{access_token,refresh_token,expires_in,user}` |
| POST | `/auth/refresh` | 无(body带rt) | `{refresh_token}` | 同上，旧 jti 轮转 |
| POST | `/auth/logout` | user | `{refresh_token}` | 204 |
| GET | `/auth/me` | user | — | `{id,username,display_name,role,token_version}` |
| PATCH | `/auth/me` | user | `{display_name?,password?{old,new}}` | 200；改密 bump `token_version` |
| GET | `/users` | admin | `?q&cursor&limit&role&is_active` | `{items[],next_cursor}` |
| POST | `/users` | admin | `{username,password,role,display_name}` | 201 |
| PATCH | `/users/{id}` | admin | `{role?,is_active?,password_reset?}` | 200（禁止把自己降级/停用：service 校验） |
| GET | `/datasources` | user | `?kind&q` | **只返回有权的**：`[{id,name,kind,host,port,status,last_sync_at,access:'owner'\|'granted'\|'global',table_count}]`（as-built 006：`?kind&q` 与 `table_count` 未做——表数来自 `meta_table`，那是 007 的表；列表当前就是 `DataSourceOut` 的字段集） |
| POST | `/datasources` | user(可建) | `{name,kind,host,port,catalog_name,connect_user,connect_password,params,include_schemas,include_tables,exclude_tables,row_limit,timeout_ms}` | 201（密码即刻 Fernet 加密） |
| GET | `/datasources/{id}` | read 权 | — | 详情，`connect_password` **永不回传**（回 `password_masked:'••••'` + `has_secret:true`） |
| PATCH | `/datasources/{id}` | owner/admin | 同上 + `status` | 200（密码字段缺省=不改） |
| DELETE | `/datasources/{id}` | owner/admin | — | 软删 `deleted_at` |
| POST | `/datasources/{id}/test` | owner/admin | `{}` 或 `{"connect_password":"临时未保存口令"}` | `{ok, server_version, visible_schemas, est_table_count, table_count, view_count, grants:{read_only:bool, code:'readonly_capability_missing'\|null, warnings[]}, supports_max_execution_time:bool, latency_ms}` **← 建库前先探规模**；`table_count/view_count` 按 `include_schemas + include/exclude_tables`（源原生 LIKE）口径统计，并把探到的 `server_version` 写回 §2.2 那一列；`supports_max_execution_time` 决定 011 的超时能否由源库兜底；`grants.code` 只作机读警告，不阻断（§4.1），阻断留给 011 |
| GET | `/datasources/{id}/grants` | admin | — | `[{principal_type,principal_id,principal_name,permission}]` |
| PUT | `/datasources/{id}/grants` | admin | `{items:[{type:'user'\|'role',id?,role?,permission}]}` | 200（整体替换） |
| POST | `/datasources/{id}/sync` | sync 权 | `{"force":false}` | 202 `{job_id}`；并发冲突 409。**as-built(007)**：P2 落的是 `POST /api/sync/jobs`（`{datasource_id}` → **200 + counters**，同步执行完再回）。这一行的 202+背景执行与 `force` 覆盖是 P3 的事，届时**只换返回码不动路径**——工单 007 的拍板表里记着这条决定。行权也不是本表写的 "sync 权" 而是 **owner/admin 档**（与 DELETE/test 同一把尺）：`datasource_grants` 至今没有任何读取点（grants 那两行也未实现），"授予某人 sync 权"这句话没有落脚处；且同步会拿这个源的凭据去连库，能触发就等于能试探它的口令，宁可收窄到 owner 也不放宽到 'global'。grants 实现后按本表恢复 "sync 权" 口径 |
| GET | `/sync/jobs` | user | `?datasource_id&status&cursor` | 列表 |
| GET | `/sync/jobs/{id}` | 同 ds 权 | — | `{status,phase,progress,counters,warnings,errors,started_at,finished_at}` |
| POST | `/sync/jobs/{id}/cancel` | 同 ds 权 | — | 置 `cancel_requested`，worker 批间检查 |
| POST | `/sync/jobs/{id}/retry` | 同 ds 权 | — | 新 job，`retry_of` 指向原 |
| **GET** | `/sync/jobs/{id}/events` | 同 ds 权 | — | **SSE**（`text/event-stream`，`Retry-After`、`X-Accel-Buffering: no`）。用 GET+EventSource，前端简单；心跳 `: ping` 每 15s |
| GET | `/metadata/databases` | read 权 | `?datasource_id` | 列表 + stale 计数 |
| GET | `/metadata/tables` | read 权 | `?datasource_id&database&q&type&hidden&cursor&sort` | `[{table_uid,id,schema,name,table_type,comment_zh,comment_raw,column_count,approx_rows,is_stale,has_card}]` |
| GET | `/metadata/tables/{table_uid}` | read 权 | — | 表详情（列/索引/关系/卡片文本/最近同步） |
| PATCH | `/metadata/tables/{table_uid}` | sync 权 | `{comment_zh?,business_desc?,granularity?,is_hidden?}` | 200（**人工字段，触发卡片重建**） |
| PATCH | `/metadata/columns/{id}` | sync 权 | `{comment_zh?,business_desc?}` | 200 |
| POST | `/metadata/relations` | sync 权 | `{from_table_uid,from_column,to_table_uid,to_column}` | 201 `source_kind='manual'` |
| DELETE | `/metadata/relations/{id}` | sync 权 | — | 204（manual/inferred 可删，extracted 不可删） |
| GET | `/metadata/relations/inferred` | read 权 | `?datasource_id&min_confidence` | 待确认清单（UI 一键 accept→转 manual） |
| POST | `/metadata/purge-stale` | admin | `{datasource_id, dry_run:true}` | 预览/执行 |
| GET | `/kb/status` | read 权 | `?datasource_id` | `{active_profile, card_count, embedded_count, pending_count, last_build_at, dim}` |
| POST | `/kb/rebuild` | admin | `{datasource_id?, profile_id?}` | 202 job（复用 sync_jobs 的 card_build 阶段） |
| POST | `/kb/search` | read 权 | `{query,datasource_ids[],k=5,top_vector=80,ef_search=100,trgm_threshold=0.25,mode:'hybrid'\|'vector'\|'keyword'}` | `{items:[{card_id,table_uid,kind,title,score_vec,score_kw,fused,text_preview}],took_ms,used_profile}` |
| GET | `/kb/cards/{id}` | read 权 | — | 卡片全文 + embedding 元信息（维度/模型/建索引时间） |
| CRUD | `/kb/terms` | sync 权 | `{name,definition,table_uid?,column?}` | `kind='term'` 卡片 |
| GET | `/chat/sessions` | user | — | 自己的会话 |
| POST | `/chat/sessions` | user | `{title?,datasource_id?}` | 201 |
| PATCH/DELETE | `/chat/sessions/{id}` | owner | — | |
| GET | `/chat/sessions/{id}/messages` | owner | — | 历史消息（含 retrieved/sql/result/chart，用于刷新后完整还原） |
| **POST** | **`/chat/ask`** | read 权 | `{session_id?, datasource_id, question, history_ids?[], options:{enable_rewrite,temperature}}` | **SSE**（POST → 前端 fetch-stream） |
| POST | `/chat/messages/{id}/rerun` | owner | `{sql}` | **SSE**，同样走完整 guard（**绝不复用旧 sql_final**） |
| POST | `/chat/messages/{id}/feedback` | owner | `{verdict,note?,corrected_sql?}` | 201 |
| GET | `/chat/history` | admin | `?user_id&datasource_id&status&q&cursor` | 全量审计列表 |
| GET | `/settings/index-profiles` | admin | — | `[{id,name,model,dim,is_active,card_count,built_at}]` |
| GET | `/stats/overview` | user | — | `{datasource_count,table_count,card_count,question_count_30d,success_rate}` 首页用 |

---

## 8. 页面清单

侧栏分组：首页 Dashboard｜问数 Chat（主入口，登录后默认落地）｜数据源（列表/新建/详情 tab：概览·同步·表·权限·检索测试）｜
知识库（元数据浏览 · 术语与关系补录 · 检索预览）｜查询历史｜管理（admin：用户/系统状态/索引 profile）。

| 路由 | 组件 | 关键内容 |
|---|---|---|
| `/login` | `LoginView` | 账号密码 + 后端错误映射（锁定/密码错分文案） |
| `/` → `/chat` | `ChatView` | 左：会话列表（可 pin/重命名）；右：消息流 + 底部输入 |
| `/chat/:sessionId` | `ChatView` | URL 携带会话，刷新可还原 |
| `/datasources` | `ListView` | 名称/类型/地址/最近同步/表数/我的权限；行内"同步""测试连接" |
| `/datasources/new` | `FormDialog` | 分步：①类型+连接+**测试连接**（必须先成功）②范围（选 schema / 包含排除 / 预估表数）③限制（row_limit/timeout）④授权 |
| `/datasources/:id` | `DetailView` | tab 见上 |
| `/datasources/:id/grants` | `GrantDialog` | 左选用户/角色 → 右 permission；`allow_global` 开关 |
| `/datasources/:id/sync` | `JobListView` | 该源的 job 历史（状态/进度条/耗时/计数） |
| `/sync/jobs/:id` | `JobDetailView` | SSE 实时进度（phase 时间轴 + counters + warnings + errors 折叠）、取消、重跑 |
| `/metadata/tables?datasource_id&database` | `TableListView` | 虚拟滚动 + 搜索（表名/注释）+ 类型/隐藏/陈旧筛选 |
| `/metadata/tables/:tableUid` | `TableDetailView` | **核心页**：头部（注释可编辑 + 粒度 + 规模）；tabs：字段（列内注释可编辑、PK/索引徽标）｜索引（含列序与 sub_part）｜关系（外键列表 + 推断待确认 + 手工新增）｜建表语句｜知识卡（原文 + embedding 状态）｜"就此表提问" |
| `/knowledge/search` | `SearchView` | 输入问题 → 向量命中 / 关键词命中 / 融合结果；滑块调 `k, ef_search, trgm_threshold, mode`；每条可展开卡片全文 |
| `/knowledge/terms` | `TermView` | 术语 CRUD 表格 |
| `/history` | `HistoryView` | 个人（admin 全局）问答审计，可跳回原会话 |
| `/admin/users`, `/admin/system` | — | 用户管理 / 健康+扩展+profile（含 `CREATE EXTENSION` 失败的降级指引文案） |
| `/403` `/404` | — | |

Chat 单条 assistant 消息的分区（都是可折叠 panel，默认按阶段展开）：
阶段时间线（检索 → 生成 → 校验 → 执行 → 结论，每步耗时）→ SQL 面板（CodeMirror，只读 →
"编辑并重跑"切可编辑；底部 guard 徽标；复制/格式化/下载 .sql）→ 参考资料抽屉（分数条、命中来源 vec/kw、跳表详情）
→ 结果面板（虚拟滚动 + 类型格式化 + 导出 CSV，`truncated=true` 时顶部黄条"结果已截断，仅显示前 1000 行"）
→ 图表面板（自动选型 + 右上手动切换 line/bar/pie/scatter/table）→ 结论面板（markdown 渲染，必须经 DOMPurify）
→ 反馈条（👍/👎/采纳为示例/纠错 SQL）。

空/失败态精确区分并给下一步：`NO_SCHEMA_FOUND` → "该源未同步，点此同步"；
`GUARD_REJECTED` → 展示 violations + "点此编辑 SQL"；执行错误 → 展错误码与耗时。

---

## 9. 驱动与运行时选型

| 场景 | 选型 | 理由 |
|---|---|---|
| 应用业务库（PG） | `asyncpg` + SQLAlchemy `postgresql+asyncpg` | 最快；SQLAlchemy 官方异步方言（psycopg3 的 async 方言在 SA 2.0 不可用） |
| 源 MySQL（抽取） | **`pymysql`（同步）+ `asyncio.to_thread`** | 抽取是"一次性批量 SELECT + 逐行处理"，同步代码远比异步好写；纯 Python 零编译，Windows 稳 |
| 源 MySQL（问数执行） | **`asyncmy`** | 有 `cp312-win_amd64` wheel；`mysql+asyncmy` 方言；流式取数 + 超时控制天然走异步 |
| 源 PG（抽取） | `psycopg[binary]` 3.x（同步） | 单驱动支持 sync/async、binary wheel 免编译；DSN 与 asyncpg 不通用，抽取侧统一用 psycopg |
| 源 PG（问数执行） | psycopg3 `AsyncConnection` | 与抽取共用驱动，减依赖面 |

- **不用 SQLAlchemy 的 `inspect()` 做远端反射**：MySQL 方言没有 `get_multi_*` 批量覆写，会逐表
  `SHOW CREATE TABLE` 正则解析（2000 表 = 2000+ 次往返），且丢 `STATISTICS.CARDINALITY`、`SUB_PART`、
  `TABLE_ROWS`、`ENGINE`。因此 MySQL/PG 两侧都**手写批量 information_schema / pg_catalog SQL**
  （见 metadata-model.md）。
- `pymysql` 同时是 `asyncmy` 装不上时的兜底（`AIWEB_EXTRACT__MYSQL_DRIVER=auto|asyncmy|pymysql`），
  这不是冗余依赖。
- `sqlglot` 要锁上界并在启动自检里打印版本：守卫黑名单依赖具体 AST 节点名，跨大版本会漂移；
  升级 sqlglot 必须先跑攻击语料。

---

## 10. 规划期已核实的事实（影响架构，勿再讨论）

| 事实 | 证据 | 对方案的影响 |
|---|---|---|
| **pgvector 的 `vector` 扩展不是 trusted**（pg_trgm 是） | pgvector 0.8.6 的 `vector.control` 无 `trusted = true` | `CREATE EXTENSION vector` **必须超级用户/DBA 预建**；启动自检必须区分"缺扩展"与"无权限建扩展"两种报错文案。`pg_trgm` 可由应用自己 `CREATE EXTENSION IF NOT EXISTS` |
| **HNSW/IVFFlat 索引上限：vector 2000 维、halfvec 4000 维**（`vector` 类型本身可存 16000 维） | pgvector README | embedding 维度硬约束 ≤2000，`settings` 里 `dimension > 2000` 直接校验失败；`text-embedding-3-large`(3072) 出局，3072 维必须用 `halfvec` |
| **sqlglot 是 transpiler，不是 validator** | README："The parser is intentionally lenient…" | AST 白名单**不足以**当唯一防线，必须叠加引擎侧 EXPLAIN dry-run + 会话只读，三层独立 |
| **MySQL 5.7 的 `information_schema` 中文注释可能返回 `???`** | 5.7 IS 列走 `character_set_system_variables`（部分构建默认 utf8mb3） | 抽完首批统计 comment 中 `?` 占比 > 0.3 → warning `CHARSET_SUSPECT`，并回退 `SHOW CREATE TABLE` / `SHOW FULL COLUMNS` 逐表取注释（走表真实字符集）；连接必须 `charset='utf8mb4'`。**as-built(007)**：本机 5.7.17 实测**没有** `character_set_system_variables` 这个变量（`select @@...` 报 1193），存在的是 `character_set_system=utf8`；代表"注释到达客户端的编码"的是会话级 `character_set_results`，`ServerInfo.charset` 取它。中文注释实测正常到达，乱码回退路径因此 P2 未实现（见 metadata-model.md §10.1） |
| **Windows 上 asyncpg 与 Proactor 事件循环不兼容** | asyncpg/uvicorn 行为 | 必须用 `WindowsSelectorEventLoopPolicy`。uvicorn `--loop asyncio` 会自动设，但**自写脚本与 pytest-asyncio 不会** → `conftest.py` 与 `scripts/*.py` 顶部统一：`if sys.platform == "win32": asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())` |
| `asyncmy` 0.2.15 提供 cp312 win_amd64 wheel；SQLAlchemy 2.0 有 `mysql+asyncmy` 方言 | PyPI 文件清单 + 方言源码 | 本机 Windows + Python 3.12 可走异步 MySQL，不必退回 pymysql |
| MySQL 5.7：`max_execution_time` 系统变量 5.7.4+、`MAX_EXECUTION_TIME()` hint 5.7.8+、仅对只读 SELECT 生效；`ONLY_FULL_GROUP_BY` 默认开启 | 官方文档口径（未直连实例验证） | 进 prompt 约束与 executor 会话初始化 |
| Vite 8 要求 node `^20.19 \|\| >=22.12`；pinia 4 / vue-router 5 强制 vue `^3.5.34` | `npm view peerDependencies` | 前端版本必须成组锁，不能各升各的 |
| `passlib` 停在 1.7.4（与 bcrypt≥4.1 不兼容）、`python-jose` 长期缺维护 | PyPI | 密码哈希用 `pwdlib[argon2,bcrypt]`，JWT 用 `PyJWT` |
| 维度是列类型的一部分 | PG `ALTER TYPE` 不支持 | 换 embedding 模型必须新表/新列 + 新 profile，不能原地 ALTER |
| `HNSW` 而非 `IVFFlat` | pgvector：IVFFlat 需先有数据训练，空表建不了 | 同步是"先建空卡再灌"的流程，HNSW 可空表建，且免去 `lists` 调参 |
