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
│   │   ├── sse.py            # sse_format() + 按 `seq > cursor` 追读 `sync_job_event` 的异步生成器
│   │   │                     # + `JobWake`（as-built(P3-017)：叫醒用的是 `asyncio.Event`，置位它的
│   │   │                     # 回调住在 `services/job_queue.subscribe`；不是这一行原写的"进程内
│   │   │                     # asyncio.Queue"——一次 PG 能广播无数条 NOTIFY，而一个作业被叫醒十次
│   │   │                     # 和一次要做的事完全相同，去重语义由 Event 免费提供）
│   │   ├── sync_vocabulary.py    # phase(9 值)→stage(5 值) 的唯一翻译处（as-built(P3-017)）
│   │   │                     # 同层住在 core 而不是 services：写侧（services）与读侧（core/sse）
│   │   │                     # 都要用它，而 core 不许反向 import services。这一份只 import typing
│   │   ├── errors.py         # AppError 层级 + 统一 envelope {error:{code,message,detail}}
│   │   ├── pagination.py     # 游标/页码统一
│   │   └── logging.py        # 结构化 JSON 日志 + request_id contextvar
│   ├── models/               # SQLAlchemy 2.0 typed ORM，一文件一聚合
│   ├── schemas/              # Pydantic v2 DTO（与 models 解耦）
│   ├── services/
│   │   ├── datasource_service.py   # CRUD + Fernet + test_connection
│   │   ├── sync_service.py         # 同步编排、幂等 upsert、软删除标记
│   │   ├── job_queue.py            # `JobQueue` Protocol + PG 实现：enqueue/claim/subscribe（as-built(P3-016/017)）
│   │   ├── sync_vocabulary.py      # phase(9 值)→stage(5 值) 的唯一翻译处（as-built(P3-017)）
│   │   ├── kb_service.py           # 卡片生成（纯函数）+ 建索引 + 检索
│   │   ├── embedding_client.py     # OpenAI 兼容 /embeddings（httpx，可注入便于 mock）
│   │   ├── llm_client.py           # OpenAI 兼容 /chat/completions（含流式）
│   │   ├── chart_advisor.py        # 结果集 → 图表选型（纯函数）
│   │   ├── sql_guard.py            # ★ sqlglot AST 只读白名单
│   │   ├── source_manager.py       # 按 datasource 缓存只读 async engine/pool
│   │   └── nl2sql/
│   │       ├── pipeline.py         # 编排 ②→⑨ + 会话落库（HTTP/SSE 端点归 P8，as-built(P2-012)）
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
├── scripts/                  # init_demo_mysql.sql / init_demo_pg.sql / seed_admin.py / check_env.py / demo_ask.py
│                             # / run_worker.py（as-built(P3-016)：作业执行体）；reembed.py 归 P4，尚未落地
└── tests/                    # conftest.py + guard/ + unit/ + integration/ + fixtures/
```

关键接缝：`retriever.search()` 定义成 `Protocol`，最小链路阶段提供 `LikeRetriever`（元数据列注释
ILIKE + 按命中的不同词数排序，见 §5.1），完整版换 `HybridRetriever`，`pipeline` 只依赖 Protocol。
这样"向量检索不可用时降级回结构化路径"是生产可用的容错分支，而不是抛弃代码。

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

> **as-built(P2-013)**：这个相对值按**仓库根**解析成绝对路径，判定只有一个地方
> （`app/settings.py::_under_repo_root`，`ResultGroup.dir` 与 `KbDocsGroup.dir` 共用），
> 不再随进程 cwd 漂移。理由与现场证据见 [nl2sql-safety.md](./nl2sql-safety.md#7-结果文件下载与目录穿越防护)
> 的 as-built(P2-013)——012 之前落点跟着 cwd 走，而体检脚本按另一个根检查，两处可以互相错开都显示 ok。

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

> **as-built(P2-012 开工前拍板)**：这两处 `spec`/`EChartsOption` 是**翻译后**的形状。012 的
> `chart_advisor` 与 `chat_messages.chart_spec` 存的是 metadata-model §2.7 的自有形状
> `{type,x,series,title}`，把它变成 ECharts option 是 P8 接口层/前端的活。这样切是因为选型规则
> （§4.2）是确定性的领域判断，而 ECharts 的 option 结构是渲染细节——绑在一起的话，换图表库
> 就要动选型函数和它的单测。

**失败即早退（fail-fast）**：检索为空 → **不生成 SQL**，直接返回 `NO_SCHEMA_FOUND`，
并给"请补录表注释 / 检查权限 / 点此同步"的具体下一步。宁可拒答，不要让模型对着空 schema 编 SQL。

> **as-built(P2-013)**：下一步的**措辞分层**了，因为"点此"这个动作在 P2 根本不存在。
> 服务端 message（`NO_SCHEMA_HINT`，落 `chat_messages.error_message`）只写**动作名**——
> "请补录表注释 / 检查授权 / 触发一次同步"；具体走哪条路交给**调用方那一层**：
> CLI（`scripts/demo_ask.py::_NEXT_STEPS`）给的是今天真能执行的三条（同步是
> `POST /api/sync/jobs`，007 已交付），P8 的前端才把它渲染成"点此同步"的按钮（§7 与 ui-design.md）。
> 理由：把界面话写进服务端消息，在没有界面的这一档就成了一句指不到任何东西的空话。

> **as-built(P2-010)**：④ 这一步落地为纯函数 `services/nl2sql/prompt_builder.build_prompt()`
> （模板 `app/prompts/nl2sql_system.j2` / `nl2sql_user.j2`），四条口径是原来文档没有、施工时必须拍的：
> ① **输出契约**：user 消息末尾要求模型只回一个 JSON 对象
> `{"sql": "…", "explanation": "…", "clarify": "…"}`，不包代码围栏、不附解释文字。
> 三种畸形返回（围栏 / 答案后跟解释 / 被 `max_tokens` 截断）的兜底**归 012**，010 不设防（拍板）。
> ② **预算口径**：`AIWEB_RETRIEVAL__TOKEN_BUDGET` 管的是**素材段**（schema → 术语 → few-shot），
> 问题、输出格式与 system 里的硬约束是固定开销、不参与竞争——roadmap 那句"与 LLM max_output 之和
> 留出余量"兜的就是这部分。few-shot 另受 `FEW_SHOT_TOKEN_BUDGET` 夹一道，装不下就**整段丢弃**。
>   > **as-built(P2-010) 补刀**：`build_prompt(token_budget=None, few_shot_budget=1500)` 的默认值是
>   > **不裁切**与"镜像 `Settings` 里那个 1500"，两个参数的**真接线在 012 的装配点**——
>   > 那边漏传 `get_settings().retrieval.token_budget` 就会静默变成"素材段不限长"，
>   > 报错形式是上下文超限而不是配置缺失。**010 今天没有任何生产调用点**，
>   > 唯一把配置接上去的地方是 `tests/integration/test_prompt_live.py`。
> ③ **裁切粒度是卡片段而不是字符**：008 的主卡带着表头和全部 PK/索引/外键列，
> 所以"68 列宽表不整段塞爆"的实现是**丢切片、保主卡**；连主卡都装不下的表整张丢弃，
> 并用 `logger.info` 记下被裁的表名（roadmap **P4** 验收 6 要求"不报错但要可查"）。
>   > "保留表名与主键列"这句今天靠的是**跨片组合**：`_cut_tables` 只保证"段不被截断、主卡在 seq=1"，
>   > 主键列一定在主卡里是 008 的性质；68 列真卡片在真预算下跑 `build_prompt` 这一条**没有测试覆盖**
>   > （离线用例是手写 fixture 自带 PK 行），端到端那层归 012。
> ④ **JOIN 段只渲染候选表的直连边**，推断边按 §5.3 的 ≥0.8 门槛过滤（这条门槛今天筛的是
> 007 时代按常数 `0.7` 落库的历史行，不是新写的边；且**只管【可 JOIN】清单**，管不到卡片
> 【可关联】行里的同一张边——理由与判据见 §5.3 的 as-built）；BFS/桥表/环/不可达与"候选度>8 抑制"归工单 014。
>   > 边**按起点筛、不按终点筛**：目标表可能没被召回、也可能被预算裁掉，此时那条边仍然出现在
>   > 【可 JOIN】里。这是刻意的（外键指向谁是结构事实，删掉就查不到"为什么这条 JOIN 没了"），
>   > 代价是标题不能声称"列出来的表都能用"——模板写的是
>   > "边的目标表若没出现在【候选表】里，它只是结构提示，不要写进 SQL"，
>   > 与 system 那句"只能引用给定的表"对齐。钉住处：`tests/unit/test_prompt_builder.py::test_目标表被预算裁掉后边仍在可_join_段_但标题已声明不可引用`。
>   > **as-built(P2-014) 补刀**：本条的"只渲染直连边"已被 ③ 顶替——【可 JOIN】的**唯一来源**现在是
>   > `join_graph` 的输出（路径 + 单边提示两种形状），010 的两条口径（`inferred ≥0.8`、按起点筛）
>   > 原样搬到图侧（门槛住在 `_admissible`，一条边"能不能进图"全由它判；"按起点筛"住在 `joinable_edges`），
>   > `prompt_builder` 只剩**预算裁切后的存活判定**。
>   > 标题那句也改了口：路径可能跨桥表，"每行是一组可执行的关联条件"，见 `app/prompts/nl2sql_user.j2`。

> **as-built(P2-014)**：③ 这一步落地为 `services/nl2sql/join_graph.py`，公共表面是六个入口
> （外加两个纯函数 `build_graph`/`expand`，单测直接钉它们）：
> `load_relations`（唯一真 IO：读 `meta_relation`，按 `database_id` 卡两端、排除 `is_stale`）、
> `graph_for`（`networkx.MultiDiGraph` 建图 + 进程内缓存，键 `(datasource_id, database_id)`、
> 上限 32 条按插入顺序淘汰）、
> `expansion_for`（BFS 自写：跳数=桥表张数定上限、§5.3 权重定胜负、并列才标 `ambiguous`、
> 不可达标 `needs_cartesian`、表度 > `MAX_JOIN_DEGREE` 只禁当桥不禁当端点）、
> `structure_hints`（010 那份"候选表自己声明的边"清单的图侧版本，按起点筛、**不吃** `max_degree`）、
> `joinable_edges`（`structure_hints` 的单库内核）、`invalidate`（按库一级，没有"整源清"那一档）。
> 三条接线口径是这一片真正的风险点，各自钉了用例：
> ① **失效住在 `run_sync` 的 `finally`，不在成功分支末尾**——一个 catalog 一个事务，
> 第二个库失败回滚时第一个库的提交**不会**退回去，所以"提交过的库各清一次、回滚的那个不许清"
> 只有在 finally 里才同时成立（钉在 `tests/integration/test_sync_cache_pg.py`，
> 中间那一格的证据是响应 `status=partial` 而 `warehouse` 的键还在）。
> ② **桥表卡片由 `pipeline` 补查**，`join_graph` 保持零 IO——①~⑦ 才用手算图跑纯函数；
> 补进来的桥表**不占** `final_tables_k` 名额但一起过 `token_budget`，被裁的桥表让经过它的路径整体消失
> （`JoinLine.requires` 带路径全部 uid 就是为了这一判定，钉在 `test_prompt_builder.py`）。
> ③ **图只出结构化事实，措辞归 `pipeline`**：降级单表与歧义那两句是 `notes`，模板里
> 010 那句 `{% if not joins %}` 兜底已删——同一件事两处各写一份，改文案就要动两处。
> 已知缝（诚实记）：路径本来存在、却因桥表卡片被预算裁掉而整条消失时，"只按单表回答"那句**不会**补上，
> 兜底靠标题那句"只能引用给定的表"；不为此把 notes 也做成带 `requires` 的对象，理由见
> `pipeline._join_notes` 的 docstring。
> CLI 那头的 `{② 检索 → ③ JOIN 图 → ④ 进 prompt 的表}` 是本片新加的一行，原来的"③ 进 prompt 的表"
> 顺移成 ④（与 §4.1 的编号对齐）。
> **真链路实录（2026-09-29，真 LLM + 真演示库，`chat_messages#13`）**：
> `demo_ask.py "每个产品的订单总金额最高的前10个产品名称和金额是多少"`——
> 检索 5 张候选，图给 **10 行【可 JOIN】、0 句关联说明**，其中最长的是三跳
> （`payment_record → order_main ← order_item → product`，两张桥表正好顶满 `hops=2`），
> ④ 进 prompt 的表 6 张（比候选多出的那一张就是桥表补查：`order_main`/`order_item`/
> `product_stats_wide` 都在名单里）。模型据此写出
> `SELECT p.name, SUM(oi.qty*oi.unit_price) … FROM ai_web_demo.order_item oi JOIN ai_web_demo.product p ON p.id = oi.product_id … LIMIT 10`、
> 守卫 PASS、执行 10 行未截断、⑧ 选 bar、⑨ 结论 240 字。
> 各步耗时 `retrieve 28ms → join_graph 6ms → schema 6ms → prompt 5ms → generate 2789ms → guard 8ms
> → execute 324ms → chart 0ms → conclude 4701ms = 合计 7867ms`。
> 图这一步的查询账：一次问数取两遍图（`expansion_for` 给路径、`structure_hints` 给直连边），
> 两遍各自发一次"候选属于哪个 `(源, 库)`"的分组查询，图本体按库分键进进程内缓存——
> 所以**冷链路三次 SELECT**（分组 ×2 + 读该库 `meta_relation` ×1）、**热链路两次**（只剩两次分组查询）。
> **同一条问句的上一次运行（`#10`）⑨ 结论是空串**：上游用 200 回了空正文（`conclude 9135ms`、
> `conclusion=''`、`error_code` 为 NULL），`_message_content` 只判"是不是字符串"、空串照收，
> pipeline 因此静默落一个空结论。同一问句连跑三次（`#11`/`#12`/`#13`，结论 150/208/240 字）
> 都正常，所以那是上游抖动、不是本片接线；
> 但"空结论没有任何兜底或告警"是**真实的缺口**——012-B 认领的三档畸形返回管的是 ⑤ 的 SQL 正文，
> ⑨ 的正文形状至今没有任何工单认领，已记入工单 014 的交付记录（连同"下载端点归 P8、
> `RETENTION_DAYS` 清理作业无实现点"一起，是同一类"文档承诺了但没人认领"的洞）。
> **as-built(P3-018)**：那句"`RETENTION_DAYS` 清理作业无实现点"到此闭掉一半——
> `scripts/run_worker.py` 每轮心跳顺带按 `AIWEB_RESULT__RETENTION_DAYS`（30 天）删 `sync_job_event`
> 的旧行。**结果 csv 的回收仍然是那个洞**：这一片的已定口径把它限死在事件表上，
> 删文件不可逆，`data/results/` 里躺着 P2 验收 4/5 的现场证据（§6 as-built 第 9 条）。

