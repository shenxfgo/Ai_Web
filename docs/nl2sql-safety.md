# NL2SQL 安全模型

> 本文是评审对象。三层防御**彼此独立**：任何一层被绕过，另外两层仍必须挡住。
> 这个前提来自一条已核实的事实——**sqlglot 是 transpiler，不是 validator**
> （README 原文："The parser is intentionally lenient…"），所以 AST 白名单**不足以**当唯一防线。

## 0. 三层防御总览

| 层 | 位置 | 手段 | 失败表现 |
|---|---|---|---|
| **第一层** | `app/services/sql_guard.py` | sqlglot 正向 AST 白名单 + 危险子结构/函数扫描 + 表白名单 + 强制 LIMIT + **重生成** | 400 `sql_guard_rejected` + `rule_id` + violations |
| **第二层** | `sql_guard` 末尾，引擎侧 | `EXPLAIN` dry-run（不执行，只验证与估行） | 拒 `TOO_COSTLY` / `CARTESIAN_PRODUCT` / 语法对象不存在 |
| **第三层** | `app/services/nl2sql/executor.py` | 会话级只读 + 语句超时 + 行数上限 + 单元格/payload 截断 + 并发与限流 | 驱动报错（不是守卫报错），事务回滚 |

外加两条贯穿性约束：**数据源口令 Fernet 加密且永不回传**（§6），
**结果集落文件且下载防目录穿越**（§7）。

---

## 1. 第一层：sqlglot 正向 AST 白名单

### 1.1 接口

```python
@dataclass(frozen=True)
class Violation:  code: str; message: str; node: str | None = None

@dataclass(frozen=True)
class GuardResult:
    ok: bool
    sql_final: str | None          # 由 AST 重新生成的、强制带 LIMIT 的 SQL
    tables: tuple[QualifiedTable, ...]
    violations: tuple[Violation, ...]

def guard(
    sql: str, *,
    dialect: Literal["mysql", "postgres"],
    allowed_tables: set[QualifiedTable],   # 来自检索结果 ∩ 用户有权表（不是"全库"）
    allowed_schemas: set[tuple[str, str]],
    max_rows: int,
    forbid_files: bool = True,
) -> GuardResult: ...
```

按顺序执行，任一失败即 `ok=False`，但**收集全部 violations 再返回**，便于前端一次展示所有问题。
每条拒绝都带枚举化的 `rule_id`（便于统计模型常犯哪一类）。

### 1.2 规则清单

**① 预处理**：剥 ``` fences、去尾随 `;`、拒绝非 ASCII 之外的控制字符、长度上限（8000 字符）。

**② 解析必须成功且不宽容**

```python
stmts = sqlglot.parse(sql, dialect=dialect, error_level=ErrorLevel.RAISE)
```

不 try/except 降级，也不用默认的 `IGNORE`。
`len(stmts) != 1 or stmts[0] is None` → `MULTI_STATEMENT`（挡住 `;DROP TABLE` 拼贴）。

**③ 顶层类型白名单（正向枚举，不是黑名单）**

```
isinstance(ast, (exp.Select, exp.Union, exp.Except, exp.Intersect))   # 其余全拒
```

`Union/Intersect/Except` 需再校验 `ast.this` / `ast.expression` 也是上述类型。
显式拒绝清单（记进 violations 以便测试断言）：
`Command, Insert, Update, Delete, Drop, Create, Alter, Grant, Revoke, Merge, Copy, Set, Transaction,
Use, Call, Explain, Analyze, Optimize, Vacuum, Truncate, Refresh, Show, Describe, Pragma, Attach, Kill`。

> `SHOW` / `DESCRIBE` 在 sqlglot 里常落成 `exp.Command` → 靠"顶层白名单"自动挡住。
> **这就是必须用白名单而非黑名单的原因。**

**④ 危险子结构扫描（`ast.walk()`）**

- `exp.Into` → `SELECT_INTO` / `INTO_OUTFILE` / `INTO_DUMPFILE`（覆盖 `SELECT ... INTO @var`、`OUTFILE`、`DUMPFILE`）。
- `exp.Lock` / `Select.args.get("for_update")` / `lock` → `ROW_LOCKING`（`FOR UPDATE`、`LOCK IN SHARE MODE`）。
- `exp.Placeholder, exp.Parameter, exp.SessionVar`（MySQL `@@x` / `@x`）、`exp.Op` 中的 `:=` → `VARIABLE_ACCESS`。
- `exp.CTE` 允许（PG 需要），但 `INSERT/UPDATE/DELETE` 前缀的 CTE 在 ③ 已被拒。

**⑤ 函数黑名单**（`exp.Func` 子类名 + `exp.Anonymous.this`，小写比对）

```
load_file, into outfile, into dumpfile, sleep, benchmark, randombytes,
pg_read_file, pg_read_binary_file, pg_ls_dir, pg_stat_file, pg_ls_logdir,
pg_terminate_backend, pg_cancel_backend, pg_advisory_lock, pg_ls_waldir,
lo_import, lo_export, dblink, dblink_exec, query_to_xml, query_to_xmlschema,
table_to_xml, xmlforest(可放), pg_sleep, pg_backend_pid, current_setting(非白名单值),
system, exec, xp_cmdshell, sp_executesql, utl_inaddr,
```

另外：**拒绝一切 `Anonymous` 函数**（未知即危险，方言专有函数最容易藏副作用），
violations 里提示"函数 X 未被允许，请换写法"。
`AIWEB_GUARD__DANGLING_EXTRA_RULES` 可逗号分隔追加黑名单函数（与代码内置取并集）。

**⑥ 表引用全部落入白名单**

```python
cte_names = {c.alias_or_name.lower() for c in ast.expressions of type exp.CTE}   # 排除 CTE 别名
alias_map = {a.alias.lower() for a in ast.find_all(exp.TableAlias)}              # 排除别名
for t in ast.find_all(exp.Table):
    if t.name.lower() in cte_names: continue
    parts = (t.catalog or '', t.db or '', t.name)   # sqlglot 已解引号/反引号
    qt = QualifiedTable.from_parts(parts, dialect)
    if qt not in allowed_tables → TABLE_NOT_ALLOWED
