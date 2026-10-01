# 实施路线图与配置分层

> 前半是 P1–P10 阶段表与验收标准（含依赖图与人日），后半是配置四层归属与 env 键清单。
> 计划期人日估算基于 solo dev、工作日 4h 有效编码。

## 1. 阶段总览与依赖图

```
P1 ─▶ P2(最小端到端) ─┬─▶ P3(抽取+同步) ─┬─▶ P6(权限)  ─▶ P9(知识运营)
                      ├─▶ P4(混合检索)   ─┘
                      ├─▶ P5(守卫加固)
                      └─▶ P7(前端基座) ─▶ P8(Chat SSE) ─▶ P9
P10(收口) 依赖 P3..P9，但测试骨架从 P2 起并行生长
```

| 阶段 | 内容 | 人日 | 依赖 |
|---|---|---|---|
| P1 | 脚手架 / 配置 / 环境勘察 | 2.5 | — |
| P2 | **最小端到端问数（CLI）** | 6 | P1 |
| P3 | 抽取 + 同步 + SSE | 5 | P2 |
| P4 | 混合检索 + profile 切换 | 5 | P3 |
| P5 | 守卫加固三层防御 | 3.5 | P2 |
| P6 | 权限贯通 | 3 | P3,P4 |
| P7 | 前端基座 | 6 | P2 |
| P8 | Chat 页 | 6 | P7,P4,P5 |
| P9 | 知识运营 | 4 | P6,P8 |
| P10 | 收口 | 3 | all |
| | **合计** | **44.5** | |

风险缓冲：首次接远端 PG / 国产 embedding 端点的兼容性调通按 **+20%** 计（≈9 人日）。
若必须先出可演示物，砍掉 P7 的 Dashboard 与 P9 的术语管理，可在 32 人日内拿到能演示的 v0。

## 2. 各阶段目标与验收标准

### P1｜脚手架 + 配置层 + 环境勘察（2.5 人日）

目标：把 `.env` → `settings` → PG 连接 → 健康检查这条"能开机"的路铺平，并把远端 PG 的能力边界探清楚。
涉及：`pyproject.toml`/`uv.lock`/`.env.example`、`app/{main,settings,deps}.py`、
`app/core/{db,errors,logging,pagination}.py`、`endpoints/health.py`、`scripts/check_env.py`、
`alembic/{env.py,0001_*}`、`tests/conftest.py`、`Makefile`、`scripts/dev.ps1`、前端 Vite 脚手架 + proxy。

验收：
1. `uv run uvicorn app.main:app --reload` 后 `curl -s localhost:8000/api/healthz` 返回 `{"status":"ok"}`；
2. 环境体检脚本打印自检结果，且**故意把 `AIWEB_EMBEDDING__DIMENSION=3072` 设进去时进程退出码非 0 并给出中文 hint**；
3. `uv run alembic upgrade head` 在远端 PG 建立 `aiweb` schema + 扩展；把 `AIWEB_PG__PASSWORD` 改错时报的是
   "权限/连接"而不是栈溢出；
4. `uv run alembic downgrade base` 能干净回滚（0001 无表，验证 down 链存在）；
5. 浏览器打开 `http://127.0.0.1:5173` 能看到页面且经 proxy 的 `/api/healthz` 返回 200（**证明 proxy 通，P8 的 SSE 才有地基**）；
6. `0001` 迁移里 `CREATE EXTENSION vector` 失败的报错文案包含"pgvector 的 vector 扩展不是 trusted，需超级用户执行"。

> **P1 as-built（2026-09-23）**：① 端点实际路径是 `/api/healthz`（`endpoints/health.py`），不是 `/api/health`；
> ② 第 6 条按实现方式调整过——`0001_baseline.py` **不尝试建 `vector`**（非 trusted，非超管必失败且会脏掉迁移），
> 建扩展的引导交给 `scripts/check_env.py`：`embedding` 已配置而 `vector` 缺失时报 `fail`，
> hint 为"请 DBA 执行 CREATE EXTENSION vector;（0.4+ 才支持 HNSW）"。`pg_trgm` 是 trusted，仍由 0001 建。
> ③ 第 3、4 条依赖远端 PG 凭据，本机未执行；`alembic upgrade head --sql`（离线）已验证会渲染
> `CREATE SCHEMA`，凭据到位后需补跑一次真实 upgrade/downgrade。

踩坑预警：① Alembic `env.py` 必须 `import pgvector.sqlalchemy` 且 `target_metadata` 指向带
`schema="aiweb"` 的 MetaData，否则 autogenerate 会把 `vector` 渲染成未知类型、并把表生成到 `public`；
② `version_table_schema="aiweb"` 要显式传；③ **Windows 上 `asyncpg` 不兼容 Proactor 事件循环**——
uvicorn 的 `--loop asyncio` 会自动设 `WindowsSelectorEventLoopPolicy`，但自写脚本和 pytest-asyncio 不会，
需在 `conftest.py` 与 `scripts/*.py` 顶部统一加
`if sys.platform == "win32": asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())`。
（已落地：`backend/scripts/check_env.py`、`backend/tests/conftest.py` 都有这一行。）

### P2｜最小可跑问数链路（端到端 walking skeleton）（6 人日）⚠️ 硬门槛

目标：命令行一句话问数 → 出结果表格。**检索先用 LIKE 兜底、没有前端、没有 pgvector 向量路**，
但全链每一环真实存在。

关键实现约束：
- `retriever.search()` 定义成 `Protocol`，P2 给 `LikeRetriever`，P4 换 `HybridRetriever`，
  `pipeline` 只依赖 Protocol——这样 P2/P4 不打架，也让"向量挂了降级回 LIKE"成为生产容错分支。
- `kb_card.embedding` 列在 P2 就按 `vector(dim)` 建（维度从 settings 渲染进迁移，
  as-built(0008)：走 `pgvector.sqlalchemy.Vector(dim)` 类型层，**不是**拼字符串的 `op.execute`——
  后者会让列宽与 ORM 类型各写一份，`alembic` 的 ORM↔迁移比对就失效；见 `metadata-model §2.6`），
  HNSW 索引也 P2 就建，P4 只回填数据——
  **避免"P4 才发现维度不对要改迁移"**。
- `demo_ask.py` 串起：登录拿 token → 取数据源 → 若 `meta_table` 为空则现场跑一次最小抽取 →
  `pipeline.ask(question)` → 打印 `retrieved_tables / sql / guard_verdict / rows`。
  > **as-built(P2-012 开工前拍板)**：本行那句"登录拿 token"与验收 7（"P2 允许没有 chat endpoint，
  > 只做 `pipeline` 单测 + CLI"）互斥，按**验收 7** 收口：012 不开端点，所以没有 token 可拿——
  > `demo_ask.py` 是**进程内**装配，pipeline 收 `actor: User` 对象，鉴权走
  > `datasource_service.get_authorized` 同一条路（与 HTTP 层共用同一个函数，权限面不因为绕过
  > 端点而变小）。验收 5 后半句"`/api/chat/ask` 的 dry-run 分支要返回 400"同理落不到 P2，
  > 013 工单已按"在 pipeline 返回值上断言 `sql_guard_rejected`"执行，HTTP 那半归 P8。

验收：
1. `init_demo_mysql.sql` 建成 `ai_web_demo`（9 表 + 1 视图，见 verification.md §1 的枚举清单），`SHOW TABLES` 可见；
2. `seed_admin.py` 建 admin，登录返回 `access_token`；
3. 建数据源成功，且 `SELECT left(convert_from(secret_enc,'UTF8'),12)` 看到的是 `gAAAAAB` 开头
   （Fernet 指纹）而不是明文——`secret_enc` 是 `bytea`，PG 没有 `left(bytea,int)`，必须先
   `convert_from` 再截；`GET /api/datasources` 响应体不含口令；
4. `demo_ask.py "2024 年每个月的订单总金额是多少"` → 合法 `SELECT`、`guard=pass`、非空结果行；
5. 同一条命令加 `"把 orders 表删了"` → 必须走到"分类为不可答/无匹配表"分支，**不得**产出 `DROP`；
   即使模型产出 `DROP`，`/api/chat/ask` 的 dry-run 分支要返回 400 `sql_guard_rejected`；
   > **as-built(P2-013)**：这条验收的后半句是 HTTP 端的（012 已拍板端点归 P8），前半句按
   > **"不得把 `DROP` 产出成 `sql_raw`/`sql_final`/被执行的 SQL"** 判定，不是"输出里一个 DROP 字面都没有"。
   > 差别是真跑出来的：`demo_ask.py "把 orders 表删了"` 走的是检索 0 张候选表 → `no_schema_found`
   > 早退，压根没到模型；换成点名表 `"把 order_main 表删掉"` 时模型确实接到了 prompt，
   > 而它的回答是 **clarify（追问）分支**，解释文字里原样写出了 `DROP TABLE ai_web_demo.order_main`
   > 这个字符串——`sql_raw` 为空、未执行、未落结果文件。**判定为通过**：那句 DROP 在 `clarify`
   > 的追问文本里，而 `{"sql","explanation","clarify"}` 三格是 010 定的输出契约（见 §4.1 as-built ①），
   > 模型**该**用文字说清它为什么没给 SQL。"输出里不得出现 DROP"这种读法会把拒绝理由本身算成违规，
   > 逼出来的处置是删掉解释字段——那是拿可解释性换一个字面匹配。
   > 守卫那半边的真链路证据不在这次实录里（模型没写 DROP SQL），改由桩钉：
   > `tests/integration/test_pipeline_orchestration.py::test_模型硬产出_DROP_时守卫拦下且一次都不进执行器`
   > ——路线回复 `sql="DROP TABLE orders"`，断言 `spies.executor_calls == []`（一次都没进执行器）、
   > `sql_final is None`、违规 code `top_level_not_select`、落库行 `sql_raw` 保留模型原文、
   > 且 `data/results` 目录为空（**没执行就不会有文件**）。
   > 拒答文案那半边（验收要"接住人"不只端错误码）钉在 `test_demo_ask_print.py` 的三条下一步用例，
   > 措辞按 P2 今天真能走的路写：服务端消息只给**动作名**（原来的"点此同步"是界面话，已删，
   > 并有用例断 `"点此" not in error_message`），具体入口交给调用方那一层——CLI 给 007 已交付的
   > `POST /api/sync/jobs`。分层理由见 architecture §4.1 的 as-built(P2-013)。
   > 见 safety §7、architecture §4.1 的 ④ 与 §3.3。
6. `uv run pytest tests/guard -q` 全绿；
7. P2 允许没有 chat endpoint，只做 `pipeline` 单测 + CLI（此项在 P8 才验收）。

踩坑预警：① 拼 MySQL DSN 忘 `quote_plus`（口令含 `@#%` 报"未知主机"，极难查）；
② naive `datetime` 直接进 `json.dumps` 会 500，executor 统一 `default=str` + 按列类型显式序列化 ISO；
③ decimal → float 精度丢失，首版按字符串返回；④ prompt 里必须给"必须带 LIMIT"与"只能引用给定表"
两条硬约束，否则 guard 拒绝率极高、白测一天；⑤ `parse` 与 `.sql()` 两处的 `read`/`dialect` 参数要一致。

> **as-built(P2-012)**：④ 那句"必须带 LIMIT"有一根 012 才看见的倒刺——守卫的截断探针 `+1`
> **只在语句本来没有 LIMIT 时注入**，于是模型越听话，`truncated` 越恒假（⑥ 那一档见 safety §9.2 ⑧，
> **同日拍板改成"缺则补、超则钳"，撞上限那一支的倒刺已拔掉**；模型自己写小于上限的 `LIMIT` 那一支
> 仍无探针、`truncated` 恒假，是有意的语义（safety §1.2 ⑦ 末）。
> 教训不是"约束写错了"，是**"桩给的草稿要照模板写"**：012 的桩用例手写的那句没带 LIMIT，
> 所以 `LIMIT 1001` 断言全绿，而真 LLM 每次都带。验收 4 强制真端点跑一次，抓出来的正是这种
> "桩测绿在一条真链路上不存在的路径上"。

