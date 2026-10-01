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
  (md5(datasource_id::text || <分隔符> || catalog_name || <分隔符> || schema_name || <分隔符> || table_name)) STORED,
UNIQUE (table_uid)
```

> as-built(0004)：**不能写 `concat_ws(<分隔符>, ...)`**。`concat_ws` 在 PG 里是 **STABLE**
> 而不是 IMMUTABLE（`select provolatile from pg_proc where proname='concat_ws'` 实测为 `s`；
> 同批实测 `md5`/`encode`/`chr` 都是 `i`），生成列要求表达式 IMMUTABLE，建表直接报
> `generation expression is not immutable`。四列都是 `NOT NULL text`，`||` 链与 `concat_ws` 等价。
> 另注意 `datasource_id` 是 `bigint`，`||` 不隐式转换，必须 `::text`。

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
last_analyze_at timestamptz NULL   -- 统计新鲜度；太旧提示 AI 别信 row count（取值口径见下方 as-built(P3-022)）
is_hidden bool DEFAULT false       -- 手工排除某些表（临时表/备份表 _old/_bak）
is_stale bool                      -- 本次同步未出现的表打陈旧标记，不物理删
synced_at / created_at
UNIQUE (datasource_id, catalog_name, schema_name, table_name)
```

> as-built(0004)：两处踩过坑，留在这里省别人一次调试。
> ① `collation` 是 **PG 的保留字**（`pg_get_keywords` 里有它），但 SQLAlchemy 的保留字表按
> SQL:2008 收，**不含 `collation`**——直接写 `Column("collation", Text)` 会生成不带引号的
> `collation`，建表报 `syntax error at or near "collation"`。ORM 与迁移两侧都要
> `quoted_name("collation", True)`（见 `app/models/meta.py` 的 `COLLATION_COL`）。
> ② §2.4 标题写"6 张"是对的，但按前缀数容易漏掉子表 **`meta_index_column`**——0004 落地的就是
> 六张 + `sync_jobs`（§2.5）。

> as-built(P3-022)：**`last_analyze_at` 的取值口径**（工单 022 拍板，MySQL 半边已落
> `rows_to_tables()`；这一列的注释语义是"统计新鲜度"，口径钉死在这里，方言层照抄）：
> ① **`last_analyze_at` = UPDATE_TIME 优先；UPDATE_TIME 为空则退回 CREATE_TIME；两者都空则 NULL**。
> 5.7 的 InnoDB `UPDATE_TIME` 在服务器重启后归 NULL，所以"退回 CREATE_TIME"是常态支而不是兜底摆设。
> ② **视图不给造新鲜度**：`table_type='VIEW'` 这一行恒 NULL——MySQL 里视图的 CREATE_TIME 是
> **定义时间**、UPDATE_TIME 常为 NULL，两者都不是"数据被更新"，填进去就是给卡片那句
> "最近更新 D"喂假日期的假新鲜度；NULL 在这里不是 bug。
> ③ **时区口径**：MySQL 的 `datetime` 无时区、本列是 `timestamptz`，转换必须显式——抽取层
> （`app/extractor/mysql.py` 的 `_stamp_to_aware`）按**源库所在机器时区**用 `zoneinfo` 显式
> attach 后交给下游，不许靠驱动的隐式转换（011 那批 `serialize_cell` 的时区教训同源）。
> 平台取不到 IANA 键时（如 Windows 的 `zoneinfo.TZPATH` 为空）退回机器当前 UTC 偏移的显式
> 固定偏移；演示库与 API 同机同区，该替换无信息损失。
> ④ **PG 侧预留**（落地归 024，本片不做——PG 抽取器今天还不存在）：原料是 §8.2 B 里
> `LEFT JOIN pg_catalog.pg_stat_user_tables` 带出的 `s.last_analyze, s.last_autoanalyze`，
> 两个值**取较晚者，都为 NULL 则 NULL**。

**`aiweb.meta_column`**

```
id / table_id FK CASCADE / ordinal_position int / column_name
data_type text            -- 归一化后：'int','bigint','varchar(64)','numeric(10,2)','datetime',...
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
counters jsonb DEFAULT '{}'   -- 交付的键（sync_service._Tally）：
                               -- {databases,tables,columns,indexes,relations_extracted,
                               --  relations_inferred,tables_stale,tables_failed,cards,batches}
                               -- （as-built(P3-016)：原记的 `{tables_seen,tables_ok,fks,cards_embedded}`
                               --   从未落地过——007 落前八个，`cards` 由 008 加；`fks` 那一格按 §5.3
                               --   的分类拆成 extracted/inferred，`cards_embedded` 要等向量启用（P4）。
                               --   改它而不是留着：017 的收尾帧要按 key 渲染这一格）
                               --   as-built(P3-020)：`batches` 是第十个键，说的是"这一轮读源库真的
                               --   发了几批"（跨库相加），不是"落了多少行"。它只进这一本行级总账，
                               --   不进 §2.8 那五键的进度账：进度条按对象数画百分比，批数对它没有意义）
warnings jsonb DEFAULT '[]'   -- [{code:'permission_hidden', detail:'库 x 下 12 张表不可见'}]
errors   jsonb DEFAULT '[]'   -- [{code, detail}]，分类过的 AppError 再多带一个 data
                               -- （as-built(P3-016)：原记的 `{phase, table, message}` 从未落地过，
                               --   007 起就是 `{code, detail}`，本片补上的是 `data`。进程分离之后
                               --   202 只带 job_id，这一列成了 §6 那三条结构化出路到达前端的唯一
                               --   一跳，只留人话就只剩一个 code 能看。`detail` 一律是字符串
                               --   （给人读的那一句），机器可渲染的结构住在 `data`——读这一列的人
                               --   不必为同一个键写两种分支，所以不让 detail 兼任结构）
force boolean NOT NULL DEFAULT false  -- as-built(P3-017 建列 / P3-021 接线)：迁移 0007 顺带建的列
                               -- （工单 021 数据层原话"并进迁移 0007，别为它单开一次迁移"）。
                               -- 017 只建列；021 接通了读写：端点收请求体 `{"force":true}`、
                               -- enqueue 落这一格、claim 随行带回、run_sync(force=) 真时不传
                               -- MAX_TABLES。默认 false 的意思是"没说就覆盖不了"，历史行不会
                               -- 因加列变成"曾被强制覆盖过"。实装是请求体而 §6 文案字面是
                               -- `?force=true`，这处形状差记在工单 021 交付记录，
                               -- 措辞统一归 025 收口
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
    -- as-built(0008)：这里的 `|` 是示意，实际拼接用 **单元分隔符 chr(31)**，与 ADR-0005 给
    -- `table_uid` 定的同一套：名字/段号里真出现 `|` 时，竖线分隔会让两组不同输入撞出同一个 md5，
    -- 而 chr(31) 不可能出现在标识符里。算法住在 `kb_service.card_doc_uid()`，别处不重算。
title text                  -- 'db.orders（订单表）'
text_md  text NOT NULL      -- 送 embedding 的完整文档
search_text text NOT NULL   -- 关键词检索用（标识符+注释+值，去 markdown 噪声）
token_count int NOT NULL
meta jsonb DEFAULT '{}'     -- {approx_rows, column_count, tags, confidence}
index_profile_id bigint NOT NULL
embedding vector(dim)      -- dim = settings.embedding.dimension，迁移里不硬编码
                             -- as-built(0005)：类型取自 `pgvector.sqlalchemy.Vector(dim)`，
                             -- **不是**手写 `op.execute("vector(%d)")`——渲染结果同为 `vector(1536)`，
                             -- 但走类型层才能让 `alembic` 的 ORM↔迁移比对和 `Base.metadata` 说同一件事
                             -- 可插拔增强；未启用向量端点时全 NULL 不影响链路
                             -- as-built(0005)：dim 必须 >0——`vector(0)` 非法，所以"未启用向量"
                             -- 由 embedding.configured（base_url/key/model 齐不齐）判定，不由 dim=0 判定
embedded_at timestamptz NULL
sync_job_id / created_at / updated_at / deleted_at
```

