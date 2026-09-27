# 元数据模型

> 覆盖：唯一性模型、人工/同步列分离、关系来源与删除差分、同步任务与心跳/僵尸回收、
> 以及各方言的批量抽取 SQL 原文。所有表都在远端 PostgreSQL 的 `aiweb` schema 内。

## 1. 唯一键设计陷阱（决定所有子表结构）

同一个 PG 应用库要装多个远端数据源的元数据，而**"表名"只在四元组内才唯一**：

- **MySQL**：`information_schema.TABLES.TABLE_SCHEMA` 语义上是 **database**，`TABLE_CATALOG` 恒为 `def`。
- **PostgreSQL**：`TABLE_CATALOG` 才是真 database（**一个连接只能看到一个 catalog**），
  `TABLE_SCHEMA` 是 `public` 这类。
- SQL Server（未来）：catalog=库、schema=`dbo`。

两个反面做法都要避免：

- 直接 `UNIQUE (datasource_id, table_name)` → 不同库的 `orders` 互相覆盖。
- 把四元组拼成主键外泄到所有子表 → 每张子表都背 4 列复合外键，索引巨大且难写。

**做法**：所有元数据实体都有 `id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY` 作**代理键**，
只在"根实体"上挂业务唯一键，子表一律 FK 到根 id。

```sql
-- 规范化列（跨方言统一语义）
catalog_name  text NOT NULL DEFAULT ''   -- PG: database / MySQL: 恒 ''（不要塞 'def'）
schema_name   text NOT NULL DEFAULT ''   -- MySQL: 等于数据库名 / PG: public 等
-- MySQL 映射： catalog_name='', schema_name=<db>；PG 映射： catalog_name=<db>, schema_name=<schema>
UNIQUE (datasource_id, catalog_name, schema_name, table_name)
```

再加一个**稳定自然指纹**，供 embedding doc id、URL、跨同步比对、导入导出使用
（避免暴露自增语义）：

```sql
table_uid char(32) GENERATED ALWAYS AS
  (md5(concat_ws(<分隔符>, datasource_id, catalog_name, schema_name, table_name))) STORED,
UNIQUE (table_uid)
```

- 分隔符用 `\x1f`（单元分隔符）这类**不可能出现在标识符里的字符**，
  否则 `a_b`+`c` 与 `a`+`b_c` 会撞出同一个 md5。
- 类型用 `char(32)` 定长 + `UNIQUE` 走 btree 精确命中；**不要用 `uuid`**（md5 不是 uuid 形状，别硬套）。
- `data_sources.catalog_name` 与 `meta_*` 的 `catalog_name` 语义不同：前者是"PG 目标库；MySQL 留空"，
  后者是跨方言规范化列。

## 2. 表清单与前缀

前缀：`usr_` 用户域 / `ds_` 数据源域 / `meta_` 元数据快照 / `sync_` 同步 / `kb_` 知识 / `chat_` 问答。
实际物理表名见下列 DDL 段（`users`、`data_sources`、`meta_table` …）。

```sql
CREATE SCHEMA IF NOT EXISTS aiweb;
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- trusted=true，非超管也能建
CREATE EXTENSION IF NOT EXISTS btree_gin; -- 供复合 GIN（可选）
-- CREATE EXTENSION vector  ← 可插拔的向量增强，需超级用户/DBA 预建（0.8.6 的 vector 非 trusted）
```

`alembic_version` 也落在 `aiweb` schema 内（`version_table_schema` 必须显式传）。
ORM 侧 `MetaData(schema=settings.pg.schema_name, naming_convention=...)` 在建表时就固定 schema，
Alembic 与应用必须看到同一个值，否则会"看起来没迁移"。命名约定：

```python
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}
```

### 2.1 `aiweb.users`

```
id            bigint identity PK
username      citext NOT NULL UNIQUE            -- 大小写不敏感用户名（需 citext 扩展；否则 lower 表达式唯一索引）
display_name  text NOT NULL DEFAULT ''
email         citext NULL UNIQUE
password_hash text NOT NULL                      -- argon2id
role          text NOT NULL CHECK (role IN ('admin','member')) DEFAULT 'member'
is_active     bool NOT NULL DEFAULT true
created_at / updated_at  timestamptz NOT NULL DEFAULT now()
last_login_at timestamptz NULL
token_version int NOT NULL DEFAULT 0             -- 改密/登出全失效：JWT 携带并比对
```

> 会话/refresh token 落库（`refresh_token` 表：jti PK, user_id, expires_at, revoked_at, ua, ip），支持吊销；
> **不引入 Redis**（单进程，DB 足够）。

### 2.2 `aiweb.data_sources`