### P3｜生产级元数据抽取 + 同步作业（SSE / 分批 / 心跳）（5 人日）

要点：MySQL 侧手写 `information_schema` 批量 SQL（`COLUMNS`/`STATISTICS`/`KEY_COLUMN_USAGE`/
`REFERENTIAL_CONSTRAINTS`/`TABLES`，各自 `WHERE table_schema=%s AND table_name IN (...)`，
一批 200 表）；PG 侧 `pg_attribute`+`pg_description`+`pg_index`+`pg_constraint`
（`unnest(i.indkey) WITH ORDINALITY` 保序，`atttypmod` 归一）。批内一事务、`progress` 累计、
每 10s `heartbeat_at`、启动回收僵尸、`(datasource_id) WHERE status='running'` 部分唯一索引。

> **as-built(P3 收口)**：上面这一行要点里有四处与最终交付的口径不同，逐条在此更正（细节住在
> metadata-model §6 的 as-built 链，不重复论证）：
> ① **"批内一事务"不成立**——批是**发送切片**，提交粒度仍是"每库一事务"（§6 拍板第 5 条；
> `slice_names` 只切 `IN (...)` 的名单，020）。② **`progress` 不是"累计"出来的**，它由
> `progress_of(counters)` 从**事件帧里那一份** `{done,total}` 折算，`total<=0` 时根本不写那一格
> （025，见 verification §2.1 的 `sync_jobs.progress` 行）。③ **回收不在启动钩子里**：僵尸判定住在
> worker 心跳循环的每一轮，"只在启动扫一次"被否掉（§6 拍板第 4 条；真跑侧的时间线见 §1.7.1 的
> `killbatch` 段——启动行落在 14.49s、回收落在 15.49s，差一整轮）。④ **索引的 WHERE 含 `pending`**：
> 实装是 `status IN ('pending','running')`，所以排队中的作业也挡新作业（§6 施工后第 2 条）。
> 另更正一处字面：要点原写 `indkey::int[]`，而 SQL 原文与落地版用的都是不带转换的
> `unnest(i.indkey)`，那个显式转换至今没在 live 上证过（spec 故事 21 交给 024 判定的那一格，
> 判定与依据写在 metadata-model §8.2 那句保序的 as-built 里）。

验收：
1. `POST /api/sync/jobs` → 返回 job id；
2. `GET /api/sync/jobs/1/events`（SSE）输出 `event: progress` 序列（含 `phase=extract/embed/upsert`、
   `done=..., total=10`）并以 `event: done` 结束，`total` 与**排除下划线前缀对象后**的库内对象计数一致
   （**口径已拍板（2026-09-27）：报 10 = 9 张 `BASE TABLE` + 1 张 `VIEW`**；
   `progress` 事件除 `done/total` 外还要带 `base_table`/`view` 两个计数，视图卡片降级走 `view` 那一路。
   注意实测直接 `SHOW FULL TABLES` 是 **11** 行，因为建库脚本按 verification.md §1.2 第 2 步在库里
   保留了标记表 `_aiweb_demo_marker`——**不能**照字面把 `total` 断言成它的行数，
   详见 verification.md §1.2 末注）；
3. 同时再发一次 POST → 409 `sync_already_running`（证明部分唯一索引生效，不是代码 race）；
4. 同步中途 `kill` 进程再重启：日志出现"回收 N 个僵尸 job"，重启后重新同步得到 `status=success`；
5. 故意让 3 张表 embed 抛错 → `status=partial`、`errors` 含表名、其余 6 张表元数据已落库；
6. PG 数据源跑通，`test_pg_type_normalize.py` 覆盖 `_text/_int4/_numeric/_timestamptz/_varchar/[]/_jsonb`；
7. `meta_index` 里 `CARDINALITY`/`SUB_PART` 非空（证明没退回 `SHOW CREATE TABLE` 方案）。
   as-built(0007)：`SUB_PART` 这一半原先在演示库里**测不出来**（夹具没有前缀索引），已补
   `product.idx_product_name(name(32))` 并把期望值钉成 `sub_part == 32`，见 verification.md §1 注。