```sql
-- 向量索引（as-built(0005)：随建表同批建，不等"启用 embedding profile"——
-- HNSW 可以建在空表上（这正是选它而非 IVFFlat 的理由），空表建好省掉 P4 再改迁移）
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
>
> as-built(0008) 四条落地口径：
> ① **P2 只落一条行，且直接 `is_active=true`**：`ensure_profile()` 按 `name` 幂等复用，
> 没有 draft/retired 状态机（那是上面那句"原子切换"，属 P4）。所以 `kb.status` 语义在 P2 就是
> "当前配置那套 = 唯一那套"，检索侧还谈不上选 profile。
> ② `name` 的三段里 model 缺省时用占位字面 **`no-embedding`**（未配端点时 `settings.embedding.model`
> 为空串，而 `name` 是 UNIQUE 键，不能是 `@1536@tplv1` 这种以分隔符开头的残串）。
> ③ `card_template_version` 是 **int**（§2.6 的列类型），`tplv1` 那个带前缀的形态只出现在 `name` 里。
> ④ `provider` 恒写 **`openai_compatible`**（architecture §5 只认 OpenAI 兼容端点，没有第二家可填），
> `distance_fn` 由 `profile_row()` **省略**、落 §2.6 列上的 `DEFAULT 'cosine'`——不在代码里重述默认值，
> 将来真出现非 cosine 的 profile 时改列默认就行。
> 每次同步行权的位置在 `sync_service` 的 `card_build` 阶段：`index_profile_id` **不进** upsert 的
> `set_`，因为 `doc_uid` 里已经拌了它——让它可改等于允许一轮同步把卡从旧 profile 搬到新 profile，
> 而 ①那条"两套各一批行、可回滚"就没了。

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
  sql_final text NULL                           -- guard 重写后实际执行的（顶层 LIMIT 缺则补、超则钳，见 safety §1.2 ⑦）
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

> **as-built(0006 / P2-012)**：这两张表由工单 012 建（迁移 `0006_chat`），`chat_feedback` **不建**——
> 它是 few-shot 采纳的来源，而采纳入口在 P9。P2 落地时有四格的真实状态要写清楚，否则下一片会
> 以为这些列已经在动了：
> ① `question_resolved` 恒 NULL——① 改写这一步的开关 `AIWEB_RETRIEVAL__ENABLE_REWRITE` 缺省就是
> `false`（省一次 LLM 往返），关掉时"原文"与"对齐后"是同一句，写两份是冗余而不是信息。
> ② `retrieved` 里的 `score_vec`/`fused` 恒 `null`，只有 `score_kw` 有值——L1 是唯一的检索档，
> 向量与 RRF 融合是 P4。这正是要留着这一列的理由：**可解释性要能区分"没命中"和"没算"**。
> ③ `prompt_tokens`/`completion_tokens` P2 恒 `0`（列建 NOT NULL）。不是忘了，是原料不存在：
> `llm_client.complete()` 只返回 `choices[0].message.content`，响应体里的 `usage` 被丢掉了。
> 真 usage 归 P8（流式那侧本来就要从末帧取）， heuristic 估算**不许**写进这一列——它是审计列，
> 装一个估算值就等于谎报。
> ④ `executed`/`error_code`/`error_message` 是早退分支的落点：检索为空、畸形返回、守卫拒绝三类
> 都必须留下一行（`executed=false`），"拒了但没痕迹"等于没拒。
> ⑤ `result_columns` 上面称"列定义"，实际**只有 `{"name": …}`**——行出 executor 时已过
> `serialize_cell`，`Decimal`/`datetime` 的源库类型在那一刻就丢了，填不进列的东西不在列里写。
> `test_chat_pg.py` 里原本那个 `{"name":…, "type":…}` 夹具字面同步改成只有 `name`，
> 否则两处形状各自长，P8 的渲染层会按不存在的那一格去接。
> ⑥ `result_stats.elapsed_ms` 记的是 **`execute` 那一步**的耗时，不是整条链的合计（合计在
> `latency_ms`）。两列分开才答得出"慢在模型还是慢在源库"。同键里还多一个 `run_id`——
> 它是结果文件的身份，013 的下载侧只认这个引用，不认路径。

### 2.8 `aiweb.sync_job_event`（as-built(P3 开工前拍板) 新增，迁移 `0007_sync_event`）

```
id       bigserial PK
job_id   bigint FK sync_jobs ON DELETE CASCADE  NOT NULL
seq      bigint NOT NULL                 -- 每个 job 内单调 +1，SSE 的游标就是它
stage    text NOT NULL CHECK ('extract','embed','upsert','card_build','done')
phase    text NULL                       -- 细粒度真相（9 值词表里的那一个），排障用
counters jsonb NOT NULL DEFAULT '{}'     -- 该事件时刻的累计账（done/total/base_table/view/cards…）
payload  jsonb NOT NULL DEFAULT '{}'     -- {table:'db.t', code:'card_build_failed', detail:…, skipped:bool}
created_at timestamptz NOT NULL DEFAULT now()
UNIQUE (job_id, seq)
INDEX (job_id, seq)             -- as-built(P3-017)：不单独建，见下方 ⑤
```

只追加、不改写：一行代表"作业状态发生过一次可对外说明的变化"。它是 P3 进度的**唯一真相**，
而 `sync_jobs.progress` 只是一个百分比（`numeric(5,2)`），两者不互相代替。

> ① **`stage` 与 `phase` 并存是有意的**（ADR-0010）：粗粒度给 SSE 与前端进度条，细粒度给排障。
>    两套词表之间的映射**只许住在一个纯函数里**，任何第二处重复它的位置都是 bug——
>    010 那次数出三处 `>= 0.8` 判定，就是同一个门槛散在多处后的形状。
> ② **`NOTIFY` 不带这些字段**。通道 `sync_job_event` 的 payload 只有 `job_id`，作用是把正在
>    `LISTEN` 的 SSE 连接叫醒，让它自己去按 `seq > cursor` 追读。原因写在 ADR-0010：
>    `NOTIFY` 在没有监听者的那一刻永久丢失，asyncpg 重连也不补，所以它不配当真相。
> ③ **保留期**由 worker 的回收循环按 `AIWEB_RESULT__RETENTION_DAYS`（30 天）删旧行——
>    这张表是全仓唯一一张确定会随时间线性增长的表，而"保留 30 天"这句配置承诺此前没有任何读取点。
>    结果 csv **不在本表的管辖范围**，也不在回收范围内（删文件不可逆，且 P2 的现场证据就在里面）。
> ④ `ON DELETE CASCADE` 到 `sync_jobs`：作业行被人删掉时事件跟着走，不留孤儿。
>
> **as-built(P3-017 已交付)**：
> ⑤ 上面那行 `INDEX (job_id, seq)` **没有单独建**，是刻意的：`UNIQUE (job_id, seq)` 本身就是一棵
>    按这两个字段排序的 b-tree，而读侧唯一那条查询正是 `WHERE job_id=:j AND seq>:c ORDER BY seq`
>    （`core/sse.read_events`）。再补一条同键索引只多一份写放大，换不到任何一次不同的扫描。
> ⑥ **一帧一档，`counters` 说的是"那一刻"**：本轮写事件的调用点是五处——`discover`（→extract）、
>    每个库的 `tables`（→upsert，与那次元数据写入**同一个事务**，见 §6 as-built(P3 开工前拍板) 第 5 条）、
>    `card_build`、`embed`、`done`。所以 `card_build` 那一帧的 `cards` 是 **0**：它发在
>    `sync_cards()` 之前，那一刻一张卡都还没落成，卡片数从 `embed` 帧起才是实际条数。
>    拿最终卡片数去断 `card_build` 帧会把这条口径记反（`test_sync_events_live.py` 钉的就是 `[0,0,0,12,12]`）。
> ⑦ `seq` 由写侧在**同一事务**里取 `max(seq)+1`（`sync_service._add_event`），`UNIQUE` 只是兜住
>    并发的那一层。不需要序列号分配器：同一个作业同一时刻只有一个 worker 在写它（§6 的 claim）。
> ⑧ `NOTIFY` 与事件行**同事务**发出。PG 的事务型 `NOTIFY` 到 COMMIT 才投递，所以"被叫醒"天然蕴含
>    "那一行已可见"；写进同一个事务是为了让回滚的那一帧连带把叫醒一起撤掉——否则会出现
>    "流被叫醒、读到的还是旧游标"的空转。读侧本来靠游标补读能自愈，但没有理由留着这个窗口。
> ⑨ **as-built(P3-020)：`tables` 那一帧的 `payload` 多带一格 `batches`** ——
>    `[[这一批的表名...], [下一批...]]`，顺序即发出顺序（原料是 `SourceManifest.batches`）。
>    它是**每批一条记录**而不是**每批一帧**：本表 ⑥ 说的那五个发帧点没变，因为分批不许改提交边界
>    （§6 as-built(P3 开工前拍板) 第 5 条），而事件行与元数据同事务——多加的批次帧只会在同一次
>    COMMIT 后一起可见，把流拉长却不让进度条早一格动。要"每批立刻可见"得先让每批立刻提交，那是
>    `stream_manifest`（§7 as-built(007) 第 2 条）的活，不归 020。

## 3. 人工列与同步列的分离（幂等重跑的关键）

`comment_raw` 由同步写，`comment_zh` / `business_desc` / `granularity` / `is_hidden` 是人工字段，
**同步永不冲掉**。三段式 upsert（每个 batch 一个事务——这里的 batch 指**一个 catalog 一次
`collect`**，即 §6 的失败隔离单位；它与工单 020 那个"读侧发送批"不是一个东西，后者不另起事务）：

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
-- 2a) 字段：自然键 (table_id, column_name) upsert，人工列同样 COALESCE 保护
INSERT INTO aiweb.meta_column (table_id, column_name, <同步列...>, comment_zh, business_desc)
VALUES (...)
ON CONFLICT (table_id, column_name) DO UPDATE SET
  ordinal_position = EXCLUDED.ordinal_position,
  data_type / raw_data_type / nullable / default_value / is_generated /
  comment_raw / is_primary_key / is_unique / is_indexed / enum_values /
  char_length / numeric_precision / numeric_scale   = EXCLUDED.<同名列>,
  comment_zh    = COALESCE(aiweb.meta_column.comment_zh,    EXCLUDED.comment_zh),
  business_desc = COALESCE(aiweb.meta_column.business_desc, EXCLUDED.business_desc),
  synced_at     = now();
-- upsert 不会碰"源库已删掉"的列，全量替换的语义靠这一步补回来（见下方要点）
DELETE FROM aiweb.meta_column
 WHERE table_id = ANY(%ids) AND synced_at < %this_sync_started_at;
-- 2b) 索引：delete-then-insert per table（这一对子表才真的无人工字段）
DELETE FROM aiweb.meta_index WHERE table_id = ANY(%ids);  INSERT ...
-- 3) 关系：只删 extracted，manual/inferred 各自走自己的重建逻辑
DELETE FROM aiweb.meta_relation
 WHERE datasource_id=%d AND source_kind='extracted'
   AND from_table_id IN (本轮写完的那些库的表)   -- as-built(0007)：差分范围是库，不是数据源（见 §4）
   AND id NOT IN (刚 upsert 的 id 集合);   -- 用 CTE: with keep as (insert ... returning id) delete where id not in (select id from keep)
```

