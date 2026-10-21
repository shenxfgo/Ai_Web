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

### 1.7 P3 同步链路真跑实录（工单 025 验收 4）

§1.6.1 那一份对账数字来自 live 用例，而用例按工单 016 的已定口径**不真起子进程**（`run_once`
在测试进程里代跑）。这一节补的就是那一条被让掉的路：**两条真进程 + 一次当场打断 + 一次自愈**，
它是 roadmap §P3 验收 1/2/3/4 与"?force 开关""真分批"这五处的独立真相源。

前置（缺一判定的方向就会反过来）：

- API 按文档那条命令起：`dev.ps1 dev`（127.0.0.1:8000）。**本机跑它的字面是
  `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 dev`**——这台机器只装了
  Windows PowerShell 5.1，PATH 上没有 `pwsh`（PowerShell 7 未安装），文档其余各处写 `pwsh` 的地方
  在本机都要换成 `powershell`（Makefile 自己用的就是 `powershell`，见 roadmap §6 那行的差额账）。
  这条命令的**前端那一半在本机是失败的**：`Start-Process -FilePath "npm"` 报 `%1 is not a valid Win32 application`
  （`npm` 是 `.cmd` 垫片，`Start-Process` 不吃），后端那一半照常起来并常驻（`api.log` 里两件事挨着：
  先是这条报错，紧接着 `Uvicorn running on http://127.0.0.1:8000`）。实录只需要 API，所以本轮没为它改脚本——
  **记成无主项**（工单 025「不做新功能代码」），归下一次碰 `dev.ps1` 的那一片。
  另：`api.log` 第 14 行有一次 `WatchFiles detected changes in 'app\services\sync_service.py'. Reloading...`，
  就是抄录期间存盘触发的自动重启，落在 job 19 那一轮的时刻上。它不影响本节四格证据：入队与终局都在 PG 里，
  worker 是脚本自己起的子进程、不经 uvicorn 的重启器；顺带把 §3 第 15 行说的那个"`--reload` 老形态"演了一次。
  **worker 不在这里起**——实录脚本自己按 `backend/.venv/Scripts/python.exe scripts/run_worker.py`
  起子进程并按 pid 杀（同一条命令、同一个入口文件，只是为了拿得到 pid）。
  理由是验收 4 那句"kill 的对象是 worker 进程，不是 uvicorn"。
- 凭证只在运行时读：应用账号 `backend/.setup/local_admin.txt`、源库口令 `backend/.setup/aiweb_ro.cnf`，
  两者都只进请求体，不打进日志也不回显（登录行只打 token 长度）。
- **全程零手工 SQL**：写侧只有 HTTP（`POST /api/datasources` / `POST /api/sync/jobs`），
  元数据库侧一律 `SELECT`，一条 `UPDATE`/`INSERT`/DDL 都不发。僵尸行被回收、`enum_values` 被自清，
  都是系统自己做的，不是脚本替它做的。
- 用的源是新登记的 `demo-mysql-025`（请求体里 `include_schemas=["ai_web_demo"]`、`exclude_tables=["\\_%"]`）
  → 抽取分母 **10**（9 张 `BASE TABLE` + 1 张 `VIEW`），与 §1.2 末注同口径。库里另一份 `demo-mysql`
  （id=1）的库行配置相同（`out_probe_f.txt`：两行的 `include_schemas` 都是 `['ai_web_demo']`、
  `exclude_tables` 都是 `['\\_%']`、`include_tables` 都是 `NULL`），只是它是 P2 时代就在的那一份。
- **范围三列不在数据源接口的投影里**，这一条本节自己绕过过一次坑，写下来免得下次再踩：
  `out_probe_f.txt` 同时打了 `GET /datasources` 与 `GET /datasources/{1,2}` 的响应原文，三个响应的键集合里
  **都没有** `include_schemas`/`include_tables`/`exclude_tables`（这三个字段只挂在 `DataSourceCreate`
  上（`schemas/datasource.py:29`/`:31`），读侧的 `DataSourceOut`（`:39` 起）里没有它们）。
  早先那份 `ds.txt` 里 ds=1 的 `include_schemas: None` 是探针自己 `row.get(k)` 对一个**写死的键清单**
  取不到时的默认值，不是库里的值——把它读成"这一列没配"就反了。所以"exclude 真的生效了"的证据
  不是任何回读行，而是 discover 帧的 `total=10` 与 `meta_table` 里那 10 行（`_aiweb_demo_marker` 不在其中）。

| # | 流程（`evidence/evidence025.py` 的第一个参数） | 期望证据 | 本轮 |
|---|---|---|---|
| 1 | `register` | 201 + `id`，随后 `GET /datasources` 里看得见这一行（范围三列**不在**响应里，见上面前置的第 4 条——所以 exclude 是否生效由第 4 行的分母背，不由回读行背） | `id=2`，`out_register.txt` |
| 2 | `legacy` | 跑之前 `meta_column.enum_values` 仍是 JSON 字面 `'null'::jsonb` 的行数 **> 0**，一次 MySQL 同步之后 **= 0** | 125 → 0 |
| 3 | `409` | 队列里有一行 pending、worker **还没起**，此时同库再发一次 → 409 `sync_already_running` | 挡住的是索引 |
| 4 | `timeline <job_id>` | 后起的 worker 领走那一行：`retry: 3000` → 五帧 `event: progress`（`id` 1..5）→ `event: done`；库里 `progress=100.00` | 全帧序见下 |
| 5 | `force <ds_id>` | worker 侧 `AIWEB_EXTRACT__MAX_TABLES=1`：不带 force 那次 `failed` + `errors[0].data.remedies` 三条原文；`force=true` 那次 `success` | job 19 / job 20 |
| 6 | `killbatch <ds_id>` | 压 `BATCH_SIZE=3` + `HEARTBEAT_INTERVAL_S=3` + `STALE_JOB_RECLAIM_S=10`（两个 worker 这三个键相同），worker1 另加 `BATCH_INTERVAL_MS=5000` 好让它来得及在批间断气，跑到一半 `kill()` 掉它 → 僵尸在场时再发起仍 409 → 12s 后起 worker2（`BATCH_INTERVAL_MS=200`，为了这一轮别拖太久）→ 日志出现"回收 1 个僵尸 job" → 重新同步 `success` 且**批数是 4、形状 `[3,3,3,1]`** | job 21 → 23 |

#### 1.7.1 实跑输出的枚举清单（逐字，四段）

下面四段是从 `.scratch/p3-production-sync/evidence/` 的那几份原始文件**整段照抄**的：不转述、不加省略号、
不合行、不把 `payload` 之类"看着是空的"容器删掉。原始文件是 CRLF 行尾，摘录只去掉了行尾的 `\r`，
其余逐字符相同（要复核就把本节代码块跟那几个文件 `diff`）。工单 025 的口径那句"每条枚举清单与实跑输出
逐字一致"落到这里就是这一条纪律——上一版本节把五帧压成五行可读摘要，读起来舒服，但**它不再是证据**：
压缩掉的 `payload` 与 `event: progress` 恰好是 §2.1 那条帧形状用例钉的东西。

> **订正边界(as-built(P4-029))：这四段是 P3 那一次真跑的快照，逐字保留，不跟着今天的代码改写。**
> 其中 `embed` 那一帧有两处已经从当时长成别的样子：① 未配置那支的 `detail` 现在点名**四格**
> （`EmbeddingGroup.configured` 判的是 `BASE_URL`/`API_KEY`/`MODEL`/`DIMENSION>0`，当时那句只点三格）
> 并把出路（配真端点 / 起闸内替身）写在同一句里；② 配了端点时这一档不再是单帧 `skipped`，
> 而是**逐表一帧** `{table, embedded}`。新的形态证据在 §1.8 与 `tests/integration/test_kb_reindex_live.py`，
> 不在本节——本节的价值是"P3 收口那天事件流长什么样"，改掉它就把这份对账依据毁了。

`legacy`（验收 2 的自清那半，跑在 `demo-mysql` id=1 上）← `out_legacy.txt`：

```
[t+   0.26s] 登录 ok（token 长度 171，不打印内容）
[t+   0.33s] 跑之前 meta_column.enum_values 是 JSON 字面 null 的行数 = 125
[t+   0.38s] POST /api/sync/jobs datasource_id=1 force=False → 202 {"job_id":16}
[t+   1.75s] job 16 终局 status=success counters={'cards': 12, 'tables': 10, 'batches': 1, 'columns': 133, 'indexes': 27, 'databases': 1, 'tables_stale': 0, 'tables_failed': 0, 'relations_inferred': 1, 'relations_extracted': 8}
[t+   1.75s] 跑之后 JSON 字面 null 的行数 = 0
```

125 → 0 就是 024 那一修的**真效果**：`_COLUMN_SYNC_COLS` 是整列覆盖，所以一次正常同步把历史脏数据自己洗掉了，
脚本一条 `UPDATE` 都没发（对照 `out_list.txt` 里跑前那一条 `= 125`，那是同一张 `meta_column` 的同一条件）。

`409`（验收 3 的互斥那一半，跑在 ds=2 上）← `out_409.txt`：

```
[t+   0.27s] 登录 ok（token 长度 171，不打印内容）
[t+   0.28s] POST /api/sync/jobs datasource_id=2 force=False → 202 {"job_id":17}
[t+   0.36s] 入队后那一行（还没有 worker 在跑）：status=pending started_at=None progress=0.00
[t+   0.36s] POST /api/sync/jobs datasource_id=2 force=False → 409 {"error":{"code":"sync_already_running","message":"该数据源已有一个未结束的同步任务","detail":null}}
[t+   0.36s] 同库再发一次 → 409 {"error":{"code":"sync_already_running","message":"该数据源已有一个未结束的同步任务","detail":null}}
[t+   0.36s] 此刻那一行仍是 ('pending', 'connect', Decimal('0.00'), None, None, None, {}, [], False)
[t+   0.36s] （job_id=17 留给下一个流程消费：SSE 用 --job 17）
PENDING_JOB=17
```

`started_at=None` 与 `status='pending'` 是同一条事实的两面（入队那条 `INSERT` 里不许出现 `running`，
也不许顺手点上那两个钟，见 §2.1 的 `job_queue` 行）。连打的两行 `→ 409` 是**同一次请求的两条打印**
（`_post()` 先报原始响应，流程再转述一次），不是发了两次；挡人的是 `ux_sync_running` 这个部分唯一索引，
不是"有人在跑"——此刻库里一个 worker 都没有。
最后那行元组是 `_row()` 直接 print 的 `sqlalchemy.Row`，`select(...)` 的列顺序是
`(status, phase, progress, started_at, finished_at, heartbeat_at, counters, errors, force)`——
那一格里三个 `None` 连排，正是"入队后谁都没跑过"的形状（两个钟都没点上、`counters`/`errors` 是空容器）。
同理 `事件=[(1, 'extract', 'discover'), …]` 的三元组顺序是 `(seq, stage, phase)`。

`timeline`（验收 1/2/3 的正跑那一半；job 17 就是 409 那一步留下的 pending，被这里后起的 worker 领走）
← `out_timeline.txt`（`event: done` 那一帧的原始字节连 HTTP 头一起另存在同目录 `sse_17.txt`，
里面有 `x-request-id: ec8576d7a5ca466e` 与 `content-type: text/event-stream; charset=utf-8`）：

```
[t+   0.25s] 登录 ok（token 长度 171，不打印内容）
[t+   1.28s] worker 启动行：[worker] 开始消费同步队列（空转每 2s 扫一次，心跳每 10s 一次、回收阈值 180s、事件保留 30 天，Ctrl+C 退出）
[t+   1.48s] retry: 3000
[t+   1.48s] 
[t+   1.51s] id: 1
[t+   1.51s] event: progress
[t+   1.51s] data: {"stage": "extract", "phase": "discover", "done": 0, "view": 1, "cards": 0, "total": 10, "base_table": 9, "payload": {}}
[t+   1.51s] 
[t+   1.51s] id: 2
[t+   1.51s] event: progress
[t+   1.51s] data: {"stage": "upsert", "phase": "tables", "done": 10, "view": 1, "cards": 0, "total": 10, "base_table": 9, "payload": {"schema": "ai_web_demo", "batches": [["category", "customer", "order_item", "order_main", "payment_record", "product", "product_stats_wide", "refund_record", "user_activity_log", "v_daily_sales"]]}}
[t+   1.51s] 
[t+   1.51s] id: 3
[t+   1.51s] event: progress
[t+   1.51s] data: {"stage": "card_build", "phase": "card_build", "done": 10, "view": 1, "cards": 0, "total": 10, "base_table": 9, "payload": {}}
[t+   1.51s] 
[t+   1.51s] id: 4
[t+   1.51s] event: progress
[t+   1.51s] data: {"stage": "embed", "phase": "embed", "done": 10, "view": 1, "cards": 12, "total": 10, "base_table": 9, "payload": {"code": "embedding_not_configured", "detail": "embedding 三键（base_url / api_key / model）为空，向量化归 P4", "skipped": true}}
[t+   1.51s] 
[t+   1.51s] id: 5
[t+   1.51s] event: progress
[t+   1.51s] data: {"stage": "done", "phase": "done", "done": 10, "view": 1, "cards": 12, "total": 10, "base_table": 9, "payload": {}}
[t+   1.51s] 
[t+   1.51s] event: done
[t+   1.51s] data: {"job_id": 17, "status": "success", "counters": {"cards": 12, "tables": 10, "batches": 1, "columns": 133, "indexes": 27, "databases": 1, "tables_stale": 0, "tables_failed": 0, "relations_inferred": 1, "relations_extracted": 8}, "warnings": [], "errors": [], "duration_ms": 276}
[t+   1.51s] 
[t+   1.80s] job 17 终局（读库）：status=success phase=done progress=100.00
[t+   1.80s]                 counters={'cards': 12, 'tables': 10, 'batches': 1, 'columns': 133, 'indexes': 27, 'databases': 1, 'tables_stale': 0, 'tables_failed': 0, 'relations_inferred': 1, 'relations_extracted': 8}
[t+   1.80s]                 事件=[(1, 'extract', 'discover'), (2, 'upsert', 'tables'), (3, 'card_build', 'card_build'), (4, 'embed', 'embed'), (5, 'done', 'done')]
[t+   1.80s] worker stdout：
[worker] 开始消费同步队列（空转每 2s 扫一次，心跳每 10s 一次、回收阈值 180s、事件保留 30 天，Ctrl+C 退出）
[job 17] status=success duration=234ms
          counters={'databases': 1, 'tables': 10, 'columns': 133, 'indexes': 27, 'relations_extracted': 8, 'relations_inferred': 1, 'tables_stale': 0, 'tables_failed': 0, 'cards': 12, 'batches': 1}
```

这一段是"五帧对应库里五行 `seq`"的**唯一一份同时看得见两边**的记录：帧侧的 `id: 1..5` 与
读库侧 `事件=[(1, 'extract', 'discover'), …, (5, 'done', 'done')]` 逐位相等，且
`event: done` 收尾那一帧**没有 `id`**（游标只属于进度帧）。默认批大小 200 时 `batches` 是 1，
名单十张全在同一个子数组里——这正是 §1.7.1 末段那个 `[3,3,3,1]` 的对照组。

`force`（`force` 开关那半，worker 侧 `AIWEB_EXTRACT__MAX_TABLES=1`）← `out_force.txt`：

```
[t+   0.26s] 登录 ok（token 长度 171，不打印内容）
[t+   1.31s] POST /api/sync/jobs datasource_id=2 force=False → 202 {"job_id":19}
[t+   1.31s] worker 侧 AIWEB_EXTRACT__MAX_TABLES=1：不带 force 那次 → 202
[t+   3.55s]   → job 19 status=failed force=False errors=[{'code': 'extract_scope_too_large', 'data': {'remedies': ['配 include_tables 白名单', '只同步部分 schema（include_schemas）', 'admin 用 ?force=true 覆盖上限'], 'max_tables': 1, 'table_count': 10}, 'detail': '抽取范围里有 10 张表，超过上限 1'}]
[t+   3.56s] POST /api/sync/jobs datasource_id=2 force=True → 202 {"job_id":20}
[t+   5.73s] force=true 那次 → job 20 status=success force=True progress=100.00 counters={'cards': 12, 'tables': 10, 'batches': 1, 'columns': 133, 'indexes': 27, 'databases': 1, 'tables_stale': 0, 'tables_failed': 0, 'relations_inferred': 1, 'relations_extracted': 8}
[t+   5.73s] worker stdout：
[worker] 开始消费同步队列（空转每 2s 扫一次，心跳每 10s 一次、回收阈值 180s、事件保留 30 天，Ctrl+C 退出）
[job 19] 异常（终局已由 run_sync 写好）：ExtractScopeTooLarge: 抽取范围里有 10 张表，超过上限 1
[job 20] status=success duration=187ms
          counters={'databases': 1, 'tables': 10, 'columns': 133, 'indexes': 27, 'relations_extracted': 8, 'relations_inferred': 1, 'tables_stale': 0, 'tables_failed': 0, 'cards': 12, 'batches': 1}
```

三条出路里第三条的字面是 `admin 用 ?force=true 覆盖上限`（`SCOPE_REMEDIES` 原文，021 换回来的那一版），
而**实装的形状是请求体** `{"force":true}`（`architecture.md` §7 那行的 as-built 记着为什么不二开
查询参数入口）。措辞到本节为止统一：文档提这一档时写"`force` 开关（HTTP 请求体字段）"，
`?force=true` 只作为 `remedies` 里的**被钉字面量**出现，不再当作调用写法。

`killbatch`（验收 4 + "一批 200 表"那条的真分批）← `out_killbatch.txt`：

```
[t+   0.27s] 登录 ok（token 长度 171，不打印内容）
[t+   0.28s] POST /api/sync/jobs datasource_id=2 force=False → 202 {"job_id":21}
[t+   1.41s] 打断时：status=running phase=connect heartbeat=2026-10-01 13:11:03.688542+00:00 progress=0.00
[t+   1.41s] SIGKILL worker1 pid=21092
[t+   1.42s] POST /api/sync/jobs datasource_id=2 force=False → 409 {"error":{"code":"sync_already_running","message":"该数据源已有一个未结束的同步任务","detail":null}}
[t+   1.42s] 僵尸在场时再发起 → 409 {"error":{"code":"sync_already_running","message":"该数据源已有一个未结束的同步任务","detail":null}}
[t+  14.49s] worker2 启动行：[worker] 开始消费同步队列（空转每 2s 扫一次，心跳每 3s 一次、回收阈值 10s、事件保留 30 天，Ctrl+C 退出）
[t+  15.49s] worker2 的回收日志：['[worker] 回收 1 个僵尸 job：[21]']
[t+  15.50s] 回收后那一行：status=failed finished_at=2026-10-01 13:11:16.862941+00:00 errors=[{'code': 'reclaimed', 'detail': 'heartbeat 超时'}]
[t+  15.50s] 回收补的事件：[(1, 'done', 'done')]
[t+  15.52s] POST /api/sync/jobs datasource_id=2 force=False → 202 {"job_id":23}
[t+  15.69s] retry: 3000
[t+  16.58s] id: 1
[t+  16.58s] event: progress
[t+  16.58s] data: {"stage": "extract", "phase": "discover", "done": 0, "view": 1, "cards": 0, "total": 10, "base_table": 9, "payload": {}}
[t+  17.27s] id: 2
[t+  17.27s] event: progress
[t+  17.27s] data: {"stage": "upsert", "phase": "tables", "done": 10, "view": 1, "cards": 0, "total": 10, "base_table": 9, "payload": {"schema": "ai_web_demo", "batches": [["category", "customer", "order_item"], ["order_main", "payment_record", "product"], ["product_stats_wide", "refund_record", "user_activity_log"], ["v_daily_sales"]]}}
[t+  17.27s] id: 3
[t+  17.27s] event: progress
[t+  17.27s] data: {"stage": "card_build", "phase": "card_build", "done": 10, "view": 1, "cards": 0, "total": 10, "base_table": 9, "payload": {}}
[t+  17.33s] id: 4
[t+  17.33s] event: progress
[t+  17.33s] data: {"stage": "embed", "phase": "embed", "done": 10, "view": 1, "cards": 12, "total": 10, "base_table": 9, "payload": {"code": "embedding_not_configured", "detail": "embedding 三键（base_url / api_key / model）为空，向量化归 P4", "skipped": true}}
[t+  17.33s] id: 5
[t+  17.33s] event: progress
[t+  17.33s] data: {"stage": "done", "phase": "done", "done": 10, "view": 1, "cards": 12, "total": 10, "base_table": 9, "payload": {}}
[t+  17.33s] event: done
[t+  17.33s] data: {"job_id": 23, "status": "success", "counters": {"cards": 12, "tables": 10, "batches": 4, "columns": 133, "indexes": 27, "databases": 1, "tables_stale": 0, "tables_failed": 0, "relations_inferred": 1, "relations_extracted": 8}, "warnings": [], "errors": [], "duration_ms": 886}
[t+  17.56s] 重新同步 job 23：status=success progress=100.00 counters={'cards': 12, 'tables': 10, 'batches': 4, 'columns': 133, 'indexes': 27, 'databases': 1, 'tables_stale': 0, 'tables_failed': 0, 'relations_inferred': 1, 'relations_extracted': 8}
[t+  17.58s] tables 帧的 payload.batches = {'schema': 'ai_web_demo', 'batches': [['category', 'customer', 'order_item'], ['order_main', 'payment_record', 'product'], ['product_stats_wide', 'refund_record', 'user_activity_log'], ['v_daily_sales']]}
[t+  17.58s] 批数=4 每批大小=[3, 3, 3, 1]
[t+  17.58s] worker2 stdout：
[worker] 开始消费同步队列（空转每 2s 扫一次，心跳每 3s 一次、回收阈值 10s、事件保留 30 天，Ctrl+C 退出）
[worker] 回收 1 个僵尸 job：[21]
[job 23] status=success duration=843ms
          counters={'databases': 1, 'tables': 10, 'columns': 133, 'indexes': 27, 'relations_extracted': 8, 'relations_inferred': 1, 'tables_stale': 0, 'tables_failed': 0, 'cards': 12, 'batches': 4}
```

