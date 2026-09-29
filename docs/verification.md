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

## 2. 测试分层

### 2.1 单元测试重点（纯函数，不碰 DB / 不碰网络）

| 目标 | 断言方式 | 关键用例 |
|---|---|---|
| `token_estimate` | 值域 + 单调性 + 分支（as-built(0008)：本行六条**不由 008 全认领**。008 交付的是 heuristic 纯函数，钉了 ③④ + 五条手算值域用例（`tests/unit/test_token_estimate.py`）。as-built(P2-0010)：①② **在 P2 不做**——用户拍板"prompt 预算沿用 008 的 heuristic 口径，不引 `tiktoken`"，`AIWEB_RETRIEVAL__TOKENIZER` 键与真分词器参照一起推给引入依赖的那一档（roadmap §P4）；⑤⑥ 走的是结果集与 `TOKEN_BUDGET` 裁切，属 011（010 只做卡片段的预算裁切，用的就是这个 heuristic 函数） | ① [P4] 与 `tiktoken.encode` 计数偏差 ≤8%（`cl100k_base` 可用时）；② [P4] 强制走 heuristic 分支时中文/英文/混合三段偏差 ≤25%（宁高估勿低估）；③ 单调：文本变长则估计不减；④ 空串=0；⑤ 超长 JSON 行结果集不炸（011）；⑥ "结果集只喂摘要"路径的预算 ≤`TOKEN_BUDGET`（**as-built(P2-012)：这条改归 012**——它是 ⑨ 结论那一步的输入预算，住在 `pipeline` 而不是 executor；011 交付的是 ⑤ 那一半。**落地形态是"超预算告警、不裁切"**：`result_brief(token_budget=)` 算一遍**选中那份素材**的体量，超了就 `logger.warning`，一个字不删。三条理由：摘要分支比整表**更长**（要开 stats 表加两张头尾表），"装不下就换摘要"会把超预算变成更超预算；摘要再裁就只剩"共 N 行"，那不如让模型看全；语义与 P4 验收 6"被裁的表要记日志、不报错但要可查"同一条。用例三条——典型 60 行摘要 ≤1500（预算不是摆设，1500 用字面量而不是 `Settings`，跟着配置断等于断"它不小于自己"）、超预算仍回整表（不改道）、几百个数值列的摘要超预算时报警且内容一字不缩） |
| RRF 融合 | 手算黄金值 | ① 两路各 `[a,b,c]`/`[c,a,b]`、k=60 → 期望顺序与分数硬编码；② 只有一路有结果时仍参与；③ `keyword_weight=0` 退化为纯向量；④ 并列名次的稳定排序（同分按表名字典序，防抖动）；⑤ 空输入返回 `[]` 不抛 |
| `join_graph` BFS | 图与路径（as-built(P2-0010)：**整行不属于 010**。全仓没有 `join_graph` 实现，010 只渲染候选表之间的直连边；图算法与 architecture §5.3 的边权重/环/多路径歧义/降级四条一起归**工单 014 JOIN 图切片**） | ① 无 FK 但按命名约定推出边、`confidence` 按 §5.3 加权公式算（as-built(P2-0010)：两处口径都改了。常数"=0.7"与公式互斥，已由 010 闭环——0.7 现在是**公式地板**；原举例 `order_item.order_id → order_main.id` 在演示库**推不出来**：`order_id` 的名词是 `order`，库里没有 `order`/`orders` 表（真表叫 `order_main`），命名约定不认"前缀加后缀"的自由变体。库里真实存在的那条推断边是 `user_activity_log.product_id → product.id`（两侧同为 `INT`，实测算得 **1.00**），拿它当例）；② 自环 `parent_id` 不产生 1-hop 自引用；③ hops=2 时最长路径不超 2；④ 环（A→B→C→A）不死循环、路径去重；⑤ 桥表扩展：问 A、D 两表时能拉进 B/C，且 `path=[A,B,C,D]` 顺序可渲染进 prompt；⑥ 不可达时返回"需笛卡尔积"标记而不是硬连 |
| `chart_advisor` | 规则决策表 | 时间+数值→line、类别(NDV≤12)+数值→bar、NDV>12+数值→bar(取 top10 + "其他")、双数值→scatter、单行单值→`kpi`、全 NULL 列→table only、行数 >200→不画图；`EChartsOption` 结构快照（键存在性，不比像素）。as-built(P2-012 开工前拍板)：本行**由 012 认领**（此前 architecture §2 的目录树里挂着 `chart_advisor.py` 却没有任何工单写它，roadmap P8 验收 5 是它的 UI 双证、不是它的出生地）。两处字面改了：① "单行单值→number" 与 architecture §4.2 的 `kpi` 是同一样东西的两个名字，**统一写 `kpi`**（`chart_spec.type` 是要进 golden 的字面，两个名字等于两条对不上的断言）；② 快照对象是**自有形状** `{type,x,series,title}`（metadata-model §2.7），不是 `EChartsOption`——翻译那一层归 P8，理由见 architecture §6.1 的 as-built |
| PG/MySQL 类型归一 | 映射表 | PG `_text[]`/`numeric(10,2)`/`varchar(64)`/`timestamptz`/`jsonb`/`serial`/`generated always`；MySQL `decimal unsigned zerofill`/`enum('a','b')`/`set`/`tinyint(1)`→bool 与否、`datetime(3)`、生成列、`utf8mb4_0900_ai_ci`(8.0) 出现在 5.7 时的容错 |
| 卡片模板 golden | 快照 | `tests/fixtures/prompts/kb_card__{table}.expected.txt`，`{{ }}` 空白控制要断言（不然 diff 全是空白）；覆盖：无注释表、60 列宽表、纯视图、含 enum 列、无 PK 表、中文/反引号/`a b` 空格表名（转义必须可见）。as-built(0008)：宽表的第 2/3 段各多一份快照，命名 `kb_card__{table}__seq{n}.expected.txt`（原模式只有一个 `{table}` 槽位，而一张宽表要出 3 份文本）；快照是**按 §5 模板手写**的，不是从渲染器 dump 的——dump 只能证明"以后没变"，手写才证明"渲染出来的就是文档那一份"。六个场景落 `tests/unit/test_kb_card_golden.py`，空白控制是一条独立断言（首尾裸换行 / 空行 / 行尾空白 / 行首缩进四类各钉一次） |
| `prompt_builder` | 结构断言 | 段落顺序（角色→硬约束→schema→JOIN→术语→示例→问题→输出格式）、"必须带 LIMIT"与"只能引用给定的表"两句恒定存在（as-built(P2-0010)：原写的是"禁止引用未给出表"，与 roadmap §P2 踩坑 ④ 的措辞不一致，而 golden 要锁字面，用户拍板统一成后者）、few-shot 段在预算不足时被**整段**丢弃。<br>as-built(P2-010 双轴审查后补三条)：① **golden 逐字节**锁整段 user 消息（`tests/fixtures/prompts/nl2sql__order_main.expected.txt`，手写不是 dump）；② 预算估算用的**行形状**与 j2 模板渲染出的行**同源**——`_term_line` / `_example_line` 各由一条"手写期望行 == 渲染在场 == 估算式产出"的三段断言钉住，改模板改不出漂移；③ ≥0.8 门槛的辖区**只是【可 JOIN】清单**，卡片【可关联】行里的同一条低置信边照旧在场（口径见 architecture §5.3）；边按**起点**筛不按终点筛，目标表被裁掉时标题那句"不要写进 SQL"就是唯一防线 |
| `pipeline` 编排 | 接缝在 `pipeline.ask()`，LLM 走 respx、检索走 `Retriever` Protocol 的桩 | as-built(P2-012)：本行由 012 新增。① 顺序：检索 → 组装 → 生成 → 守卫 → 执行，**守卫在 executor 被调用之前**（spy 断言"守卫拒绝时 executor 一次都没进"，不是看日志）；② 早退三类各留一行 `chat_messages`：`NO_SCHEMA_FOUND` / 畸形返回 / `sql_guard_rejected`，且都不落 CSV；③ 素材齐料：`build_prompt` 收到的 `token_budget`/`few_shot_budget` 来自 `Settings`（010 的硬接线义务，漏传就是静默不裁切，用例直接断言装配点传给 `build_prompt` 的关键字参数值）；④ 畸形返回四档：围栏与前后附解释**能恢复**出同一条 SQL、截断**不恢复**且抬 `llm_bad_response`、`clarify` 非空不执行；⑤ 每步耗时在场（roadmap P2 踩坑 ② 那句"卡在哪一步"的可观测面）；⑥ **⑦ 的入参键集合被钉死**（多一个键就是调用方能传进来的一个入参，`row_limit`/`max_cell_chars`/`result_dir` 一旦被请求体摸到，L4/L3 口径与 ADR-0004 同时作废），`password` 断的是 `decrypt_secret` 那条线；⑦ **跨源只取相关度第一名的那个源**，断的是"补卡片的请求只带同源 uid"（断 prompt 里没它是桩造的假象——桩对任何 uid 都回同一张表）。<br>as-built(P2-012 施工后)：用例落点**不是工单原写的 `tests/unit/test_pipeline.py`**，而是 `tests/integration/test_pipeline_orchestration.py`（`pg` marker）——早退三类的验收本体是"留下一行 `chat_messages`"，不落库的断言只证明了返回值、没证明留痕。真跑一次 LLM 的那条是 `test_pipeline_live.py`（`live` marker）。**当时已知的洞**：模型按 ④ 模板写了 `LIMIT` 时守卫不补 `+1` 探针，`truncated` 恒假，本行 ⑤ 的耗时与 ② 的早退都真、只有这一格是死的——**2026-09-29 同日拍板闭环**，守卫改成"钳位 + 探针"（safety §1.2 ⑦ / §9.2 ⑧），撞上限那一支的 `truncated` 活了；模型自己写小于上限的 `LIMIT n` 那一支仍无探针、恒假，是有意的语义。<br>**as-built(P2-013)**：本行 ② 的三类早退补了两件东西。① **守卫拒绝那一支打了真 SQL**——用例 `test_模型硬产出_DROP_时守卫拦下且一次都不进执行器` 让路线回复直接产出 `DROP TABLE orders`，断 `executor` spy **零调用**、`sql_final is None`、违规 code `top_level_not_select`、落库行保留模型 `sql_raw` 原文、且结果目录**空**（没执行就不该有文件）。② **拒答文案有断言**——`no_schema_found` 那一行的 message 必须同时含"补录表注释 / 检查授权 / 同步"三个词（用例断的是这三个**词**，不是整句），CLI 侧另由 `test_demo_ask_print.py` 三条用例钉"每一种拒答都跟一行下一步动作"、"守卫拒绝那步说清了没执行也没落文件"、"无匹配表那步给的是今天真能走的三条动作"（`POST /api/sync/jobs` 而不是界面按钮）；分层本身还有一条反断言——服务端 message 里**不许出现"点此"**（界面话属于 P8 那一层，见 architecture §4.1 的 as-built(P2-013)） |
| L1 检索三件纯函数（as-built(0009)：`query_terms` / `column_hits` / `rank_candidates`，`app/services/nl2sql/retriever.py`） | 手算黄金值，不碰 DB | ① 切词：`订单金额是多少` 只发二字滑窗（7 字 > 整段阈值 4）、`订单金额` 整段+滑窗都发、`pay_amount` 出整段与两个子词、单字段被丢；② 命中：同一列被两个词命中算两行理由但只算**一列**，比对字段按 `column_name → comment_raw → comment_zh` 顺序各查一次，判定用"子串 + casefold"以与 SQL 侧的 `ILIKE '%term%'` 严格同构；③ 排序：主键**命中的不同词数**、次键命中列数、`table_uid` 收尾（architecture §5.1 那条 as-built 注），同表多卡 `boost=0.05×(n-1)` 只进 `score_kw` 不进排序键；④ `k` 是名额上限；⑤ 零命中返回 `[]` 而不是猜一张表（阶梯 L4 的判据，013 要用） |
| `executor`（as-built(P2-011)：`app/services/nl2sql/executor.py`，会话级只读 + 超时 + 截断 + csv 落盘） | 离线纯函数手算 + live 真连演示库 | 离线八件（`tests/unit/test_executor_*.py`，共 31 用例）：① `serialize_cell` — Decimal 按 `str` 原样回（`"0.30"` 不变 `"0.3"`）、naive/aware `datetime` 走 `isoformat()`（前者无偏移后缀、后者保留 `+08:00`）、`date` 走 `YYYY-MM-DD`、`time` 走 `isoformat()`（TIME 列 → `"14:30:00"`）、`timedelta` 按 **MySQL TIME 字面量** `[-]H:MM:SS` 手工拆（`1:00:00`/`-1:00:00`/`30:00:00`/`0:00:45`——**不是** `str(timedelta)` 的 Python 规范形 `"-1 day, 23:00:00"`，否则 §3 第 10 步的逐位对数对不上）、`bytes` 分 `<binary 3B>` 与 `<binary 1.2KB>` 两档、超 `max_cell_chars` 的 str 截断、int/float/bool/None 原样透传；② `split_truncated` — 1001 行 & `row_limit=1000` → `truncated=True` 且末行为探针被丢、500/1000/`[]` 三种边界都不截断；③ `new_run_id` + `result_csv_path` — 形状 `[A-Za-z0-9]+`（生成器与校验器**共用一份正则**，防止"生成的名字过不了自己的门"），越界 run_id（`../etc/passwd`、`/abs/x`、`a\x00b`、`a/b`、`..`、`""`）一律 `ValueError`；④ `write_result_csv` — `utf-8-sig` BOM 头幂等回读、自动建父目录、逗号+引号 roundtrip；⑤ `enforce_readonly_grants` — SELECT-only 通过、`INSERT/UPDATE/DELETE ON ai_web_demo.*` 或 `ALL PRIVILEGES ON *.*` 抛 `ReadonlyCapabilityMissing`；⑥ `classify_source_error` — errno 3024/`"57014"` 翻成 `QueryTimeout`（detail 必须同时含 `timeout_ms` 数值与来源键名），非超时的 1146 原样 re-raise；⑦ `session_statements` — MySQL 顺序 `READ ONLY` 必须**第一位**、`wait_timeout` 用 `timeout_ms//1000+10`（15000→25），PG 走 `SET statement_timeout = '<n>ms'`；⑧ `resolve_timeout`（双轴审查收口新增）— 列上有值走 `data_sources.timeout_ms`、列上缺值落 `AIWEB_QUERY__TIMEOUT_MS` 且值必须 >0；**来源判定不在调用方**（`execute_readonly` 不收这两个入参）；⑨ **`resolve_result_file`**（as-built(P2-013)，下载侧那半边，`tests/unit/test_executor_path.py`）——三道门（名字不合法 / realpath 逃出目录 / 不是文件）塌成**同一档** `ResultFileUnavailable`，用例直接断 `{(code, message, status_code)}` **集合只有一个元素**且 `detail is None`（三种失败对外逐字相同，任何差别都是"run_id 是否存在"的 oracle），四个入参**逐字对上三道门**。形状门**复用写入侧的 `result_csv_path`** 而不是另写一份正则，它的 `ValueError` 在这里被翻掉——不翻就是客户端输入打出 500。门②（realpath 逃逸）**在本机真跑**：`os.symlink` 要特权（winerror 1314）但 **junction 不要**，而守卫判的是 `resolve()` 后的落点、与链接类型无关——`test_目录里的链接指向外面时拒绝` 用 junction 造逃逸，并断服务端日志出现 `reason=realpath 逃出目录`（异常种类分不出门②与门③，判定顺序被换掉时用例照样绿，所以要钉日志）；`test_结果目录自己是指向别处的链接时照常放行` 钉反面（目录级链接不算逃逸，运维挪结果盘的正当做法）。用例先试 `os.symlink` 再退 junction，两者都建不出来才 skip——skip 只可能意味着这台机器造不出该文件系统对象。**仍未证明的只剩一格**：指向**文件**的符号链接这一具体形态本机复现不出来（缺的是构造手段，不是代码分支），见 safety §7 的 as-built。<br>live 四条（`tests/integration/test_execute_live.py`，全 `pytest.mark.live`）：① 只读查询跑通 + Decimal/DATETIME 序列化 + csv 落在服务端拼的 `tmp_path` 内（不在 `data/results/`）；② `row_limit=5` 时 `truncated=True`、csv 恰好 6 行（1 表头 + 5 数据）；③ **引擎级写拒绝 ≠ 应用级写拒绝分开断言**（验收 ①）——应用侧 `check("update ...")` 抛 `SqlGuardError`，绕过守卫直喂 `execute_readonly` 时源库自己拒：`DBAPIError.orig.args[0] in (1792, 1142)`，**刻意不收 1046**（那说明是连接没选库的配置错、不是"写被拦"，混进来这条就假过）；④ **超时真中断**（验收 ②）——`select sleep(1) from order_item limit 5` 每行都睡 1 秒踩行边界（`select sleep(2)` 单行不响应，`count(*)` 也只吐一行；这是 MySQL `MAX_EXECUTION_TIME` 的坑，测试 docstring 已钉），命中 3024 → `QueryTimeout` 且 detail 含 `data_sources.timeout_ms` + `300`（键名由执行器从行上自己判出，不是用例传的） |

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
| 6 | 立即同步 | SSE 进度到 100%；`sync_jobs.status=success`；phase 序列完整 |
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