```
id                bigint identity PK
name              text NOT NULL UNIQUE            -- UI 展示名
kind              text NOT NULL CHECK (kind IN ('mysql','postgres'))   -- 预留 mssql/oracle/clickhouse
host              text NOT NULL
port              int  NOT NULL
catalog_name      text NOT NULL DEFAULT ''        -- PG 目标库；MySQL 留空
connect_user      text NOT NULL                   -- 连接账号（建议只读账号）
secret_enc        bytea NOT NULL                  -- Fernet(token) → 二进制，别用 text
server_version    text NULL                       -- test_connection 时探测写入
params            jsonb NOT NULL DEFAULT '{}'::jsonb  -- {charset:utf8mb4, ssl_mode, tenant...}
include_schemas   text[] NULL                     -- 白名单；NULL=自动发现
include_tables    text[] NULL / exclude_tables text[] NULL   -- 正则/通配
readonly_enforced bool NOT NULL DEFAULT true   -- as-built(006)：表单不收这一列（POST 请求体里没它），
                                               -- 所以它恒为默认 true，没有任何证据支持。源库真相看
                                               -- test_connection 的 grants.{read_only,code,warnings}；
                                               -- 拿只读结论去挡执行在 011（executor）那一片
row_limit         int NOT NULL DEFAULT 1000
timeout_ms        int NOT NULL DEFAULT 15000
allow_global_access bool NOT NULL DEFAULT false   -- 授权给全体
status            text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active','disabled'))
last_sync_at      timestamptz NULL
created_by        bigint FK users(id) ON DELETE RESTRICT
created_at/updated_at/deleted_at
```

> **不设 `database` 单列存"选中的库"**——MySQL 的库在 `include_schemas`（schema==db）里表达，
> 避免两方言语义分叉。`row_limit` / `timeout_ms` 属于 L4 数据源级配置，可覆盖全局默认。

### 2.3 `aiweb.datasource_grants`

```
id bigserial PK / datasource_id FK CASCADE / principal_type CHECK('user','role')
principal_id bigint NULL(user.id) / principal_role text NULL('member')
permission text CHECK (permission IN ('read','sync','own')) DEFAULT 'read'
UNIQUE (datasource_id, principal_type, COALESCE(principal_id,0), COALESCE(principal_role,''))
```

> PG 不允许 UNIQUE 里写 `COALESCE` → 用两条部分唯一索引：
>
> ```sql
> CREATE UNIQUE INDEX ON aiweb.datasource_grants(datasource_id, principal_id)  WHERE principal_type='user';
> CREATE UNIQUE INDEX ON aiweb.datasource_grants(datasource_id, principal_role) WHERE principal_type='role';
> ```

### 2.4 元数据快照（6 张，全部 CASCADE 到 data_sources）

**`aiweb.meta_database`**（PG=catalog / MySQL=schema，统一"顶层可选单位"）

```
id PK / datasource_id FK / catalog_name '' / schema_name
raw_collation text / raw_engine text / table_count int
approx_rows bigint / approx_size_bytes bigint
is_visible bool DEFAULT true      -- 权限不可见时 false，用于权限缺失告警
sync_job_id bigint               -- 最后一次成功写入它的任务
updated_at
UNIQUE (datasource_id, catalog_name, schema_name)
```

**`aiweb.meta_table`** ★

```
id bigserial PK / table_uid char(32) UNIQUE / datasource_id / database_id FK
catalog_name / schema_name / table_name
table_type text CHECK ('BASE TABLE','VIEW')      -- 视图单独色，NL2SQL 里降权
comment_raw   text NULL      -- 源库原样注释（可能是英文/空）
comment_zh    text NULL      -- 人工/AI 补录中文（人工优先，同步不覆盖）
business_desc text NULL      -- 业务描述："这张表记录什么、粒度是什么、多久更新"
granularity   text NULL      -- 'one row = ?'  ← 极大提升 NL2SQL 准确率，人工填
engine / row_format / collation / charset text NULL
approx_rows bigint NULL / data_bytes / index_bytes bigint NULL
last_analyze_at timestamptz NULL   -- 统计新鲜度；太旧提示 AI 别信 row count
is_hidden bool DEFAULT false       -- 手工排除某些表（临时表/备份表 _old/_bak）
is_stale bool                      -- 本次同步未出现的表打陈旧标记，不物理删
synced_at / created_at
UNIQUE (datasource_id, catalog_name, schema_name, table_name)
```

**`aiweb.meta_column`**

```
id / table_id FK CASCADE / ordinal_position int / column_name
data_type text            -- 归一化后：'int','bigint','varchar(64)','decimal(18,2)','datetime',...
raw_data_type text        -- 方言原文 longtext / character varying / enum('a','b')
nullable bool / default_value text / is_generated bool
comment_raw text / comment_zh text / business_desc text
is_primary_key bool / is_unique bool / is_indexed bool
enum_values jsonb NULL    -- ["待支付","已支付"] —— 中文枚举值直接决定 where 条件能否命中
sample_values jsonb NULL  -- 少量低基数distinct值（可选，默认关）
char_length int / numeric_precision int / numeric_scale int
synced_at
UNIQUE (table_id, column_name)
```

**`aiweb.meta_index`**

```
id / table_id FK / index_name / is_unique bool / is_primary bool
index_type text            -- BTREE / FULLTEXT / SPATIAL / HASH(GIN/GiST for PG)
comment text / is_visible bool / cardinality bigint NULL
funcdef text NULL          -- 表达式索引（PG）
UNIQUE (table_id, index_name)
```

**`aiweb.meta_index_column`**