**这一份与 `out_timeline.txt` 有一处形状差别，差别是记录方式不是系统行为**：`_stream()` 是按行 echo 的，
空行也照打（timeline 那份里每帧后面都有一行 `[t+ …] ` 加一个空格），而 killbatch 这一轮我在抄录时把
那些空白行省了。原始文件 `out_killbatch.txt` 里它们**在**（第 13、17、21、25、29、33、36 行），
所以本节这一段严格说是"逐字但跳空白行"。要连空白行一起对账就读原文件；留下的理由是一屏放不下两屏空行。

这一段一次答出四件事：**僵尸判的是心跳不是排队时长**（13:11:03 那一记 `heartbeat_at` 是它，
`STALE_JOB_RECLAIM_S=10` 到点就改判，`errors[0].code` 是 `reclaimed`）、**回收住在心跳循环里而不是启动钩子里**
（worker2 的启动行落在 `t+14.49s`，回收那一行落在 `t+15.49s`——整整晚一轮；"不必等重启也能改判"那一格
本节证不了，它由 `test_worker_heartbeat_pg.py` 的"一轮循环改判 + 补终局事件"那条背着）、
**没消费掉的 `ux_sync_running` 仍挡新作业**（打断后 1ms 内再发就是 409）、
**默认 200 走不到的分批路径真的走到了**（`batches=4` + `[3,3,3,1]`）。

**这一节的证据边界**：证到的是"真 HTTP → 真队列 → **真子进程 worker** → 真 MySQL → 真元数据库"。
进程边界本身不是本节才第一次跨——工单 017 的 `tests/integration/test_sync_events_live.py`
就同时子进程 worker + 子进程 API + 真流式读（那里讲的理由是 `ASGITransport` 攒完 body 才返回，
"首帧在作业结束之前到达"在那条传输上没有对应事实）。本节的增量是那一条用例**没有**的四格：
**当场 `kill()` 之后系统自己 recover**（017 的用例不杀进程）、**批大小压到 3 的真跑**
（默认 200 时那条路径永远走不到）、**`force` 的两面**（超限拒绝 + 覆盖成功各一次）、
**历史脏数据被一次正常同步自清**（`enum_values` 125→0），而且跑的是开发者手头那两条命令的形状
（`dev.ps1 dev` 起的常驻 API，不是测试临时起的端口）。没证到的是**界面**——P3 前端只有"系统状态"一页
（`frontend/src/layouts/nav.ts` 的"数据源""表结构"两个入口至今 `routeName` 为空、标 P3 交付但未接），
所以 §1.6 第 4~8 步那种"打开页面看一眼"在本节一律是"库里的行 + HTTP 响应"证到的，
`GET /sync/jobs`（列表）与 `/sync/jobs/{id}`（详情）两个端点也没有实现（roadmap §P3 明确不做，挂账见
metadata-model §6 末 as-built(P3 收口)）。批大小压到 3 时抽取分母是 **10** 不是 `SHOW FULL TABLES`
的 11 行（`exclude_tables=["\\_%"]` 把建库脚本自己的 `_aiweb_demo_marker` 排掉了），
所以刀口是 `[3,3,3,1]` 而不是 `[3,3,3,2]`——两个数的关系同 §1.2 末注。

#### 1.7.2 闸（工单 025 验收 6）

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 check` 整轮全绿。原始文件
`.scratch/p3-production-sync/evidence/gate025.txt`（`.scratch/` 是 gitignored 的，所以结论数字抄在这里而不是只给路径）。
下面抄的是**每步的命令回显行 + 结论行**，中间省略的是 pytest 那十几行 `[ 7%]` 进度点与 npm 的两行脚本回显——
省略的都在原文件里，要连进度点一起对账就读原件：

```
+ uv run ruff check app scripts tests alembic  (backend)
All checks passed!
+ npm run typecheck  (frontend)
+ uv run python -m pytest  (backend)
980 passed, 2 skipped, 1 warning in 171.30s (0:02:51)
```

`dev.ps1 check` 只有 `lint` + `typecheck` + `test` 三步，**格式与类型两步不在它里面**——`ruff format --check`
从来不是目标（`fmt` 是**写**的 `ruff format`，不是 `--check`），`mypy` 也不是：roadmap §6 的 `lint` 那一行写着
`ruff check . && mypy app`，而 Makefile 与 `dev.ps1` 两边的 `lint` 都只到 `ruff check` 为止，
**今天没有任何闸目标背 `mypy`**（§2 的"CI 形态"那行把它列成 CI 的一步，属 P10 落地）。本轮手工补的是下面这两条，
同样逐字，命令回显行连后面的方括号一起抄自原文件：

```
+ uv run ruff format --check app scripts tests alembic  (backend)  [dev.ps1 无此目标，025 手工补]
166 files already formatted
+ python -m mypy app  (backend)  [dev.ps1 无此目标，025 手工补]
Success: no issues found in 54 source files
```

对照基线：工单 014 交付时是 **677 passed / 2 skipped**，本轮 **980 passed / 2 skipped**（收集 982 = 980 + 2），
高出的 303 条是 P3 那十片加进来的。**两个 skip 没有变多**，仍在 `tests/guard/test_corpus_mutation.py:61`
——那是守卫变异语料（nl2sql-safety §8.3 那五种变异）的两个空靶，来历见该文 §7 末注"闸从 629/3 变成 631/2"
那一句，与 DB 介质无关。"闸含 live"这一条本轮也复核过：`-m "pg or live"`
选得出 **185 条**且全部真跑，也就是 roadmap §6 差额账里那句"换一台没配 DSN 的机器就是 185 条 skip"的
反面——本机这轮属于"配齐"那一支。

### 1.8 P4 向量路真跑手测（工单 029 起头，034 逐条证据化）

§1.7 那一份实录证的是**关键词那半边**的进程分离。P4 这一节要证的多了两件：向量真的落进
`kb_card` 的那一根列，以及一套新基准诞生时**线上那套一个字都不动**。本节现在只是起头——
029 交付的是"闸内能证的全部证了"，下面那几格真端点才有的东西是留白，不是没做完。

**已在闸内有证据（每一行在它自己那片交付时逐条复跑过，不需要手工重做；行末那格写了片号）**：

| 格 | 闸内靶 | 它凭什么算证据 |
|---|---|---|
| reindex 的 202 形状、draft 出生、撞名复用、撞 active 409、embedding 没配齐（四格）400 | `tests/integration/test_kb_reindex_pg.py` | 走真 HTTP + 真入队 + `run_once` 代跑，档案行是查库读回来的 |
| 配了端点的普通同步真的灌向量（决策 15 的正半边） | `tests/integration/test_kb_reindex_live.py::test_普通同步配了端点时向量真落且embed帧是真计数` | 起 028 的替身真发 HTTP；除"非空"外还按"卡自己的正文哈希出来的那个点"逐卡比对 |
| 换维度 = 同表第二列，旧列逐字节不动 | 同文件 `test_reindex到另一维时新列真写旧列一字不动且检索结果相同` | 比的是该列的**文本形态**逐行相等，不是"行数没变"；同时比 `/kb/search` 的 `items` 前后全等 |
| 单表失败 → `partial` 且其余表向量真在场 | 同文件 `test_单表embed失败时落partial其余表的向量真在场` | 失败注入在**替身侧**（`--fail-on`），不是给编排加 test-only 形参；查库断"炸那张 NULL、另外两张非空" |
| 维度不符 → 整颗 `failed` 且不盖 `built_at` | 同文件 `test_端点回的宽度与档案不符时整颗作业failed且不盖built_at` | 替身按 model 名回 999 维，失败信息里两个数同时在场 |
| 请求体问的是**档案那一行的 model**，不是配置当前那个 | 同文件 `test_请求体问的是档案那一行的模型名而不是配置当前那个` | 入队后把 `MODEL` 换成替身不认的名字、`DIMENSION` 留在同宽：宽度那一路分不出两侧，只有名字能。读 settings 的错法在替身侧是 400 → 终局 `partial`（本轮变异实测过：把客户端换回 `get_settings().embedding` 只有这一条红） |
| 换基准的**三态流转**：切换、回滚、幂等、四种前置各回各的码 | `tests/integration/test_kb_activate_pg.py`（16 条）+ `test_kb_activate_endpoint_pg.py`（8 条） | 前者打 service 那条缝，逐条断的是**库里的 active 计数**而不是返回码；后者走真 HTTP 真鉴权，钉 admin-only 那一档与错误 envelope 里点名的源名。切换那条做过变异：让它在切换后把新档案的向量清空 → 只有"两根向量列内容未变"那一条红 |
| **向量路与 RRF 的装配**：三条 `mode`、融合次序、`ef_search` 下发、未预置维度显式报错 | `tests/integration/test_kb_search_hybrid_pg.py`（14 条）+ `tests/unit/test_retriever_rrf.py`（12 条）+ `tests/unit/test_retriever_vector_sql.py`（7 个函数 / 8 条收集，其中一条是参数化） | 向量名次是**用例自己摆出来的**（`q / one-hot / -q`，余弦距离严格 `0<中<2`），融合次序是手算的 `1/62+1/61` 那一串，都不从实现反推；`set_config` 那条与召回同事务由"四次 `SHOW` 夹一次 `search()`、全在同一条连接"钉。**激活换到哪一维就读哪一列**（030 欠的真口径）在这里补回：两列的次序反着摆，读错列必红 |
| 实现切换缝：`pipeline` 与端点都不认识实现的名字 | `tests/unit/test_retriever_seam.py` | 机械检查 `Hybrid`/`ef_search`/维度字面不出现在调用方，且测试里换一个实现后 pipeline 用例不改而仍绿（工单 031 验收 5） |
| 收缩迁移 0009：`is_active` 从库里消失，且回滚能按 `status` 把内容恢复回来 | `tests/integration/test_kb_is_active_shrink_pg.py` | 在自建的临时 schema 里真跑 `alembic` 的 `0007→0008→head→downgrade 0008→head`，读的是 `information_schema` 的列名而不是 ORM 属性。**跑的是迁移本体**，不是把那句 SQL 重抄一遍 |
| **对外那一面**（032）：`/kb/search` 的三格（两路名次 + 融合分）与顶层三格、权限那两格分开、留痕写名次 | `tests/integration/test_kb_search_api_pg.py`（18 条）+ `test_kb_search_hybrid_pg.py`（14 条）+ `test_pipeline_orchestration.py`（新增 3 条） | 摘刀那条（工单 032 验收 2）是**三幕**：基线 → 把链路上唯一那道应用层权限门换成"一被调用就抬错"→ 把喂给 SQL 的 id 清单放宽到全部源。第三幕让那张越权卡当场出头名，前两幕的"结果不变"才不是"根本没人在筛"；它**只有在向量 SQL 那一格写对时**才绿。名次由用例摆出来（越权卡距离摆 0 而不是"差不多近"），`rrf_score` 不 round 而 `score_kw` round（头两名常差 1e-5，抹平就与融合层的次序不是同一个）。`used_profile.dimensions` 与 030 那条回滚用例连着跑（激活 1024 → 报 1024，回滚 → 报 1536）。403 那条把 `mode:'vector'` 也点一次名，钉的是"门在取实现之前"——挪到后面就会先被 503 顶掉。`retrieved` 三种 `mode` 各一条：把 `fused` 改成直接跟着 `vector_rank` 走时 hybrid 与 vector 两支同红， 而"非空就给位次"那种写法只有 vector 红——后一族里前两条钉不住的那一档由 `mode='vector'` 那条守着 |

**留白（必须真端点，闸内造不出语义）**：

- roadmap §P4 验收 2 那句"『销售额』能命中 `payment_record` 与 `order_main`"。替身回的是
  字 n-gram 哈希，它保证的是**形状与确定性**，"近邻=语义相近"对它不成立（§2.3 第一条）。
- 真端点的**计费与限速形状**（真 429/5xx 之后退避多久、一轮全量灌完花多少）。闸内那条
  重试证据（§2.3 末）只证"5xx 会重试、用尽后按表落 `partial`"，不证真端点的额度。

**手测怎么跑（工单 034 已按这张表逐条填"本轮"列；"本轮"= 2026-10-03 那一段真跑，替身 = 028 的 `fake_embedding_server`，逐字输出在 §1.8.1）**：

| # | 动作 | 期望证据 | 本轮 |
|---|---|---|---|
| 1 | 把 `backend/.env` 那四格指向**真端点**（键名同 §2.3 那块命令，值换成真的），跑 `scripts/check_env.py` | `embedding.dimension ok` 且维度=配置值；探针与客户端连的是同一个 `{base_url}/embeddings` | **未跑**。本轮三键指向替身（`http://127.0.0.1:8123/v1`），两个模型名各跑一轮（`text-embedding-3-small`→1536、`bge-large-zh-v1.5`→1024）。真端点下"探针与客户端同源"这一格今天还没验过 |
| 2 | `POST /api/kb/reindex {datasource_id}` → 记下 `job_id`/`profile_id`，按 §1.7 那条命令起 worker | 202 两键齐；库里多一行 `status='draft'`、`built_at IS NULL`；active 那一行逐格不变 | **替身下跑了**（§1.8.1 ①）：库里多出的那一行是 `(2, 'text-embedding-3-small@1536@tplv1', 'text-embedding-3-small', 1536, 1, 'draft', …)`，active 那行 `(1, 'no-embedding@1536@tplv1', '', 1536, 1, 'active', None, …)` 逐格与开工前探针（`out_probe.txt`）相同。**两处口径要分清**：`built_at IS NULL` 是**入队那一刻**的形状，跑完的 draft 就带 `built_at` 了——故事 6 那句"还没跑完不许激活"靠的正是这一格；202 那两键由 `test_kb_reindex_pg.py::test_reindex_回_202_两个键齐_库里多出一行_draft_且_active_整行不动` 钉，本轮那一发的响应体只在终端，落盘的实物是终局帧的 `payload.profile_id: 2` 加 `sync_jobs.reindex_profile_id=2` |
| 3 | SSE 追这颗作业：`GET /api/sync/jobs/{job_id}/events?cursor=0` | `embed` 段**一表一帧**，`payload` 是 `{table, embedded}` 且 `embedded` 逐表上涨、全程没有 `skipped` 键 | **跑了**（§1.8.1 ①，job 26 那 14 帧逐字落盘）：`embedded` 1→2→3→4→5→6→**9**→10→11→12（`product_stats_wide` 一张表 3 张卡，所以那一跳跨三格），`skipped` 一个都没出现 |
| 4 | 跑完查库：`SELECT count(*) FILTER (WHERE embedding IS NOT NULL), count(*) FROM kb_card WHERE index_profile_id=:draft` | 非空数 = 该档案下的卡片数（真端点不丢发的话） | **替身下相等**：`(24, 24, 0, 24)`（卡片数 / `embedding` 非空 / `embedding_1024` 非空 / `embedded_at` 非空，§1.8.1 ②）。两个"24"要交代来源：它是**两颗** reindex 各 12 张叠出来的（job 26 ds=1、job 27 ds=2 共用同一行 draft——这正是工单口径条 2 那句"撞名复用"的实物）。**它不背"真端点不丢发"**：丢发那一格由 §1.8.1 ⑥ 反向证（12 张里只有 8 张有向量，另外 4 张的表名在 `errors[]` 里逐张点名） |
| 5 | `POST /api/kb/profiles/{id}/activate` → 再问一次 §3 那句问句 | 200 回 `{activated, retired}` 两格；库里 `count(*) WHERE status='active'` 恒为 1；旧向量列仍在（可回滚） | **跑了**（§1.8.1 ③）：档案 2 那一发 200、`{activated, retired}` 两格齐且各自带三元组；`active` 计数在五个快照里都是 `(1,)`。"旧向量仍在"不按"行数没变"比，按**逐行内容 md5** 比：三串（`be943793…` 全 NULL 那一族 / `b03bc7d5…` 档案 2 的 1536 列 / `fbfa9546…` 档案 3 的 1024 列）跨激活、跨切到 1024、跨回滚一字不变。问句那一格见第 7 行 |
| 6 | 换一个 `member` 身份再发同一条请求 → 然后把基准切**回**旧那套 | 前者 403 `forbidden` 且库里三态一个字没动；后者 200、`activated` 是旧那套、库里仍只有一个 active，且两根向量列的内容与切换前逐字节相同 | 前半**未跑**：本轮只有 admin 一个身份（`login-probe` 那一发只验了未带 token → 401），403 那一档归 `test_kb_activate_endpoint_pg.py`（`global` 档那条）。后半**跑了**：`retired`→`active` 200（§1.8.1 ④），md5 两份快照逐字符相等。**顺带一条实测**：`built_at IS NULL` 那条前置只管 draft，`retired` 档案不论 `built_at` 都能再激活——所以本轮靠 API 就把基准恢复回开工那套（终局 1 active + 2 retired），零手工 SQL |
| 7 | `POST /api/kb/search {"query":"销售额","mode":"vector"}`（真端点在位） | 200 且 `items` 非空、次序是余弦距离的次序；`took_ms` 有量（一发 HTTP + 一条索引查询）。**这一格是 roadmap §P4 验收 2 的语义那一格**，替身证不了（下面"留白"第一条） | **语义那一格留空**（写法照 §1.8.1 ⑤ 那句两句话）。真库 + 替身基准下"销售额"三种 `mode` 各发一次，`items` 三次**全空**；同一句换"订单"在激活替身基准后回 5 项、`vector_rank` 1..5 齐、`took_ms=308`。**形状跑通了，语义没跑通**，两件事不互替 |
| 8 | 同一句问句在**真语料**（几千张卡）上分别给 `ef_search=64` 与 `400`，比两次向量名次集合 | 64 的集合 ⊆ 400 的集合，且 400 那一发**真的多召回**几张 | 前半**跑了，但只能给到"相等"**：24 张卡的真库上两档都回 `[1,2,3,4,5]`（相等即 ⊆ 成立）。后半**未跑**——真语料规模不到。**"参数没传下去"这一族失效换了一格排除**：同一句问句加 `top_vector=3`，向量名次集合变 `[1,2,3]`（召回宽度跟着请求体变）。`SET LOCAL hnsw.ef_search` 与召回同事务那一格仍只有闸内那条四次 `SHOW` 夹一次 `search()` 的用例背——原因还是原行末注那一条：小表上规划器选 scan + top-N，`ef_search` 根本不参与（architecture §5.2 as-built(P4-031) 实测第 2、3 条） |

### 1.8.1 P4 真跑实录（工单 034 验收 1/3/5 的那一段真跑，语义那一格留空见 ⑤）

**这一段证的是什么**：档案的**出生、切换、退场、回滚**四步在真进程、真元数据库、真 HTTP 上走通，
以及 `embed` 那一档第一次不是 `skipped`。它**不证**中文语义命中——那是 ⑤ 那一格，也是本节唯一
一处"只有用例、没有真跑"。

**运行形态**（与前一份 P3 实录同一套，只多一个替身）：

- 替身：`backend/scripts/fake_embedding_server.py --port 8123`，就绪行逐字为 `LISTENING 127.0.0.1 8123`；
  两轮分别把 `AIWEB_EMBEDDING__MODEL` 设成 `text-embedding-3-small`（替身回 1536 维）与
  `bge-large-zh-v1.5`（回 1024 维），`DIMENSION` 跟着改。密钥那一格填的是占位串 `in-gate-stand-in`，
  不是任何真凭证。
- API：`uvicorn app.main:app --host 127.0.0.1 --port 8124`（8000 被本机另一个项目占着，
  所以取证脚本走 `AIWEB_EVIDENCE_API` 而不是写死基址）。
- worker：`scripts/run_worker.py` 独立进程，与 §1.7 同一进入方式。
- 登录只从 gitignored 的 `backend/.setup/local_admin.txt` 读，实录里每一行都只有
  `[login] 200，access_token 长度 171`——token 本身不进文件。
- **元数据库结构这一整段一个字没动**：`alembic current` = `0009`（探针第一行），全程只有 HTTP
  与只读 `SELECT`。取证脚本 `evidence034.py` 里一条写语句都没有。

#### ① 一次 reindex 的完整时间线（job 26，ds=1，替身 1536）

`GET /api/sync/jobs/26/events?cursor=0` 的首帧、`embed` 段每一表和终局，逐字（全 14 帧在
`out_reindex_ds1_timeline.txt`）：