```

- **归一化**：MySQL 大小写敏感取决于 `lower_case_table_names` → 统一 lower 比较；
  PG 未加引号的标识符折叠小写 → 加引号的表名要**保留原样**，用 `(is_quoted, name)` 双 key。
- 跨库禁止：MySQL 的 `db.table` 中 `db` 必须 ∈ `allowed_schemas`；PG 的 schema ∈ `allowed_schemas`；
  `catalog` 非空即 `CROSS_CATALOG`。
- 系统对象禁止：`information_schema, mysql, performance_schema, sys, pg_catalog, pg_toast`
  （**即便用户有权也拒**；元数据从我们自己库读）。
- `exp.Column` 上带 `catalog/db` 前缀的（`SELECT x.y.z`）→ 拆出表部分同样校验，
  防 `mysql.user` 三段式绕过。

**⑦ 强制 LIMIT**

无顶层 `exp.Limit` 时，注入 `max_rows + 1`（`+1` 用于探测截断）。
包装写法 `exp.select('*').from_(ast.subquery('__aiweb_q')).limit(max_rows+1)` 在 MySQL 5.7 上
会被优化器丢掉子查询里的 ORDER BY，**实测更稳的做法是直接 `ast.limit(max_rows+1)` 加在最外层**，
不包一层（代码里保留这条取舍注释）。
`AIWEB_GUARD__FORCE_LIMIT=true`、`AIWEB_QUERY__HARD_LIMIT` 决定注入值上限。

**⑧ 重新生成（不是原样执行 LLM 文本）**

```python
sql_final = stmt.sql(dialect=dialect, comments=False, pretty=False)
```

见 §2。

**⑨ 引擎 dry-run** 见 §3。

### 1.3 守卫一致性不变式（防 rewrite 漂移）

守卫**执行的是重生成的 SQL**，并断言：

```python
assert out.sql.endswith("LIMIT 1001") or "LIMIT" in out.sql.upper()
assert ";" not in out.sql and "/*" not in out.sql          # 重生成后的硬不变式
assert guard.parse(out.sql, error_level=RAISE)             # 幂等：可再解析
```

以及"重生成后再 parse 一次的 AST 与首次 parse 的 AST 归一化相同"——防 rewrite 漂移。
重跑（`POST /chat/messages/{id}/rerun`）**绝不复用旧 `sql_final`**，同样走完整 guard。

---

## 2. `comments=False` 重生成不变式与 hint 拒绝

`stmt.sql(comments=False)` 是挡住 **MySQL 可执行注释** 的关键安全参数：
`/*!50100 UNION SELECT ... */` 这类注释在 sqlglot AST 里是 comment，重生成时被抹掉 →
天然挡住这条经典绕过（对应语料 `executable_comment`）。顺带完成方言归一：
AI 写了 `LIMIT 10` 给 PG 也能被重写成合法形态。重生成结果必须满足不变式：**不含 `;`、不含 `/*`**。

**副作用与对策（这是设计里最微妙的一条）**：`comments=False` 同时会剥掉优化器 hint——
MySQL 的 `/*+ MAX_EXECUTION_TIME(n) */` 也是注释。这会造成"**守卫看到的 SQL ≠ 实际执行的 SQL**"，
属于最危险的一类不一致。因此定稿的规则是：

1. **检测到任何 hint 节点即拒绝**（`rule_id = hint_not_allowed`）——模型本来就不该生成 hint。
2. **超时必须走会话变量而不是 hint**：MySQL 用 `SET SESSION MAX_EXECUTION_TIME = <ms>`，
   PG 用 `SET LOCAL statement_timeout`。会话变量作用于之后所有 SELECT，天然覆盖 `UNION` 场景，
   也就不需要再判断"UNION 不加 hint"。
   （`AIWEB_QUERY__USE_SESSION_MAX_EXEC_TIME=true` 即此语义。）

> 早期草案曾打算在 SQL 前挂 `/*+ MAX_EXECUTION_TIME(n) */` 做双保险（因为 session 变量可能被中间件重置），
> 该做法与 `comments=False` 冲突，已作废；现在只保留 `SET SESSION` 路径。

---

## 3. 第二层：引擎 EXPLAIN dry-run

弥补 sqlglot "宽容非校验"的第二道独立闸门：

- **MySQL**：`EXPLAIN <sql_final>`（5.7 对 SELECT 支持；`EXPLAIN` 不执行）。
- **PG**：
  ```sql
  BEGIN READ ONLY; SET LOCAL statement_timeout='3000ms'; EXPLAIN <sql_final>; ROLLBACK;
  ```
- 估行超阈值：`rows_estimated > AIWEB_QUERY__MAX_EXPLAIN_ROWS`（默认 5e7）→ 拒 `TOO_COSTLY`。
- 笛卡尔积：v1 **只对"无 join 条件的逗号连接"硬拒**，其余放行
  （`materialize` / `Nested Loop all` + 无索引 Join 的检测误报率高，写进 known limitation）。
- `AIWEB_QUERY__DRY_RUN=true`；关掉即失去第三道防御之外的第二层，**prod 启动自检禁止关闭**。
- dry-run 失败信息可回喂模型自动重试一次（`retry_with_error`），**最多 1 次**。

---

## 4. 第三层：会话级只读、超时、行上限

### 4.1 MySQL（session 建立后依次执行，全在同一连接上）

```sql
SET SESSION TRANSACTION READ ONLY;   -- 5.7 支持；挡住一切写
SET SESSION MAX_EXECUTION_TIME = <timeout_ms>;  -- 5.7.4+，仅作用于只读 SELECT，最可靠的路径
SET SESSION sql_mode = CONCAT(@@sql_mode, ',NO_ENGINE_SUBMIT');  -- 不改；仅读取并记入日志
SET SESSION NETWORK_COMPRESSION = OFF;  -- 视驱动支持，失败忽略
SET SESSION wait_timeout = <timeout_ms/1000 + 10>;
SET SESSION autocommit = 1;
```

> **as-built(P2-011)：本表是"目标顺序"，`session_statements('mysql', ...)` 实际只发四条 SET**——
> `READ ONLY` → `MAX_EXECUTION_TIME` → `wait_timeout` → `autocommit=1`。
> `sql_mode` 那条注释已经写明"不改，仅读取并记入日志"，属于观察项而不是执行项，011 不做；
> `NETWORK_COMPRESSION` 在 asyncmy 上没有对应会话变量（是 MySQL 服务端 `--skip-network-compression`
> 启动参数或客户端握手选项），发出去就是 1193 unknown sysvar，与 §4.1 想要的"失败忽略"结果一样，
> 索性不发。两条都是"注释里的意图已达成，语句本身跳过"。

- `SET SESSION TRANSACTION READ ONLY` 前**不能在已有事务里**。

  > **as-built(P2-011)：落地方式 = 拿到连接后立刻 `await conn.execution_options(isolation_level="AUTOCOMMIT")`**。
  > SQLAlchemy 2.0 的 AsyncConnection 会 autobegin，不切 AUTOCOMMIT 的话紧随 `show grants`（本身是
  > SELECT）就把事务开了，随后那条 `SET SESSION TRANSACTION READ ONLY` 会被 MySQL 5.7 拒。
  > AUTOCOMMIT 让每条 SET/SELECT 各自即时提交，不进 SQLAlchemy 的事务包裹。
  > 另：`execution_options` 在 async 侧是 awaitable（同 `AsyncConnection` 上的 `run_sync` 一样要 await），
  > 写成 `conn = raw_conn.execution_options(...)` 会得到一个 coroutine 而不是 connection。

- 账号侧要求：**只给 `GRANT SELECT ON <db>.* TO 'aiweb_ro'@'%'`**，数据源表单提示"请建只读账号"。
  UI 检测 `SHOW GRANTS` 里出现 `ALL|INSERT|UPDATE|DROP|CREATE` → 黄色警告（不阻断，admin 可确认）；
  连上的账号不具备只读能力时 `test_connection` 报 `readonly_capability_missing`（能力探测前置）。

  as-built(P2-0011 开工前拍板)：**"不阻断"只属于登记阶段**（`test_connection` 回 200 + 机读码，
  让 admin 自己决定要不要留这个数据源）；**执行阶段是硬阻断**，011 的 `executor` 在建好会话级只读
  之前先跑 `SHOW GRANTS`、复用 `grants_verdict`（`datasource_service.py:192`），`code` 非空即拒，
  错误码 `readonly_capability_missing`，**没有 admin 覆盖旋钮**。理由：第一层拦的是"SQL 形状"，
  而真实权限属于"账号能力"——形状对得上但账号能写，三层里只有这一格能拦住。
  代价与取舍：结论**不缓存进元数据库**（缓存分不清"当时只读"和"后来被提权"），所以每条问数
  多一次 `SHOW GRANTS` 往返（源库内网 ≈1ms，可接受）；`data_sources.readonly_enforced` 是用户在
  表单里勾的自报家门列，**不作为阻断依据**，只作 UI 提示。
- 取数：无缓冲 cursor + 逐 1000 行 `fetchmany`，累计到 `max_rows+1` 立即 `cursor.close()`。
  **不做 `KILL <connection_id>`**——`KILL` 是写操作，只读账号本来就无权限，依赖
  `MAX_EXECUTION_TIME` 自杀 + 连接归还前 `ROLLBACK`。这是 MVP 的诚实取舍，写进 known limitation。

  > **as-built(P2-011)：本条降级为"缓冲 `execute()` + `fetchall()`"**。理由不是"实现偷懒"，而是
  > 流式取数会让**超时分类废掉**：asyncmy 在 `stream(partitions(1000))` 下遇到服务端 3024
  > 不会把 3024 传给我们，而是先吐 2013（Lost connection）并挂一条
  > `coroutine '_finish_unbuffered_query' was never awaited` 警告；分类器拿不到 timeout code，
  > `QueryTimeout` 就翻不出来，验收 ② 直接假过。缓冲取回时 3024 到得了客户端，行为一致，
  > 而**内存上限由守卫注入的 `LIMIT row_limit+1` 钉住**，与是否流式无关。
  > 代价：绕过守卫直调 `execute_readonly` 的话没有那一行兜底，会退化成"整结果集进内存"——
  > 012 接线时必须保证 `sql_final` 只能来自 `sql_guard.check(max_rows=...)` 的返回值。
  > 等 asyncmy 修好流式模式下的 3024 传播再回到本条的原口径。

- `MAX_EXECUTION_TIME` **只在行边界检查**。这意味着：`count(*)` 从头到尾只吐一行，中间从不看时钟；
  单条 `SELECT SLEEP(n)` 同样只有一行，也查不到；两者都不会触发超时中断。演示库要测这个必须让
  **每一行都慢**（011 的 live 用例用 `select sleep(1) from order_item limit 5`，第一行结束即命中阈值）。

  > **as-built(P2-011)：源库不支持 `MAX_EXECUTION_TIME`（MySQL < 5.7.4，errno 1193 unknown sysvar）时
  > 降级靠驱动侧 `read_timeout`**：分类器 `_is_unsupported_max_execution_time` 命中 1193 后 warning +
  > `continue`，其余 SET 语句照旧执行、执行本身不阻断。演示库 5.7 支持该变量，这条**live 不测**——
  > 降级分支只在单测的 `session_statements` 顺序里存在，触发路径要真·5.7.3 才能演，P2 不设这个环境。

- 错误码分档 as-built(P2-011)：3024（MySQL `MAX_EXECUTION_TIME` 自杀）与 PG `"57014"` 翻成 `QueryTimeout`；
  1792（会话 READ ONLY 挡写）/ 1142（账号无写权限）走原始 `DBAPIError` **不翻译**，是"引擎拒 ≠ 应用拒"
  的证据（验收 ① 靠这两档分开断言）；翻译了反而分不清拦下来的是 MySQL 还是我们。演示库上 aiweb_ro
  只有 SELECT 权限，实测 1142 先命中，(1792, 1142) 都算通过。

- 超时上限与其来源键名的**判定位置** as-built(P2-011 双轴审查收口)：收在执行器内部的
  `resolve_timeout(row)`，`execute_readonly` **不收** `timeout_ms`/`timeout_source` 入参。
  原口径把判定推给调用方传字符串——012 一撒谎（比如永远传全局键名），验收 ② 的
  "错误里带超时上限来自哪个配置项"就假过：detail 的键名与用户实际能改的那格对不上，
  用户按提示改全局键但仍然超时。现实口径：`data_sources.timeout_ms` 是 NOT NULL 列
  （server_default=15000，登记时把全局缺省落进列值），所以正常登记行**恒**走数据源档；
  列上缺值（手插行/未 flush 的内存对象）才落 `Settings.query.timeout_ms` 并点名
  `AIWEB_QUERY__TIMEOUT_MS`。钉它的是 `tests/unit/test_executor_resolve_timeout.py` 两条 +
  live 慢查询用例（detail 含 `data_sources.timeout_ms` 且值等于行上的 300）。

- SET 循环的错误处理 as-built(P2-011 双轴审查收口)：非降级的 SET 失败**直接 `raise`**，
  由外层统一分档；不在循环内再调一次 `classify_source_error`——那会让非超时异常被分档两遍，
  以后谁往分类器里加计数或日志就重复触发。1193 降级分支（warning + continue）不变。

### 4.2 PostgreSQL

```sql
SET default_transaction_read_only = on;      -- 或连接参数 options='-c default_transaction_read_only=on'
BEGIN; SET LOCAL transaction_read_only = on;
SET LOCAL statement_timeout = '<n>ms';
SET LOCAL idle_in_transaction_session_timeout = '30s';
SET LOCAL lock_timeout = '3s';
SET LOCAL application_name = 'aiweb-nl2sql';
```

连接串可选带 `target_session_attrs=prefer-standby`：有备库时走只读副本，这是 PG 侧最强的一道防线。

> **as-built(P2-011)**：`session_statements('postgres', ...)` 只发**两条**——
> `SET default_transaction_read_only = on` 和 `SET statement_timeout = '<n>ms'`。
> `BEGIN; SET LOCAL ...` 那一串属于"事务级"写法，需要执行器把 SET 和真实查询包在同一个事务里；
> 011 的 MySQL 侧走的是 AUTOCOMMIT，PG 侧不做双分支实现，统一 `SET`（会话级）够用。
> `application_name` / `lock_timeout` / `idle_in_transaction_session_timeout` 三条是观测/微优化项，
> P2 没启用。
> **且 `execute_readonly` 目前只走 MySQL 分支**，PG kind 直接抛 `NotImplementedSource`——与 006
> `test_connection`、007 抽取的现状对齐；PG 主链在 006-011 全线通了再上（roadmap §P2 未把 PG 主链
> 列为验收）。

### 4.3 行上限与截断

| 键 | 默认 | 作用 |
|---|---|---|
| `AIWEB_QUERY__ROW_LIMIT` | 1000 | 返回给前端 + 喂结论模型的行数上限 |
| `AIWEB_QUERY__HARD_LIMIT` | 5000 | 注入 `LIMIT(+1)` 的值上限 |
| `AIWEB_QUERY__TIMEOUT_MS` | 15000 | 默认语句超时；数据源级可覆盖 |
| `AIWEB_QUERY__MAX_TIMEOUT_MS` | 30000 | 全局天花板，UI 不许超过 |
| `AIWEB_QUERY__CELL_MAX_CHARS` / `AIWEB_RESULT__MAX_CELL_CHARS` | 1000 / 2000 | 超长文本单元格截断 |
| `AIWEB_RESULT__MAX_PAYLOAD_MB` | 2 | 单页 JSON payload 上限 |
| `AIWEB_QUERY__CONCURRENCY_PER_DS` | 2 | 按数据源限并发（`asyncio.Semaphore`），防连点把源库打满 |
| `AIWEB_QUERY__RATE_LIMIT_PER_USER_PER_MIN` | 20 | 每用户每分钟问数次数（内存滑动窗口） |

> **as-built(P2-0011 开工前拍板，2026-09-28)**：本表是**目标形态**，P2 实际只有六个键
> （`Settings.QueryGroup` 实有 `row_limit / timeout_ms / concurrency_per_ds /
> rate_limit_per_user_per_min / max_explain_rows / allow_sql_edit` 六个，
> 另有 `ResultGroup.dir / retention_days / preview_rows / max_cell_chars / max_payload_mb` 五个）。
> 用户拍板"**只用现有键，文档对齐现实**"，
> 于是：
> - `HARD_LIMIT` / `MAX_TIMEOUT_MS` **不建**。注入上限就是 `row_limit + 1`，
>   由守卫的 `check(max_rows=)` 参数带进去（`sql_guard.py:397`），调用方当下只传全局 `row_limit`；
>   "数据源级 `row_limit` 覆盖"属接口层那一跳（P3），"UI 不许超过天花板"属有 UI 输入的那一档。
> - `CELL_MAX_CHARS` **不建**，用已有的 `AIWEB_RESULT__MAX_CELL_CHARS`（`ResultGroup.max_cell_chars=1000`）。
> - `USE_SESSION_MAX_EXEC_TIME` **不建**：011 无条件试 `SET SESSION MAX_EXECUTION_TIME`，
>   失败就降级靠驱动超时并把降级原因写进错误/日志（源库是否支持在 006 的 `supports_max_execution_time` 里已知）。
> - `DRY_RUN` **不建**：§3 的 EXPLAIN dry-run 整层属 P3。
> - `AIWEB_GUARD__FORCE_LIMIT`（§1.2 ⑦ 那个开关）**不建**：强制补 LIMIT 当下是无条件行为，
>   开关等"允许用户提交不带 LIMIT 的 SQL"这种需求出现时再补。
> - 并发闸（`CONCURRENCY_PER_DS` / `RATE_LIMIT_PER_USER_PER_MIN`）两键在 `Settings` 里，
>   **但没有读取点**，真接线属 P3。
>
> `.env.example` ↔ `Settings` 由 `test_env_example.py` 钉着一一对应，本表**不在闸里**——
> 这就是这类漂移能活到今天的原因。

- 截断探测：注入 `max_rows + 1`，取到 `max_rows+1` 行即判定 `truncated=true` 并丢弃最后一行。
- 单元格保护：`str` 截断；`bytes`/`Binary` → `"<binary 1.2KB>"`；
  `Decimal` → **str**（避免前端精度丢失，不能让金额变成 `0.30000000000000004`）；日期 → ISO 字符串。
  `executor` 统一 `default=str` + 按列 `type` 显式序列化，否则 naive `datetime` 进 `json.dumps` 会 500。

  > **as-built(P2-011)**：`serialize_cell(value, *, max_cell_chars)` 就是这一条的**唯一入口**，
  > 顺序是 Decimal → datetime/date/time → timedelta → bytes → str 超长 → 原样透传（int/float/bool/None）。
  > `default=str` 只在 `json.dumps` 兜"未知的类型"，不进入本条主路径。
  > bytes 占位分档：`<binary 3B>`（<1KiB）与 `<binary 1.2KB>`（≥1KiB，`round(n/1024, 1)`）。
  > **`time` 走 `isoformat()`**（TIME 列 → `"14:30:00"`）。
  > **`timedelta` 不用 `str()`**——asyncmy 对 TIME 列与 `TIMEDIFF()` 返回 timedelta，而
  > `str(timedelta(hours=-1))` 是 `"-1 day, 23:00:00"`（Python 规范形），与 `mysql>` 手工逐位对数
  > （verification §3 第 10 步）对不上；`_format_timedelta` 按 MySQL TIME 字面量的
  > `[-]H:MM:SS` 手工拆分（H 可负、可超 24，整秒归一）。钉它的是
  > `tests/unit/test_executor_serialize.py::test_timedelta_按_mysql_time_字面量形状回` 四条手算值。

- 结果 csv 落盘形态 as-built(P2-011)：
  - 编码 **`utf-8-sig`**（带 BOM），Excel 打开中文列头/单元格不乱码；写入时 `newline=""` 交给 csv 模块自己管
    CRLF/LF，跨平台一致。
  - 文件命名 `run_id` = **UTC 时间戳 `%Y%m%d%H%M%S` + `uuid4().hex`**，形状白名单 `[A-Za-z0-9]+`
    （`_RUN_ID` 同一份正则同时给生成器和校验器用，两者不一致的话会出现"生成的名字过不了自己的门"）。
  - 写入侧防护：`result_csv_path(result_dir, run_id)` 对越界 run_id（`..`、`/`、绝对路径、空字节、空串）
    **一律 `ValueError`**，路径拼出来必在 `result_dir.resolve()` 之内。§7 的下载侧一半（realpath 落
    回目录内、403/404 不区分）**归 013**。
  - `execute_readonly` 一次调用生成一个 `run_id`，落在 `ExecutionResult.run_id` 与 `.result_file` 两个字段，
    接口层（012）只把它们抄进响应体，没有任何入参能反向指定。

- 抽取侧同样保护：全部 IS 查询前 `SET SESSION max_execution_time` / `SET LOCAL statement_timeout`，
  批大小固定 200，批间 `await asyncio.sleep(EXTRACT__BATCH_INTERVAL_MS)`，单连接串行，不开并发打源库。

---

## 5. 权限与白名单的关系

- `allowed_tables` 的来源是 **检索结果 ∩ 该用户有权的表**，不是"全库"。执行前二次校验
  "SQL 引用的表 ⊆ 该用户可访问表"。
- member 直接 `POST /chat/validate {"sql":"SELECT * FROM secret_table"}` → `403 table_not_granted`。
- 跨用户会话不可见；`chat_message` 记录 `actor_id`。
- "用户手改 SQL 后重跑"由 `AIWEB_QUERY__ALLOW_SQL_EDIT=admin|all|none` 控制。
  **开启时输入来源从"模型输出"扩到"人输入"**，守卫的 hint/comment 拒绝与 `table_not_allowed`
  必须在此模式下 100% 覆盖（语料里 §8.6 的 hint 用例与跨库用例就是为这条准备的）。

---

## 6. 数据源凭据处理

- 口令字段 `aiweb.data_sources.secret_enc bytea NOT NULL`（Fernet 密文 → **二进制，不用 text**）。
  写入即刻加密，`create`/`update` 后内存里不留明文。
- **任何 API 响应都不回传口令**：`GET /datasources/{id}` 只回 `password_masked:'••••'` + `has_secret:true`；
  PATCH 时密码字段缺省 = 不改。前端"表单里已填的口令不会回填到任何 GET 响应"是验收项（Network 面板核对）。
- 轮换：`AIWEB_FERNET__KEYS` 当前 key + 保留的旧 key 列表（`MultiFernet`，新把在前，解密按多把尝试）。
  启动自检做加解密哨兵（失败 fatal）+ 逐条试解密库内 credential（有失败者按 datasource id 列出，warn）；
  `AIWEB_FERNET__REENCRYPT_ON_STARTUP=true` 是一次性把所有密文用当前 key 重写。
- 日志脱敏：`RedactFilter` 同时按"键名正则（password/api_key/credential/Authorization）"
  和"值形态正则（JWT、`gpt-`、`sk-`）"双保险；含密 model 覆写 `__repr__` 输出 `redacted`。
  否则启动打印配置或异常 traceback 会把 DSN 打出来。
- 密钥类字符串**一律禁止进 `app_settings`**（admin 端点对 value 做
  `^(sk-|.*api_key.*|.*password.*)$` 拒绝写入并返回 422）。
- DSN 拼接必须 `quote_plus` 用户名与口令：含 `@ # % /` 不转义会被解析成主机名，报"未知主机"且极难查
  （`PgGroup.dsn()` 有专门单测覆盖）。