语义要点：

- `COALESCE(现有值, 新值)` = "库里已有人工值就保留，只有为空时才接受新值"。
  `is_hidden` 直接原样保留（人工开关，连新值都不接受）。
- **as-built(0007)：原文这一条写的是"子表（列/索引）无人工字段，所以 delete-then-insert 最简"，
  与 §2.4 自相矛盾**——`meta_column` 就带 `comment_zh` / `business_desc` 两个人工列（§2.4 明确列了它们，
  而 §3 的人工字段清单也包含这两个名字）。对 `meta_column` 走 delete-then-insert 等于**每次点"同步"
  就把字段级的中文补录全清光**，幂等重跑的核心承诺当场失效。
  修正后的口径：**只有 `meta_index` / `meta_index_column` 无人工字段**（`comment` / `is_visible` /
  `cardinality` / `sub_part` 全部来自 `STATISTICS`，是同步列），它们才用 delete-then-insert；
  `meta_column` 与主表同构，走自然键 upsert。
- upsert 换不来"删掉的列消失"这件事，所以补一个 `synced_at < 本轮同步开始时刻` 的清扫。
  比较基准必须取**本轮同步开始前**捕获的时间戳（用 `sync_jobs.started_at`），不能用语句里的
  `now()`：PG 的 `now()` 是事务时间戳，同一事务内恒定，拿它和自己比永远不成立。
- `enum_values` 归同步列而不是人工列：它是源库 `COLUMN_TYPE` 的投影（§8.1 C 的 `enum_def`），
  同步每次都能重算出来，人工改它没有意义（要改语义写 `comment_zh`）。

## 4. `meta_relation.source_kind` 与删除差分

- 三种来源优先级：`manual` > `extracted` > `inferred`。
- **delete-diff 只作用于 `source_kind='extracted'`**：
  > 关键：inferred 与 manual **不参与同步删除**（同步只按 `source_kind='extracted'` 做 delete-diff），
  > 否则每次点"同步"人工补录的关系全没了。
- `inferred` 由命名约定打分产生（`confidence` ≥0.8 才进 prompt，标签 `[推断,置信 0.87]`），
  UI 可一键 accept → 转成 `manual`（`GET /metadata/relations/inferred` + `POST /metadata/relations`）。
- `extracted` 不可在 UI 删除（`DELETE /metadata/relations/{id}` 只允许 manual/inferred）。
- `is_authors_enforced` 记录"外键存在但库没启用 FK 约束"这类现实情况。
- **as-built(0007)：delete-diff 的范围是"本轮写完的那些库"**（`from_table_id IN (这些库的表)`），
  不是整个数据源。范围放到数据源级时，同一个同步里**后写的库会把先写的库刚落的 extracted 边
  整批收走**——演示库只有一个 schema，这条缺陷在里面永远看不见，多 schema 的库里边只剩
  最后一个库的。两个方向都要钉（`test_sync_pg.py::test_多库同步时后写的库不能删光先写的库的边`）：
  还在的外键不许被收走，源库删掉的必须被收走。
- **as-built(0007)：外键为 0 也要走差分的另一半**（纯 DELETE 清扫，`meta_relation_prune()`）。
  §3 第 3 段那句 CTE 是"upsert + 顺手删差集"，零行时 upsert 什么都不做、DELETE 也跟着不跑
  （executemany 空列表不执行任何语句）——源库删光外键后上一轮的 extracted 边就永远残留。
  所以 `_write_catalog` 对关系没有"跳过"这条路：有行走 replace，无行走 prune。

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
| 进程崩溃留下僵尸 running | worker 心跳循环**每轮**扫一次：`UPDATE sync_jobs SET status='failed', errors = errors || '[{"code":"reclaimed","detail":"heartbeat 超时"}]'::jsonb WHERE status IN ('pending','running') AND heartbeat_at < now() - interval '180 seconds'`；之后用户可重新发起。**as-built(P3 开工前拍板)**：原句写的是"启动 `lifespan` 里回收"与 `error='reclaimed on startup'` 两处失实——① 列名是 `errors`（`jsonb NOT NULL DEFAULT '[]'`），没有 `error` 这一列；② worker 长驻不重启是常态，只扫一次等于僵尸行永久占住 `ux_sync_running`，正是 as-built(007) 第 1 条堵过的那类故障换了个进程而已 |
| 抽取把源库拖垮 | 全部 IS 查询前 `SET SESSION max_execution_time` / `SET LOCAL statement_timeout`；批大小默认 200（`AIWEB_EXTRACT__BATCH_SIZE`，020 起有读取点）；批间 `await asyncio.sleep(EXTRACT__BATCH_INTERVAL_MS)`；单连接串行，不开并发打源库 |

对应配置：`AIWEB_EXTRACT__HEARTBEAT_INTERVAL_S=10`、`AIWEB_EXTRACT__STALE_JOB_RECLAIM_S=180`
（体检/自检里 heartbeat_at 超过该值判僵尸并在启动时回收）、`AIWEB_EXTRACT__BATCH_SIZE=200`
（**as-built(P3-020) 更正**：原句"每批一个事务，也是 `partial` 的粒度"从未成立过，也不该成立——
批是**读源库的发送切片**，一个 catalog 的几批共用同一个元数据事务，`partial` 的粒度仍是 catalog
（上面拍板第 5 条）。改它需要先把 `collect` 换成 `stream_manifest` 的边抽边 yield，那不是 020），
`AIWEB_EXTRACT__MAX_TABLES=2000`、`AIWEB_EXTRACT__MIN_MYSQL_VERSION=5.7`、
`AIWEB_EXTRACT__MIN_PG_VERSION=12`。

三条实现约束：

1. `heartbeat_at` 必须在**独立连接**上更新——主事务未提交时进度才可见（否则前端进度条 3 分钟不动）。
2. 批内一事务，但**源库读和元数据库写不要混在一个 session**（跨两个 engine）。
3. `manifest_digest` 命中上次成功指纹时短路，避免无变化的重复重建。