> **as-built(P3 开工前拍板)**（2026-09-30，grill 逐条走完；执行模型与队列介质见 ADR-0010 / ADR-0011，
> 全部口径细节在 metadata-model §6 末的那块 as-built）
>
> - **验收 1**："返回 job id"按 `POST /api/sync/jobs` 落 202 + `{job_id}` 判定。
>   路径沿用 P2 的那一条（工单 007 已拍板"只换返回码不动路径"），`architecture.md` §7 端点表里
>   `POST /datasources/{id}/sync` 那行的路径按此作废。`pending` 这个状态值从此有了写入点。
>   **as-built(P3-016)：本条已交付**，且断言不止"码是 202"——响应体 `keys() == {"job_id"}`、
>   读库断 `pending` + `started_at IS NULL` + 抽取器**一次都没被调用**，三条各挡一种假 202。
> - **验收 2**：`phase=extract/embed/upsert` 这三个字面**不在** `sync_jobs.phase` 的 CHECK 里
>   （那边是 9 值细粒度词表，`extract`/`upsert` 写进去会被 PG 直接拒）。判定改按双层词表：
>   SSE 上出现的是粗粒度 `stage`，库里存的是细粒度 `phase`，映射只住在一个纯函数里。
>   `total` 与"排除下划线前缀对象"的计数口径不变（仍是 10）。
>   **as-built(P3-017)：本条已交付**，真跑实录（工单 017 交付记录）是 `retry: 3000` 打头 →
>   五帧 `event: progress`（`stage` 依次 extract / upsert / card_build / embed / done，`id` 就是库里
>   那五行 `seq`，逐条对得上）→ 一帧 `event: done` 收尾；首帧 `total=10, base_table=9, view=1, done=0`，
>   **不是** `SHOW FULL TABLES` 数出来的 11。断线重连按 `?cursor=` 与 `Last-Event-ID` 两条路各钉一次，
>   两边都从 seq=3 起、一条不丢也不重。验收 5 那条"映射唯一"的实测口径是**翻译点唯一**而不是
>   "字面唯一"（`upsert`/`extract` 这类词在 SQL 语句名里避不开）：全仓 `stage_of` 只有一个调用点
>   （`sync_service.py:653`，写事件的那一处），五个粗档字面作为**词表**只出现在三处——迁移 0007
>   的 stage CHECK、它的 ORM 镜像 `models/meta.py:345`、`core/sync_vocabulary.py` 的 `_PHASE_TO_STAGE`。
>   另有三处同字面但**不同义**，都不算第二处翻译：`sync_jobs.phase` 那 9 值 CHECK 本来就含
>   `card_build`/`embed`/`done`（细值）、`run_sync` 里传给 `_set_phase` 的也是细值、`core/sse.py` 的
>   `EVENT_DONE = "done"` 是 **SSE 帧名**（与 §6.1 问数流的最后一帧同名，终局档的判定走
>   `sync_vocabulary.is_terminal_stage`，读侧没有再抄一个字面）。测试里出现的五个字面是从文档抄下来的
>   **期望值**，不是翻译点。
> - **验收 3**：409 的代码路径在 P2 已经通了（`_open_job` 撞 `ux_sync_running` → `Conflict`），
>   P3 要补的是"API 只入队"之后这条还成不成立——入队写的是 `pending`，而索引管
>   `status IN ('pending','running')`，所以互斥面反而变大（排队中的作业也挡住新作业）。
>   **as-built(P3-016)：成立，而且比 P2 更纯**——`_open_job` 已随进程分离删掉，
>   `enqueue` 里没有任何"先查有没有未结束作业"的分支，409 只能由 `IntegrityError` 撞
>   `ux_sync_running` 翻出来（只认这一个索引名，FK/CHECK 之类的约束失败不许报成"已经在同步了"）。
>   钉子 `test_排队中的_pending_行也挡住新作业`：队列里那行还是 pending、没有任何代码看过它。
> - **验收 4**：kill 的对象是 **worker 进程**，不是 uvicorn。"重启后回收 N 个僵尸 job"因此要在
>   worker 的启动日志里看，而回收动作按拍板是**心跳循环每轮都扫**（只在启动扫一次被否掉：
>   worker 长驻不重启是常态）。
> - **验收 5**：注入点从 `embed` 换成 **`card_build`**——本机 embedding 三键全空
>   （`settings.embedding.configured` 为 False），P3 的 `embed` 档是 `skipped=true` + 原因，
>   向量回填整块归 P4。"其余 6 张已落库"能成立的前提是卡片改成**逐表提交**；
>   元数据侧仍是每库一事务（007 现状不动），所以元数据真抛错时是整库回滚。
>   三张失败表由测试专用的注入钩子造，走真 HTTP → 真入队 → 真跑。
> - **验收 6**：需要一个**能被登记的 PG 源库**，由 `backend/scripts/init_demo_pg.sql`
>   + 外层 `scripts/demo_pg.ps1`（`make demo-db-pg`）建
>   （`ai_web_demo_pg` / schema `demo`：**9 张业务表 + 1 视图**，其中 8 张表带中文表注释、
>   `t_no_comment` 与 `v_daily_sales` 故意不带（降级分支），把 `text/_int4/_numeric/_timestamptz/_varchar/text[]/jsonb`
>   全铺上 + 一处 `serial` + 两处 `generated always` + 表达式索引/部分索引各一处 +
>   `demo_pg_ro` 只读账号），用户以超管跑一次，形状与 002 的 MySQL 建库同构，
>   四个口径数同样对成 **10/9/1/11**。枚举表、每表考点、口令通道与 34 行自检清单见
>   [verification.md §1.5](./verification.md)。
>   类型归一化按 metadata-model §9 落，`test_pg_type_normalize.py` 这个名字按本条建。
>   **注意**：`MIN_PG_VERSION` / `MIN_MYSQL_VERSION` 的 probe 版本闸门**本片不接**（连同
>   `SCHEMA_PARTIALLY_VISIBLE` + `known_complete` 门控、`manifest_digest` 短路一起转 P6/P10）——
>   "PG 抽取器落地而不做版本判定"是明示的偏离，不是遗漏。
>   **as-built(P3-024，代码半边已交付)**：`app/extractor/postgres.py` 落地（§8.2 五条 SQL +
>   行→`Raw*` 映射 + 020 的批形状 + 022 的新鲜度 + 023 的归一接线），`psycopg[binary]` 进依赖，
>   `sync_service` 的 501 判定改成按 kind 分发（**工单 024 的**验收 2 的两面由
>   `tests/unit/test_sync_dialect_dispatch.py` 钉，"列写法跟着 kind 走"那半由
>   `test_sync_pg.py::test_postgres_源的范围条件按_pg_的列写法下发` 钉）。
>   与本轮选路有关的两个事实写进 architecture §9 末注（psycopg3 async 在 Windows Proactor 下
>   `InterfaceError`，因此抽取侧是**同步 + `to_thread`**，不是"改走异步方言"）；
>   §8.2 原文两处照抄必报错的坑（`facts` 别名不存在、`pg_stat_user_tables` 没有 `reltuples`）
>   连同八处偏离记在 metadata-model §8.2 末注（这一份是**第八处**的目录：C 的 `modifiers` 搬到
>   Python，因此 C 是五条里唯一"文档原文 ≠ 落地查询文本"的那条，见 §8.2 末注 ⑧）。
>   **as-built(P3-024 live：验收 1/3/4/5/7 也已交付)**：用户以超管跑过建库脚本、`ai_web_demo_pg` 已在本机，
>   `tests/integration/test_extract_pg_live.py` 4 条走真 HTTP 登记 → 真入队 → 进程内 worker 真跑，
>   §8.2 五条 SQL 第一次落在真 PG 上，计数对成 **10 个对象（9 表 + 1 视图）/ 82 列 / 14 索引 /
>   15 条索引列 / 6 条外键 / 2 条推断 / 10 张卡片**，逐位清单与每个数的推导写在
>   [verification.md §1.6.1](./verification.md)。live 抓出两个**离线全绿**的缺陷，都已修并各留一条永久用例：
>   ① `sync_service.inferred_rows` 的表 id 查找键写死 `catalog=""`，而 PG 侧那一格是真库名（§1 口径），
>   于是推断边整批查不到表 id 被**静默丢弃**（`counters.relations_inferred` 报 0，`meta_relation` 少两条）——
>   修法是让 `InferredRelation` 带 `catalog_name`、由 `relation_infer` 透传，钉它的用例是
>   `tests/unit/test_sync_rows.py::test_推断行的表id查找带catalog_pg侧那是真库名`；
>   ② `meta_column.enum_values` / `sample_values` 用的是 SQLAlchemy `JSONB` 默认 `none_as_null=False`，
>   Python `None` 落成 JSON 字面 `'null'::jsonb`，§2.4 那两格的"没有值域"因此在 SQL 侧 `IS NOT NULL` 成立——
>   修法是那两列加 `none_as_null=True`，live 用例则改成拿 `count(*) filter (where … is null)` 这个
>   **数据库自己的谓词**来断，而不是拿测试端读回来的值反推。
>   **live 仍未证的**（逐条列在 metadata-model §8.2 末注"证到哪一步"，那里是唯一的一份）：
>   `relkind` 的 `'p'/'m'/'f'` 三分支（演示库只有 `r`/`v`；注意 `'m'`/`'f'` 在落地版是被 WHERE
>   筛掉的、不是"原样落库"，见 §8.2 末注 ⑤，所以那两支没有实测样本连"落库长什么样"都无从证）、
>   ④ 的"两枚时间戳同时在场时取较晚那枚"
>   （实测 0 张表两枚都在场，分辨力在桩上）、③ 的"每批重取 FK 会不会落两次"（默认批 200 只有一批）、
>   以及 C 条**真枚举数组**那一格（库里没有 `CREATE TYPE`，`typtype='e'` 实测 0 行）。
>   注意别把它跟 ② 搞混：② 的 `modifiers`（`varchar` 长度 / `numeric` 精度标度 / 数组三值全空）
>   **已经在真数据上证到了**，没证到的只有"元素类型是枚举的那个数组会不会命中 C 条的枚举段"。
>   还有一格是缺口而不是未证：表达式索引的文本有列可存（§2.4 `meta_index.funcdef`），
>   但 `RawIndexColumn` 不搬它，live 只钉到"当前恒空"。
>   **验收 6（MySQL 侧回归不变）**：本片把 `mysql.py` 的 `slice_names`/`in_list`/`in_params` 与
>   `apply_index_flags` 搬进了 `batching.py`/`base.py`，MySQL 那条链的回归面正是被搬动的地方，
>   "PG 没建库"不是跳过它的理由。为什么这一条在 live 半边必须重写口径：上面那两个修复落在
>   **MySQL 也走的同一条路**上（`inferred_rows` 对 MySQL 的 `catalog` 就是空串，`meta_column` 两列
>   属于元数据库而非源库方言），所以"PG live 跑通"与"MySQL 回归不变"不能各自单独自证——
>   as-built 的记法是**合并跑**：`-m "pg or live"` **182 条全绿**，含
>   `test_sync_live.py` + `test_extract_mysql_live_batches.py` + `test_kb_cards_live.py` +
>   `test_sync_card_isolation_pg.py` 四张（10 对象那一条是
>   `test_sync_live.py::test_验收1_十个对象落进_meta_table_且宽表_68_列带中文注释`）。
>   那四张只是选择器里的子集——同一个选择器 `--collect-only` 数出 **32 张文件 / 182 条**，
>   写清这个是为了别把"182"读成"就这四张"，也别拿手写拆分去代替数（工单 024 的交付记录里
>   记着这里漂过一次）。
>   一句限定：卡片那半的 golden 逐字符比较（`test_sync_card_isolation_pg.py`）喂的是手工摆出来的
>   `meta_*` 输入，它钉的是模板与逐表提交路径；"真抽取出来的文本没被搬动改坏"那半在
>   `test_extract_mysql_live_batches.py` 的两次真跑对比里。
> - **验收 7**：`CARDINALITY`/`SUB_PART` 早在 007 就通了（`mysql.py:121` 读、`:259`/`:265` 映射、
>   `sync_service.py:433`/`:444` 落库），P3 只补"分批后每批都还读到"这一格。
>   **as-built(P3-020)：这一格补上了，补法是"两次真跑比形状"**——`test_extract_mysql_live_batches.py`
>   的第二条用例先按默认 200（一批装完）跑一次、再按 3 切四批跑一次，比对索引那条链的
>   `(表名, 索引名, SEQ_IN_INDEX, 列名, SUB_PART, cardinality 是否非空)` 有序清单逐位相同，
>   并对分批那一次断"每一行 `cardinality` 都非空 + 全库唯一那处前缀索引 `product.name(32)` 仍在场"。
>   为什么比的是"非空"而不是**数值**：`CARDINALITY` 是 InnoDB 的采样估算，两次真跑之间它可以合法地变，
>   把它钉进"分批没改变结果"就变成看运气发红。为什么这比 007 那条用例多证明了一件事：
>   `test_sync_live.py` 的 `test_验收2_…` 用的是**默认批大小**（10 个对象一批装完），
>   它证明的是那条链通着，而"切成四批后每批还读得到"必须是走分批那条路才答得出。
>   实测变异：把 D 条投影里的 `s.CARDINALITY, s.SUB_PART` 换成 `NULL AS …`，这条 live 用例红在
>   `has_cardinality` 那一行（`test_sync_live.py` 的同名锚点也一起红——两者不互替，一个管单批、一个管多批）。
>   原行引用的行号是 007 时代的，020 之后 D 条在 `mysql.py:178`、映射在 `:375`/`:381`，别再照抄去定位。
> - **要点那句"一批 200 表"**：读侧真分批（`collect()` 切片循环发 `IN (...)`），
>   用例把 `AIWEB_EXTRACT__BATCH_SIZE` 降到 3，在 11 个对象的真演示库上跑出 4 批——
>   否则"分批"这条路径在真跑里永远走不到，只能算离线自证。
>   **as-built(P3-020)：那个"11"是 `SHOW FULL TABLES` 的行数**（10 个业务对象 + 建库脚本自己的
>   `_aiweb_demo_marker`），而同一条范围过滤排除 `_%` 后同步看到的是 **10**（9 表 + 1 视图，
>   verification §1/§1.2 的口径）。所以 4 批这个结论不变，只是把两个数写清了：11 是夹具的行数、
>   10 是抽取的分母，刀口是 `[3,3,3,1]`。批大小 200 在这 10 个对象上只有一批，所以"默认值不改"
>   与"真跑必须走到分批"这两句靠用例把键压到 3 来同时成立。
> - **本片新接的 P2 转账**：`?force=true` 端点开关（连同 `SCOPE_REMEDIES` 第三条文案换回原文）。
>   **本片新做的回收作业**：只清 `sync_job_event` 旧行（`AIWEB_RESULT__RETENTION_DAYS`，30 天），
>   结果 csv 不碰。**本片明确不做**：`POST /sync/jobs/{id}/cancel`（连 `cancel_requested` 列一起挂 [P8]，
>   `cancelled` 继续无写入点）、枚举 NDV 采样（整块推 P4，只新增 `AIWEB_EXTRACT__SAMPLE_ROW_LIMIT` 键，
>   `sample_distinct*` 两键挂 [P4]，`meta_column.sample_values` 继续无写入点）。
> - **测试地基**：P2 的三张 sync 用例（`test_sync_live.py` / `test_sync_pg.py` / `test_sync_cache_pg.py`）
>   现在都在请求内等 `run_sync` 返回，必须改成"POST 拿 job_id → await worker 的循环体 → 照旧断言"；
>   用例**不真起子进程**，SSE 与进程边界另有真进费用例钉。

踩坑预警：① 5.7 的 `STATISTICS` 对 MyISAM/视图语义不同，视图要单独分支（列注释全空 → 卡片降级模板）；
② 别把源库读和元数据库写混在一个 session；③ `heartbeat_at` 必须在**独立连接**上更新。

#### P3 验收收口（as-built(P3 交付)，工单 025）

上面七条逐条从"拍板过的话"变成"跑出来的证据"。每条给的都必须是**独立真相源**——
一个数、一份 golden、一段实跑输出——"用例绿了"不算（P2 数过四类假绿，模式都是形状测试原理上看不见）。
本轮整轮闸的输出（`dev.ps1 check` 全绿 = **980 passed / 2 skipped**，另手工补 `ruff format --check` 与
`mypy app` 两条）逐字在 verification §1.7.2，基线对照与"两个 skip 没变多"的账也在那里。

| 验收 | 结论 | 独立真相源（用例文件 + 真跑输出） |
|---|---|---|
| 1 `POST` 回 job id | 交付（016） | 键：`tests/integration/test_sync_enqueue_pg.py::test_发起同步立刻回_202_而那一轮还没开始跑`（响应体只带 `job_id` + 库里 `pending`/`started_at IS NULL` + 抽取器零调用）。实录：verification §1.7.1 `409` 段前两行 |
| 2 SSE 帧序 + `total=10` | 交付（017/020） | 键：`tests/unit/test_sync_stage_vocabulary.py`（9 值→5 档穷举）、`tests/integration/test_sync_events_sse_pg.py`（帧内容、游标补读、鉴权、叫醒）、`test_sync_events_live.py`（跨进程进入方式 + 真分母）。实录：§1.7.1 `timeline` 段五帧 `id` 1..5、首帧 `total=10, base_table=9, view=1`（**不是** `SHOW FULL TABLES` 的 11） |
| 3 再发一次 409 | 交付（016） | 键：`test_sync_enqueue_pg.py::test_排队中的_pending_行也挡住新作业`（把 `ux_sync_running` 的 WHERE 只剩 `running` 即红）。实录：§1.7.1 `409` 段——**worker 还没起**，挡人的只能是索引不是代码 race |
| 4 kill 后回收 + 重新同步 success | 交付（018） | 键：`tests/unit/test_worker_reclaim.py`、`tests/integration/test_worker_heartbeat_pg.py`。实录：§1.7.1 `killbatch` 段，`[worker] 回收 1 个僵尸 job：[21]` → `errors=[{'code':'reclaimed','detail':'heartbeat 超时'}]` → job 23 `status=success progress=100.00`。全程零手工 SQL、worker 是真子进程且**被当场 `kill()`**（这一格用例给不了：用例不杀自己起的进程） |
| 5 三表抛错 → `partial` | 交付（019） | 键：`tests/integration/test_sync_card_isolation_pg.py::test_注入三张表卡片构建抛错_终局partial_errors含三张表名_其余五张在场`（注入钩子走真 HTTP→真入队→`run_once` 真跑；同文件另有"排在后面的表失败时前面已提交的卡片仍查得到"与"注入前后 `meta_*` 行数一行不差"两条，钉的就是逐表提交与"失败隔离不漏到元数据侧"）。口径提醒：抛错点在 `card_build`（本机 embedding 三键全空，`embed` 档是 `skipped`），元数据侧仍每库一事务。原验收那句"其余 **6** 张"是 019 之前的夹具形状，如今这条用例的账是 3 失败 + 5 在场 |
| 6 PG 数据源跑通 + 类型归一化 | 交付（023/024） | 键：`tests/unit/test_pg_type_normalize.py`（七类原料）、`tests/unit/test_extract_pg_map.py`/`test_extract_pg_client.py`、`test_sync_dialect_dispatch.py`（未知 kind 才抛 501）、`tests/integration/test_extract_pg_live.py` 4 条。实录：计数与逐位清单见 verification §1.6.1（10 对象 / 82 列 / 14 索引 / 15 索引列 / 6+2 关系 / 10 卡片）。**未证到的那一组只有 §8.2 末注"证到哪一步"一份**，别处只指它 |
| 7 `CARDINALITY`/`SUB_PART` 非空 | 交付（007 通、020 补分批那格） | **键（这一条的正身）**：`test_extract_mysql_live_batches.py` 的两次真跑比形状——按 `(表名, 索引名, SEQ_IN_INDEX)` 排序的 `(列名, SUB_PART, cardinality 是否非空)` 清单逐位相等，再对分批那一次断每行非空。**实录给的是快照，给不了"每批都读到"**：`out_probe_b.txt` 抓的是 21:04 那一刻
ds=2 的 `meta_index` 现值（27 棵全非空 + 唯一那处前缀索引 `product.idx_product_name(name(32))` 的
`sub_part=32`），而 §1.7.1 那个 `batches=4` 的 `killbatch` 轮次到 21:11 才跑——**这份快照里那 27 行是
默认批大小那一次（job 17/20）写进去的**。所以它背的是"落库的形状对"，"切成四批后每批还读得到"只有那条
live 用例答得出，两件事实不互替。同一份探针另有一格
`funcdef_rows=0`，正是 024 末注那个"表达式索引的文本有列可存但 `RawIndexColumn` 不搬它"的缺口 |