```
id / index_id FK / seq_in_index int / column_name text NULL   -- NULL 表示表达式位
collation text NULL  -- 'A'/'D'/NULL
sub_part int NULL    -- 前缀索引长度（MySQL 特有，SQLAlchemy 会丢）
UNIQUE (index_id, seq_in_index)
```

**`aiweb.meta_relation`**（外键 + 推断 + 人工，**统一一张表**）

```
id / datasource_id
source_kind text CHECK ('extracted','inferred','manual')   -- 优先级 manual > extracted > inferred
fk_name text NULL
from_table_id FK / from_column_name text
to_table_id   FK / to_column_name   text
on_delete text NULL / on_update text NULL / deferability NULL
confidence numeric(4,3) NULL DEFAULT 1.0    -- inferred 才有；≥0.8 才进 prompt
is_authors_enforced bool DEFAULT true       -- 外键存在但库没启用 FK 约束
created_by / created_at / updated_at
UNIQUE (datasource_id, source_kind, from_table_id, from_column_name, to_table_id, to_column_name)
```

### 2.5 `aiweb.sync_jobs`

```
id bigserial PK / datasource_id FK / triggered_by FK users
status text CHECK ('pending','running','success','partial','failed','cancelled')
phase  text CHECK ('connect','discover','tables','columns','indexes','fks','card_build','embed','done')
progress numeric(5,2) DEFAULT 0
counters jsonb DEFAULT '{}'   -- {tables_seen,tables_ok,tables_failed,columns,indexes,fks,cards,cards_embedded}
warnings jsonb DEFAULT '[]'   -- [{code:'permission_hidden', detail:'库 x 下 12 张表不可见'}]
errors   jsonb DEFAULT '[]'   -- [{phase:'indexes', table:'db.t', message:...}]
manifest_digest text NULL      -- 与上次成功的指纹，命中则短路
heartbeat_at timestamptz       -- 用于回收僵尸 running 任务
started_at / finished_at / created_at
```

> `partial` 是必需态：2000 表跑到一半失败时，前面的成果保留、界面可见"部分成功 + 失败清单"，
> 比整单回滚实用得多（元数据表带 `synced_at`，天然支持部分写）。

### 2.6 `aiweb.kb_card` / `aiweb.kb_index_profile`

embedding 粒度决策：**以「表级卡片」为主体，字段只在字段过多时切片，不做字段级 embedding**。
三条硬理由：① 检索单元 = schema linking 单元（NL2SQL 判的是"涉及哪些表"，字段级向量会让 top-30
挤进同表 15 个碎片，还要再加一层聚合）；② 字段必须带表上下文才有判别力（`amount` 无判别力，
`oms_order.pay_amount(订单实付金额,decimal)` 才有）——这个拼接正好就是表级卡片；
③ 卡片数 = 表数（几百~几千），成本可控可重跑，字段级是 10–40 倍量。
字段级向量唯一价值在"值/枚举反查"，第一版不做，改为把 `enum_values` 写进卡片文本。

```
aiweb.kb_card
id bigserial PK
datasource_id FK
kind text CHECK ('table','table_columns','term')
    -- 'table'        : 表头 + 概览 + 前 N 字段 + 关系（一表一卡）
    -- 'table_columns': 宽表切片（第 2/3.. 段字段清单）
    -- 'term'         : 业务术语/指标口径（人工录入，第一版只做手工）
table_id FK NULL            -- term 卡为 NULL
seq int DEFAULT 0           -- 同一 table 多卡时的段号
doc_uid char(32) UNIQUE     -- md5(card_kind|table_uid|seq|index_profile_id)
title text                  -- 'db.orders（订单表）'
text_md  text NOT NULL      -- 送 embedding 的完整文档
search_text text NOT NULL   -- 关键词检索用（标识符+注释+值，去 markdown 噪声）
token_count int NOT NULL
meta jsonb DEFAULT '{}'     -- {approx_rows, column_count, tags, confidence}
index_profile_id bigint NOT NULL
embedding vector(1536)     -- 维度来自 settings（可插拔增强；未启用向量时全 NULL 不影响链路）
embedded_at timestamptz NULL
sync_job_id / updated_at / deleted_at
```

```sql
-- 向量索引（仅在启用 embedding profile 时需要）：HNSW（cosine），卡片量小用默认参数起步
CREATE INDEX ix_kb_card_emb ON aiweb.kb_card
  USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
-- 关键词索引
CREATE INDEX ix_kb_card_search_trgm ON aiweb.kb_card USING gin (search_text gin_trgm_ops);
CREATE INDEX ix_kb_card_search_fts  ON aiweb.kb_card
  USING gin (to_tsvector('simple', search_text));         -- 'simple' 不做词干，中文按整串
CREATE INDEX ix_kb_card_ds_kind     ON aiweb.kb_card (datasource_id, kind) WHERE deleted_at IS NULL;
```

> 选 HNSW 而非 IVFFlat 的确定理由：pgvector 明确 IVFFlat 需先有数据训练、空表建不了索引，
> 而同步是"先建空卡再灌"的流程。语料几千条时两者都秒回，但 HNSW 免去 `lists` 调参。

