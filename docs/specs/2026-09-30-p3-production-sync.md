# P3 规格：生产级元数据抽取与同步作业

日期：2026-09-30（grill 走完 12 条拍板后综合，未再访谈）
依据：`docs/adr/0010`、`docs/adr/0011`、`docs/metadata-model.md` §2.8/§6/§8/§9、
`docs/architecture.md` §7、`docs/roadmap.md` §P3（含 as-built(P3 开工前拍板)）、`CONTEXT.md` 同步节。
术语以 `CONTEXT.md` 为准：同步作业 / worker / 队列接口（JobQueue）/ 进度事件 / 阶段（phase）与档（stage）/
僵尸回收 / 删除差分 / partial。

## 1. 问题陈述

P2 交付的同步是**请求内跑完**的：`POST /api/sync/jobs` 同步调用 `run_sync` 并在 200 里带回计数
（`endpoints/sync.py:16-28`）。它能演示，但有三个已知的、当场就能复现的问题：

1. **一次存盘就打断一次同步**。`AIWEB_RELOAD=true` 是本机默认，而被打断的那行 `running` 会占住
   `ux_sync_running`（部分唯一索引，`alembic/versions/0004_meta.py:119-126`），该数据源此后每次同步都 409，
   只能手工进库删行救。007 的双轴审查把"终局状态必须写在 `finally`"堵掉了同一个坑的另一半
   （`sync_service.py:904`），但进程被重启这一半没堵。
2. **前端什么都看不见**。`sync_jobs.progress` 列至今没有任何写入点（`_set_phase` 的 `values` 字典
   `sync_service.py:629-640` 不碰它），`heartbeat_at` 只在开 job 时写一次（`:603`），
   SSE 端点与 `GET /sync/jobs/{id}/events` 一个都不存在。P8 的同步进度 UI 没有可接的东西。
3. **一批配置承诺是空的**。`batch_size` / `batch_interval_ms` / `heartbeat_interval_s` /
   `stale_job_reclaim_s` / `sample_distinct` / `sample_distinct_max_distinct` /
   `min_mysql_version` / `min_pg_version`（`settings.py:146-153`）与 `result.retention_days`（`:191`）
   ——**九个键只有定义点没有读取点**。"读取点"这三个字在这里不是修辞：键存在而无人读，
   配置改了没反应，是 P2 数过两次的那类假承诺。

外加一条能力缺口：PG 数据源登记得了、测得了连通性，但同步必炸——`_extractor_for`
（`sync_service.py:651-659`）对 `kind != 'mysql'` 抛 `NotImplementedSource`（HTTP 501）。

## 2. 方案概述

把"发起同步"与"执行同步"拆开，并把进度变成可回放的事实：

- **队列接口（JobQueue）**：`enqueue / subscribe / worker` 三个动作的 Protocol。唯一的实现落在元数据库
  自己身上：`sync_jobs` 行是队列元素，`sync_job_event` 是进度真相，`NOTIFY` 只是叫醒（ADR-0010）。
- **worker**：新进程 `scripts/run_worker.py`。它从队列抢一行、跑 `run_sync`、在独立连接上刷心跳、
  每轮顺手扫僵尸、每轮顺手做保留期回收。API 进程不再执行作业（ADR-0011）。
- **API**：`POST /api/sync/jobs` 改成"权限校验 → 写 `pending` 行 → 202 + `{job_id}`"，路径不动
  （007 已拍板"只换返回码不动路径"）；`GET /sync/jobs/{id}/events` 是 SSE，按 `seq` 游标追读事件表。
- **抽取器**：MySQL 侧读侧真分批；新增 PG 侧抽取器（`metadata-model §8` 已写好那五条 SQL 原文）；
  类型归一化在**抽取层（写侧）**落地，两方言一起做。
- **失败隔离**：元数据侧维持 007 的"每库一事务"，卡片改成**逐表提交**——这是验收 5 那句
  "其余表已落库"能成立的唯一前提。