> **as-built(0007)**：P2 真跑之后，上表有三处需要按实现口径收紧，其中第一条是缺陷修复而非偏差。
>
> 1. **终局状态写在 `finally` 里，不是 try 的末尾**。上表只安排了"进程崩溃留下僵尸 running"的
>    启动回收，前提是不重启就永远撞不到——但元数据库还没开通时进程确实不重启，而 `run_sync`
>    一旦让**没分类过的异常**（pymysql 的驱动异常、`StatementError`…）裸冒到端点，那行 job 就
>    永远停在 `'running'`；`ux_sync_running` 是**部分唯一索引**，于是这条源此后每次同步都 409，
>    用户侧表现为"这个源再也点不动了"，只能手工进库删行才能救。因此终局收尾必须在 `finally`，
>    并且除了 `AppError`/`SQLAlchemyError` 两支之外还要有一支"没分类过的异常"：落 `failed` +
>    `errors[{code:'sync_failed'}]` 后再抛。**任何 raise 路径都不例外。**
> 2. **`partial` 的粒度是 catalog（一个 schema 一次 `collect` + 一个事务），不是"某张表"**。
>    上表那行"中途某张表 IS 查询失败 → 继续下一批"讲的是 P3 的分批（`BATCH_SIZE=200`）语义；
>    P2 没有分批，所以失败的原子单位就是整个 schema。
> 3. **`counters` 只累计确实提交完了的那个 catalog**。边写边涨的总账会在"中途 rollback、
>    一行都没落"时谎报"10 张表同步好了"——分账 (`_Tally`) 写完由调用方在提交成功后并进总账。
>    `ds.last_sync_at` 同一条规矩：只有走完全程才推它，失败的同步不该把"最近一次同步"往前挪。
> 4. `ExtractScopeTooLarge` 是**拒绝开工**，不是"某一批失败"：它必须穿过 per-catalog 的兜底
>    `except` 让整个请求以 400 结束，且 `detail` 里那三条出路要原样到达响应体（前端按结构化
>    出路渲染，只剩一个 `code` 就没有可操作性了）。
>    **as-built(P3-016)**：本条两处都随进程分离改了落点——请求在抛错之前就已经以 202 返回了，
>    所以"整个请求 400"变成"**整个作业 `failed`**"（穿过 per-catalog 兜底这一半不变，
>    见 `run_sync` 里那句 `except ExtractScopeTooLarge: raise`），"原样到达响应体"变成
>    "原样到达 `sync_jobs.errors[0].data.remedies`"。钉子从 400 改成 202 + 三条文案逐字相等，
>    落 `tests/integration/test_sync_pg.py`。
> 5. `?force=true`（旧表格里 admin 覆盖上限那条）：`run_sync(force=)` 参数在，
>    **端点没有开关**，工单 007 的验收里没有它 → P3 接背景执行时一并补。开关落地前，
>    第三条出路的文案是"调高 `AIWEB_EXTRACT__MAX_TABLES` 上限（admin 改配置）"——出路必须
>    **当下可操作**，指向一个不存在的开关等于让人猜（三条出路的原文由 `SCOPE_REMEDIES`
>    常量统一，`test_extract_mysql_client.py` 按本表钉字面量）。P3 落地时把这条文案换回
>    "admin 用 `?force=true` 覆盖上限"并同步改常量与用例。
>    同理 `AIWEB_EXTRACT__MIN_MYSQL_VERSION`（第 1 行）也还没有读取点。