```
aiweb.kb_index_profile   -- 把"embedding 模型 + 维度 + 卡片模板版本"变成一等公民
id / name UNIQUE('text-embedding-3-small@1536@tplv1')
provider text / model text / dimensions int
card_template_version int / distance_fn text DEFAULT 'cosine'
is_active bool / created_at / built_at
```

> 用途：换模型/改模板时**新建 profile → 全量重建 → 原子切 active**（同事务里把旧 active 置 retired），
> 检索永远只命中 active 的卡片（`WHERE index_profile_id = active`）。否则新旧向量混在一个索引里，
> 结果不可解释且无法回滚。列类型 `vector(1536)` 是硬约束——维度是列类型的一部分，
> 换维度必须新表/新列（PG 不支持 `ALTER TYPE` 改它），记进迁移。

### 2.7 问答

```
aiweb.chat_sessions : id / user_id FK / datasource_id FK NULL / title / is_pinned / created_at / updated_at / deleted_at
aiweb.chat_messages :
  id / session_id FK / role('user','assistant','system')
  question_text text NULL                       -- 改写前原文
  question_resolved text NULL                   -- 术语对齐/指代消解后
  retrieved jsonb NULL                          -- [{card_id,table_uid,score_vec,score_kw,fused}] ★可解释性
  prompt_tokens / completion_tokens int
  sql_raw text NULL                             -- LLM 原文
  sql_final text NULL                           -- guard 重写后实际执行的（含 LIMIT）
  guard_result jsonb NULL                       -- {ok,violations:[{code,field,message}]}
  executed bool / error_code text / error_message text
  result_columns jsonb / result_stats jsonb     -- {row_count,truncated,elapsed_ms,dialect,结果文件引用}
  chart_spec jsonb NULL                         -- {type:'bar', x:'dt', series:[...], title}
  conclusion text NULL                          -- 自然语言结论
  latency_ms int / model text
  created_at
aiweb.chat_feedback : id / message_id FK UNIQUE / user_id / verdict CHECK('up','down','adopted')
  note text / corrected_sql text NULL / created_at
  -- corrected_sql + verdict='adopted' 就是 few-shot 示例的来源
```

> **行集不在这张表里**：架构正文的早期字段清单里出现过 `result_rows jsonb`，
> 但收口口径是**结果集落 `data/results/` 文件、不落元数据库**（附录 §8 的
> `AIWEB_RESULT__DIR` 分组注释原文："结果集文件（不落元数据库）"）。
> 因此 `chat_messages` 只保留 `result_columns`（列定义）、`result_stats`
> （`{row_count,truncated,elapsed_ms,dialect,结果文件引用}`）与 `chart_spec`。
> 见 architecture.md §3.3。

## 3. 人工列与同步列的分离（幂等重跑的关键）

`comment_raw` 由同步写，`comment_zh` / `business_desc` / `granularity` / `is_hidden` 是人工字段，
**同步永不冲掉**。三段式 upsert（每个 batch 一个事务）：

```python
# 1) 按自然键 upsert，人工字段永不被覆盖
INSERT INTO aiweb.meta_table (...) VALUES (...)
ON CONFLICT (datasource_id, catalog_name, schema_name, table_name) DO UPDATE SET
  comment_raw     = EXCLUDED.comment_raw,
  engine          = EXCLUDED.engine,
  approx_rows     = EXCLUDED.approx_rows,
  table_type      = EXCLUDED.table_type,
  comment_zh      = COALESCE(aiweb.meta_table.comment_zh, EXCLUDED.comment_zh),  -- 人工优先
  business_desc   = COALESCE(aiweb.meta_table.business_desc, EXCLUDED.business_desc),
  granularity     = COALESCE(aiweb.meta_table.granularity,  EXCLUDED.granularity),
  is_hidden       = aiweb.meta_table.is_hidden,      -- 人工开关，原样保留
  synced_at       = now();
-- 2) 字段/索引：delete-then-insert per table（子表无人工字段，全量替换最简）
DELETE FROM aiweb.meta_column WHERE table_id = ANY(%ids);  INSERT ...
-- 3) 关系：只删 extracted，manual/inferred 各自走自己的重建逻辑
DELETE FROM aiweb.meta_relation
 WHERE datasource_id=%d AND source_kind='extracted'
   AND id NOT IN (刚 upsert 的 id 集合);   -- 用 CTE: with keep as (insert ... returning id) delete where id not in (select id from keep)
```

语义要点：

- `COALESCE(现有值, 新值)` = "库里已有人工值就保留，只有为空时才接受新值"。
  `is_hidden` 直接原样保留（人工开关，连新值都不接受）。
- 子表（列/索引）**无人工字段**，所以 delete-then-insert 全量替换最简。

## 4. `meta_relation.source_kind` 与删除差分

- 三种来源优先级：`manual` > `extracted` > `inferred`。
- **delete-diff 只作用于 `source_kind='extracted'`**：
  > 关键：inferred 与 manual **不参与同步删除**（同步只按 `source_kind='extracted'` 做 delete-diff），
  > 否则每次点"同步"人工补录的关系全没了。
- `inferred` 由命名约定打分产生（`confidence` ≥0.8 才进 prompt，标签 `[推断,置信 0.87]`），
  UI 可一键 accept → 转成 `manual`（`GET /metadata/relations/inferred` + `POST /metadata/relations`）。