跨过的接缝只有一个新增的：JobQueue。其余都在已有接缝上（`run_sync` 的签名、`Extractor` Protocol、
`kb_service` 的卡片写入、SSE 事件契约）。

## 3. 用户故事（每条可独立验证）

### A. 入队与执行分离

1. 已登录用户带 `datasource_id` POST `/api/sync/jobs`，拿到 **202** 与 `{job_id}`，此时
   `sync_jobs` 里那一行是 `pending`，且**响应已经返回而作业还没跑完**。
2. 同一数据源在 `pending`/`running` 期间再次 POST，得到 **409 `sync_already_running`**
   （判据是数据库的部分唯一索引，不是代码 race）。排队中的作业也挡新作业，这是本片的**预期行为**。
3. worker 启动后把 `pending` 那行claim 成 `running`，且**同一个 job 只会被一个 worker 拿到**
   （条件更新的行数判定）。
4. 用户不带 token 直接跑 `scripts/run_worker.py` 时，它从队列里取到作业并跑完，终局状态写在 `finally`
   （任何异常路径都不例外，含未分类异常）。
5. `?force=true`（请求体 `{"force": true}`）能覆盖 `AIWEB_EXTRACT__MAX_TABLES` 上限：超限不再 400，
   而是照常开工；同时 `SCOPE_REMEDIES` 的第三条出路文案换回"admin 用 `?force=true` 覆盖上限"，
   §6 表 / 常量 / `test_extract_mysql_client.py` 三处字面一起动。

### B. 进度事件与 SSE

6. 作业每发生一次可对外说明的状态变化，`sync_job_event` 就多一行（`seq` 在 job 内单调 +1），
   并 `NOTIFY` 一次、payload 只有 `job_id`。
7. `GET /api/sync/jobs/{id}/events`（同数据源权限）以 `text/event-stream` 输出 `event: progress` 序列，
   每帧带 `{stage, done, total, base_table, view}`，并以 `event: done` 结束；`total` 是
   **排除下划线前缀对象后的计数**（演示库 = 10：9 张 `BASE TABLE` + 1 张 `VIEW`，2026-09-27 已拍板，
   不许照 `SHOW FULL TABLES` 的 11 断）。
8. 断线重连带 `Last-Event-ID`（或 `?cursor=`）时，**从上次游标接着读，一条不丢**——这条是选事件表
   而不是纯推 `NOTIFY` 的全部理由，必须有用例钉住。
9. 外部观察者每 15s 至少收到一帧 `: ping` 注释行，即使作业没有新事件。
10. `sync_jobs.phase`（细粒度，9 值 CHECK 不变）与事件里的 `stage`（粗粒度：extract / embed /
    upsert / card_build / done）之间**只存在一个映射函数**；`grep` 不到第二处把细值翻译成粗值的地方。
11. `embed` 档在未配置 embedding 端点时以 `skipped=true` + 原因出现在事件流里，作业整体仍可以是 `success`
    （本机 `.env` 的 embedding 三键为空，`settings.embedding.configured` 为 False）。

### C. 心跳、僵尸回收与保留期

12. worker 在**独立连接**上每 `AIWEB_EXTRACT__HEARTBEAT_INTERVAL_S`（10s）刷一次 `heartbeat_at`；
    主事务未提交时那个值也已经可见（否则进度条永远不动）。
13. 同一个循环每轮扫一次僵尸：`status IN ('pending','running') AND heartbeat_at < now() - 180s`
    的行改成 `failed`，并向 `errors`（`jsonb`，**没有 `error` 这一列**）追加一条带 `reclaimed` 码的记录。
14. 杀掉 worker 再重启：日志出现"回收 N 个僵尸 job"，重新发起同步得到 `status=success`，
    且**不需要手工进库删行**。
15. 回收循环按 `AIWEB_RESULT__RETENTION_DAYS`（30 天）删除 `sync_job_event` 的旧行；
    **`data/results/` 下的 csv 一个字节都不动**（删文件不可逆，且 P2 验收 4/5 的现场证据就在里面）。

### D. 分批与失败隔离