- 演示/生产都用只读专用账号 `aiweb_ro`，只授 `SELECT ON <db>.*`，不给 `ON *.*`
  （否则"跨库读 `mysql.user`"这类攻击用例在本地永远测不出真拦截）。

---

## 7. 结果文件下载与目录穿越防护

结果集落 `AIWEB_RESULT__DIR`（默认 `data/results/`）后，前端按文件名请求下载。规则：

- 文件名由**服务端生成**，客户端只能提交服务端此前返回过的标识；不接受任意相对/绝对路径。
- 解析路径后必须校验其 realpath 仍落在结果目录内（拒绝 `..`、符号链接逃逸、绝对路径、空字节），
  越界一律 403/404，不区分"不存在"与"无权限"的细节。
- 同一套约束适用于 `knowledge/` 覆盖层的文件读写（目录根来自配置，不由用户拼接）。
- 结果内容不进版本库（`.gitignore` 的 `data/*`），保留期由 `AIWEB_RESULT__RETENTION_DAYS` 控制；
  体检项 `query.row_limit` > 5000 会 warn，理由就是"结果集落文件也吃磁盘"。

---

## 8. 攻击语料（测试 oracle）

`backend/tests/guard/corpus.py` 里两个常量列表 + 两个 parametrize，一条用例一次跑完：