- `extracted` 不可在 UI 删除（`DELETE /metadata/relations/{id}` 只允许 manual/inferred）。
- `is_authors_enforced` 记录"外键存在但库没启用 FK 约束"这类现实情况。

## 5. 陈旧标记与硬删

再对"本次未出现的表"打 `is_stale=true`（**不物理删**，因为 `chat_messages` 还引用它）。
加一个 `POST /metadata/purge-stale`（admin，带 `dry_run`）才真删。

## 6. 同步任务语义：心跳、互斥、僵尸回收、partial

| 情况 | 处置 |
|---|---|
| 连接失败 / 认证失败 / 网络 | `probe()` 阶段直接 `failed`，`error_code` 分类（`AUTH_FAILED`/`HOST_UNREACHABLE`/`TIMEOUT`/`UNSUPPORTED_VERSION`）。**MySQL < 5.7、PG < 12 视为不支持并明确报错**（5.6 无 IS 统计、PG<12 部分函数缺） |
| 中途某张表 IS 查询失败 | 记 `errors[]` + `counters.tables_failed++`，**继续下一批**，最终 `status='partial'` |
| 权限不全（MySQL 只能看到被 grant 的对象） | `discover()` 里比对 `SHOW DATABASES` 结果 vs `SCHEMATA` 可见集合；差异写 `warnings[{code:'SCHEMA_PARTIALLY_VISIBLE'}]`；UI 黄条提示"该账号看不到 N 个库/表，请补 GRANT SELECT"。**绝不允许**因为"看不到"就删掉上次同步到的元数据 → 只有 `catalog` 层确认成功枚举到的 schema 才参与 stale 判定（`known_complete=True` 时才做 delete-diff） |
| 超大库（>2000 表） | `probe()` 先 `SELECT COUNT(*)` 预估 → 超过 `AIWEB_EXTRACT__MAX_TABLES`（默认 2000）**拒绝**并返回结构化提示 + 三个出路：①配 `include_tables` 白名单 ②只同步部分 schema ③admin 用 `?force=true` 覆盖上限 |
| 宽表（>200 列） | 抽取照常，卡片构建走列切片，并给 warning"字段过多建议拆视图" |
| 同步中重复点"同步" | 部分唯一索引：`CREATE UNIQUE INDEX ux_sync_running ON aiweb.sync_jobs(datasource_id) WHERE status IN ('pending','running');` → 天然互斥，冲突返回 409 `sync_already_running`（是数据库保证，不是代码 race） |
| 进程崩溃留下僵尸 running | 启动 `lifespan` 里 `UPDATE sync_jobs SET status='failed', error='reclaimed on startup' WHERE status IN ('pending','running') AND heartbeat_at < now()-interval '3 minutes'`；之后 admin 可"重跑" |
| 抽取把源库拖垮 | 全部 IS 查询前 `SET SESSION max_execution_time` / `SET LOCAL statement_timeout`；批量大小固定 200；批间 `await asyncio.sleep(EXTRACT__BATCH_INTERVAL_MS)`；单连接串行，不开并发打源库 |

对应配置：`AIWEB_EXTRACT__HEARTBEAT_INTERVAL_S=10`、`AIWEB_EXTRACT__STALE_JOB_RECLAIM_S=180`
（体检/自检里 heartbeat_at 超过该值判僵尸并在启动时回收）、`AIWEB_EXTRACT__BATCH_SIZE=200`（每批一个事务，
也是 `partial` 的粒度）、`AIWEB_EXTRACT__MAX_TABLES=2000`、`AIWEB_EXTRACT__MIN_MYSQL_VERSION=5.7`、
`AIWEB_EXTRACT__MIN_PG_VERSION=12`。

三条实现约束：

1. `heartbeat_at` 必须在**独立连接**上更新——主事务未提交时进度才可见（否则前端进度条 3 分钟不动）。
2. 批内一事务，但**源库读和元数据库写不要混在一个 session**（跨两个 engine）。
3. `manifest_digest` 命中上次成功指纹时短路，避免无变化的重复重建。

## 7. `SourceDialect` 抽象与中间结构