> **as-built(P3 开工前拍板)**（2026-09-30，ADR-0010 / ADR-0011；共识由 grill 逐条走完）
>
> 1. **执行模型换成独立 worker 进程**。`POST /api/sync/jobs` 只写 `pending` 行 + 返回 202 与 `job_id`，
>    `run_sync` 归 `scripts/run_worker.py`。理由不是"背景执行更高级"，是 `AIWEB_RELOAD=true` 是本机默认，
>    进程内跑作业等于**存一次盘打断一次同步**，而被打断的 `running` 会占住 `ux_sync_running`
>    让该源永久 409（上表 as-built(0007) 第 1 条那个故障的另一个来源）。
>    路径沿用 P2 的 `POST /api/sync/jobs`（工单 007 拍板："只换返回码不动路径"），
>    `architecture.md` §7 端点表里 `POST /datasources/{id}/sync` 那一行的路径按此作废。
> 2. **阶段词表是两套，且映射只能住在一个地方**。`sync_jobs.phase` 继续写 9 值细粒度（迁移不动，
>    CHECK 不改）；对外（SSE）用粗粒度 `stage ∈ {extract, embed, upsert, done}`。
>    **as-built(P3-017)**：这里当场拍的是四个值，落地是**五个**——多了 `card_build`。判据不是偏好：
>    roadmap P3 验收 2 与工单 017 的"已定口径"两处都写着 `card_build`，而 §2.8 的 CHECK 是它的库内落点
>    （已随迁移 0007 建好）；少了这一档，进度条从"元数据写完了"直接跳到"在做向量"，
>    卡片这一段（本机最慢的一步）对外就是黑的。多出来的这一格只改词表，不改映射规则本身。
>    roadmap P3 验收 2 那句 `phase=extract/embed/upsert` 里的 `extract`/`upsert` **不在** CHECK 里，
>    按本条收口：worker 写库用细值，写事件用粗值，两者之间唯一的映射是一个纯函数。
> 3. **进度真相在 `sync_job_event`，不在 `progress` 列**。新表见 §2.8。`NOTIFY` 只携带 `job_id`
>    当叫醒信号（`NOTIFY` 在无监听者的那一刻永久丢失，所以它不配当真相）；SSE 端点按 `seq` 游标追读，
>    重连不丢事件。`progress numeric(5,2)` 那一列仍然只是一个百分比，不承载序列。
> 4. **心跳与回收都在 worker 的心跳循环里**：独立连接每 `HEARTBEAT_INTERVAL_S`（10s）刷 `heartbeat_at`，
>    **同一轮**扫一次僵尸（阈值 `STALE_JOB_RECLAIM_S`，180s）。原表那句"启动 lifespan 里回收"的
>    管辖对象从 API 进程变成 worker，且"只在启动扫一次"被否掉——worker 长驻不重启是常态。
>    两个键（`heartbeat_interval_s` / `stale_job_reclaim_s`）从"定义了点都没有读取点"变成有读取点。
>    上表第 1 行那条 `UNSUPPORTED_VERSION`（`MIN_MYSQL_VERSION` / `MIN_PG_VERSION`）**本片仍不接**，
>    连同"权限不全 `SCHEMA_PARTIALLY_VISIBLE` + `known_complete` 门控"与 `manifest_digest` 短路
>    一起转 P6/P10——PG 抽取器这轮落地而不做版本判定是**明示的偏离**，不是遗漏。
> 5. **失败隔离粒度定在"元数据每库一事务（现状不动）+ 卡片逐表提交"**。验收 5 那句
>    "3 张表抛错、其余 6 张已落库"之所以成立，是因为抛错发生在卡片阶段，而卡片改成逐表提交；
>    元数据侧一旦某张表的 IS 查询真抛错，仍然是整库回滚 + `partial`。
> 6. **`embed` 档在 P3 是 `skipped`**。本机 embedding 三键（`BASE_URL`/`API_KEY`/`MODEL`）全空，
>    `settings.embedding.configured` 为 False，事件流里该档带 `skipped=true` 与原因，
>    向量回填整块归 P4。验收 5 的注入点因此改用 `card_build`（008 的渲染器逐表调用）。
> 7. **`?force=true` 本片接**（上表第 5 条的那笔账到此闭环）：端点加 `force` 参数透传
>    `run_sync(force=)`，同时把 `SCOPE_REMEDIES` 第三条文案换回"admin 用 `?force=true` 覆盖上限"，
>    §6 表 / 常量 / `test_extract_mysql_client.py` 三处一起动。
> 8. **`cancel` 不做**，端点表那行挂 [P8]（`sync_jobs` 至今**没有** `cancel_requested` 列，
>    加列与批间检查点等 P8 前端一起做）；`status='cancelled'` 与 `'pending'` 两个 CHECK 值里，
>    `pending` 因本片的入队动作终于有了写入点，`cancelled` 继续没有。
> 9. **保留期作业只清 `sync_job_event`**（按 `AIWEB_RESULT__RETENTION_DAYS`，30 天），结果 csv 一行不碰
>    ——那些文件是 P2 验收 4/5 的现场证据，且删文件不可逆；结果文件的回收与 P8 下载端点一起考虑。
>    这个键的名字目前比它的管辖范围大，按本条写明。
> 10. **枚举 NDV 采样整块推 P4**：新增 `AIWEB_EXTRACT__SAMPLE_ROW_LIMIT` 键（此前**不存在**，
>    而 kb-workflow §8 把它当已有键在讲），`sample_distinct` / `sample_distinct_max_distinct`
>    两键挂 [P4]，`meta_column.sample_values` 继续无写入点。
> 11. **读侧真分批**：`collect()` 按 `BATCH_SIZE` 切片循环发那几条 `IN (...)` SQL，批间
>    `sleep(BATCH_INTERVAL_MS)`。演示库只有 11 个对象，所以用例把 `BATCH_SIZE` 降到 3，
>    在真库上跑出 4 批——这两个键不再是空转，而"分批"这件事也因此真的被验过。
>
> **as-built(P3-016 施工后)**（2026-09-30，工单 016；上面的拍板逐条落地，这里是施工现实）
>
> 1. 上面第 1 条已交付，`_open_job` 拆成了两条语句：`enqueue_stmt`（API 进程，只写
>    `pending` + `RETURNING id`）与 `claim_stmt`（worker 进程，`FOR UPDATE SKIP LOCKED`
>    选最老 + 外层 `AND status='pending'` 定胜负）。执行体在 `scripts/run_worker.py`，
>    本机从此两条命令（`dev.ps1 dev` / `dev.ps1 worker`，`make worker` 同义）。
> 2. **409 的唯一来源从此是索引**：P2 那句"`_open_job` 撞 `ux_sync_running` → `Conflict`"
>    连同 `_open_job` 一起没了，`enqueue` 不查"有没有未结束的作业"，它只往表里插一行。
>    于是 `status IN ('pending','running')` 里那个 `pending` 第一次真的挡人——
>    排队中的作业也挡住新作业（上面第 8 条说的"互斥面反而变大"）。钉子：
>    `test_排队中的_pending_行也挡住新作业`（把索引的 WHERE 改成只剩 `running` 它就红）。
> 3. 上面第 8 条的"`pending` 有了写入点"已真跑验证（两终端时间线见工单 016 交付记录）。
>    顺带一处搬家：`heartbeat_at` 的写入点从"建 job 时"（P2 与 `started_at` 同一句）移到了
>    **claim 时**，仍然只写一次——上面第 4 条要求的"独立连接每 10s 刷"仍归 018。移的理由是
>    僵尸回收判的是"开跑了却不再动"，而入队时刻还没有任何一帧属于执行。
> 4. `JobQueue` Protocol 只有 `enqueue` / `claim` 两个成员：`subscribe` 要和新表
>    `sync_job_event` 同时出现才有意义，在这一片挂进来就是一个没有人实现的空方法（归 017）。
> 5. worker 的循环体 `run_once(session)` 是用例的进入点（已定口径"不真起子进程"），
>    会话由调用方给、也由调用方关——018 的心跳要在**另一条连接**上刷，届时看的就是谁握着会话。
>    作业级异常收在 `run_once` 而不是 `loop()`：收在 loop 里，用例跑的那条路就少了一层
>    真进程有的保护，而 `run_sync` 的不变量恰恰是"终局写完之后把原因原样抛出去"。
>
> **as-built(P3-017 施工后)**（2026-09-30，工单 017；帧语义见 §2.8 的同名标注，这里只记写侧）
>
> 1. 上面第 4 条挂的那笔账到此闭环：`JobQueue` Protocol 现在是 `enqueue` / `claim` / **`subscribe`**
>    三个成员，`subscribe(job_id)` 返回一个 `JobWake`。句柄这个类型住在 `core/sse.py`，置位它的
>    回调住在 `services/job_queue.py`——依赖方向仍然是 services → core，换掉队列介质时不动读侧。
>    同一条理由管着另一件东西：`phase`→`stage` 的那份词表读侧也要用（流的终局判定），
>    所以它住在 `core/sync_vocabulary.py` 而**不是** `services/`——core 反向 import services
>    会让这一行写的方向当场失效。
> 2. **`LISTEN` 必须挂在一条专用连接上**，借用会话自己那条会静默失效：读循环每轮都要
>    `rollback()`（它凭什么一直读到新事件？就是靠重开快照），而归还掉的连接在池化下会被交给
>    下一个借用者（那位没订过阅）、在 NullPool 下被直接关闭。两种结局都是"进度永远不动"，
>    且一条都不报错。付的代价是每条 SSE 流占两条连接，写在本片交付记录里。
> 3. 引擎要从 `session.bind` 取，**不是 `session.get_bind()`**：后者在 `Session` 层就把引擎拆成了
>    同步 `Engine`（本机 `type()` 实测），对它的 `.connect()` 在没有 greenlet 的上下文里发起真 IO，
>    抛 `MissingGreenlet`。这一条是被这个异常逼出来的，不是风格选择。
> 4. **写事件只有一个入口**（`sync_service._add_event`），而它只被 `_set_phase(event_counters=…)`
>    调用——不是另开一处，这正是工单 017 "涉及层"要求的那个形状。`_set_phase` 因此多了 `commit=False`：
>    那一档的 phase 写、事件写、`meta_*` 写并进同一次 commit（上面"已定口径"的 D1），于是"库失败回滚 →
>    那一帧跟着消失"由事务保证，而不是由代码顺序保证。
> 5. **分母先于第一帧**（D2）：`Extractor.count_scope()` 跑在 `discover` **之后**、第一帧之前
>    （它要吃 `discover` 回来并经过 include_schemas 过滤的那批 catalog，不然连该数哪几个库都不知道），
>    所以每一帧都说得出 `x/y`。它只发分组计数那一条 SQL，昂贵的 IS/列/索引一条都不发。演示库上
>    `total=10` 而 `SHOW FULL TABLES` 数出 11，差的正是下划线前缀那几个建库脚本内部对象——
>    分母口径与 §2.5 那本行级总账（`_Tally`）是两本账，故意不合并（`_Progress` 的理由写在源码里）。
>
> **as-built(P3-019 施工后)**（2026-09-30，工单 019；上面第 5 条的卡片半边落地，元数据半边原样不动）
>
> 1. 事务边界挪进 `sync_cards` 的循环体：**一张表一次 `execute` + 一次 `commit`**，失败的那张
>    自己 `rollback`（回滚射程只覆盖它自己——前面已提交的卡片不在里面）。旧形状"攒够全部 rows
>    再一次 upsert、由调用方单次提交"没了，`run_sync` 的 card_build 段因此**不再**为卡片发 commit；
>    §6 已定口径那句"`_set_phase` 自己会 commit，卡片提交与 phase 提交不许并成一个事务"
>    由"这里根本没有卡片提交"来满足，而不是由顺序满足。
> 2. `ensure_profile` 后面跟着一次**单独的 commit**：它若还留在第一张表的事务里，那张表失败时的
>    rollback 会把它一起带走，此后每张表的成功提交都在 `kb_card.index_profile_id` 的外键上当场拒收。
>    这是逐表提交换来的、旧形状里不存在的一步。
> 3. `sync_cards` 的返回值从 `int` 变成 `CardBuildOutcome(cards, failures)`：`cards` 仍只数**已提交**
>    的条数（上面第 5 条的"只改事务边界、不改计数"），`failures` 带 `table_full_name`。调用方把它
>    逐条转成 `errors` 的既有形状 `{code:'card_build_failed', detail:'<表全名>: <原句>'}` 并把终局判
>    `partial`——与库级失败同一条追加路径，没有第二种错误形状。
> 4. 注入点是参数不是分支：`run_once(session, *, card_build_hook=…)` → `run_sync(card_build_hook=…)`
>    → `sync_cards(card_build_hook=…)`，在每张表构建的**起点**用表全名调用。生产 `loop()` 不传，
>    源码里 grep 不到"如果这是测试就抛错"。
>
> **as-built(P3-018 施工后)**（2026-09-30，工单 018；上面第 4 条与第 9 条落地，真跑见工单 018 交付记录）
>
> 1. **心跳是一个独立的并发任务，不是"跑完一个作业顺手刷一次"**。`loop()` 里 `beat_forever` 与
>    主循环并行，共享的只有 `_Current` 那一格（这个进程**此刻**在跑哪个作业，由 `run_once` 的
>    `on_claim` 在领取成功那一刻点上）。上面第 4 条说的"独立连接"落到代码上的形状是：
>    心跳走 `create_engine(poolclass=NullPool)` 现开的第二条引擎——每次现开一条连接、还回去就关掉，
>    与主会话**结构上不可能**共用一条连接，而不是"池子大概不会把同一条借出去"。
>    为此 `core/db.py` 的 `create_engine` 补了一处配合：换了池型就把 `pool_size` / `max_overflow`
>    摘掉，NullPool 收到 QueuePool 的两个尺寸参数会当场抛 `InvalidRequestError`。
> 2. **一轮心跳做三件事，各自提交**：刷自己的钟 → 扫僵尸 → 清过期事件。刷钟不等回收，
>    回收补的终局事件不等保留期；唯独**回收的 UPDATE 与它补的那条事件在同一个事务**——
>    分开提交会留下"已改判 `failed`、读侧却永远等不到最后一帧"的窗口，而那正是 017 的流式读侧最怕的形状。
> 3. **`pending` 在候选里，但判据是心跳而不是存在时长**：从没被领取的行 `heartbeat_at` 是 NULL，
>    匹配不上 `< now() - interval` 这个比较，天然排除在"超时"之外。躺很久的排队行说明的是"没有 worker"，
>    而起 worker 是运维动作，不是把别人的待办改判 `failed` 的理由。
>    注意这条 WHERE 里**没有** `heartbeat_at IS NULL` 这个显式谓词：NULL 是被"小于比较匹配不上"
>    排除掉的，所以它是三值逻辑给的，不是有人写下来的——改这条语句时别把那句当成已经写过了。
> 4. **回收不认识"是我自己在跑"，靠的是钟而不是身份**：`reclaim_update_stmt` 里没有
>    `id != own_job_id` 这一格，也没有 `errors` 的读改写。一个跑得很久的作业之所以不被自己的
>    worker 判成僵尸，是因为每 10s 有人替它把钟推到"现在"。`RETURNING id` 让"我回收了哪些"由这条
>    语句自己回答——两个 worker 同扫时后到的那个拿到空列表，终局事件不会被补写两遍。
> 5. **补写终局事件走 017 的那个唯一入口**：`sync_service.append_terminal_event`（本片新增的公共
>    helper，因为它在 `run_sync` 之外被调用）。`counters` 抄该作业**最后一帧**的账（一帧都没有才归零）——
>    凭空写五个 0 会把"已经抽了 8 个对象"抹成"什么都没做"，而那一行正被进度条看着；
>    粗档 `done` 仍由 `stage_of("done")` 现算，全仓第二次翻译没有发生。
> 6. **三个阈值/间隔/天数全是入参**：`heartbeat_interval_s`(10) / `stale_job_reclaim_s`(180) /
>    `retention_days`(30) 由 `loop()` 经 `run_worker.cadence(settings)` 一次翻成 `beat_forever`
>    的三个入参——"哪个键喂给哪个参数"这件事全仓只住在那一个纯函数里，`test_worker_reclaim.py`
>    钉它（实测过：把 `interval_s` 与 `stale_seconds` 两个键互换，整轮用例只红那一条），
>    循环体和语句文本里都不出现这三个数（P2 那本"只剩定义点"的账，本片闭掉三条）。
>    PG 的间隔写法是 `make_interval(secs => N)`：`func.make_interval(secs=N)` 会被 SQLAlchemy 当成
>    函数构造选项直接 `TypeError`，`text()` 绑参又渲染不出 `literal_binds` 下的数字（单测断的正是
>    "换一个入参、文本里的数跟着换"），所以走 `literal_column`。拼接前有一道**拒绝式**闸门：
>    参数名必须落在 `make_interval` 那七个之内、值必须是 `int` 且不是 `bool`，否则当场抛。
>    这里第一版写的是"一道 `int()` 闸门"，那句是错的、已实测纠正：`int()` 是**静默截断**
>    （`secs=180.7` 编出 `make_interval(secs => 180)`，`secs=True` 编出 `1`），
>    一个 float 阈值会悄悄少一秒——而这条语句的全部语义就是"差多少秒算僵尸"。
> 7. **保留期的射程只有 `sync_job_event`**。上面第 9 条按原文落地：那条 DELETE 的目标表里不出现
>    `sync_jobs`（删作业行是灭迹不是保留期），`data/results/` 的 csv 由用例做 mtime+size 清单快照
>    断"回收前后逐字节相同"。结果文件自己的回收仍归 P8 下载端点那一片。
> 8. **有一笔代价没被任何验收钉住，先记在纸上不动它**：第 7 条那条 DELETE 的谓词是
>    `created_at < now() - interval`，而本表在 `created_at` 上**没有索引**——2026-09-30 在真 `aiweb`
>    上查 `pg_indexes` 实测，只有 `pk_sync_job_event(id)` 与 `uq_sync_job_event_job_id_seq(job_id, seq)`
>    两棵 b-tree，当前 7 行。所以它是**每轮心跳一次的全表扫描**，而 §2.8 第 3 条自己写着这张表
>    "是全仓唯一一张确定会随时间线性增长的表"。今天它免费；不免费的时候付账的是 worker 里那条
>    心跳任务，不是任何一个请求，所以它不会以"接口变慢"的形式被看见，只会以"worker 那一轮迟了几秒"
>    的形式被看见。
>    **当前形状不违反已定口径**（"同一轮顺手扫"要的主语就是这一轮），两条退路也都不该在没有观测
>    之前预先走：(a) 补 `INDEX (created_at)` 要给写侧多一棵树，而 §2.8 ⑤ 刚以"多一份写放大换不到
>    任何一次不同的扫描"为理由拒掉过同表的一条索引；(b) 把清理降频成"每天一次"需要一个跨轮次的
>    记账点，018 里没有。**都不做，留这一条字。**真正会先疼的是 (c)：清理的频率不是清理自己
>    要的，是搭刷钟那班车的（`heartbeat_interval_s`=10s），两件事同轮是 §6 as-built(P3-018) 第 2 条
>    的拍板——于是这条扫描的次数被一个与它无关的常数绑住。
>
> **as-built(P3-020 施工后)**（2026-10-01，工单 020；上面拍板第 11 条落地，真跑见工单 020 交付记录）
>
> 1. **两个键从这一片起才有读取点**：`run_sync` 在 `sync_service.py:989-990` 读
>    `extract.batch_size` / `extract.batch_interval_ms`，作为**必填**关键字参数交给 `collect()`。
>    P2 那本"配置键只剩定义点"的账到此闭掉最后两条。必填而不给默认值是刻意的：给了默认就在方言层
>    多出第二个 200，而改这个数的人改的是 `Settings`。
> 2. **B 那条不参与分批**（§8.1 的表清单查询）。它是名单的来源，把它分批就没有全量可切了；
>    所以"四条查询都要跟着分批"这句工单原文的落地形状是 **1 条 B + 3×批数 条 C/D/E**，
>    而不是"四条各乘以批数"。真跑实测（拦 `MySQLExtractor._rows` 数发出条数）：演示库 4 批时
>    B（认它靠 `t.row_format`，A 与分母那条共用同一个 FROM）只出现 1 次，C/D/E 各 4 次，
>    且第 k 次收到的 `IN` 名单正是第 k 批。
> 3. **事件形状是"一帧带名单"，不是"一批一帧"**（§2.8 ⑨）。发帧点仍是 ⑥ 说的那五个，
>    `tables` 帧的 `payload` 多一格 `batches`，`sync_jobs.counters` 多一格 `batches`（§2.5）。
>    理由不是省事：事件行与元数据同事务，多出来的批次帧只会在同一次 COMMIT 后一起可见，
>    流被拉长而进度条一格也不提前。钉子 `test_分批的两个配置键真有读取点_每批的表名随_tables_帧交回`
>    ——实测过：把 `batch_size` 那行换成硬编码 200，这条整轮只红它自己（终局账里 `batches: 1`）。
> 4. **提交边界一行没动**：一个 catalog 一次 `collect`、一个事务，`partial` 的粒度仍是 catalog
>    （上面拍板第 5 条）。§7 原文"service 每批一个事务提交"与 §6 配置行"每批一个事务，也是
>    `partial` 的粒度"两句都是**从未落地过的承诺**，已按本条更正而不是照抄。要走到那句原文，
>    得先实现 `stream_manifest`（边抽边 yield），它动的是提交边界与 stale 判定，不归 020。
> 5. **失败隔离粒度没有因为分批而变细**：上表第 2 行"中途某张表 IS 查询失败 → 继续下一批"
>    到今天仍然**没有落地**——`collect` 里那三条 `IN` 任何一条抛错，异常照冒，整个 catalog 回滚。
>    020 只改发送节奏；"按批隔离失败"需要 (4) 那条 yield 先在场，否则失败的批与成功的批
>    还在同一个事务里，分开记也没意义。这一格继续挂在纸上，不属于 019（卡片逐表提交）也不属于 020。
> 6. **规模保护仍在昂贵查询之前**（已定口径）：`max_tables` 超限那一步只看 B 的结果，
>    C/D/E 一条都不发。分批把它挪后是最容易顺手改错的地方，所以用 `executed` 里 IS 语句的
>    **条数**钉死（`test_超限判断仍在昂贵的列查询之前_分批不许把它挪后`：只有 1 条）。
> 7. **真演示库的实样**（2026-10-01 跑，`BATCH_SIZE=3`）：10 个业务对象切成 4 批
>    `[3,3,3,1]`，末批只剩 1 张——正好是验收 2 那个边界的真库版本；`counters.batches=4`、
>    `counters.tables=10`（与 §1 的 9 表 + 1 视图对齐）。批间隔 250ms 那一轮 `duration=1985ms`，
>    同一份源、同样 4 批但间隔设 0 的那一轮 `duration=1125ms`——差出来的约 860ms 就是三个间隔，
>    这条下限因此不是"库本来就慢"能蒙过去的。工单原文的"11 个可见对象"是**含内部标记表**那本账
>    （§1.2 已拍板：排除 `_%` 后 `total=10`），10/3 与 11/3 同样是 4 批，用例按排除口径跑，
>    这样验收 1 与验收 5 说的是同一次同步。
> 8. **"分批不改变落库结果"跟的是另一次真跑，不是 008 的 golden**：那七份快照喂的是按 §2.1
>    手工摆出来的 `meta_*` 输入（`test_sync_card_isolation_pg.py` 开头记着），不是演示库实样，
>    拿它对真库等于比两件本来不该相等的事。所以 live 用例的做法是先按默认 200 跑一轮、再按 3 跑一轮，
>    比五张 `meta_*` 的行数与全部卡片 `text_md` **逐字符**，只放行 `counters.batches` 这一格变化。
> 9. **验收 7（`CARDINALITY`/`SUB_PART` "分批后每批都还读到"）补的是形状而不是数值**：同一条 live
>    用例两侧各取一份按 `(表名, 索引名, SEQ_IN_INDEX)` 排序的 `(列名, SUB_PART, cardinality 是否非空)`
>    清单逐位相等，再对分批那一次断每行 `cardinality` 非空、且全库唯一那处前缀索引
>    `product.name(32)`（§1 的夹具）仍在场。数值不比是因为 `CARDINALITY` 是 InnoDB 的采样估算，
>    两次真跑之间它可以合法地变——把"没改变结果"钉在它上面就变成看运气发红。这跟 007 那条
>    `test_sync_live.py::test_验收2_…` 不互替：那一条走默认批大小（10 个对象一批装完）。
>    实测变异：D 条投影换成 `NULL AS CARDINALITY, NULL AS SUB_PART` 后这条 live 用例红在
>    `has_cardinality` 那一行（007 那条同步变红，两个靶心各自独立）。
> 10. **上面表里第 5 行只有分批那两件事落地了**：`SET SESSION max_execution_time` /
>     `SET LOCAL statement_timeout` 这半句到今天**仍无实现点**——`mysql.py` 只有
>     `supports_max_execution_time` 这个**探测**（probe 阶段问源库支不支持），没有任何地方真的发过
>     `SET`。本行不是 020 的承诺（工单的已定口径与验收 1~7 都没提它），记在这里是为了让"抽取把源库
>     拖垮"这一格不被误读成整行已交付：现在真正保护源库的只有批大小与批间隔两条。

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
    approx_rows: int | None = None          # §8.1 A 的 SUM(TABLE_ROWS) → meta_database.approx_rows（§2.4）
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