```python
# tests/guard/test_reject_corpus.py
@pytest.mark.parametrize("sql,rule", REJECT_CASES, ids=[c[0][:38] for c in REJECT_CASES])
def test_reject(sql, rule):
    with pytest.raises(SqlGuardError) as ei: guard.check(sql, allowed=DEMO_TABLES, dialect="mysql")
    assert ei.value.rule_id == rule, f"{sql!r} 被拒但规则不符预期"

@pytest.mark.parametrize("sql", ALLOW_CASES, ids=[s[:38] for s in ALLOW_CASES])
def test_allow(sql):
    out = guard.check(sql, allowed=DEMO_TABLES, dialect="mysql")
    assert out.sql.endswith("LIMIT 1001") or "LIMIT" in out.sql.upper()
    assert ";" not in out.sql and "/*" not in out.sql          # 重生成后的硬不变式
    assert guard.parse(out.sql, error_level=RAISE)             # 幂等：可再解析
```

### 8.1 必须被拒（≥15，标 `rule_id`）

| # | SQL | 期望 rule_id |
|---|---|---|
| 1 | `SELECT 1; DROP TABLE kb_card` | `multi_statement` |
| 2 | `DROP TABLE order_main` | `top_level_not_select` |
| 3 | `UPDATE customer SET level='vip' WHERE 1=1` | `top_level_not_select` |
| 4 | `DELETE FROM order_item WHERE 1=1` | `top_level_not_select` |
| 5 | `INSERT INTO category VALUES (99,'x','y')` | `top_level_not_select` |
| 6 | `CREATE TABLE tmp_x AS SELECT * FROM customer` | `top_level_not_select` |
| 7 | `TRUNCATE TABLE payment_record` | `top_level_not_select` |
| 8 | `SELECT id, name FROM customer INTO OUTFILE '/tmp/c.csv'` | `into_outfile` |
| 9 | `SELECT id FROM customer INTO DUMPFILE '/tmp/x'` | `into_outfile` |
| 10 | `SELECT LOAD_FILE('/etc/passwd')` | `danger_function` |
| 11 | `SELECT /*!50100 DROP TABLE user */ FROM dual` | `executable_comment` |
| 12 | `SELECT /*!32302 1/0, */ 1 AS x` | `executable_comment` |
| 13 | `SELECT SLEEP(30)` | `sleep_function` |
| 14 | `SELECT BENCHMARK(50000000, MD5('a'))` | `danger_function` |
| 15 | `SELECT pg_read_file('/etc/passwd')` | `pg_file_access` |
| 16 | `SELECT * FROM pg_ls_dir('/')` / `SELECT pg_sleep(10)` | `pg_file_access` |
| 17 | `SELECT * FROM mysql.user` | `table_not_allowed`（跨库） |
| 18 | `SELECT name FROM information_schema.columns WHERE table_name='user'` | `table_not_allowed` |
| 19 | `SELECT * FROM order_main FOR UPDATE` | `locking_clause` |
| 20 | `SELECT * FROM order_main LOCK IN SHARE MODE` | `locking_clause` |
| 21 | `WITH x AS (UPDATE customer SET level='vip' RETURNING id) SELECT * FROM x` | `dml_in_cte` |
| 22 | `SELECT 1 WHERE pg_catalog.pg_sleep(1) IS NULL` | `danger_function`（子句内函数扫描） |
| 23 | `CALL sp_purge()` | `top_level_not_select` |
| 24 | `PREPARE s FROM 'SELECT 1'; EXECUTE s` | `prepared_statement` |
| 25 | `HANDLER customer OPEN; READ customer FIRST` | `top_level_not_select` |
| 26 | `LOAD DATA INFILE '/etc/passwd' INTO TABLE customer` | `top_level_not_select` |
| 27 | `SELECT * FROM order_main GROUP BY id PROCEDURE ANALYSE()` | `procedure_analyse` |
| 28 | `SELECT * FROM (SELECT 1) t WHERE (SELECT COUNT(*) FROM customer) > 0 UNION SELECT user,password FROM mysql.user` | `table_not_allowed` |
| 29 | `SET GLOBAL general_log = 'ON'` | `top_level_not_select` |
| 30 | `SELECT id FROM customer WHERE name = '' OR 1=1; -- ` + 换行 `DROP TABLE customer` | `multi_statement` |
| 31 | `SELECT * FROM customer LIMIT 1 INTO @v` | `into_outfile` |
| 32 | `SELECT 1 /*+ MAX_EXECUTION_TIME(1) */` | `hint_not_allowed`（§8.6） |
| 33 | `SELECT $$\n DROP TABLE x\n $$` / `SELECT E'\\x2d\\x2d'`（非常规 quoting / dollar-quoted） | `unsupported_construct` |
| 34 | `SELECT * FROM customer c JOIN order_main o ON c.id=o.cid FOR SHARE o.id` | `locking_clause` |