```python
# backend/app/extractor/base.py
from typing import Protocol, Sequence, Iterable
from dataclasses import dataclass, field
import datetime as dt

@dataclass(slots=True, frozen=True)
class RawCatalog:
    catalog_name: str
    schema_name: str
    charset: str | None
    collation: str | None
    approx_size_bytes: int | None
    visible_table_count: int | None
    grant_limited: bool = False           # 该 schema 疑似因权限被 IS 隐藏

@dataclass(slots=True, frozen=True)
class RawTable:
    catalog_name: str; schema_name: str; table_name: str
    table_type: str                        # 已归一：'BASE TABLE' | 'VIEW'
    comment: str | None
    engine: str | None; charset: str | None; collation: str | None
    approx_rows: int | None
    data_bytes: int | None; index_bytes: int | None
    create_sql: str | None                 # 供 UI "查看建表语句"，也是注释兜底来源

@dataclass(slots=True, frozen=True)
class RawColumn:
    catalog_name: str; schema_name: str; table_name: str
    column_name: str; ordinal_position: int
    data_type: str; raw_data_type: str
    nullable: bool; default: str | None; generated: bool
    comment: str | None
    char_length: int | None; num_precision: int | None; num_scale: int | None
    enum_values: tuple[str, ...] | None
    is_primary_key: bool; indexed_columns: tuple[str, ...] = ()

@dataclass(slots=True, frozen=True)
class RawIndexColumn:
    column_name: str | None; seq_in_index: int; collation: str | None; sub_part: int | None

@dataclass(slots=True, frozen=True)
class RawIndex:
    catalog_name: str; schema_name: str; table_name: str
    index_name: str; is_unique: bool; is_primary: bool
    index_type: str; comment: str | None; cardinality: int | None
    columns: tuple[RawIndexColumn, ...]

@dataclass(slots=True, frozen=True)
class RawForeignKey:
    catalog_name: str; schema_name: str; table_name: str   # from
    fk_name: str
    from_column: str; to_catalog: str | None; to_schema: str | None
    to_table: str; to_column: str
    seq: int
    on_delete: str | None; on_update: str | None

@dataclass(slots=True)
class SourceManifest:
    kind: Literal["mysql", "postgres"]
    server_version: str
    collected_at: dt.datetime
    catalogs: list[RawCatalog]
    tables: list[RawTable]
    columns: list[RawColumn]
    indexes: list[RawIndex]
    foreign_keys: list[RawForeignKey]
    warnings: list[ExtractWarning] = field(default_factory=list)
    truncated: bool = False                # 触发规模保护时为 True

class SourceDialect(Protocol):
    kind: ClassVar[str]
    def __init__(self, conn_spec: ConnectionSpec) -> None: ...
    async def probe(self) -> ServerInfo: ...                        # 版本/字符集/权限自检
    async def discover(self) -> list[RawCatalog]: ...
    async def stream_manifest(
        self,
        catalogs: Sequence[RawCatalog],
        *,
        include_tables: Sequence[str] | None,
        exclude_tables: Sequence[str] | None,
        table_limit: int,
        batch_size: int = 200,
        cancel: Callable[[], Awaitable[bool]],
    ) -> AsyncIterator[ManifestBatch]: ...                          # 分批 yield，边抽边落库
    def render_create_sql(self, t: RawTable) -> str | None: ...
    async def sample_distinct(                        # 可选：枚举值采样，默认 disabled
        self, t: RawTable, cols: Sequence[RawColumn], *, max_ndv: int = 30
    ) -> dict[str, list[str]]: ...
    async def close(self) -> None: ...
```

`ManifestBatch` = `dataclass(tables=[...], columns=[...], indexes=[...], fks=[...])`，
一批一个 schema（MySQL 一个 db、PG 一个 schema），每批 ≤ `batch_size` 张表
→ **service 每批一个事务提交**，这就是 `partial` 状态和进度百分比的来源。

## 8. 抽取 SQL 原文

### 8.1 MySQL 5.7（4 条批量 SQL 拿全一切，与表数量无关）