16. MySQL 的 `collect()` 按 `AIWEB_EXTRACT__BATCH_SIZE` 切片发那几条 `IN (...)` SQL，批间
    `sleep(BATCH_INTERVAL_MS)`。用例把 `BATCH_SIZE` 设成 3，在**11 个对象的真演示库上跑出 4 批**，
    且批数与每批表数从事件流里断得出来（不是只断总数）。
17. 元数据侧：某个 schema 的整批抽取真抛错时，该 schema 回滚、其余 schema 继续，终局 `partial`
    ——这是 007 的现状，本片**不许改**它的粒度，也不许把它悄悄放宽成逐表。
18. 卡片侧：让 3 张表的 `card_build` 抛错（测试专用注入钩子，走真 HTTP → 真入队 → 真跑），
    终局 `partial`、`errors` 含那三张表名、**其余表的卡片照常在场**，且元数据一行不少。
19. 卡片逐表提交后，某一张表失败不回滚已成功的那些张——用例要证明"已提交的卡片不在回滚射程内"。

### E. PG 数据源与类型归一化

20. 用户以超管跑一次 `scripts/demo_pg.ps1`（`make demo-db-pg`，内部执行 `backend/scripts/init_demo_pg.sql`）
    之后（建 `ai_web_demo_pg` + 9 张业务表/1 视图，其中 8 张带中文表注释 +
    只读账号 `demo_pg_ro`），登记成 PG 数据源并同步，能真跑通"discover → 分批读 → 落库 → 建卡片"。
21. PG 抽取器按 `metadata-model §8` 已写好的五条 SQL 原文实现（`pg_attribute` ⋈ `pg_description`、
    `pg_index` 的 `unnest(i.indkey) WITH ORDINALITY` 保序、`pg_constraint`）。
    **开工第一件真跑事项**：§8:869 那句"`indkey::int[]` 用来保序"里的显式数组转换从未在 live 上证过
    （§8 的 SQL 原文 :845 用的是不带转换的 `unnest(i.indkey)`）。015 的自检因此改用
    `pg_index.indexprs IS NOT NULL` 判表达式索引；若 024 真跑时证明 `::int[]` 可行，回写 §8 那句话，
    不行就把 §8 的表述改成与 :845 一致。
22. 类型归一化落在**抽取层**：`data_type` 写归一值、`raw_data_type` 写方言原文。
    `tests/unit/test_pg_type_normalize.py` 覆盖 roadmap 验收 6 点名的
    `_text` / `_int4` / `_numeric` / `_timestamptz` / `_varchar` / `[]` / `_jsonb`。
23. MySQL 侧同片归一：`decimal unsigned zerofill`、`enum('a','b')`、`set`、`datetime(3)` 一起处理，
    两方言的 `data_type` 口径一致（否则会出现 MySQL 存 `varchar`、PG 存 `varchar(64)`）。
    卡片 golden 快照因此要重录——现 8 份 `tests/fixtures/prompts/kb_card__*.expected.txt`，
    逐份只许在类型字面处变化。**这是本片已知的影响面，不是回归**。
    `tinyint(1)` 归成 `bool` 还是保留原样，由 008 侧的用例先拍一次并写进文档，不许两方言各走一边。
24. `CREATE_TIME` / `UPDATE_TIME` 接进映射：SQL 早已 SELECT 了（`mysql.py:84`），`RawTable` 的
    `last_analyze_at` 字段**也在**（`base.py:87`），下游 upsert 与卡片渲染都通了
    （`sync_service.py:97`、`:375`、`kb_service.py:422`）——**缺的只有 `rows_to_tables()`
    （`mysql.py:181-201`）没读那两列**。后果是 `meta_table.last_analyze_at` 至今恒 NULL、
    卡片【规模】那句"最近更新"永不出现（008 账 ④ 明写"归下一张碰抽取器的工单"——就是这张。
    那条账里"`RawTable` 也没有对应字段"半句失实，本片一并改口）。
    PG 侧的对应原料 §8.2 B 早已写好——那条 SQL 就 SELECT 了 `s.last_analyze, s.last_autoanalyze`
    （`metadata-model.md:809-812`，LEFT JOIN `pg_stat_user_tables`），取哪一个的口径归 024 落地。
    **as-built(022)：MySQL 半边已落**——取值口径（UPDATE_TIME 优先 → 退回 CREATE_TIME → 都空
    NULL；视图恒 NULL；时区显式 attach）逐字写进 `metadata-model §2.4`，上面引的"没有对应字段"
    失实半句就此收口（全链只有 `rows_to_tables()` 那一处映射，已补）；PG 半边（取较晚者）留在
    024 落地。