再加两个非 SQL 文本用例：空串、纯注释 `-- hi` → `empty_statement`；超长（>20k 字符）→ `too_large`。

> 第 27 条要留意：5.7 的 `PROCEDURE ANALYSE()`、`INTO @var`、`FOR UPDATE` 都需要单独规则，
> sqlglot 的宽容 parser 可能不把 `PROCEDURE ANALYSE` 识别为子结构 → **必须有正则级兜底**。

### 8.2 必须被放行（≥6）

1. `SELECT o.id, SUM(o.amount) AS total FROM order_main o JOIN customer c ON c.id=o.customer_id WHERE o.created_at >= '2024-01-01' GROUP BY o.id ORDER BY total DESC LIMIT 10`
2. `WITH monthly AS (SELECT DATE_FORMAT(created_at,'%Y-%m') m, SUM(amount) s FROM order_main GROUP BY 1) SELECT * FROM monthly ORDER BY s DESC LIMIT 20`
3. `SELECT status, COUNT(*) c FROM payment_record GROUP BY status HAVING COUNT(*) > 5 ORDER BY c DESC`（无 LIMIT → 断言注入 `LIMIT 1001`）
4. `SELECT c.id, c.name FROM customer c LEFT JOIN order_main o ON o.customer_id=c.id WHERE o.id IS NULL LIMIT 50`
5. `SELECT 1 AS a UNION SELECT 2`（顶层 Union 在已定稿白名单内）
6. `SELECT id, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY created_at) rn FROM order_item LIMIT 100`
7. `SELECT DATE(created_at) d, COUNT(*) FROM order_main GROUP BY 1 ORDER BY 1 LIMIT 400`
8. `SELECT json_extract(extra_col,'$.city') FROM customer LIMIT 10`
9. `SELECT o.id FROM order_main o WHERE o.id IN (SELECT om.order_id FROM order_item om WHERE om.qty>3) EXCEPT SELECT r.order_id FROM refund_record r`
10. `SELECT p.name, s.* FROM product_stats_wide s JOIN product p ON p.id=s.product_id ORDER BY s.gmv DESC LIMIT 1`（宽表自连接冒烟）