```
[status] 200  content-type=text/event-stream; charset=utf-8
id: 3
event: progress
data: {"stage": "card_build", "phase": "card_build", "done": 10, "view": 1, "cards": 0, "total": 10, "base_table": 9, "payload": {}}
id: 4
event: progress
data: {"stage": "embed", "phase": "embed", "done": 10, "view": 1, "cards": 12, "total": 10, "base_table": 9, "payload": {"table": "ai_web_demo.category", "embedded": 1}}
id: 10
event: progress
data: {"stage": "embed", "phase": "embed", "done": 10, "view": 1, "cards": 12, "total": 10, "base_table": 9, "payload": {"table": "ai_web_demo.product_stats_wide", "embedded": 9}}
id: 13
event: progress
data: {"stage": "embed", "phase": "embed", "done": 10, "view": 1, "cards": 12, "total": 10, "base_table": 9, "payload": {"table": "ai_web_demo.v_daily_sales", "embedded": 12}}
id: 14
event: progress
data: {"stage": "done", "phase": "done", "done": 10, "view": 1, "cards": 12, "total": 10, "base_table": 9, "payload": {"profile_id": 2}}
event: done
data: {"job_id": 26, "status": "success", "counters": {"cards": 12, …}, "warnings": [], "errors": [], "duration_ms": 4508}
```

三件事在这里看得见、在别处看不见：

1. **`embed` 段一表一帧**，`payload.embedded` 是**累计卡数**不是表数（1→2→…→6→**9**→10→11→12，
   `product_stats_wide` 那一跳跨 3 张卡）。这是 P3 那份 `{skipped: true, code: …}` 单帧的形状
   第一次被真计数换掉（spec 故事 12）。
2. **终局帧里带着 `profile_id`**（`payload.profile_id: 2`）——"这颗作业建的是哪套档案"是
   可回放的事实，不用猜 202 的返回值（spec 故事 7）。
3. `warnings: []` 且 `errors: []`：reindex 那一路上 027 那道 settings 守卫**不插手**（已定口径），
   所以本轮把 `MODEL` 换成替身不认的名字时，作业照样 `success`——靶是档案那一行，不是配置。

#### ② 跑完之后库里那三行（`out_state_after_reindex.txt`）

```
### kb_index_profile（三态 + 三元组 + built_at）
  (1, 'no-embedding@1536@tplv1', '', 1536, 1, 'active', None, datetime.datetime(2026, 9, 29, 3, 34, 59, 277178, tzinfo=datetime.timezone.utc))
  (2, 'text-embedding-3-small@1536@tplv1', 'text-embedding-3-small', 1536, 1, 'draft', datetime.datetime(2026, 10, 3, 5, 39, 14, 17270, tzinfo=…))

### active 行数（ADR-0013 要求恰好 1）
  (1,)

### 每档案：卡片数与两根向量列非空数
  (1, 'no-embedding@1536@tplv1', 1536, 'active', 24, 0, 0, 0)
  (2, 'text-embedding-3-small@1536@tplv1', 1536, 'draft', 24, 24, 0, 24)
```

这一份快照里叠着三条事实：档案 2 **建完了但还没激活**（`draft` + `built_at` 已写）、
`embedding` 那一列 24 张全非空而 `embedding_1024` 全空（**同一张表的第二根列没被顺手写**）、
以及 active 那行的 `built_at` 仍是 `NULL`（线上那套从 P2 起就没有向量，本轮一个字没动）。

**"两颗作业共用一行 draft"的实物**：job 26（ds=1）与 job 27（ds=2）在 `sync_jobs` 里
`reindex_profile_id` **都是 2**，而档案表只有两行、卡片数是 `12 + 12 = 24`。这就是工单 034
口径条 2 那句"档案是全局的，同三元组必共用一行 draft"的真跑版本——它同时解释了为什么
`activate` 的前置校验必须按"覆盖当前 active 正在服务的**所有**源"来判。

#### ③ 激活：200 的两格与 active 计数

```
### POST /kb/profiles/2/activate
[status] 200
[body]   {"activated": {"id": 2, "name": "text-embedding-3-small@1536@tplv1", "model": "text-embedding-3-small", "dimensions": 1536, "card_template_version": 1}, "retired": {"id": 1, "name": "no-embedding@1536@tplv1", "model": "", "dimensions": 1536, "card_template_version": 1}}
```

激活后再查：`(2, …, 'active', …)`、`(1, …, 'retired', None, …)`、active 行数 `(1,)`。
**`retired` 不是删除**：档案 1 那 24 张卡一行没少（`out_state_after_activate.txt` 里
`(1, …, 'retired', 24, 0, 0, 0)`），这是"可回滚"的前提而不是结果。

#### ④ 换维度（1024）→ 读的是第二列 → 回滚，两根列的内容一字不变

同一套流程在 1024 那侧走一遍（job 29/30 → 档案 3），关键几行（**逐字来自落盘的那几份**）：

```
### 每档案：卡片数与两根向量列非空数
  (3, 'bge-large-zh-v1.5@1024@tplv1', 1024, 'draft',   12, 0, 8, 8)   ← out_state_1024_partial.txt:13
  (3, 'bge-large-zh-v1.5@1024@tplv1', 1024, 'retired', 24, 0, 24, 24) ← out_state_after_rollback.txt:13
                                                                        反向：1536 列全空

### POST /kb/profiles/3/activate                                        ← out_activate_p3.txt:4~5
[status] 200   [body] {"activated": {"id": 3, …, "dimensions": 1024}, "retired": {"id": 2, …}}

### POST /kb/search {"query": "订单"}                                   ← out_search_on_1024.txt:180~193
  "used_profile": {"id": 3, "name": "bge-large-zh-v1.5@1024@tplv1", "dimensions": 1024}
  [向量名次集合] [1, 3, 4, 5, 6]      ← 与 1536 基准下的 [1, 2, 3, 4, 5] **不是同一串名次**
```

**这里要按实物认一格账**：档案 3 正处在 `active` 那一刻**没有落盘快照**——那一发 activate 之后
紧跟着就是检索与回滚，本轮没在中间再 dump 一次 `每档案`。所以"3 曾成为基准"的证据是
**两头那两行 + 中间那发 200 + 检索响应里 `"used_profile": {"id": 3}`** 四件拼出来的，
不是某一份文件里的一行。（`24, 0, 24, 24` 那两个计数在 `active` 与 `retired` 两态下不会变，
变的是 `status` 那一格——所以这一格缺的只是"当时那一瞥"，不缺结论。）

"旧向量仍在（可回滚）"这一格**不按行数比，按逐行内容比**（`out_vecsum_*.txt`，
把每张卡的向量列 `::text` 按 `id` 序拼起来取 md5；全 NULL 那一族也照样有一个指纹，
所以"被写坏了"与"本来就是空"分得开）：

```
（激活档案 3 之前）            （回滚到档案 2 之后）
  (1, 'retired', be943793…, be943793…, 24)     (1, 'retired', be943793…, be943793…, 24)
  (2, 'active',  b03bc7d5…, be943793…, 24)     (2, 'active',  b03bc7d5…, be943793…, 24)
  (3, 'draft',   be943793…, fbfa9546…, 24)     (3, 'retired', be943793…, fbfa9546…, 24)
```

三串指纹（`be943793` / `b03bc7d5` / `fbfa9546`）跨"激活 3 → 检索 → 回滚 2"全部不变，
唯一变的是 `status` 那一格（上面那两行 `每档案` 里 1536 列那格都是 `0`、1024 列都是 `24`，
就是这两串指纹计数出来的同一件事）。回滚那一发是 `POST /kb/profiles/2/activate` → 200，
`retired` 的档案被重新激活——**spec 故事 6 后半句（"`retired` 可以再激活，这就是'可回滚'的落点"）
在真 HTTP 上是这一发**。顺带一条实物与字面的差：那道前置校验只管 `built_at IS NULL` 的 **draft**，
`retired` 不论建成没建成都能再激活，所以本轮靠 API 就把基准恢复回了开工那套（终局
1 active + 2 retired），零手工 SQL。回滚后再发一次 `{"query":"订单"}`，
`used_profile.dimensions` 回到 `1536`、名次集合回到 `[1,2,3,4,5]`。

#### ⑤ 语义那一格：**只有用例，没有真跑**（roadmap 验收 2 的空正文）

两句话，按已定口径分开写：

- **闸内替身跑通了形状**：`订单` 在激活替身基准后 200、`items` 5 项、每项 `keyword_rank` /
  `vector_rank` / `rrf_score` 三列齐（头一名 `ai_web_demo.order_main`，`keyword_rank 1` /
  `vector_rank 4` / `rrf_score 0.032018442622950824`，`took_ms 346`）。
- **真端点跑通了没有？未跑**。本轮真库里那句 `销售额` 在三种 `mode` 下各发一次，`items`
  **三次全空**（`out_search_pre_{keyword,vector,hybrid}.txt`，`[向量名次集合] []`、无 `table_uid` 行）。

全空这一格要说清它**不是**验收 2 的反证：替身回的是字 n-gram 哈希向量，"近邻=语义相近"对它
不成立（§2.3 第一条），而关键词那一路是 `ILIKE` + trgm——`销售额` 三个字在演示库的卡片文本里
**一个字都没有出现**（列名是 `amount` / `pay_amount`，注释是"支付金额"），所以关键词路空是
**正确行为**。验收 2 那句"销售额命中 `payment_record` 与 `order_main`"要的是**语义**那一路，
它的地基是用户自己配的 `AIWEB_EMBEDDING__*` 三键（密钥不进对话）。到位之前，这一格在
roadmap 的收口表里是**空正文 + 一句"只有用例、没有真跑"**，与 P3 收口那四格（故事 3/8/9/15）同形。

#### ⑥ 单表 embed 失败 → `partial`，其余表的向量在场（job 28，替身注入 `--fail-on order_main`）

```
### 每档案：卡片数与两根向量列非空数
  (3, 'bge-large-zh-v1.5@1024@tplv1', 1024, 'draft', 12, 0, 8, 8)

### 最近 5 颗作业
  (28, 1, 'partial', 'done', False, 3, Decimal('100.00'), {…}, [],
   [{'code': 'embedding_upstream_error', 'detail': 'ai_web_demo.order_item: embedding 端点不可用（HTTP 500），重试后仍没成功'},
    {'code': 'embedding_upstream_error', 'detail': 'ai_web_demo.order_main: …'},
    {'code': 'embedding_upstream_error', 'detail': 'ai_web_demo.payment_record: …'},
    {'code': 'embedding_upstream_error', 'detail': 'ai_web_demo.refund_record: …'}])
```

12 张卡里 **8 张有向量**、4 张没有，失败的**表名**逐张点名（不是"4 张卡"）。为什么替身按
`order_main` 这个串注入却炸了四张表：`--fail-on` 是对**请求文本**做子串匹配，而 `order_item` /
`payment_record` / `refund_record` 的卡片正文里有 `order_main` 的 FK 说明行——这是替身的
既定性状（§2.3），本轮正好拿它当"失败不止一张表"的自然样本。

**顺带一条本轮才在真库上看见的**：这颗作业终局是 `partial`，而档案 3 那一行的 `built_at`
**照样写了**（`2026-10-03 05:42:52`）。这一格**文档与用例都在**，不是本轮新发现的洞——
architecture §7 那行由 029 写明"终局 `success`/`partial` 才给靶盖 `built_at`，`failed` 不盖"，
用例 `tests/integration/test_kb_reindex_live.py:405` 那句"partial 也盖 `built_at`（已定口径）"钉着它；
本轮真 HTTP 真库又走了一遍，两边一致。要说清的是它**连着另一格**：`activate` 的前置判的是**卡片行数**
（`kb_service.missing_card_sources`），不是**向量在场率**，所以"12 张卡 / 8 根向量"这样一套有洞的档案
照样能上线——本轮没有演这一发（档案 3 后来被 1024 那轮撞名复用重灌成 24/24，再没带着洞过前置），
所以这一句是**从代码与用例读出来的**，不是跑出来的。口径本身是 spec 故事 5 与决策 ⑨ 定的，
"要不要再补一格向量在场率"归下一片（见 roadmap §P4 收口"账"段第 1 条）。

#### ⑦ 读路径的三元组漂移 → 502，而且**整请求失败**（本轮新看见的，闸内没有等价物）

把基准回滚到档案 1（`model=''`、1536 维、**24 张卡全 NULL**）之后，进程里的 `MODEL` 还指着
替身，于是配置的三元组与 active 档案不一致。写侧那条守卫本轮又跑到了（不写卡、告警、作业仍
`success`，形状与 §1.7.1 的告警段同）；**读侧不一样**：

```
### POST /kb/search {"query": "订单"}
[status] 502
{"error": {"code": "embedding_dimension_mismatch", "message": "端点返回 1024 维，这套档案期望 1536 维", "detail": null}}
```

（这一发跑在"替身回 1024、active 档案期望 1536"那一步上，所以 message 里两个数是 1024/1536。）

**要拍板的是形状而不是这个数**：`mode` 缺省是 `hybrid`，此时向量腿报错就**整请求 502**，
不会自动降级成关键词那一路。这与 spec 决策 ⑦ 那句"`mode='keyword'` 是降级路径"读起来是两件事——
降级路径今天只有**调用方显式改 `mode`** 这一条入口，系统自己不降。是不是要加"向量腿失败 →
退 keyword + 一条告警"，**归下一片**（已记无主账），本片按实物写。

#### 这一段真跑给不了的四件事（不许在收口表里被算成已闭）

1. **中文语义命中**（验收 2 那一格）——等地基是真端点，见 ⑤。
2. **`ef_search` 在真 HNSW 上把召回集合撑大**（验收 3 后半句）——24 张卡的规模下规划器不选
   索引扫，见 §1.8 手测表第 8 行。
3. **`member` 身份发 `activate` 的 403**——本机常驻只有一个 admin 账号，见 §1.8 第 6 行前半。
4. **真端点的计费与限速形状**（§1.8"留白"第二条）——替身回 500 只证"5xx 会重试、用尽后按表
   落 `partial`"（⑥ 那一发），不证额度、退避时长与全量耗时。

### 1.9 P4 枚举采样手测（工单 033 验收 4/5）

§8 那三步的默认值是 `false`，所以"开了才扫源库"这件事必须有人手工跑一遍两遍——闸内它绿，
只说明**用例自己**把开关按开了。本节两张表分别对应"闸内已证"与"手测才证"。

**已在闸内有证据（不需要手工重做）**：

| 格 | 闸内靶 | 它凭什么算证据 |
|---|---|---|
| 三枚键的默认值：`false` / `30` / `1000` | `tests/unit/test_settings.py`（字段默认那三行 + §8 键表来源注释） | 默认值与文档字面一对；`sample_distinct` 从 `true` 改判 `false` 是 P4 决策 ⑩，真相源写在断言旁边（roadmap 分组 7「扫源库数据，默认关」） |
| 候选筛子的四档：清单内六类都认 / `bool` 是 `boolean` 归一后的写法 / 清单外一概不认 / 数组后缀不认，再加生成列与"定义已给值域"两档不进 | `tests/unit/test_extract_sample_distinct.py`（七条） | 纯函数，带长度的归一写法（`varchar(64)`、`numeric(10,2)`）按基名判那一条钉的是"括号不许把候选筛掉" |
| 裁决口径：频次降序、同频次按字面升序、NULL 桶占一个分组但不进值域、全 NULL 不写空值域、去重后才算阈值、超阈值**什么都不写** | 同文件（七条，含"恰好等于阈值仍写 / 超一个就不写"那一对，以及"两格今天写同一份值域"） | 期望值是手算的字符串列表，不是从实现 dump；阈值非正数当场抛而不是安静不写 |
| `LIMIT` 是 `阈值 + 10` 的表达式而不是字面 40 | 同文件两条（默认阈值那条对文档字面 40，改阈值那条钉"它不是 40"） | 只写第二条的话第一条会跟着一起漂，所以两条成对 |
| SQL 形状：先扫窗再分组、两个键各自真的进语句、标识符在文本里而数值走绑定参数、名字里带引号时成对写进语句、只有读且一个会话变量都不碰 | 同文件五条（`test_采样语句逐字对得上_先扫窗再分组` 一族） | 逐字对语句，内层那个 `LIMIT :row_limit` 没有 `ORDER BY` 就是在这里被钉住的 |
| 三件小工具各自的边界：标识符转义（三种引号写法 + doubling + 空标识符当场抛）、值文本化（`bytes`/`bool`/`int`/超长按 SQL 字面那一份来）、行→组（`None` 留成 NULL 桶、次数收成 `int`） | 同文件五条（转义三条 + 文本化一条 + 行→组一条） | 这三件都在"发 SQL 之前 / 收结果之后"那两侧，方言各错各的，所以不并入形状那五条 |
| 一列发一条、没有候选时一条都不发 | 同文件两条（`test_一个候选列发一条_而且组挂回自己那一列` / `test_没有候选列时一条都不发`，两方言参数化） | 断的是**发包数与列的对应**，以及组挂回自己那一列——挂错列的话值域会张冠李戴，而两件事在"结果非空"上都看不出来 |
| 采样能力位不许成为第二份真相 | `tests/unit/test_sync_dialect_dispatch.py::test_采样能力位与工厂真产出的那个对象成对` | 断的是 `can_sample_distinct == isinstance(factory(...), SupportsSampleDistinct)`，防的是加一种源只改一张表 |
| 开关那两向与两键下发：打开后候选列的值域**第一次有写入点**、默认配置下一次都不发而作业照样 `success`、`max_distinct`/`row_limit` 改配置后下发的那一份跟着变 | `tests/integration/test_sync_sample_distinct_pg.py` 三条（`test_开关打开后_候选列的值域第一次有了写入点` / `test_默认配置下一次采样都不发_作业照样success` / `test_阈值与行数上限两键真的下发_改配置下发的那一份跟着变`） | 走真元数据库 + 真编排，看的是发出去的语句清单与**库里那一格**，不是"返回值没变"；两键那一条是 `monkeypatch.setenv` + `cache_clear` 走完 `run_sync`，钉的是"改了配置真生效" |
| 逐表失败隔离与 `partial`（`sample_failed` 新档）、失败不碰元数据行数、方言无能力时整步跳过 | 同文件三条 | 失败注入在 extractor 桩上，断的是**库里那一列这一轮的内容**与 `errors[].detail` 的 `schema.table:` 前缀 |
| 超阈值那一列什么都不写（验收 4 的编排那一半） | 同文件 `test_超阈值那一列什么都不写_阈值两侧各一列` | 同一张表两列**一写一不写**摆在一条用例里：只测"超的不写"会放过"整列一律不写"那种写法 |
| 同一轮内新采的值域就出现在卡片「取值」段；卡片那一步失败也带不走已提交的值域 | 同文件两条 | 前者是 §8 三步到 prompt 的那一跨（回整行文本逐字对，不是"有没有"），后者钉的是逐表事务边界 |
| 真 MySQL 上开了才扫：16 条采样 SQL、7 列拿到值域、关掉那一轮两格回 NULL | `tests/integration/test_sync_live.py::test_工单033_开采样跑真MySQL_短文本列首次出现值域_关掉那一轮又抹回NULL` | 期望值全部来自 `init_demo_mysql.sql` 的 `ELT(...)` 字面清单与写用例前的只读探针；含窗口≠全表那一格（`customer.city` 窗口 4 / 全表 12）与真 ENUM 列反证（`sample_values` 恒 `NULL`）；同一轮里 `counters["cards"]==0` 与"7 列有值域"同时成立，那是"采样不属于档案"的实物 |
| 真 PG 上开了才扫：只有 2 列拿到值域、`text` 与数组一律不碰 | `tests/integration/test_extract_pg_live.py::test_工单033_开采样跑真PG演示源_两列拿到值域_text与数组列一律不采` | 布尔那列经真 psycopg 回 Python `bool`，值域字面是**小写** `false`/`true`，与 MySQL 那侧的 `"0"`/`"1"` 同一归一值不同字面——这一点只有各跑一次真库才看得见；四个事件值在窗口里各 250 次全同频，所以那一格的次序由 tie-break 决定 |

> 一处形状留白，写在这里而不是埋在断言里：被采样命中的那一列，`enum_values` 与 `sample_values`
> 今天**内容相同**（`adjudicate` 一次返回、两格一起写，`test_两格今天写的是同一份值域` 钉的就是
> 这个事实本身）。两格的分家要等到"值域只进下拉、不进 prompt"或反之那一天——现在把它们写成
> 两个不同的东西会是假话。

**手测怎么跑（两步，都不需要新命令）**：

| # | 动作 | 期望证据 |
|---|---|---|
| 1 | 什么都不改（`AIWEB_EXTRACT__SAMPLE_DISTINCT`  absent 或 `false`），按 §1.7 那条命令起 worker、点一次"同步" | 源库侧 `general_log`（或 `pg_stat_statements`）里**没有** `AS sample_value` 那条语句；元数据库 `select count(*) from meta_column where sample_values is not null` = 0 |
| 2 | `backend/.env` 加一行 `AIWEB_EXTRACT__SAMPLE_DISTINCT=true` 重启后端，再点一次"同步" | 同一句 count 变成 7（MySQL 源）或 2（PG 源）；卡片正文里对应列多出一行「取值:」；终局仍是 `success` 而不是 `partial` |