## 4. 已定决策（不要翻案）

| # | 决策 | 依据 |
|---|---|---|
| 1 | 队列介质是 PG，接缝是 JobQueue Protocol；Redis 明确不引入 | ADR-0010 |
| 2 | 作业在独立 worker 进程执行，API 只入队 | ADR-0011 |
| 3 | 进度真相在 `sync_job_event`，`NOTIFY` 只带 `job_id` | ADR-0010 |
| 4 | 阶段词表双层：库内 9 值 `phase` 不动，对外 `stage` 粗粒度，映射唯一 | metadata-model §6 as-built 第 2 条 |
| 5 | 心跳与僵尸回收都在 worker 心跳循环里，**每轮都扫** | metadata-model §6 as-built 第 4 条 |
| 6 | 失败隔离 = 元数据每库一事务（现状）+ 卡片逐表提交 | §6 as-built 第 5 条 |
| 7 | 读侧真分批，用例用 `BATCH_SIZE=3` 在真库跑出多批 | §6 as-built 第 11 条 |
| 8 | `embed` 档 P3 为 `skipped`，验收 5 的注入点换成 `card_build` | §6 as-built 第 6 条 |
| 9 | 类型归一化落抽取层（写侧），PG 与 MySQL 一起做 | 本轮补拍 |
| 10 | `?force=true` 本片接；回收作业只清事件表 | §6 as-built 第 7、9 条 |

## 5. 测试决策

- **最高接缝是 HTTP + 队列**：验收 1/2/3/5/7 都从 `POST /api/sync/jobs` 进，再 await worker 的循环体
  （不真起子进程——可调试堆栈，且 005 那次"进程内/子进程环境变量口径不一致"的教训还在）。
  进程边界与 SSE 真流由**一张真进费用例**单独钉。
- **用例改写是工作项不是副作用**：P2 的 `test_sync_live.py` / `test_sync_pg.py` / `test_sync_cache_pg.py`
  三张都在请求内等 `run_sync` 返回，必须改成"拿 job_id → await worker → 照旧断言"。
  断言本体尽量不动——这正是选"await 循环体"而不是"真起子进程"的原因。
- **期望值来自独立真相**：`total=10`、批数 4、`sub_part == 32`、golden 卡片文本，都手算或来自文档，
  不许由被测函数自己算一遍。
- **归一化是纯函数**，`postgres_types.normalize()` / MySQL 侧同理，用 §9 那张映射清单当 oracle，
  不连库。
- **假绿防线**（P2 数过四类，模式都是"形状测试原理上看不见"）：分批要断到**每批的表名集合**；
  僵尸回收要在**没有重启**的路径上证明扫到过；卡片逐表提交要证明**已提交的卡片不受后续回滚影响**；
  `409` 要证明它来自索引而不是代码分支。
- 闸是 `scripts/dev.ps1 check`（ruff + vue-tsc + pytest，含 live）+ 手动的 `ruff format --check` 与
  `mypy app`。跑闸前要确认本机 MySQL 演示库与 PG 在线。

## 6. 明确不做

- **`cancel`**：`architecture §7` 那行挂 [P8]。`sync_jobs` 至今没有 `cancel_requested` 列，
  加列与批间检查点等 P8 的"停止同步"按钮一起做；`status='cancelled'` 继续没有写入点。