放行用例还要断言：`LIMIT 5 OFFSET 10` 不被改坏（幂等，不出现双 LIMIT）、
注入的 LIMIT 值是 `HARD_LIMIT + 1`、`table_refs` 集合恰好等于 SQL 里的表（含别名解析后）。

### 8.3 变异测试（一条 parametrize 自动扩到全语料）

`test_corpus_mutation.py`：对每条 ALLOW 用例施加 5 种变异并断言必须被拒 ——
追加 `; DROP TABLE x`、把某表名换成 `mysql.user`、在末尾加 `FOR UPDATE`、
把 `SELECT` 换成 `SELECT ... INTO OUTFILE '/tmp/a'`、在注释里塞 `/*!50100 ...*/`。
这是防"规则只挡顶层"的探针，价值高于再加 10 条手写用例。

### 8.4 三层独立性的验证方式

- 手工提交一条不带 `LIMIT` 的 `SELECT * FROM order_item` → 实际执行 SQL 日志里看到 `LIMIT 1001`
  且返回 1000 行 + `truncated:true`（第一层的强制 LIMIT 生效）。
- 在 MySQL 5.7 上提交 `SELECT SLEEP(20)` → 被黑名单拒；**把黑名单临时清空** →
  被 `SET SESSION MAX_EXECUTION_TIME=1000` 截断（错误来自驱动 timeout 而非守卫），
  证明第二/第三层独立有效。