第 2 步那一格的"卡片多一行"是 §8 三步的**唯一用户可见收益**，值域本身在 `meta_column` 里躺着
没人读就等于没做。它不在闸内的 live 用例里重复一遍：live 那一轮没有 active 档案（工单 033
把"采样不属于档案"钉成了同一轮里 `counters["cards"]==0` 与"7 列有值域"同时成立），而渲染那一跨
在 `test_sync_sample_distinct_pg.py` 里已经按整行文本逐字钉过。

**本轮（2026-10-03，工单 034 收口）按这两步各跑一次的实物**（运行形态同 §1.8.1 开头：真 API
`127.0.0.1:8124` + 真 worker 子进程 + 真元数据库；只发 `ds=1` 那个 MySQL 演示源）：

| 步 | 那一发 | 源库侧看到的 | 元数据库侧看到的 |
|---|---|---|---|
| 1（关） | job 31。入队那一发的响应体**只在终端**，落盘的实物是事件流（`out_final_sync_off.txt`，202 那两键由 `test_sync_enqueue_pg.py` 钉）与库里那一行 `started_at 07:16:30.687929Z` / `finished_at 07:16:31.562393Z` | 源库侧那把尺**换了表**：本机没开 `general_log`，用的是 `performance_schema.events_statements_summary_by_digest`（探针 `probe_digest.sql`，判据从"没有那条语句"变成"那条语句的 `count_star` 没涨"）。那 16 条含 `` AS `sample_value` `` 的摘要一条都没动：两份快照里都是 `count_star=17`、`last_seen` 停在 `13:36:22`，而 job 31 落在一小时之后的 `15:16:30–31`（本机）——**这一轮源库一条采样语句都没收到** | `select count(*) … where sample_values is not null` = **0**（ds1 `(133, 8, 0)`）；终局 `success`、`errors=[]`、`duration_ms=874` |
| 2（开） | job 32：`202` + `{"job_id": 32}`（`out_final_sync_on.txt`，五帧逐字在场） | 同一份探针里那 16 条**各 +1**（17→18），`last_seen` 全部落进 `15:21:20`/`15:21:21`，正好在 job 32 的窗口（`15:21:19–20` 本机）里——**"开了才扫"这一格的方向反过来也钉住了**：发包数与那一轮一一对应 | ds1 `(133, 15, 7)`：`enum_values` 8→15、`sample_values` 0→7（与 §1.9 表末真 MySQL 那行的 live 期望值 7 个**同一个数**）；ds2 一字不动 `(133, 8, 0)`——本轮没发它，所以"两方言都实现"那一格仍只由 `test_extract_pg_live.py` 那条与闸内那族背；终局 `success`、`errors=[]`、`duration_ms=904` |

三个数（`133` / `15` / `7`）是那一段探针的 `(datasource_id, 列数, enum_values 非空数, sample_values 非空数)`。
本机 MySQL 的 `last_seen` 是 **UTC+8** 而 `sync_jobs.started_at` 是 **UTC**，比对必须 +8——上面写的
两个本地时刻就是这么对上的，不是巧合。

再加三条只有这一跑才看得见的：

1. **第 1 步那一发没有东西可抹，所以它证的是"不发也不写"而不是"关掉会清空"**。开工探针
   `out_state_final.txt`（14:19，job 31 之前 ds=1 最后一次同步之后）已经是 `(133, 8, 0)`，所以
   `sample_values` 非空数在 job 31 前后都是 0。"把上一轮采到的值域抹回 NULL"这一格**本轮没被这两步
   重演**，它的证据是 §1.9 表末那条真 MySQL 的 live 用例（`_COLUMN_SYNC_COLS` 整列覆盖那条口径的
   后果），别把它算进本轮实录。运维口径照旧成立但出处要写对：**关开关等于清空，不是停止更新**。
2. **卡片正文里那些「取值」行第一次在场**（`out_final_cardtext_on.txt`）：`product.brand` 那 12 个
   中文品牌（常喜 / 木白 / 潮汐…）、`refund_record.reason` 那 15 条中文短语、`category.level` 的
   `2 / 1`、`product.tags` 的 20 条逗号短语——落库的都是**值域**不是行数据（§8 第二步阈值 30 之内，
   超的那几列今天什么都不写）。同一张卡里 `status enum` 那一行（`on_sale / off_sale / draft`）走的是
   §8.1 C 的真 ENUM 老通路，两路原料不同但渲染成同一行格式，从卡片文本上分不出来——分得开的地方在
   库里（`data_type='enum'` 的列 `sample_values` 恒 NULL，那条反证在 §1.9 表末第一行）。
3. **本机 `backend/.env:64` 今天是 `AIWEB_EXTRACT__SAMPLE_DISTINCT=true`**（033 手测遗留，本轮第 2 步
   就按这张表的字面要求留着它）。**代码默认仍是 `false`**（`app/settings.py` 与 `.env.example` 两处），
   §1.9 表第一行那条默认值证据不受影响。后果是这台机器每次同步都会真去源库跑 `GROUP BY`——
   要不要落回 `false` 由用户定，收口没代它决定。

### 1.10 P4 收口的闸（工单 034 验收 8）

`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 check` 整轮全绿（exit 0）。原始文件
`.scratch/p4-hybrid-retrieval/evidence/gate034.txt`（gitignored，所以结论数字抄在这里而不是只给路径）。
抄的是**每步的命令回显行 + 结论行**，中间省略的是 pytest 的进度点与 npm 的两行脚本回显：

```
+ uv run ruff check app scripts tests alembic  (backend)
All checks passed!
+ npm run typecheck  (frontend)
+ uv run python -m pytest  (backend)
1230 passed, 2 skipped, 1 warning in 905.76s (0:15:05)
```

`dev.ps1 check` 仍然只有 `lint` + `typecheck` + `test` 三步（§1.7.2 末那段话在 P4 一个字没变：
格式与类型**不在它里面**，`mypy` 今天仍没有闸目标背它，接进 `check` 归 P10 的 CI 那一片）。本轮手工补的
两条，逐字（这两行现在也有一份落盘文件了：`evidence/out_tools034.txt`，as-built(P4-034) 补——原文只抄回显
不给文件；本节另一处"只以文字进文档、没有落盘文件"的是 §1.10 末那条源库 `pg_stat` 探针，两处的账分开记）：

```
+ .venv/Scripts/ruff.exe format --check app scripts tests alembic  [dev.ps1 无此目标，034 手工补]
194 files already formatted
+ .venv/Scripts/python.exe -m mypy app  [dev.ps1 无此目标，034 手工补]
Success: no issues found in 57 source files
```

对照基线：工单 025 收口时是 **980 passed / 2 skipped**，本轮 **1230 passed / 2 skipped**——高出的 250 条
是 P4 那八片（026~033）加进来的，**只加不减**（034 本片零代码）。**两个 skip 没有变多、也没有换地方**：
本轮单独跑 `tests/guard/test_corpus_mutation.py -rs` 复核，两条 SKIPPED 都仍指向同一行
`tests\guard\test_corpus_mutation.py:61`，理由仍是"该用例没有 〈某个变异〉 的靶子"那一族，两支今天点名：
`表名换成系统表` 与 `改成 INTO OUTFILE`（`-rs` 回显落 `evidence/out_skips034.txt`，as-built(P4-034) 补）。
（守卫变异语料的
两个空靶，来历见 nl2sql-safety §7 末注"闸从 629/3 变成 631/2"那一句，与 DB 介质无关）。

**这一族整轮闸今天有三份留存物**（原文此前写"跑了两次，耗时分别是 549.17s 与 905.76s"——那个 549.17s 那一发**没有留下任何输出文件**，收口不许把一个查无实据的数留在文档里，故按现存的三份重写；订正 as-built(P4-034)，第三条由 as-built(P4-034 补账) 加入）：
① `gate034.txt`：`1230 passed, 2 skipped in 905.76s`（034 真跑实录那一段之后跑的，全绿）；
② 收口重跑（034-E 之后）：`1 failed, 1229 passed, 2 skipped in 513.04s`，那**一条红不是代码回归**——
`tests/integration/test_extract_pg_live.py::test_验收5_last_analyze_at_按两列取较晚者_视图与未分析过的表留空`
末段那条"至少一张表被分析过"的守卫红了。本轮直连源库只读探针实录：`ai_web_demo_pg` 的
`pg_stat_user_tables` 里九张业务表的 `last_analyze` / `last_autoanalyze` / `n_live_tup` **全是 NULL/0**、
`pg_stat_database.stats_reset` 也是 NULL，而 `pg_postmaster_start_time()` = 2026-10-19 09:00——
**postmaster 起来之后这个库没被分析过**（该守卫的用途正是"一个都没分析过时，这一格什么都没证到"，
它红得其所）。同一条用例前面那几行断言（逐张等于源库两枚时间戳里较晚的一枚、视图留 NULL）**照旧绿**，
也就是说"搬运没走样"这一半仍然成立，红的只是"这台机器现在没有新鲜度可搬"那一半。
出路是**由源库属主跑一次 `ANALYZE;`**（`demo_pg_ro` 是只读账号，跑不动这条；口令照旧不进对话），
跑完这条 live 用例即复绿。
③ `gate034_third.txt`（as-built(P4-034 补账)，2026-10-20 14:00）：`1230 passed, 2 skipped, 1 warning
in 499.67s (0:08:19)`，**exit 0**——用户按下面 runbook 补了 `ANALYZE;` 之后的整轮复跑，② 那一格已复绿。
三份的落盘处境逐个说清，不含糊：**① 与 ③ 各有一份文件**（`evidence/gate034.txt` /
`evidence/gate034_third.txt`，都在 gitignored 的 `.scratch` 下），**② 没有输出文件**——那两个数只存在于
本节与工单 034 交付记录抄下的那一行（与 549.17s 那一发同一种处境）。它这次**不因没落盘就被删掉**：
② 记的是"这一格红过、且红得其所"这件事本身，那是账 11 与账 12 的根据，删了它这两笔账就失去实物。

**这一发的 runbook**（as-built(P4-034)，用户拍板"命令写进文档、择时由本人跑"）：

```powershell
# 以 role `postgres` 的身份（= §1.5.3 建 `ai_web_demo_pg` 时那份 `pg_login.env` 里的 PGUSER；
# 该文件此刻不在 backend/.setup 里，所以这一发只能由持有它口令的人亲手跑）。
# ANALYZE 要求表属主身份：本轮实测 `information_schema`/`pg_tables` 里 schema `demo` 的
# 10 张表 `tableowner` 全是 `postgres`，`demo_pg_ro` 只有 CONNECT + USAGE + SELECT，跑不动这条。
& "D:\Program Files\PostgreSQL\18\bin\psql.exe" -h 127.0.0.1 -p 5432 -U postgres -d ai_web_demo_pg -c "ANALYZE;"
```

