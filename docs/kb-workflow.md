# 知识库工作流（markdown 覆盖层 + 表级知识卡片）

> 两条独立的产物，共同构成"AI 看得懂你的库"：
> ① 元数据库里的**表级知识卡片**（自动生成，供检索与 prompt 使用）；
> ② 仓库里的 `knowledge/` **markdown 覆盖层**（人工维护，进 git 审阅）。
> 两者之间是**单向回写**。

## 1. `knowledge/` 覆盖层的定位

| 属性 | 约定 |
|---|---|
| 目录根 | `AIWEB_KB_DOCS__DIR`，默认 `knowledge`（仓库根） |
| 版本控制 | **属于版本库内容，不忽略**——`.gitignore` 里专门留了注释行说明这条 |
| 内容性质 | 人工维护的知识文件：中文名、业务描述、粒度、枚举含义、关系确认、口径 |
| 可写性检查 | 启动体检 `check_dirs` 会对 `kb_docs.dir` 与 `result.dir` 做"建目录 + 写探针 + 删除" |
| 回写开关 | `AIWEB_KB_DOCS__AUTO_IMPORT=true` —— 启动时把**人工栏**回写元数据库 |
| 清理策略 | `make clean` 明确"只清构建产物与缓存，绝不动数据库、结果集和 knowledge/" |

## 2. 文件命名

```
knowledge/<ds>/<schema>.<table>.md
```

- `<ds>`：数据源的 `name`（`aiweb.data_sources.name` 全局唯一，所以能当目录名）。
- `<schema>` / `<table>`：规范化后的 `schema_name` / `table_name`（见 metadata-model.md §1 的方言映射：
  MySQL 的 schema 就是数据库名；PG 是 `public` 这类）。
- 一个表一个文件；文件名里的 `<schema>.<table>` 与库内自然键
  `UNIQUE (datasource_id, catalog_name, schema_name, table_name)` 一一对应，
  导入时按这条自然键定位行，定位不到就报"未同步的表"而不是新建。
- `table_uid`（md5 四元组）作为跨同步/跨环境的稳定指纹，用于比对与去重。
- 目录名/文件名不允许出现路径分隔符或 `..`：读写都要校验解析后的真实路径仍落在 `AIWEB_KB_DOCS__DIR` 内
  （与结果文件下载同一套目录穿越防护，见 nl2sql-safety.md §7）。

## 3. front-matter 字段

文件头用 YAML front-matter 存身份与同步状态，**不参与人工编辑**：

| 字段 | 含义 |
|---|---|
| `datasource` | 数据源 `name`（对应 `<ds>` 目录） |
| `catalog` / `schema` / `table` | 规范化自然键三元组（MySQL 的 catalog 恒空串） |
| `table_uid` | md5 指纹，用于确认文件与库内行是否同一对象 |
| `kind` | `BASE TABLE` / `VIEW` |
| `synced_at` | 最后一次同步写入自动区的时间 |
| `card_template_version` | 生成自动区所用的模板版本（模板变更触发重算） |

身份字段以下的内容全部在正文两个区块里。

## 4. `## 自动生成` 与 `## 人工维护` 的覆写契约

单个 markdown 文件分两个区块：

```markdown
---
datasource: demo-mysql
catalog: ""
schema: ai_web_demo
table: order_main
table_uid: 3f2a…
kind: BASE TABLE
synced_at: 2026-09-23T09:44:00+08:00
card_template_version: v1
---

## 自动生成
（表结构快照：字段、类型、索引、外键、统计——由同步作业重建）

## 人工维护
- comment_zh: 订单主表（一笔订单一行）
- business_desc: 记录买家下单到完结的主信息，金额口径以 pay_amount 为准
- granularity: one row = 一笔订单
- is_hidden: false
- 关系确认: order_main.customer_id → customer.id（人工确认）
- 枚举含义: status.completed = 已完成（用户口中的"已完成订单"）
```

覆写规则（与元数据库的 upsert 语义严格同源）：

| 方向 | 规则 |
|---|---|
| 同步作业 | **只重写 `## 自动生成` 区块**，整段替换；`## 人工维护` 区块原样保留 |
| 导入（`AUTO_IMPORT=true`，启动时） | 只把 `## 人工维护` 区块的值回写进库内的**人工列**：`comment_zh` / `business_desc` / `granularity` / `is_hidden`（列级同理） |
| 回写落库方式 | 与 upsert 一致：人工列用 `COALESCE(现有值, 新值)` 语义、`is_hidden` 原样保留——**人工优先，永不冲掉人工值** |
| 导出 | 只从库里取人工列写进 `## 人工维护`；结构快照写进 `## 自动生成` |
| 双向？ | **不是双向**。人工内容走 `markdown → DB`；结构内容走 `源库 → DB → markdown 自动区`。DB 永不反向改写人工区块 |