> **as-built(P2-011)**：⑦ 这一步落地为 `services/nl2sql/executor.execute_readonly(...)`（async），
> 返回 `ExecutionResult(columns, rows, truncated, row_count, run_id, result_file)`。三个口径要记：
> ① **会话准备 = AUTOCOMMIT + SHOW GRANTS 硬阻断 + 四条 SET**（顺序与降级细节见 safety §4.1 的
> as-built）。执行前的 `SHOW GRANTS` 判定**不缓存**、`readonly_enforced` 列**不作阻断依据**——这两条
> 是 006 拍板过、011 沿用不重开。
> ② **取数用缓冲 `execute()+fetchall()`，不是"无缓冲 cursor + fetchmany 逐 1000"**。理由是 asyncmy
> 流式游标下 3024 会变 2013 从而废掉超时分类；内存上限由守卫输出的那条顶层 LIMIT 钉住——不变式是
> **顶层 LIMIT 恒 `<= row_limit+1`**（缺则补、超则钳；钳位半边是 2026-09-29 拍板补上的，见 safety
> §1.2 ⑦），与是否流式无关。
> 原口径的理由仍在 safety §4.1，本片的降级作为 as-built 记录，等 asyncmy 修好再回到原口径。
> ③ **错误分档三挡**：`QueryTimeout`（3024/"57014"，detail 点名 timeout_ms 配置来源）→ `ReadonlyCapabilityMissing`
> （SHOW GRANTS 判定，403）→ **原始 `DBAPIError` 不翻译**（1792/1142 是"引擎拒 ≠ 应用拒"的证据，
> 翻译了就分不清）。这三挡的验收分别钉在 `tests/integration/test_execute_live.py` 与
> `tests/unit/test_executor_timeout.py` / `test_executor_grants.py`。
> ④ **超时上限与来源键名的判定在执行器内部**（`resolve_timeout(row)`，双轴审查收口）——
> `execute_readonly` 不收 `timeout_ms`/`timeout_source` 入参，调用方无从"传话"。细节见 safety §4.1。
>
> **as-built(P2-013)**：011 那句"接口层未接线"的下半边已接。下载侧的公共入口是
> `executor.resolve_result_file(result_dir, run_id)`——纯函数，三道门（名字不合法 / realpath 逃出目录 /
> 不是文件）塌成**同一档** `ResultFileUnavailable`（404 + 同一句话，原因只进日志）。它仍不是 HTTP
> 端点：013 拍板"只做纯函数 + 单测，端点归 P8"，与 012 那次"012 不开端点"是同一个道理。细节见
> safety §7 的 as-built(P2-013)——realpath 逃逸那一门在本机**有真用例**（`os.symlink` 要特权，
> 但 junction 不要，而守卫判的是 `resolve()` 后的落点、与链接类型无关），仍缺的只是"指向文件的
> 符号链接"这一具体构造。
> **012 的接线义务清单**（缺一条就有一格验收假过）：
> ① `sql_final` 必须来自 `sql_guard.check(max_rows=row_limit)` 的返回值——绕过守卫直调会退化成
> "整结果集进内存"，因为 ② 的内存上限靠守卫那条顶层 LIMIT 兜底（不变式：`<= row_limit+1`）；
> ② `row_limit`/`max_cell_chars`/`result_dir` 从 `Settings` 取值传入（`AIWEB_QUERY__ROW_LIMIT` /
> `AIWEB_RESULT__MAX_CELL_CHARS` / `AIWEB_RESULT__DIR`），任何**请求体字段都不许**映射到这三个参数；
> `result_dir` 传进来的还必须是 `Settings` 里那个**已按仓库根解析成绝对路径**的值（见 §3.3 的
> as-built(P2-013)），调用方自己 `Path("data/results")` 再传给执行器会重新引入 cwd 依赖；
> ③ `QueryTimeout`/`ReadonlyCapabilityMissing`/`NotImplementedSource` 走全局 handler 落成对应 status
> + code，引擎级 `DBAPIError` 按现有 handler 归 `database_error`（不回显驱动原文）；
> ④（013 追加）P8 的下载端点**只能**走 `resolve_result_file` 拿路径，不许自己拼 `result_dir / f"{run_id}.csv"`
> 再 send_file——绕过它就等于把 realpath 那两道门拆了。**归属判定查出的"这不是这个用户的文件"必须落到
> 同一档 404**，不许换成 403/`Forbidden`：用状态码区分"存在但不是你的"和"不存在"，就是把 run_id 是否存在
> 做成了可查询的 oracle（safety §7 的不区分原则）。
>
> **as-built(P2-012 开工前拍板)**：工单 012 的涉及层只有 `pipeline.py` + `scripts/demo_ask.py`，
> 而本节⑦ 那句"端点归属 012"是 011 收尾时写的——两边打架，用户拍板按**工单**收口。四条结论：
> ① **012 不开任何 HTTP 端点**，`POST /api/chat/ask`（含 §6 的 SSE 契约）整块归 **P8**。理由：SSE 的
> 验收本体就是"DevTools 里逐帧推进"和"关浏览器后 `pg_stat_activity` 稳定"（roadmap P8 验收 2/4），
> 没有前端的流式端点无法真验收，只能算假绿。上面那条清单的 ③ 因此在 012 只成立一半：四类异常在
> pipeline 里以**稳定 code** 出现在返回值/上抛，HTTP status 映射等 P8 建端点时再做。
> ② **⑧⑨ 归本片**，取最小实现：`chart_advisor` 按 §4.2 做纯函数（规则口径见那里的 as-built），
> ⑨ 走第二次 LLM 调用、按本节 ⑨ 原文只喂摘要 + 前 50 行。`chat_messages.chart_spec` 存的是
> **自有形状 `{type,x,series,title}`**（metadata-model §2.7），§6 那帧里的 `spec: EChartsOption` 是
> P8 从这一形状翻译出来的，不是 012 的产物。
> ③ **chat 两表本片建**（迁移 `0006_chat`：`chat_sessions` + `chat_messages`；`chat_feedback` 归 P9）。
> 这一条推翻了工单 012 原先那句"数据层：无新增"——009 早就写着 retrieval 结果落
> `chat_messages.retrieved`，而没有这两张表时 pipeline 的可解释性就只活在 stdout 里。
> 结果行**照旧不进库**（ADR-0004），库里只有 `result_columns` / `result_stats`（含结果文件引用）。
> ④ **三种畸形 LLM 返回的兜底口径**（010 拍板归本片，见上面 §4.1 ④ as-built ①）。模型该回的是
> 单个 JSON 对象 `{"sql","explanation","clarify"}`，实际会给出三种残形状，处置分档：
> ` ```sql` 围栏、JSON 前后粘解释文字 → **可恢复**，剥围栏 / 取第一个 `{` 到最后一个 `}` 再解析；
> `max_tokens` 截断（大括号不闭合）→ **不猜**，直接 `llm_bad_response` 早退。理由与守卫的
> "改一个字符就换一棵 AST"同一条：半截 SQL 补全出来的东西没人能证明模型本来想说什么，
> 而这条链路的失败成本是执行一条没人授权的语句。`clarify` 非空 → 不执行，本轮就是追问。
>
> **as-built(P2-012 施工后，2026-09-29)**：上面那四条按拍板落地，另有五处施工现实要记下来：
> ① **步名清单以代码为准**：`retrieve → schema → prompt → generate → guard → execute → chart → conclude`
> 八格（① 登录、⑩ 返回不是 pipeline 的步）。每格是**增量**耗时，合计写进 `latency_ms`；
> `result_stats.elapsed_ms` 单独记 `execute` 那一步，两列分开才答得出"慢在模型还是慢在源库"。
> **逐步耗时目前只在 stdout**（`AskOutcome.steps` 是它的载体，库里没有这一格）——
> 把它端给界面是 P8 的活，而 §6 那条 `done` 帧现在只写着 `latency_ms`，**没有逐步那一段**。
> 所以这是一个"要不要扩这一帧"的待决项，不是"已经存了、只是没显示"。
> ② **三类早退不留假步**：检索为空 / 畸形返回 / 守卫拒绝各自在其之后的步就断掉了，
> 补 chart/conclude 两个 0ms 会把"没跑"渲染成"跑完但没结果"。
> ③ **终局留痕写在 `finally`，但那一次 commit 不许顶掉真故障**：元数据库自身就是故障源时
> （连接断、超时、schema 被删），失败的 commit 会抛 `PendingRollbackError` 盖掉 `QueryTimeout`，
> 调用方拿到一个与现场无关的码。所以留痕失败只 `logger.exception`，`AskOutcome.message_id` 留 `None`
> （这就是它为什么不是 `int`——`0` 会被读成"有一行 id=0 的记录"，而它不存在）。
> ④ **`result_columns` 只有 `name`**：§2.7 称其为"列定义"，但行出 executor 时已过 `serialize_cell`，
> `Decimal`/`datetime` 的源库类型在那一刻就丢了。填不进列的东西不在列里写。
> ⑤ **模型听话地写了 `LIMIT` 的那一支，守卫当时不补探针**，`truncated` 因此恒假——这不是本片能改的
> （动的是守卫裁决语义），口径与钉子见 nl2sql-safety §9.2 ⑧。**同日拍板闭环（2026-09-29）**：
> 守卫改成"钳位 + 探针"（§1.2 ⑦），撞上限的那一支 `truncated` 恢复可用，验收里"`sql_final` 是
> 重生成后的版本、与 `sql_raw` 不同"也不再**只在模型漏写 LIMIT 时成立**；两种形状仍各有一条用例站着。
> **闭环只到"撞上限"为止**：模型自己写 `LIMIT n`（`n < max_rows`）时没有探针、`truncated` 恒假，
> 那是有意的语义（上限没碰到就谈不上截断）。

### 4.2 图表选型规则（确定性，可单测）

1. 1 行 1 列 → `kpi`（大数字卡）。
2. 首列是日期/时间/整数序数（distinct 占比 > 0.6）+ ≥1 数值列 → 单数值列用 **line**（时间）/ **bar**（离散类别）；
   多数值列且列名同族（`2024-01`、`2024-02`… 或 `_sum` 后缀）→ 转长表后 **stacked bar** / **multi-line**。
3. 首列低基数文本（**NDV ≤ 12** 且非时间）+ 1 数值 → **bar**；合计≈100 或列名含 ratio/pct/share/rate → **pie**（series ≤6，其余归"其他"）。
4. 2 个数值列、无类别列、行数 ≥ 30 → **scatter**。
5. 列数 ≥ 8 或 distinct/rows > 0.9 → **table**（不画图）。
6. 一律给 `fallback='table'`，UI 提供手动切换 tab —— 图表类型判断永远会错，逃生门必须留。

> **as-built(P2-012 开工前拍板)**：这四条今天由 012 落成真代码（`services/chart_advisor.py`，纯函数），
> 三处口径是拍出来的，改任何一条都要同时动 verification §2.1 与 roadmap P8 验收 5：
> ① **类别阈值是 12 不是 20**——原文写 `distinct ≤ 20`，而 verification §2.1 与 roadmap P8 验收 5
> 两处都写 NDV≤12（两处一致、本处孤证），按 12 收口。**>12 时仍然画 bar，但只取 top10 + "其他"**
> （这条原本只写在 verification 里，现在两边各有对方缺的规则，一并补齐）。
> ② 补两条 verification 有、这里没有的下线条件：**全 NULL 的候选列不参与选型**（只能 table）、
> **行数 > 200 不画图**（点太密，图比表更难读）。
> ③ **`type` 的字面值就是 §6 存储里那一个**：`kpi` / `line` / `bar` / `pie` / `scatter` / `table`。
> verification §2.1 那行写的"单行单值→number"是同一样东西的旧名，已统一成 `kpi`——
> golden 快照锁字面，两个名字会直接变成对不上的断言。
>
> **as-built(P2-012 施工后，2026-09-29)**：实现时又撞出两条下线条件，都是"形状在此输入下无定义"
> 而不是"保守起见"：
> ⑦ **列名重复 → table**。`SELECT SUM(a) AS 金额, SUM(b) AS 金额` 是合法 SQL，而 `ChartSpec` 的
> `x`/`series` 存的是**列名**（§2.7）——`x="金额"` 指第几列没有答案。取值一律按**下标**
> （`numeric_column_slots` 回 `(下标, 列名)`），因为 `columns.index("金额")` 只会回到第一次出现，
> 摘要表里那一行的 sum/min/max 全是错值且错得看不出来。
> ⑧ **非有限值不算数值**。`float()` 认 `NaN`/`Infinity` 两个字面量，而 `serialize_cell` 对
> int/float/str **原样透传**，所以两者都到得了这里。认它作数值的话，一格 `NaN` 把整列合计毒成
> `NaN`，"合计≈100 判饼图"和摘要的 sum/avg 全废——废的形式是"那一格照样显示成数字"，看不出来。

---

## 5. 检索阶梯与可插拔的向量增强

### 5.1 四层阶梯（主链路）

问数链路对"找到相关表"这件事是**分层降级**的，任何一层拿不准就落到下一层：

| 层 | 手段 | 依赖 | 说明 |
|---|---|---|---|
| **L1 结构化关键词** | 在元数据库上对表名/列名/`comment_raw`/`comment_zh`/业务描述做精确、前缀、ILIKE、pg_trgm 匹配，并按**命中的不同词数**排序（次键：命中列数） | 只需 `pg_trgm`（trusted，应用可自建） | 永不下线的主链路；`LikeRetriever` 即其最小实现 |
| **L2 LLM 目录摘要选表** | 把候选表目录（表名 + 一行注释）压成 digest 交给 LLM 直接挑表 | 只需 LLM | 超过 `AIWEB_RETRIEVAL__CATALOG_DIGEST_MAX_TABLES`（默认 1000）时，**先按关键词预筛**再交给模型选表，避免目录本身打爆 prompt |
| **L3 完整卡片抓取** | 命中的表取其**表级知识卡片全文**（字段、枚举取值、索引、关系、方言约束）进 prompt | 元数据快照 + 卡片构建 | 卡片模板见 kb-workflow.md；宽表按列切片 |
| **L4 `NO_SCHEMA_FOUND`** | 前三层都拿不到足够证据时明确拒答 | — | 早退，给补录/授权/同步的可执行下一步 |

> **as-built(P2-0009)：L1 的排序主键从"命中列数"改成"命中的不同词数"。**
> 原口径在演示库真语料上被打穿了：68 列的 `product_stats_wide` 有 10 列的注释都带 `金额`
> （`refund_amt_*`、`return_rate_*` 两族同名列），命中列数是全场最高的，就把只对上一个词的它
> 排到了只对上 `订单`+`金额` 两个词、5 列的 `order_main` 前面，roadmap §P2 验收 ① 当场破。
> 一个词在一族同名列上重复命中是一份证据，不是十份；两个不同的词各自命中才是两份。
> 所以排序键定为 **（命中的不同词数 ↓，命中列数 ↓，`table_uid` ↑）**，第三键保证并列不抖。
>
> 两个连带后果，都写在明处：
> 1. **P2 的 `score_kw` 只是展示字段，不参与排序**。关键词路的分数是"每个词取 ILIKE/trgm 的最大值
>    再求和"，它反映"这张卡的文本和问句有多像"，而表次序现在完全由两个计数决定。
>    同一 (词数, 列数) 下 trgm 分高者不再靠前——这是 P2 的已知取舍，P4 的 RRF（(3)）会把分数
>    重新变成次序本身。
> 2. **词数按"不同的词"去重，而 P2 的切词是 2-gram 近似**（见 kb-workflow.md §9），
>    同族滑窗词（`订单`/`单金`/`金额`）会一起把计数抬上去。同一族词几乎总是成组出现，
>    所以表与表的相对次序仍稳；接进真分词器（pinyin/中文 tokenizer）后这个口径自动变准。
> 3. **本行列出的五个匹配面，P2 只有三个进"命中计数"**。计数面 = 列名 / `comment_raw` / `comment_zh`；
>    表名与表注释只进得来**召回**（它们就在卡片正文里，决定这张卡在不在候选集、影响 `score_kw`），
>    不进计数——计数是要给界面说出"是**哪一列**对上的"，表级命中给不出这一句。
>    `business_desc` 在 P2 三头皆空：没有写入点（`.knowledge/` overlay 导入与两个 PATCH 端点都在 P3）、
>    不进 `search_text`、也不进计数面。人工知识一片落地时它同时补上这三处，L1 的匹配面才与本行齐平。

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

as-built(P2-0009)，P2 只跑 (2)(4)(5) 这三步，其余步骤的位置留着不接：

- **(2c) `simple` FTS 这一路 P2 不接**。PG 没有中文分词器，`simple` 配置把整串中文当一个 lexeme，
  对 L1 等于零贡献；接上只会让 SQL 多一个从不命中的分支。(2a) ILIKE 与 (2b) `similarity()`
  两路取 max，仍是本节说的"三路 max"的退化形态。
- **(4) 聚合到表**：分数是"每个词取该卡上的最大值、再跨词求和"，同表多卡按 `boost=0.05×(n-1)`
  叠加；`table_columns` 切片卡与主卡同 `table_id`，不各自占名额。表次序见上面 §5.1 那条注。
- **(5) 权限过滤在 SQL 里就做完**（`datasource_id = ANY(有权源)`），不是"先全召回再筛"——
  召回窗口只有 80 张卡，先筛才有意义。预览端点在这一层之外多一道门：问句**点名**了某个源而
  当前用户无权时直接 403，而不是静默少一张表（静默过滤留着给 012 的自动链路，理由不同）。

### 5.3 JOIN 路径推导

- 边权重：`manual` = 0.1、`extracted` = 0.5、`inferred` = 1/confidence；两两 targets 求最短简单路径后取并集。
  > **as-built(P2-0010)**：本条与下面"环与自关联""多路径歧义""完全推不出路径时降级"三条是
  > **一整块没人实现的 JOIN 图**（全仓 grep 不到 `join_graph`/BFS，而 verification §2.1 把它当成
  > 有 6 条单测的单元接缝）。010 只把**候选表之间已有的边**渲染进 prompt 的【JOIN】段
  > （extracted/manual 全收，inferred 过 ≥0.8），图算法整块归**工单 014**（JOIN 图切片）。
  >
  > **拍板(P2-0014 开工前，2026-09-29)：本条的"最短"是两件事叠出来的，不是一种**——
  > `hops` 数的是**桥表张数**（跳数上限，roadmap 分组 9 那句"JOIN 桥表扩展深度"就是这个意思），
  > 权重只在**同一跳数内**定胜负。两条各自会失败：只按权重跑 Dijkstra，`hops` 截的就成了
  > "权重预算"而不是表数（一张 `inferred` 边权重 1.0，两条 `manual` 才 0.2，为省 0.05 多绕两张桥表
  > 是合法解）；只按跳数跑，`manual` 优先于 `inferred` 这条承诺就没人读了。
  > 跳数并列**且权重也并列**时才是 §5.3 末"多路径歧义"的 `ambiguous`；权重能分胜负就不算歧义。
  >
  > **拍板(P2-0014)：图存方向、扩展按无向**。真实 FK 的方向恒为子→父，
  > `payment_record → order_main ← order_item → product` 这条链必须沿一条反向边走；
  > 若按有向可达，演示库里除了"子表→父表"这一跳以外一律不可达，桥表扩展整块空转。
  > `MultiDiGraph` 保留方向的唯一用途是渲染 `ON` 子句时知道哪列等哪列。
  > 跨库/跨源不合并建图（缓存键即 `(datasource_id, database_id)`），跨库的那一对表走下面"推不出路径"
  > 那一条，与"不做跨数据源建图"同口径。

- 无外键的分析库（现实常态）：按命名约定推断 `t1.c → t2.c'`（列名同、`t2` 的 PK 是该列、类型族兼容），
  打分 `0.35 + 0.25[一侧是PK] + 0.15[类型完全相同] + 0.15[后缀 _id/_no/_code 且前缀与 t2 主名词匹配] + 0.10[t2 表名是 c 去后缀的单/复数变体]`，
  写入 `meta_relation(source_kind='inferred', confidence)`，**只有 ≥0.8 才进 prompt**，且带 `[推断,置信 0.87]` 标签。
  候选度 > 8 的字段（如 `org_id` 出现在 40 张表）需另一端有唯一约束，否则丢弃并 warning（抑制组合爆炸）。
  > **拍板(P2-0014 开工前)：这句的"否则丢弃"在现行减法规则下不可达，写侧改为"只告警、不丢边"**。
  > 判据来自 `infer_relations()` 自己的前置条件：能落库的推断边，目标端按构造必是**单列主键**
  > （MySQL 抽取里 PRIMARY 索引 `NON_UNIQUE=0`，PK 列恒 `is_unique=True`），所以"另一端没有唯一约束"
  > 这个分支永远走不到；而 `org_id` 出现在 40 张表时，每张表各自推出的是 `→ org.id` **一条**边，
  > 不存在 40 个候选互相打架。为了"丢弃"那半句去放宽减法规则（改成 PK 或任一唯一索引列都算）
  > 等于凭空造一条文档没要求的连接能力。
  > 落地：`relation_infer.py` 加**平行**纯函数 `high_degree_fields()`（`infer_relations` 签名一字不动），
  > 告警走现成的 `sync_jobs.warnings` 通道，用户在"同步"结果里看得见"这个名词太泛"；
  > 真正拦组合爆炸的是图侧的表度抑制（下面"环与自关联"那条 as-built）。
  > 门槛常量两侧共用一键 `AIWEB_RETRIEVAL__MAX_JOIN_DEGREE=8`（§5.3 原文的"8"是同一个数字）。

  > **as-built(P2-0010)：本节开头的加权公式与 roadmap §P4、verification §2.1 的"命名约定 = 0.7"
  > 曾是互斥的两套口径**（常数 0.7 配"≥0.8 才进 prompt"，等于推断边永远进不了 prompt，
  > 且不报错、只静默降级成单表问答）。0007 施工时发现，挂在这里等拍板；**2026-09-28 由用户拍板闭环**：
  > 公式落地在写侧（`services/relation_infer.py` 算 confidence），门槛保持 ≥0.8 不改，
  > 常数 `0.7` 降格为**公式的下限钳位**（实现是 `round(max(加权和, 0.7), 2)`：任何低于 0.7 的
  > 加权和都被抬到 0.7，而不是"一项加成都拿不到时才算出来的那一档"——单拿"类型完全相同"是
  > 0.5、单拿"目标是 PK"是 0.6，两者落库后同为 0.7，**分数不再可逆推出加了哪几档**。
  > 这三档全在 0.8 门槛之下、一条都进不了 prompt，所以钳位与"退化值"两种读法**行为上等价**，
  > 但读文档时别把它当成可反推的证据）。roadmap §P4 与 verification §2.1 那两处"0.7"同步改掉。
  >
  > **门槛的管辖范围（2026-09-28 双轴审查后由用户拍板收窄）**：`≥0.8` 筛的是 prompt 里
  > **【可 JOIN】这份"本次可用于跨表的边清单"**，不是"prompt 里不许出现这条边"。
  > 同一张低置信边仍会出现在卡片全文的【可关联】行里（008 的 `card_template.j2` 渲染关系段时
  > 不看 confidence，且 010 的口径是"卡片原文进 prompt、不在装配层二次加工"），
  > 那里它带着 `[推断,置信 0.7]` 字样，正是"这条边为什么没被拿去 JOIN"的诊断线索。
  > 判据：能被 JOIN 的是【可 JOIN】列出的那些；卡片里的【可关联】是结构描述，不是授权。
  >
  > 落地后要认一个事实：`infer_relations()` 的前置条件已经把"目标列是该表单列主键""后缀属
  > `_id/_no/_code`""表名是去后缀的单/复数变体"三项做成了**恒真**，所以公式实际只剩
  > "类型完全相同"这一档加成 —— **confidence 只会是 0.85 或 1.00，门槛 ≥0.8 因此不筛掉任何一条边**。
  > 它不是判别器，只是形式上过了线。真正拦噪声的是本节末"候选度 > 8 需另一端有唯一约束"那条抑制，
  > 而它需要把索引原料喂进推断函数（现在的签名只有 `columns` + `foreign_keys`），**至今没实现**。
  > 演示库踩不到（`ai_web_demo` 没有 40 表共用同名列），所以这是一笔**已识别的欠账**，
  > 与 §5.3 的 BFS/桥表/环/不可达那一整块一起归工单 014（JOIN 图切片），不在 010 范围内。
  > **as-built(P2-0014)：这笔账已结，但结法不是"喂索引原料"** —— 见上面那条拍板：
  > 丢弃分支在现行减法下不可达，写侧只出告警（因此**不需要**索引原料），抑制主体搬到图侧的
  > 表度门槛 `AIWEB_RETRIEVAL__MAX_JOIN_DEGREE=8`，两侧共用这一个键。
  >
  > 另：本节的算例原写 `user_activity_log.user_id → user.id`，**演示库里没有 `user` 表**
  > （真表是 `customer`，`user_id` 因此推不出边——0007 验收 4 就钉着这件事）。
  > 现成的真算例是 `user_activity_log.product_id → product.id`：两侧都是 `INT`，
  > `0.35+0.25+0.15+0.15+0.10 = 1.00`。