```sql
-- A. catalogs（= databases）+ 尺寸
SELECT s.SCHEMA_NAME, s.DEFAULT_CHARACTER_SET_NAME, s.DEFAULT_COLLATION_NAME,
       COALESCE(SUM(t.DATA_LENGTH + t.INDEX_LENGTH),0) AS size_bytes,
       COALESCE(SUM(t.TABLE_ROWS),0)                  AS approx_rows
FROM information_schema.SCHEMATA s
LEFT JOIN information_schema.TABLES t ON t.TABLE_SCHEMA = s.SCHEMA_NAME
WHERE s.SCHEMA_NAME NOT IN ('information_schema','mysql','performance_schema','sys')
GROUP BY s.SCHEMA_NAME, s.DEFAULT_CHARACTER_SET_NAME, s.DEFAULT_COLLATION_NAME;

-- B. tables + comments + engine（一次拿完）
SELECT t.TABLE_SCHEMA, t.TABLE_NAME,
       CASE t.TABLE_TYPE WHEN 'BASE TABLE' THEN 'BASE TABLE' WHEN 'VIEW' THEN 'VIEW' ELSE t.TABLE_TYPE END AS table_type,
       t.TABLE_COMMENT, t.ENGINE, t.TABLE_COLLATION, t.TABLE_ROWS,
       t.DATA_LENGTH, t.INDEX_LENGTH, t.CREATE_TIME, t.UPDATE_TIME
FROM information_schema.TABLES t
WHERE t.TABLE_SCHEMA = %s
  AND (t.TABLE_TYPE IN ('BASE TABLE','VIEW'))
  AND (t.TABLE_NAME REGEXP %s OR %s IS NULL)        -- include 正则
  AND (t.TABLE_NAME NOT REGEXP %s OR %s IS NULL);   -- exclude 正则

-- C. columns（含 COLUMN_COMMENT / 枚举 / 生成列）
SELECT c.TABLE_NAME, c.COLUMN_NAME, c.ORDINAL_POSITION, c.DATA_TYPE, c.COLUMN_TYPE,
       c.IS_NULLABLE, c.COLUMN_DEFAULT, c.EXTRA, c.COLUMN_COMMENT,
       c.CHARACTER_MAXIMUM_LENGTH, c.NUMERIC_PRECISION, c.NUMERIC_SCALE,
       c.COLLATION_NAME,
       CASE WHEN c.DATA_TYPE='enum' OR c.DATA_TYPE='set' THEN c.COLUMN_TYPE END AS enum_def
FROM information_schema.COLUMNS c WHERE c.TABLE_SCHEMA = %s AND c.TABLE_NAME IN (%s);

-- D. indexes（STATISTICS，保留 CARDINALITY / SUB_PART）
SELECT s.TABLE_NAME, s.INDEX_NAME, (s.NON_UNIQUE=0) AS is_unique,
       (s.INDEX_NAME='PRIMARY') AS is_primary, s.INDEX_TYPE, s.NULLABLE,
       s.COLUMN_NAME, s.SEQ_IN_INDEX, s.CARDINALITY, s.SUB_PART, s.COLLATION,
       IFNULL(it.COMMENT,'') AS index_comment
FROM information_schema.STATISTICS s
LEFT JOIN information_schema.STATISTICS it
       ON it.TABLE_SCHEMA=s.TABLE_SCHEMA AND it.TABLE_NAME=s.TABLE_NAME
      AND it.INDEX_NAME=s.INDEX_NAME AND it.SEQ_IN_INDEX=1
WHERE s.TABLE_SCHEMA=%s AND s.TABLE_NAME IN (%s)
ORDER BY s.TABLE_NAME, s.INDEX_NAME, s.SEQ_IN_INDEX;

-- E. foreign keys（KEY_COLUMN_USAGE ⋈ REFERENTIAL_CONSTRAINTS）
SELECT k.TABLE_NAME, k.CONSTRAINT_NAME, k.COLUMN_NAME, k.ORDINAL_POSITION,
       k.REFERENCED_TABLE_SCHEMA, k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME,
       r.DELETE_RULE, r.UPDATE_RULE
FROM information_schema.KEY_COLUMN_USAGE k
JOIN information_schema.REFERENTIAL_CONSTRAINTS r
  ON r.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA AND r.CONSTRAINT_NAME = k.CONSTRAINT_NAME
WHERE k.TABLE_SCHEMA=%s AND k.REFERENCED_TABLE_NAME IS NOT NULL AND k.TABLE_NAME IN (%s);
```

> 视图的 `TABLE_COMMENT` 在 5.7 拿不到（IS 里视图注释存 `information_schema.VIEWS` 但常为空）
> → 用 `SHOW CREATE TABLE` / `SHOW FULL COLUMNS` 作**注释兜底**，仅对 B/C 返回空的表触发，且限流 ≤N 张。

### 8.2 PostgreSQL（同样 4–5 条）