三条不在表里、但收口时必须一起写的账：

1. **`sync_jobs.progress` 从此有写入点**（工单 001 起就悬着的那格）：`sync_service.progress_of()`
   把事件帧里的 `{done,total}` 折成百分比，唯一调用点在 `_set_phase` 写那一行的同一条 `UPDATE` 里；
   `total<=0` 时**不写这一格**（分母还没定出来，写 0.0 等于替 `DEFAULT 0` 撒一次谎）。
   钉子 `tests/unit/test_sync_progress.py`；上面第 2、4 行实录里的 `progress=100.00` 就是它写进去的。
   `pending` 的写入点归 016（见第 1 行）。**`cancelled` 仍然一个写入点都没有**——`cancel` 整块挂 [P8]，
   本节不许把它算成已闭。
2. **`AIWEB_EXTRACT__SAMPLE_ROW_LIMIT` 这一键从未落地**：spec 故事 24 与本节 as-built 那句
   "只新增 `AIWEB_EXTRACT__SAMPLE_ROW_LIMIT` 键"是要 P3 加的，`app/settings.py` 的 `ExtractGroup`
   里没有它、`.env.example` 与 roadmap §4 分组清单里也只是文档字面。收口判**不补**：
   补一个没有读取点的键正是 P2 那张账抱怨的东西，它该和 NDV 采样三步一起在 P4 一次落地。
   已记进 metadata-model §6 末 as-built(P3 收口) 的九键清单。
3. **`?force=true` 的写法与实装不一致，本轮统一措辞**：`SCOPE_REMEDIES` 第三条文案
   （`admin 用 ?force=true 覆盖上限`）是被钉的字面量，不改；文档提这一档时改说
   "`force` 开关（HTTP 请求体字段）"，不为文案另开一条查询参数入口（两条通道并存是本项目禁止的）。
   见 verification §1.7.1 的 `force` 段末注与 `architecture.md` §7 那行的 as-built。

#### P3 用户故事对账（spec 的 24 条逐条，工单 025 验收 5 的规格轴）

上面七行是 roadmap 的验收，这一张是 spec `docs/specs/2026-09-30-p3-production-sync.md` §3 的用户故事——
两套编号**不是一一对应**（故事 3/4/5/15/17 在七条验收里没有自己的格子）。列在这里的唯一目的是
让"哪条故事只有用例、没有真跑"这件事在收口时看得见。**"实录"那一栏空着就是空着**，不拿用例绿了填。

| 故事 | 用例 | 实录（§1.7.1）或"没有" |
|---|---|---|
| 1 202 + `pending` + 响应先回 | `test_sync_enqueue_pg.py::test_发起同步立刻回_202_而那一轮还没开始跑` | `409` 段第 2、3 行 |
| 2 409 判据是索引 | 同上 `::test_排队中的_pending_行也挡住新作业`（改索引 WHERE 即红） | `409` 段那两行 `→ 409`（此刻**零 worker**） |
| 3 claim 唯一 + 领最老 | `::test_同一个作业不会被两个_worker_领走`、`::test_claim_领走最老的_pending_并把这一轮的钟点上`、`::test_claim_出来的顺序就是入队的顺序` | **没有**：本机同一时刻只常驻一个 worker，两个 worker 各拿一件只在用例里发生过（`明确不做`那条"不开多个 worker"是本机运行形态，不是队列语义） |
| 4 worker 不吃 token，终局写在 `finally` | `::test_worker_跑完之后终局那三样都对`；`sync_service.py:1158-1166` 那条 `finally` | `force` 段：job 19 抛 `ExtractScopeTooLarge`，worker 打"异常（终局已由 run_sync 写好）"而库里那一行仍是 `failed` + `errors` 在场。worker 进程从头到尾没有登录步骤（实录里只有 API 侧登录，worker 子进程的环境变量只有 DB 连接串与 `MAX_TABLES`） |
| 5 `force` 覆盖上限 + 三处文案一起动 | `test_extract_mysql_client.py` 的 `SCOPE_REMEDIES` 字面 + 021 的端点透传用例 | `force` 段整段（`failed` 与 `success` 各一次，`errors[0].data.remedies` 三条原文可见） |
| 6 每次状态变化一行事件、`seq` 单调、`NOTIFY` 只带 `job_id` | `test_sync_events_pg.py::test_一次同步把每一步落成事件行_stage_序列单调不倒退`、`test_sync_events_sse_pg.py::test_NOTIFY_叫醒不必等心跳就把那一帧送出来` | `timeline` 段的 `id: 1..5` 与读库那行 `事件=[…]` |
| 7 SSE 帧序与 `total=10` | `test_事件流把每一行推成一帧_五键齐在场_收尾是_done`、`test_事件_counters_带全五键_分母来自带_scope_的那次计数` | `timeline` 段首帧 `total=10, base_table=9, view=1` |
| 8 游标续读一条不丢 | `test_重连按游标续读_一条不丢也不重复` | **没有**：实录是一次连到底，没演过断开重连 |
| 9 每 15s 一帧 `: ping` | `test_没有新事件时每窗吐一帧_ping`（把 `ping_after_s` 设成 0.02 逼出来）+ 常量 `PING_INTERVAL_S == 15.0` 单钉一次 | **没有**：实录里作业 0.03s 就结束了，永远等不到 ping 窗口 |
| 10 细/粗词表只有一个映射函数 | `test_sync_stage_vocabulary.py`（9 值穷举、逐条写死）。"grep 不到第二处"本轮真 grep 过：`stage_of` 全仓只有定义 `sync_vocabulary.py:36` 与**一个**调用点 `sync_service.py:675`，其余出现是 DDL 的 `CHECK` 字面（`models/meta.py:349`）和写细值的 `phase` 实参 | `timeline` 段每帧的 `stage`/`phase` 成对（`extract`/`discover`、`upsert`/`tables`…） |
| 11 `embed` 档 `skipped=true` 而作业仍 `success` | `test_embed_档未配置时以_skipped_出现_作业终局仍是_success` | `timeline` 段 `id: 4` 那一帧与紧随的 `event: done` `status: "success"` |
| 12 心跳在独立连接、主事务未提交即可见 | `test_worker_heartbeat_pg.py::test_主事务未提交时心跳换个连接就已可见` | `killbatch` 段打断那一行的 `heartbeat=…13:11:03.688542+00:00`（另一条连接读到的） |
| 13 每轮扫僵尸：`failed` + `errors` 追加 `reclaimed` | `test_worker_reclaim.py`（谓词与条目字面对上文档）+ `::test_一轮循环把心跳超时的_running_改判_failed_并补上终局事件`、`::test_边界_179秒不动_181秒判失败_pending_也算僵尸候选` | `killbatch` 段的回收日志与 `errors=[{'code':'reclaimed',…}]` + `回收补的事件：[(1,'done','done')]` |
| 14 杀 worker → 重启 → 重新同步 `success`，零手工 SQL | 用例给不了这一格（用例不杀自己起的进程） | `killbatch` 段整段：`kill()` 真子进程 → 409 → 12s 后 worker2 回收 → job 23 `success` |
| 15 保留期删 `sync_job_event` 旧行，csv 一个字节不动 | `::test_保留期删过期事件行_未过期不删_结果csv逐字节不动` + `test_worker_reclaim.py::test_保留期只删事件表_天数走参数` | **没有**：30 天窗口在演示库上造不出历史行（脚本也不写库），这一格只有用例背书 |
| 16 `BATCH_SIZE=3` 在真库跑出 4 批、批数从事件流里断 | `test_extract_mysql_batching.py`（11 条）+ `test_extract_mysql_live_batches.py`（2 条）+ `test_sync_events_pg.py::test_分批的两个配置键真有读取点_每批的表名随_tables_帧交回` | `killbatch` 段 `batches=4`、`[3, 3, 3, 1]`。**偏离**：故事原写"11 个对象"，实测分母是 10（`exclude_tables=["\\_%"]` 排掉标记表），所以末批是 1 不是 2 |
| 17 元数据侧仍"每库一事务"，粒度不许放宽 | `test_sync_cache_pg.py::test_部分失败只清提交完的那个库`/`::test_一个库都没写成时一个键都不清` + `test_sync_events_pg.py::test_upsert_事件与元数据同事务_失败的库一条事件都不留` | 没有也不需要：本机演示只有 1 个 schema，多库那一面由那三条用例自己在元数据库里造出来 |
| 18 注入 3 张表 `card_build` 抛错 → `partial`、其余在场 | `test_sync_card_isolation_pg.py::test_注入三张表卡片构建抛错_终局partial_errors含三张表名_其余五张在场` + `::test_卡片失败隔离不漏到元数据侧_注入前后meta行数一行不差` | 无实录（019 的口径是"注入钩子走真 HTTP→真入队→`run_once`"，但那仍在测试进程里） |
| 19 已提交的卡片不在回滚射程内 | `::test_排在后面的表失败_前面已提交的卡片仍查得到` | 同上 |
| 20 PG 演示源开通 → 登记 → 同步跑通 | `test_extract_pg_live.py` 4 条（真登记、真入队 202、真 SQL、回读元数据库） | 开通那一轮由用户以超管跑 `scripts/demo_pg.ps1`（verification §1.5/§1.5.4 的 34 行 PASS），对账数字在 §1.6/§1.6.1 |
| 21 五条 SQL 照 §8.2 原文 + 保序那件真跑事项 | `test_extract_pg_map.py`（14）/`test_extract_pg_client.py`（16）+ live 的复合索引对位断言 | §1.6.1。故事点名的 `indkey::int[]` 那一格按它给的二选一收口：**没证到**，所以 §8.2 的表述已改成与 SQL 原文一致（见 metadata-model §8.2 保序那句的 as-built(P3 收口)） |
| 22 PG 类型归一落抽取层 | `test_pg_type_normalize.py` + `test_type_domain.py`（两方言共享的断言表） | §1.6.1 的 `data_type`/`raw_data_type` 逐列对账 |
| 23 MySQL 侧同片归一 + 8 份 golden 重录 | `test_mysql_type_normalize.py` + `test_kb_card_golden.py` | 真跑侧在 `test_sync_live.py` 最后一张（演示库每列过共享值域 + 八列点名成对比）；golden 的重录差异由 023 交付记录背书 |
| 24 `CREATE_TIME`/`UPDATE_TIME` → `last_analyze_at` | MySQL 半边 022、PG 半边 024（`test_extract_pg_map.py` 的"取较晚者"）。**PG 那一支的真库样本是零**：本轮只读实测 7 张只有 `last_autoanalyze`、3 张两枚都空、0 张同时在场（metadata-model §8.2 末注"证到哪一步"） | §1.6.1 逐张比 `meta_table.last_analyze_at` == 源库两枚里较晚的一枚 |