> **as-built(P3-020)：上面这句里的"一批一个事务"没有落地，而且刻意不落地。**
> 020 交付的分批住在 `collect()` **内部**：一个 schema 的 C/D/E 三条 `IN (...)` 按 `batch_size`
> 切片循环发，`SourceManifest` 仍然**一次交回整个 catalog**，service 因此仍然一 catalog 一事务
> （§6 拍板第 5 条"元数据每库一事务不动"）。要走到原文那个"每批 yield、每批一提交"，得先实现
> 上面 `stream_manifest` 那条协议（as-built(007) 第 2 条至今挂着它），那件事的爆炸半径是提交边界
> 与 stale 判定，不是发送节奏，所以它不属于 020。
> 落地形状：`collect(..., *, max_tables, batch_size: int, batch_interval_ms: int)` —— 后两个
> **故意不给默认值**（原文的 `batch_size: int = 200` 会在方言层留下第二个定义点，而 200 这个数
> 的口径住在 `Settings.extract`），`SourceManifest.batches: list[list[str]]` 交回"本轮实际分了
> 哪几批、每批哪些表名"，由 §2.8 ⑨ 投影进 `tables` 帧的 `payload`。

> as-built(007)：这段协议在落地时有三处必须先说清楚，否则读代码的人会以为少实现了东西。
>
> 1. **`ConnectionSpec` 原文被引用但从未定义**。现在它定义在 `app/extractor/base.py`：
>    `host/port/user/password/database/charset='utf8mb4'/connect_timeout_s`。
>    刻意不认识 `DataSource`——ORM 行、Fernet 密文、Settings 都留在 `sync_service` 那一侧，
>    方言层拿到的是已解密的明文参数，这样加第三种源库不用改 service。
> 2. **P2 只实现 `probe/discover/collect/close` 四个方法**。`stream_manifest`（分批 yield +
>    cancel 回调）、`render_create_sql`、`sample_distinct` 属 P3，没有实现也没有桩——
>    与其留一个永不通过的假实现，不如让 Protocol 显式小一点（`app/extractor/base.py` 的
>    `Extractor` 就是这个子集，注释里写着少掉的三个）。
> 3. **`collect` 的抽取范围是调用方渲染好的 SQL 片段 + 绑定参数**（`table_sql`/`table_params`），
>    而不是原文的 `include_tables/exclude_tables` 两个序列。原因是这两列的口径必须由 006 的
>    `test_connection` 与 007 的抽取共用同一个渲染函数（§8.1 B 的 as-built 注），
>    方言层因此不认识"正则还是通配"这个问题。`table_limit` 同理变成 `max_tables` 参数（§6）。
>
> `Raw*` 的取值形状补一条实测：结果集键名 = SQL 里写的那个大小写（5.7.17 + pymysql 实测
> `SELECT c.TABLE_NAME` 回 `TABLE_NAME`，`AS enum_def` 回 `enum_def`），所以映射层按 §8.1 原文
> 的大小写取键，不需要做大小写归一。