- `POST /api/chat/validate {"sql":"SELECT 1; DROP TABLE users"}` →
  400 `{code:"sql_guard_rejected", rule_id:"multi_statement"}`。
- 即使模型产出了 `DROP`，`/api/chat/ask` 的 dry-run 分支也要返回 400 `sql_guard_rejected`，
  绝不落到执行层。

---

## 9. Known limitations（诚实清单）

| 项 | 现状 |
|---|---|
| 服务端查询无法主动 `KILL` | 只读账号没有 `KILL` 权限；依赖 `MAX_EXECUTION_TIME` 自杀 + 归还前 `ROLLBACK` |
| 笛卡尔积检测 | v1 只对"无 join 条件的逗号连接"硬拒，其余放行（误报率高） |
| sqlglot 宽容 parser | 需要正则级兜底（`PROCEDURE ANALYSE` 等）；升级 sqlglot 必须先跑全语料 |
| MySQL 5.7 `ONLY_FULL_GROUP_BY` 默认开启 | 进 prompt 约束，不在守卫里强行改写 SQL |
| 中间件/代理重置会话变量 | 曾考虑用 hint 双保险，与 `comments=False` 冲突，改为拒绝 hint |

### 9.1 语料与 sqlglot 30.19 实测的差异（实现时逐条核实过）