**单向回写（one-way）**的含义就是：文件里的人工区块是人工内容的**唯一权威来源**，
git diff 即人工知识的变更审计；数据库只是它的消费者。想让某条人工知识生效，改 markdown 提交、
重启（或触发导入）即可；想撤销，`git revert` 再导入。

> UI 上编辑人工字段（`PATCH /metadata/tables/{table_uid}`、`PATCH /metadata/columns/{id}`）改的是库内人工列；
> 导出 markdown 时这些值出现在人工区块里。两者是同一份语义的两个入口，以"人工优先、自动不覆盖"为共同不变式。

## 5. 表级知识卡片模板

卡片文本由 Jinja 模板生成（`app/prompts/card_template.j2`），这张卡既是 embedding 文档，也是喂 prompt 的 schema 段：

```jinja
【表】{{ full_name }}  {{ '（视图）' if table_type=='VIEW' else '' }}
【说明】{{ table_comment or '（源库无表注释）' }}
【粒度】{{ granularity or '未知：一行代表一条记录' }}
【规模】约 {{ approx_rows | human_int }} 行{{ '，最近更新 ' + last_update if last_update else '' }}
【字段】共 {{ columns | length }} 个{{ '（本卡仅列出第 {{seg_from}}-{{seg_to}} 个，完整清单见主卡）' if shard else '' }}：
{% for c in columns -%}
- {{ c.name }} {{ c.data_type }}{{ ' NOT NULL' if not c.nullable else ' 可空' }}{{ ' 主键' if c.is_pk }}{{ ' 唯一' if c.is_unique }}: {{ c.comment_zh or c.comment_raw or '（无注释）' }}{% if c.default %} [默认 {{ c.default }}]{% endif %}{% if c.enum_values %} 取值: {{ c.enum_values | join(' / ') }}{% endif %}
{% endfor -%}
{% if indexes -%}
【索引】
{% for i in indexes -%}
- {{ i.name }}（{{ '唯一 ' if i.unique }}{{ i.type }}）: {{ i.columns | join(', ') }}{{ ' 前缀 ' ~ i.sub_part if i.sub_part else '' }}
{% endfor -%}
{% endif -%}
{% if relations -%}
【可关联】
{% for r in relations -%}
- {{ r.from_column }} → {{ r.to_table_full }}.{{ r.to_column }}{% if r.via %}（经 {{ r.via }} 中转）{% endif %}{{ ' [推断,置信 ' ~ r.confidence ~ ']' if r.kind=='inferred' }}{{ ' [人工确认]' if r.kind=='manual' }}
{% endfor -%}
{% endif -%}
{% if terms -%}
【术语】{% for t in terms %}{{ t.name }}={{ t.definition }}；{% endfor %}{% endif %}
【方言】{{ dialect_name }} {{ server_major }}{{ ' — 不支持 CTE 与窗口函数，且默认 ONLY_FULL_GROUP_BY' if dialect_name=='mysql' and server_major=='5.7' else '' }}
```

设计要点（每条都为检索或为 LLM 服务）：

- **中英混排、标识符原样**：`pay_amount decimal(18,2): 实付金额`。向量模型和 trgm 都能命中
  "实付金额"和"pay_amount"两个入口。
- **枚举取值必须进文本**：中文库里 `status='已完成'` 是唯一能对上自然语言的东西；
  缺了它 AI 只能猜 `status='done'`。
- **方言约束写在卡片尾部**：把"MySQL 5.7 无 CTE/窗口函数/ONLY_FULL_GROUP_BY"作为可检索上下文注入，
  比只写在 system prompt 里对 5.7 更稳（不同表分属不同数据源时也能带上）。
- **不做 markdown 表格**（`|` 会稀释 trigram 且吃 token）。
- `search_text` = 去掉【】和连接符的扁平拼接 + 表名字段名的**拼音首字母**（可选，用 `pypinyin`，
  可选 extra `cn`；老仓库字段缩写 `ddgs`→"订单概算" 这类靠它救）。第一版把拼音当可开关的增强项，
  同时用它绕开远端 PG 没有 `pg_jieba`/`zhcfg` 中文分词器的问题。

卡片编辑的语义：编辑后 `template_version` 不变但 `content_hash` 变，标记"人工修订"，
并触发该表 embedding 重算（`POST /kb/reembed {table_id}`）。
模板本身变更时升 `AIWEB_EXTRACT__CARD_TEMPLATE_VERSION`（写进 `kb_index_profile`），触发全量重算。

## 6. 分块策略（宽表）

| 场景 | 切法 |
|---|---|
| 列数 ≤ 40 且 `token_count ≤ 900` | 一张 `kind='table'` 卡，字段全列 |
| 列数 > 40 | 主卡（表头 + 前 25 列 + 全部 PK/索引列/外键列）+ `kind='table_columns'` 卡 `seq=1..n`，每卡 30 列、**重复表头块**（否则切片自身无判别力） |
| 术语/指标口径 | `kind='term'` 卡，人工录入（第一版）；一术语一卡，`table_id` 关联到落地表 |
| 视图 | 与表同构，`meta.weight=0.6`（检索后降权，物理表优先） |