## 8. 抽取 SQL 原文

### 8.1 MySQL 5.7（4 条批量 SQL 拿全一切，与表数量无关）

> **as-built(P3-020)：标题里"与表数量无关"这个前提被有意换掉了，换到的是"一次 `IN` 装两千张表"**
> **那条路走不通**。C/D/E 三条现在按 `AIWEB_EXTRACT__BATCH_SIZE` 切片循环发，语句条数从 3 变成**3×批数**
> （A/B 两条不变，各一条：B 是名单的来源，把它分批就没有全量可抽了）。省下的是一句 1064 和
> IS 的锁，多付的是往返次数——批间隔 `BATCH_INTERVAL_MS` 就是为后者准备的让气。
> 钉子：`tests/integration/test_extract_mysql_live_batches.py`（真演示库、`BATCH_SIZE=3` → 4 批，
> 三条昂贵查询各 4 发，且第 k 发问的就是第 k 批）与 `tests/unit/test_extract_mysql_batching.py`。

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
       t.TABLE_COMMENT, t.ENGINE, t.ROW_FORMAT, t.TABLE_COLLATION, t.TABLE_ROWS,
       t.DATA_LENGTH, t.INDEX_LENGTH, t.CREATE_TIME, t.UPDATE_TIME
FROM information_schema.TABLES t
WHERE t.TABLE_SCHEMA = %s
  AND (t.TABLE_TYPE IN ('BASE TABLE','VIEW'))
  AND (t.TABLE_NAME LIKE %s OR %s IS NULL)        -- include 通配
  AND (t.TABLE_NAME NOT LIKE %s OR %s IS NULL);   -- exclude 通配

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

> as-built(007)：B 条有两处与原文不同，都是被真库逼出来的。
> ① 多了 `t.ROW_FORMAT`——`RawTable.row_format` 与 `meta_table.row_format` 都有这一列，原文漏 SELECT。
> ② 范围过滤是**源原生 LIKE**，不是 `REGEXP`：`data_sources.include_tables/exclude_tables`
> 里存的本就不是正则（006 实测），而且渲染这段的 `datasource_service.table_scope_filter`
> 是 006 的 `test_connection` 与 007 的抽取**共用**的同一个函数——两边各写一套的话，
> 探测报 9 表 1 视图、同步抽出 11 张，同一颗按钮给出两个数（见 verification.md §1.2）。
> 占位符在落地版里是 SQLAlchemy 命名参数（`:schema` / `:tbl_0`），因为抽取和探测要共用同一段渲染。
>
> ③ A 条多了 `COUNT(t.TABLE_NAME) AS visible_table_count`：§2.4 的 `meta_database.table_count`
> 要这一列，原文 A 只 SELECT 了尺寸/行数，是原文漏列不是实现多加。
> ④ C 条多了 `ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION`：只为快照/人读的输出确定性，
> 语义上不依赖（`meta_column.ordinal_position` 自己带着序）。
> ⑤ 测试口径是**子串钉**不是整段原文钉（`test_extract_mysql_snapshot.py` 对 A–E 各钉若干
> 关键子串）——所以 ①–④ 这类偏差不会让用例变红，由本条 as-built 充当记录。

> D 条那个 `LEFT JOIN STATISTICS it ... AND it.SEQ_IN_INDEX=1` 的自连接不是冗余：`COMMENT` 是
> **索引级**属性，IS 给每一行都带一份，不钉住第 1 行就会让复合索引的注释随列数翻倍。
> 本机 5.7.17 实测 `STATISTICS` 同时存在 `COMMENT` 与 `INDEX_COMMENT` 两列，所以不需要退回
> `SHOW CREATE TABLE` 去捞索引注释。

> 视图的 `TABLE_COMMENT` 在 5.7 拿不到（IS 里视图注释存 `information_schema.VIEWS` 但常为空）
> → 用 `SHOW CREATE TABLE` / `SHOW FULL COLUMNS` 作**注释兜底**，仅对 B/C 返回空的表触发，且限流 ≤N 张。
> as-built(007)：这条兜底路径**P2 未实现**（演示库的视图注释实测能拿到），实现落在抽取层之外的
> 补救环节；`RawTable.create_sql` 字段已预留，`render_create_sql()` 尚未有人认领。

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
-- as-built(P3-023)：上面那句 `t.typname AS data_type` 到落库前不再是终值——`data_type` 存归一值，
-- 原料要取 `raw_data_type`（`format_type` 的人话名，带精度与 `[]`）过 `postgres_types.normalize()`；
-- `typname` 既没有长度（`varchar` 而不是 `character varying(64)`）、数组又是下划线形态，
-- 拿它当归一结果会抹掉 §9 要求保留的括号。接线归工单 024（PG 抽取器本体），本片只交付那个纯函数。

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
- MySQL：`decimal unsigned zerofill`（→ `numeric(10,0)`，见下方 as-built(023) 的塌板结论）/
  `enum('a','b')` / `set` / `tinyint(1)`（→ `bool`，已拍板，结论与三条理由见本节末 as-built(023)）/
  `datetime(3)` / 生成列 / `utf8mb4_0900_ai_ci`（8.0 的排序规则出现在输入时的容错）