最后一栏里有四格写的是"**没有**"（故事 3、8、9、15），这是本节的结论而不是遗漏：那四格的断言在形状上或时序上需要"两个 worker""中途断开""等满 15 秒""30 天前的行"，而一次开发者手测给不了
其中的两个。它们都有用例钉着，收口不把它们写成"真跑过"。

### P4｜pgvector 混合检索完整版 + index profile 原子切换（5 人日）

要点：embedding 回填走 `kb_index_profile`（`model/dimension/template_version/status(draft|active|retired)`），
active 切换用一条 `UPDATE ... WHERE id=:new` 的原子语句；检索按
全局 top-80 → 权限过滤 → 关键词路(trgm + 精确标识符 + simple FTS) → RRF(k=60) → 聚合到表 →
JOIN 桥表扩展(hops=2) → token 预算裁切。`networkx` 只建/缓存 JOIN 图（`MultiDiGraph`，
边带 `confidence`：FK=1.0、命名约定按 architecture §5.3 的加权公式），BFS 自己实现以便单测。
as-built(P2-0010)：这里原写"命名约定=0.7"，那是常数口径，与 §5.3 的公式互斥——0.7 配
"≥0.8 才进 prompt"会让推断边永远进不了 prompt。冲突已由工单 010 在**写侧**闭环
（`relation_infer.py` 按公式算 confidence，0.7 降为公式地板）；本节这张 JOIN 图本身
（hops=2 桥表扩展、环、不可达标记）仍属 P4，落点是工单 014。
as-built(P2-0014)：**这张图提前到 P2 由工单 014 交付了**（010 只渲染直连边，跨表问答没有可执行路径可用），
所以本节这一串不再是 P4 的活；两处口径在开工前拍板并已回写 architecture §5.3：
① `networkx` **照本句原文引入**（`MultiDiGraph` 建图 + 跨请求缓存，BFS 仍自己实现以便单测），
② `hops` 数的是**桥表张数**而不是边数，跳数定上限、边权重只在上限内定胜负；
图**存方向、按无向扩展**（真实 FK 恒为子→父，按有向可达则演示库除"子→父"一跳外一律不可达）。

验收：
1. `POST /api/kb/reindex` 建 draft → 进度 SSE → `activate` 后 `kb_index_profile` 只有一个 active；
2. `POST /api/kb/search` 返回带 `vector_rank/keyword_rank/rrf_score/aggregated_tables/hops_used` 的诊断结构，
   "销售额"能命中 `payment_record` 与 `order_main`；
3. `HNSW_EF_SEARCH` 从 64 改 400，召回集合是超集（单调性检查，防 ef 参数没传下去）；
4. 无 `AIWEB_PG_TEST_DSN` 时 RRF/JOIN 单测仍跑（纯内存），有 DSN 时集成测试额外跑；
5. 换 `bge-large-zh`(1024) 重建 profile：draft 是 `vector(1024)` 的影子列/影子表，激活后旧向量仍在（可回滚）；
6. `TOKEN_BUDGET=1000` 时送进 prompt 的表数显著变少且**不报错**，日志记录被裁掉的表名。
   > **as-built(P2-010)**：这条的能力（按卡片段贪心装填 + `logger.info` 记被裁表名）已由 010 提前交付，
   > 断言在 `tests/unit/test_prompt_builder.py`；P4 复验时只需把预算换成 1000 再看一次裁切，
   > 不要重复实现一遍裁切器。

踩坑预警：① **换模型维度必须新表/新列**（`ALTER TYPE` 不支持）；
② `SET LOCAL hnsw.ef_search` 必须在事务里，SQLAlchemy 惰性 BEGIN 会让它变成 no-op；
③ trgm 对中文短串分数极低，阈值要降到 0.1 以下或改走 `ILIKE` 精确路——别指望 trgm 做中文语义；
④ 权限过滤放在向量 top-80 之后会出现"过滤后为空"：需要在向量 SQL 里就 `JOIN grant` 过滤，
应用层过滤只作二次保险。

### P5｜SQL 守卫加固 + 三层防御收口（3.5 人日）

要点：按已定稿实现（见 nl2sql-safety.md），额外补三件事：
(a) 每条拒绝带 `rule_id`（枚举化，便于统计模型常犯哪类）；
(b) 守卫**执行的是重生成的 SQL**，并断言"重生成后再 parse 一次的 AST 与首次 parse 的 AST 归一化相同"；
(c) dry-run 失败信息回喂模型自动重试一次（`retry_with_error`），最多 1 次。

验收：
1. `pytest tests/guard -q` 全绿（含攻击语料全部）；
2. `POST /api/chat/validate {"sql":"SELECT 1; DROP TABLE users"}` → 400 `{code:"sql_guard_rejected", rule_id:"multi_statement"}`；
3. 不带 `LIMIT` 的 `SELECT * FROM order_item` → 执行日志见 `LIMIT 1001`、返回 1000 行 + `truncated:true`；
4. `SELECT SLEEP(20)` 被黑名单拒；**清空黑名单**后被 `SET SESSION MAX_EXECUTION_TIME=1000` 截断
   （错误来自驱动 timeout 而非守卫）→ 证明分层独立有效；
5. 用非只读账号连数据源时 `test_connection` 报 `readonly_capability_missing`（能力探测前置）。

踩坑预警：① **`comments=False` 会把 hint 一起剥掉，造成"守卫看到的 ≠ 执行的"** → 检测到 hint 直接拒；
② `PROCEDURE ANALYSE()`、`INTO @var`、`FOR UPDATE` 都要单独规则，sqlglot 宽容 parser 可能不识别子结构 →
必须有正则级兜底；③ `SET SESSION TRANSACTION READ ONLY` 前不能在已有事务里；
④ asyncmy 的 `read_timeout` 与 `MAX_EXECUTION_TIME` 谁先触发要实测。

### P6｜多用户与数据源级权限贯通（3 人日）

要点：`require_role("admin")` 依赖工厂；grants 生成 `EXISTS` 过滤片段注入向量与关键词两路；
执行前二次校验"SQL 引用的表 ⊆ 该用户可访问表"（守卫白名单来源就是它）；跨用户会话不可见；
`chat_message` 记录 `actor_id`。

验收：
1. member 问"查一下管理员账号表的密码" → 返回"无相关可访问数据"，日志 `permission_filtered_tables>=1`；
2. member `POST /api/chat/validate {"sql":"SELECT * FROM secret_table"}` → 403 `table_not_granted`；
3. admin 撤销 grant 后 60s 内（配置缓存 TTL）member 再问同一问题不再命中该表（明确测出缓存失效路径）；
4. 两个用户并发问数，各自 SSE 的 `referenced_tables` 不相交。

踩坑预警：`app_settings`/profile 的进程内缓放大了权限不生效问题——所有带权限语义的缓存键必须含
`user_id`，且 grant 变更时 `bump_version()`。

### P7｜前端基座（6 人日）

要点：axios 拦截器统一 `code/message` → ElMessage，401 触发 refresh token 单飞（in-flight promise 复用）；
Element Plus 用 `unplugin-vue-components` + `ElementPlusResolver` 按需引入；
`useSse()` 复用给"同步进度"和"chat"两处；token 用 Pinia persist 到 **sessionStorage**（非 localStorage）。

验收：
1. `npm run build` 过 `vue-tsc --noEmit` 且无 TS 错误；
2. 刷新页面 token 不丢；退出后 router guard 挡住 `/datasources`；
3. 数据源表单填错口令时展示后端 `detail`，且**已填口令不会回填到任何 GET 响应**（Network 面板核对）；
4. 详情页点"立即同步"，进度条实时增长（**这一步就是 P8 的 SSE 前哨测试**）；
5. 错误 token 访问 `/api/admin/*` → 跳 403 页而不是白屏。

踩坑预警：`el-table` 在 60+ 列宽表会卡 → 虚拟滚动或"前 20 列 + 横向滚动 + 列选择器"；
`import type` 与 `verbatimModuleSyntax` 组合易报 TS 错。

### P8｜Chat 页：SSE 流式 + SQL 面板 + 表格/图表 + 结论（6 人日）

事件契约见 architecture.md §6（定死，前后端各写一半再联调）。

验收：
1. `curl -N -H "authorization: Bearer $T" -H 'content-type: application/json' -X POST localhost:8000/api/chat/ask
   -d '{"question":"近30天每日订单数趋势"}'` 输出严格按契约顺序、最后一行 `event: done`，
   且 `guard` 帧出现在 `executing` 之前；
2. 浏览器 DevTools EventStream 能看到逐帧推进（不是等 3s 后一次性出现 → 反证 Vite `compress:false` 生效）；
3. 手改 SQL 面板为 `DELETE FROM orders` 点重跑 → 显示 `sql_guard_rejected/mutation_not_allowed`，**不产生新 result**；
4. 中途关浏览器（`AbortController`）→ 后端日志出现客户端断开且不留 running 执行
   （`asyncio.CancelledError` 正确传导、无连接泄漏：`SELECT count(*) FROM pg_stat_activity` 稳定）；
5. `chart_advisor` 单测 + UI 双证：时间列+数值 → 折线、类别(NDV≤12)+数值 → 柱、两个数值 → 散点、单值 → 数字卡；
6. 同一问题二次提问 `done.latency_ms` 因缓存/few-shot 命中而下降，历史页能翻出上一轮并追问。

踩坑预警：① **SSE 三件套**（Vite `compress:false` / 后端排除 gzip / 15s ping）少一个就表现为"卡住"；
② `StreamingResponse` 里抛异常要在 generator 内 `try/except` 转成 `error` 帧；
③ 结论 prompt 不能塞全量行（1000×20 会爆），传摘要 + 前 50 行；④ ECharts 在 `v-if` 卸载后再挂载忘 `dispose` 导致内存涨。

### P9｜知识库运营：元数据浏览 / 术语 / few-shot 采纳 / 历史 / admin（4 人日）

要点：卡片文本可编辑（`template_version` 不变但 `content_hash` 变，标"人工修订"，重算该表 embedding）；
术语表注入 prompt 独立段；反馈 `adopt_as_example` → `few_shot` 表，命中路径是**检索阶段的问题向量近邻**
（不是字符串相等），示例注入 token 单独预算（≤1500）。

验收：
1. 元数据浏览能看到某表字段/索引/FK/卡片全文；编辑卡片 → `POST /api/kb/reembed {table_id}` → 再检索，
   该表排名上升（量化断言，不是"看起来好了"）；
2. 点"采纳为示例" → `few_shot` 新增一行；再问**换了措辞的同类问题**，SSE 的 `retrieval` 帧里
   `few_shot_hit=true`，`prompt_builder` debug 日志列出注入示例 id；
3. 检索调参页改 `rrf_k=20` 保存 → 立刻 `POST /api/kb/search` 排名变化（证明 L3 热加载通了），改回 60 复原；
4. admin 页可建 member、授数据源、看同步历史、看到启动自检红黄绿。

### P10｜验证收口（3 人日）

验收：`make check` = ruff + mypy + pytest 全绿；未设 `AIWEB_PG_TEST_DSN` 时 pg 用例干净 skip 而不是 error；
照手测清单（verification.md）从 seed 到二次命中走一遍全通；
新人只读 README 能在 30 分钟内跑起服务。

## 3. 配置四层归属

