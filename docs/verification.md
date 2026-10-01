# 验证方案

> 四块：本机演示库 `ai_web_demo`、SQL 守卫语料（指针）、测试分层策略、端到端手测清单与 SSE 冒烟。
> 攻击语料全文是测试 oracle，放在 [nl2sql-safety.md](./nl2sql-safety.md#8-攻击语料测试-oracle)。

## 1. 演示库 `ai_web_demo`（本机 MySQL 5.7）

脚本：`backend/scripts/init_demo_mysql.sql`，**9 张业务表 + 1 个视图**（外加 `_seq` 数字表与
`_aiweb_demo_marker` 标记表两个脚本内部对象），每张表都对应一个抽取/检索/JOIN 难点。

| 表 | 行数 | 表注释（中文） | 关键考点 |
|---|---|---|---|
| `category` | 40 | `商品分类表，两级树结构` | 自引用 `parent_id`（测 JOIN 图自环处理） |
| `product` | 1,200 | `商品主表（SKU 粒度）` | `status ENUM('on_sale','off_sale','draft')`、`price DECIMAL(10,2)`、`tags VARCHAR` 里逗号分隔、`created_at DATETIME`、**前缀索引 `idx_product_name(name(32))`**（元数据验收 2 的 `SUB_PART` 靠它才有非空值可断言，见下注） |
| `customer` | 3,000 | `客户档案表` | `gender ENUM('M','F','U')`、`level ENUM('normal','silver','gold','platinum')`、手机号唯一索引、`register_at DATETIME` |
| `order_main` | 30,000 | `订单主表（一笔订单一行）` | `customer_id` FK、`status ENUM('pending','paid','shipped','completed','cancelled','refunding')`（**NL2SQL 高频：'已完成订单' 要能映射到枚举值，这是 SAMPLE_DISTINCT 的主要受益点**）、`amount`/`discount`/`pay_type`、`created_at`+`updated_at`、复合索引 `(customer_id,created_at)` |
| `order_item` | 88,000（>50k 例外，为测聚合与 LIMIT 截断） | `订单明细行表` | `order_id`/`product_id` 双 FK、`qty`/`unit_price`/`is_gift` |
| `payment_record` | 26,000 | `支付流水表（一单可多次）` | FK→`order_main`、`channel ENUM('alipay','wechat','card','offline')`、`paid_at DATETIME`（**跨表问数主角：区域×月份×渠道回款**）、`trade_no` 唯一 |
| `refund_record` | 2,400 | `退款申请与处理表` | 用于 `EXCEPT`/`NOT EXISTS` 类问题（"未退款的订单"） |
| `product_stats_wide` | 1,200 行 × **68 列** | `商品运营统计宽表（按天累计口径）` | 68 列全部带中文注释（`gmv_7d/gmv_30d/uv_*/cvr_*/return_rate/repurchase_cnt/...`），测 prompt 的 token 预算裁切与前端宽表渲染 |
| `user_activity_log` | 50,000 | `用户行为埋点日志（无外键约束）` | **只写 `user_id`/`product_id`/`session_id` 列名，不建 FK**，靠命名约定推断 JOIN（验证 `FORCE_FK_INFER`）；`event_type VARCHAR`、`extra JSON`、`created_at` |
| `v_daily_sales`（视图） | — | — | 测视图列注释缺失的卡片降级 |

守卫语料里的表名（`order_main`/`order_item`/`customer`/`payment_record`/`refund_record`/
`product_stats_wide`/`category`/`product`）与这张演示库一一对应 —— 语料的 `DEMO_TABLES` 白名单就是它。

> **口径已定（2026-09-23 定，2026-09-27 收尾）**：以**枚举出来的对象清单为准 = 9 张业务表 + 1 视图**。
> 同步验收里的 `total` 随之为 **10**（排除下划线前缀对象后的全计数，含那张 `VIEW`）；
> 直接 `SHOW FULL TABLES` 是 **11** 行，因为库里按 §1.2 第 2 步保留了标记表 `_aiweb_demo_marker`，
> 所以**不能**照字面把 `total` 断言成它的行数。详见 §1.2 末注。
> 原方案 §10.4 标题的"8 张"是笔误，roadmap 的 P2/P3 验收已按 9 表改齐。
>
> **前缀索引是 0007 补进来的考点（2026-09-27）**：原夹具 9 张表全是整列索引，而 MySQL 只在
> 前缀索引（`KEY (col(n))`）上给 `information_schema.STATISTICS.SUB_PART` 填非空值，
> 于是"抽取层没有退回逐表 `SHOW CREATE TABLE`"这条验收锚点的 `SUB_PART` 半边**在 live 上永远
> 测不出来**（30 行 STATISTICS，`SUB_PART` 非空 0 行）。它和 `user_activity_log` 不建外键
> 是同一类考点，不该由建库脚本自己抹平。`idx_product_name(name(32))` 补在产品名上，
> 因为按中文名检索本来就是真实写法；期望值因此是硬的：`sub_part == 32`。
> 建库脚本末尾的自检加了 `index:prefix(sub_part=32)` 一行，`tests/unit/test_demo_mysql_script.py`
> 钉住它的存在（夹具漂移要当场红，不能等 live 用例发现全 NULL）。

### 1.1 数据生成要点

- **5.7 没有 CTE/递归** → 数字表法造行：
  ```sql
  CREATE TABLE _seq (n INT PRIMARY KEY);
  INSERT INTO _seq SELECT a.n + b.n*10 + c.n*100 + d.n*1000 AS n
  FROM (SELECT 0 n UNION ... SELECT 9) a, (...) b, (...) c, (...) d;   -- 0..9999
  -- 再 INSERT INTO order_main SELECT ... FROM _seq WHERE n BETWEEN 1 AND 30000;
  ```
- **随机但可复现**：`SET @seed = 20260922;` + 用 `MD5(CONCAT(@seed, n))` 派生
  `CONV(LEFT(md5,8),16,10)/4294967295` 当 `RAND()` 替代，别用裸 `RAND()`
  （每次跑结果不同，端到端手测就无法核对数字）。
- **时间分布**：`created_at = '2023-01-01' + INTERVAL FLOOR(86400*365*u) SECOND`，
  覆盖 2023–2025 三年且近 30 天有数据（否则"近30天"问数是空表，看着像 bug）。
- **中文注释**：文件以 UTF-8 **无 BOM** 保存；脚本首行 `SET NAMES utf8mb4;`；
  所有表 `ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci`。
  **不要用 8.0 的 `utf8mb4_0900_ai_ci`，5.7 不认**（从 8.0 dump 脚本改过来最常踩的一条）。
- **不做的事**：不建存储过程（免得被误当业务表相关元数据）；不用 `utf8`(=utf8mb3)；不建外键到 `_seq`。
- 脚本末尾 `SELECT 'row counts' , (SELECT COUNT(*) FROM order_main), ...` 便于人工核对。

### 1.2 安全执行流程（含"只建不删"守卫）

1. **只读探测**（不写任何东西）：
   ```bash
   export MYSQL_PWD="$LOCAL_MYSQL_PWD"          # 口令来自 shell env，绝不写进脚本/git
   mysql -h 127.0.0.1 -P 3306 -u root --default-character-set=utf8mb4 \
     -e "SELECT VERSION(); SHOW DATABASES;"
   mysql ... -e "SHOW DATABASES LIKE 'ai_web_demo';"   # 必须返回空才继续
   ```
2. 脚本内部第一道闸（防误跑第二次覆盖别人的库，也防同名）：
   ```sql
   -- 若 ai_web_demo 已存在且不是本脚本建的，直接中止
   SET @exists = (SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='ai_web_demo');
   SET @marker = (SELECT COUNT(*) FROM information_schema.tables
                  WHERE table_schema='ai_web_demo' AND table_name='_aiweb_demo_marker');
   -- 用 prepared statement: IF @exists=1 AND @marker=0 THEN 'SIGNAL SQLSTATE ...'
   ```
   （MySQL 5.7 的 `SIGNAL` 只能在存储程序里用 → 实操做法是**脚本头部用 `\! ` 不可靠，
   改由外层 `scripts/demo_db.ps1` 先查再决定是否 pipe 文件**；SQL 里只保留
   `CREATE DATABASE IF NOT EXISTS` + 建标记表 `_aiweb_demo_marker(version, created_at)`。）
3. **只建不删**：脚本**不含任何 `DROP DATABASE`**；重复执行时若 marker 已存在则打印
   "已建过，如需重建请手工执行 `DROP DATABASE ai_web_demo`（本脚本拒绝代劳）"并退出 0。
4. **数据源账号不用 root**，脚本尾部：
   ```sql
   CREATE USER IF NOT EXISTS 'aiweb_ro'@'localhost' IDENTIFIED BY '<从 env 注入>';
   GRANT SELECT ON ai_web_demo.* TO 'aiweb_ro'@'localhost';
   GRANT SELECT ON `ai_web_demo`.* TO 'aiweb_ro'@'%';      -- 若应用从别的 host 连
   FLUSH PRIVILEGES;
   ```
   （**权限只给 `ai_web_demo` 一个库**，不给 `ON *.*` —— 否则"跨库读 `mysql.user`"这类攻击用例
   在本地永远测不出真拦截。）口令用占位符 + 外层替换，或者干脆
   `IDENTIFIED WITH mysql_native_password USING '<env 提供的 hash>'`。
5. 全量执行日志重定向到 `logs/demo_mysql_apply.log`（`logs/` 已在 .gitignore），便于出问题回看。
6. 收尾核对：
   `SELECT table_name, table_comment, table_rows FROM information_schema.tables WHERE table_schema='ai_web_demo'`
   —— 这条也正是 `extractor/mysql.py` 第一批要跑的 SQL，一举两得（用它验证抽取 SQL 的正确性）。

> `make demo-db` 的语义就是第 1+3 步：先探测 `SHOW DATABASES LIKE 'ai_web_demo'`，
> **不存在才** pipe `init_demo_mysql.sql`。
>
> **口令通道已改（2026-09-27 实测后回写）**：不再读 `LOCAL_MYSQL_PWD` 环境变量，改读
> `backend/.setup/my_login.cnf`（`[client]` 段 + `--defaults-extra-file`，目录已 gitignore）。
> 理由：Windows 上 `MYSQL_PWD` 会进子进程环境块、`-p<pwd>` 会进命令行与历史，
> 而 extra-file 是 mysql 自己的机制，口令不进 argv。外层脚本只校验该文件非空，从不回显其内容。
>
> **`aiweb_ro` 的 host 范围已收窄（有意偏离第 4 步示例）**：只开 `'localhost'` 与 `'127.0.0.1'`，
> 不给 `@'%'`。本机开发没有跨主机应用连接，把账号开到网段是无谓的风险；
> 哪天需要网段访问，再显式改这一条并同步文档。
>
> **`SHOW FULL TABLES` 实测是 11 行，不是 10；口径已拍板（2026-09-27）**：`_aiweb_demo_marker` 是
> 真实的 `BASE TABLE`，而 §1.2 第 2 步要求库里保留它（`_seq` 已在脚本末尾收走）。
> **`total` = 排除下划线前缀对象后的 10**（9 张 `BASE TABLE` + 1 张 `VIEW`），
> **不能**直接取 `SHOW FULL TABLES` / `information_schema.tables` 的行数（那是 11，会把内部标记表
> 当成业务对象算进进度、并污染表卡片与 prompt）。
> 同步逻辑内部**必须区分表与视图**：SSE 的 `progress` 事件除 `done/total` 外还带
> `base_table`/`view` 两个计数，视图的卡片降级路径（§1 的 `v_daily_sales` 考点）由 `view` 那一路触发。
> 建库脚本把 10/9/1/11 四个数都自检出来（`business_all`/`business_base_table`/`business_view`/
> `show_full_tables`），任一口径漂移就会在那份清单里露出来。
>
> **已建过的库不会自动长出新考点（0007）**：`demo_db.ps1` 的语义是"库存在就跳过"，而脚本本身
> 只建不删，所以往 `CREATE TABLE` 里加索引**对已经建好的库无效**——新考点只出现在全新机器上，
> 老机器反而永久测不到。为此脚本在自检前有一段 `PREPARE`/`EXECUTE` 守卫的**漂移补丁**
> （`idx_product_name` 不存在才建，存在就只打一行说明），配合这一条命令收敛：
> ```
> mysql --defaults-extra-file=backend/.setup/my_login.cnf --default-character-set=utf8mb4 \
>   ai_web_demo -e "ALTER TABLE product ADD INDEX idx_product_name (name(32));"
> ```
> （补丁段让"重放整个脚本"是安全的，但正常路径仍由外层探测拦住，不会替谁重放。）
>
> **as-built（006）：这条排除规则住在数据源配置里，不是代码内建的。** `test_connection` 统计
> `table_count`/`view_count` 时按 `include_schemas` + `include/exclude_tables`（**源原生 LIKE**，
> 不是正则）过滤，所以要得到上面的 9/1，登记这条源时必须填 `exclude_tables: ["\\_%"]`；
> 不填就是 10/1（库里真实可见数）。007 同步侧要么沿用"范围由源配置负责"这一语义，
> 要么把下划线排除升级为内建规则——两处口径必须一致，否则 SSE 的 `total` 和探测报的数会打架。

### 1.5 PG 演示源 `ai_web_demo_pg`（本机 PostgreSQL 18，schema `demo`）

P3 验收 6「PG 数据源跑通同步」的原料。**它是被抽取的源库**，与元数据库 `aiweb` / 测试库
`aiweb_test` 无关——建库 SQL 里不出现那两个库名。外层复验会向 `aiweb` 发**一条**
`SELECT count(*) FROM aiweb.users`（期待它被权限挡下），不改任何东西、不发 DDL，
其余语句全部落在 `ai_web_demo_pg` 内。

脚本：`backend/scripts/init_demo_mysql.sql` 的同构兄弟 `backend/scripts/init_demo_pg.sql`
（进版本库，只含占位符 `__AIWEB_PG_RO_PASSWORD__`），外层 `scripts/demo_pg.ps1` / `make demo-db-pg`。
对象构成与 §1 完全对齐：**9 张业务表 + 1 个视图**，外加内部标记表 `_aiweb_demo_marker`
（下划线前缀，同时是"排除规则在 PG 侧也生效"的活体考点）与 `other_app.secret_table`
（不在 `demo` 里，不影响计数）。四个数同样是 **10 / 9 / 1 / 11**。

| 表（schema `demo`） | 行数 | 列数 | 关键考点（MySQL 版没有的那一半） |
|---|---|---|---|
| `customer` | 200 | 7 | **`serial` 主键**（§9 映射清单点名：真身是 `pg_attrdef` 里的 `nextval(...)`，与 identity 要分得开）、`phone` 唯一索引、`register_at timestamptz` |
| `category` | 40 | 3 | 自引用**真外键** `fk_category_parent`（MySQL 版只有列名关系），JOIN 图自环在两边都可测 |
| `product` | 500 | 8 | **`tags text[]`**、**`attrs jsonb`**、**表达式索引** `ix_product_name_lower ((lower(name)))`（`pg_index.indkey` 里那一位是 0 → §8.2 D 的 `column_name` NULL 分支）、**部分索引** `ix_product_on_sale ... WHERE status='on_sale'`（`indpred` 非空） |
| `order_main` | 2,000 | 9 | **`GENERATED ALWAYS AS IDENTITY`**（§9 点名的另一样）、`updated_at` 可空、复合索引 `(customer_id, created_at)`、`status` 六值枚举口径同 MySQL |
| `order_item` | 5,000 | 6 | 双 FK（→`order_main`/`product`）、`is_gift boolean`（MySQL 侧是 `tinyint(1)`，归一化两方言都要落） |
| `payment_record` | 1,500 | 6 | identity 主键、`trade_no` 唯一、`paid_at timestamptz`（跨表问数主角，口径同 MySQL） |
| `user_activity_log` | 3,000 | 10 | **不建外键**（靠命名推断 JOIN，验证 `FORCE_FK_INFER`）；三种数组都挂这张表：`hit_ids int4[]`、`scores numeric(10,2)[]`、`occurred_at timestamptz[]`，另有 `extra jsonb` |
| `product_stats_wide` | 500 | **25** | 宽表裁切考点（MySQL 版 68 列，这边 25 列够测 token 预算）；**`top_keywords varchar(64)[]`** 是归一化最难的一格——数组必须保住 `atttypmod`，否则 `(64)` 这个修饰符就丢了 |
| `t_no_comment` | 50 | 4 | **无表注释 + 无列注释 + 无主键**三条降级分支合到一个对象上（刻意合并，免得对象总数漂到 11 打乱 `total` 口径） |
| `v_daily_sales`（视图） | 随时间轴分布而变，只核对非空 | 4 | **不写注释**：视图列注释缺失的卡片降级，与 §1 的 `v_daily_sales` 同一考点 |

守卫语料里的表名在两个演示源里一一对应，所以同一条问数可以在 MySQL 源与 PG 源之间对拍
抽取、归一化和卡片渲染的差异。

#### 1.5.1 与 MySQL 版的四处口径差异（不是 bug，是方言本性）

1. **没有跨库引用表的写法**。§1.2 第 4 步的"用只读账号读 `mysql.user` 必须被拒"在 PG 侧没有等价语法
   （PG 不支持一条 SQL 里引用别的库的表）。
   等价用例是**换库再读**：复验时以 `demo_pg_ro` 连进 `aiweb`，发
   `SELECT count(*) FROM aiweb.users`，期待它失败且失败原因里出现 `permission denied`。
   这里特意**不接受**"没提供口令 / 认证失败"当作通过——那种失败对一个本来有权限的账号同样成立，
   是假绿；所以 passfile 里 `demo_pg_ro` 那行的库名写成 `*` 而不是钉死 `ai_web_demo_pg`。
   另一个不显然的点：PG 的**库级 `CONNECT` 默认就给了 `PUBLIC`**，所以"连不上 `aiweb`"根本不能当证据
   （真连不上多半是 `pg_hba` 或口令问题，与授权无关）。能证的只有**对象级读权限**，
   因此断言落在"读 `aiweb.users` 被拒"上，而不是"连 `aiweb` 被拒"上。
2. **没有 `SHOW FULL TABLES`**。对象枚举走 `pg_class` × `pg_namespace`，`relkind` 里 `r`/`v`
   两支就够（本夹具不用分区表/物化视图，但自检的 `relkind IN ('r','p','m','f','v')` 已把
   四条表类分支都写上，将来加分区表不用改口径）。
3. **权限按 schema 而非按库**。`GRANT SELECT ON ALL TABLES IN SCHEMA demo` + `USAGE ON SCHEMA demo`
   两条都要，缺 `USAGE` 时读表报的是 schema 级拒绝，容易误判成"表没授到"。
   序列**不授**（只读账号不该有 `nextval`）。
4. **`_aiweb_demo_marker` 是"这个 schema 是我们建的"的凭证**，作用同 MySQL 的库级 marker；
   PG 的 marker 挂在 schema 上，因为库由外层 `CREATE DATABASE` 建、schema 由 SQL 建，
   两者失败点不同。半途失败留下的"库在、marker 在、数据不全"要靠手工
   `DROP DATABASE ai_web_demo_pg` 重来，外层脚本第二次跑只会报"已建过，跳过"。

#### 1.5.2 数据生成要点（与 §1.1 的差异）

- 造行用 `generate_series(1, N)`，**不用 `random()`**：派生值全部由下标做同余，
  所以两次执行的行数/金额/枚举分布逐字一致，端到端手测才核得掉数字。
  口令与时间轴是仅有的两处例外（时间轴上界锚在 `now()`，保证"近 30 天"类问数永远非空）。
- 表/列注释用 `COMMENT ON`，全部中文；文件 **UTF-8 无 BOM**（BOM 会跑到第一条语句前面，psql 当场语法错），
  脚本首段 `SET client_encoding = 'UTF8'`，外层再设 `PGCLIENTENCODING=UTF8`。
  **库本身的编码由外层显式建定**：`CREATE DATABASE ai_web_demo_pg WITH ENCODING 'UTF8' TEMPLATE template0`，
  脚本开头另有一道 `pg_encoding_to_char(encoding) <> 'UTF8'` 即中止的守卫（给手工 `psql -f` 留的）。
  这两条防的是同一件事：集群若不是 UTF8，中文注释会安静地写成乱码，而要等到 024 真跑抽取才暴露，
  那时夹具已经不可信了。
- 幂等靠 `CREATE ... IF NOT EXISTS` + 带主键的 `INSERT ... ON CONFLICT DO NOTHING`；
  `t_no_comment` 没主键，改用 `WHERE NOT EXISTS`；视图用 `CREATE OR REPLACE`。
  外键与角色是"存在就跳过"的 `DO` 守卫，因为 PG 没有 `ADD CONSTRAINT IF NOT EXISTS`。
- `serial` 建完数据后要把序列推到 `max(id)`（走 `DO/PERFORM` 而不是裸 `SELECT setval(...)`——
  后者往 stdout 吐一行数字，会混进外层的 PASS/FAIL 裁决计数里）。
- **不做的事**：不建存储过程、不建分区表、不给 `other_app` 授任何权限、不动 `aiweb`/`aiweb_test`。

#### 1.5.3 安全执行流程

1. **口令通道**（与 §1.2 的 `--defaults-extra-file` 同一路由）：用户手工填
   `backend/.setup/pg_login.env`（`PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD` 四行，目录已 gitignore）。
   外层读出后写一份**临时 passfile**（`backend/.setup/pg_probe.pass`，格式
   `host:port:db:user:password`，冒号与反斜杠转义，UTF-8 无 BOM），只把路径交给环境的
   `PGPASSFILE`，`PGPASSWORD` 从头到尾不设，`psql` 永远带 `--no-password`（匹配不上就失败，
   不挂在交互式提示上）。临时文件与替换后的 rendered SQL 都在 `finally` 里删除。
   passfile 落盘后立即断开 ACL 继承、只授当前用户（Windows 上 EDB 那套 libpq **不检查** passfile
   权限，POSIX 的 0600 在这里不存在，只靠 gitignore 不够）；`demo_pg_ro.env` 同样处理。
2. **只建不删**：脚本不含 `DROP DATABASE` / `DROP SCHEMA ... CASCADE`；库已存在且认得 marker 时
   只复验只读，认不出 marker 时**中止**（不接管别人的 schema）。库不存在、但集群里还留着
   `demo_pg_ro` 角色而 `demo_pg_ro.env` 也不在时同样**中止**——角色是集群级对象，建库脚本对
   已存在的角色是"跳过创建、沿用旧口令"，此时新生成的口令不会生效，让它跑下去只会抛一个
   看不出根因的认证失败。
3. **口令写盘晚于建库成功**：失败时不留一份没人认领的凭证。日志落 `logs/demo_pg_apply.log`，
   任何一条 `psql` 输出在回显或落盘前都先过一遍脱敏（建库语句报错时 psql 会把
   `CREATE ROLE ... PASSWORD '<明文>'` 抄进 stderr）。
4. **只读账号 `demo_pg_ro`**：口令由脚本生成（只用字母数字，避免 passfile 分隔符与 SQL 引号两套转义），
   落 `backend/.setup/demo_pg_ro.env`。授权只有 `CONNECT`(本库) + `USAGE`(schema demo) +
   `SELECT`(该 schema 全部表) + `ALTER DEFAULT PRIVILEGES`；序列不授；`other_app` 不授。
5. **收尾复验四条**（"已建过"分支也要重跑，否则中途失败过一次就再没验证过第二遍）：
   `SELECT demo.order_main` 通（2000 行）、读 `other_app.secret_table` 以 `permission denied` 失败、
   `INSERT demo.t_no_comment` 以 `permission denied` 失败、以 `demo_pg_ro` 连进 `aiweb` 读
   `aiweb.users` 以 `permission denied` 失败。
   三条"期待失败"的用例都**只认 `permission denied`**——换成别的错误（包括"没提供口令"）
   说明授权其实漏了，或者用例根本没碰到那道门。

#### 1.5.4 建库脚本自检清单（34 行 PASS，期望值全部手算）

`init_demo_pg.sql` 末尾的裁决查询逐条打印 `PASS`/`FAIL`，外层按"有 FAIL 即中止、PASS 少于 34 行
即视为日志被截断"两道闸判绿（§1 那条 27 行下限的同一手）。这 34 行分三段，就是本节与实跑输出的对账清单：

```
PASS  business_objects=10              ← 排除下划线对象后的全计数（= 同步的 total）
PASS  base_tables=9
PASS  views=1
PASS  all_objects_with_marker=11       ← 不能拿它当 total（含内部标记表）
PASS  commented_objects=8              ← 8 张业务表有表注释
PASS  uncommented_objects=2            ← t_no_comment 与 v_daily_sales 走降级
PASS  type:text=…                      ← 两方言共有
PASS  type:jsonb=…                     ← product.attrs / *_log.extra / wide.channel_split / t_no_comment.meta
PASS  type:text[]=1                    ← product.tags
PASS  type:int4[]=1                    ← user_activity_log.hit_ids
PASS  type:numeric[]=1                 ← scores（带精度的数组）
PASS  type:timestamptz[]=1             ← occurred_at
PASS  type:varchar64[]=1               ← top_keywords
PASS  varchar_array_keeps_modifier=1   ← atttypmod 没丢，否则归一化无从断言
PASS  generated_always_identity=2      ← order_main.id、payment_record.id
PASS  serial_column=1                  ← customer.id（pg_attrdef 里的 nextval，与 identity 分开数）
PASS  expression_index=1               ← ix_product_name_lower（判据是 indexprs 非空；indkey 是 int2vector，
                                          `indkey::int[]` 那个转换没在 live 上证过，所以自检不用它 —— 见工单 024 第一件事）
PASS  partial_index=1                  ← ix_product_on_sale（indpred 非空）
PASS  fk_constraints=6                 ← user_activity_log 那两个列名不该有 FK
```

行数段（9 张表逐张断精确行数 + 视图只断非空，共 10 行）。**这一段必须是断言而不是打印**：
只核对结构不核对数据，"表建齐了但一行没插"也能凑够上面那 19 行 PASS，而那正是 002 在 MySQL
侧踩过的同一类假绿。视图不断精确值，是因为它按天聚合、行数随 `now()` 漂移，手算不出来。

> 这 34 行由 `backend/tests/unit/test_demo_pg_script.py` 在**不连 PG** 的情况下与本文对账
> （`make check` 每次跑）：三件事钉死——这里列的每一行在脚本里都有对应的断言、行数段与枚举段那 15 行
> 的**期望值**与上面那张对象表逐字一致、外层 `demo_pg.ps1` 的行数下限等于这里列的行数。
> 结构段那 19 行的**具体数值**（10/9/1/11/8/2 这些）比不了：脚本里它们是 `'PASS  base_tables=' || 实际值`
> 的拼接形，期望值写在 `WHERE` 子句的 CTE 列名上，与文档这里的标签名不同源——那 19 个数的最终裁决
> 仍在 `make demo-db-pg` 的真跑输出里。

```
PASS  rows:customer=200                ← 以下 9 行逐表对齐 §1.5 枚举表的行数列
PASS  rows:category=40
PASS  rows:product=500
PASS  rows:order_main=2000
PASS  rows:order_item=5000
PASS  rows:payment_record=1500
PASS  rows:user_activity_log=3000
PASS  rows:product_stats_wide=500
PASS  rows:t_no_comment=50
PASS  rows:v_daily_sales>0             ← 只断非空（时间轴锚在 now()，精确值不可手算）
```

枚举值段（5 行）：造数用下标同余 `(ARRAY[...])[(n % k) + 1]`，`k` 写错时数据看着仍然"很满"
但少一个值，卡片与 `SAMPLE_DISTINCT` 的考点静默消失——MySQL 侧曾因 `draft` 恒 0 漏过真 bug，
24 行结构 PASS 一条没拦。期望值就是 `init_demo_pg.sql` 里那五个 `ARRAY[...]` 字面的长度。

```
PASS  enum:order_main.status=6         ← pending/paid/shipped/completed/cancelled/refunding
PASS  enum:product.status=3            ← on_sale/off_shelf/draft
PASS  enum:payment_record.channel=4    ← alipay/wechat/card/offline
PASS  enum:customer.gender=3           ← M/F/U
PASS  enum:customer.level=4            ← normal/silver/gold/platinum
```

#### 1.5.5 登记数据源时的连接参数（工单 024 直接照此抄）

| 字段 | 值 |
|---|---|
| host / port | `127.0.0.1` / `5432` |
| 数据库 | `ai_web_demo_pg` |
| schema | `demo`（`include_schemas: ["demo"]`） |
| 排除 | `exclude_tables: ["\\_%"]` —— 同 §1.2 末注的 as-built(006)：这条规则住在**源配置**里，不填就变 10/1 |
| 账号 | `demo_pg_ro`，口令来源 `backend/.setup/demo_pg_ro.env` 的 `PGPASSWORD=`（不进对话、不进 git） |
| 期望探测结果 | `table_count=9`、`view_count=1` |

> **as-built(P3-024)**：这一格今天**人肉点不到**。工单 024 只把"同步"那条路按 kind 分发
> （「涉及层」点名的是 `sync_service._extractor_for`），`datasource_service._connect_and_describe`
> 仍然 `kind != 'mysql'` 就抛，所以 PG 源点"测试连接"得到的是 501 `not_implemented`。
> 那两个数本轮是按 §1.5.4 的自检查询与 live 落库行数对上的，不是探测端点回吐的；
> 接探测那一张片（未认领，见 §2.1 末注）跑通后这一格才算被界面路径证过。

### 1.6 PG 源同步手测（工单 024 验收 7）

前置：§1.5.3 跑完（`ai_web_demo_pg` 已建、`backend/.setup/demo_pg_ro.env` 在场且 `PGUSER=demo_pg_ro`——
拿超管跑下面这些步的话，第 9 步那三条"期待失败"会全绿成"竟然读得到"，判定就反了）；
元数据库侧 `uv run alembic upgrade head` 到位；两条进程都起着（`dev.ps1 dev` + `dev.ps1 worker`）。

| # | 动作 | 期望证据 |
|---|---|---|
| 1 | 新建数据源 `demo-pg`：`kind='postgres'`、`host`/`port` = `127.0.0.1`/`5432`、**库名填进 `catalog_name`**（`ai_web_demo_pg`，不是 MySQL 那样留空）、`include_schemas=["demo"]`、`exclude_tables=["\\_%"]`、账号 `demo_pg_ro` | 201 + `id`；那一行的口令只存在于 `secret_enc`（Fernet 密文），`data_sources` 里任何一列都不是明文 |
| 2 | 点"测试连接" | 501 `not_implemented`（见上面 §1.5.5 的 as-built 注）——**这一行是"应该还没有"而不是"失败"** |
| 3 | 立即同步 | `POST /api/sync/jobs` → 202 + `job_id`；终局 `status='success'`、`errors=[]`、`warnings=[]`、`counters` 逐字见 §1.6.1；`sync_job_event` 按 `seq` 从 1 连续可回放、最后一帧 `stage='done'`；`tables` 那一帧的 `payload.batches` 是**一个含 10 个表名的列表**（`AIWEB_EXTRACT__BATCH_SIZE` 默认 200，10 对象抽不满一批） |
| 4 | 元数据浏览 → `product` | 8 列；中文注释在场（`tags` 一列的注释是「标签数组（text[]，归一化考点）」）；三棵索引，其中 `ix_product_name_lower` 的**索引列位置是空**（表达式索引：`indkey` 那一位是 0，§8.2 D 接不到 `pg_attribute`），`ix_product_on_sale` 的索引列是 `price` 而不是 `status`（`WHERE` 只是谓词） |
| 5 | 元数据浏览 → `v_daily_sales` | `table_type='VIEW'`、四列且列注释全空（夹具刻意不写注释，抽取器不许补造）、`engine IS NULL`（没被硬造成 `heap`）、`last_analyze_at IS NULL`（视图本来就不在 `pg_stat_user_tables` 里，§2.4 注② 的口径是方言无关的） |
| 6 | 元数据浏览 → `t_no_comment` | 表注释与四列注释全空、没有任何一列是主键（它没有 PRIMARY KEY）、`is_indexed` 全 false；它**在名单里**——名字没有下划线前缀，不归排除规则管 |
| 7 | 关系页 | 6 条 `extracted`（`fk_category_parent`/`fk_product_category`/`fk_order_main_customer`/`fk_order_item_order`/`fk_order_item_product`/`fk_payment_order`，`on_delete` 全是 `NO ACTION`——夹具一条 ON DELETE 子句都没写）+ **2 条 `inferred`**（`user_activity_log.product_id → product`、`product_stats_wide.product_id → product`，都是 1.00）；`user_id` 不在推断清单里（库里没有 `user` 表，§5.3 不许编） |
| 8 | `select ... from aiweb.meta_column` 抽类型 | §1.6.1 那张七类原料表逐字一致：`data_type` 是 §9 归一值、`raw_data_type` 是 `format_type` 的 PG 原文 |
| 9 | （反向）用 `demo_pg_ro` 手工连源库 | 读 `other_app.secret_table`、`INSERT demo.t_no_comment`、连进 `aiweb` 读 `aiweb.users` 三条都以 `permission denied` 失败（§1.5.3 末条的同一复验，只认这一个错误码） |

#### 1.6.1 实跑输出的枚举清单（一次 PG 同步的全部对账数字）

本轮实跑（2026-10-01，`ai_web_demo_pg` + `demo_pg_ro`）终局 counters，逐字：

```
{'databases': 1, 'tables': 10, 'columns': 82, 'indexes': 14,
 'relations_extracted': 6, 'relations_inferred': 2, 'tables_stale': 0,
 'tables_failed': 0, 'cards': 10, 'batches': 1}
```

`tables=10` 是 9 张 BASE TABLE + 1 张 VIEW（排除规则生效后的全计数，§1.5.4 首行 `business_objects=10`
同字）；`columns=82` 是逐张数过 `init_demo_pg.sql` 的 `CREATE TABLE`：7+3+8+9+6+6+10+25+4+4；
`indexes=14` = 8 张有主键的表各一棵 `*_pkey`（`t_no_comment` 无主键、视图无索引各出 0 棵）
+ `uq_customer_phone` + `order_main_order_no_key` + `payment_record_trade_no_key`
+ `ix_product_name_lower` + `ix_product_on_sale` + `ix_order_main_customer_created`，
它们在 `meta_index_column` 占 **15** 位（只有最后那棵复合索引占 2 位）；`cards=10` 是一表一卡——
PG 侧最宽的 `product_stats_wide` 只有 25 列，过不了 kb-workflow §6 那条 40 列切片线
（MySQL 版的同名表 68 列，所以那边是 12 张，差异来自两份夹具的 DDL 而不是切片逻辑）。

七类类型原料（015 的 `typ` CTE 同一份清单，`raw` = `format_type` 原文、`norm` = §9 归一值）：

| 表.列 | `raw_data_type` | `data_type` |
|---|---|---|
| `customer.remark` | `text` | `text` |
| `product.attrs` | `jsonb` | `jsonb` |
| `product.tags` | `text[]` | `text[]` |
| `user_activity_log.hit_ids` | `integer[]` | `int[]` |
| `user_activity_log.scores` | `numeric(10,2)[]` | `numeric(10,2)[]` |
| `user_activity_log.occurred_at` | `timestamp with time zone[]` | `timestamptz[]` |
| `product_stats_wide.top_keywords` | `character varying(64)[]` | `varchar(64)[]` |

外加三条只在 PG 侧才有的对账：`customer.name` 的 `char_length=64`（`character varying(64)`），
`product.price` 的 `numeric_precision/scale = 10/2`，而**数组列的三个修饰值全空**——
`character varying(64)[]` 里那个 64 属于元素而不属于列，填上就是假话。`serial` 与 `identity`
都是 `integer`/`int`，分开靠 `default_value`：`customer.id` 是 `nextval(...)` 那一串，
`order_main.id`/`payment_record.id` 是 NULL（两份都不是生成列）。

**这一路的证据边界（本轮真跑说清楚）**：上面每一个数字都来自
`tests/integration/test_extract_pg_live.py` 的四条 live 用例——它们走真 `POST /api/datasources`
→ `POST /api/sync/jobs`（202）→ worker 的循环体 → 回读元数据库那一行，源库是真 `ai_web_demo_pg`、
账号是真 `demo_pg_ro`、§8.2 五条 SQL 一条不落空。两处按工单 016 的已定口径**没有**真起子进程
（`run_once` 在测试进程里代跑），而第 4~8 步的**界面**侧未逐字复跑，是"库里的行"证到的。
仍未证到的那一组也说清楚（**唯一一份清单在 metadata-model §8.2 末注"证到哪一步"**，别处只指它）：
`typtype='e'` 的真枚举类型在这个演示库里一个都没有（§1.5.4 实测 0 行），所以 §8.2 C 那条
`jsonb_agg(...)` 的形状 live 覆盖不到——本轮能证的只有"没有枚举时那一格落
**SQL NULL** 而不是 JSON `null`"，而这一句本身是 live 抓出来的（`enum_values` 一度 82 列全非空）；
同类的还有 `relkind` 的 `'p'/'m'/'f'`（夹具只有 `r`/`v`）、④ 的"两枚时间戳同时在场时取较晚那枚"
（实测 0 张表两枚都在场）、③ 的"每批重取 FK 会不会落两次"（默认批 200 只有一批）。
第 9 步的四条判定不在 live 用例里（用例只把"`PGUSER` 必须是 `demo_pg_ro`"钉成前置，越权那一格留给脚本）：
本轮由一次性 psql 复验跑过、跑完即删，仓库里可复跑的那一份住在 `scripts/demo_pg.ps1` 的收尾复验里
（§1.5.3 末条，"已建过"分支也会重跑一遍）。

## 2. 测试分层

### 2.1 单元测试重点（纯函数，不碰 DB / 不碰网络）

| 目标 | 断言方式 | 关键用例 |
|---|---|---|
| `token_estimate` | 值域 + 单调性 + 分支（as-built(0008)：本行六条**不由 008 全认领**。008 交付的是 heuristic 纯函数，钉了 ③④ + 五条手算值域用例（`tests/unit/test_token_estimate.py`）。as-built(P2-0010)：①② **在 P2 不做**——用户拍板"prompt 预算沿用 008 的 heuristic 口径，不引 `tiktoken`"，`AIWEB_RETRIEVAL__TOKENIZER` 键与真分词器参照一起推给引入依赖的那一档（roadmap §P4）；⑤⑥ 走的是结果集与 `TOKEN_BUDGET` 裁切，属 011（010 只做卡片段的预算裁切，用的就是这个 heuristic 函数） | ① [P4] 与 `tiktoken.encode` 计数偏差 ≤8%（`cl100k_base` 可用时）；② [P4] 强制走 heuristic 分支时中文/英文/混合三段偏差 ≤25%（宁高估勿低估）；③ 单调：文本变长则估计不减；④ 空串=0；⑤ 超长 JSON 行结果集不炸（011）；⑥ "结果集只喂摘要"路径的预算 ≤`TOKEN_BUDGET`（**as-built(P2-012)：这条改归 012**——它是 ⑨ 结论那一步的输入预算，住在 `pipeline` 而不是 executor；011 交付的是 ⑤ 那一半。**落地形态是"超预算告警、不裁切"**：`result_brief(token_budget=)` 算一遍**选中那份素材**的体量，超了就 `logger.warning`，一个字不删。三条理由：摘要分支比整表**更长**（要开 stats 表加两张头尾表），"装不下就换摘要"会把超预算变成更超预算；摘要再裁就只剩"共 N 行"，那不如让模型看全；语义与 P4 验收 6"被裁的表要记日志、不报错但要可查"同一条。用例三条——典型 60 行摘要 ≤1500（预算不是摆设，1500 用字面量而不是 `Settings`，跟着配置断等于断"它不小于自己"）、超预算仍回整表（不改道）、几百个数值列的摘要超预算时报警且内容一字不缩） |
| RRF 融合 | 手算黄金值 | ① 两路各 `[a,b,c]`/`[c,a,b]`、k=60 → 期望顺序与分数硬编码；② 只有一路有结果时仍参与；③ `keyword_weight=0` 退化为纯向量；④ 并列名次的稳定排序（同分按表名字典序，防抖动）；⑤ 空输入返回 `[]` 不抛 |
| `join_graph` BFS | 图与路径（as-built(P2-0010)：**整行不属于 010**。全仓没有 `join_graph` 实现，010 只渲染候选表之间的直连边；图算法与 architecture §5.3 的边权重/环/多路径歧义/降级四条一起归**工单 014 JOIN 图切片**） | ① 无 FK 但按命名约定推出边、`confidence` 按 §5.3 加权公式算（as-built(P2-0010)：两处口径都改了。常数"=0.7"与公式互斥，已由 010 闭环——0.7 现在是**公式地板**；原举例 `order_item.order_id → order_main.id` 在演示库**推不出来**：`order_id` 的名词是 `order`，库里没有 `order`/`orders` 表（真表叫 `order_main`），命名约定不认"前缀加后缀"的自由变体。库里真实存在的那条推断边是 `user_activity_log.product_id → product.id`（两侧同为 `INT`，实测算得 **1.00**），拿它当例）；② 自环 `parent_id` 不产生 1-hop 自引用；③ hops=2 时最长路径不超 2；④ 环（A→B→C→A）不死循环、路径去重；⑤ 桥表扩展：问 A、D 两表时能拉进 B/C，且 `path=[A,B,C,D]` 顺序可渲染进 prompt；⑥ 不可达时返回"需笛卡尔积"标记而不是硬连；⑦（010 转来的债）表度 > `MAX_JOIN_DEGREE` 的表**只禁当桥、不禁当端点**（它的直连边照旧进【可 JOIN】）——as-built(P2-0014)：本行**由 014 认领并加了第七行**；`hops` 数的是**桥表张数**（不是边数），跳数定上限、§5.3 的边权重只在上限内定胜负、权重再并列才算 `ambiguous`；图**存方向、按无向扩展**（真实 FK 恒为子→父，按有向可达则演示库除"子→父"一跳外一律不可达）；缓存键 `(datasource_id, database_id)`、同步终局后按库精确失效。**用例落点**：①~⑦ 全部在 `tests/unit/test_join_graph.py` 用手算图跑纯函数（本行口径"不依赖真库"），另加 `tests/integration/test_join_graph_rows.py` 钉唯一真 IO——读 `meta_relation` 的查询：演示库真边上 `payment_record↔product` 经 `order_main`+`order_item` 两张桥表可达、`category.parent_id` 自环不产生 1-hop 自引用、`user_activity_log→product` 带 1.00。写侧的字段度只做**告警**不丢边（丢弃分支在现行减法下不可达，理由见 architecture §5.3 的 as-built(P2-0014)），钉在 `tests/unit/test_sync_infer.py`（六条，`high_degree_fields` 的门槛是**严格大于**）+ `tests/integration/test_sync_pg.py`（断的是同步**响应**里的 `warnings`：`join_field_too_generic` 恰好一条、detail 带"出现在 10 张表"，而 `counters.relations_inferred == 9` 一条不少）。**as-built(P2-014)：本行已交付**——公共表面是 `join_graph.py` 的 `load_relations`/`graph_for`/`expansion_for`/`structure_hints`/`joinable_edges`/`invalidate` **六个**入口（纯函数 `build_graph`/`expand` 由单测直接钉）。①~⑦ 落 `tests/unit/test_join_graph.py`（**20 条**，全手算图）：验收 ①~⑦ 各一条，加的是门槛边界那一格（度 8 不抑制、度 9 抑制，且用例先断"度确实是 9"再断行为——门槛写成 `>=` 时不会假过）、自环那条钉的是 `number_of_edges() == 0`（"进图后被特殊对待"和"根本不进图"在路径断言上分不出来）、**同一对列上 `manual` 与 `extracted` 并存**时的覆盖顺序（真库的 `uq_meta_relation_key` 认这三档，不钉就是后加入的悄悄赢）、结构提示不受度门槛管、缓存三条（按库分键 / 失效只清那一个库 / 有条数上限）与"命中缓存不查库、但边变了只能靠失效"。真库那一半落 `tests/integration/test_join_graph_rows.py`（**8 条**，用例自己插行、不依赖演示库当前状态）：`payment_record↔product` 穿两张桥表、`hops=1` 时那一对就是连不上、`category` 自环照常参与跨表、0.700 那档被门槛筛掉、候选跨两个库时那一对按 `needs_cartesian` 报、新落库的边要等同步失效才进图。缓存失效落 `tests/integration/test_sync_cache_pg.py`（3 条：成功清、partial 只清提交过的库、一个都没写成则一个都不清），接线那一半落 `tests/integration/test_pipeline_orchestration.py`（**5 条**：桥表卡片被补查且进守卫白名单、**补不出卡片时整条路径撤下而不是留半条**、`hops`/`max_degree` 真从 `Settings` 传下去而 `structure_hints` 两个都不吃、单候选不降级、两候选零路径才降级）。真链路一条实录见 architecture §4.1 的 as-built(P2-014)。 |
| `chart_advisor` | 规则决策表 | 时间+数值→line、类别(NDV≤12)+数值→bar、NDV>12+数值→bar(取 top10 + "其他")、双数值→scatter、单行单值→`kpi`、全 NULL 列→table only、行数 >200→不画图；`EChartsOption` 结构快照（键存在性，不比像素）。as-built(P2-012 开工前拍板)：本行**由 012 认领**（此前 architecture §2 的目录树里挂着 `chart_advisor.py` 却没有任何工单写它，roadmap P8 验收 5 是它的 UI 双证、不是它的出生地）。两处字面改了：① "单行单值→number" 与 architecture §4.2 的 `kpi` 是同一样东西的两个名字，**统一写 `kpi`**（`chart_spec.type` 是要进 golden 的字面，两个名字等于两条对不上的断言）；② 快照对象是**自有形状** `{type,x,series,title}`（metadata-model §2.7），不是 `EChartsOption`——翻译那一层归 P8，理由见 architecture §6.1 的 as-built |
| PG/MySQL 类型归一 | 映射表（as-built(P3-023)：本行**由 023 交付**。用例三张——`tests/unit/test_pg_type_normalize.py`（PG 原料）、`tests/unit/test_mysql_type_normalize.py`（MySQL 原料 + `rows_to_columns()` 的接线）、`tests/unit/test_type_domain.py`（两方言共用的**那张断言表**，文件名以 `test_` 开头是为了让它真被收集执行）。规范字面全仓唯一住在 `app/extractor/type_normalize.py`（`VALUE_DOMAIN` + 两张 SYNONYMS + `compose`），`postgres_types.py`/`mysql_types.py` 只做方言原料的解析。归一落**抽取层（写侧）**：`data_type` 存归一值、`raw_data_type` 存 `COLUMN_TYPE` 原文）| PG `_text[]`/`numeric(10,2)`/`varchar(64)`/`timestamptz`/`jsonb`/`serial`/`generated always`；MySQL `decimal(10,0) unsigned zerofill`→**`numeric(10,0)`**、`enum('a','b')`→`enum`、`set`、`tinyint(1)`→**`bool`**（原行的"→bool 与否"问号已收，理由见 §9 as-built(P3-023)）、`datetime(3)`、生成列、`utf8mb4_0900_ai_ci`(8.0) 出现在 5.7 时的容错。跨方言塌成一个字面的两族：布尔（`boolean`/`tinyint(1)`→`bool`）与任意精度小数（`numeric`/`decimal`/`dec`/`fixed`→`numeric`，**值域里没有 `decimal`**，由 `test_decimal_不是归一值域的成员` 钉）。真跑那一半落 `tests/integration/test_sync_live.py` 的最后一张用例（工单 023 验收 5：演示库每一列的 `data_type` 过共享值域断言、不含空格即无修饰残留，再对八列点名比对 `(data_type, raw_data_type)` 成对）。**PG 侧的接线归 024**（PG 抽取器本体），本片只交付纯函数 + 用例 |
| 卡片模板 golden | 快照 | `tests/fixtures/prompts/kb_card__{table}.expected.txt`，`{{ }}` 空白控制要断言（不然 diff 全是空白）；覆盖：无注释表、60 列宽表、纯视图、含 enum 列、无 PK 表、中文/反引号/`a b` 空格表名（转义必须可见）。as-built(0008)：宽表的第 2/3 段各多一份快照，命名 `kb_card__{table}__seq{n}.expected.txt`（原模式只有一个 `{table}` 槽位，而一张宽表要出 3 份文本）；快照是**按 §5 模板手写**的，不是从渲染器 dump 的——dump 只能证明"以后没变"，手写才证明"渲染出来的就是文档那一份"。六个场景落 `tests/unit/test_kb_card_golden.py`，空白控制是一条独立断言（首尾裸换行 / 空行 / 行尾空白 / 行首缩进四类各钉一次） |
| `prompt_builder` | 结构断言 | 段落顺序（角色→硬约束→schema→JOIN→术语→示例→问题→输出格式）、"必须带 LIMIT"与"只能引用给定的表"两句恒定存在（as-built(P2-0010)：原写的是"禁止引用未给出表"，与 roadmap §P2 踩坑 ④ 的措辞不一致，而 golden 要锁字面，用户拍板统一成后者）、few-shot 段在预算不足时被**整段**丢弃。<br>as-built(P2-010 双轴审查后补三条)：① **golden 逐字节**锁整段 user 消息（`tests/fixtures/prompts/nl2sql__order_main.expected.txt`，手写不是 dump）；② 预算估算用的**行形状**与 j2 模板渲染出的行**同源**——`_term_line` / `_example_line` 各由一条"手写期望行 == 渲染在场 == 估算式产出"的三段断言钉住，改模板改不出漂移；③ ≥0.8 门槛的辖区**只是【可 JOIN】清单**，卡片【可关联】行里的同一条低置信边照旧在场（口径见 architecture §5.3）；边按**起点**筛不按终点筛，目标表被裁掉时标题那句"不要写进 SQL"就是唯一防线 |
| `pipeline` 编排 | 接缝在 `pipeline.ask()`，LLM 走 respx、检索走 `Retriever` Protocol 的桩 | as-built(P2-012)：本行由 012 新增。① 顺序：检索 → 组装 → 生成 → 守卫 → 执行，**守卫在 executor 被调用之前**（spy 断言"守卫拒绝时 executor 一次都没进"，不是看日志）；② 早退三类各留一行 `chat_messages`：`NO_SCHEMA_FOUND` / 畸形返回 / `sql_guard_rejected`，且都不落 CSV；③ 素材齐料：`build_prompt` 收到的 `token_budget`/`few_shot_budget` 来自 `Settings`（010 的硬接线义务，漏传就是静默不裁切，用例直接断言装配点传给 `build_prompt` 的关键字参数值）；④ 畸形返回四档：围栏与前后附解释**能恢复**出同一条 SQL、截断**不恢复**且抬 `llm_bad_response`、`clarify` 非空不执行；⑤ 每步耗时在场（roadmap P2 踩坑 ② 那句"卡在哪一步"的可观测面）；⑥ **⑦ 的入参键集合被钉死**（多一个键就是调用方能传进来的一个入参，`row_limit`/`max_cell_chars`/`result_dir` 一旦被请求体摸到，L4/L3 口径与 ADR-0004 同时作废），`password` 断的是 `decrypt_secret` 那条线；⑦ **跨源只取相关度第一名的那个源**，断的是"补卡片的请求只带同源 uid"（断 prompt 里没它是桩造的假象——桩对任何 uid 都回同一张表）。<br>as-built(P2-012 施工后)：用例落点**不是工单原写的 `tests/unit/test_pipeline.py`**，而是 `tests/integration/test_pipeline_orchestration.py`（`pg` marker）——早退三类的验收本体是"留下一行 `chat_messages`"，不落库的断言只证明了返回值、没证明留痕。真跑一次 LLM 的那条是 `test_pipeline_live.py`（`live` marker）。**当时已知的洞**：模型按 ④ 模板写了 `LIMIT` 时守卫不补 `+1` 探针，`truncated` 恒假，本行 ⑤ 的耗时与 ② 的早退都真、只有这一格是死的——**2026-09-29 同日拍板闭环**，守卫改成"钳位 + 探针"（safety §1.2 ⑦ / §9.2 ⑧），撞上限那一支的 `truncated` 活了；模型自己写小于上限的 `LIMIT n` 那一支仍无探针、恒假，是有意的语义。<br>**as-built(P2-013)**：本行 ② 的三类早退补了两件东西。① **守卫拒绝那一支打了真 SQL**——用例 `test_模型硬产出_DROP_时守卫拦下且一次都不进执行器` 让路线回复直接产出 `DROP TABLE orders`，断 `executor` spy **零调用**、`sql_final is None`、违规 code `top_level_not_select`、落库行保留模型 `sql_raw` 原文、且结果目录**空**（没执行就不该有文件）。② **拒答文案有断言**——`no_schema_found` 那一行的 message 必须同时含"补录表注释 / 检查授权 / 同步"三个词（用例断的是这三个**词**，不是整句），CLI 侧另由 `test_demo_ask_print.py` 三条用例钉"每一种拒答都跟一行下一步动作"、"守卫拒绝那步说清了没执行也没落文件"、"无匹配表那步给的是今天真能走的三条动作"（`POST /api/sync/jobs` 而不是界面按钮）；分层本身还有一条反断言——服务端 message 里**不许出现"点此"**（界面话属于 P8 那一层，见 architecture §4.1 的 as-built(P2-013)） |
| L1 检索三件纯函数（as-built(0009)：`query_terms` / `column_hits` / `rank_candidates`，`app/services/nl2sql/retriever.py`） | 手算黄金值，不碰 DB | ① 切词：`订单金额是多少` 只发二字滑窗（7 字 > 整段阈值 4）、`订单金额` 整段+滑窗都发、`pay_amount` 出整段与两个子词、单字段被丢；② 命中：同一列被两个词命中算两行理由但只算**一列**，比对字段按 `column_name → comment_raw → comment_zh` 顺序各查一次，判定用"子串 + casefold"以与 SQL 侧的 `ILIKE '%term%'` 严格同构；③ 排序：主键**命中的不同词数**、次键命中列数、`table_uid` 收尾（architecture §5.1 那条 as-built 注），同表多卡 `boost=0.05×(n-1)` 只进 `score_kw` 不进排序键；④ `k` 是名额上限；⑤ 零命中返回 `[]` 而不是猜一张表（阶梯 L4 的判据，013 要用） |
| `executor`（as-built(P2-011)：`app/services/nl2sql/executor.py`，会话级只读 + 超时 + 截断 + csv 落盘） | 离线纯函数手算 + live 真连演示库 | 离线八件（`tests/unit/test_executor_*.py`，共 31 用例）：① `serialize_cell` — Decimal 按 `str` 原样回（`"0.30"` 不变 `"0.3"`）、naive/aware `datetime` 走 `isoformat()`（前者无偏移后缀、后者保留 `+08:00`）、`date` 走 `YYYY-MM-DD`、`time` 走 `isoformat()`（TIME 列 → `"14:30:00"`）、`timedelta` 按 **MySQL TIME 字面量** `[-]H:MM:SS` 手工拆（`1:00:00`/`-1:00:00`/`30:00:00`/`0:00:45`——**不是** `str(timedelta)` 的 Python 规范形 `"-1 day, 23:00:00"`，否则 §3 第 10 步的逐位对数对不上）、`bytes` 分 `<binary 3B>` 与 `<binary 1.2KB>` 两档、超 `max_cell_chars` 的 str 截断、int/float/bool/None 原样透传；② `split_truncated` — 1001 行 & `row_limit=1000` → `truncated=True` 且末行为探针被丢、500/1000/`[]` 三种边界都不截断；③ `new_run_id` + `result_csv_path` — 形状 `[A-Za-z0-9]+`（生成器与校验器**共用一份正则**，防止"生成的名字过不了自己的门"），越界 run_id（`../etc/passwd`、`/abs/x`、`a\x00b`、`a/b`、`..`、`""`）一律 `ValueError`；④ `write_result_csv` — `utf-8-sig` BOM 头幂等回读、自动建父目录、逗号+引号 roundtrip；⑤ `enforce_readonly_grants` — SELECT-only 通过、`INSERT/UPDATE/DELETE ON ai_web_demo.*` 或 `ALL PRIVILEGES ON *.*` 抛 `ReadonlyCapabilityMissing`；⑥ `classify_source_error` — errno 3024/`"57014"` 翻成 `QueryTimeout`（detail 必须同时含 `timeout_ms` 数值与来源键名），非超时的 1146 原样 re-raise；⑦ `session_statements` — MySQL 顺序 `READ ONLY` 必须**第一位**、`wait_timeout` 用 `timeout_ms//1000+10`（15000→25），PG 走 `SET statement_timeout = '<n>ms'`；⑧ `resolve_timeout`（双轴审查收口新增）— 列上有值走 `data_sources.timeout_ms`、列上缺值落 `AIWEB_QUERY__TIMEOUT_MS` 且值必须 >0；**来源判定不在调用方**（`execute_readonly` 不收这两个入参）；⑨ **`resolve_result_file`**（as-built(P2-013)，下载侧那半边，`tests/unit/test_executor_path.py`）——三道门（名字不合法 / realpath 逃出目录 / 不是文件）塌成**同一档** `ResultFileUnavailable`，用例直接断 `{(code, message, status_code)}` **集合只有一个元素**且 `detail is None`（三种失败对外逐字相同，任何差别都是"run_id 是否存在"的 oracle），四个入参**逐字对上三道门**。形状门**复用写入侧的 `result_csv_path`** 而不是另写一份正则，它的 `ValueError` 在这里被翻掉——不翻就是客户端输入打出 500。门②（realpath 逃逸）**在本机真跑**：`os.symlink` 要特权（winerror 1314）但 **junction 不要**，而守卫判的是 `resolve()` 后的落点、与链接类型无关——`test_目录里的链接指向外面时拒绝` 用 junction 造逃逸，并断服务端日志出现 `reason=realpath 逃出目录`（异常种类分不出门②与门③，判定顺序被换掉时用例照样绿，所以要钉日志）；`test_结果目录自己是指向别处的链接时照常放行` 钉反面（目录级链接不算逃逸，运维挪结果盘的正当做法）。用例先试 `os.symlink` 再退 junction，两者都建不出来才 skip——skip 只可能意味着这台机器造不出该文件系统对象。**仍未证明的只剩一格**：指向**文件**的符号链接这一具体形态本机复现不出来（缺的是构造手段，不是代码分支），见 safety §7 的 as-built。<br>live 四条（`tests/integration/test_execute_live.py`，全 `pytest.mark.live`）：① 只读查询跑通 + Decimal/DATETIME 序列化 + csv 落在服务端拼的 `tmp_path` 内（不在 `data/results/`）；② `row_limit=5` 时 `truncated=True`、csv 恰好 6 行（1 表头 + 5 数据）；③ **引擎级写拒绝 ≠ 应用级写拒绝分开断言**（验收 ①）——应用侧 `check("update ...")` 抛 `SqlGuardError`，绕过守卫直喂 `execute_readonly` 时源库自己拒：`DBAPIError.orig.args[0] in (1792, 1142)`，**刻意不收 1046**（那说明是连接没选库的配置错、不是"写被拦"，混进来这条就假过）；④ **超时真中断**（验收 ②）——`select sleep(1) from order_item limit 5` 每行都睡 1 秒踩行边界（`select sleep(2)` 单行不响应，`count(*)` 也只吐一行；这是 MySQL `MAX_EXECUTION_TIME` 的坑，测试 docstring 已钉），命中 3024 → `QueryTimeout` 且 detail 含 `data_sources.timeout_ms` + `300`（键名由执行器从行上自己判出，不是用例传的） |
| `sync_job_event` → SSE 那一条链（as-built(P3-017)：`app/core/sse.py` 的 `sse_format` / `progress_frame` / `stream_job_events` + `job_queue.subscribe`） | 读侧**只碰元数据库**，不碰源库；写侧与读侧分两处钉，因为它们的失败模式完全不同（写侧错在"帧与库不符"，读侧错在"攒到底才吐"） | ① **帧形状**：带游标的帧是 `id: <seq>\nevent: progress\ndata: {…}\n\n`，`id` 在**第一行**（`Last-Event-ID` 靠它，顺序写反了浏览器存不到游标）；流的第一个块是 `retry: 3000`，收尾是 `event: done` 且**没有 `id`**。负载在 `progress_frame` 里**摊平一次**（`{stage, phase, done, total, base_table, view, cards, payload}`），端点里再展一遍就会出现"库里改了帧没改"的分叉。② **游标补读**：`read_events` 只有 `WHERE job_id=:j AND seq>:c ORDER BY seq` 一个形状；用例先读到 seq=2、断开、带 `?cursor=2` 与 `Last-Event-ID: 2` 各重连一次，两边都从 seq=3 起（一条不丢也不重），参数与头冲突时**查询参数赢**，两个都给不成数字时**回全量**而不是 400。这一条是"选事件表而不是纯推 NOTIFY"的全部理由。③ **心跳**：常量 `PING_INTERVAL_S == 15.0` 单独钉一次（它是文档承诺的数字，跟着它断等待时长等于断"它不小于自己"），行为侧把生成器的 `ping_after_s` 设成 0.02 逼出三帧 `: ping\n\n`，不真等 15s。④ **叫醒**：`subscribe` 用的是 `asyncio.Event`（去重语义免费：一个作业被叫醒十次和一次要做的事完全相同），且必须挂在**一条专用连接**上——会话自己那条连接会被读循环每轮的 `rollback()` 归还，池化下交给下一个借用者、NullPool 下直接关掉，两种结局都是"进度永远不动"且一条都不报错。引擎从 `session.bind` 取而不是 `get_bind()`（后者给的是同步 `Engine`，对它的 `.connect()` 抛 `MissingGreenlet`）。⑤ **终局判据是 `sync_jobs.finished_at` 那一格有没有被点上**，不是 `status`：终局三个值在 `schemas/sync.py` 已有一份，读侧再抄一份就是"两处规则各自漂移"的老路（010 那族教训）。⑥ **会话在流里开、在流里关**（`get_stream_sessionmaker` 给的是**工厂**不是会话：FastAPI 会在 `StreamingResponse` 开始迭代之前就关掉 yield 型依赖，端点里再 `async with` 一次是唯一活法，测试也必须覆盖这个依赖）。⑦ **真进费用例**（`test_sync_events_live.py`，`live`）：三个真进程（API + worker + pytest 这个"盯进度条的人"），顺序是**先入队 → 再开流 → 最后才放 worker 出门**，拿到首帧那一刻**当场**回库读 `finished_at` 断它还是 NULL——"首帧在作业结束之前到达"这句话在 `httpx.ASGITransport` 上没有对应的事实（那个传输把 body 片段攒进 list 才返回，`resp.is_closed` 恒真），所以这一条必须走真 HTTP。用例落点：`tests/unit/test_sync_stage_vocabulary.py`（4 条，映射 9 值逐条写死）+ `tests/unit/test_sync_event_ddl.py`（6 条）+ `tests/integration/test_sync_events_pg.py`（5 条，写侧）+ `tests/integration/test_sync_events_sse_pg.py`（6 条，读侧）+ 真进程那 1 条。演示库上的分母实录 `total=10`（9 表 + 1 视图，不是 `SHOW FULL TABLES` 的 11） |
| `job_queue` 两条语句的**形状**（as-built(P3-016)：`app/services/job_queue.py` 的 `enqueue_stmt` / `claim_stmt`） | 编译成 PG 方言文本比对，**不执行**（与 `test_sync_upsert_sql.py` 同一条理由：进程分离的三条口径——互斥靠 `ux_sync_running`、claim 靠**条件更新行数**、排队中的 pending 也算未结束——全是"语句长什么样"的事，写错一个谓词在真库上照样可能绿。`claim` 少外层那个 `status='pending'` 就是两个 worker 同时抽同一个源，而这一格要到 019 的僵尸回收真跑起来才看得见） | ① 入队只写 `pending`：`INSERT` 里不许出现 `running`，也不许顺手把 `started_at` / `heartbeat_at` 点上（那两列是 claim 的钟，也是"响应返回时这一轮还没开始跑"的可断言证据）；② claim 两处条件缺一不可：`FOR UPDATE SKIP LOCKED`（多 worker 各拿一个、谁都不等谁）+ 外层 `AND status='pending' RETURNING`（行数才是"我抢到了"的判据），且终局那几列（`finished_at`/`counters`）不许出现在这里。行为侧的七条在 `tests/integration/test_sync_enqueue_pg.py`（202/409/第二 worker 空手/源删后级联带走 job 行/终局三样/入队顺序） |
| `run_worker` 一轮心跳的三条语句（as-built(P3-018)：`scripts/run_worker.py` 的 `heartbeat_update_stmt` / `reclaim_update_stmt` / `retention_delete_stmt`） | 同一套手法：编译成 PG 方言文本比对，**不执行**。三条口径全是"语句长什么样"的事——心跳只动 `heartbeat_at` 那一格、回收往 `errors`（`jsonb`，**没有 `error` 这一列**）里**追加**、保留期只删 `sync_job_event`——写错一个谓词或把追加写成覆盖，在真库上照样可能绿（`tests/unit/test_worker_reclaim.py`，7 条） | ① 刷钟那条里 `finished_at`/`errors`/`failed` 一个字都不许出现（它是"我还活着"的声明不是终局），且带 `status IN ('pending','running')` 守卫：收尾先落定时这一句必须打不中；② 回收那条逐字对上 §6 表格原句——`errors = (errors || CAST('[{"code":"reclaimed","detail":"heartbeat 超时"}]' AS JSONB))`、`heartbeat_at < now() - make_interval(secs => N)`、`RETURNING id` 三者缺一不可，条目常量 `RECLAIM_ENTRY_JSON` 直接抄文档字面量（`json.loads` 比回去只能证明序列化没变形，证明不了那两个词没被改写）；③ **阈值是入参不是字面量**：换 42 重编译，文本里必须出现 42 且不出现 180；④ 保留期那条编译文本里只许出现 `sync_job_event`，出现 `sync_jobs` 就是灭迹。⑤ **三个键各喂给哪个参数**：`cadence(settings)` 是那条映射的唯一住处（`loop()` 里不出现第二个 `settings.extract.*`），用例拿三个互不相同的数（11/181/31）断它——这条是补上来的：把 `interval_s` 与 `stale_seconds` 互换后跑完整轮，**只有这一条红**（真效果是刷钟 181s、回收阈值 11s，正在跑的作业被自己的 worker 判成僵尸）。⑥ **阈值不是整数就当场抛**：`180.7` / `True` / `"7; DROP TABLE x"` / `None` 四种都从**公共** builder 驱动，期望 `(TypeError, ValueError)`；同一用例再断 `180` 正常编译，否则"全拒"和"没生效"分不开。理由是实测：`int()` 式闸门会把 180.7 悄悄截成 180，而 `make_interval` 那条语句的全部语义就是"差多少秒算僵尸"。行为侧五条在 `tests/integration/test_worker_heartbeat_pg.py`：主事务未提交时换连接已可见、一轮循环改判 + 补终局事件、NOTIFY 丢失时最迟一个心跳周期仍读到、179s 不动 / 181s 判失败 / `pending@NULL` 不杀、保留期删 31 天那行且 `data/results/` 清单快照逐字节相同 |
| MySQL 读侧的**发送切片**（as-built(P3-020)：`app/extractor/mysql.py` 的 `slice_names` + `collect()` 的批循环 + `SourceManifest.batches`） | 切片是纯函数、批是编排，两层分开钉：`collect` 的批语义钉在**假连接**上（`_rows` 是方言层唯一的 I/O 出口，同 §2.1 `test_extract_mysql_client` 那条理由），真库那一份钉在拦下来的语句清单上。为什么必须两层：假件看得见"第 k 条昂贵查询收到哪几个表名"，真库只看得见条数与顺序，而这两件都不是"总共发了几条" | 单测 11 条在 `tests/unit/test_extract_mysql_batching.py`：① 验收 2 那三个边界各一条（整除无空末批 / 末批只剩 1 张 / `batch>N` 退化成一批，外加空名单回 `[]` 而不是 `[[]]`——`collect` 靠"没有表就不发 C/D/E"躲开 `IN ()` 这个语法错误）；② `batch_size` 非正整数当场抛（`0` 会让切片要么死循环要么空清单）；③ 三条昂贵查询各 4 发、**每发各自的名单**——那四刀是手写的字面量，不写 `slice_names(VISIBLE, 3)`：用被测的那把刀去算刀口，红了也只是"刀和期望一起改错"；④ 反注入两条一起断（占位符 `:tbl_0..n` 与这一批的表名**逐一对应**，且语句文本里一个真表名都不出现——只断前者的话"`IN (:` + 拼接"那种形状照样过）；⑤ 退化路径与分批前同形（默认 200 在 10 个对象上必须退回"一 schema 四条"，防"分批把自己变成永远多发"）；⑥ 分批不改归并结果（批大小 3 与 200 抽出的列/索引逐个相同）；⑦ `MAX_TABLES` 仍在昂贵查询**之前**判（断的是 IS 语句**条数**==1，不是错误码——把门槛挪到 C/D/E 之后也照样抛同一个错）；⑧ 批间隔用耗时下限钉（120ms×3 个间隔 → `>=0.36`，并留 `<0.6` 的上限排除"每批之前都睡"）。live 两条在 `tests/integration/test_extract_mysql_live_batches.py`（验收 1/3/4/5/7）：批名单同时出现在事件负载与那三条昂贵查询的 `IN` 清单里，两边逐批相等；间隔 250ms 时 4 批之间三个间隙各 `>=0.25s`（实测同一份源：间隔 0 那一轮 `duration=1125ms`、间隔 250 那一轮 `1985ms`，差出来的就是那三次让气）。名单**不硬写"第一批是哪三张"**——§8.1 B 没有 `ORDER BY`，交付顺序不是源库的承诺；断的是每批的成员（批大小逐个钉 3/3/3/1、批间不重叠、并起来正好是 §1 那份 10 个对象的名单）。**验收 5 比的是另一次真跑而不是 008 的 golden**——那七份快照喂的是手工摆出来的 `meta_*` 输入（见上面卡片 golden 行），拿它对演示库等于比两件本来不该相等的事；做法是先按 200 跑一轮、再按 3 跑一轮，比五张 `meta_*` 行数与全部卡片 `text_md` 逐字符，只放行 `counters.batches` 那一格变化。**同一轮还比索引那条链的形状**（验收 7"P3 只补'分批后每批都还读到'那一格"）：两侧各取一份按 `(表名, 索引名, SEQ_IN_INDEX)` 排序的 `(列名, SUB_PART, cardinality 是否非空)` 清单逐位相等，再对分批那一次断每一行 `cardinality` 都非空、且全库唯一那处前缀索引 `product.name(32)` 仍在场。比"非空"而不比**数值**：`CARDINALITY` 是 InnoDB 的采样估算，两次真跑之间它可以合法地变，把"分批没改变结果"钉在它上面就变成看运气发红。这跟 007 那条 `test_sync_live.py::test_验收2_cardinality_与_sub_part_真的有非空值` 不互替——那一条走的是默认批大小（10 个对象一批装完），证的是链通着；"切成四批后每批还读得到"只有走分批那条路才答得出。实测变异：D 条投影换成 `NULL AS CARDINALITY, NULL AS SUB_PART` 后这条 live 用例红在 `has_cardinality` 那一行（007 那条同步变红，两个靶心各自独立）。**两个键都走真配置通道**（`monkeypatch.setenv` + `get_settings.cache_clear()`，同 021 的 `max_tables_one`）：桩掉 `get_settings` 的话"`run_sync` 有没有把配置传给方言"就永远验不到。实测过——把 `sync_service.py:989` 那行换成硬编码 `200`：`test_sync_events_pg.py` 只红 `test_分批的两个配置键真有读取点_每批的表名随_tables_帧交回` 那一条，live 两条**一起红**（`counters.batches` 变成 1），而方言层那 11 条单测全绿——它们自己传 `batch_size`，本来就不该替编排层背书 |
| PG 抽取器的**两半**（as-built(P3-024)：不连库那半是 `app/extractor/postgres.py` 的五条 §8.2 SQL 与行→`Raw*` 映射，加上 `sync_service` 的方言驱动表 `_DIALECTS`；live 那半是 §1.6 那份对账清单） | SQL 文本与映射钉在**假连接**上（`_rows` 仍是方言层唯一的 I/O 出口，同上面 020 那行的理由），501 分发与"列写法跟着 kind"钉在纯函数 + 真 `run_sync` 上 | **30 条不连库单测分两张**：`tests/unit/test_extract_pg_map.py` 14 条吃 §8.2 那条查询**自己的别名**当行键（假件与真 SQL 同形才有意义），钉的是 serial 与 identity 分得开（`default_value` 是 `nextval(...)` 那一串）、`_text[]`/`numeric(10,2)[]` 走 023 的归一而 `raw_data_type` 留原文、`modifiers` 白名单只认 `varchar`/`bpchar`/`numeric` 且数组一律 `NULL`、表达式索引那一位的 `column_name` 是 `NULL`（§8.2 D 的分支）、视图 `last_analyze_at` 恒 `NULL` 而表取 `last_analyze`/`last_autoanalyze` **较晚者**（§2.4 注④ 的 PG 半边）、`idx_scan` 只进 `cardinality_hint` 不进 `cardinality`（单位不同）。`tests/unit/test_extract_pg_client.py` 16 条把编排钉在同一颗桩上：`probe` 的三值 + **只有 42704 才降级**（`OperationalError` 且 `sqlstate is None` 不许被吞——那是连不上，不是版本不支持）、`discover` 排除三个系统 schema 与 `pg\_temp%`/`pg\_toast%`、四条查询的发出顺序 `pg_class → pg_index → pg_attribute → pg_constraint`、空范围**不发 `IN ()`**、`MAX_TABLES` 仍在昂贵查询之前判且三条出路原文到达、PG 侧永不产出 `CHARSET_SUSPECT`（那一格的原料是 MySQL 的会话变量）、020 的批形状在 PG 上同形（10 对象/批 3 → 4 批、昂贵查询 12 发、`:tbl_N` 与名单逐一对应、表名不拼进文本）、以及"方言层不许发写语句"——那条用**词边界正则**而不是子串禁（`AS ON_UPDATE` 里的 `ON ` 会被子串 ban 误杀，误杀一次就没人再敢加别名）。**驱动表 10 条**在 `tests/unit/test_sync_dialect_dispatch.py`，钉工单 024 验收 2 的两面：`kind='postgres'` 不再命中 `NotImplementedSource`（只构造不连接，所以演示源没建时这条不会 skip——skip 掉的正好是要钉的那一句），而 `oracle`/`mssql`/`MySQL`/`""` 仍抛且 `code=='not_implemented'`（不许顺手放宽）；另有一条配对断言，把 `table_scope_filter(ds, column=该格的 scope_column)` 渲染出的片段拿去断它**逐字出现在该方言的 B 查询里**（构造器由测试自己按 kind 查表，不从 `_DIALECTS` 拿——从被测那张表读出的构造器去验同一张表，红了只是两格一起改错）。**接线那一条是集成用例**：`test_sync_pg.py::test_postgres_源的范围条件按_pg_的列写法下发`——`kind='postgres'` 的源跑真 `run_sync`，形状桩件把收到的 `table_sql` 原样记下（桩件自己不渲染口径）。实测变异：把编排里那一行换回硬编码 `t.table_name` 之后**只有这一条红**，上面 40 条全绿（它们都不经过 `run_sync`），红了的内容是"PG 源带着 MySQL 的别名出发了"，而那一句本来要等源库报 42703/1054 才看得见。**as-built(P3-024 live 半边)：那条 SQL 已经在真 `ai_web_demo_pg` 上跑过**——`tests/integration/test_extract_pg_live.py` 四条（验收 1/3/4/5），走真 `POST /api/datasources` → `POST /api/sync/jobs`（202）→ worker 循环体 → 回读元数据库，源库与账号都是真的，逐字对账清单与"证到哪一格"写在 §1.6/§1.6.1；仍没证到的那一组（枚举数组形状、`relkind` 的 `'p'/'m'/'f'`、④ 的"两枚时间戳同时在场时取较晚那枚"、③ 的"每批重取 FK 会不会落两次"）**逐条只列在 metadata-model §8.2 末注"证到哪一步"那一份里**，本行不复述清单以免两处各漂一次。live 另钉到一个**缺口**而不是未证项：表达式索引的文本有列可存（§2.4 `meta_index.funcdef`），但 `RawIndexColumn` 不搬它，所以当前恒空。live 抓出两个**不在 PG 抽取器里**的缺陷并已修：① `inferred_rows` 把表 id 的查找键写死成 `_key("", …)`（MySQL 的 catalog 恒空串惯例），PG 侧每条推断边都查不到而**静默丢弃**（`relations_inferred` 报 0），修法是让 `InferredRelation` 带上它本来就该带的 `catalog_name`，`tests/unit/test_sync_rows.py` 补一条纯函数用例钉住（变异验过：换回 `""` 只有这条红）；② `meta_column.enum_values` 落的不是 SQL NULL 而是 JSON `null`（SQLAlchemy 的 JSON 类型默认 `none_as_null=False`，把 Python `None` 序列成 `'null'::jsonb`，于是 `IS NOT NULL` 成立、82 列全都"有枚举值域"），修法是 `enum_values`/`sample_values` 两格显式 `JSONB(none_as_null=True)`——只改绑定值不动 DDL，而 `_COLUMN_SYNC_COLS` 是整列覆盖，重跑一次同步就自清。**验收 6（MySQL 侧回归不变）在两个修复之后随整轮闸重跑**：被搬动的 `slice_names`/`in_list`/`apply_index_flags` 那三样正对着 MySQL 链，细节记在 roadmap §P3 验收 6 的 as-built |

配置层的已落地单测：嵌套 `__` 解析、DSN 凭据转义（`u@site` / `p@ss#w/1` → `%40`/`%23`/`%2F`）、
`masked_dsn` 不出明文、embedding 维度 >2000 被拒、部分配置时 `configured=False`、
默认 JWT secret 被标记、本地默认值（`host=127.0.0.1`、`allow_sql_edit=admin`、`sample_distinct=true`）、
`/api/healthz` 不碰数据库 + `X-Request-Id` 头存在。
> **as-built(P2-013)**：新增三条目录基准用例（`tests/unit/test_settings.py`）——读出来就是绝对路径、
> 相对路径按**仓库根**解析而与 cwd 无关、绝对路径原样保留。期望根由测试文件自己的
> `Path(__file__).resolve().parents[3]` 独立算出，不引用被测的 `REPO_ROOT`（拿被测常量断被测常量
> 就是同义反复）。配套一条 `test_check_env_secrets.py`：目录体检报出的路径**逐字等于**
> `settings.result.dir`——013 之前这两处一个跟 cwd 走、一个按 `backend/` 的父目录拼，可以互相错开
> 都显示 ok，这条断言钉的就是"体检看到的是执行器真会写的那个目录"。

### 2.2 没有本地 PostgreSQL 时

**结论先行：不要试图用 SQLite 跑迁移，也不要用 SQLite 假装 PG。**

1. **pgvector 集成测试条件跳过**
   - `pyproject.toml` 注册 marker：`markers = ["pg: 需要真实 PostgreSQL + pgvector，通过 AIWEB_PG_TEST_DSN 提供"]`。
   - `conftest.py` 里做 *skip 而非 collection error*：
     ```python
     def pytest_collection_modifyitems(config, items):
         if os.getenv("AIWEB_PG_TEST_DSN"): return
         skip = pytest.mark.skip(reason="AIWEB_PG_TEST_DSN 未设置，跳过 pgvector 集成测试")
         for it in items:
             if "pg" in it.keywords: it.add_marker(skip)
     ```
     （用 `pytest.importorskip`/`skipif` 会让"忘设变量"和"代码坏了"无法区分；
     marker + `--strict-markers` 更好。再加 `AIWEB_REQUIRE_PG_TESTS=1`：设了它 yet 无 DSN 就直接
     `exit 2`，供未来 CI 用。已落地的 `tests/conftest.py` 正是这个形状。）
   - 每个 pg 测试会话**独占随机 schema**：`aiweb_test_<uuid8>`，通过 `search_path` + 覆盖
     `settings.pg.schema_name` 跑 `alembic upgrade head`，session 结束 `DROP SCHEMA ... CASCADE`。
     绝不动 `aiweb`（那里面有真卡片）。
   - 远端 PG 上专建"测试专用 database `aiweb_test`"（不是 schema），给应用账号该库全权，
     避免测试把生产库撑大 / 误 drop。
2. **迁移层在无 DB 情况下的验证（schema-only 断言）**
   - 用 `sqlalchemy.schema.CreateTable(tbl).compile(dialect=postgresql.dialect())` 与 `CreateIndex`
     对 **DDL 字符串**做断言：包含 `vector(1536)`、`USING hnsw`、`WITH (m=16, ef_construction=64)`、
     `USING gin (search_text gin_trgm_ops)`、`CREATE UNIQUE INDEX ... WHERE status = 'running'`、
     `SET SCHEMA` / `"aiweb".` 前缀。90% 的"迁移写错了"能在无 DB 下被抓到，且跑得极快。
   - **断言字面的三种写法要分清（as-built(0008)）**：同一条 HNSW 索引在三个地方长得不一样，
     照本行的字面去断言会必红：
     ① SQLAlchemy 渲染出的是 `WITH (m = 16, ef_construction = 64)`（等号两边带空格）；
     ② `metadata-model §2.6` 的手写 DDL 也是这个带空格的形状；
     ③ 真库 `pg_indexes.indexdef` 回显成 `WITH (m='16', ef_construction='64')`（数字带引号）。
     所以 schema-only 断言按 ① 的字面比，pg 断言则先把空格和引号抹掉再比 ②/③——
     这不是"断言写松了"，而是**渲染层与回显层本来就不是同一个字符串**，
     硬要求一处字面统一反而会把实现逼成字符串拼接。
   - `vector(dim)` 的渲染方式（as-built(0005)）：`metadata-model §2.6` 原写"迁移里用 `op.execute` 渲染"，
     实现改走 `pgvector.sqlalchemy.Vector(dim)`（`op.execute` 拼字符串会让列宽与 ORM 的类型各写一份，
     漂移无人看守）。渲染出的类型名是大写 `VECTOR(1536)`，断言时统一 `.upper()` 再比。
   - ORM↔迁移漂移：`alembic check`（有 DSN 时跑，无 DSN skip）。
   - **剩下的 10%（HNSW 真能建、`vector` 维度上限、trgm 可用、`SET LOCAL hnsw.ef_search` 生效）
     无法在 SQLite 上验证**，只能靠远端 `aiweb_test` 库——这是必须争取远端 PG 访问权的核心理由。
   - 明确禁止：不要给模型加 SQLite 方言 shim（`Vector` 类型在 SQLite 上渲染不出来，
     硬凑会引入"只为测试存在的生产代码分支"）。
3. **LLM / Embedding 用 `respx` mock**
   - 两个 client 统一走**注入的 `httpx.AsyncClient`**（`deps.py` 提供），测试里
     `respx.mock(base_url=settings.llm.base_url)` 拦截，不要 mock `openai` SDK。
   - 回放 fixture：`tests/fixtures/llm/{sql_ok,sql_with_garbage_markdown,sql_reject_and_retry,json_truncated,timeout_504,rate_limit_429}.json`。
     三个必测的畸形返回形态：**包在 ```sql 里**、**后面跟解释文字**、**JSON 被 max_tokens 截断**
     —— 解析层要各自有明确降级路径。
   - `respx` 加 `AssertionError` side_effect 模拟超时，断言 pipeline 在 `LLM__MAX_RETRIES` 内退出
     且 SSE 发 `error{code:"llm_timeout"}`。
   - embedding mock 返回**由输入文本 hash 决定的确定性维度向量**，
     这样"相似问题向量近邻"的单测可复现，不用真调计费 API。
   - 另设 `@pytest.mark.live` 打真 API（默认 `addopts = -m "not live"`），手工跑，用于每日确认上游没改行为。
     **as-built(P3-017)**：这一句的前半**从未落地**，`addopts` 至今是 `-q --strict-markers`，
     所以 `make test` / `dev.ps1 check` 里 live 用例是**跟着闸一起跑**的，不是手工档。
     这不等于"CI 上没有 DSN 就会红"：live 用例的前置条件是 PG DSN 与 `backend/.setup/` 里那个只读账号，
     缺任何一项由 fixture 自己 `skip`（`pg` 标记的 skip 在 `tests/conftest.py`，账号缺失在
     `tests/integration/conftest.py`），闸里看到的仍是绿。代价是本机每次闸多花约一分半真进程时间，
     换来的是"进程边界"这类断言天天被跑，而不是等到收口才第一次红。
4. **源库（MySQL 5.7）**：本机 `ai_web_demo` 是**手测/集成**目标，不参与 CI 单测；
   单测里对抽取 SQL 的验证方式是对**生成的 SQL 文本做 snapshot 断言**
   （防 `IN (…)` 参数占位写错、防 `information_schema` 查询漏了 `table_schema` 条件）。
5. **CI 形态（无 Docker）**：`ruff check` + `ruff format --check` + `mypy app` +
   `pytest -q -m "not pg and not live"` + `npm run typecheck && npm run build`。
   pg 用例**只在有 DSN 的 job 跑**（本地 `make test-pg`）。不假装 CI 能验向量。

## 3. 端到端手测清单（15 步，全绿即 v0 可用）

| # | 动作 | 期望证据 |
|---|---|---|
| 1 | `uv run python scripts/check_env.py` | 无 fatal，vector/pg_trgm/hnsw/schema 全绿；维度一致项通过 |
| 2 | `uv run alembic upgrade head` | `aiweb` schema 下表齐全 + `alembic_version` 在 aiweb 内（不在 public） |
| 3 | `uv run python scripts/seed_admin.py` | 建 admin；**第二次运行幂等且不重置口令**；库里存的是 argon2 hash |
| 4 | 前端登录 → 首登强制改密 | 旧 token 立即失效（as-built 005：只有 `users.token_version`（令牌里的 `tv`），没有 `pwd_ver` 那一种） |
| 5 | 新建数据源 `demo-mysql` → 测试连接 | 返回"连接成功 / 9 表 1 视图 / 只读能力=OK / MySQL 5.7.x / max_execution_time 支持=yes"。要拿到 9/1 必须在**排除表**里填 `_%`（源原生 LIKE 里 `\_` 才是字面下划线）：库里可见的是 11 张，多出来的 `_aiweb_demo_marker`/`_numbers` 这类是脚本内部表，口径见 §1.2 的 as-built 注 |
| 6 | 立即同步 | SSE 进度到 100%；`sync_jobs.status=success`；phase 序列完整。**as-built(P3-016)**：本机从此**两条命令**——`dev.ps1 dev` 起 API，`dev.ps1 worker` 起执行体（`make worker` 同义）。只起一条时这一行的表现是"202 拿到了、那一行永远停在 `pending`、`started_at` 为 NULL"，那是缺消费者不是缺索引；SSE 那一半归 017，此步现在能看的只有库里那一行 |
| 7 | 元数据浏览 → 打开 `order_main` | 字段列表含中文注释；索引含复合索引两列顺序正确；关系显示指向 `customer`/`order_item`；卡片文本可复制 |
| 8 | 知识库检索预览（调参页）输入"各区域每月回款金额" | 返回 `payment_record`/`order_main`/`customer` 三表且带 `vector_rank`/`keyword_rank`/`rrf_score` 三列排名；把 `final_tables` 改成 2 后第三表消失。**as-built(0009)**：这一行是**向量启用后**的完整形态。009 交付的是接口层 `POST /api/kb/search`（没有调参页 UI，也没有 `vector_rank`/`keyword_rank`/`rrf_score` 三列——P2 只有关键词一路，融合排名无从谈起），P2 侧的可人肉验证证据是 `tests/integration/test_kb_search_live.py` 三条 live 用例（问订单总金额 → `order_main` 头名、`payment_record` 进前 5；问客户手机号 → `customer` 头名且理由三元组齐；二次同步后候选不漂移）。调参页 UI 与三列排名归引入向量那一张工单 |
| 9 | Chat 问"2025 年每个渠道的总回款金额，按金额降序取前 5" | SSE 顺序完整；`guard.verdict=pass`；SQL 引用的表 ⊆ 第 8 步命中集合。**as-built(P2-012)**：这一行是**端点与前端就位后**（P8）的形态——012 不开端点（口径见 architecture §4.1 ⑦ 的拍板块）。P2 侧等价证据是命令行示踪弹：`uv run python scripts/demo_ask.py "2025 年每个渠道的总回款金额，按金额降序取前 5"`，它打印 `retrieved_tables / sql_raw / guard_verdict / sql_final / rows / chart_spec / conclusion` 与各步耗时，其中"SQL 引用的表 ⊆ 命中集合"由守卫的 `allowed` 集在链路内强制（越界即 `SqlGuardError`），不是靠人眼比 |
| 10 | **校验 SQL**：把 SQL 复制到 `mysql> ` 手工执行 | 数字与界面结果**逐位一致**（防止 executor 侧类型/时区改写造成"界面好看但数据不对"）；金额字段是 decimal 字符串而不是 `0.30000000000000004` |
| 11 | 结果与图表 | 折线/柱由 `chart_advisor` 选对（时间→折线、类别≤12→柱）；表格横向滚动；结果 1000 行时显示"已截断，仅展示前 1000 行" |
| 12 | 结论卡片 + 反馈"采纳为示例" | `few_shot` 新增行；结论里的数字能在结果表格里找到出处（抽查一处，防模型编数） |
| 13 | 换一个措辞再问（"看下去年各支付通道收了多少钱"） | `retrieval` 帧含 `few_shot_hit=true`，生成 SQL 与第 9 步结构高度相似；`done.latency_ms` 下降 |
| 14 | （反向）member 账号问 `user_activity_log` 之外的未授权表 | 403/无相关数据，且日志无该表名泄露 |
| 15 | （健壮）同步进行中重启服务 → 重启后僵尸 job 被回收、可重新同步 | 日志 + `sync_jobs` 状态变化 |

第 10 步是整套手测里最有价值的一步：**界面数字必须能被源库原样复现**，否则前面全绿也没意义。

## 4. SSE 冒烟

SSE 是本项目最容易"本地正常、链路不通"的部分，所以把验证做成一条命令。

### 4.1 `make sse-smoke`

一段 `curl -N` + 计时，验证帧不被缓冲：

```bash
# 1) 同步进度通道（GET + EventSource 语义）
time curl -N -H "authorization: Bearer $T" \
  http://127.0.0.1:8000/api/sync/jobs/1/events

# 2) 问数通道（POST → fetch-stream，curl 用 -N 关缓冲）
time curl -N -H "authorization: Bearer $T" -H 'content-type: application/json' \
  -X POST http://127.0.0.1:8000/api/chat/ask \
  -d '{"question":"近30天每日订单数趋势","datasource_id":1}'
```

判据：

1. 帧**逐条打印**而不是最后一次性涌出（把 `-N` 去掉再跑一次，能感知缓冲差别）；
2. 事件顺序符合契约，`guard` 帧出现在 `executing` 之前，最后一行是 `event: done`；
3. 空闲期每 15s 见 `: ping` 注释帧；
4. 响应头含 `content-type: text/event-stream`、`cache-control: no-cache`、`x-accel-buffering: no`；
5. 计时：首帧时间应显著小于总耗时（首屏不等全量，靠 `result_meta` + 分批 `result_batch` / `result`）。

> **as-built(P3-017)**：上面 1 号那条命令（`GET /api/sync/jobs/1/events`）**这一片起才有对应端点**。
> 五条判据里属于同步通道的 ①④⑤ 由 `tests/integration/test_sync_events_live.py` 在真进程上代跑了一次
> （①逐帧推进与⑤首帧早于终局是同一条断言，④是 `content-type`/`cache-control`/`x-accel-buffering` 三个头）。
> **判据 ③（`: ping`）这条代跑不了**：live 跑的是有推进的作业，15s 一帧的注释行在那条流上根本不会出现，
> 要断它就得让一条流真空等 15s。心跳因此只钉在生成器层
> （`test_sync_events_sse_pg.py::test_没有新事件时每窗吐一帧_ping`，把窗口 monkeypatch 成 20ms），
> 真 HTTP 上的心跳仍未录
> （真 `curl` 现场仍未录，因为 `make sse-smoke` 这个目标**至今没落地**——`Makefile` 与 `scripts/dev.ps1`
> 里都没有它，roadmap §工具清单那一行仍然挂账，落地归 P8 前端联调那一档）。
> 判据 ②（`guard` 帧早于 `executing`）是**问数通道**的，同步通道没有这两帧，按本表 1 号命令的上下文读。
> 补一条本片的实测：`cache-control` 实际发的是 `no-cache, no-transform`（判据 4 只写了 `no-cache`），
> 而 `Connection: keep-alive` **没设**，理由见 architecture §6.2 的 as-built(P3-017)。

### 4.2 反缓冲三件套（少一个就表现为"卡住"）

| 位置 | 要求 |
|---|---|
| Vite dev | `server.compress: false`，并在 proxy 的 `proxyRes` 上对 `text/event-stream` 改写 `cache-control: no-cache, no-transform`、`x-accel-buffering: no`、去掉 `content-encoding` |
| 后端 | `media_type="text/event-stream"` + `Cache-Control: no-cache` + `X-Accel-Buffering: no`，且**不挂 GZipMiddleware**（对 `text/event-stream` 显式排除） |
| 心跳 | 每 15s 发 `: ping\n\n` 注释帧，否则远端链路 idle 超时会静默断流 |

浏览器侧判据：DevTools → Network → EventStream 标签能看到逐帧推进；
若"等 3s 后一次性出现"，则 `compress:false` 没生效。
前端**不用 `EventSource`**（无法带 `Authorization` 头），用 `fetch` + `ReadableStream` 手解帧。

### 4.3 断连与资源回收

- 客户端中途关页（`AbortController`）→ 后端日志出现客户端断开，**不留 running 执行**：
  `SELECT count(*) FROM pg_stat_activity` 稳定不涨（`asyncio.CancelledError` 正确传导）。
- `/chat/ask` 的生成协程包 `asyncio.timeout(ASK_TIMEOUT_S)`；
  客户端断连（`request.is_disconnected()`）→ 取消 LLM 流 + 中止源查询。
- MVP 断连语义：客户端断开 = 请求作废（不落 `chat_message`）；同步 job 不受影响，
  重连 SSE 从 `Last-Event-ID` = job 当前 `progress` 续。
- 相关前端超时必须大于后端：`VITE_REQUEST_TIMEOUT_MS=120000` 要 > LLM timeout + 执行超时，
  否则前端先断，现象和"后端挂了"一样。

## 5. P10 收口标准

`make check` = ruff + mypy + pytest 全绿；未设 `AIWEB_PG_TEST_DSN` 时 pg 用例**干净 skip 而不是 error**；
本文件 §3 的 15 步从 seed 到二次命中走一遍全通；
新人（或一周后的你）只读 README 能在 30 分钟内跑起服务。