两个容易写错的实物：① **这台机器的 PG 18 装在 `D:\Program Files\` 而不是 `C:\Program Files\`**（服务
`postgresql-x64-18` 的 PathName 实测指 `D:\Program Files\PostgreSQL\18\bin\pg_ctl.exe`，`C:\Program Files`
下没有任何 PostgreSQL 目录，PATH 上也没有 `psql`）；② 不带表名的 `ANALYZE;` 分析**当前库**，正好覆盖
`demo` 那十张，不必逐张列。

跑完要重跑的三样（顺序即依赖序）：
1. `cd backend && .venv/Scripts/pytest.exe tests/integration/test_extract_pg_live.py`——先单点确认那条守卫复绿；
2. 整轮闸 `powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 check`；
3. 把第三份回显补进本节上面 ①② 两跑之后，以及 roadmap §P4 闸门段那句"两份留存物"里（那里今天写的是
   `1230 passed` 与 `1 failed, 1229 passed`，收口不许让它停在已知会复绿的那一版上）。

**这三样都已经落了（as-built(P4-034 补账)，2026-10-20：用户本人以 role `postgres` 补了那一发 `ANALYZE;`）**：
1. 单点复跑 → **`5 passed in 19.79s`**，红了的那条守卫复绿。**顺带把一处计数核清**：该文件今天收集
   **5** 条而不是文档多处写的"四条"——第四条之后 033 在同一个文件里加了
   `test_工单033_开采样跑真PG演示源_两列拿到值域_text与数组列一律不采`（`test_extract_pg_live.py:720`）。
   六处"四条 / 4 条"（architecture.md:974、metadata-model.md:1546、verification.md:371、roadmap.md:233
   / 327 / 380）**逐处回去看过，它们都是 024 那一轮的实录**（前四处带 `as-built(P3-024 live)` 或
   "本轮真跑说清楚"的抬头，后两处是 §P3 状态表里 023/024 那一行的证据列），**没有一处在陈述今天的
   收集数**，所以一处也不改写——把那一天的"四条"改成"五条"才是假话。今天的数记在这里。
2. 整轮闸 → 上面 ③ 那一发：`1230 passed, 2 skipped in 499.67s`、exit 0（同一份 counts 与 ① 逐字相同，
   只是那条守卫由红转绿，所以 `1 failed, 1229` 回到 `1230`）。
3. 本段就是那一格补账的实物；roadmap §P4 闸门段同一时刻从"两跑"改成"三跑"。

复绿不只靠用例自己说话，还有一发同账号只读探针（`demo_pg_ro`，只发 `SELECT`，回显落
`evidence/out_analyze_after.txt`）：`demo` schema 在 `pg_stat_user_tables` 里 **10 行**（9 张业务表 +
建库脚本的标记表 `_aiweb_demo_marker`；标记表按数据源排除规则不进同步范围，所以 §1.6.1 那个
"10 个对象（9 表 + 1 视图）"的口径**不变**），`last_analyze` **10/10 全部非 NULL**、
`last_autoanalyze` **0/10**（说明这一发是手动 `ANALYZE`，不是 autovacuum 顺手触发的）、
`sum(n_live_tup)` 从 ② 那一份的 **0** 变成 **12791**、逐张时间戳都是 `2026-10-20 13:50:25~26`，
而 `pg_postmaster_start_time()` 仍是 `2026-10-19 09:00:20`——**postmaster 起来之后这个库第一次有统计**。
账 11 那笔"直连源库发只读 SELECT 的记法待用户认"**照旧挂着**；差别只在这一发的输出落了盘，
所以它的可复现性不再比其他实录低一档。

**为什么这不是"闸门坏了一半"**：`ANALYZE` 是源库的维护动作，本来就不在本仓库的构建链里；本仓库对它的
依赖只有那一条守卫用例，而它拒绝在"这台机器一个表都没分析过"时假装证到了新鲜度——所以红是证据在说话，
不是回归。把 `ANALYZE` 做成脚本入口（`Makefile` 的 `demo-db-pg` 旁边、或 `scripts/demo_pg.ps1` 加一步）
**本片不做**：034 是零代码收口片，加脚本能力要另立工单，已按这条记进 roadmap §P4 的无主账（见那里
新增的账 12）。

这一路里有真 LLM 端点与真 embedding 替身的往返，时长本来就不可复现，**收口只按计数对账**。

无 DSN 那一支（roadmap §P4 验收 4 的左半边）另跑一次，逐字在 `out_unit_no_dsn.txt`：
命令 `env -u AIWEB_PG_TEST_DSN -u AIWEB_REQUIRE_PG_TESTS .venv/Scripts/python.exe -m pytest tests/unit -q`，
结果 **750 collected / 750 passed / 0 skipped / 0 failures**（`--junitxml` 计数，6.247s；那份逐行点阵里
没有任何 `s` 字符）。`tests/unit` 整目录在 DSN 缺失时**照跑、一条都不跳**，这才是那条验收要的形状——
它证的不是"跳过得干净"，而是"根本没东西需要跳"。

---

### 1.11 P5 守卫加固的闸门口径（工单 035 起头）

P5 是**零迁移**片，所以本节开头重复 §1.8.1 那个探针的动作：查实真库版本。本轮（2026-10-20）跑
`cd backend && .venv/Scripts/alembic.exe current` → **`0009 (head)`**，与 P4 实录里"探针第一行 = `0009`"
同一个落点。
P5 全程**不该出现第二个数**；041 收口时再跑一次做对照，若那一刻读到 `0010`，就是有人往零迁移片里
塞了迁移，先回工单再说。

闸门基线沿用 §1.10 的第三跑：**1230 passed / 2 skipped、exit 0**（HEAD `0cba24f`）；
035 施工半边整轮实跑 **1238 passed / 2 skipped、exit 0**（正是基线 + 本片新增 8 条，一条未减），
`ruff format --check app scripts tests alembic` 195 files clean、`mypy app` 57 files 零问题。
`tests/guard` 一族在 035 之前实测收集 **186 条**（`pytest tests/guard --collect-only -q` 逐文件：
`test_reject_corpus` 54 + `test_allow_corpus` 20 + `test_corpus_mutation` 90 + `test_guard_api` 8 +
`test_postgres_dialect` 14），035 之后是 **194 条**（多出的 8 条全在新文件）。
注意 **54 与 18 是语料条数、20 与 186 是收集条数**：allow 那一族
`ALLOW_CASES` 是 18 条，收集出 20 是因为另有两条非参数化用例。035 的口径是**只加不减**，
新增全部落新文件 `tests/guard/test_rule_id_closed_set.py`，一条现有用例都不改——真改了，
说明动的不是类型而是判定或值域，那是越界，回滚重做。

035 的验收 1 不靠"跑过了"这句话，靠**同一份探针跑两遍**：脚本
`.scratch/p5-sql-guard-hardening/evidence/snap_ruleid035.py`（只读探针，不改任何文件、不连任何库）
把 54 条必拒语料的 `expected / actual / type(rule_id).__name__ / violations 条数 / message 原文`，
与 18 条必放行语料的 `tables / sql_final 原文`逐条落盘，改动前后各跑一次。before 用 `git show` 把头
那份 `sql_guard.py` 的字节回灌到工作树再跑，跑完按 md5 断言换回来——快照对 `actual` 与 `message` 原文
逐条落盘，所以"before"必须真是改动前那份实物，不能是我记的实物。两份各 **230 行**，
`ruleid_before035.txt` / `ruleid_after035.txt` 的 unified diff 共 **56 行**，逐行分类实测：
标题那行（`label=before/after`）**1**、"GuardRuleId 在场？"（False→True）**1**、
`kind=` 那一栏（`str`→`GuardRuleId`，即异常里 `rule_id` 的实际类型换了）**54**——
抹掉这三类后两份**逐行完全相同**（脚本断言归一化后相等），54 条的 `expected`/`actual`/`message`
与 18 条的 `sql_final` **没有一个字变过**，两份汇总行都是 `期望与实测不一致的条数 = 0`。

同一份探针顺带钉住 §1.1.1 那张词表的算术：17 枚码在 54 条语料里的期望值分布逐枚相加
正好 **54**，且**双向无孤儿**——没有一枚码在用例里点不到名，也没有一条期望值不是文件里在场的码。
这两个"没有"就是那条封闭集合用例的靶子（表驱动，036/037 加新码时只动枚举一处、不维护平行清单）。

验收 3 要的"故意塞一枚假成员必须红"是**一次性变异**，实现在
`.scratch/p5-sql-guard-hardening/evidence/mut_ruleid035.py`（结论落同目录 `mutation_ruleid035.txt`，
**没有**留进永久用例）。四格每格都先断锚点在源文件里正好命中一次，写脏后跑整族 `tests/guard`，
再 `try/finally` + md5 复原；四格与实测红格：
① 塞一枚没有语料点过名的假成员 → 红 **2** 条（"双向无孤儿"与"可穷举"）；
② 把一枚成员的值改脏（多一个 `s`）→ 红 **25** 条（语料逐条归因 3 + 放行侧攻击变异 18 + 枚举手自身 4），
这一格就是"对外值不许变"那条口径的实物防线；
③ 把一个调用点的首参退回裸字符串 → 红 **3** 条（两条 `ast` 机械检查，外加"两条接缝同一枚成员"那条的
`is` 身份断言——裸串与成员相等但不 `is`，所以身份断言不是多余的）；
④ 把 `_scan_raw_text` 三元组表里一枚码退回裸字符串 → **只**红 1 条（"码字面量只住枚举手"）：
首参那条抓不到它，因为传进去的是变量——**这一格正是第二条检查存在的理由**，否则它就是重复劳动。
四格各自 `rc=1`，复原后整目录回 `rc=0`。

036 在这一节留两格口径变化与一份十格变异实录。

**收集数**：`tests/guard` 从 035 之后的 **194** 续到 **213**（逐文件小计相加，实录
`.scratch/p5-sql-guard-hardening/evidence/collect036.txt`），多出的 **19** 条里 **18** 条在新文件
`tests/guard/test_regeneration_invariant.py`、**1** 条在 `test_rule_id_closed_set.py`（下面"判据放宽"那一格
补的那条独立用例），其余五个文件的计数**一个没动**。**"一条现有用例没改"这句话要收窄**：
036 确实改了一个既有测试文件——`test_rule_id_closed_set.py` 的机械判据本身（035 原判据会把
`regeneration_drift` 判成孤儿），除此之外一条没改；语料条数 **54 拒 / 18 放行**
按工单口径**一字未动**（036 的 hint 那三种形状作为参数化用例住在新文件里，没塞进 `corpus.py`——
塞进去就会把 18 变成 21，那是改语料不是加用例）。整轮闸 **1257 passed / 2 skipped、exit 0**
（= 035 的 1238 + 本片 19，一条未减；534.20s，完整汇总行落盘 `evidence/gate036_final.txt`），
**这一份是第四跑**——前三跑分别在补那第 9 条用例之前、汇总行被 PowerShell 截断、以及 17:18 启动而
两个测试文件在 17:21／17:23 才落笔，都没量到最终字节；第四跑启动前用
`find backend/app backend/tests scripts -name "*.py" -newermt …` 核过"最后一个动过的 `.py` 早于启动"，
理由与教训记在工单 036 交付记录的偏离 ⑦。
`ruff format --check app scripts tests alembic` 196 files clean、
`mypy app` 57 files 零问题。

**封闭集合那条用例的机械判据在 036 放宽过一次，同片收尾时又补严了两道**（不是把新码从判据里摘掉，是加了一条
并列来源）：`regeneration_drift` 是内部一致性故障，今天没有任何一支模型 SQL 能触发它，所以它**不可能**被语料
点过名，035 那版"每枚码都要在 54 条期望值里出现"会把它判成孤儿。判据因此改成
**语料期望值 ∪ `tests/guard` 断言里以 `GuardRuleId.<成员>` 形式点名的 AST 引用**——用 AST 而不是子串，
是因为子串抓不到"点的是枚举手而不是别的同名文本"；工单字面"不许为绿而放宽"这一条按实物兑现为
"放宽的是**点过名的方式**，值域本身仍不可穷举即红"。
收尾时双轴审出第一版留了一个空洞并补了两次（实录在 `test_rule_id_closed_set.py` 的 as-built 注里）：
① 扫描范围从"整个文件"收到**只看 `ast.Assert` 里面**——写在注释、docstring 或一句游离赋值里的点名不算钉住；
② 第一版那句 `node.attr in members` 的预过滤让"成员名拼错／成员被删"这一类**在构造上永远红不了**
（非成员值根本进不了集合），现在原样收名字，非成员的由新增的那条 `test_用例点名的成员全在枚举里` 单独红
——这就是 `tests/guard` 从 212 到 **213** 的那一条。放宽真正的代价（"点过名"≠"那条分支被走过"）
机械判据管不到，靠的是 `regeneration_drift` 自己那条注入失败的永久用例，这句话写在文档面上。
用例与判据同在 `test_rule_id_closed_set.py`。

**比对口径从 `Expression.__eq__` 换成"一条不动点 + 三格承重事实"**，换形的两处实测否证都在
`.scratch/p5-sql-guard-hardening/evidence/`：`drift_diagnosis036.txt`（`__eq__` **过火**——
`SELECT id FROM customer LIMIT 5000 UNION ALL SELECT id FROM payment_record LIMIT 3` 今天合法放行，
生成器给带 LIMIT 的臂加括号，重 parse 回来 `.this` 从 `Select` 变 `Subquery`，语义一字未变却判成漂移）、
`hint_probe036.txt`（`__eq__` **不及物**——30.19 下它看不见注释丢失：PG 那支 `/*+ SeqScan(t1) */`
剥掉注释重 parse 回来，树上已无那个节点（"重 parse 之后 hint 还在树上？ False"），而同一支的
"tree == reparsed（sqlglot `__eq__`）？ True" 照样成立）。
换形由用户 2026-10-20 拍板，钉子是 `test_守卫给臂加的括号不算漂移`：`__eq__` 那一版它红，四格这一版它绿。

**十格变异实录**（`evidence/mut_drift036.py` → `evidence/mutation_drift036.txt`，每格 try/finally + md5
逐字复原，源文件 `78a9135716215582adf7c589355e520e`＝**最终 shipped 那一份**，末格复原后整目录回绿）。
**这一版是重跑的**：上一版电池跑在 mypy 那两处理顺之前的 `f2c8dd4a…` 上，两处锚点（`type(tree)`、
`set(refs)`）在最终字节上已经不是那个字面了，按 035 立的"补跑而不是解释"整电池重跑：
A 摘掉整道自校验 → 红 **2**（只红"接线"那两条，真实输入的判定不因它而变，这就是"永真保险丝"的实物形态）；
B/C/D/E 把某一格改成永不成立 → **各红 1**，且各自只红自己那一条（四格没有互为重复的证据）；
F 工单 036 验收 2 字面指定的 `comments=True` → 红 **0**，是**等价变异**：注释在 parse **之前**就被
`_strip_comments` 抹掉，树上一个 `.comments` 节点都没有，所以出口那两个参数对守卫的输出逐字相同
（`evidence/comment_free_parse036.py` 逐支量 5 条带注释的必放行语料，两版一字不差）——
**这一格如实记成"造不出漂移"，没有为了验收 2 变红去故意写错实现**；
G 把比对左侧从 `final` 换成入口 `stmt` → 红 **14**，全是合法查询被误判成内部故障，证明 §1.3 那句
"比对的两侧是钳位之后的 `final`"是承重的不是措辞；
H 从端出去的文本里抹掉 ` LIMIT 1001` → 红 **15**（打在 ④ 顶层行数）；
I 把端出去的文本里 `customer` 换成 `payment_record` → 红 **27**（打在 ③ 表引用）。
H/I 不是今天存在的 bug，而是这道自校验存在的理由。
J **总是钳位**（工单验收 2 字面第一发"对已带 LIMIT 的语句也钳一次"的实物版，收尾时补的一格）→ 红 **3**，
而红的**全是钳位语义**那三条（`test_上限之下的_LIMIT_原样保留`、带 `OFFSET` 那支语料、
`test_守卫给臂加的括号不算漂移`），036 的自校验用例**一条都不红**——
这一格就是"那一发造不出漂移"的凭证：上一版这里只写了一句"`.limit()` 是替换不是叠加"的分析，
没有 artifact，双轴审查按 035 的纪律要求补跑，跑完这句话仍然成立但现在是实测而不是推理。

**性能那一格按 shipped 实现复测**（N=1000 取均值，`evidence/perf036.txt`）：关掉自校验的 `guard()`
**0.702 ms/次** → 四格版 **1.273 ms/次**（+0.571 ms，+81.2%；参照物单跑一次
`parse(sample).sql(comments=False)` = 0.502 ms）。文档半边那版记的 0.619 → 1.090 是 `__eq__` 版的同一口径
实测，换形之后重跑才写新数——旧数留在 `hint_probe036.txt` 里可查，不覆盖。同一份 shipped 字节
（md5 `78a91357…`）连跑三轮的增量落在 **0.571~0.592 ms**，本节此前记的 0.712 → 1.279（+79.6%）
量在最后一处改动之前的字节上：这个来回全在噪声带里，**不许**读成"那次提取省了时间"。

**037 的第二层：这一节的账先按实测记，施工半边的数不落笔。**
P5 仍是零迁移片，真库版本沿用本节开头那个探针（`0009`）。037 改的是运行期行为，
**不动元数据库一个字节**，所以它在这一节留下的第一格是"开工前重核"而非"库变了"：
`AIWEB_QUERY__DRY_RUN` 当时**不存在**（`QueryGroup` 无此字段、`.env.example` 的 `AIWEB_QUERY__` 那一组
没有这一行）、`max_explain_rows: int = 50_000_000` **只有定义点零读取点**、`EXPLAIN` 在 `app/` 下
只出现在注释里（`datasource_service.py` 那句"第二层 EXPLAIN dry-run"）。
认字不认行号，量于 2026-10-20。

四份实测把设计钉死（都在 `.scratch/p5-sql-guard-hardening/evidence/`，真 MySQL 5.7.17 + 演示库
`ai_web_demo`，口令从 gitignored 的 `.setup/aiweb_ro.cnf` 读、不入对话不回显）：

| 探针 | 量的是什么 | 关键读数 |
|---|---|---|
| `probe_explain037.txt` | 13 条"用户真会问"的形状各自的 `EXPLAIN` 逐行 `rows` | 12 列、估行在 `rows`、逐表一行；`UNION` 两臂是 `[30226, 87912, None]` |
| `probe_explain_null037.txt` | 同一批里**哪些条给出 `NULL`** | 13 条里 **3 条**：裸聚合 `[None]`、派生表 `[1, None]`、`UNION` `[30226, 87912, None]` |
| `probe_explain_json037.txt` | `NULL` 那一档引擎到底知不知道 | `count(*)` 的 `FORMAT=JSON` 里**没有任何 cost/rows 键**，只有 `"message": "Select tables optimized away"`——是"证得了它不扫表"，不是"证不了" |
| `probe_cartesian_ast037.txt` | sqlglot 30.19.0 的 `Join` 节点在六种写法下的 `side`/`kind`/`method`/`on`/`using` 与重生成文本 | 逗号连接与 `FROM a JOIN b`（不写 `ON`）**逐字同形**（AST 与文本两侧都分不开）；`NATURAL` 住在 `args["method"]`，`side`/`kind` 都是空串 |

`NULL` 那一格是本轮**唯一一次口径被实测顶回来**的：工单 037 的"已定口径"写的是"读不出来就拒"，
按字面它会把 `SELECT count(*) FROM t`（最常见的那类问句）永久拒掉，而 `DRY_RUN` 默认开、prod 锁死不能关。
处理方式是量完 13 条再摆三个选项交用户拍板（037 施工前选定：**全 `NULL` 才算读不出来，
且每一行都自证 `optimized away` 时放行**），既没悄悄收窄也没硬按字面破口——
裁决口径与理由在 nl2sql-safety §3.2，这里只记"这一格是问过的、答案是用户给的"。

顺带一条不写就容易被当成实测过的：`dry_run_unreadable` 那一档在演示源上**今天不可达**
（13 条里没有一条落到它），它由桩件驱动；`EXPLAIN SHOW TABLES` 那一支（`1064`）在真链路上取不到，
因为 `guard()` 对 `EXPLAIN` 前缀先报 `top_level_not_select`。这两格在 §9 的诚实清单里有单独的行，
交付记录里不许写成"真跑过"。

**收集数、整轮闸、变异电池留给施工半边**（037-C~E）——文档半边不预填，那些数还没有对应的字节。

后续 038~040 的实测与手测往本节续写，一格一把；041 收口时把 P5 五条验收逐条对上这里的证据路径。

---

## 2. 测试分层

### 2.1 单元测试重点（纯函数，不碰 DB / 不碰网络）

| 目标 | 断言方式 | 关键用例 |
|---|---|---|
| `token_estimate` | 值域 + 单调性 + 分支（as-built(0008)：本行六条**不由 008 全认领**。008 交付的是 heuristic 纯函数，钉了 ③④ + 五条手算值域用例（`tests/unit/test_token_estimate.py`）。as-built(P2-0010)：①② **在 P2 不做**——用户拍板"prompt 预算沿用 008 的 heuristic 口径，不引 `tiktoken`"，`AIWEB_RETRIEVAL__TOKENIZER` 键与真分词器参照一起推给引入依赖的那一档（roadmap §P4）；⑤⑥ 走的是结果集与 `TOKEN_BUDGET` 裁切，属 011（010 只做卡片段的预算裁切，用的就是这个 heuristic 函数） | ① [P4] 与 `tiktoken.encode` 计数偏差 ≤8%（`cl100k_base` 可用时）；② [P4] 强制走 heuristic 分支时中文/英文/混合三段偏差 ≤25%（宁高估勿低估）；③ 单调：文本变长则估计不减；④ 空串=0；⑤ 超长 JSON 行结果集不炸（011）；⑥ "结果集只喂摘要"路径的预算 ≤`TOKEN_BUDGET`（**as-built(P2-012)：这条改归 012**——它是 ⑨ 结论那一步的输入预算，住在 `pipeline` 而不是 executor；011 交付的是 ⑤ 那一半。**落地形态是"超预算告警、不裁切"**：`result_brief(token_budget=)` 算一遍**选中那份素材**的体量，超了就 `logger.warning`，一个字不删。三条理由：摘要分支比整表**更长**（要开 stats 表加两张头尾表），"装不下就换摘要"会把超预算变成更超预算；摘要再裁就只剩"共 N 行"，那不如让模型看全；语义与 P4 验收 6"被裁的表要记日志、不报错但要可查"同一条。用例三条——典型 60 行摘要 ≤1500（预算不是摆设，1500 用字面量而不是 `Settings`，跟着配置断等于断"它不小于自己"）、超预算仍回整表（不改道）、几百个数值列的摘要超预算时报警且内容一字不缩） |
| RRF 融合 | 手算黄金值 | ① 两路各 `[a,b,c]`/`[c,a,b]`、k=60 → 期望顺序与分数硬编码；② 只有一路有结果时仍参与；③ `keyword_weight=0` 退化为纯向量；④ 并列名次的稳定排序（同分按表名字典序，防抖动）；⑤ 空输入返回 `[]` 不抛。**as-built(P4-031)：本行由 031 认领**，五条落 `tests/unit/test_retriever_rrf.py`（12 条，031 交 11 条、032 补一条 `test_融合之后的表对象自己带着两路名次与融合分`；全手算：期望分数写成 `1/61 + 1/64` 这种能对着 §5.2 (3) 核的字面而不是小数）。三处与字面不同：③ **没有 `keyword_weight` 这个旋钮**——工单 031/故事 27 的落法是三路开关，"纯向量"那一档就是 `mode='vector'`（用例在 `tests/integration/test_kb_search_hybrid_pg.py`，因为它要真发一次 HTTP），所以这一格从"权重为 0"改成"只有一条腿"；②的形态相应改成"一条腿为空时融合次序就是另一条自己照抄"；④ 的 tie-break 实物是 **`table_uid` 升序**而不是"表名字典序"（CONTEXT.md「表唯一键」：它是 md5 over `\x1f` 连接的三段，稳定但不可读——与 §5.1 那三个计数键的第三键同一个来源，两处不各写一份）。④ 之外还加了一条正向钉子（验收 2）：把 `score_kw` 跨表均匀放大/平移、或把宽表分数打到窄表的一千倍，**融合名次一字不动**——若有人按 `score_kw` 排名次，009 那次"68 列宽表打穿 `order_main`"的破验收会当场复现 |
| `join_graph` BFS | 图与路径（as-built(P2-0010)：**整行不属于 010**。全仓没有 `join_graph` 实现，010 只渲染候选表之间的直连边；图算法与 architecture §5.3 的边权重/环/多路径歧义/降级四条一起归**工单 014 JOIN 图切片**） | ① 无 FK 但按命名约定推出边、`confidence` 按 §5.3 加权公式算（as-built(P2-0010)：两处口径都改了。常数"=0.7"与公式互斥，已由 010 闭环——0.7 现在是**公式地板**；原举例 `order_item.order_id → order_main.id` 在演示库**推不出来**：`order_id` 的名词是 `order`，库里没有 `order`/`orders` 表（真表叫 `order_main`），命名约定不认"前缀加后缀"的自由变体。库里真实存在的那条推断边是 `user_activity_log.product_id → product.id`（两侧同为 `INT`，实测算得 **1.00**），拿它当例）；② 自环 `parent_id` 不产生 1-hop 自引用；③ hops=2 时最长路径不超 2；④ 环（A→B→C→A）不死循环、路径去重；⑤ 桥表扩展：问 A、D 两表时能拉进 B/C，且 `path=[A,B,C,D]` 顺序可渲染进 prompt；⑥ 不可达时返回"需笛卡尔积"标记而不是硬连；⑦（010 转来的债）表度 > `MAX_JOIN_DEGREE` 的表**只禁当桥、不禁当端点**（它的直连边照旧进【可 JOIN】）——as-built(P2-0014)：本行**由 014 认领并加了第七行**；`hops` 数的是**桥表张数**（不是边数），跳数定上限、§5.3 的边权重只在上限内定胜负、权重再并列才算 `ambiguous`；图**存方向、按无向扩展**（真实 FK 恒为子→父，按有向可达则演示库除"子→父"一跳外一律不可达）；缓存键 `(datasource_id, database_id)`、同步终局后按库精确失效。**用例落点**：①~⑦ 全部在 `tests/unit/test_join_graph.py` 用手算图跑纯函数（本行口径"不依赖真库"），另加 `tests/integration/test_join_graph_rows.py` 钉唯一真 IO——读 `meta_relation` 的查询：演示库真边上 `payment_record↔product` 经 `order_main`+`order_item` 两张桥表可达、`category.parent_id` 自环不产生 1-hop 自引用、`user_activity_log→product` 带 1.00。写侧的字段度只做**告警**不丢边（丢弃分支在现行减法下不可达，理由见 architecture §5.3 的 as-built(P2-0014)），钉在 `tests/unit/test_sync_infer.py`（六条，`high_degree_fields` 的门槛是**严格大于**）+ `tests/integration/test_sync_pg.py`（断的是同步**响应**里的 `warnings`：`join_field_too_generic` 恰好一条、detail 带"出现在 10 张表"，而 `counters.relations_inferred == 9` 一条不少）。**as-built(P2-014)：本行已交付**——公共表面是 `join_graph.py` 的 `load_relations`/`graph_for`/`expansion_for`/`structure_hints`/`joinable_edges`/`invalidate` **六个**入口（纯函数 `build_graph`/`expand` 由单测直接钉）。①~⑦ 落 `tests/unit/test_join_graph.py`（**20 条**，全手算图）：验收 ①~⑦ 各一条，加的是门槛边界那一格（度 8 不抑制、度 9 抑制，且用例先断"度确实是 9"再断行为——门槛写成 `>=` 时不会假过）、自环那条钉的是 `number_of_edges() == 0`（"进图后被特殊对待"和"根本不进图"在路径断言上分不出来）、**同一对列上 `manual` 与 `extracted` 并存**时的覆盖顺序（真库的 `uq_meta_relation_key` 认这三档，不钉就是后加入的悄悄赢）、结构提示不受度门槛管、缓存三条（按库分键 / 失效只清那一个库 / 有条数上限）与"命中缓存不查库、但边变了只能靠失效"。真库那一半落 `tests/integration/test_join_graph_rows.py`（**8 条**，用例自己插行、不依赖演示库当前状态）：`payment_record↔product` 穿两张桥表、`hops=1` 时那一对就是连不上、`category` 自环照常参与跨表、0.700 那档被门槛筛掉、候选跨两个库时那一对按 `needs_cartesian` 报、新落库的边要等同步失效才进图。缓存失效落 `tests/integration/test_sync_cache_pg.py`（3 条：成功清、partial 只清提交过的库、一个都没写成则一个都不清），接线那一半落 `tests/integration/test_pipeline_orchestration.py`（**5 条**：桥表卡片被补查且进守卫白名单、**补不出卡片时整条路径撤下而不是留半条**、`hops`/`max_degree` 真从 `Settings` 传下去而 `structure_hints` 两个都不吃、单候选不降级、两候选零路径才降级）。真链路一条实录见 architecture §4.1 的 as-built(P2-014)。 |
| `chart_advisor` | 规则决策表 | 时间+数值→line、类别(NDV≤12)+数值→bar、NDV>12+数值→bar(取 top10 + "其他")、双数值→scatter、单行单值→`kpi`、全 NULL 列→table only、行数 >200→不画图；`EChartsOption` 结构快照（键存在性，不比像素）。as-built(P2-012 开工前拍板)：本行**由 012 认领**（此前 architecture §2 的目录树里挂着 `chart_advisor.py` 却没有任何工单写它，roadmap P8 验收 5 是它的 UI 双证、不是它的出生地）。两处字面改了：① "单行单值→number" 与 architecture §4.2 的 `kpi` 是同一样东西的两个名字，**统一写 `kpi`**（`chart_spec.type` 是要进 golden 的字面，两个名字等于两条对不上的断言）；② 快照对象是**自有形状** `{type,x,series,title}`（metadata-model §2.7），不是 `EChartsOption`——翻译那一层归 P8，理由见 architecture §6.1 的 as-built |
| PG/MySQL 类型归一 | 映射表（as-built(P3-023)：本行**由 023 交付**。用例三张——`tests/unit/test_pg_type_normalize.py`（PG 原料）、`tests/unit/test_mysql_type_normalize.py`（MySQL 原料 + `rows_to_columns()` 的接线）、`tests/unit/test_type_domain.py`（两方言共用的**那张断言表**，文件名以 `test_` 开头是为了让它真被收集执行）。规范字面全仓唯一住在 `app/extractor/type_normalize.py`（`VALUE_DOMAIN` + 两张 SYNONYMS + `compose`），`postgres_types.py`/`mysql_types.py` 只做方言原料的解析。归一落**抽取层（写侧）**：`data_type` 存归一值、`raw_data_type` 存 `COLUMN_TYPE` 原文）| PG `_text[]`/`numeric(10,2)`/`varchar(64)`/`timestamptz`/`jsonb`/`serial`/`generated always`；MySQL `decimal(10,0) unsigned zerofill`→**`numeric(10,0)`**、`enum('a','b')`→`enum`、`set`、`tinyint(1)`→**`bool`**（原行的"→bool 与否"问号已收，理由见 §9 as-built(P3-023)）、`datetime(3)`、生成列、`utf8mb4_0900_ai_ci`(8.0) 出现在 5.7 时的容错。跨方言塌成一个字面的两族：布尔（`boolean`/`tinyint(1)`→`bool`）与任意精度小数（`numeric`/`decimal`/`dec`/`fixed`→`numeric`，**值域里没有 `decimal`**，由 `test_decimal_不是归一值域的成员` 钉）。真跑那一半落 `tests/integration/test_sync_live.py` 的最后一张用例（工单 023 验收 5：演示库每一列的 `data_type` 过共享值域断言、不含空格即无修饰残留，再对八列点名比对 `(data_type, raw_data_type)` 成对）。**PG 侧的接线归 024**（PG 抽取器本体），本片只交付纯函数 + 用例 |
| §8 三步采样的**三层**（as-built(P4-033)：纯半边 `app/extractor/sampling.py`、两方言的 `build_sql_sample`、编排 `sync_service._run_sample_stage`） | 三层分开钉，因为三处会各错各的：筛子错在"谁进了候选"，裁决错在"值域的顺序与要不要写"，编排错在"发了几条、失败时丢了谁"。SQL 形状仍钉在**假连接**上（`_rows` 是方言层唯一 I/O 出口，同上面 020/024 那两行的理由），值域**真落库**只能走 `pg`，而"真源库给的类型是什么"只有 `live` 答得出 | 纯函数 28 个用例函数（参数化展开为 **55** 条）在 `tests/unit/test_extract_sample_distinct.py`：候选筛子七条（六类都认 / `bool` 是 `boolean` 归一后的写法 / 清单外不认 / 带长度按基名判 / 数组后缀不认 / 生成列与"定义已给值域"两档不进）、裁决七条（频次降序 + 同频字面升序、恰好等于阈值仍写与超一个就不写**成对**、NULL 桶占分组但不进值域、全 NULL 不写空值域、去重后才算阈值、阈值非正数当场抛、**两格今天写同一份值域**）、`LIMIT` 表达式两条（默认阈值那条对 §8 字面 40，改阈值那条钉"它不是 40"——只写后者会两条一起漂）、SQL 形状五条 + 逐列发包与"无候选一条不发"两条 + 标识符转义三条 + 值文本化一条 + 行→组一条。编排九条在 `tests/integration/test_sync_sample_distinct_pg.py`（真元数据库 + extractor 桩）：开关那两向各一条（打开才第一次有写入点 / 默认配置下语句清单为空而作业仍 `success`）、两键真下发且改配置跟着变、超阈值那一列什么都不写且**阈值两侧各摆一列**、单表失败时其余表已写入的值域照常在场（`partial` + `errors[].detail` 的 `schema.table:` 前缀）、采样失败不碰元数据行数（注入前后同一份，钉的是"丢列不丢表"）、同一轮内新采的值域就出现在卡片「取值」段（回**整行文本**逐字对，012 那条老教训：「取值」与列名分开断会放过"取值行来自别的列"）、卡片那一步失败也带不走已提交的值域（逐表事务边界）、方言无这档能力时整步跳过不报错。**live 两条**（各跑一次真库，同 §1.9 表末两行）：真 MySQL 16 条采样 SQL / 7 列值域 / 关掉那一轮两格回 NULL，真 PG 只有 2 列拿到值域、`text` 与数组一律不碰。三条分辨力**都实跑过**（改一处、跑那一族、再改回来，结果记在这里而不是写在预期里）：① 把 `_COLUMN_SYNC_COLS` 里的 `sample_values` 摘掉 → **两条**红：真 MySQL 那条"关掉那一轮抹回 NULL"，外加 `test_单表采样失败_其余表已写入的值域照常在场`——后者红在"失败那张表的值域没被抹掉"，也就是分家的那个形状；② 把 `errors[].code` 从 `sample_failed` 改成复用 `embed_failed` → 只有那一条失败隔离红（`detail` 的 `schema.table:` 前缀照旧对得上，所以动的确实只有 code 那一格）；③ 把候选筛子的数组分支写松（`_base_name` 见 `[` 也切）→ 红的**只有**纯函数那一条`test_归一值里带数组后缀的不认`，**真 PG 那一发反而没红**：`product_stats_wide.top_keywords`那 40 行数组每行都不同，跨过阈值什么都不写，两格照样是 NULL。所以"数组不进候选"这一档的证据在闸内而不在 live——本片的 live 增量靠的是类型字面（`bool` 的小写、`text` 不进清单），不是这一格 |
| 卡片模板 golden | 快照 | `tests/fixtures/prompts/kb_card__{table}.expected.txt`，`{{ }}` 空白控制要断言（不然 diff 全是空白）；覆盖：无注释表、60 列宽表、纯视图、含 enum 列、无 PK 表、中文/反引号/`a b` 空格表名（转义必须可见）。as-built(0008)：宽表的第 2/3 段各多一份快照，命名 `kb_card__{table}__seq{n}.expected.txt`（原模式只有一个 `{table}` 槽位，而一张宽表要出 3 份文本）；快照是**按 §5 模板手写**的，不是从渲染器 dump 的——dump 只能证明"以后没变"，手写才证明"渲染出来的就是文档那一份"。六个场景落 `tests/unit/test_kb_card_golden.py`，空白控制是一条独立断言（首尾裸换行 / 空行 / 行尾空白 / 行首缩进四类各钉一次） |
| `prompt_builder` | 结构断言 | 段落顺序（角色→硬约束→schema→JOIN→术语→示例→问题→输出格式）、"必须带 LIMIT"与"只能引用给定的表"两句恒定存在（as-built(P2-0010)：原写的是"禁止引用未给出表"，与 roadmap §P2 踩坑 ④ 的措辞不一致，而 golden 要锁字面，用户拍板统一成后者）、few-shot 段在预算不足时被**整段**丢弃。<br>as-built(P2-010 双轴审查后补三条)：① **golden 逐字节**锁整段 user 消息（`tests/fixtures/prompts/nl2sql__order_main.expected.txt`，手写不是 dump）；② 预算估算用的**行形状**与 j2 模板渲染出的行**同源**——`_term_line` / `_example_line` 各由一条"手写期望行 == 渲染在场 == 估算式产出"的三段断言钉住，改模板改不出漂移；③ ≥0.8 门槛的辖区**只是【可 JOIN】清单**，卡片【可关联】行里的同一条低置信边照旧在场（口径见 architecture §5.3）；边按**起点**筛不按终点筛，目标表被裁掉时标题那句"不要写进 SQL"就是唯一防线 |
| `pipeline` 编排 | 接缝在 `pipeline.ask()`，LLM 走 respx、检索走 `Retriever` Protocol 的桩 | as-built(P2-012)：本行由 012 新增。① 顺序：检索 → 组装 → 生成 → 守卫 → 执行，**守卫在 executor 被调用之前**（spy 断言"守卫拒绝时 executor 一次都没进"，不是看日志）；② 早退三类各留一行 `chat_messages`：`NO_SCHEMA_FOUND` / 畸形返回 / `sql_guard_rejected`，且都不落 CSV；③ 素材齐料：`build_prompt` 收到的 `token_budget`/`few_shot_budget` 来自 `Settings`（010 的硬接线义务，漏传就是静默不裁切，用例直接断言装配点传给 `build_prompt` 的关键字参数值）；④ 畸形返回四档：围栏与前后附解释**能恢复**出同一条 SQL、截断**不恢复**且抬 `llm_bad_response`、`clarify` 非空不执行；⑤ 每步耗时在场（roadmap P2 踩坑 ② 那句"卡在哪一步"的可观测面）；⑥ **⑦ 的入参键集合被钉死**（多一个键就是调用方能传进来的一个入参，`row_limit`/`max_cell_chars`/`result_dir` 一旦被请求体摸到，L4/L3 口径与 ADR-0004 同时作废），`password` 断的是 `decrypt_secret` 那条线；⑦ **跨源只取相关度第一名的那个源**，断的是"补卡片的请求只带同源 uid"（断 prompt 里没它是桩造的假象——桩对任何 uid 都回同一张表）。<br>as-built(P2-012 施工后)：用例落点**不是工单原写的 `tests/unit/test_pipeline.py`**，而是 `tests/integration/test_pipeline_orchestration.py`（`pg` marker）——早退三类的验收本体是"留下一行 `chat_messages`"，不落库的断言只证明了返回值、没证明留痕。真跑一次 LLM 的那条是 `test_pipeline_live.py`（`live` marker）。**当时已知的洞**：模型按 ④ 模板写了 `LIMIT` 时守卫不补 `+1` 探针，`truncated` 恒假，本行 ⑤ 的耗时与 ② 的早退都真、只有这一格是死的——**2026-09-29 同日拍板闭环**，守卫改成"钳位 + 探针"（safety §1.2 ⑦ / §9.2 ⑧），撞上限那一支的 `truncated` 活了；模型自己写小于上限的 `LIMIT n` 那一支仍无探针、恒假，是有意的语义。<br>**as-built(P2-013)**：本行 ② 的三类早退补了两件东西。① **守卫拒绝那一支打了真 SQL**——用例 `test_模型硬产出_DROP_时守卫拦下且一次都不进执行器` 让路线回复直接产出 `DROP TABLE orders`，断 `executor` spy **零调用**、`sql_final is None`、违规 code `top_level_not_select`、落库行保留模型 `sql_raw` 原文、且结果目录**空**（没执行就不该有文件）。② **拒答文案有断言**——`no_schema_found` 那一行的 message 必须同时含"补录表注释 / 检查授权 / 同步"三个词（用例断的是这三个**词**，不是整句），CLI 侧另由 `test_demo_ask_print.py` 三条用例钉"每一种拒答都跟一行下一步动作"、"守卫拒绝那步说清了没执行也没落文件"、"无匹配表那步给的是今天真能走的三条动作"（`POST /api/sync/jobs` 而不是界面按钮）；分层本身还有一条反断言——服务端 message 里**不许出现"点此"**（界面话属于 P8 那一层，见 architecture §4.1 的 as-built(P2-013)） |
| L1 检索三件纯函数（as-built(0009)：`query_terms` / `column_hits` / `rank_candidates`，`app/services/nl2sql/retriever.py`） | 手算黄金值，不碰 DB | ① 切词：`订单金额是多少` 只发二字滑窗（7 字 > 整段阈值 4）、`订单金额` 整段+滑窗都发、`pay_amount` 出整段与两个子词、单字段被丢；② 命中：同一列被两个词命中算两行理由但只算**一列**，比对字段按 `column_name → comment_raw → comment_zh` 顺序各查一次，判定用"子串 + casefold"以与 SQL 侧的 `ILIKE '%term%'` 严格同构；③ 排序：主键**命中的不同词数**、次键命中列数、`table_uid` 收尾（architecture §5.1 那条 as-built 注），同表多卡 `boost=0.05×(n-1)` 只进 `score_kw` 不进排序键；④ `k` 是名额上限；⑤ 零命中返回 `[]` 而不是猜一张表（阶梯 L4 的判据，013 要用） |
| 向量路的 SQL 形状与实现切换缝（as-built(P4-031)：`hnsw_tuning_statement` / `vector_recall_statement` / `_retriever_for`，仍在 `app/services/nl2sql/retriever.py`） | 不连库钉**形状**、`pg` 那一路钉**行为**、外加一条缝的机械检查——三份各有理由 | **为什么形状要单独钉**（与 029 的 `test_sync_embed_sql.py` 同一条理由）：`pg` 那一路看得见"召回对不对"，看不见"顺手漏了一格谓词"，而向量路漏掉 `index_profile_id` 照样返回结果，只是新旧两套档案的向量混进同一个 `ORDER BY`（ADR-0012 立论要防的正是它）——形状错了但结果看着正常，正是这一族 bug 唯一能不靠读代码抓住的时刻。形状落 `tests/unit/test_retriever_vector_sql.py`（7 个函数 / 8 条收集，其中一条是参数化）：① 距离算子排在**档案那一维的那一列**上、另一根列在 SQL 里一字不现（1536/1024 两档参数化）；② `ORDER BY` 是距离升序且选回的原料够 `_vector_table` 折成表；③ `index_profile_id = :p` 与 `datasource_id IN (可见源)` 两格谓词都在 WHERE（决策 ⑨/ADR-0008 的落点）；④ `<列> IS NOT NULL`（ADR-0003「卡片先于向量」的读侧一半——PG 把 NULL 排在升序**末尾**，少了这一格"还没灌向量"就伪装成"最不相关"）；⑤ `LIMIT` 的值就是请求给的 `top_vector`；⑥ `ef_search` 以 `set_config(…, true)` 下发且是**绑定值而不是拼接**（它的源头是请求体，拼串等于在一条每次检索都发的语句上开注入面）；⑦ `iterative_scan` 与 ef 在**同一条**语句里（分开发就是"设了 ef 没设扫描模式"那种半生效，而 SQL 文本上看不出区别）。行为落 `tests/integration/test_kb_search_hybrid_pg.py`（14 条——031 交 12 条、032 在同一个文件里补两条，本轮 `--collect-only` 实数；靶是 028 的闸内替身：真进程真 socket），向量名次由用例**摆出来**（`q / one-hot / -q` → 余弦距离严格 `0<中<2`）、融合次序手算、`SET LOCAL` 与召回同事务由"四次 `SHOW` 夹一次 `search()` 且全在同一条连接"钉。缝落 `tests/unit/test_retriever_seam.py`（3 条，验收 5）：两条实现的签名与协议逐字相同、`Hybrid`/`ef_search`/维度字面不出现在调用方（`pipeline.py` 与 `search_preview()`）、全仓构造向量化实现的语句只有一处。 |
| `executor`（as-built(P2-011)：`app/services/nl2sql/executor.py`，会话级只读 + 超时 + 截断 + csv 落盘） | 离线纯函数手算 + live 真连演示库 | 离线八件（`tests/unit/test_executor_*.py`，共 31 用例）：① `serialize_cell` — Decimal 按 `str` 原样回（`"0.30"` 不变 `"0.3"`）、naive/aware `datetime` 走 `isoformat()`（前者无偏移后缀、后者保留 `+08:00`）、`date` 走 `YYYY-MM-DD`、`time` 走 `isoformat()`（TIME 列 → `"14:30:00"`）、`timedelta` 按 **MySQL TIME 字面量** `[-]H:MM:SS` 手工拆（`1:00:00`/`-1:00:00`/`30:00:00`/`0:00:45`——**不是** `str(timedelta)` 的 Python 规范形 `"-1 day, 23:00:00"`，否则 §3 第 10 步的逐位对数对不上）、`bytes` 分 `<binary 3B>` 与 `<binary 1.2KB>` 两档、超 `max_cell_chars` 的 str 截断、int/float/bool/None 原样透传；② `split_truncated` — 1001 行 & `row_limit=1000` → `truncated=True` 且末行为探针被丢、500/1000/`[]` 三种边界都不截断；③ `new_run_id` + `result_csv_path` — 形状 `[A-Za-z0-9]+`（生成器与校验器**共用一份正则**，防止"生成的名字过不了自己的门"），越界 run_id（`../etc/passwd`、`/abs/x`、`a\x00b`、`a/b`、`..`、`""`）一律 `ValueError`；④ `write_result_csv` — `utf-8-sig` BOM 头幂等回读、自动建父目录、逗号+引号 roundtrip；⑤ `enforce_readonly_grants` — SELECT-only 通过、`INSERT/UPDATE/DELETE ON ai_web_demo.*` 或 `ALL PRIVILEGES ON *.*` 抛 `ReadonlyCapabilityMissing`；⑥ `classify_source_error` — errno 3024/`"57014"` 翻成 `QueryTimeout`（detail 必须同时含 `timeout_ms` 数值与来源键名），非超时的 1146 原样 re-raise；⑦ `session_statements` — MySQL 顺序 `READ ONLY` 必须**第一位**、`wait_timeout` 用 `timeout_ms//1000+10`（15000→25），PG 走 `SET statement_timeout = '<n>ms'`；⑧ `resolve_timeout`（双轴审查收口新增）— 列上有值走 `data_sources.timeout_ms`、列上缺值落 `AIWEB_QUERY__TIMEOUT_MS` 且值必须 >0；**来源判定不在调用方**（`execute_readonly` 不收这两个入参）；⑨ **`resolve_result_file`**（as-built(P2-013)，下载侧那半边，`tests/unit/test_executor_path.py`）——三道门（名字不合法 / realpath 逃出目录 / 不是文件）塌成**同一档** `ResultFileUnavailable`，用例直接断 `{(code, message, status_code)}` **集合只有一个元素**且 `detail is None`（三种失败对外逐字相同，任何差别都是"run_id 是否存在"的 oracle），四个入参**逐字对上三道门**。形状门**复用写入侧的 `result_csv_path`** 而不是另写一份正则，它的 `ValueError` 在这里被翻掉——不翻就是客户端输入打出 500。门②（realpath 逃逸）**在本机真跑**：`os.symlink` 要特权（winerror 1314）但 **junction 不要**，而守卫判的是 `resolve()` 后的落点、与链接类型无关——`test_目录里的链接指向外面时拒绝` 用 junction 造逃逸，并断服务端日志出现 `reason=realpath 逃出目录`（异常种类分不出门②与门③，判定顺序被换掉时用例照样绿，所以要钉日志）；`test_结果目录自己是指向别处的链接时照常放行` 钉反面（目录级链接不算逃逸，运维挪结果盘的正当做法）。用例先试 `os.symlink` 再退 junction，两者都建不出来才 skip——skip 只可能意味着这台机器造不出该文件系统对象。**仍未证明的只剩一格**：指向**文件**的符号链接这一具体形态本机复现不出来（缺的是构造手段，不是代码分支），见 safety §7 的 as-built。<br>live 四条（`tests/integration/test_execute_live.py`，全 `pytest.mark.live`）：① 只读查询跑通 + Decimal/DATETIME 序列化 + csv 落在服务端拼的 `tmp_path` 内（不在 `data/results/`）；② `row_limit=5` 时 `truncated=True`、csv 恰好 6 行（1 表头 + 5 数据）；③ **引擎级写拒绝 ≠ 应用级写拒绝分开断言**（验收 ①）——应用侧 `check("update ...")` 抛 `SqlGuardError`，绕过守卫直喂 `execute_readonly` 时源库自己拒：`DBAPIError.orig.args[0] in (1792, 1142)`，**刻意不收 1046**（那说明是连接没选库的配置错、不是"写被拦"，混进来这条就假过）；④ **超时真中断**（验收 ②）——`select sleep(1) from order_item limit 5` 每行都睡 1 秒踩行边界（`select sleep(2)` 单行不响应，`count(*)` 也只吐一行；这是 MySQL `MAX_EXECUTION_TIME` 的坑，测试 docstring 已钉），命中 3024 → `QueryTimeout` 且 detail 含 `data_sources.timeout_ms` + `300`（键名由执行器从行上自己判出，不是用例传的） |
| `sync_job_event` → SSE 那一条链（as-built(P3-017)：`app/core/sse.py` 的 `sse_format` / `progress_frame` / `stream_job_events` + `job_queue.subscribe`） | 读侧**只碰元数据库**，不碰源库；写侧与读侧分两处钉，因为它们的失败模式完全不同（写侧错在"帧与库不符"，读侧错在"攒到底才吐"） | ① **帧形状**：带游标的帧是 `id: <seq>\nevent: progress\ndata: {…}\n\n`，`id` 在**第一行**（`Last-Event-ID` 靠它，顺序写反了浏览器存不到游标）；流的第一个块是 `retry: 3000`，收尾是 `event: done` 且**没有 `id`**。负载在 `progress_frame` 里**摊平一次**（`{stage, phase, done, total, base_table, view, cards, payload}`），端点里再展一遍就会出现"库里改了帧没改"的分叉。② **游标补读**：`read_events` 只有 `WHERE job_id=:j AND seq>:c ORDER BY seq` 一个形状；用例先读到 seq=2、断开、带 `?cursor=2` 与 `Last-Event-ID: 2` 各重连一次，两边都从 seq=3 起（一条不丢也不重），参数与头冲突时**查询参数赢**，两个都给不成数字时**回全量**而不是 400。这一条是"选事件表而不是纯推 NOTIFY"的全部理由。③ **心跳**：常量 `PING_INTERVAL_S == 15.0` 单独钉一次（它是文档承诺的数字，跟着它断等待时长等于断"它不小于自己"），行为侧把生成器的 `ping_after_s` 设成 0.02 逼出三帧 `: ping\n\n`，不真等 15s。④ **叫醒**：`subscribe` 用的是 `asyncio.Event`（去重语义免费：一个作业被叫醒十次和一次要做的事完全相同），且必须挂在**一条专用连接**上——会话自己那条连接会被读循环每轮的 `rollback()` 归还，池化下交给下一个借用者、NullPool 下直接关掉，两种结局都是"进度永远不动"且一条都不报错。引擎从 `session.bind` 取而不是 `get_bind()`（后者给的是同步 `Engine`，对它的 `.connect()` 抛 `MissingGreenlet`）。⑤ **终局判据是 `sync_jobs.finished_at` 那一格有没有被点上**，不是 `status`：终局三个值在 `schemas/sync.py` 已有一份，读侧再抄一份就是"两处规则各自漂移"的老路（010 那族教训）。⑥ **会话在流里开、在流里关**（`get_stream_sessionmaker` 给的是**工厂**不是会话：FastAPI 会在 `StreamingResponse` 开始迭代之前就关掉 yield 型依赖，端点里再 `async with` 一次是唯一活法，测试也必须覆盖这个依赖）。⑦ **真进费用例**（`test_sync_events_live.py`，`live`）：三个真进程（API + worker + pytest 这个"盯进度条的人"），顺序是**先入队 → 再开流 → 最后才放 worker 出门**，拿到首帧那一刻**当场**回库读 `finished_at` 断它还是 NULL——"首帧在作业结束之前到达"这句话在 `httpx.ASGITransport` 上没有对应的事实（那个传输把 body 片段攒进 list 才返回，`resp.is_closed` 恒真），所以这一条必须走真 HTTP。用例落点：`tests/unit/test_sync_stage_vocabulary.py`（4 条，映射 9 值逐条写死）+ `tests/unit/test_sync_event_ddl.py`（6 条）+ `tests/integration/test_sync_events_pg.py`（5 条，写侧）+ `tests/integration/test_sync_events_sse_pg.py`（6 条，读侧）+ 真进程那 1 条。演示库上的分母实录 `total=10`（9 表 + 1 视图，不是 `SHOW FULL TABLES` 的 11） |
| `job_queue` 两条语句的**形状**（as-built(P3-016)：`app/services/job_queue.py` 的 `enqueue_stmt` / `claim_stmt`） | 编译成 PG 方言文本比对，**不执行**（与 `test_sync_upsert_sql.py` 同一条理由：进程分离的三条口径——互斥靠 `ux_sync_running`、claim 靠**条件更新行数**、排队中的 pending 也算未结束——全是"语句长什么样"的事，写错一个谓词在真库上照样可能绿。`claim` 少外层那个 `status='pending'` 就是两个 worker 同时抽同一个源，而这一格要到 019 的僵尸回收真跑起来才看得见） | ① 入队只写 `pending`：`INSERT` 里不许出现 `running`，也不许顺手把 `started_at` / `heartbeat_at` 点上（那两列是 claim 的钟，也是"响应返回时这一轮还没开始跑"的可断言证据）；② claim 两处条件缺一不可：`FOR UPDATE SKIP LOCKED`（多 worker 各拿一个、谁都不等谁）+ 外层 `AND status='pending' RETURNING`（行数才是"我抢到了"的判据），且终局那几列（`finished_at`/`counters`）不许出现在这里。行为侧的七条在 `tests/integration/test_sync_enqueue_pg.py`（202/409/第二 worker 空手/源删后级联带走 job 行/终局三样/入队顺序） |
| `run_worker` 一轮心跳的三条语句（as-built(P3-018)：`scripts/run_worker.py` 的 `heartbeat_update_stmt` / `reclaim_update_stmt` / `retention_delete_stmt`） | 同一套手法：编译成 PG 方言文本比对，**不执行**。三条口径全是"语句长什么样"的事——心跳只动 `heartbeat_at` 那一格、回收往 `errors`（`jsonb`，**没有 `error` 这一列**）里**追加**、保留期只删 `sync_job_event`——写错一个谓词或把追加写成覆盖，在真库上照样可能绿（`tests/unit/test_worker_reclaim.py`，7 条） | ① 刷钟那条里 `finished_at`/`errors`/`failed` 一个字都不许出现（它是"我还活着"的声明不是终局），且带 `status IN ('pending','running')` 守卫：收尾先落定时这一句必须打不中；② 回收那条逐字对上 §6 表格原句——`errors = (errors || CAST('[{"code":"reclaimed","detail":"heartbeat 超时"}]' AS JSONB))`、`heartbeat_at < now() - make_interval(secs => N)`、`RETURNING id` 三者缺一不可，条目常量 `RECLAIM_ENTRY_JSON` 直接抄文档字面量（`json.loads` 比回去只能证明序列化没变形，证明不了那两个词没被改写）；③ **阈值是入参不是字面量**：换 42 重编译，文本里必须出现 42 且不出现 180；④ 保留期那条编译文本里只许出现 `sync_job_event`，出现 `sync_jobs` 就是灭迹。⑤ **三个键各喂给哪个参数**：`cadence(settings)` 是那条映射的唯一住处（`loop()` 里不出现第二个 `settings.extract.*`），用例拿三个互不相同的数（11/181/31）断它——这条是补上来的：把 `interval_s` 与 `stale_seconds` 互换后跑完整轮，**只有这一条红**（真效果是刷钟 181s、回收阈值 11s，正在跑的作业被自己的 worker 判成僵尸）。⑥ **阈值不是整数就当场抛**：`180.7` / `True` / `"7; DROP TABLE x"` / `None` 四种都从**公共** builder 驱动，期望 `(TypeError, ValueError)`；同一用例再断 `180` 正常编译，否则"全拒"和"没生效"分不开。理由是实测：`int()` 式闸门会把 180.7 悄悄截成 180，而 `make_interval` 那条语句的全部语义就是"差多少秒算僵尸"。行为侧五条在 `tests/integration/test_worker_heartbeat_pg.py`：主事务未提交时换连接已可见、一轮循环改判 + 补终局事件、NOTIFY 丢失时最迟一个心跳周期仍读到、179s 不动 / 181s 判失败 / `pending@NULL` 不杀、保留期删 31 天那行且 `data/results/` 清单快照逐字节相同 |
| MySQL 读侧的**发送切片**（as-built(P3-020)：`app/extractor/mysql.py` 的 `slice_names` + `collect()` 的批循环 + `SourceManifest.batches`） | 切片是纯函数、批是编排，两层分开钉：`collect` 的批语义钉在**假连接**上（`_rows` 是方言层唯一的 I/O 出口，同 §2.1 `test_extract_mysql_client` 那条理由），真库那一份钉在拦下来的语句清单上。为什么必须两层：假件看得见"第 k 条昂贵查询收到哪几个表名"，真库只看得见条数与顺序，而这两件都不是"总共发了几条" | 单测 11 条在 `tests/unit/test_extract_mysql_batching.py`：① 验收 2 那三个边界各一条（整除无空末批 / 末批只剩 1 张 / `batch>N` 退化成一批，外加空名单回 `[]` 而不是 `[[]]`——`collect` 靠"没有表就不发 C/D/E"躲开 `IN ()` 这个语法错误）；② `batch_size` 非正整数当场抛（`0` 会让切片要么死循环要么空清单）；③ 三条昂贵查询各 4 发、**每发各自的名单**——那四刀是手写的字面量，不写 `slice_names(VISIBLE, 3)`：用被测的那把刀去算刀口，红了也只是"刀和期望一起改错"；④ 反注入两条一起断（占位符 `:tbl_0..n` 与这一批的表名**逐一对应**，且语句文本里一个真表名都不出现——只断前者的话"`IN (:` + 拼接"那种形状照样过）；⑤ 退化路径与分批前同形（默认 200 在 10 个对象上必须退回"一 schema 四条"，防"分批把自己变成永远多发"）；⑥ 分批不改归并结果（批大小 3 与 200 抽出的列/索引逐个相同）；⑦ `MAX_TABLES` 仍在昂贵查询**之前**判（断的是 IS 语句**条数**==1，不是错误码——把门槛挪到 C/D/E 之后也照样抛同一个错）；⑧ 批间隔用耗时下限钉（120ms×3 个间隔 → `>=0.36`，并留 `<0.6` 的上限排除"每批之前都睡"）。live 两条在 `tests/integration/test_extract_mysql_live_batches.py`（验收 1/3/4/5/7）：批名单同时出现在事件负载与那三条昂贵查询的 `IN` 清单里，两边逐批相等；间隔 250ms 时 4 批之间三个间隙各 `>=0.25s`（实测同一份源：间隔 0 那一轮 `duration=1125ms`、间隔 250 那一轮 `1985ms`，差出来的就是那三次让气）。名单**不硬写"第一批是哪三张"**——§8.1 B 没有 `ORDER BY`，交付顺序不是源库的承诺；断的是每批的成员（批大小逐个钉 3/3/3/1、批间不重叠、并起来正好是 §1 那份 10 个对象的名单）。**验收 5 比的是另一次真跑而不是 008 的 golden**——那七份快照喂的是手工摆出来的 `meta_*` 输入（见上面卡片 golden 行），拿它对演示库等于比两件本来不该相等的事；做法是先按 200 跑一轮、再按 3 跑一轮，比五张 `meta_*` 行数与全部卡片 `text_md` 逐字符，只放行 `counters.batches` 那一格变化。**同一轮还比索引那条链的形状**（验收 7"P3 只补'分批后每批都还读到'那一格"）：两侧各取一份按 `(表名, 索引名, SEQ_IN_INDEX)` 排序的 `(列名, SUB_PART, cardinality 是否非空)` 清单逐位相等，再对分批那一次断每一行 `cardinality` 都非空、且全库唯一那处前缀索引 `product.name(32)` 仍在场。比"非空"而不比**数值**：`CARDINALITY` 是 InnoDB 的采样估算，两次真跑之间它可以合法地变，把"分批没改变结果"钉在它上面就变成看运气发红。这跟 007 那条 `test_sync_live.py::test_验收2_cardinality_与_sub_part_真的有非空值` 不互替——那一条走的是默认批大小（10 个对象一批装完），证的是链通着；"切成四批后每批还读得到"只有走分批那条路才答得出。实测变异：D 条投影换成 `NULL AS CARDINALITY, NULL AS SUB_PART` 后这条 live 用例红在 `has_cardinality` 那一行（007 那条同步变红，两个靶心各自独立）。**两个键都走真配置通道**（`monkeypatch.setenv` + `get_settings.cache_clear()`，同 021 的 `max_tables_one`）：桩掉 `get_settings` 的话"`run_sync` 有没有把配置传给方言"就永远验不到。实测过——把 `sync_service.py:1106` 那行换成硬编码 `200`：`test_sync_events_pg.py` 只红 `test_分批的两个配置键真有读取点_每批的表名随_tables_帧交回` 那一条，live 两条**一起红**（`counters.batches` 变成 1），而方言层那 11 条单测全绿——它们自己传 `batch_size`，本来就不该替编排层背书 |
| PG 抽取器的**两半**（as-built(P3-024)：不连库那半是 `app/extractor/postgres.py` 的五条 §8.2 SQL 与行→`Raw*` 映射，加上 `sync_service` 的方言驱动表 `_DIALECTS`；live 那半是 §1.6 那份对账清单） | SQL 文本与映射钉在**假连接**上（`_rows` 仍是方言层唯一的 I/O 出口，同上面 020 那行的理由），501 分发与"列写法跟着 kind"钉在纯函数 + 真 `run_sync` 上 | **30 条不连库单测分两张**：`tests/unit/test_extract_pg_map.py` 14 条吃 §8.2 那条查询**自己的别名**当行键（假件与真 SQL 同形才有意义），钉的是 serial 与 identity 分得开（`default_value` 是 `nextval(...)` 那一串）、`_text[]`/`numeric(10,2)[]` 走 023 的归一而 `raw_data_type` 留原文、`modifiers` 白名单只认 `varchar`/`bpchar`/`numeric` 且数组一律 `NULL`、表达式索引那一位的 `column_name` 是 `NULL`（§8.2 D 的分支）、视图 `last_analyze_at` 恒 `NULL` 而表取 `last_analyze`/`last_autoanalyze` **较晚者**（§2.4 注④ 的 PG 半边）、`idx_scan` 只进 `cardinality_hint` 不进 `cardinality`（单位不同）。`tests/unit/test_extract_pg_client.py` 16 条把编排钉在同一颗桩上：`probe` 的三值 + **只有 42704 才降级**（`OperationalError` 且 `sqlstate is None` 不许被吞——那是连不上，不是版本不支持）、`discover` 排除三个系统 schema 与 `pg\_temp%`/`pg\_toast%`、四条查询的发出顺序 `pg_class → pg_index → pg_attribute → pg_constraint`、空范围**不发 `IN ()`**、`MAX_TABLES` 仍在昂贵查询之前判且三条出路原文到达、PG 侧永不产出 `CHARSET_SUSPECT`（那一格的原料是 MySQL 的会话变量）、020 的批形状在 PG 上同形（10 对象/批 3 → 4 批、昂贵查询 12 发、`:tbl_N` 与名单逐一对应、表名不拼进文本）、以及"方言层不许发写语句"——那条用**词边界正则**而不是子串禁（`AS ON_UPDATE` 里的 `ON ` 会被子串 ban 误杀，误杀一次就没人再敢加别名）。**驱动表 10 条**在 `tests/unit/test_sync_dialect_dispatch.py`（**数目订正 as-built(P4-034)**：这张文件今天是 **7 个函数 / 12 条收集**——033 在里面加了 `test_采样能力位与工厂真产出的那个对象成对` 那条参数化用例，本行原文的"10 条"写的是 024 那一刻的数，一字未改地留着只为了让它别变成第二个"文档说的数没人核过"的对象），钉工单 024 验收 2 的两面：`kind='postgres'` 不再命中 `NotImplementedSource`（只构造不连接，所以演示源没建时这条不会 skip——skip 掉的正好是要钉的那一句），而 `oracle`/`mssql`/`MySQL`/`""` 仍抛且 `code=='not_implemented'`（不许顺手放宽）；另有一条配对断言，把 `table_scope_filter(ds, column=该格的 scope_column)` 渲染出的片段拿去断它**逐字出现在该方言的 B 查询里**（构造器由测试自己按 kind 查表，不从 `_DIALECTS` 拿——从被测那张表读出的构造器去验同一张表，红了只是两格一起改错）。**接线那一条是集成用例**：`test_sync_pg.py::test_postgres_源的范围条件按_pg_的列写法下发`——`kind='postgres'` 的源跑真 `run_sync`，形状桩件把收到的 `table_sql` 原样记下（桩件自己不渲染口径）。实测变异：把编排里那一行换回硬编码 `t.table_name` 之后**只有这一条红**，上面那三张全绿（14 + 16 + 驱动表那张，本轮收集数 42 条；原文写"40 条"是 024 那一刻的三张之和——它们都不经过 `run_sync`），红了的内容是"PG 源带着 MySQL 的别名出发了"，而那一句本来要等源库报 42703/1054 才看得见。**as-built(P3-024 live 半边)：那条 SQL 已经在真 `ai_web_demo_pg` 上跑过**——`tests/integration/test_extract_pg_live.py` 四条（验收 1/3/4/5），走真 `POST /api/datasources` → `POST /api/sync/jobs`（202）→ worker 循环体 → 回读元数据库，源库与账号都是真的，逐字对账清单与"证到哪一格"写在 §1.6/§1.6.1；仍没证到的那一组（枚举数组形状、`relkind` 的 `'p'/'m'/'f'`、④ 的"两枚时间戳同时在场时取较晚那枚"、③ 的"每批重取 FK 会不会落两次"）**逐条只列在 metadata-model §8.2 末注"证到哪一步"那一份里**，本行不复述清单以免两处各漂一次。live 另钉到一个**缺口**而不是未证项：表达式索引的文本有列可存（§2.4 `meta_index.funcdef`），但 `RawIndexColumn` 不搬它，所以当前恒空。live 抓出两个**不在 PG 抽取器里**的缺陷并已修：① `inferred_rows` 把表 id 的查找键写死成 `_key("", …)`（MySQL 的 catalog 恒空串惯例），PG 侧每条推断边都查不到而**静默丢弃**（`relations_inferred` 报 0），修法是让 `InferredRelation` 带上它本来就该带的 `catalog_name`，`tests/unit/test_sync_rows.py` 补一条纯函数用例钉住（变异验过：换回 `""` 只有这条红）；② `meta_column.enum_values` 落的不是 SQL NULL 而是 JSON `null`（SQLAlchemy 的 JSON 类型默认 `none_as_null=False`，把 Python `None` 序列成 `'null'::jsonb`，于是 `IS NOT NULL` 成立、82 列全都"有枚举值域"），修法是 `enum_values`/`sample_values` 两格显式 `JSONB(none_as_null=True)`——只改绑定值不动 DDL，而 `_COLUMN_SYNC_COLS` 是整列覆盖，重跑一次同步就自清。**验收 6（MySQL 侧回归不变）在两个修复之后随整轮闸重跑**：被搬动的 `slice_names`/`in_list`/`apply_index_flags` 那三样正对着 MySQL 链，细节记在 roadmap §P3 验收 6 的 as-built |
| `sync_jobs.progress` 那一格（as-built(P3-025)：`app/services/sync_service.py` 的 `progress_of()`，唯一调用点在 `_set_phase` 写事件帧的同一条 `UPDATE`） | 纯函数手算 + 一条真链路 | 这是 §2.5 那列 `numeric(5,2)` 自 0004 建表（`alembic/versions/0004_meta.py:65`）以来**第一次**有写入点。三条口径各钉一次：① 只有 `{done,total}` 参与折算，`base_table`/`view`/`cards` 是三本不同的账，把它们算进百分比会得到一个"看着像进度"的数；② `total<=0` **回 None**，调用方据此**不写那一格**——discover 之前分母还没定出来，写 `0.0` 等于替 `DEFAULT 0` 宣布"我确定现在是 0%"，而真话是"还不知道"（这一支最容易写错成"回 0.0"，因为两种写法在终局那一帧上都显示 100）；③ 值的来源是**事件帧里那一份** counters，不是另算一次——同一个数有两条产生路径就是 §2.8 决策 3（序列真相在 `sync_job_event`）要堵的东西。用例 `tests/unit/test_sync_progress.py`；真效果在 §1.7.1 的两处实录里：`timeline` 段末行 `progress=100.00` 与 `killbatch` 段打断那一行的 `progress=0.00`（那一轮 `heartbeat_at` 有了、`started_at` 有了，而 discover 还没交回分母，正是 ② 那一支的真实现场） |
| `card_build` 的**档案守卫**（as-built(P4-027)：`app/services/sync_service.py` 的 `card_build_guard()`，唯一调用点在 `run_sync` 的 card_build 段；读侧唯一入口 `kb_service.read_active_profile()`） | 判决本身是纯函数（吃 `ActiveProfile \| None`，回 `{code, detail}` 或一份档案），所以两处守卫各钉一次单测；**"这一轮到底有没有卡"的真效果只能走整条链**（真入队 + 真 worker + 换连接回库），因为要断的是库里的行数而不是返回值 | 三条各钉一件事：① **没有 active 档案时同步 `success` 且零卡片**（`test_没有_active_档案时同步成功且零卡片_档案表一行都没多`）——一次断三件：`status=='success'`、`counters.cards==0`、`SELECT count(*) FROM kb_index_profile` 前后相同。第三件才是本条存在的理由：前两件在旧形状（"读不到就按 settings 建一套"）里照样绿，而"不许留那条兜底"正是本片要关的后门；② 差哪一格就不写卡，**两个方向各一条**（`@pytest.mark.parametrize` 的 改维度 / 改模型），且**被改的那一格在配置这一侧**（`monkeypatch.setenv` + `get_settings.cache_clear()`，档案行仍是夹具钉住的那一套）——这是验收 2 的字面动作"改 `AIWEB_EMBEDDING__DIMENSION` 再跑一次同步"；漂移造在档案行上虽然走同一个比对函数，却钉不到"这台机器改了 `.env` 之后同步看不看它"，而那正是后门原来藏身的地方（worker 在本进程里跑所以 env 改动对它可见，换成真子进程这条就不成立）。detail 必须**两个值都在场**（配置的与档案的）并点出下一步 `POST /api/kb/reindex`——只说"不一致"等于让运维再跑一次 SQL 去查是哪个键歪了；③ 守卫**只长在 card_build 那一步**：`counters.tables`/`columns` 照常有值、`phase` 序列里 `card_build`/`embed`/`done` 三帧都在场，把守卫挪到整段收尾之前（或干脆判成 `failed`）都在这里红。读侧那半边同一片改：`retriever._active_profile_id` 改成从 `read_active_profile` 投影，夹具 `active_profile` 与守卫用例共用**同一个** `conftest.insert_active_profile()`（`provider` 字面与 `name` 三段拼法只住那一处，029/031 改档案行形状时是一处不是一族），且它**只写 `status`、故意不写 `is_active`**，于是"读侧还在按那把镜像列筛"表现成"召回全空"而不是一个数字差——这一条红过才改。用例落点：`tests/unit/test_kb_active_profile.py`（`triplet_drift` 五档：三格全一致 / 各差一格 / 三格全差且顺序固定）+ `tests/unit/test_sync_card_build_guard.py`（判决四档：一致 / 无 active / 差维度 / 差模型，再加"不缓存结论"那条）+ `tests/integration/test_kb_active_profile_pg.py`（读入口五条，含"只有 draft/retired 时回 None"与"读的尺子是 `status` 不是那把镜像列"）+ `test_sync_card_profile_guard_pg.py`（上面三件事，其中②的两个方向各一条 → 共 5 条用例）+ `test_kb_cards_profile_source_pg.py`（卡片挂在传入的档案上、构建不再落档案行）+ `test_kb_cards_live.py` 验收 4（一整轮真同步跑完，`status='active'` 仍只有夹具那一行）。验收 4 那半句"文本与 008 golden 逐字符相同"的靶**不在本片新文件里**，而是 019 那条既有用例 `tests/integration/test_sync_card_isolation_pg.py::test_未注入时写进库的卡片文本与008golden快照逐字符相同`——它挂上 `active_profile` 夹具后照常绿，这才是"来源换了、渲染出来的还是文档那一份"的证据。**"改的是来源不是构造"的反证**：`git diff --name-only -- backend/tests/unit` 只回一个名字——`test_kb_upsert_sql.py`，而那 `+3` 行全在 `test_profile指针与身份键不进set_` 那条的 docstring 里（补一句"来源变了、这条口径没变"，断言一个字没动）；四张 `test_kb_card_*`（golden / identity / rows / text）在 `sync_cards` 签名变化之后**零改动**，一行断言都没跟着动 |
| 向量路的**两只脚**（as-built(P4-028)：生产侧唯一的 `app/services/embedding_client.py` + 闸内替身 `scripts/fake_embedding_server.py`，判据见 ADR-0015） | 纯半边（编请求体 / 读响应体）走单测不碰网络；凡需要客户端**真发一次 HTTP** 的一律 `live`——**没有第三条路**，因为 `EmbeddingClient.__init__` 有意不收 `httpx`/`transport` 参数（`test_构造签名里没有_transport_注入点` 用 `inspect.signature` 钉着那条接缝不存在） | 单测十一条（`tests/unit/test_embedding_client.py`）：① 请求体是 `{model, input:[…]}` 的手写字面量（**单条也发数组**，两条请求形状等于只测到其中一条）；② 归位读 `data[i].index` 而不是数组下标——用例把 `data` 故意倒序摆，按下标取就回成 `[c,a,b]`，那种错在库里两行都合法、只是卡片 A 从此挂着卡片 B 的向量，检索永远指错表且一条日志都没有；③ 维度不符是**一整档专属错误**（`EmbeddingDimensionMismatch`，029 靠它把整颗作业判 `failed`）且文案里期望维度与实收维度两个数都在场；④ 形状坏（缺 `data`、条目不是 `{index,embedding}`、`index` 缺失或非数、向量元素非数值、少回一条）全落 `EmbeddingBadResponse` 而不是 `KeyError`/`JSONDecodeError`——裸异常冒出去是一个没有 envelope 的 500，而"少回一条"若退化成"返回拿到的那几条"，调用方 zip 短对齐时后面的向量会悄悄挂到前面的卡片上；⑤ 重试词表 `{408,429,500,502,503,504}` 逐字钉且**断 400/401 不在场**（替身对未知 model 回 400，被重试就永远拿不到那条可辨识的失败）。live 十二条（**数目订正 as-built(P4-029)：那张文件今天是 13 条**——029 在其中加了
`test_期望宽度由调用方给_客户端不读自己配置里那一格`，钉的是"期望宽度由调用方按档案行给，客户端不许拿
`settings.embedding.dimension` 自己判"；上面列的十二条逐条仍在，一句没动。`tests/integration/test_embedding_server_live.py`，夹具 `fake_embedding` 一条用例一份，`--port 0` 起、读 stdout 的 `LISTENING` 当就绪、结束杀）：确定性三条（同问句两次**逐位**相同 + 换问句不同——只断前一半的话，一个永远回同一个向量的替身照样绿；批量里的一条 == 它单发；**换一个替身进程**还是同一个点——内置 `hash()` 按进程加盐，用它的实现只在前两条绿）；两套宽度按 `model` 名分且期望值由 param 表自己声明（不从响应 `len()` 读回来比，那只能证明"它不小于自己"）；未知 model → `EmbeddingBadRequest` 且文案带出那个名字；维度不符 → 两个数都在；杀进程后 → `EmbeddingUnreachable` 且 `<10s` 秒回（不是等满 60s），同一条用例顺带在死端口上断 `embed([]) == []`（空批**一次 HTTP 都不发**，否则"没有内容要嵌"会报成"端点不通"）；另起一份杀掉后那个端口再没人听（验收 6 的另一半，红的形态是"下一条用例连上了上一条的僵尸"）；**验收 4 的两条**打的是探针不是客户端（`check_env.check_embedding` 自己的 `httpx.post` 与它自己的判据）：`ok` 一支 + 维度比对那一支，后者做过变异——把 `actual != emb.dimension` 换成恒假**只有它红**，那条 fail 分支自 0005 写下以来第一次真跑到。**闸内当时未证的那一格（as-built(P4-029) 已补上）**：028 交付时这里写的是"替身从不回 5xx，
重试/退避的真效果没有端到端证据"，那根拉杆（`--fail-on`）就是 029 为它加的——
`test_单表embed失败时落partial其余表的向量真在场` 让一发真吃满两次退避（`0.5 * 2**attempt`）
再落进那颗作业的 `errors[]`。仍然没证的只剩**真端点那种坏法**：429 之后服务端希望的等待时长
（客户端不吃 `Retry-After`，退避是自己的公式）、网关超时、额度用尽返回的形状 |
| embed 档**真写向量**与 `维度→列名` 那份唯一映射（as-built(P4-029)：`app/services/sync_service.py` 的 `_run_embed_stage()`、`app/services/kb_service.py` 的 `card_vector_update()` / `create_draft_profile()` / `mark_profile_built()`、`app/core/vector_columns.py` 的 `vector_column_for()`，端点是 `POST /api/kb/reindex`） | 三层分开钉，因为三层的失败模式互不相干：**映射与 SQL 形状**是纯函数（不碰 DB），**端点形状与档案出生**走 `pg`（真 HTTP + 真入队 + 换连接回库，但不真发 HTTP 到任何端点），**向量真落列**只能走 `live`（要 028 的替身真起一份）。为什么不给 `run_sync` 加第二个 test-only 注入参数（工单已定口径）：019 的 `card_build_hook` 已经是签名上的代价，逐表失败改从**替身侧**注入 | 单测 **19** 条分两张（**数目订正 as-built(P4-029 双轴审查)：9 + 10**——审查在 `test_sync_embed_sql.py` 里补了一条"落点带的是档案那一行的模型名"，见下面第三处）：`tests/unit/test_vector_columns.py` 钉映射（两格逐字 + 未预置维度**抛**而不是回落到第一列——回落是最像没坏的形状：列宽恰好兼容时落进一批语义无关的数，此后检索返回"看起来正常"的错结果；一条**漂移门**扫 `app/` 与 `alembic/versions/` 断 `embedding_1024` 这个字面只出现在 allowlist 那三个文件里，这是验收 11 那句"全仓 `grep` 只有一处"的机械版本，**它只扫得到第二列那个字面**——`embedding` 与 ORM 属性同形，裸 grep 会在每一处 `card.embedding` 上误报，所以第一维那一半的代偿是 `ALL_VECTOR_COLUMNS` 那份投影 + `test_kb_upsert_sql.py` 里从 ORM 取期望值的"洗全部向量列"；另有一条断映射给出的列名在 `KbCard.__table__` 上真存在）+ `tests/unit/test_sync_embed_sql.py` 钉 UPDATE 形状（`{列, embedded_at, updated_at}` 三格、`text_md`/`search_text` 不在场——embed 档不改文本，文本没变向量才有效；param 两根列各一遍；反证那条比的是**列名集合**而不是子串，因为 `embedding_1024` 本身含 `embedding`，子串检查会自己骗人；另有四条各钉一件本片的口径——`mark_profile_built()` 那条 UPDATE **只动
`built_at` 一格**（`status` 不在射程内，draft→active 是 030 那道门槛的权力）、`_embed_skipped()`
的回落帧只剩"未配置"那一支（`embedding_not_implemented` 那支随本片删除，验收 11 后半）、
"期望宽度由**调用方**给客户端而不是客户端自己读 settings"、以及"`VectorTarget` 必须带 `model`
且它逐字等于档案那一行传进来的那一个"（双轴审查·规格轴补的那条：029 初版只把 `dimensions` 从档案读，
`model` 仍从 settings 拼请求体，于是同一颗作业一半认档案一半认配置）。pg 侧 `tests/integration/test_kb_reindex_pg.py` 九条（202 两键齐 + draft 出生 + **active 整行逐格不动**、embedding 四格没配齐 400 且不建空壳（工单字面写作"三键"）、同三元组第二个源复用同一行 draft、同源连发两次 409 且档案行不翻倍（那把互斥是 `ux_sync_running` 给的，不是代码判重）、配置与 active 一致时**拒绝** reindex（不把线上基准当靶）、撞名撞到 `retired` 也复用、`global` 档不能发这条端点（owner 档那把尺）、`claim` 把 `reindex_profile_id` 随行带回而普通源为 None、卡片全挂在 draft 那一行且终局 `done` 帧的 payload 恰为 `{profile_id}`——验收 2 那句"重放事件流查得到"钉的就是最后这一格）。live 侧 `tests/integration/test_kb_reindex_live.py` **六**条（同上订正：审查补了"请求体问的是档案那一行的模型名"那条），五条期望值各有独立出处（文件头写着这五条）：**逐卡配对**比的是"这张卡存的向量 == 替身对**它自己正文**算出的那个点"，而不是"非空"——只断非空的话，客户端按下标归位那种错法（卡 A 挂卡 B 的向量）照样绿；这条做过变异（三张表的向量轮流挪到下一张表的卡上）→ 两条用例红、共 4 处"不配对"。**旧列不动**比的是该列 `::text` 的**逐行文本**相等，不是"行数没变"——行数不变而值被覆盖正是 ADR-0012 要防的那件事。**`partial` 那格除了字符串还查库**：炸那张 `(1,0)`、另外两张 `(1,1)`，`errors[0].code` 是客户端自己的 `embedding_upstream_error`（500 属可重试档，退避用尽才翻出来），不是新发明的 `embed_failed`。**`/kb/search` 前后比 `items`**，不比顶层 `took_ms`（它是耗时不是名次，比进去只会得到一条永远红的用例）。数值比对口径 `_VECTOR_RTOL=1e-6`：pgvector 的 `vector` 是 **float4**（实测双精度 `0.07753724297456155` 落库回读成 `0.077537246`），挂错的向量与正确点差在 1e-1 量级、差四个数量级，所以这个容差是表示精度不是"差不多就行"——031 的 `<=>` 名次比较在同一个 float4 上做，这条事实写在这里而不是埋在断言里。**写侧不需要注册编解码器**：`pgvector.sqlalchemy.VECTOR.bind_processor` 把 `list[float]` 渲染成 `[v1,v2,…]` 那个字面量，异步驱动因此直接认；读侧解文本 (`json.loads`) 就够，`register_vector` 是 031 读侧第一次要的事。**闸内没有的那一格**（如实）：真端点的语义召回与"销售额命中 `payment_record`/`order_main`"，替身给不了（§1.8 留白那两条） |

配置层的已落地单测：嵌套 `__` 解析、DSN 凭据转义（`u@site` / `p@ss#w/1` → `%40`/`%23`/`%2F`）、
`masked_dsn` 不出明文、embedding 维度 >2000 被拒、部分配置时 `configured=False`、
默认 JWT secret 被标记、本地默认值（`host=127.0.0.1`、`allow_sql_edit=admin`、`sample_distinct=false`
——as-built(P4-033)：这一格原写 `true`。键没有读取点时那个默认值只是文档噪音，033 给了它读取点，
它就变成"每次同步真去源库跑 `GROUP BY`"，于是随 P4 决策 ⑩ 改判 `false`，见 §1.9 与 §6 第 10 条）、
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
     **as-built(P4-028)：这一句的"mock"形态已被 ADR-0015 换掉，只对 LLM 那半边保留。**
     `EmbeddingClient` 的构造签名里**有意没有** `http`/`transport` 参数（有一条单测钉着它），
     所以向量路没有"打桩"这条路——注入点一开，base_url 拼接、超时、非 200、维度不符这四件事
     就再也没有真跑证据。替代形态是闸内一个**真 HTTP 替身**（`scripts/fake_embedding_server.py`，
     只 subprocess 一层），起法与判据见下面 §2.3。
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

### 2.3 闸内伪向量替身：怎么起、怎么把四格指过去（工单 028 / ADR-0015）

**它解决的是"检索的向量路在没有计费端点的机器上也要能真跑"这件事**，不是"语义好"的证明。
替身是一个真 HTTP 服务（标准库 `ThreadingHTTPServer`，无额外依赖），走 OpenAI 兼容的
`POST /v1/embeddings`；向量内容是**字 n-gram 的 blake2b 哈希**（1/2 元、L2 归一），
所以"同输入逐位同输出"成立、而"近邻=语义相近"**不**成立——凡是要证语义的验收，这一格
仍然只能等真端点（见 roadmap §P4 的 as-built 留白）。

```powershell
# 起（前台，Ctrl+C 收；端口换一个没人在听的）
uv run python scripts/fake_embedding_server.py --port 8111
# 指（backend/.env 的四格，键名不变、不新增键）
#   AIWEB_EMBEDDING__BASE_URL=http://127.0.0.1:8111/v1
#   AIWEB_EMBEDDING__API_KEY=***
#   AIWEB_EMBEDDING__MODEL=text-embedding-3-small
#   AIWEB_EMBEDDING__DIMENSION=1536
uv run python scripts/check_env.py   # 应报 embedding.dimension ok，1536 维
```

三个要点：

- **`/v1` 是 base_url 自己带的**。探针与生产客户端都拼 `{base_url}/embeddings`，
  少一段就变成"探针连得上、客户端连不上"那种最难分的错；替身对 `POST /embeddings`
  直接 404，就是为了让这种配错在闸里表现成一次可定位的失败而不是静默换路。
- **`MODEL` 与 `DIMENSION` 必须成对**：替身只认两套——`text-embedding-3-small`=1536、
  `bge-large-zh-v1.5`=1024（第二套是 026 的 `embedding_1024` 那列的靶）。名字不认识回 400
  （不"顺手给个默认宽度"），宽度与配置不符则由 `check_env.py` 那条比对分支报
  "端点返回 N 维，配置是 M 维"——**这一条从今天起有真跑证据**：
  `tests/integration/test_embedding_server_live.py::test_探针那条维度比对分支能真的动一次`
  把它逼红过一次（把比对判据换成恒假，只有这条红）。
- **闸里不需要手工起**。`tests/integration/conftest.py::fake_embedding` 夹具自己
  `--port 0` 起一份、读它 stdout 打印的 `LISTENING <host> <port>` 当就绪信号、用例结束杀干净
  （function 作用域，因为有一条用例专门要"杀掉再连"）。
  需要真客户端的验收全部挂 `live`；纯函数半边（请求体形状、按 `data[i].index` 归位、
  维度不符那一档）在 `tests/unit/test_embedding_client.py`，不占端口。
- **它有一根 5xx 的拉杆**（`--fail-on TEXT`，可重复；含 `TEXT` 的那一发回 500）。
  这是 029 加的形状，为的是工单那条已定口径——**逐表失败注入走替身侧**，不给 `run_sync`
  加第二个 test-only 形参（019 的 `card_build_hook` 已经是签名上的代价）。夹具那一侧对应
  `fake_embedding_with_switches`；子串够用是因为卡片正文里带着 `schema.table`（kb-workflow §5
  的表头），替身因此只认"文本"这一层，不认表。

**证据边界（如实，as-built(P4-029) 改写本段）**：028 交付时这里写的是"客户端的重试/退避那一支
在闸内**没有**端到端证据——替身从不回 5xx"。**这句已经不成立**：上面那根拉杆就是为它加的，
`tests/integration/test_kb_reindex_live.py::test_单表embed失败时落partial其余表的向量真在场`
让替身对一张表回 500，真跑过 `RETRYABLE_STATUS` 那一支的两次退避重试（`_MAX_RETRIES=2` /
`_BACKOFF_BASE_S=0.5`），用尽后由客户端翻成 `EmbeddingUpstreamError`、由编排落进那颗作业的
`errors[]`。所以闸内现在证到的是**机制**（会重试、重试不永远赢、失败按表隔离）。
仍然没证到的只剩**真端点的那一种坏法**：429 之后服务端希望的等待时长（客户端**不吃
`Retry-After`**，退避是自己的 `0.5 * 2**attempt`，与 `llm_client` 同源）、网关超时、额度用尽
之后返回的形状——替身不认识这些，也不该认识（把它写成会回 429 就等于用替身冒充端点）。

## 3. 端到端手测清单（15 步，全绿即 v0 可用）

| # | 动作 | 期望证据 |
|---|---|---|
| 1 | `uv run python scripts/check_env.py` | 无 fatal，vector/pg_trgm/hnsw/schema 全绿；维度一致项通过 |
| 2 | `uv run alembic upgrade head` | `aiweb` schema 下表齐全 + `alembic_version` 在 aiweb 内（不在 public） |
| 3 | `uv run python scripts/seed_admin.py` | 建 admin；**第二次运行幂等且不重置口令**；库里存的是 argon2 hash |
| 4 | 前端登录 → 首登强制改密 | 旧 token 立即失效（as-built 005：只有 `users.token_version`（令牌里的 `tv`），没有 `pwd_ver` 那一种） |
| 5 | 新建数据源 `demo-mysql` → 测试连接 | 返回"连接成功 / 9 表 1 视图 / 只读能力=OK / MySQL 5.7.x / max_execution_time 支持=yes"。要拿到 9/1 必须在**排除表**里填 `_%`（源原生 LIKE 里 `\_` 才是字面下划线）：库里可见的是 11 张，多出来的 `_aiweb_demo_marker`/`_numbers` 这类是脚本内部表，口径见 §1.2 的 as-built 注 |
| 6 | 立即同步 | SSE 进度到 100%；`sync_jobs.status=success`；phase 序列完整。**as-built(P3-016)**：本机从此**两条命令**——`dev.ps1 dev` 起 API，`dev.ps1 worker` 起执行体（`make worker` 同义）。只起一条时这一行的表现是"202 拿到了、那一行永远停在 `pending`、`started_at` 为 NULL"，那是缺消费者不是缺索引；SSE 那一半归 017，此步现在能看的只有库里那一行。**as-built(P3 收口)**：这一行今天整条成立，
逐字实录在 §1.7.1（五帧 `event: progress` + `event: done`，库里 `progress=100.00`——那格从 025 起才有写入点，
此前它一直是 `DEFAULT 0`）|
| 7 | 元数据浏览 → 打开 `order_main` | 字段列表含中文注释；索引含复合索引两列顺序正确；关系显示指向 `customer`/`order_item`；卡片文本可复制 |
| 8 | 知识库检索预览（调参页）输入"各区域每月回款金额" | 返回 `payment_record`/`order_main`/`customer` 三表且带 `vector_rank`/`keyword_rank`/`rrf_score` 三列排名；把 `final_tables` 改成 2 后第三表消失。**as-built(0009)**：这一行是**向量启用后**的完整形态。009 交付的是接口层 `POST /api/kb/search`（没有调参页 UI，也没有 `vector_rank`/`keyword_rank`/`rrf_score` 三列——P2 只有关键词一路，融合排名无从谈起），P2 侧的可人肉验证证据是 `tests/integration/test_kb_search_live.py` 三条 live 用例（问订单总金额 → `order_main` 头名、`payment_record` 进前 5；问客户手机号 → `customer` 头名且理由三元组齐；二次同步后候选不漂移）。调参页 UI 与三列排名归引入向量那一张工单 |
| 9 | Chat 问"2025 年每个渠道的总回款金额，按金额降序取前 5" | SSE 顺序完整；`guard.verdict=pass`；SQL 引用的表 ⊆ 第 8 步命中集合。**as-built(P2-012)**：这一行是**端点与前端就位后**（P8）的形态——012 不开端点（口径见 architecture §4.1 ⑦ 的拍板块）。P2 侧等价证据是命令行示踪弹：`uv run python scripts/demo_ask.py "2025 年每个渠道的总回款金额，按金额降序取前 5"`，它打印 `retrieved_tables / sql_raw / guard_verdict / sql_final / rows / chart_spec / conclusion` 与各步耗时，其中"SQL 引用的表 ⊆ 命中集合"由守卫的 `allowed` 集在链路内强制（越界即 `SqlGuardError`），不是靠人眼比 |
| 10 | **校验 SQL**：把 SQL 复制到 `mysql> ` 手工执行 | 数字与界面结果**逐位一致**（防止 executor 侧类型/时区改写造成"界面好看但数据不对"）；金额字段是 decimal 字符串而不是 `0.30000000000000004` |
| 11 | 结果与图表 | 折线/柱由 `chart_advisor` 选对（时间→折线、类别≤12→柱）；表格横向滚动；结果 1000 行时显示"已截断，仅展示前 1000 行" |
| 12 | 结论卡片 + 反馈"采纳为示例" | `few_shot` 新增行；结论里的数字能在结果表格里找到出处（抽查一处，防模型编数） |
| 13 | 换一个措辞再问（"看下去年各支付通道收了多少钱"） | `retrieval` 帧含 `few_shot_hit=true`，生成 SQL 与第 9 步结构高度相似；`done.latency_ms` 下降 |
| 14 | （反向）member 账号问 `user_activity_log` 之外的未授权表 | 403/无相关数据，且日志无该表名泄露 |
| 15 | （健壮）同步进行中重启服务 → 重启后僵尸 job 被回收、可重新同步 | 日志 + `sync_jobs` 状态变化。**as-built(P3 收口)**：这一行的原句有两处按实现改了口径——① kill 的对象是 **worker**，不是 API（`--reload` 存一次盘就是这条的老形态，ADR-0011 拆进程之后已经不存在这个触发面）；② 回收**不必等重启**，心跳循环每轮扫一次，所以"重启后"只是最快看得见的那条路。逐字实录（真子进程 + 当场 `kill()` + 自愈 + 重新同步 `success`）在 §1.7.1 的 `killbatch` 段 |

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