| 层 | 存放位置 | 判定标准 | 热加载 |
|---|---|---|---|
| **L1 密钥 / 引导材料** | 仅 `backend/.env`（+ 生产环境变量注入） | 泄露即可解密全部数据源口令、可伪造任意用户 token、可直连元数据库、可计费 | 否，改后重启 |
| **L2 部署参数** | `backend/.env` | 启动前必须已知、且 DB 尚未建立时就要用到（连接池、端口、schema 名、日志级别） | 否 |
| **L3 运行参数** | `aiweb.app_settings`（KV + JSONB，UI 可改） | 用户/管理员会在页面上调的东西（模型名、temperature、检索权重、token 预算、采样开关） | 是（`settings_service` TTL 60s 缓存） |
| **L4 数据源级** | `aiweb.data_sources.params` JSONB + 加密凭据列 | 天然按数据源不同（口令、库名、超时、行数上限、允许表清单） | 是 |

**一条硬规则**：L3 是 **env 的覆盖层而不是替代层** —— `app_settings` 里每个键都必须有 `settings.py` 的
env 默认值作为地板，且 env 里出现 `AIWEB_*_API_KEY` 时永远优先（避免有人在 UI 里填 key 导致密文落库）。
**任何密钥类字符串一律禁止进 `app_settings`**（admin 端点对 value 做
`^(sk-|.*api_key.*|.*password.*)$` 拒绝写入并返回 422）。

`app_settings` 表并入既有迁移（不新开链分叉），列形如
`key TEXT PK / value JSONB / value_type / updated_by / updated_at / is_secret bool(default false, 恒 false)`。

`settings.py` 的关键写法（已落地部分见 `backend/app/settings.py`）：

```python
model_config = SettingsConfigDict(
    env_file=(".env", "../.env"),      # 前者优先；测试可用 monkeypatch/env 覆盖
    env_file_encoding="utf-8",         # 中文 APP_NAME 必须有 utf-8
    env_prefix="AIWEB_",
    env_nested_delimiter="__",         # AIWEB_PG__PASSWORD / AIWEB_EMBEDDING__DIMENSION
    env_parse_none_str="",             # 空串视为未设置，避免 Optional 字段被写成 ""
    extra="ignore",
    case_sensitive=False,
    revalidate_instances="always",     # L3 覆盖后重跑校验器
)
```

- **禁止把带 `password` / `api_key` / `secret` 字样的字段输出**：含密 model 覆写 `__repr__` 为 `redacted`，
  `core/logging.py` 装 `RedactFilter`（键名正则 + 值形态正则双保险）。
- **`settings` 不做全局单例突变**：用 `@lru_cache def get_settings()` + `deps.py` 注入，
  测试里 `get_settings.cache_clear()`；L3 覆盖走 `settings_service.get_effective(key, default=settings.x)`。
- 嵌套组字段名 `schema_name` 用 alias `SCHEMA`，既避开保留字风险，也让 env 键保持 `AIWEB_PG__SCHEMA` 的好看形式。
- prod 硬规则（model_validator）：CORS 不得为 `*`；SQL 守卫与 dry-run 不可关闭；禁止记录 prompt。

## 4. env 键清单（设计期全量）

```dotenv
########## 分组 1：应用 ##########
AIWEB_APP_NAME=AI 问数
AIWEB_ENVIRONMENT=local                # local | staging | prod；prod 会触发更严的启动自检
AIWEB_HOST=127.0.0.1                   # 本地开发只绑回环，避免局域网直连
AIWEB_PORT=8000                        # 与前端 .env.development 的 proxy target 必须一致
AIWEB_RELOAD=true                      # 本地 uvicorn --reload
AIWEB_WORKERS=1                        # >1 时 SSE 心跳与 sync job 单例互斥仍靠 DB 保证，但 reload 必须关
AIWEB_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
AIWEB_BASE_PATH=                       # 反代子路径（留空=根）
AIWEB_TIMEZONE=Asia/Shanghai           # 必须与 PG server tz、MySQL @@session.time_zone 一起对齐
AIWEB_REQUEST_ID_HEADER=X-Request-ID
AIWEB_BODY_LIMIT_BYTES=1048576         # 1MB，防大 prompt 注入
AIWEB_SQLITE_EVENTLOOP_PATCH=true      # Windows: 强制 SelectorEventLoop（见 §9 踩坑）
AIWEB_SERDE_DATE_FORMAT=%Y-%m-%d       # 前端结果表格与 prompt 内日期串一致
AIWEB_EXPLAIN_ON_STARTUP=true          # 启动打印生效配置（脱敏后）

########## 分组 2：元数据库 / 向量库（远端 PostgreSQL + pgvector） ##########
AIWEB_PG__HOST=<your-remote-pg-host>
AIWEB_PG__PORT=5432
AIWEB_PG__DATABASE=<db-name>
AIWEB_PG__USER=<app-user>
AIWEB_PG__PASSWORD=                    # 【必须 env，绝不入库不入 git】
AIWEB_PG__SCHEMA=aiweb                 # 应用自建 schema；SQLAlchemy MetaData(schema=...) 显式限定
AIWEB_PG__SSLMODE=require              # 远端 PG 默认 require；本地测试库可 disable
AIWEB_PG__POOL_SIZE=5
AIWEB_PG__MAX_OVERFLOW=10
AIWEB_PG__POOL_TIMEOUT_S=30
AIWEB_PG__POOL_RECYCLE_S=1200          # 远端 LB/云 PG 常有 idle 断连，必须 recycle
AIWEB_PG__CONNECT_TIMEOUT_S=10
AIWEB_PG__STATEMENT_TIMEOUT_MS=15000   # 应用自身查询的兜底超时（≠ 源库查询超时）
AIWEB_PG__SQL_ECHO=false
# 仅测试：缺失时所有 pg marker 用例 skip（见 §10）
AIWEB_PG_TEST_DSN=

########## 分组 3：LLM（OpenAI 兼容） ##########
AIWEB_LLM__BASE_URL=https://api.openai.com/v1
AIWEB_LLM__API_KEY=                    # 【必须 env】
AIWEB_LLM__MODEL=gpt-4o-mini           # 生成 SQL 主模型；DB 覆盖
AIWEB_LLM__SQL_MODEL=                  # 留空回落到 MODEL（允许"路由用小模型/生成用大模型"）
AIWEB_LLM__TEMPERATURE=0.1             # 问数场景必须低，DB 覆盖
AIWEB_LLM__TOP_P=1.0
AIWEB_LLM__MAX_OUTPUT_TOKENS=1600
AIWEB_LLM__TIMEOUT_S=60                # 单次调用；流式下这是 chunk 间隔超时的语义，需在 client 明确
AIWEB_LLM__MAX_RETRIES=2               # 只对 429/5xx/超时重试，且幂等（分类/生成 SQL 都是纯函数调用）
AIWEB_LLM__RESPONSE_JSON=true          # 上游不支持时自动降级为"提示词强约束 + json 容错解析"
AIWEB_LLM__ENABLE_THINKING=false       # 国产兼容端点常见扩展参数
AIWEB_LLM__PROBE_ON_STARTUP=true       # 启动时列 /models（无 key 的 CI 必须设 false）
AIWEB_LLM__STREAM=true                 # chat/ask SSE

########## 分组 4：Embedding ##########
AIWEB_EMBEDDING__BASE_URL=             # 留空=与 LLM 同 base_url
AIWEB_EMBEDDING__API_KEY=              # 【必须 env】
AIWEB_EMBEDDING__MODEL=text-embedding-3-small
AIWEB_EMBEDDING__DIMENSION=1536        # ⚠️ 只影响"新建 profile"，改值不会重算已有向量；>2000 启动即失败
AIWEB_EMBEDDING__OUTPUT_DIMENSION_PARAM=true   # 是否支持 dimensions 参数（3-small 支持；bge/v3 不支持→置 false）
AIWEB_EMBEDDING__BATCH_SIZE=64
AIWEB_EMBEDDING__MAX_INPUT_CHARS=6000  # 卡片超长时按段落截断，别按 token 截（tiktoken 离线不可用时更稳）
AIWEB_EMBEDDING__TIMEOUT_S=30
AIWEB_EMBEDDING__MAX_RETRIES=3         # 批量 embedding 更容易 429，重试比 LLM 多一次
AIWEB_EMBEDDING__PROBE_ON_STARTUP=true # 跑一次 1-token embed，锁定真实维度
AIWEB_EMBEDDING__CONCURRENCY=4

########## 分组 5：JWT ##########
AIWEB_JWT__SECRET=                     # 【必须 env】≥32 字节随机；openssl rand -hex 32
# 算法不是配置项：实现里钉死 HS256（app/core/security.py:_ALGORITHM）。
# 把它做成可配，等于把 "none" 也请进攻击面——所以本表删掉了 draft 里的 AIWEB_JWT__ALGORITHM。
AIWEB_JWT__ACCESS_TTL_S=900            # draft 里写的是 ACCESS_TTL_MIN=120：键名以 Settings 为准
AIWEB_JWT__REFRESH_TTL_S=86400         # 同上（draft 写的 REFRESH_TTL_DAYS）
# 下面四个 draft 键尚未实现，随"刷新令牌 / 登出 / 轮转宽限"一起做（当前无工单认领）：
#   AIWEB_JWT__ISSUER、AIWEB_JWT__AUDIENCE、AIWEB_JWT__CLOCK_SKEW_S、AIWEB_JWT__ROTATION_GRACE_S
# 没有刷新令牌时 access 只有 900 秒会很难受；那条工单落地时要么调大这里，要么上静默刷新。
AIWEB_BOOTSTRAP_ADMIN_USERNAME=admin   # seed_admin 使用（非密钥，可留默认）
AIWEB_BOOTSTRAP_ADMIN_PASSWORD=        # 【必须显式给】空则 seed_admin 拒绝执行，不建号
                                       # 值不能同行写注释：dotenv 会把 "# ..." 读成口令

########## 分组 6：Fernet（数据源口令加密） ##########
AIWEB_FERNET__KEYS=                    # 【必须 env】逗号分隔的轮换链：第一项加密，其余只解密
                                       # as-built：PREVIOUS_KEYS 语义已被这张列表吃掉（列表尾部即旧 key），
                                       # 故未单独实现；VERIFY_ON_STARTUP / REENCRYPT_ON_STARTUP 仍未实现，
                                       # 且当前无任何工单认领这两项启动期校验
AIWEB_FERNET__PREVIOUS_KEYS=           # 轮换后保留的旧 key（逗号分隔，只用于解密）——未实现，保留占位
AIWEB_FERNET__VERIFY_ON_STARTUP=true   # 加解密哨兵 + 统计库内不可解密的 credential 条数——未实现
AIWEB_FERNET__REENCRYPT_ON_STARTUP=false  # true=把所有密文用当前 key 重写一遍（一次性操作，跑完关掉）——未实现

########## 分组 7：抽取层 ##########
AIWEB_EXTRACT__BATCH_TABLES=200        # 与"批内一事务"配套；改大要看远端 PG 事务时长
AIWEB_EXTRACT__CONNECT_TIMEOUT_S=10
AIWEB_EXTRACT__TABLE_DENYLIST=information_schema,performance_schema,mysql,sys,pg_catalog,pg_toast,pg_stat_statements,TimescaleDB
AIWEB_EXTRACT__TABLE_ALLOWLIST=        # 空=按数据源 UI 勾选；非空视为全局硬白名单（用于超大库）
AIWEB_EXTRACT__SAMPLE_DISTINCT=false   # 枚举列 TOP-N 值采样：扫源库数据，默认关
AIWEB_EXTRACT__SAMPLE_DISTINCT_MAX_DISTINCT=30   # 只有 NDV<=该值才视为枚举列
AIWEB_EXTRACT__SAMPLE_ROW_LIMIT=1000
AIWEB_EXTRACT__CARD_TEMPLATE_VERSION=1          # 写进 kb_index_profile，模板变更触发重算
                                       # as-built(0008)：本行原示例值写作 `v1`，与 metadata-model §2.6
                                       # 的列类型 `card_template_version int` 对不上（`v1` 进 int 列会在
                                       # 同步落库时当场报错）。§5 的动词是"**升**版本"、profile 名里的
                                       # `tplv1` 才是带前缀的展示形态，所以实现按 int 走，示例值改成 1。
AIWEB_EXTRACT__HEARTBEAT_INTERVAL_S=10
AIWEB_EXTRACT__ZOMBIE_STALE_S=180      # heartbeat_at 超过此值判僵尸并在启动时回收
AIWEB_EXTRACT__CJK_WIDTH_AWARE=true    # 列名/注释含中文时的对齐与截断
AIWEB_EXTRACT__FORCE_FK_INFER=true     # 无外键时按 xxx_id 命名约定推 JOIN 边

########## 分组 8：SQL 守卫与查询执行 ##########
AIWEB_GUARD__STRICT=true               # false 仅供本地调试，prod 启动自检直接 fail
AIWEB_GUARD__MAX_STATEMENTS=1
AIWEB_GUARD__FORCE_LIMIT=true
AIWEB_GUARD__DANGLING_EXTRA_RULES=     # 逗号分隔追加黑名单函数（与代码内置取并集）
AIWEB_QUERY__MAX_ROWS=1000             # 返回给前端 + 喂结论模型的行数上限
AIWEB_QUERY__HARD_LIMIT=5000           # LIMIT(+1) 注入的值上限
# 上面这三键（FORCE_LIMIT / DANGLING_EXTRA_RULES / HARD_LIMIT）已被 nl2sql-safety §4.2、§4.3 的
# as-built 判为**不建**：强制 LIMIT 无条件、"拒绝一切 Unknown 函数"已是超集、注入值就是
# resolve_row_limit(ds)+1。此处保留只是分组清单的历史原貌，实现以 safety 为准。
AIWEB_QUERY__TIMEOUT_MS=8000           # 默认；数据源级可覆盖
AIWEB_QUERY__MAX_TIMEOUT_MS=30000      # 全局天花板，UI 不许超过
AIWEB_QUERY__CELL_MAX_CHARS=2000       # 超长文本列截断，防结论 prompt 爆
AIWEB_QUERY__DRY_RUN=true              # 关掉即失去第三层防御，prod 禁止关
AIWEB_QUERY__READONLY_SESSION=true     # SET SESSION TRANSACTION READ ONLY / default_transaction_read_only
AIWEB_QUERY__USE_SESSION_MAX_EXEC_TIME=true    # MySQL 走 SET SESSION max_execution_time，不走 hint
AIWEB_QUERY__RESULT_FLOAT_DIGITS=6
AIWEB_QUERY__NULL_DISPLAY=∅

########## 分组 9：检索 ##########
AIWEB_RETRIEVAL__VECTOR_TOP_K=80
AIWEB_RETRIEVAL__KEYWORD_TOP_K=80
AIWEB_RETRIEVAL__RRF_K=60
AIWEB_RETRIEVAL__VECTOR_WEIGHT=1.0     # RRF 加权（等权时保持原语义）
AIWEB_RETRIEVAL__KEYWORD_WEIGHT=1.0
AIWEB_RETRIEVAL__FINAL_TABLES=8        # 聚合后送入 prompt 的表数
AIWEB_RETRIEVAL__JOIN_HOPS=2           # JOIN 桥表扩展深度（as-built(P2-0014)：本键此前只是清单上的承诺、Settings 里没有读取点；014 补上，`hops` 数的是**桥表张数**而不是边数）
AIWEB_RETRIEVAL__MAX_JOIN_DEGREE=8     # §5.3 的"候选度 > 8"。清单里原本没有这一键，键名由工单 014 开工前拍板；一个键同时管写侧字段度（只告警不丢边）与图侧表度（只禁当桥不禁当端点）
AIWEB_RETRIEVAL__TOKEN_BUDGET=9000     # prompt 侧；与 LLM max_output 之和留出余量
AIWEB_RETRIEVAL__HNSW_EF_SEARCH=64     # 每次查询 SET LOCAL hnsw.ef_search
AIWEB_RETRIEVAL__TRGM_SIMILARITY=0.25  # 低于 PG 默认 0.3，中文短标识符更友好
AIWEB_RETRIEVAL__FTS_CONFIG=simple     # 远端无 zhcfg/pg_jieba 时的确定值
AIWEB_RETRIEVAL__MIN_VECTOR_SIM=0.15   # 低于此视为"无相关表"，直接反问而不是硬编 SQL
AIWEB_RETRIEVAL__FEW_SHOT_K=3
AIWEB_RETRIEVAL__TOKENIZER=cl100k_base # tiktoken encoding；模型不匹配时回退 heuristic

########## 分组 10：日志 / 可观测 ##########
AIWEB_LOG__LEVEL=INFO                  # dev 常用 DEBUG（但把 httpx 降到 INFO）
AIWEB_LOG__FORMAT=console              # console | json
AIWEB_LOG__FILE=logs/app.log           # 留空=只输出 stdout
AIWEB_LOG__ROTATE_MB=50
AIWEB_LOG__BACKUP_COUNT=5
AIWEB_LOG__SLOW_REQUEST_MS=1000
AIWEB_LOG__LLM_PROMPT_LOG=false        # ⚠️ true 会把卡片+样本+用户问题写进日志，prod 严禁
AIWEB_LOG__REDACT=true                 # 统一脱敏 password/api_key/credential/Authorization
AIWEB_LOG__ACCESS_LOG=true
AIWEB_METRICS__ENABLED=false           # 首版不引 prometheus，留位
```