- 环与自关联（`parent_id → id`）：`max_hops` 截断，并注明"自引用层级，注意递归在 MySQL 5.7 不可用"。
  > **as-built(P2-0014)**：本条与下面两条的**执行者**定死了——`join_graph` 只输出结构化事实
  > （`paths` / `ambiguous` / `needs_cartesian`），**不写一句给人看的话**；措辞与"要不要因此换一条回答分支"
  > 归 `pipeline`。三条理由：文案住在逻辑层会让改一句话去动图算法文件；"降级为单表问答"是**编排**决定，
  > 图无权决定流程；而工单 014 的"涉及层"原写"接口层：无"，接线既已拍板归本片，那一格改为 pipeline 一格。
  > 同一条 as-built 还钉住**表度抑制的对象**：`MAX_JOIN_DEGREE` 只禁一张表**当桥表**（BFS 不穿过它），
  > 不禁它**当端点**——候选表里有它时直连边照旧进【可 JOIN】。组合爆炸发生在扩展，不发生在直连；
  > 把端点也禁掉等于惩罚用户点名要问的那张表，且与卡片【可关联】行（008 渲染器不看度）说法相冲。
- 多路径歧义（同对表 ≥2 条等长路径，或 1↔N 二义）→ 标 `ambiguous` 并在结论里请用户澄清。
- **完全推不出路径时降级为单表问答**，在结论里明说"未能确定跨表关联，建议补充表关系后重问"，
  而不是让 AI 瞎 JOIN。
  > **as-built(P2-0014)：桥表进 prompt 的两条义务**。① 桥表**不占**检索的 `final_tables_k` 名额
  > （否则一次 `hops=2` 扩展能把召回排第一的表顶掉，问数质量被图算法抢跑），但**一起过** `token_budget`
  > 裁切；② 路径是"可执行"承诺，与 010 已定的单边口径不同——010 允许【可 JOIN】里出现目标表未给的
  > 边（那只是结构提示，标题已写明），而**被预算裁掉的桥表必须让经过它的路径整体从【可 JOIN】消失**，
  > 否则就是要求模型 JOIN 一张没给卡片的表，正面违反 010 钉死的"只能引用给定的表"。
  > 桥表的**卡片**由 `pipeline` 补查（`join_graph` 保持零 IO，六条验收才能用手算图跑纯函数）。
  >
  > **as-built(P2-0014)：建图侧的三条口径**（都只有读代码的人看得懂、但改代码的人会踩）：
  > ① **自环不进图**（`_admissible` 挡在 `build_graph` 之前，不是进图后再特殊对待）。`MultiDiGraph`
  > 上一条 `A→A` 的自环会让"无向邻居"里 `A` 出现两次，`_bridge_banned` 的度因此多算 1；
  > 而自环本来就不跨表，卡片【可关联】行仍由 008 的渲染器直接读 `meta_relation` 给出，不经本模块。
  > ② **门槛只有一处**：一条边"能不能进图"全由 `_admissible` 判（自环、`inferred` 且缺分或
  > 低于 0.8 的都进不来），判定住在 `build_graph` 里——`load_relations` 只负责把行读出来、
  > 不带任何策略，改门槛不必碰 SQL。路径、结构提示、表度门槛这些"图建好之后的东西"因此
  > 一律不再复查 confidence：门槛散在两处迟早分叉，而 010 的双轴审查已经在收它的辖区
  > （只管【可 JOIN】，卡片【可关联】那一半不管）。
  > ③ **平行边按 `(source_kind, from_column, to_column)` 存**，与 `uq_meta_relation_key` 同档：
  > 同一对列上 `manual` 与 `extracted` 可以并存（那条唯一约束认的就是这三档），旧 key 少了
  > `source_kind`，后加入的一条会**原地覆盖**前一条——不是多留一条边，而是属性被换掉。
  > 并存之后由 `_adjacency` 取权重最小者，权重并列时**先插入的赢**，而插入顺序由
  > `_relations_statement` 的 `ORDER BY`（补了 `source_kind` 才是全序）决定，不看存储引擎的心情。


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
  **as-built(P3-017)**：带游标的那一类帧多一行**前置** `id: <seq>`（形状 `id:…\nevent:…\ndata:…\n\n`）——
  浏览器/`fetch` 解帧后把它存成 `Last-Event-ID`，§2.8 的 `seq > cursor` 补读才有地方接。
  这一行原来写的"严格两行"漏了它，因为写的时候只有问数流（无游标）用到这一节。