token 估算**不引真 tokenizer 到写入路径**：`CJK 字符数×1 + ASCII 词数×1.3` 的启发式
（`app/services/token_estimate.py`，纯函数 + 单测）；只在最后组装 prompt 时用 `tiktoken` 精算裁切
（`AIWEB_RETRIEVAL__TOKEN_BUDGET`）。
宽表超预算时降级为"仅主卡摘要"，优先保留前 25 列 + PK/FK 列。

## 7. 构建与重建

- 卡片构建与 embedding 回填是同步作业的 `card_build` / `embed` 两个 phase（`sync_jobs.phase` 枚举里就有），
  也可由 `POST /kb/rebuild`（admin）单独触发，202 返回 job id、SSE 看进度。
- 换 embedding 模型：新建 `kb_index_profile`（draft）→ 全量重建 → `POST /api/kb/profiles/{id}/activate`
  原子切换（同事务把旧 active 置 retired）。维度不同时要**新表/新列**（影子列/影子表），
  激活后旧 profile 向量仍在，可回滚。
- `scripts/reembed.py`：换模型时全量重建。
- 未启用向量（`settings.embedding.configured=False`：base_url/key/model 任一不齐，或 `DIMENSION=0`）时
  卡片照样构建，`search_text` 与关键词索引可用，`embedding` 列留 NULL——这是 pgvector 可插拔的具体体现。
  **注意"可插拔"指的是运行期不调 embedding 端点，不是迁移期不需要 `vector` 类型**：
  `AIWEB_EMBEDDING__DIMENSION` 同时是 `kb_card.embedding` 的**列宽**，所以 `0005` 迁移要求它 `>0`，
  为 `0` 时拒绝建表并给中文 hint。向量路真正的开关是 `embedding.configured`（见 architecture.md §5.1）。

## 8. 枚举 distinct 值采样

中文枚举值是问数准确率的第一大坑（用户说"已完成"，库里存 `completed`）。采样策略：

| 键 | 默认 | 语义 |
|---|---|---|
| `AIWEB_EXTRACT__SAMPLE_DISTINCT` | `true`（演示库）/ 计划建议真实大库**默认关**、按数据源在 UI 勾选 | 采样会**扫源库数据**，涉及"是否读了业务数据"的合规边界与同步耗时 |
| `AIWEB_EXTRACT__SAMPLE_DISTINCT_MAX_DISTINCT` | `30` | 只有 NDV ≤ 该值才视为枚举列 |
| `AIWEB_EXTRACT__SAMPLE_ROW_LIMIT` | `1000` | 单次采样扫描上限 |

执行顺序（先过滤再扫，别拿全表试）：

1. 先按列类型筛候选：`information_schema.columns.data_type IN ('enum','set','varchar','char','boolean','tinyint')`
   （只挑 enum/短文本，且低基数）。
2. 再判基数：`SELECT col, COUNT(*) ... GROUP BY col LIMIT 40` —— 一旦超过阈值即放弃该列。
3. 通过的列写入 `meta_column.enum_values`（`["待支付","已支付"]`）与 `sample_values`，
   并由卡片模板的 `取值: …` 段落带进检索文本与 prompt。

`.env.example` 的注释口径是"**只取值不存行数据**"：采样读的是聚合结果（DISTINCT 值），
结果集不落 `data/results/`，只有值列表进元数据列。

代价与取舍（原文明确记录）：开采样会显著上升同步时长与源库读压力（演示库可忽略，真实大库可能翻倍），
但"中文到枚举值的映射"是用户第一周就会遇到的主要 bad case，所以演示/验收期建议开、
真实大库按数据源单独勾。`SourceDialect.sample_distinct()` 在接口上是可选方法，默认 disabled。

## 9. 术语与 few-shot

- 术语：`glossary(term, definition, synonyms[], table_refs[])` 注入 prompt 的独立段；
  也做成 `kind='term'` 卡片参与检索。第一版人工录入（`CRUD /kb/terms`）。
- few-shot：v1 用内置 `examples/*.yaml`（10 条手写 Q→SQL，覆盖时间分组、Top-N、
  同比环比在 5.7 的写法、多表 JOIN、HAVING、中文枚举过滤）+ 对示例问题做同一套 embedding 取 top-3
  且要求 `result_stats.row_count > 0`。
  预算单独算（`AIWEB_RETRIEVAL__FEW_SHOT_EXAMPLES=3`、`FEW_SHOT_TOKEN_BUDGET=1500`），
  预算不足时**整段丢弃**而不是半条。
- 回流：`chat_feedback.verdict='adopted'` 或 `corrected_sql` 非空 → 生成"候选示例"进审核列表，
  admin 一键批准才写入（**不自动回流**，防污染）。命中路径是检索阶段的**问题向量近邻**（不是字符串相等），
  命中时 SSE 的 `retrieval` 帧带 `few_shot_hit=true`。