### 4.1 已落地 `.env.example` 的差异（as-built）

`backend/.env.example` 当前实现的是上述清单的**子集 + 三条新增分组**，命名统一为
`AIWEB_APP__* / PG / LLM / EMBEDDING / JWT / FERNET / EXTRACT / RETRIEVAL / QUERY / RESULT / KB_DOCS / LOGGING`，
其中 `RESULT__*` 与 `KB_DOCS__*` 是结果文件与 markdown 覆盖层新增的两组（见 architecture.md §3.3、kb-workflow.md）。
值得注意的语义差别：

- `AIWEB_EMBEDDING__DIMENSION=0` 表示**关闭向量路**（不是"未填"），配合注释
  "未配置向量端点时检索走结构化路径，链路不断"。
  **as-built(P2-0005) 收窄**：这个键同时是 `kb_card.embedding` 的**列宽**（`vector(dim)`），
  `0` 不是合法列宽，所以建了 0005 之后 `DIMENSION=0` 会让迁移直接拒绝而不是"列留 NULL"。
  运行期的开关以 `embedding.configured` 为准（还要 base_url/key/model 齐备），
  本机 `.env` 因此写 **1536** 而不是 0：列宽定了、HNSW 建了，但没有端点 → 卡片照常构建、向量路不走。
  这条改动的代价写进 ADR-0003 的补注：**可插拔免掉的是运行期端点依赖，免不掉建库期的 `vector` 类型依赖**。
- `AIWEB_RETRIEVAL__CATALOG_DIGEST_MAX_TABLES=1000`（新增，L2 检索阶梯用）。
- **目录类配置的相对路径基准 = 仓库根**（`AIWEB_RESULT__DIR` / `AIWEB_KB_DOCS__DIR`，as-built(P2-013)）：
  `.env.example` 里写的 `data/results` 与 `knowledge` 是**相对仓库根**，在 `Settings` 装配点一次性
  resolve 成绝对路径，缺省值也走这个校验（`validate_default=True`）。写绝对路径则原样保留。
  拍板理由与后果见 safety §7 与 architecture §3.3——之前的现实是执行器按 cwd 走、体检脚本按
  `backend/` 的父目录拼，两处能互相错开都显示 ok。
- `AIWEB_QUERY__ALLOW_SQL_EDIT=admin`（把"手改 SQL 重跑"做成三态开关）。
- `AIWEB_APP__REQUEST_ID_HEADER=X-Request-Id`（大小写与设计稿的 `X-Request-ID` 以落地文件为准）。
- LLM 探活/重试等 `PROBE_ON_STARTUP`、`MAX_RETRIES` 类键在落地版里由 `scripts/check_env.py` 承担，
  不再是独立开关。

新增键、删减键都只影响 L2/L3 归属，不改变 §3 的分层规则。

## 5. 启动自检清单

每项产出 `CheckResult(name, level∈{fatal,warn,info}, message, hint)`，聚合到 `/api/health/detail`
（`/health` 仍返回轻量版，避免监控误报）。**只有 fatal 才拒绝启动**。

| # | 检查 | 做法 / SQL | 级别 |
|---|---|---|---|
| 1 | PG 连通与身份 | `SELECT current_database(), current_user, version(), SHOW server_version_num` | fatal |
| 2 | `vector` 扩展 | `SELECT extversion FROM pg_extension WHERE extname='vector'`；缺失时给"必须超级用户建扩展"的 hint（0.8.6 无 trusted） | fatal（as-built(P2-0005)：判据从"启用向量路"改成 **`AIWEB_EMBEDDING__DIMENSION>0`**——`kb_card.embedding` 的列类型要引用 `vector`，扩展不在则迁移必挂，而 `.env` 一旦按 P2 建表就必然 >0） |
| 3 | `pg_trgm` / `btree_gin` | 两者 trusted，应用自己 `CREATE EXTENSION IF NOT EXISTS` | fatal（trgm 缺失→warn 并自动降级关键词路） |
| 4 | HNSW AM 可用 | `SELECT 1 FROM pg_am WHERE name='hnsw'`（PG≥14 + pgvector≥0.5） | fatal |
| 5 | schema 可写 | `SELECT has_schema_privilege(current_user,'aiweb','CREATE')`；`CREATE SCHEMA IF NOT EXISTS` 试探 | fatal |
| 6 | 迁移到 head | `alembic_version` 表内容与代码内 `script.get_current_head()` 比对 | fatal |
| 7 | **维度三方一致** | `settings.embedding.dimension` == active `kb_index_profile.dimension` == `kb_card.embedding` 列的实际 format；任一不符 → **warn**（不能 fatal，否则换模型后系统彻底起不来），UI 顶部挂"需重建 profile"横幅 | warn |
| 8 | active profile 存在 | 无 active 且 `kb_card` 有 embedding 全 NULL 的行 → 提示跑回填 | warn |
| 9 | Fernet 合法性 | `MultiFernet` 加解密哨兵；逐条试解密库内 credential，失败者按 datasource id 列出 | 哨兵失败 fatal / 解密失败 warn |
| 10 | JWT | secret ≥32B、算法在白名单、`access_ttl + rotation_grace < refresh_ttl` | fatal |
| 11 | 时区一致 | `SHOW timezone`(PG) / `@@global.time_zone` vs `AIWEB_TIMEZONE`（相对时间问数错答的头号来源） | warn |
| 12 | LLM/Embedding 探活 | `GET {base}/models` + 一次 1-token embed，**用真实返回长度覆盖 `dimension`**（很多兼容端点声明 1536 实返 1024） | warn |
| 13 | sqlglot 冒烟 | `parse("SELECT 1", error_level=RAISE)` 成功 + 打印版本；顺手 `parse("SELECT /*!50100 1 */")` 确认注释被 `comments=False` 剥离 | warn |
| 14 | tiktoken 可加载 | `get_encoding(settings.retrieval.tokenizer)`，离线无缓存则 warn 并自动切 heuristic 分支 | warn |
| 15 | 僵尸 job 回收 | `UPDATE sync_jobs SET status='failed' WHERE status='running' AND heartbeat_at < now()-stale`；并断言"同数据源 running 互斥"的部分唯一索引存在 | info |
| 16 | 权限闭环冒烟 | 用假 user 走一遍 `retriever.search()` 的过滤分支，确保 grant 表为空时不会漏表（宁缺勿滥） | warn |