- 响应头：`media_type="text/event-stream"` + `Cache-Control: no-cache, no-transform`
  + `Connection: keep-alive` + `X-Accel-Buffering: no`。
  **as-built(P3-017)**：`Connection` 这一项**没设**，是有意偏离而不是漏做——它是 hop-by-hop 头，
  HTTP/1.1 的 keep-alive 本来就是默认，而 uvicorn/h11 会剥掉应用自己写的 `Connection`；
  真设一次只会在现场多一条"为什么这行代码没用"。SSE 断线重连的间隔也不走 `Retry-After`
  （那是 HTTP 头的机制），走**帧字段** `retry: <毫秒>`：流的第一个块就是它，常量为 3000。
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
  **as-built(P3-017)**：这一行原写的 "`Last-Event-ID` = 当前 `progress`" 落不了地——`progress` 是
  `numeric(5,2)` 的百分比，回不到"读到第几行"。游标是 `sync_job_event.seq`（§2.8 的 `UNIQUE (job_id,seq)`），
  帧里以 `id:` 送出、重连时从 `Last-Event-ID` 头或 `?cursor=` 读回，**后者优先**（URL 是显式的，
  头是浏览器代管的）。问数流那一半按原文不变：它没有事件表，也就没有游标。

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
| POST | `/datasources/{id}/test` | owner/admin | `{}` 或 `{"connect_password":"临时未保存口令"}` | `{ok, server_version, visible_schemas, est_table_count, table_count, view_count, grants:{read_only:bool, code:'readonly_capability_missing'\|null, warnings[]}, supports_max_execution_time:bool, latency_ms}` **← 建库前先探规模**；`table_count/view_count` 按 `include_schemas + include/exclude_tables`（源原生 LIKE）口径统计，并把探到的 `server_version` 写回 §2.2 那一列；`supports_max_execution_time` 决定 011 的超时能否由源库兜底；`grants.code` 在**登记阶段**只作机读警告、不阻断（§4.1，admin 可确认），011 起在**执行阶段**硬阻断（每次问数前重探 `SHOW GRANTS`，结论不缓存，详见 safety §4.1 as-built） |
| GET | `/datasources/{id}/grants` | admin | — | `[{principal_type,principal_id,principal_name,permission}]` |
| PUT | `/datasources/{id}/grants` | admin | `{items:[{type:'user'\|'role',id?,role?,permission}]}` | 200（整体替换） |
| POST | `/datasources/{id}/sync` | sync 权 | `{"force":false}` | 202 `{job_id}`；并发冲突 409。**as-built(007)**：P2 落的是 `POST /api/sync/jobs`（`{datasource_id}` → **200 + counters**，同步执行完再回）。这一行的 202+背景执行与 `force` 覆盖是 P3 的事，届时**只换返回码不动路径**——工单 007 的拍板表里记着这条决定。行权也不是本表写的 "sync 权" 而是 **owner/admin 档**（与 DELETE/test 同一把尺）：`datasource_grants` 至今没有任何读取点（grants 那两行也未实现），"授予某人 sync 权"这句话没有落脚处；且同步会拿这个源的凭据去连库，能触发就等于能试探它的口令，宁可收窄到 owner 也不放宽到 'global'。grants 实现后按本表恢复 "sync 权" 口径。**as-built(P3 开工前拍板)**：P3 落 202 + `{job_id}` 与
`{"force":true}` 覆盖上限，但**路径保持 P2 的 `POST /api/sync/jobs`**（007 拍板"只换返回码不动路径"），
执行体是独立 worker 进程而非请求内跑完，理由与后果见 ADR-0011。**as-built(P3-016 已交付)**：202 +
`{job_id}` 与 worker 骨架落地（`app/services/job_queue.py` 的 `enqueue`/`claim` + `scripts/run_worker.py`，
本机另开 `dev.ps1 worker`），终局只能从 `GET /sync/jobs/{id}` 那一行读、而它还没实现（下面两行仍挂无主账），
所以 `sync_jobs.errors[].data` 成了结构化出路到前端的唯一一跳。**as-built(P3-021 已交付)**：
`force` 这半接通——请求体收 `{"force":true}`（strict 布尔，非布尔 422），`enqueue` 落
`sync_jobs.force`，`claim` 经 `ClaimedJob.force` 随行带回，`run_worker` 透传 `run_sync(force=)`，
真时不传 `MAX_TABLES`（只此一档，scope 过滤与删除差分不变）。实装形状是**请求体**而
§6 文案字面是 `?force=true`：不为此开查询参数第二条入口（两条通道并存是本项目禁止的），
措辞统一归 025 收口；`SyncJobAccepted` 仍只回 `{job_id}` |
| GET | `/sync/jobs` | user | `?datasource_id&status&cursor` | 列表 |
| GET | `/sync/jobs/{id}` | 同 ds 权 | — | `{status,phase,progress,counters,warnings,errors,started_at,finished_at}` |
| POST | `/sync/jobs/{id}/cancel` | 同 ds 权 | — | 置 `cancel_requested`，worker 批间检查。**as-built(P3 开工前拍板)：挂 [P8]** —— `sync_jobs` 至今没有 `cancel_requested` 列（metadata-model §2.5 那份列清单里没有），加列与批间检查点等 P8 前端的"停止同步"按钮一起做；`status='cancelled'` 因此继续没有写入点 |
| POST | `/sync/jobs/{id}/retry` | 同 ds 权 | — | 新 job，`retry_of` 指向原。**as-built(P3 开工前拍板)：本片不做** —— `retry_of` 列同样不存在，且 P3 七条验收里没有一条要求重跑；记为无主项（见 `project-spec-holes` 那一类账） |
| **GET** | `/sync/jobs/{id}/events` | 同 ds 权 | — | **SSE**（`text/event-stream`，`Retry-After`、`X-Accel-Buffering: no`）。用 GET+EventSource，前端简单；心跳 `: ping` 每 15s。**as-built(P3 开工前拍板)**：事件真相是 metadata-model §2.8 的 `sync_job_event`（append-only，带 `seq` 游标），PG 的 `NOTIFY` 只携带 `job_id` 当叫醒信号、不带任何事实（`NOTIFY` 在无监听者的那一刻永久丢失，所以它不配当真相，详见 ADR-0010）；客户端断开重连按 `seq > cursor` 补读，一条不丢。**as-built(P3-017 已交付)**：路径就是 `GET /api/sync/jobs/{job_id}/events`，鉴权与 `POST /api/sync/jobs` 同一把尺（owner/admin，不是本表的 "sync 权"，理由同上面那行）。块顺序 = `retry: 3000` → 若干 `event: progress`（带 `id: <seq>`）→ 一个 `event: done`（**无 `id`**，负载是 `GET /sync/jobs/{id}` 那一行的字段集 + 算出来的 `duration_ms`）；异常走 `event: error {code,message}`（§6.2 那条"生成器里的异常必须转成帧"。**as-built(P3-017 双轴审查后)**：`AppError` 之外的那一类（元数据库断线、收尾那一瞬作业行被 CASCADE 删掉）也转同一帧，`code=internal_error`，而驱动原文只进服务日志不进帧——同 `errors.py` 里 SQLAlchemyError 处理器一条理由：原文带连接串）。进度帧的负载是**摊平**的：`{stage, phase, done, total, base_table, view, cards, payload}`——本表与 spec 故事 7 点的那五个键在顶层，多出的三个是 §2.8 那两列（`phase` 排障、`counters.cards`、`payload`）原样带出，展开的位置只有 `core/sse.progress_frame()` 一处。`?cursor=<seq>` 与 `Last-Event-ID` 头二选一时**查询参数优先**。心跳与重连的机制见 §6.2 的 as-built |
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
| POST | `/kb/search` | read 权 | `{query,datasource_ids[],k=5,top_vector=80,ef_search=100,trgm_threshold=0.25,mode:'hybrid'\|'vector'\|'keyword'}` | `{items:[{card_id,table_uid,kind,title,score_vec,score_kw,fused,text_preview}],took_ms,used_profile}` **← 目标形态**。**as-built(0009)**：P2 这颗只实现关键词路，请求侧只认 `{query,datasource_ids[],k}`（`query` 1..200 字、`k` 1..50；`top_vector/ef_search/trgm_threshold/mode` 传了直接 **422**（`extra="forbid"`——P2 不认这些旋钮，静默忽略等于假装它能调，前端只会得到"我传了 vector 怎么结果没变"这种查不出来的错），`trgm_threshold` 一律取配置值）；响应 `items` 是**表粒度**不是卡粒度（§5.2 (4)），每项 = `{table_uid,title,score_kw,matched_term_count,matched_column_count,hits:[{column_name,field,term}],cards:[{card_id,kind,seq,score,text_preview}]}` + 顶层 `took_ms`。`score_vec/fused/used_profile` **P2 不给**——向量路没接，给了就是编的；`text_preview` 是卡片正文前 200 字（预览端点给人眼判断用，010 拼 prompt 用的是 `cards` 里带回的完整段）。边界口径：点名的源无 read 权 → **403**（不是静默少一张表，理由见 §5.2 (5) 那条注）；不点名 → 在该用户看得见的所有源里找；零命中 → `items:[]`；`query` 空或超长 → 422；带未知字段（含那四个向量旋钮）→ 422；未登录 → 401。 |
| GET | `/kb/cards?table_uid=` | read 权 | `?table_uid` | **as-built(0008)**：一张表的全部卡片段（主卡 `seq=0` 在前，宽表再带 `table_columns` 切片），每项 = 卡片全文 + embedding 元信息（`index_profile:{id,name,model,dimensions,card_template_version}` 与 `embedded_at`，后者 NULL = 还没向量化）。本行原写的 `/kb/cards/{id}`（按单张卡 id 取）**未实现、也不打算实现**：一张表切几段只有服务端按 §6 的策略算得出，要前端先知道段数才能取全素材是倒置的依赖。边界口径：表存在而零张卡 → `[]`（"同步跑过、卡片还没建"是真实中间态，不是"表不存在"）；`table_uid` 找不到 → 404；无 read 权 → 403。 |
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