> **as-built(0007) 已被 as-built(023) 取代**：0007 时归一还没落，两列都只存原文。工单 023 把
> 归一化接到了**抽取层（写侧）**——这是本轮拍板的落点（不再放卡片层，避免"多处重复归一"）。

> **as-built(P3-023)：§9 的归一化已在抽取层落地，`data_type` 从此是归一值、`raw_data_type` 仍是
> 方言原文。** 具体：
> - 归一放**抽取层（写侧）**。MySQL 接线在 `extractor/mysql.py::rows_to_columns()`——
>   `data_type = mysql_types.normalize(COLUMN_TYPE)`、`raw_data_type = COLUMN_TYPE`（原文照存，
>   `unsigned`/`zerofill`/显示宽度这些修饰只有原文看得见，"raw 还在"那层保证没破）。
>   `DATA_TYPE` 那一格从此不再是 `data_type` 的来源：归一只看 `COLUMN_TYPE`（它才带宽度与修饰）。
> - 落点两个，对称命名：`app/extractor/postgres_types.py::normalize()`（§9 指名；PG 抽取器本体是
>   工单 024 的活，本片只交付这个纯函数 + 用例，不接 PG 的 SQL）与
>   `app/extractor/mysql_types.py::normalize()`。两者都只做方言原料的**解析**，映射与规范字面
>   全收在 `app/extractor/type_normalize.py` 这**一处**（`VALUE_DOMAIN` 值域 + `PG_SYNONYMS` /
>   `MYSQL_SYNONYMS` 两张表 + `compose`）——全仓不许出现第二份映射字面，两方言的 `data_type`
>   值域由测试侧那张**同一张断言表** `tests/unit/test_type_domain.py` 钉住（那张表自己也是一条用例，
>   文件名以 `test_` 开头是为了让它被收集执行）。
> - **`tinyint(1) → bool` 已拍板**。三条理由：① 归一的目的是跨方言可比，PG 的 `boolean` 归一后
>   就是 `bool`，MySQL 若保留 `tinyint(1)` 会造出"同一语义两种字面"；② `raw_data_type` 继续存
>   方言原文，`tinyint(1)` 没丢；③ 两方言共用同一结论是工单已定口径。据此本节那条"（→bool 与否）"
>   的问号收掉：MySQL 与 PG 的布尔都归一到 `bool`。`tinyint(4)` 不在此列——它是窄整型，归 `tinyint`。
> - **任意精度小数两方言都归 `numeric`，值域里没有 `decimal` 这个基名**（2026-09-30 补拍）。
>   PG 的 `numeric`/`decimal` 与 MySQL 的 `decimal`/`numeric`/`dec`/`fixed` 本是同一家族的方言语面，
>   各留一个名字就是"同一语义两种字面"，卡片与 prompt 跨方言比较时还得再归一次。代价是 MySQL 侧
>   `data_type` 写出来的字面（`numeric(12,2)`）不是 MySQL 用户眼里的名字——所以 `raw_data_type`
>   必须一直在：那里存的仍是 `decimal(12,2)`。
> - 归一口径要点：变长/定长字符与 numeric/datetime/time/timestamp 保留长度或精度括号
>   （`varchar(32)` / `numeric(10,2)` / `datetime(3)`，两方言同形状）；整型显示宽度（`bigint(20)`/
>   `int(11)`/`tinyint(4)`）不是语义、归一后剥掉；`enum('a','b')`/`set('a','b')` 塌成基名
>   `enum`/`set`（取值清单住在 `meta_column.enum_values`，卡片正文在那里渲染）；PG 数组
>   `integer[]`→`int[]`、下划线 typname `_int4`→`int[]` 一并断出形状，二维保留层级（`int[][]`）；
>   `serial` 取物理基名 `int`；`GENERATED ALWAYS` 是列属性、归一从原料里剥出类型部分。
> - 真跑证据（验收 5）：`tests/integration/test_sync_live.py` 最后一张用例对真演示库跑一轮同步，
>   把 `meta_column` 每一列的 `data_type` 逐个过共享值域断言，并点名八列比对
>   `(data_type, raw_data_type)` 成对——包括 `order_item.is_gift` 的 `bool`/`tinyint(1)` 与
>   `order_main.amount` 的 `numeric(12,2)`/`decimal(12,2)`。视图聚合列的精度由引擎算，只断"落在值域内"。
> - 归一后卡片与 prompt 因此能跨方言说话：golden 快照里出现的类型字面就是归一值
>   （`varchar` 现在带长度成 `varchar(32)`；`decimal(18,2)` 写成 `numeric(18,2)`；
>   `bigint`/`int`/`enum`/`date`/`text`/`numeric` 这类本就规范的字面不变）。八份快照里**只有三份**因此改了行，
>   其余五份diff 为空——这是"每份 diff 只出现在类型字面处"的可核对版本。夹具侧另加一条守卫
>   （`test_kb_card_golden.py` 的 `test_夹具里的每个类型字面都在归一值域内`），
>   漏改一个类型字面会红，而不是两侧一起错还继续绿。

## 10. MySQL 5.7 特有的两个坑（写进代码注释）

1. **中文注释乱码**：MySQL 5.7 的 `information_schema` 列走 `character_set_system_variables`
   （部分构建默认 utf8mb3），中文 `TABLE_COMMENT` 可能返回 `???`。
   检测：抽完第一批后统计 comment 里 `?` 占比 > 0.3 → 打 warning `CHARSET_SUSPECT`，
   并自动回退用 `SHOW CREATE TABLE` / `SHOW FULL COLUMNS` 逐表取注释（这两条走表的真实字符集）。
   连接必须 `charset='utf8mb4'`。
   as-built(007)：判定函数是 `extractor.mysql.comment_charset_suspect`，口径按原文
   （**全部**注释拼起来后 `?` 的字符占比 > 0.3，一条注释都没有时不算乱码）。
   **乱码回退路径 P2 未实现**——本机 5.7.17 + `aiweb_ro` 实测中文注释正常到达，`?` 占比 0，
   没有可复现的乱码源库可对着做，硬写就成了没有验收对象的代码。
   另外原文的变量名在本机不存在：`select @@character_set_system_variables` 报 1193，
   实际存在的是 `character_set_system=utf8`（服务器级）；能代表"注释到达客户端时是什么编码"的
   是会话级的 `character_set_results`，所以 `ServerInfo.charset` 取的是后者。
2. **`utf8mb4` 索引前缀 / 排序规则**：5.7 默认 `utf8mb4_general_ci`；不要用 `utf8mb4_0900_ai_ci`（那是 8.0），
   也别在 IS 查询里手写 `COLLATE`，否则 `%s` 字面量与 IS 列比较会撞 "Illegal mix of collations"。
   正则过滤用 `REGEXP` 而不是 `LIKE ... COLLATE`。

另外：5.7 的 `information_schema.STATISTICS` 对 MyISAM / 视图列语义不同，视图要走
`TABLES.table_type='VIEW'` 的单独分支（视图列注释全空，卡片会很丑 → 单独降级模板）。

验收锚点：`meta_index` 表里能看到 `CARDINALITY` / `SUB_PART` 非空——
这就证明没有退回 SQLAlchemy Inspector 的逐表 `SHOW CREATE TABLE` 方案。

as-built(0007)：这个锚点原来是**半不可满足**的，原因不在抽取侧而在演示库。本机实测
`ai_web_demo` 的 30 行 `STATISTICS` 里 `CARDINALITY` 非空 30/30（这一半一直成立），
但 `SUB_PART` 非空 **0/30**——整个演示库一个前缀索引都没建，而 5.7 里只有前缀索引
（`KEY (col(n))`）会让 `SUB_PART` 非空。也就是说"`sub_part` 从 IS 到 `meta_index_column`
"这条管道当时只有单测覆盖，live 用例永远看到全 NULL，锚点被建库脚本自己抹平了
（同 `fk:user_activity_log(must be 0)` 那条自检防的是同一类事故）。
已按"考点补进夹具"处理：`product` 加了 `KEY idx_product_name (name(32))`，
于是 `SUB_PART=32` 是**有硬期望值**的断言，而不是"看起来非空就行"。
详见 verification.md §1 考点表与 §1.2 的漂移补丁说明。