```sql
-- A. schemas（跨库无法枚举 → catalog 固定为 current_database()）
SELECT n.nspname AS schema_name, pg_catalog.obj_description(n.oid,'pg_class') AS schema_comment
FROM pg_catalog.pg_namespace n
WHERE n.nspname NOT IN ('pg_catalog','information_schema','pg_toast')
  AND n.nspname NOT LIKE 'pg_temp%%' AND n.nspname NOT LIKE 'pg_toast%%'
  AND has_schema_privilege(n.oid,'USAGE');

-- B. tables + 表注释（obj_description / pg_description）+ 尺寸 + 统计新鲜度
SELECT n.nspname, c.relname,
  CASE c.relkind WHEN 'r' THEN 'BASE TABLE' WHEN 'p' THEN 'BASE TABLE'
                 WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                 WHEN 'f' THEN 'FOREIGN TABLE' ELSE c.relkind::text END AS table_type,
  pg_catalog.obj_description(c.oid,'pg_class') AS comment,
  c.relam IN (SELECT oid FROM pg_am WHERE amname='heap') AS is_heap,
  pg_catalog.pg_total_relation_size(c.oid) AS size_bytes,
  COALESCE(s.reltuples::bigint, c.reltuples::bigint) AS approx_rows,
  s.last_analyze, s.last_autoanalyze
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
LEFT JOIN pg_catalog.pg_stat_user_tables s ON s.relid=c.oid
WHERE c.relkind IN ('r','p','v','m','f') AND n.nspname = ANY(%s) AND c.relname NOT LIKE 'pg_%%';

-- C. columns + 列注释（pg_attribute ⋈ pg_description）+ 域/枚举标签
SELECT n.nspname, c.relname, a.attnum, a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod) AS raw_data_type,
  t.typname AS data_type, (NOT a.attnotnull) AS nullable,
  pg_catalog.pg_get_expr(d.adbin, d.adrelid) AS default_value,
  a.attgenerated <> '' AS is_generated,
  col_description(a.attrelid, a.attnum) AS comment,
  CASE WHEN t.typtype='e' THEN (SELECT jsonb_agg(e.enumlabel ORDER BY e.enumsortorder)
                                FROM pg_enum e WHERE e.enumtypid=t.oid) END AS enum_values,
  CASE WHEN t.typname IN ('varchar','bpchar','numeric') THEN
       ARRAY[(a.atttypmod-4), facts.numeric_scale] END AS modifiers
FROM pg_catalog.pg_attribute a
JOIN pg_catalog.pg_class c ON c.oid=a.attrelid
JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
JOIN pg_catalog.pg_type t ON t.oid=a.atttypid
LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
WHERE a.attnum>0 AND NOT a.attisdropped AND n.nspname = ANY(%s) AND c.relname = ANY(%s);

-- D. indexes（pg_index / pg_class）
SELECT pn.nspname, tc.relname AS table_name, ic.relname AS index_name,
  i.indisunique, i.indisprimary, am.amname AS index_type,
  pg_catalog.obj_description(ic.oid,'pg_class') AS comment,
  s.idx_scan AS cardinality_hint,
  pg_get_indexdef(ic.oid) AS def,
  a.attname AS column_name, (x.ord)::int AS seq_in_index
FROM pg_index i
JOIN pg_class ic ON ic.oid=i.indexrelid
JOIN pg_am am ON am.oid=ic.relam
JOIN pg_class tc ON tc.oid=i.indrelid
JOIN pg_namespace pn ON pn.oid=tc.relnamespace
CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS x(attnum, ord)
LEFT JOIN pg_attribute a ON a.attrelid=tc.oid AND a.attnum=x.attnum
LEFT JOIN pg_stat_user_indexes s ON s.indexrelid=ic.oid
WHERE pn.nspname = ANY(%s) AND tc.relname = ANY(%s)
ORDER BY tc.relname, ic.relname, x.ord;
-- attnum=0 表示表达式索引列 → column_name 为 NULL，从 pg_get_indexdef 里回捞表达式

-- E. foreign keys（pg_constraint）
SELECT sn.nspname, rc.relname AS table_name, con.conname,
  att.attname AS from_column, (x.ord)::int AS seq,
  rn.nspname AS to_schema, rt.relname AS to_table,
  ta.attname AS to_column,
  CASE con.confdeltype WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT' WHEN 'c' THEN 'CASCADE'
       WHEN 'n' THEN 'SET NULL' WHEN 'd' THEN 'SET DEFAULT' END AS on_delete,
  con.confupdtype  -- 同理解析
FROM pg_constraint con
JOIN pg_class rc ON rc.oid=con.conrelid JOIN pg_namespace sn ON sn.oid=rc.relnamespace
JOIN pg_class rt ON rt.oid=con.confrelid JOIN pg_namespace rn ON rn.oid=rt.relnamespace
CROSS JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY AS x(colid, refid, ord)
JOIN pg_attribute att ON att.attrelid=rc.oid AND att.attnum=x.colid
JOIN pg_attribute ta ON ta.attrelid=rt.oid AND ta.attnum=x.refid
WHERE con.contype='f' AND sn.nspname = ANY(%s);
```

PG 侧 `indkey::int[]` 与 `unnest ... WITH ORDINALITY` 用来**保序**，`atttypmod` 用来归一
`numeric(10,2)` / `varchar(n)`。

## 9. 类型归一化

PG `format_type` → 内部 `data_type`：`character varying(64)→varchar(64)`、
`timestamp with time zone→timestamptz`、`double precision→float8`、`integer[]→int[]`。
实现为 `postgres_types.py::normalize()`，纯函数，重点测试。

单测要覆盖的归一映射：

- PG：`_text[]` / `numeric(10,2)` / `varchar(64)` / `timestamptz` / `jsonb` / `serial` / `generated always`
- MySQL：`decimal unsigned zerofill` / `enum('a','b')` / `set` / `tinyint(1)`（→bool 与否）/ `datetime(3)` /
  生成列 / `utf8mb4_0900_ai_ci`（8.0 的排序规则出现在输入时的容错）

## 10. MySQL 5.7 特有的两个坑（写进代码注释）

1. **中文注释乱码**：MySQL 5.7 的 `information_schema` 列走 `character_set_system_variables`
   （部分构建默认 utf8mb3），中文 `TABLE_COMMENT` 可能返回 `???`。
   检测：抽完第一批后统计 comment 里 `?` 占比 > 0.3 → 打 warning `CHARSET_SUSPECT`，
   并自动回退用 `SHOW CREATE TABLE` / `SHOW FULL COLUMNS` 逐表取注释（这两条走表的真实字符集）。
   连接必须 `charset='utf8mb4'`。
2. **`utf8mb4` 索引前缀 / 排序规则**：5.7 默认 `utf8mb4_general_ci`；不要用 `utf8mb4_0900_ai_ci`（那是 8.0），
   也别在 IS 查询里手写 `COLLATE`，否则 `%s` 字面量与 IS 列比较会撞 "Illegal mix of collations"。
   正则过滤用 `REGEXP` 而不是 `LIKE ... COLLATE`。

另外：5.7 的 `information_schema.STATISTICS` 对 MyISAM / 视图列语义不同，视图要走
`TABLES.table_type='VIEW'` 的单独分支（视图列注释全空，卡片会很丑 → 单独降级模板）。

验收锚点：`meta_index` 表里能看到 `CARDINALITY` / `SUB_PART` 非空——
这就证明没有退回 SQLAlchemy Inspector 的逐表 `SHOW CREATE TABLE` 方案。