拒绝结论一条没少，只有**归因**随实际解析行为调整；`tests/guard/corpus.py` 里同步注明。

| §8.1 用例 | 实测 |
|---|---|
| #25 `HANDLER`、#26 `LOAD DATA` | 直接 ParseError → 归 `unsupported_construct`，不是 `top_level_not_select` |
| #24 `PREPARE s FROM 'SELECT 1'; EXECUTE s` | 是两条语句，规则先后使它归 `multi_statement`；单条 `PREPARE` 才归 `prepared_statement` |
| #22 `WHERE pg_catalog.pg_sleep(1)` | 与 #16 同一个函数，统一归 `pg_file_access` |
| #33 的 PG `E'\x2d\x2d'` | 解析成普通字符串字面量、重生成后仍是字面量，不构成注入面 → 未收进语料 |
| 行首注释（`-- 说明\nSELECT …`） | 放行。① 只剥围栏与尾随 `;`，"整条都是注释"才判 `empty_statement`——注释扫描在剥完之后 |
| §8.1 之外的 4 条补充语料 | `orders`（短表名无权）、`other_db.x`（跨库）、`mysql.user.User`（三段式列）、`ai_web_demo.information_schema.tables` 来自 §1.2 ⑥ 的规则描述，corpus 里逐条标注了出处 |
| 无表引用（#5 放行用例 `SELECT 1 UNION SELECT 2`） | 放行：没有可越权的对象；危险函数规则排在表白名单之前命中，不因此漏挡 |

### 9.2 §1.1 接口与实现的差额（评审时逐条对过）

实现比文档**更严**的地方一律不动接口，只在表里说明；需要放宽时必须先改本文。

| §1.1/§1.2 要求 | 实现现状 |
|---|---|
| `allowed_schemas` 单独入参 | 折进 `allowed`（表白名单里已带 `db`，短表名由 `default_schema` 补全）→ 效果等价且更严 |
| `forbid_files: bool = True` | 恒为真，不给关 |
| ⑥ `CROSS_CATALOG` 独立规则 | 归 `table_not_allowed`（catalog 非空的白名单条目本来就不存在） |
| ③ `Union/Except/Intersect` 递归校验臂 | 未写：sqlglot 对 `SELECT 1 UNION DROP TABLE x` 直接 ParseError，规则跑不到那一层 |
| ⑤ `AIWEB_GUARD__DANGLING_EXTRA_RULES` 追加黑名单 | 未实现。"拒绝一切 Anonymous" 已经是超集，追加黑名单只在"想放开某个函数"时才有意义，而那不在 v1 计划里 |
| ⑦ `HARD_LIMIT` 收敛模型自写的 `LIMIT 999999` | 未收敛，交给第三层的行数上限截断 + `truncated` 标记 |