- **`retry`**：`retry_of` 列同样不存在，P3 七条验收没有一条要求重跑 → 记为无主项。
- **版本闸门**：`MIN_MYSQL_VERSION` / `MIN_PG_VERSION` 仍不接。**PG 抽取器落地而不做版本判定是明示的
  偏离**，不是遗漏（§6 表第 1 行那句 `UNSUPPORTED_VERSION` 挂注转 P6/P10）。
- **权限不全那套**：`SCHEMA_PARTIALLY_VISIBLE` 告警与 `known_complete` 门控不做
  （`RawCatalog.grant_limited` 字段在但无人写，delete-diff 仍无条件跑）。
- **`manifest_digest` 短路**：不做。它一做，验收 5 的"其余表已落库"就得重写成"上一轮已落"，
  测试形状变复杂。
- **枚举 NDV 采样**：三步整块推 P4。本片只新增 `AIWEB_EXTRACT__SAMPLE_ROW_LIMIT` 键
  （此前根本不存在，而 kb-workflow §8 一直当它存在在讲），`sample_distinct*` 两键挂 [P4]，
  `meta_column.sample_values` 继续无写入点。
- **结果文件的清理**：不碰。
- **`/sync/jobs` 列表端点、`GET /sync/jobs/{id}` 详情端点**：SSE 够用就先不上，缺了记进无主账。
- **不引入 Redis、不引入 Celery/arq、不开多个 worker、不做分布式锁**。

## 7. 补充说明

- **环境前置（唯一卡住验收 6 的一格）**：`init_demo_pg.sql` 由我写，**用户以超管 psql 跑一次**。
  口令/凭据不进对话、不进命令行、不进 git（沿用 002 的 `.setup/*.cnf` 那套做法）。
  跑之前要确认本机 PG 服务 `postgresql-x64-18` 在线；`aiweb` / `aiweb_test` 两个库**一个都不许动结构**
  ——`aiweb` 是真元数据库，`alembic_version` 至今停在 0002，别把"真库没这些表"当 bug，也别对它发 DDL。
- **迁移**：`0007_sync_event`（建 `sync_job_event`）。ORM ↔ 迁移漂移的 `alembic check` 至今没接线，
  所以建表那一条要人工比对 `models/meta.py` 一次。
- **PG 源驱动这一格是空的**（024 开工前必读）：`source_manager.py:23` 把 `kind='postgres'` 映射到
  `postgresql+psycopg`，而 `psycopg` **根本不在依赖里**（`pyproject.toml` 只有 `asyncpg`（元数据库）+
  `asyncmy` + `pymysql`）。本机实测：`create_async_engine("postgresql+psycopg://…")` →
  `ModuleNotFoundError: No module named 'psycopg'`——也就是说今天登记一个 PG 源点"测试连接"就炸在方言加载。
  `architecture §9` 的驱动表对此还写着两条打架的话（`:760`"psycopg3 的 async 方言在 SA 2.0 不可用"、
  `:763`"源 PG 抽取用 psycopg 同步"）。**`:760` 那句已被证伪**：本机 SQLAlchemy 2.0.54 的
  `postgresql/psycopg.py` 模块文档就写着 `create_async_engine` 用法（详见 §9 的 as-built 注）。
  024 因此走"补 `psycopg[binary]` 依赖 + 异步方言"这条路，但仍要**真连一次 `ai_web_demo_pg`** 才算数。
- **dev.ps1 / Makefile**：worker 是本机第二条命令，`scripts/dev.ps1` 与 roadmap §6 那张目标表要一起改，
  否则用户会遇到"点了同步但进度永远不动"——那正是入队成功而没人消费的形状。
- **Windows 约束**：worker 脚本顶部要照 P1 的坑补上 Selector 事件循环策略
  （`asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())`），`asyncpg` 在 Proactor 下不兼容；
  控制台输出照 cp936 的教训显式 UTF-8。
- **P2 遗留的账里，本片会顺手闭掉的只有两条**：`progress` 列第一次有写入点、`pending` 状态第一次有写入点。
  其余无主项不因本片自动消失。