> **as-built(P3 切工时证实)**：上表有两格与本机现状不符，动 PG 抽取（工单 024）前先看这段。
> ① "应用业务库（PG）"那格里那句括号"psycopg3 的 async 方言在 SA 2.0 不可用"**已过时**——本机 SQLAlchemy 2.0.54 的
> `dialects/postgresql/psycopg.py` 模块文档里就写着 `create_async_engine("postgresql+psycopg://…")`
> 的用法，并且定义了 `AsyncAdapt_psycopg`。元数据库这一格**继续用 `asyncpg` 不改**（001 起所有开通
> SQL、迁移与用例都对着 asyncpg 验过，换驱动零收益）。
> ② "源 PG（抽取）"那格"用 psycopg 同步"至今**没落地**：`psycopg` 根本不在依赖里
> （`uv run python -c "import psycopg"` → `ModuleNotFoundError`），而 `source_manager.py:23` 早已把
> `kind='postgres'` 映射到 `postgresql+psycopg`——今天登记一个 PG 源、点测试连接就会在方言加载处失败
> （实机复现：`create_async_engine('postgresql+psycopg://…')` → `ModuleNotFoundError: No module
> named 'psycopg'`）。024 据此选路：补 `psycopg[binary]` 依赖、走异步方言，那一格的"同步 + to_thread"
> 口径作废，届时回写本表。
>
> **as-built(P3-024)：上面 ② 末句的选路（"走异步方言、同步口径作废"）已被推翻，落地的是同步。**
> 依赖那半照做了（`psycopg[binary]` 3.3.6 进了 `pyproject.toml`，`import psycopg` 不再 ModuleNotFound，
> 于是 `source_manager` 那条 `postgresql+psycopg` 映射也第一次有了对应驱动）；方言那半回到"源 PG（抽取）"那格
> 原文的**同步 + `asyncio.to_thread`**，因为本轮在实测上撞了两层：
> ① `psycopg.AsyncConnection.connect()` 在 Windows 默认环流下当场抛
> `psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode…`
> （本机 psycopg 3.3.6 + Python 3.12.10，`sqlstate=None`；换成 `SelectorEventLoop` 之后同一段代码
> 走到的是连接超时，说明挡路的只是环流种类）；
> ② API 进程正是那个不可用的环流——`uvicorn.loops.auto.auto_loop_factory()` 在本机返回
> `asyncio.windows_events.ProactorEventLoop`（uvicorn 0.53.0，uvloop 未装）。
> worker 侧 `scripts/run_worker.py:40` 已把策略切成 `WindowsSelectorEventLoopPolicy`，异步理论上能跑，
> 但同步这一路在两种环流下都不踩那个坑，而且与 `mysql.py`/`pymysql` 同形（`_rows` 是唯一 I/O 出口，
> 编排因此能在不连库的情况下被单测钉住），所以选它。元数据库那一格继续 `asyncpg`，与 ① 的结论不变。
> **as-built(P3-024 live)：本表"PG 源抽取跑通"那一格现在有了执行证据。** `postgres.py` 的五条 SQL
> 已真发往 §1.5 的 `ai_web_demo_pg`（`tests/integration/test_extract_pg_live.py` 四条，走真
> `POST /api/datasources` → `POST /api/sync/jobs` → worker 循环体），同步 + `to_thread` 这一路把
> 五条发完、把 10 对象/82 列/14 索引/6+2 条关系/10 张卡落了行。一句限定：用例跑在
> `tests/conftest.py` 强制的 `WindowsSelectorEventLoopPolicy` 下，与 worker 进程
> `run_worker.py:40` 的策略同型，所以这一轮**没有**覆盖 ① 那个 Proactor 场景——而它也不需要：
> 抽取器只在 worker 进程里跑，API 进程对 PG 源连的是 501 那条早退路径（见 verification §1.6 第 2 步）。
> 对账数字与"仍没证到的那一格"写在 verification §1.6.1 与 metadata-model §8.2 末注 as-built(P3-024)。
> live 同时抓出两个**不在本表这一格里**的缺陷（推断边的 catalog 查找键写死了 MySQL 惯例、
> `enum_values` 落的是 JSON `null` 而不是 SQL NULL），理由与修法分别写在 metadata-model §4 与 §2.4
> 的 as-built(P3-024) 两块。

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