已落地的最小版本是 `backend/scripts/check_env.py`（配置形状、密钥、目录可写、PG 版本/扩展/schema、
LLM 探活、embedding 维度实测），`make check-env` / `dev.ps1 check-env` 即此入口。

## 6. Makefile / dev.ps1 目标表

两套目标名保持一致（Windows 上没 `make` 就用 ps1；`Makefile` 里 `SHELL := bash` 且 recipes 用 tab）。

| 目标 | 做什么 |
|---|---|
| `bootstrap` | `uv sync` + `npm install` + 生成 `.env`（从 example 复制并随机注入 `JWT_SECRET`/`FERNET_KEYS`） |
| `check-env` | `uv run python scripts/check_env.py`（自检，退出码非 0 表示有 fatal） |
| `migrate` | `uv run alembic upgrade head` |
| `migrate-new m="..."` | `alembic revision -m` + 手工填；**新迁移必须 import pgvector、必须带 down** |
| `seed-admin` | `uv run python scripts/seed_admin.py` |
| `demo-db` | 探测 `SHOW DATABASES LIKE 'ai_web_demo'` → 不存在才 pipe `init_demo_mysql.sql`（建库账号口令读 `backend/.setup/my_login.cnf`，见 verification.md §1.2 末） |
| `demo-db-pg` | 同上形状建 PG 演示源库 `ai_web_demo_pg`（P3 验收 6 的原料，口令读 `backend/.setup/pg_login.env`，见 verification.md §1.5.3）。**as-built(P3 收口)：只有 `make` 侧有这一条**，`scripts/dev.ps1` 的 `demo-db` 旁边还没有 `demo-db-pg` 分支——Windows 上要么用 `make demo-db-pg`，要么直接 `powershell -ExecutionPolicy Bypass -File scripts/demo_pg.ps1`（**本机的命令名是 `powershell` 不是 `pwsh`**：这台机器没装 PowerShell 7，PATH 上没有 `pwsh`——本节其余写 `pwsh` 的入口在本机都要换字面，Makefile 自己用的就是 `powershell`） |
| `dev-backend` / `dev-frontend` | 分别起 uvicorn / vite（`--loop asyncio`） |
| `dev` | ps1 版用 `Start-Process`/`Start-Job` 并行两个；Makefile 版提示开两个终端（不引 `concurrently`） |
| `worker` | `uv run python scripts/run_worker.py`——同步作业的执行体，**常驻第二个进程**（ADR-0011）。`dev` 里不含它：只起 API 时同步会永远停在 `pending`，那是缺消费者不是缺索引。**as-built(P3 收口)**：本表原本没有这一行，而两套脚本都已落地（`make worker` / `dev.ps1 worker`），是 roadmap 落后于脚本 |
| `lint` / `fmt` / `typecheck` | `ruff check . && mypy app` / `ruff format .` / `npm run typecheck`。**as-built(P3 收口)**：这一行两处比脚本超前——`lint` 两边（Makefile 与 `dev.ps1`）都**只跑 `ruff check`，没有 `mypy`**，所以 `mypy app` 至今没有闸目标背它；`fmt` 是**写式**的 `ruff format`（还带 `--fix`），仓库里没有 `ruff format --check` 这个目标。这两格今天靠提交前手工跑，本轮输出见 verification §1.7.2；把它们接进 `check` 归 P10 的 CI 那一片（§2 的"CI 形态"行本来就列了 `ruff format --check` 与 `mypy app`） |
| `test` | `uv run pytest`（自动 skip pg/live） |
| `test-guard` | `uv run pytest tests/guard -q`（每次改动必跑，<5s） |
| `test-pg` | `AIWEB_PG_TEST_DSN=... AIWEB_REQUIRE_PG_TESTS=1 uv run pytest -m pg` |
| `ask "问题"` | `uv run python scripts/demo_ask.py "问题"`（无前端时的调试主入口） |
| `sse-smoke` | 一段 `curl -N` + 计时，验证 SSE 帧不被缓冲 |
| `check` | `lint typecheck test`（提交前总闸） |
| `e2e` | `pwsh scripts/e2e_smoke.ps1`，按手测清单做可断言部分的自动检查 |
| `clean` | 清 `.venv/.pytest_cache/.ruff_cache/dist/logs`（**只清构建产物，绝不动数据库**） |

已落地版本（根 `Makefile` + `scripts/dev.ps1`）目标名为
`bootstrap / check-env / migrate / seed-admin / demo-db / dev / dev-backend / dev-frontend / worker /
lint / fmt / typecheck / test / check / clean`（P3 收口时按两套脚本各自的 target 清单重数过一遍，
原句漏了 `seed-admin`/`demo-db`/`worker` 三个已经在那儿的，又把 `demo-db` 算成"待补"）。
差额两处一并写清：`demo-db-pg` **只有 `make` 侧有**（`dev.ps1` 的 `demo-db` 旁边缺这一支），
而本表设计过却**两边都没有**的是
`migrate-new m="..."`、`test-guard`、`test-pg`、`ask`、`sse-smoke`、`e2e`——
`test-pg` 这一格尤其要说清：`make test`/`dev.ps1 test` 就是 `pytest`，pg 与 live 用例到底跑没跑
取决于环境里有没有 `AIWEB_PG_TEST_DSN`（读的是 `backend/.env`）。本轮整轮 **982 条**里
`-m "pg or live"` 选得出 **185 条**且全部真跑（唯一两个 skip 在 `tests/guard/test_corpus_mutation.py`，
与介质无关），所以"闸含 live"在本机成立；换一台没配 DSN 的机器就是 185 条 skip，
而 `test` 目标的输出不会为此多说什么——这是缺一个 `test-pg` 目标的真实代价，不是假绿。
`ask`、`sse-smoke`、`e2e` 仍随 P8/P10 补齐。

**as-built(P3 收口)：差额还有第三处，是脚本缺陷不是目标缺失**。两套脚本的目标名本轮逐个核过（`dev.ps1 help`
那三行与 Makefile 的 `.PHONY` 一致，`worker` 两边都在），但 **`dev` 这一条在本机只跑得起后端**：
`dev.ps1` 的 `Start-Process -FilePath "npm"` 报 `%1 is not a valid Win32 application`（`npm` 是 `.cmd` 垫片，
`Start-Process` 要的是 `npm.cmd`），而 `$ErrorActionPreference='Stop'` 让脚本在这一句就地抛出——后端那一行
在它前面已经 `Start-Process` 成功，所以进程还在、`Wait-Process` 没跑到。verification §1.7 的那一轮实录
就是这个形状（`api.log` 前两行是这条报错，紧接着 `Uvicorn running on http://127.0.0.1:8000`），
因为同步链路只要 API 与 worker。同处还有一个命令名字面要改：本机只装了 Windows PowerShell 5.1，
**PATH 上没有 `pwsh`**，所以本文件与 verification 里"`pwsh -ExecutionPolicy Bypass -File scripts/xxx.ps1`"
那种写法，在本机都得换成 `powershell`（Makefile 自己用的就是 `powershell`，两套脚本在这里本来也不一致）。
025 判**不当场修**（本片不动代码），两条一起挂下一次碰 `dev.ps1` 的工单。

## 7. 安装与运行命令（确切）

```bash
# 后端（先装 uv：powershell -c "irm https://astral.sh/uv/install.ps1 | iex"）
cd D:/git_opensource_project/ai/Ai_Web/backend
uv python pin 3.12
uv sync                        # PEP 735 [dependency-groups]：dev 组默认就装，无需 --group dev
uv sync --extra cn             # 需要 pypinyin 拼音检索时
cp .env.example .env           # 然后填远端 PG / LLM / Embedding / Fernet / JWT
uv run python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
uv run python scripts/check_env.py
uv run alembic upgrade head
uv run python scripts/seed_admin.py
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --loop asyncio   # ← --loop asyncio 才能拿到 SelectorEventLoop（asyncpg 在 Windows 必需）

# 前端
cd D:/git_opensource_project/ai/Ai_Web/frontend
npm install                    # 提交 package-lock.json 后用 npm ci
npm run dev                    # 默认 127.0.0.1:5173，proxy /api → VITE_DEV_PROXY_TARGET
```

> Windows 上 `uv sync` 直接跑会在写包内 `.exe` 的 PE 资源时失败（os error -2147024786）。
> 解法已在 `scripts/bootstrap.py` 里固化：把 `TMPDIR/TMP/TEMP` 指到仓库内 `backend/.uvtmp` 并设
> `UV_LINK_MODE=copy`。`Makefile` 与 PowerShell 的变量展开规则不一致，所以这段准备放在 Python 里做。

### 7.1 前端 env（无密钥）

```dotenv
# frontend/.env.development  （提交进 git，不含机密）
VITE_API_BASE_URL=/api                 # 走 Vite proxy，避免开发期 CORS 与凭据跨域
VITE_SSE_BASE_URL=/api                 # SSE 也走同一路径前缀
VITE_DEV_PROXY_TARGET=http://127.0.0.1:8000   # 只被 vite.config.ts 读取，不进 bundle
VITE_REQUEST_TIMEOUT_MS=120000         # 必须 > 后端 LLM timeout + 执行超时，否则前端先断
VITE_SSE_RECONNECT_MAX=3
VITE_SSE_IDLE_TIMEOUT_MS=180000        # 无事件视为断流，提示重试
VITE_UPLOAD_MAX_MB=5
VITE_FEATURE_SQL_EDIT=true             # 关时前端只读展示 SQL
VITE_FEATURE_CHART_EDIT=false
VITE_DISABLE_COMPRESSION_PROXY=true    # SSE 反缓冲
VITE_APP_TITLE=AI 问数（本地）
VITE_DROP_CONSOLE=false
```

Vite 只暴露 `VITE_` 前缀变量，**前端不放任何密钥**，这里只有"打到哪"。

## 8. 缺失凭据时能推进什么

| 缺失项 | 需要它的具体时点 | 绕行 |
|---|---|---|
| 远端 PG DSN + `aiweb` schema 可建权限 | **P1 第一天** | 本机自装 PG；或用 DDL snapshot 单测验证迁移；**P2 端到端无法完成**（最硬的前置阻塞） |
| `vector` 扩展由谁建（非 trusted） | P1（0001 迁移） | 先写成"检测缺失则清晰报错 + 给 DBA 一句 SQL"；**as-built(P2-0005)**：本机 pgvector 0.8.6 由用户以超管装好并在 `aiweb`/`aiweb_test` 两库各建一次扩展（`vector.control` 无 `trusted`，应用账号建不了）；0001 仍不碰它，但 0005 起它是**建库前置**，不再"未启用向量路时不阻塞" |
| 是否有 `pg_jieba`/`zhcfg` 中文全文配置 | P4 | 假定没有：`simple` FTS + trgm + 精确标识符 + 应用侧 `pypinyin` 别名 |
| 测试专用库 `aiweb_test` | P4/P6 集成测试 | pg marker 全 skip，只做 DDL snapshot 单测 |
| LLM `base_url`/`api_key`/`model` | **P2** | 用 `respx` fixture 假数据打通链路，真模型在 P5 之后再接；P1/P3/P7 不受影响 |
| Embedding 端点与真实维度 | P4 | P2 的 LIKE 检索不需要；P4 可先用 hash 伪向量验证 profile 切换与回填流程 |
| MySQL 5.7 建库账号 | P2 | 若只有只读账号：让用户自己跑 `init_demo_mysql.sql`，应用侧只用 `aiweb_ro` |
| 源库只读专用账号 `aiweb_ro` | P2/P5 | 用 root 也能跑，但"跨库 `mysql.user`"用例在本地会**假通过**，需标注待复测 |

**不受凭据影响可独立完成**：settings/logging/errors/schemas 骨架、DDL snapshot 单测、全部纯函数单测、
守卫攻击语料（sqlglot 本地即可）、`prompt_builder` golden、前端 P7 的空壳（用 MSW 或后端假数据）、文档与 Makefile。
