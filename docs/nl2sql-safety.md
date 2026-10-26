# NL2SQL 安全模型

> 本文是评审对象。三层防御**彼此独立**：任何一层被绕过，另外两层仍必须挡住。
> 这个前提来自一条已核实的事实——**sqlglot 是 transpiler，不是 validator**
> （README 原文："The parser is intentionally lenient…"），所以 AST 白名单**不足以**当唯一防线。

## 0. 三层防御总览

| 层 | 位置 | 手段 | 失败表现 |
|---|---|---|---|
| **第一层** | `app/services/sql_guard.py` | sqlglot 正向 AST 白名单 + 危险子结构/函数扫描 + 表白名单 + **连接条件扫描**（`cartesian_product`，as-built(P5-037)：这一支原本挂在第二层，实测归这里，§3.4）+ 强制 LIMIT + **重生成** | 400 `sql_guard_rejected` + `rule_id` + violations |
| **第二层** | `app/services/nl2sql/dry_run.py`——**由 `executor.py` 在取行之前调用**，跑在第三层那条只读会话上（as-built(P5-037)：本行原来写的位置是"`sql_guard` 末尾"，那是零实现时代的字面。dry-run 要在真库连接上发 `EXPLAIN`，它不可能住在纯 AST 的第一层里） | `EXPLAIN <sql_final>`（不执行，只验证与估行）。PG 那一支今天**只有 SQL 形状、没有真跑**——执行路对 PG 直接 501，见 §3 | 抛 `SqlGuardError`，码是 `too_costly` / `dry_run_unreadable` / `dry_run_engine_rejected`（"语法/对象不存在"就是这一枚，正文带源库原话）。本行原来写的 `TOO_COSTLY` / `CARTESIAN_PRODUCT` 是两个大写字面：**前者按实物订正成小写**，**后者根本不住这一层**——实测逗号连接在 AST 上可见，归第一层（§3 的落点实测） |
| **第三层** | `app/services/nl2sql/executor.py` | 会话级只读 + 语句超时 + 行数上限 + 单元格/payload 截断 + 并发与限流 | 驱动报错（不是守卫报错），事务回滚 |

外加两条贯穿性约束：**数据源口令 Fernet 加密且永不回传**（§6），
**结果集落文件且下载防目录穿越**（§7）。

上面这张表**只有三层**，今天仍然只有三层。as-built(口径 P5-038) 起表下面要多读一句：
第二层拒了以后，编排层会把那句拒绝原文回喂给模型**再要一次**（§3.5）。那是**一次重试**，
不是第四道防御——它不裁任何东西、不放行任何东西，两道闸门对它**逐字重跑一遍**（完整守卫 +
完整 dry-run + 重新钳位），所以它没有位置可以插进这张表的任何一行。
第一层拒的绝不回喂，第三层炸的原样上抛（口径见 §3.5 那两条收窄）。

---

## 1. 第一层：sqlglot 正向 AST 白名单

### 1.1 接口

```python
class GuardRuleId(StrEnum):
    """拒绝词表：成员名是内部名，**值才是对外接口**（落 `guard_result` JSONB、进验收字面）。
    22 枚逐枚的"拦的是什么"见下面 §1.1.1 那张表——那张表是词表的唯一清单，值与它第一列一字不差。
    as-built(P5-037)：这枚枚举从"第一层的词表"变成**三层共用的拒绝词表**——第二层那三枚
    （`too_costly` / `dry_run_unreadable` / `dry_run_engine_rejected`）进的也是它，因为它们要落进
    **同一格** `chat_messages.guard_result`（§3.2 第 4 格）。**名字仍叫 `GuardRuleId`**：值是对外契约，
    改名要动的是 JSONB 落库字面、54→57 条语料期望值、以及 roadmap §P2/§P3/§P5 的验收字面，
    而"它不再只由 `sql_guard.py` 产出"这件事用一句注释就写得下。"""

    multi_statement = "multi_statement"   # …其余 21 枚显式赋值，不用 AUTO：值不许跟着成员名漂
    ...

@dataclass(frozen=True)
class Violation:  code: GuardRuleId; message: str; node: str | None = None

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

> **as-built(P5-035)**：上面那句"枚举化"在 035 之前**只有字面没有实物**——`code` 的注解是 `str`，
> 17 枚码散在 20 个"首参是裸字符串"的调用点里各自维持一致（另有 `_scan_raw_text` 的
> `(pattern, code, hint)` 三元组表带 3 枚），全仓**没有任何一处能回答"这套系统一共有哪些拒绝原因"**。
> 035 把它收成 `GuardRuleId(StrEnum)` ＋ 一条"越界就红"的机械用例，**对外值一字未改**
> （54 条必拒语料改动前后逐条同码，对照表见 §1.1.1 末与 verification §1.11）。
> 这一枚枚举是**对外词表**：新增拒绝分支只能用它的成员，改名或改值都要先改本文。
>
> **一条必须写进接口的 Python 事实**：成员值会同时被三处消费——`SqlGuardError` 的异常正文
> `f"[{rule_id}] {message}"`、`pipeline` 落库的 `sql_validation_error` 同一支串、以及
> `guard_result` 那格 JSONB。3.12 的 `Enum.__str__` 会让 `str(成员)` 与 `f"[{成员}]"` 出
> `'GuardRuleId.multi_statement'` 而不是值，所以 `(str, Enum)` 那种混写**要么显式补
> `__str__` 返回 `self.value`、要么一次纯类型重构就顺手改掉用户可见的文案**。这里选的是后者更省的一面：
> 直接继承 `StrEnum`（`__str__` 本就是值），且 ruff 的 `UP042` 在 `target-version=py312` 下就只认这一种写法。
> `json.dumps` 走的是 `_value_`，两种写法都不受影响。

### 1.1.1 拒绝词表（22 枚，as-built(P5-037)）

顺序就是 `guard()` 的判定顺序（预处理 → 裸文本 → 解析 → 顶层 → 子结构 → 函数 → 表 → 连接条件 → 钳位/重生成），
第二层那三枚排在最后——**它们不由 `guard()` 产出**，落点在 `dry_run.py`（§3）。
"落点"写**函数名不写行号**，行号会随施工漂。语料那一列的分母是**本轮实测**：现存 54 条必拒语料的
期望值分布逐条相加正好 54（17 枚有语料、`regeneration_drift` 与第二层那三枚各 0），18 条必放行同理——
这两个数是 037 文档半边的**基线**，不是 037 的结果。
037 在此基础上补 **3 条必拒 + 1 条必放行**（→ 57 / 19）：笛卡尔积那一族的落点经实测归第一层，按 §3.4
补三条必拒；必放行补的是"逗号连接 + `WHERE` 里写连接条件"那支老式合法写法——
它比新增的拒绝分支更需要钉子，因为**误伤它就等于误伤一类合法 SQL**。
那 57/19 两个数要到 037 施工半边把语料写进去、机械用例跑完才成立，本节按 §9.2 开头那条规矩
（先改本文再改实现）**先落口径**；对不上时以重测为准并回来改这一格。
> **as-built(P5-037 施工半边)：这两个数现在是对着实物量的，不是预告**。语料今天 **57 必拒 / 19 必放行**，
> 57 条期望值逐条相加正好 57（新增那 3 条全落在 `cartesian_product` 这一枚上：
> `FROM customer, order_main`、`FROM customer JOIN order_main`（不写 `ON`）、派生表内层那支——
> 第三支钉的是"判据逐 scope"，它不能由前两支代证）；分布上**有语料的枚举手 18 枚**（036 那一轮是 17 枚），
> 零语料的是 4 枚 = `regeneration_drift` ＋第二层那三枚，各由永久用例钉住（§1.1.1 开头与 §9.2 的 037 那几行）。
> 机械判据（`test_rule_id_closed_set.py`，9 条）在同一批字节上跑绿，`tests/guard/` 整目录今天收集 **236** 条
> （036 那一轮的 213 条是那一天的数，留在 `mutation_drift036.txt` 里，不改）。
**"没有一枚码没人证过"这件事的判据从 036 起改了形**：钉住处可以是语料的一条期望值，
也可以是某条**永久用例的断言**直接点这枚成员的名——`regeneration_drift` 属后者，因为它是**我们自己代码的内部
一致性故障**，今天没有任何一支模型 SQL 能触发它（实测：18 条必放行语料逐条走等价自校验，漂移候选 0 条——
那是 **036 收尾时**量的那一批字节；037 往必放行那一组补了一条，所以这一格在施工半边**按新字节重跑过**：
19 条逐条走四格，**漂移候选 0、未放行 0**（`evidence/drift_allow037.txt`，量的是 `final` 与
`parse(sql_final)` 那一对，与 §1.3 第一行同一口径），
硬要往语料里塞一条就只能是靠 monkeypatch 才过得去的死条目。机械用例仍表驱动、零平行清单：
它扫 `tests/guard/` 全目录**断言里**对 `GuardRuleId.<成员>` 的属性引用，与语料期望值取并集（036 拍板，见 §1.1.1 末）。
> **as-built(P5-037)：扫描根加了一枚**。第二层那三枚码（`too_costly` / `dry_run_unreadable` /
> `dry_run_engine_rejected`）**不可能住在 `tests/guard/`**——`guard()` 不碰库，`EXPLAIN` 的裁决与真发语句
> 都属另一个模块，把它们的用例塞进 `tests/guard/` 只是为了迁就一条机械判据的目录字面，那是让尺子决定地形。
> 判据的扫描根因此从"一个目录"变成"两枚显式列出"：`tests/guard/test_*.py` ∪ `tests/unit/test_dry_run_*.py`。
> 放宽的**只有位置**，强度一格没动：仍只认断言里的属性引用、仍不预先过滤成员名、
> 这三枚同样各配一条**失败路径用例**（不是"点过名"就算钉住）。
> 036 那一条 `regeneration_drift` 的先例在这里被用上，也顺手证了它当初那条放宽是对的。
>
> **as-built(P5-036 收尾)：这一档"放宽"被双轴审出过一个空洞，已按实物补严**。判据第一版扫的是全文件的
> 属性引用并预先 `node.attr in members` 过滤过——那个过滤让"有人把成员名拼错、或删了成员"这一类**永远红不了**
> （非成员值在构造上就进不了集合）。收尾版改成两问分别断：**只认 `ast.Assert` 里的引用**（写在注释、docstring
> 或游离赋值里的点名不算钉住），**且不预先过滤**，非成员的名字单独一条用例红。放宽的代价（"点过名"≠"分支被走过"）
> 仍然靠 `regeneration_drift` 自己那条失败路径用例兜，机械判据管不到那一层，这一句留在文档里而不是藏在注释里。

| `rule_id` 的值 | 拦的是什么 | 落点 | 语料条数 |
|---|---|---|---|
| `empty_statement` | 剥围栏、去尾随 `;` 之后没有内容；以及"整条都是注释" | `_preprocess`、`_scan_raw_text` | 2 |
| `too_large` | 长度超 8000 字符 | `_preprocess` | 1 |
| `unsupported_construct` | **证不了的四种**：控制字符、块注释未闭合、sqlglot 任何解析错误（`ParseError`/`TokenError`/`UnsupportedError`）、解析结果不是 AST 表达式 | `_preprocess`、`_strip_comments`、`parse` | 6 |
| `hint_not_allowed` | `/*+ … */` 优化器 hint——放行等于承认"注释里能塞指令"，而重生成又会抹掉注释，两者不能兼得 | `_strip_comments` | 1 |
| `executable_comment` | `/*!50100 … */` 这类 MySQL 里**真会执行**的可执行注释 | `_strip_comments` | 2 |
| `procedure_analyse` | `PROCEDURE ANALYSE(`：会让 parser 直接失败，只能靠裸文本兜住 | `_scan_raw_text` 三元组表 | 1 |
| `locking_clause` | `FOR UPDATE` / `FOR SHARE` / `LOCK IN SHARE MODE`：`FOR SHARE o.id` 这种带目标写法解析不了，所以走裸文本 | `_scan_raw_text` 三元组表 | 3 |
| `into_outfile` | 导出与建表两类：`INTO OUTFILE`/`DUMPFILE`/`INTO @var`（裸文本），`SELECT … INTO`（AST 里的 `exp.Into`） | `_scan_raw_text` 三元组表、`_shape_violations` | 3 |
| `multi_statement` | `;` 拼贴（`SELECT 1; DROP TABLE x`）：`len(stmts) != 1` | `parse` | 3 |
| `prepared_statement` | `PREPARE`/`EXECUTE`/`DEALLOCATE`/`DISCARD`：正文绕过静态分析 | `_check_top_level` | 2 |
| `top_level_not_select` | 顶层不是 `Select/Union/Except/Intersect`（`SHOW`/`DESCRIBE` 落成 `Command` 也被这一支挡） | `_check_top_level` | 10 |
| `dml_in_cte` | CTE / 子查询里藏 `INSERT`/`UPDATE`/`DELETE`/`MERGE` | `_shape_violations` | 1 |
| `variable_access` | 变量、占位符、`:=`：取值要到运行时才知道 | `_variable_violations` | 5 |
| `pg_file_access` | pg 侧读文件/目录/时钟/锁/取消后端的具名特权函数（`pg_read_file`、`pg_sleep`…） | `_function_violations` | 4 |
| `sleep_function` | MySQL `SLEEP()`：它让语句超时形同虚设 | `_function_violations` | 1 |
| `danger_function` | **兜底那一档**：sqlglot 认不出的函数一律落成 `exp.Anonymous`，一律拒。新增的危险函数无需改这里就已经被挡 | `_function_violations` | 2 |
| `table_not_allowed` | 两种：系统字典表（`information_schema`/`mysql`/`pg_catalog`… **即便已授权也拒**）、白名单外 | `_table_violations` | 7 |
| `cartesian_product` | **第 19 枚（as-built P5-037），仍属第一层**：某个 scope 里有"`on`/`using`/`side`/`kind`/`method` **五格全空**"的 join 节点，**且该 scope 没有 `WHERE`** → 连接条件无从推出，行数上限只能按两表之积（`method` 那一格是实测逼出来的，不查它就会误拒自带条件的 `NATURAL JOIN`，§1.2 ⑥.1）。实测覆盖面比 §3 原话（"只拒无 join 条件的逗号连接"）宽一格：`FROM a JOIN b`（不写 `ON`）在 sqlglot 里与逗号连接**逐字同形**，AST 分不开（§3.4）。`CROSS`/`NATURAL`/`STRAIGHT_JOIN` 与"逗号连接 + `WHERE`"不在这一支 | `_join_violations` | 3 |
| `regeneration_drift` | **第 18 枚，也是唯一一枚防的是我们自己的代码**：`final` 序列化出的 `sql_final` 再 parse 一次，**一条不动点加三格承重事实**（顶层形状 / 表引用 / 顶层行数）任一格红即漂移——它红了就意味着"不知道执行的是哪条 SQL"，按拒答处理。它**不是**用户 SQL 违规，所以不许挂 `unsupported_construct`（那等于把我们的 bug 报成用户的错） | `guard` 出口处的 `regeneration_violation`（§1.3） | 0（钉在 `tests/guard/test_regeneration_invariant.py`，理由见本节开头） |
| `too_costly` | **第二层第一枚**：`EXPLAIN` 逐行 `rows` 的**最大值**（单次扫描的最大估行）> `AIWEB_QUERY__MAX_EXPLAIN_ROWS`。尺子为什么取最大值而不是乘积/求和，见 §3.2 的实测表——乘积会把一条合法的 `UNION` 问句拒掉 | `dry_run.explain_verdict`（§3.2） | 0（语料管不到第二层，钉在 `tests/unit/test_dry_run_shape.py`，见本节开头那条扫描根注） |
| `dry_run_unreadable` | **第二层第二枚**：`EXPLAIN` 回的结果里**一行估行都读不出来**（全 `NULL`／没有 `rows` 这一列／回零行），而引擎又没有逐行自证 `optimized away`。**2026-10-20 拍板**（工单 037 留给施工的第三格）：新枚而不是挂 `unsupported_construct`——后者是"sqlglot 证不了"，这一枚是"引擎的读数取不到"，两件事的修法与责任方不同 | `dry_run.explain_verdict` | 0（同上；演示源上今天不可达，由桩件驱动，见 §3.2 第 2 格） |
| `dry_run_engine_rejected` | **第二层第三枚**：`EXPLAIN` 语句**自己**被源库拒（实测 `1054 Unknown column`／`1146 Table doesn't exist`／`1221 UNION 与 LIMIT`），message 带**源库原话**——它是 038 回喂模型的正文来源，所以"用户的 SQL 违规"与"我们读不出来"必须是两枚码（§3.2 第 3 格） | `dry_run.run_dry_run` | 0（同上） |

**与 §1.2 那些大写名的关系**（评审期写法速记，实物从来不是这个拼法）：
`MULTI_STATEMENT`→`multi_statement`、`ROW_LOCKING`→`locking_clause`、
`VARIABLE_ACCESS`→`variable_access`、`TABLE_NOT_ALLOWED`→`table_not_allowed`、
`SELECT_INTO`/`INTO_OUTFILE`/`INTO_DUMPFILE`→**合成为一枚** `into_outfile`、
`CROSS_CATALOG` **没有对应成员**（实现折进 `table_not_allowed`，见 §9.2）。
反过来，17 枚里有 **12 枚是 §1.2 没有的**（`empty_statement`、`too_large`、`unsupported_construct`、
`hint_not_allowed`、`executable_comment`、`procedure_analyse`、`prepared_statement`、
`top_level_not_select`、`dml_in_cte`、`pg_file_access`、`sleep_function`、`danger_function`）——
它们来自 003 施工时"sqlglot 宽容 parser 需要正则级兜底"与"未知一律拒"那两条实测（§9.1）。
**词表以本表为准**，§1.2 的大写名不要再当可 grep 的码。
（as-built(P5-036)：第 18 枚 `regeneration_drift` **同样不在 §1.2 里**，且它连"拦的是一段 SQL"都不是——
§1.2 那张清单从头到尾讲的是"模型的 SQL 里有什么不许"，而这一枚讲的是"守卫自己的输出对不对"。
两者不该混在一张清单里，所以 §1.2 不加行。）

> **as-built(P5-037)：`TOO_COSTLY` 这一格被实测否掉，订正如下**。本节原来写的是"`TOO_COSTLY`（§0 表与
> §2 的 dry-run 拒因）**不在这 18 枚里**：它属第二层，今天零实现，随工单 037 加进同一枚枚举"。
> 三件事都只对了一半：① 加进枚举这件事 037 做了，但**加了四枚而不是两枚**——`too_costly`、
> `dry_run_unreadable`（工单点名的那两枚），外加 `cartesian_product`（§3.4：实测证明笛卡尔积的形状在 AST
> 上可见，归第一层）与 `dry_run_engine_rejected`（§3.2 第 3 格：`EXPLAIN` 自己被源库拒既不是"读不出来"
> 也不是 sqlglot 的解析失败，而 038 要拿它的正文回喂模型。工单只钉了两枚的命名，第三、第四枚按它
> "施工时定、定了就进枚举并写进文档"那条口径落在施工时，这里就是那份记录）；
> ② 码的**字面**按实物从小写：文档大写、语料期望值小写，两半边对不上时以落库那一侧为准；
> ③ "不在这 18 枚里"这个说法本身随 037 失效——这枚枚举从"第一层的词表"变成**三层共用的拒绝词表**（§1.1）。
> 035 的机械检查是**表驱动**的这条仍然成立：加成员只动枚举一处 + 钉住处一处。
> 036 把"钉住处"从"必须是语料"放宽成"语料或用例"（见本节开头），037 就直接用上了那次放宽——
> 三枚第二层的码**在结构上不可能有语料**（`guard()` 不碰库），没有那次放宽，本片要么红在封闭集合那条用例上、
> 要么往语料里塞一条走不到的死条目，两个都是伪造证据。

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

**⑥.1 连接条件扫描（as-built(P5-037)，新枚 `cartesian_product`）**

§3 原来把笛卡尔积挂在**第二层**。工单 037 的"已定口径"把落点交给实测：AST 可见就归第一层。
实测（sqlglot 30.19.0，`probe_cartesian_ast037.txt`）可见，所以这一支落在守卫的 `_join_violations`，
逐 scope 判定：

```
对每一个 Select 作用域：
  存在 exp.Join 满足  on 空 且 using 空 且 side 空 且 kind 空 且 method 空
  且该作用域没有 WHERE
  → cartesian_product
```

- **`method` 那一格是实测逼出来的**（第一版只查 `side`/`kind`，会把 `NATURAL JOIN` 一起拒掉）：
  sqlglot 30.19 的 `Join` 把修饰词分住三个 key——`side`（`LEFT`/`RIGHT`）、`kind`
  （`CROSS`/`STRAIGHT_JOIN`）、`method`（`NATURAL`），`NATURAL JOIN` 的 `side`/`kind` **都是空串**、
  只有 `method='NATURAL'`（`args keys` 与 `sql()` 原文实测在 `probe_cartesian_ast037.txt` 末两段）。
  `NATURAL JOIN` 自带连接条件（按同名列），不是笛卡尔积，所以判据必须把 `method` 算进去。
- **为什么带 `WHERE` 就放行**：`FROM a, b WHERE a.id = b.aid` 是老式合法写法，实测与"没有连接条件"
  在 AST 上给出的信息量差别就只有"有没有 WHERE"这一格——要再往下判就得比对 WHERE 里的列属于哪张表，
  那是把连接图重建一遍，误报率与 §9 里"其余放行"那半句同源，不在 v1 范围。
  **代价说清**：这一支挡的是"完全没条件"的那一档，`FROM a, b WHERE a.x > 1`（笛卡尔积 + 无关条件）
  **今天照样放行**（实测 `where=True`、形状与合法的老式写法同构）。
- **覆盖面比 §3 原话宽一格**：`FROM a JOIN b`（显式 `JOIN` 不写 `ON`，MySQL 允许）不只节点形状相同，
  **重生成出来的字面就是 `, b`**——与逗号连接在 AST 与文本两侧都分不开（同那段实测）。所以这一支一并拒掉。
  要按 §3 字面只放它过就得回到原 SQL 找那个逗号——正是工单禁止的方向。理由与实测都记在 §3.4。
- `CROSS JOIN`（实测 `kind='CROSS'`）与 `STRAIGHT_JOIN`（实测 `kind='STRAIGHT_JOIN'`）**不在这一支**：
  判据是"`kind` 非空即跳过"，所以它们**即便没有连接条件也放行**——`FROM a STRAIGHT_JOIN b` 不带 `ON`
  在 MySQL 里就是笛卡尔积，这一格今天挡不住，留在 §9 说清，不许在交付记录里含混过去。
  `NATURAL JOIN` 也不在这一支，但它是**应该**放行（它自带按同名列的连接条件），理由见上面 `method` 那条。
  派生表/子查询内部的逗号连接**在**这一支——实测 `find_all(exp.Join)` 看得见内层 scope。

**⑦ 强制 LIMIT**

**as-built(2026-09-29 拍板后)**：顶层 LIMIT 走"钳位 + 探针"，不是"缺了才补"。模型写的行数 `n` 只有是
**一个确定的非负整数且 `n < max_rows`** 时才原样保留（"前 5 名"不该被抬到上限），其余一律重写成
`max_rows + 1`，覆盖三种情形：① 没有顶层 LIMIT；② `n >= max_rows`——**含模型照 ④ 的 prompt 模板写
`LIMIT {{ row_limit }}` 这一真链路常态**；③ 行数**证明不了**：`LIMIT ALL`（pg 语法，mysql 方言下落成
标识符，重生成出来源库不认）、`LIMIT 1 + 1`（表达式）、`LIMIT 1e3`（数字字面量但 `int()` 认不出）、
`LIMIT -1`（sqlglot 落成 `Neg`）。注意 `LIMIT -1` 归这一档的理由**不是**"MySQL 里 -1 等于不限"
——那是 SQLite 的口径，MySQL 给负数 row_count 会报 1210；我们只是读不出确定的非负行数。
钳位顺带把这条本来必然失败的语句变成一条能跑的截断查询。

不变式（比"注入值等于 `max_rows+1`"更弱、也更要紧的一条）：**守卫输出的顶层 LIMIT 恒 `<= max_rows+1`**。
011 的缓冲取回内存上限压在它身上。钳位只管**顶层**：臂内/CTE/派生表里的 `LIMIT 5000` 不钳，
MySQL 的顶层 LIMIT 约束整条语句的返回行数，客户端缓冲因此仍有界，但源库侧物化多少行我们管不着
（那是性能问题，不是 §4.1 的内存前提）。

钳位只动 count 不动 offset（`LIMIT 4990, 5000` → `LIMIT 1001 OFFSET 4990`），改了 offset 就不是
"少给几行"而是换了一页数据。包装写法 `exp.select('*').from_(ast.subquery('__aiweb_q')).limit(max_rows+1)`
在 MySQL 5.7 上会被优化器丢掉子查询里的 ORDER BY，**实测更稳的做法是直接 `ast.limit(max_rows+1)`
加在最外层**，不包一层（代码里保留这条取舍注释）。

> **真链路实录（2026-09-29）**：问"把订单明细表的所有行都列出来"（演示库 `order_item` 8.8 万行、
> 该源 `row_limit=1000`），`chat_messages` 留痕是
> `sql_raw = SELECT order_id, qty FROM ai_web_demo.order_item LIMIT 1000` →
> `sql_final = … LIMIT 1001`，`result_stats = {row_count: 1000, truncated: true}`。
> 模型写的行数**正好等于**上限，正是闭环前探针消失的那一支；⑨ 的结论文案也因此第一次说得出
> "已被行数上限截断"。
> **这条闭环的边界要说清**：模型自己写 `LIMIT n`（`n < max_rows`）那一支**仍然没有探针**，
> `truncated` 恒假——这是有意的语义（行数上限没碰到，"被截断"就不成立），不是残留的洞。
> 原写的 `AIWEB_GUARD__FORCE_LIMIT` / `AIWEB_QUERY__HARD_LIMIT` 两个键**不建**（见 §4.2 与 §4.3
> 的 as-built，注入值就是 `resolve_row_limit(ds) + 1`）。

**⑧ 重新生成（不是原样执行 LLM 文本）**

```python
sql_final = stmt.sql(dialect=dialect, comments=False, pretty=False)
```

见 §2。

**⑨ 引擎 dry-run** 见 §3。

### 1.3 守卫一致性不变式（防 rewrite 漂移）

本节原来把三条都写成"守卫内部的动作"。**as-built(P5-036) 对着实物改口**：三条里有两条今天住在
测试侧，第三条（等价自校验）从 036 起才真在守卫内部。落点逐条写明，不再含糊：

| 不变式 | 落点（as-built 036） | 实物 |
|---|---|---|
| 顶层 LIMIT 恒 `<= max_rows + 1` | **测试侧** `tests/guard/test_guard_api.py::test_钳位不变式_守卫输出的顶层_LIMIT_恒不超过_max_rows_plus_1` | 对 11 种形状逐条 parse 输出、**独立**读一遍顶层行数（不复用 `_limit_row_count`，否则是自证） |
| 输出不含 `;`、不含 `/*` | **测试侧** `tests/guard/test_allow_corpus.py::test_合法只读查询被放行并强制带上limit` | 必放行语料逐条（036 那一份是 18 条，037 起 **19** 条；辖区见 §1.3 末那条注：这两条不是全称量词） |
| 重生成的 SQL 还能被严格解析（幂等） | **守卫内部**（036 起），另有测试侧同一件事的用例 `tests/guard/test_allow_corpus.py::test_合法只读查询被放行并强制带上limit` 最后两行 | 再 parse + 再生成，两次文本逐字相等。**as-built(P5-036)**：这一行的落点原来只写"测试侧"，036 起它自己就是那四格里的 ① 不动点；另外"再 parse 都过不去""落回两条语句"这两种形状也由 `regeneration_violation` 归 `regeneration_drift`（钉子：`test_端出去的文本连再解析都过不去时归因还是regeneration_drift`、`test_端出去的文本落回两条语句时那一格红`），测试侧那条用例保留不动 |
| **重生成等价**：`final` 判过的那些**承重事实**在 `parse(sql_final)` 里还在 | **守卫内部** `regeneration_violation`（036 起，每次 `guard()` 都跑） | 见下。**as-built(P5-036) 收尾**：这个名字**不带下划线是刻意的**，它是本节这张表的一个公共面——用例要能**单独**把某一格驱动成红，就必须直接递给它一对（树，文本）；只从 `guard()` 那一道缝进，②③④ 三格今天没有任何输入能让它们红而另外三格绿。所以它按"文档化的对外面"记账，不视作可随手私有的内部 helper |

> **as-built(2026-09-29)**：第一条原来是 `endswith("LIMIT 1001") or "LIMIT" in upper()`——那个 `or`
> 让任何带 LIMIT 的语句都算通过，等于没断。钳位落地后它才第一次有内容：**顶层 LIMIT 恒不超过
> `max_rows+1`**（值不一定是 `max_rows+1`，模型写了个更小的数时就是那个数）。

**守卫内部那一条的确切形状（这是本节唯一的实物新增，防的不是模型而是我们自己的代码）**：

- **比对的两侧是钳位之后的 `final` 与 `parse(sql_final)`，不是模型原文那棵 `stmt`**。
  `guard()` 自己会注入/改写顶层 LIMIT，所以"重生成 ≠ 原文"是**设计**而不是漂移——拿 `stmt` 来比，
  每一条缺 LIMIT 的合法查询都会报内部故障。这一格是整节最写错就全反的地方。
- **跑的是"一条不动点 + 三格承重事实"，每一格都有自己的红法**：
  ① **不动点**：`sql_final` 再 parse、再 `.sql(comments=False)`，两次文本**逐字**相等；
  ② **顶层形状**：`type(final) is type(parse(sql_final))`；
  ③ **表引用**：重 parse 回来那棵树引用的表集合与白名单判过的 `refs` **逐字相同**（比集合不比顺序）；
  ④ **顶层行数**：两边用同一个 `_limit_row_count` 读出的那个数一致。
  > **as-built(P5-036)：②③④ 这三格是**换掉** `Expression.__eq__` 得来的**，本节第一版按工单字面写的
  > 是"不动点 + 结构相等两条腿"。对着实物发现 `__eq__` **两头都不对**：
  > 它**过火**——`SELECT id FROM customer LIMIT 5000 UNION ALL SELECT id FROM payment_record LIMIT 3`
  > 今天合法放行，而生成器会给带 LIMIT 的那条臂加括号，重 parse 回来 `.this` 从 `Select` 变成
  > `Subquery`，树差就落在这一格上（`evidence/drift_diagnosis036.txt`），语义一字未变却判成漂移；
  > 它又**不及物**——30.19 下 `__eq__` **看不见注释的丢失**：探针里 PG 那支 `/*+ SeqScan(t1) */`
  > 重 parse 回来树上已经没有那个节点（同一支里并排写着"重 parse 之后 hint 还在树上？ False"与
  > "tree == reparsed（sqlglot `__eq__`）？ True"），MySQL 的普通注释那支同形
  > （`evidence/hint_probe036.txt`；注意**不是**"带注释那份重 parse 回来 == 原树"那一行——那一行比的是
  > 带注释的往返，`True` 是应当的，证不了 blindness）。
  > 换形的口径由用户 2026-10-20 拍板：**按守卫判过的那些承重事实比，渲染形状不进判定**。
  > 钉子是 `test_守卫给臂加的括号不算漂移`——`__eq__` 那一版它红，四格这一版它绿。
  四格的分工人也是实测出来的：`evidence/mutation_drift036.txt` 的 B/C/D/E 四格各把一格改成
  "永不成立"，每格**只**红自己那一条用例、`tests/guard` 整目录（本轮 213 条）一条不多红——
  没有哪两格互为重复。
- **红了就拒答，不降级成 warning**：`guard()` 把它收成一条 `Violation(regeneration_drift)` 返回
  `ok=False, sql_final=None`；`check()` 那一侧同码抛 `SqlGuardError`。理由同 §9 那族"证不了就按最严的一档"——
  这一条红了我们连"执行的是哪句话"都不知道。
- **它今天是一条永真的保险丝**（这是实测结论，不是修辞）：18 条必放行语料逐条走四格，漂移候选 **0**
  （量的是 `final` 与 `parse(sql_final)` 这一对，也就是本表第一行那句"比对的两侧"）；
  **as-built(P5-037 施工半边)**：这一格随语料补到 19 条后**按新字节重跑过**（`evidence/drift_allow037.txt`：
  mysql 方言、`max_rows=1000`、非抛异常的 `guard()`，与 `test_allow_corpus.py` 同一走法），
  结论一字未动——**19 条里漂移候选 0、未放行 0**。新补的那一支（`FROM customer c, order_main o WHERE …`）
  在四格上与其它支同形，因为它正是 §3.4 说的"老式写法与合法形状在 AST 上分不开"那一条。
  `evidence/hint_probe036.txt` 末节那个"漂移候选 **4** 条"**不是另一个结论**，它量的是**另一对**——
  模型原文 `stmt` 与钳位之后的 `final`，而那 4 条逐条标着"钳位改了形状（预期内）"，
  正是 §1.3 开头那句"重生成 ≠ 原文是**设计**"的实物清单。两个数各说各的两侧，不许互相引用来改口径。
  而工单 036 验收 2 字面指定的两发变异**都造不出漂移**——`.limit()` 是替换不是叠加（电池里的 J 格实测：
  钳位条件恒真之后红的是**钳位语义**那三条用例，036 的自校验一条都不红，因为钳位后那棵树自己就是自洽的）；
  注释在 parse **之前**就被 `_strip_comments` 抹掉，树上一个 `.comments` 节点都没有，
  所以把出口的 `.sql(comments=False)` 写成 `comments=True` 对守卫的输出**逐字相同**
  （实测 `evidence/comment_free_parse036.txt`：5 支带注释的必放行语料两版一字不差）。
  如实记在这里，不为了"验收 2 变红"去故意写错实现。
- **变异自证因此重排成十格**（`evidence/mutation_drift036.txt`，每格 try/finally + md5 逐字复原；
  实录的源文件 md5 是 `78a91357…`——**最终 shipped 那一份**。上一版电池跑在 mypy 那两处理顺**之前**的
  `f2c8dd4a…` 上，锚点 `type(tree)` 与 `set(refs)` 在最终字节上已经不存在，按 035 立的
  "补跑而不是解释"这条纪律整电池重跑过）：
  A 摘掉整道自校验 → **只**红在"接线"那两条（出口真的又解析了一次 / 出口解析失败按拒答处理），
  其余一条不红——这就是"纯保险丝"的实物形态：真实输入的判定不因它而变；
  B~E 一格一格关掉 → 每格只红自己那一条（上一条说的分工实证）；
  F 就是工单字面那一发（`comments=True`）→ 0 红，**等价变异**，理由见上一条；
  G 把比对左侧从 `final` 换成入口那棵 `stmt` → 14 红，全是钳位过的合法查询被误判成内部故障——
  这一发证明"比对的两侧是 `final` 与 `parse(sql_final)`，不是 `stmt`"那句话是**承重**的，不是措辞；
  H/I 两发不是今天存在的 bug，而是这道自校验**存在的理由**：把端出去的文本里 ` LIMIT 1001` 抹掉
  → 15 红，把 `customer` 换成 `payment_record` → 27 红，两发都在守卫出口被拦下（分别打在 ④ 与 ③）；
  J 钳位条件恒真（工单字面第一发的实物版）→ 3 红，红的**全是钳位语义**那三条
  （`test_上限之下的_LIMIT_原样保留`、带 OFFSET 那支语料、括号那支钉子），**自校验一条都不红**——
  这一格就是"那一发造不出漂移"的凭证，不是分析句。
- **运行期这一条不是 Python 的 `assert`**：`python -O` 会把它整条剥掉，而这条断言的全部意义是
  "生产路径上每次守卫都跑"。它是一次真调用（`regeneration_violation`）加一条永久用例。
  工单 036 验收 3 的字面"`grep -n assert` 从此不再是零命中"因此按实物兑现为
  "守卫内部有一条真会生效的运行期自校验，且它的失败路径有永久用例"——
  顺带记一笔：035 之后 `grep -n assert app/services/sql_guard.py` 命中的那**一**行住在
  `GuardRuleId` 这枚枚举的类 docstring 里（写的是 `check()` 那族测试的断言该长什么样），不是运行期语句。
- **性能这一格量化在这里**（口径没改，四格都在运行期每次都跑）：关掉自校验的 `guard()` 0.702 ms/次
  → shipped 1.273 ms/次（+0.571 ms，+81.2%；参照物：单跑一次 `parse(sample).sql(comments=False)`
  是 0.502 ms。1000 次取均值，`evidence/perf036.txt`）。
  文档半边那一版记的是 0.619 → 1.090，那是 `__eq__` 版的同一口径实测（`evidence/hint_probe036.txt` 末节），
  换形之后按实物复测过才写这两个新数。**收尾又复跑了一次**：上一版记的 0.712 → 1.279 落在最后一次改动
  （把比对里重复三次的 `set(refs)` 提出去）之前的字节上，同一份 shipped 字节（md5 `78a91357…`）连跑三轮
  的增量落在 0.571~0.592 ms——**这个幅度的来回是噪声，不要读成"那一次提取省了什么"**。
  绝对值相对一次 LLM 调用与一次真库查询差着两到三个数量级，所以不拿"只抽样几条"糊过去，
  也不因为"多一次解析太贵"而把某几格挪出运行期。

以及"重生成后再 parse 一次的 AST 与首次 parse 的 AST 归一化相同"——这句话的**主语在 036 订正为
`final`**（上一条第一个项目），原写的"首次 parse 的 AST"按实物是错的。
"归一化"的定义就是那四格（一条不动点 + 三格承重事实），不另造一套规范化。
重跑（`POST /chat/messages/{id}/rerun`）**绝不复用旧 `sql_final`**，同样走完整 guard。

> **"守卫执行的是重生成的 SQL"这一半早在 036 之前就是实物**，第一次点名链：
> `guard()` 返回 `sql_final=final.sql(dialect=dialect, comments=False)`（`app/services/sql_guard.py`
> 的 `guard` 出口）→ `pipeline.ask()` 里 `out.sql_final = msg.sql_final = guarded.sql_final` →
> 同函数下一步 `execute_readonly(..., sql_final=guarded.sql_final or "", ...)`。
> 中间没有任何一步把模型的 `sql_raw` 递给执行器。

---

## 2. `comments=False` 重生成不变式与 hint 拒绝

`stmt.sql(comments=False)` 是挡住 **MySQL 可执行注释** 的关键安全参数：
`/*!50100 UNION SELECT ... */` 这类注释在 sqlglot AST 里是 comment，重生成时被抹掉 →
天然挡住这条经典绕过（对应语料 `executable_comment`）。顺带完成方言归一：
AI 写了 `LIMIT 10` 给 PG 也能被重写成合法形态。重生成结果必须满足不变式：**不含 `;`、不含 `/*`**。

**副作用与对策（这是设计里最微妙的一条）**：`comments=False` 会剥掉注释，而 hint 长得像注释——
这会造成"**守卫看到的 SQL ≠ 实际执行的 SQL**"，属于最危险的一类不一致。因此定稿的规则是：

1. **检测到任何 hint 节点即拒绝**（`rule_id = hint_not_allowed`）——模型本来就不该生成 hint。
2. **超时必须走会话变量而不是 hint**：MySQL 用 `SET SESSION MAX_EXECUTION_TIME = <ms>`，
   PG 用 `SET LOCAL statement_timeout`。会话变量作用于之后所有 SELECT，天然覆盖 `UNION` 场景，
   也就不需要再判断"UNION 不加 hint"。
   （`AIWEB_QUERY__USE_SESSION_MAX_EXEC_TIME=true` 即此语义。）

> 早期草案曾打算在 SQL 前挂 `/*+ MAX_EXECUTION_TIME(n) */` 做双保险（因为 session 变量可能被中间件重置），
> 该做法与 `comments=False` 冲突，已作废；现在只保留 `SET SESSION` 路径。

**as-built(P5-036)：上面那句"`comments=False` 会剥掉 hint"按两支方言分开订正**（实测见
`.scratch/p5-sql-guard-hardening/evidence/hint_probe036.txt`，sqlglot 30.19.0）：

| 形状 | 树上的落点 | `.sql(comments=False)` 之后 | 结论 |
|---|---|---|---|
| MySQL `/*+ MAX_EXECUTION_TIME(1) */`、`/*+ NO_ICP */`（含 JOIN 那一支） | **`args["hint"]` 上的 `exp.Hint` 真节点**，内含 `Anonymous`/`Var` | **`/*+ … */` 原样在场**，且再 parse 一次它还活着 | **剥不掉**。`comments=False` 只处理 `Comment`，不处理 `exp.Hint` |
| PG `/*+ SeqScan(t1) */`、PG `/*+ x */` | 树上**没有** `exp.Hint`（落成注释） | 不在场（`comments=True` 才出 `/* + x */`） | 剥得掉 |
| MySQL `/* +MAX_EXECUTION_TIME(1) */`（`+` 前有空白） | 没有 `exp.Hint` | 不在场 | 放行是**对的**：MySQL 的 hint 语法要求 `/*+` 紧贴，源库不执行它。我们的判据是 `body.startswith("+")`，**与源库同构**，不是写松也不是写紧 |
| `SELECT '/*+ MAX_EXECUTION_TIME(1) */' AS remark`（hint 写在字符串字面量里） | 没有 `exp.Hint` | **在场**，因为它是数据 | 放行是**对的**。掩码扫描先把它认成字面量，所以既不误拒也不漏判 |

由此得到三条要长期成立的口径：

- **hint 的防线只有一道，就是裸文本层**。`_strip_comments` 在 AST 之前抛 `hint_not_allowed`，
  今天语料里那条 `SELECT 1 /*+ MAX_EXECUTION_TIME(1) */` 钉的就是它。它**不是**"额外保险"——
  MySQL 那一支如果摘掉，hint 会原样穿过钳位与重生成、被源库真的执行，而守卫的 `sql_final` 里
  也看得见它（表第一行）。这就是为什么拒绝必须落在裸文本层而不是等 AST：AST 层看见 hint 时，
  它已经是会被序列化出去的一部分了。
- **`comments=False` 不是 hint 防线**，它是**可执行注释**（`/*! … */`）的防线，
  且那一条也靠 `_strip_comments` 先拒（`executable_comment`）才成立——两件事原来写在一起，现在分开。
- **重生成等价自校验（§1.3）不能当 hint 的防线用**——四格的口径下这一点更要说清：MySQL 的 hint 在
  四格上**全都一致**，它活过了序列化，`final` 与 `parse(sql_final)` 两边都带着它，顶层形状、表引用、
  顶层行数没有一格会因为它而变（`evidence/hint_probe036.txt` 的 MySQL 两支都记着
  "两次文本逐字相等？ True" 与 "tree == reparsed（sqlglot `__eq__`）？ True"）。等价自校验量的是"端出去的还是不是
  判过的那句"，而 hint 的问题是"判的时候就不该放行"，两件事正交。
  所以 hint 归 hint、等价归等价，两族用例各钉各的：前者钉在
  `test_hint_在_MySQL_的树上活得过_comments_false`，后者钉在 `test_regeneration_invariant.py`。

> **顺带把 §1.3 那两条测试侧断言的辖区说清**（036 实测，探针表末行）：
> `";" not in sql_final` 与 `"/*" not in sql_final` **不是对全体合法输入都成立的不变式**——
> `SELECT '/*+ MAX_EXECUTION_TIME(1) */' AS remark FROM customer` 这类把 hint 写在**字符串字面量**里的
> 查询是合法的、且 `sql_final` 里就带着 `/*`（同理 `SELECT 'a;b'` 带着 `;`）。它们今天全绿是因为
> 必放行语料（037 起 **19** 条，量的是 `tests/guard/corpus.py` 今天那份）里没有这种字面量——新增那第 19 条
> `SELECT 'a INTO OUTFILE b' AS s FROM customer LIMIT 5` 带的是一枚**不含 `;` 也不含 `/*`** 的字面量，
> 它补的是正则兜底"跳过引号内内容"那一格，不碰这两条断言的辖区。真正要钉的性质是"注释与 hint 不会以**语法成分**的身份进到树上"，
> 不是"这两个子串不出现"。下一位若把这两条当全称量词去推广，就会在加语料的那一刻撞红——
> 撞红了是对的（说明该重新表述），不要改成"跳过字面量"了事，那样这条断言就谁也不防了。

---

## 3. 第二层：引擎 EXPLAIN dry-run

弥补 sqlglot "宽容非校验"的第二道独立闸门。

> **as-built(P5-037)**：本节整块在 037 之前**是零实现**。开工前重核过的事实账（2026-10-20，认字不认行号）：
> `AIWEB_QUERY__DRY_RUN` 这个**键根本不存在**（`settings.py` 的 `QueryGroup` 没有 `dry_run` 字段，
> `.env.example` 的 `AIWEB_QUERY__` 那一组没有这一行）；`max_explain_rows: int = 50_000_000` **只有定义点、
> 全仓零读取点**；`EXPLAIN` 在 `app/` 下只出现在注释里（`datasource_service.py` 说"第二层 EXPLAIN dry-run"），
> 没有任何实现。所以三层防御当时的实物是**两层**：第一层 AST 白名单 + 第三层会话级 SET 序列。
> 下面按落地情况把本节重写一遍，每小节末尾那一格是"今天到底有几分实物"。

### 3.1 跑在哪条 SQL 上、住在哪个模块

- **只跑在 `sql_final` 上，绝不跑模型原文**。036 那条"执行的就是守卫看到的"等价断言把第一层与执行钉在
  同一件东西上，第二层必须钉在**同一件东西**上，否则两层各验各的。
- 落点是 `app/services/nl2sql/dry_run.py`，由 `executor.py` 在**第三层那条只读会话建好之后、发真 SQL 取行之前**
  调用。它与第三层分家成两个模块是刻意的："分层独立有效"（roadmap §P5 验收 4 的立论）要求第二层能被单独关掉、
  单独反证；塞进 `executor.py` 它就只是第三层里的一段代码，"关掉第二层"这种用例就写不出来。
- MySQL 一支**真跑**：`EXPLAIN <sql_final>`（5.7 对 SELECT 支持；`EXPLAIN` 不执行）。
- **PG 那一支今天只有 SQL 形状、没有真跑**（2026-10-20 拍板）。`explain_statements("postgres", …)` 编译出
  §3.1.1 那四条文本、由用例逐字断（`BEGIN READ ONLY` 在 `EXPLAIN` 之前、`ROLLBACK` 在最后、
  `SET LOCAL statement_timeout` 用的是 `'3000ms'` 那个字面），但执行路径今天对 PG 直接
  `NotImplementedSource`（`executor.py` 那一发 501），语句发不出去，也就**没有真库行为证据**。
  行为用例等"PG 只读执行"落地那天补——那是另一件更大的事，不属本片。

```sql
-- 3.1.1 PG 的四条（形状已钉，今天无真跑）
BEGIN READ ONLY; SET LOCAL statement_timeout='3000ms'; EXPLAIN <sql_final>; ROLLBACK;
```

### 3.2 估行怎么读（这一段是实测写的，不是设计写的）

尺子住在纯函数 `explain_verdict(lines, *, max_explain_rows)` 里——**裁决不连库**，真发语句那一半在
`run_dry_run(...)`。实物口径来自真 MySQL 5.7.17 + 演示库 `ai_web_demo` 的 13 条"用户真会问"的形状
（`.scratch/p5-sql-guard-hardening/evidence/probe_explain037.txt`、`probe_explain_null037.txt`、
`probe_explain_json037.txt`）：

| 实测事实 | 对裁决的后果 |
|---|---|
| 传统 `EXPLAIN` 回 12 列（`id/select_type/table/partitions/type/possible_keys/key/key_len/ref/rows/filtered/Extra`），估行在 `rows` 那一列，**逐表一行** | 尺子就是"逐行读 `rows`" |
| 13 条里 **3 条给出 `NULL`**：裸聚合 `count(*)`（`[None]`）、派生表（`[1, None]`）、`UNION`（`[30226, 87912, None]`） | "读不出来"必须先定义清楚，否则**最常见的那一类问句**（"有多少订单"）被误杀，而 `DRY_RUN` 默认开、prod 不能关 |
| `count(*)` 那一行不是"引擎不知道"：`Extra` = `Select tables optimized away`，`FORMAT=JSON` 给的是 `"message": "Select tables optimized away"`（整个 JSON 里**没有任何 cost/rows 键**，实测原文在 `probe_explain_json037.txt`）——引擎在说**它证明了不需要扫表** | 这一格属"**证得了它小**"，不是"证不了"。它必须放行，否则第二层拦的是最便宜的那种计划 |
| `UNION` 的两臂是**并列的两次扫描**，不是嵌套循环 | 把逐行 `rows` **相乘**会把一条合法 UNION 判成 `too_costly`（实测 30226×87912 ≈ 2.66e9 > 默认阈值 5e7）；相加也不对。尺子取**最大值**，它是"单次扫描的最大估行"，不是总代价 |
| `EXPLAIN` 自己被源库拒的三支在这三份探针里**当场就红、根本没走到取行**（探针口径）：`1054 Unknown column`、`1146 Table doesn't exist`、`1221 Incorrect usage of UNION and LIMIT`（后者正是模型爱写的"每臂各带 LIMIT"形状）。**走我们第二层的 live 今天只有 `1054` 那一支**，另两支是探针读数——见 §3.6"用例的分配"那格 | 这一档要单独一枚码，正文带**源库原话**（038 回喂的输入） |
| `EXPLAIN SHOW TABLES` → `1064`；`EXPLAIN UPDATE` → `1142 command denied`；`EXPLAIN SELECT …` 到不了第二层（实测 `guard()` 对 `EXPLAIN` 前缀报 `top_level_not_select`） | `1064` 那一支在真链路上取不到，只能由桩件驱动，**不许写成"真库证过"**；`1142` 是第三层那把只读刀的功劳，不是第二层 |

**裁决口径（工单 037 格子 ③ 的细化，037 施工前由用户选定，选的是"全 NULL 才算读不出来 + 认 optimized away"这一档）**：

1. 取所有**非 NULL** `rows` 的**最大值**。读得出数就按它裁：`> AIWEB_QUERY__MAX_EXPLAIN_ROWS` → 拒 `too_costly`。
   混合形状（一部分行有数、一部分 `NULL`）**按有数那些行裁**——已经读出来的那一格能证大小，
   拿"另一行读不出来"去拒一条证得了的 SQL 是把我们的读法问题算成用户的代价。
2. **一行都读不出来**（全 `NULL`／结果里根本没有 `rows` 这一列／`EXPLAIN` 回零行）时，先问引擎有没有自证：
   **每一行**的 `Extra` 都含 `optimized away` → **放行**（上面那条实测就是它的理由）；
   否则 → 拒 `dry_run_unreadable`。§1.2 ⑦ 那条既有裁决"证不了它小就按最严的一档"在这一格仍然成立，
   只是"引擎自己说它不扫表"不在"证不了"里面。工单原话"不许停在先放行"没有被违反：停在哪一格都不是"先放行"，
   而是"按引擎给出的证据分档"。
3. `EXPLAIN` 语句**自己**被源库拒 → 拒 `dry_run_engine_rejected`，**message 带源库原话**——
   原话是**错误号 + 那句话**两半（实物正文 `…：1054: Unknown column 'nope' in 'field list'`），
   只留后半句就丢了最有用的那一半：038 回喂模型时，模型按 `1054` 这种号改写法是确定的，按人话只能猜。
   号取不到（驱动 `args` 不足两元）才退回整串 `str(cause)`。两半都有用例钉
   （假连接那条断 `"1054" in text and "Unknown column 'nope' …"`、live 那条断号与原话同现）。
   这一枚码单开的理由：它既不是"估行读不出来"（那是我们的读法），也不是 `unsupported_construct`
   （那是 sqlglot 的解析），而 038 要把这句话原样回喂模型。036 立的那条"我们的 bug 不许报成用户的 SQL 违规"
   在这里反过来同样成立——**用户的 SQL 违规也不许报成我们读不出来**。
4. 第二层这三枚**不进 §8.1 那批语料**：`guard()` 不碰库，语料驱动不到它们。它们的钉住处与
   `regeneration_drift` 同族（用例断言），差别只在钉子不住在 `tests/guard/` 而在
   `tests/unit/test_dry_run_shape.py`——封闭集合那条机械判据的扫描根随本片加一枚，见 §9.2 的 037 那一行。

### 3.3 关掉这一层与 prod 启动自检

- `AIWEB_QUERY__DRY_RUN`（**本片新建的键，默认 `true`**，2026-10-20 拍板：照本节原话取 `true`。
  代价说清：它意味着每条问数在源库上多一次往返，且 prod 被自检锁死不能关）。
  读取点在 `executor.py` 内部，与 `resolve_timeout` 同一条纪律——**调用方无法指定**，只能改这一格配置。
- `dry_run=False` 时**一步都不发**：关掉整层的证据是"源库一条 `EXPLAIN` 都没收到"（正向断"没发"），
  不是"结果为空"；同一次用例还要断真 SQL 照跑、结果不变。
- 本节的"**prod 启动自检禁止关闭**"落成 `check_env.py` 的 `query.dry_run` 一档：
  `AIWEB_APP__ENVIRONMENT=prod`（实物键名，工单字面那个 `AIWEB_APP__ENV` 不是真键）且 `dry_run=False`
  → **报红并点名是哪个键**；dev/test 下同一组合只报黄不阻断
  （本节只禁 prod，禁到 dev 上就是给自己加墙）。
- 关掉第二层不等于关掉防御：第三层那四条 SET 一个字不动（本片"不动第三层"是硬约束）。

### 3.4 笛卡尔积：口径没变，**落点被实测改了**

本节原话是"v1 只对**无 join 条件的逗号连接**硬拒，其余放行"，且挂在第二层。工单 037 的"已定口径"把落点
交给实测（AST 可见 → 归第一层），实测结果在
`.scratch/p5-sql-guard-hardening/evidence/probe_cartesian_ast037.txt`（sqlglot 30.19.0）：

- `FROM a, b` 落成一个 `exp.Join` 节点，`side=''`、`kind=''`、`on=None`、`using` 空——**AST 可见**。
  既然可见就该住第一层（守卫规则 + 语料），第二层拿 SQL 文本再判一次是拿字符串重做 AST 已经做过的事。
  于是这一支落成第一层的新规则 `cartesian_product`（§1.1.1 第 19 枚）：**某个 scope 里存在
  没有 `ON`、没有 `USING`、`kind`/`side` 都是空的 join 节点，且该 scope 没有 `WHERE`** → 拒。
  这句是**第一版的写法**，它少了一格（`method`），完整判据见本节下面 `NATURAL` 那一格与 §1.2 ⑥.1 的伪码。
- 一条顺带的实测发现，它让覆盖面比本节那半句**更宽**：`FROM a JOIN b`（显式 `JOIN` 不写 `ON`，MySQL 允许）
  落成的节点与逗号连接**逐字同形**，AST 分不开。所以第一层那一支实际拒的是"任何没有连接条件、
  又没有 `WHERE` 可依托的 join 写法"。要按本节字面只放逗号连接过，就必须回到 SQL 文本找那个逗号——
  正是工单禁止的方向。**这一格偏差按 §9.2 开头那条规矩记在这里，不悄悄放行回去。**
- `CROSS JOIN`（实测 `kind='CROSS'`）、`STRAIGHT_JOIN`（`kind='STRAIGHT_JOIN'`）、以及"逗号连接 + `WHERE`"
  （老式写法，实测与合法形状不可分：MySQL 自己就把 `FROM a, b WHERE a.id=b.aid` 改写成 join）
  **都不在硬拒范围**，留在 §9 的 known limitation。要说清这三格放行的**理由各不相同**：
  `CROSS` 与 `STRAIGHT_JOIN` 是按工单"v1 只硬拒无条件的逗号连接那一族"这条已定口径放的
  （`CROSS JOIN` 不带条件本身就是笛卡尔积，"它没错"不是理由，只是**本片不拒**）；
  "逗号连接 + `WHERE`"是按实测放的（判不出，硬判就误伤一类合法 SQL）。
  交付记录里不许写"笛卡尔积已全面挡住"。
  **`NATURAL JOIN` 是另一回事**：它自带按同名列的连接条件，不是笛卡尔积，所以它**必须**放行——
  而它挡住不了的原因很具体：sqlglot 把 `NATURAL` 放在 `Join.args["method"]` 里，`side`/`kind` 都是空串，
  判据若不查 `method` 就会把它一起拒掉（实测在 `probe_cartesian_ast037.txt` 末两段，
  规则那一格见 §1.2 ⑥.1）。这一发是本轮"先实测再落规则"抓出来的、否则会写进代码的误伤。

### 3.5 dry-run 失败回喂模型自动重试一次（`retry_with_error`，最多 1 次）

属工单 038。037 交到的状态是：失败即**早退**（不是 500），`guard_result` 里落的是同一枚枚举值、
错误正文已经是可回喂的形状（源库原话 + `[rule_id]` 前缀）。

**as-built(口径 P5-038，2026-10-21)**：这一节把口径钉死，**本节一个数字都没有**——用例分配、
变异电池、净成本那些要指着字节说的话，全部住在下面的 §3.7；那一节由本片的**施工半边**补上，
写下这些字时它还不存在，这是 037 立的分工（§3 的口径格与 §3.6/§3.7 的实物格分家、各自记账），不是遗漏。

- **入口只有一格**：`pipeline` 里 037 补的那支 `except SqlGuardError`（收场走
  `_rejected_by_guard(..., step="dry_run")` 那一条）。第二层抬上来的三枚码（`too_costly` /
  `dry_run_unreadable` / `dry_run_engine_rejected`）都进这一路，因为回喂的正文就是我们本来要
  端给用户的那一句，而那三枚的差别只在"引擎说了什么"，不在"要不要给模型第二次机会"。
- **第一层的拒绝绝不进**（`step="guard"` 那一条一支都不许碰）。越权表、多语句、DML 重试一百次
  还是错的；更硬的一条理由是：**回喂它们等于把系统的白名单告诉模型再骗它绕一次**。
  这一条不是"暂时无需实现"，是要有用例钉着的边界（工单 038 验收 2）。
- **第三层的失败今天不进这一路，这是对工单口径的一处收窄**，写在字面上而不是埋在代码里：
  工单 038 的"已定口径"第 3 条原文写的是"只有第二层（**和第三层**）的失败进这一路"，而实物是
  第三层炸出来的是 `SQLAlchemyError`／`QueryTimeout`（`executor.classify_source_error`：超时归一档、
  **非超时绝不吞、原样上抛**），不是 `SqlGuardError`。要接住它就得把"源库执行报错"从
  "真故障上抛"改成"早退 + 回喂"——那动的是 011 定的那一档语义，而 roadmap 要点 (c) 的原话只要了
  "dry-run 失败信息回喂"。所以本片的触发条件 = **第二层拒绝**，唯一入口。
  代价说清楚：一类"要到真正执行那一刻才暴露"的错误（源库权限、引擎侧函数缺失、超时）今天**救不回来**，
  照旧上抛或报超时。为什么这不算漏掉了"可自纠的那一类"：列名拼错／表名记混这两个 038 的目标场景
  `EXPLAIN` 当场就能拒（实测 `1054`/`1146`，§3.2 那张探针表那一格），它们在第二层就已经是那句
  源库原话了，走的是这一路。
- **封顶 1 次，写死成常量，不开键**。§3 的原话是"最多 1 次"；做成 `AIWEB_*__MAX_RETRY` 就是把
  roadmap 没要的东西塞进配置面（与 P4 那条"没有真实需求就别开旋钮"同族）。
- **第二次的产物绝不复用第一次的任何中间物**：重新 `guard()`、重新 dry-run、重新钳位。
  来源是 §1.3 末那句"重跑绝不复用旧 `sql_final`，同样走完整 guard"——同一纪律在重试上成立。
  也就是说回喂的**只有文本**（上一轮的 SQL 与那句错误），树的、`sql_final` 的、`allowed` 的一个都不传。
- **回喂的正文逐字就是我们本来要说给用户的那一句**（`f"[{rule_id}] {message}"`），不改写成
  "请检查列名是否正确"这类通用话术。理由与 011 那条"非超时错误绝不吞"同族：**改写就是把证据洗成猜测**。
  措辞与拼装住 `prompt_builder`（010 起它就是 prompt 的唯一主人），不在编排里拼字符串——
  回喂那段是有版本的东西，混在 pipeline 里就没法跟着模板一起测。
- **三次尝试的形状与留痕**：`chat_messages` 那几列今天只装得下**最后一次**（`sql_raw`/`sql_final`
  在第二次生成时被覆盖），所以"第一次的 SQL + 第一次的错误"落 `guard_result` 那块 JSONB 扩展出来的
  一个键 **`retries`**（每条 `{seq, sql_final, rule_id, message}`），**不加列、不迁移**（P5 是零迁移片）。
  形状与它的七条逐个决定写在 metadata-model §2.7 的 as-built 第 ⑦ 条，两处必须逐字对得上——
  这一列今天只有生产者没有消费者（P8 的 SQL 面板还没建），现在改形状是最便宜的时刻，
  等 P8 接上就变成破坏性变更。039 的 `POST /api/chat/validate` **不回这一栏**：它校验的是"当前这一次"。
- **耗时清单要变诚实**：`AskOutcome.steps` 里第二次要有自己的 `generate`/`guard`/`dry_run` 段。
  来源是 012 的踩坑 ②——那串步数的用途是"卡在哪一步"，把两次压成一步等于把"重试救回来了"
  从可观测面上抹掉。


### 3.6 施工半边的实物账（as-built(P5-037 施工半边)，2026-10-21）

上面几节各自承诺的"今天到底有几分实物"汇总在这里，一格一句，都对着一份跑过的字节：

- **模块表面三枚**：`explain_statements(dialect, sql_final) -> tuple[str, ...]`（纯文本）、
  `explain_verdict(lines, *, max_explain_rows)`（纯裁决，不连库）、`run_dry_run(conn, *, dialect,
  sql_final, max_explain_rows)`（唯一碰连接的那枚）。工单字面写的是单数 `explain_statement`——
  **实物是复数且返回元组**：PG 一支是四条一个序列，单条返回值的签名装不下它（§3.1.1 那个形状
  要靠"发几条"来钉，一条条调用就得在纯函数外面再写一层顺序）。这是本片对工单字面的一处不照做，
  理由与影响记在工单 037 交付记录。
- **执行器里那一格的位置是 `②.5`**（`execute_readonly` docstring 的步骤编号），代码住在
  第三层那个 `for stmt in session_statements(...)` 循环**之后**、`await conn.execute(text(sql_final))`
  **之前**。开关与阈值在执行器内部现读 `get_settings().query`，**调用方无法指定**（与
  `resolve_timeout` 同一条纪律，§3.3）。位置是承重的：电池里的 G 格把它挪到取行之后 →
  红 **5** 条，红的正是每一条"`sql_final` 不在 `sent` 里"那半句（`evidence/mutation_layer037.txt`）。
- **管线多了一支 `except SqlGuardError`**：`SqlGuardError` **不是** `AppError` 的后代，
  外面那一支 `except AppError` 兜不住它。没有这一支的话，"这条 SQL 被第二层拒了"对用户就是
  500"我们坏了"（§3.5 明写不许这样），对 038 则是那句可回喂的正文被提前掐断。收场与第一层
  **共用** `_rejected_by_guard`，只有步梯上那一格名字不同：第一层记 `guard`、第二层记 `dry_run`
  （踩坑 ② "卡在哪一步必须是事实"——钱没花完不能记在 `execute` 上）。
  这一支的反证是电池的 **H 格**（摘掉它）→ 精确红 1 条
  （`test_第二层拒绝时落早退而不是_500`）。
- **成功路径上第二层的耗时折在 `execute` 那一格里**（`watch.lap("execute")` 罩住 `execute_readonly`
  整体）。只有被拒那一支单独占 `dry_run` 一格。这一格偏离不写成"每层耗时都可观测"——它今天不可观测。
- **`EXPLAIN` 回零行归 `dry_run_unreadable`**，不是归"每一行都自证免扫 → 放行"那一档：
  `all()` 在空序列上是**空洞为真**，照 §3.2 第 2 条字面实现会读成"引擎一个字都没说 → 放行"，
  整层就变成一个永不为真的闸。钉子 `test_explain_回零行算读不出来而不是放行`（E 格把它翻回去 → 精确红 1 条）。
  **这一处是对文档字面的补细**，同时记在 §9.2。
- **`EXPLAIN` 自己超时也落 `dry_run_engine_rejected`**，不另开一档：`EXPLAIN` 不执行语句、扫不到数据，
  第三层那条 `MAX_EXECUTION_TIME` 只作用于真正执行的只读 SELECT，这一发在实机上取不到触发条件。
  真出现了正文里带着引擎原话，用户读得到。§3.2 那三枚码因此没有第四枚。
- **用例的分配**（§3.2 第 4 条那句"不进 §8.1 语料"的落点）：`tests/unit/test_dry_run_shape.py` 18 条
  （纯裁决，全桩）、`tests/unit/test_dry_run_wiring.py` 5 条（执行器接线，含"关掉整层一条语句都不发"）、
  `tests/integration/test_dry_run_mysql_live.py` **6 条真跑**（演示库 `ai_web_demo`，只读账号）。
  live 那一份把"`max_explain_rows` 真被读、引擎当场给的那个 30226 在阈值 `100` 下翻成 `too_costly`"、
  "`EXPLAIN` 被拒时正文是源库原话（**错误号与原话两半都在**）"与"关掉的半边：好 SQL 关掉与开着结果一字不变"
  证到了。**"同一条 SQL 翻面"这句话在 live 侧要说清**：live 那两条用的是同一张表的同一种计划
  （放行那条 `id, amount … limit 3`、拒的那条 `id … limit 5`，两条的实测估行都是 30226），
  **逐字同一条 `sql_final` 在两个阈值下翻面**是由假连接那条签的
  （`test_dry_run_wiring.py::test_max_explain_rows_被真读_同一条_sql_从放行翻成拒`），两边不许互相冒充。
  **口径要说清**：三支 `EXPLAIN` 时被源库当场拒的形状（`1054`/`1146`/`1221`）里，
  **只有 `1054` 这一支走过我们的第二层**（live 一条 + 假连接一条）；`1146`/`1221` 今天
  **只有探针读数**（`probe_explain037.txt:48`/`:40`），没有走我们这一层的用例——
  它们的形状由 `dry_run_engine_rejected` 这一枚码统一承接，但"承接得住"这句话今天立在 `1054` 一发上。
  `dry_run_unreadable` 那一档在演示源上仍**只有桩件**（§9 那条既有账没变）。
- **变异电池十三格**（`evidence/mutation_layer037.txt`，套件 152 条、pristine 与复原各一遍全绿）：
  A 整层摘掉 → 6 红；B 阈值那一格永不红 → 5 红；C 不认引擎自证免扫 → 精确 2 红；D 读得出数也不按数裁 → 10 红；
  E 零行当成空洞放行 → 精确 1 红；F 正文丢掉源库原话 → 2 红；**F2 只丢错误号、那句话留着 → 2 红**
  （红的还是那两条：桩件与 live 都断 `"1054" in text`，所以"号"与"话"是分家承重的，不是同一件事测两遍。
  这一格是 §3.2 第 3 条把两半并成一串之后**新长出来的靶子**——并串之前正文里没有号，那一半从来没有反证）；
  G 位置挪到取行之后 → 5 红；
  H 编排层那一支兜不住 → 精确 1 红；K `EXPLAIN` 被拒不翻成第二层的码 → 精确 2 红
  （那两红都在第二层自己的用例上：假连接那条断"真 SQL 一个字都没发"、live 那条断正文是源库原话）。
  **I/J/L 三格打的是第一层那一支**（`cartesian_product`）：
  I 判据不查 `method` → 精确 1 红（就是 §3.4 那个 `NATURAL` 误伤）、J 摘掉 `WHERE` 豁免 → 3 红、
  L 连接条件扫描整支摘掉 → 9 红。
  全称量词那一格另跑一发（`evidence/mutation_any037.txt`）：`all` → `any` 只红
  `test_只有一行自证_optimized_away_另一行读不出来时拒` 那**一**条，"每一行"三个字是承重的。
  **这份电池有两份作废的前身**：第一版把复原放在 `suite()` 口子上，格数还少一发、十二格全绿——那是假证据，
  机制修正后整电池重跑；第二版（12:07）的 F、J 两格锚的是 12:26–12:29 那批改动**之前**的字面，
  重跑时两格锚点各命中 **0 次、当场作废**（作废的意思不是"这两格不需要"，是那份 artifact 描述的
  已经不是 shipped 字节），按新字节重新锚定并补上 F2。实录里那两段说明都留着。
  此后它又跟着 app 字节整电池重跑过两遍：第三遍是因为 `ruff check` 的 E501 把一条中文注释打红、
  重折行改了字节；第四遍（落盘 13:13）是因为双轴审查之后 `dry_run.py` 的 docstring 补宽、
  `sql_guard.py` 把一枚 `.sql()` 片段绑一次。**第四遍十三格实物与第三遍逐格相同**——这句话证的
  正是"改的都是注释与等价写法"：若那枚片段的绑定动了判据或消息，第一层那两支会立刻红在本格上。
- **第二层的净成本**：同一条 `sql_final` 跑 25 次 × 3 批（关-开-关，`evidence/perf037.txt`）。
  **三轮实测**——轮 1（11:54）：开启中位 **12.6 ms** vs 两批关闭中位（10.7 / 11.6）的均值 11.15 ms →
  **+1.5 ms**，同档重复噪声 **0.9 ms**；轮 2（13:12，锚 shipped 的 `dry_run.py d77c7377…`）：
  关闭 7.1 / 7.2、开启 7.6 → **+0.4 ms**，同档噪声 **0.1 ms**；轮 3（13:15，锚双轴审查之后的
  `dry_run.py 8e1567c3…`）：关闭 10.8 / 10.8、开启 11.0 → **+0.2 ms**，同档噪声 **0.0 ms**。
  三轮合起来只支持同一句话：**多一次源库往返的净增量落在 +0.2 ~ +1.5 ms、与同档重复噪声同量级或略高**
  ——三轮符号一致为正，所以这一层不免费；而同一条 SQL 的关闭中位在三轮里是 11.15 / 7.15 / 10.8 ms，
  所以这句话也不拿绝对毫秒当承诺（差的是机器负载，不是代码）。
  轮 2/3 是按 shipped 字节**重跑**的：轮 1 那份 artifact 锚的是 12:26–12:29 那批改动之前的字节，
  按本节开头那条规矩，那种 artifact 描述的已经不是 shipped 实物。三轮读数因此都写在同一份文件里。
  这一族还有一次**重测**要记账：更早那份把每一次按整数 ms 记、差值也被 `:.0f` 抹成整数，
  写下"13 vs 11.5 → +2 ms"，而 13 − 11.5 是 1.5——读者对着那份字节核算术对不上。
  脚本只改了打印精度与每样本留一位小数，测量本身（同一条 SQL、同一账号、同一台机、25×3 关-开-关）一字未动。
- **一处顺带的实录噪声**（`evidence/note_1003_037.py` / `note_1003_037.txt`，探针四条各证一件事）：
  真 MySQL 5.7 每发一条 `EXPLAIN` 会在 **stderr** 上带出**一行裸的**
  `/* select#1 */ select …`（引擎自己的优化器改写回显）。机制验到了三层：引擎侧
  `SHOW WARNINGS` 给的是 `('Note', 1003, '<改写后的 SQL>')`；驱动侧是 `asyncmy/cursors.pyx:520`
  那一句 `logger.warning(msg)`（`warning_count > 0` 才走），`logging` 无 handler 时经 lastResort
  写 `sys.stderr`、**不带任何 `Warning 1003:` 前缀**——文档旧写法那个前缀是我不实的说法，已按实物改对；
  `warnings.catch_warnings(record=True)` 收到 **0 条**，是"它不是 `warnings.warn`、也不是我们的 print"的反证。
  它不进结果、不进 CSV、不进 `chat_messages`。**闸的实录里看不见它**：pytest 抓 stderr、用例绿时丢弃，
  `pytest tests/integration/test_dry_run_mysql_live.py -q` 的 stderr 实测 **0 行**——所以这句话的落点
  是"裸跑脚本会看到"，不是"那份实录里那一行属于源库"（旧写法同样不实）。
- **`tests/integration/test_execute_live.py` 整族把 `AIWEB_QUERY__DRY_RUN` 钉成 `false`**（模块级 autouse
  fixture + 模块 docstring 说明）。理由不是"它挂了"，是它的靶自 011 起一直是"**第三层独立有效**"
  （验收 ① 断的是源库自己拒写：`DBAPIError` 命中 1792/1142）。第二层开着时那道 `EXPLAIN` 会**先**把写语句
  判成我们的 `SqlGuardError`，那一格就变成"证了我们自己"。第二层开着时的行为另有 live 文件，
  两边不许互相冒充——这正是 §9 那条"别用一条用例替两层签字"的实物版。


### 3.7 施工半边的实物账（as-built(P5-038 施工半边)，2026-10-21）

§3.5 开头那条分工欠的话在这一节还清：下面每一格都指着跑过的字节，artifact 全在
`.scratch/p5-sql-guard-hardening/evidence/`。口径一个字不重述——要读口径读 §3.5，要读形状读 §2.7。

- **形状是一个循环，不是"失败后再抄一遍代码"**：`ask()` 里 ⑤→⑦ 那段现在是 `while True`，
  再入口**只有一处**（`pipeline.py:355` 判 `len(retries) >= _MAX_RETRIES`，常量 `_MAX_RETRIES: Final = 1`
  住在 `:183`）。`retries` 同时兼任"已经白烧了几趟"那把尺子，所以封顶**跨层自动成立**——
  第一趟被第二层拒（回喂一次）、第二趟那句连第一层都过不去，仍然只两趟。这一条由
  `test_第二次那一句也现算裁决且封顶跨层仍然只两趟` 签（它同时断 `layer2.calls == 1`：
  第二次那句草稿是**现过守卫**的，上一轮的裁决一样都没带进第二轮），不是由常量名签。
- **回喂的原料只有三样文本**：`build_retry_messages(messages, *, previous_draft, sql_final, error_message)`
  （`prompt_builder.py`）＝ `[*messages, {"role": "assistant", "content": previous_draft},
  {"role": "user", "content": 渲染 nl2sql_retry.j2}]`。模板里三格都在场：被拒的那句 `sql_final`、
  那句 `[rule_id] 正文`、以及"这是最后一次机会"那句封顶声明——第三格由 U4
  （`test_措辞里那句封顶声明逐字在场`）逐字钉着，因为它是对模型的行为承诺，不是修辞。
- **那句 `[rule_id] 正文` 全仓只有一个拼装点**，住在 `SqlGuardError.__init__`（`sql_guard.py:140`）；
  落库与回喂读的都是 `_guard_sentence(exc)`，而它**不重拼**、就是 `str(exc)`。
  **这一枚是本片双轴审查之后改的**：审查前它自己又写了一遍 `f"[{rule_id}] {message}"`，
  而同一枚函数的 docstring 正宣称"只有一个生产者"——那句是虚言，格式有两处就两处能错开。
  改成命名 `str(exc)` 之后声明才为真。等价不由我说：那 41 条断言里有一整族断的就是**整句**
  （`ENGINE_SENTENCE`），新字节上原样绿；整电池照 035 那条规矩**重跑一遍**
  （`mutation_retry038.txt` 第三轮，锚 `pipeline.py 6ced9ab9…`），**十三格红数与第二轮逐格相同**——
  这句话证的正是"动的只是写法"。
- **用例分配：新 14 条，两半各管一头。** 纯函数半边 `tests/unit/test_prompt_retry.py` 5 条
  （回喂内容同时含失败的 SQL 与那句错误原话 / 原提示逐字保留再挂两轮 / 传入的列表不被修改 /
  措辞里那句封顶声明逐字在场 / 第二层那三枚码的正文都不被加工）；编排半边
  `tests/integration/test_pipeline_retry_pg.py` 9 条（坏→好那次回喂带原话 / 库里两次都在 /
  坏→坏恰好两次 / 第一次就好一次都不多发 / 第一层拒绝绝不回喂 / 第二次那句现算裁决 /
  第二次回得更烂但历史仍在 / 第二层关掉不触发回喂 / 重试那次在步梯上有自己的三段毫秒数）。
  接缝沿用工单那条：**LLM 走 respx**（次数尺子 = `respx.Route.call_count`）、检索走 `Retriever`
  Protocol 的桩、executor 走 monkeypatch 的 spy。两条口径决定要写在明面上：
  ① spy 按 **SQL 内容**裁决（那句被点名的列名在场才拒），**不按调用次序**——次序是被测对象的一部分，
  让桩按次序应答就变成"我按我猜的次序应答"（`_draft_route` 的 docstring 反对序列桩，本片保留那个理由）；
  ② 桩的分流维度是**回喂那一段在不在 `messages[1:]` 里**，判据是那句源库原话**逐字**在场。
  两条合起来的效果：pipeline 哪怕只把原话改写成通用话术，"坏→好"那一档立刻红，
  不存在"靠桩配合变绿"这条路（K 格正是沿着这条路红的）。
- **验收 3 是查库，不是查日志**：`test_第二次成功时执行的是重生成的sql且库里两次都在` 从
  `session.get(ChatMessage, …)` 那**一行**读四件事——`retries[0]["sql_final"]` 等于 spy 记的第一次
  端出去的那句、那句里确实带着被点名的列、`rule_id == "dry_run_engine_rejected"`、
  `retries[0]["message"]` 就是源库原话一字未改；终局那三格（`executed` / `sql_final` / `error_message`）
  读的是同一次 `ask` 的第二趟。`sql_raw`/`sql_final` 被第二趟覆盖是 §2.7 ⑦.1/⑦.4 明文接受的形状，
  不是这一发撞出来的意外。
- **`retries` 键在不在 = 这一轮有没有白烧过一趟**，落点是每次 append 之后**立刻**重盖快照
  （`pipeline.py:367`），所以四个出口都带得上：正常成功、坏→坏、重试后模型回得更烂走上
  `llm_bad_response`、重试后第一层拒。D 格（这一键永不写）红 4 条，全在"要读重试历史"那四条上。
  没重试时这个键**不存在**，钉子是 `test_第一次就好时一次都不多发且retries键不出现` 里那句
  `guard_result == {"ok": True, "violations": []}` **全等**——§2.7 ⑦.2 那条"不用空列表冒充缺席"
  靠的是全等断言，不是靠注释。
- **步梯诚实**：被丢弃那一趟的成本先记账再回喂（`watch.lap("dry_run")` 在 `:370`，第二次 `generate`
  之前），所以"重试救回来了"不会在耗时面上变成"第二次生成慢了一截"（012 踩坑 ②）。
  独立真相源是 `_PinnedWatch`（monkeypatch `pipeline._Stopwatch` 发一段已知 ticks），
  断的是那 12 段的名字与各自的毫秒数，不是"看起来合理"。
- **变异电池十三格**（`evidence/mutation_retry038.txt`；套件 41 条 = 5 + 9 + 22 + 5，
  pristine 与复原各一遍全绿）：A 第二层一拒就早退不回喂 → 7 红；B 封顶放开成三次 → 2 红；
  C 回喂段里没有源库原话 → 5 红；D `retries` 键永不写 → 4 红；E 被丢弃那一趟不记耗时 → 2 红；
  F 第一层的拒绝也回喂 → 3 红；G 回喂时丢掉原 `messages` → 7 红；H 回喂段里没有失败的那句 SQL →
  精确 1 红；I 第二层那一支改成接一切异常 → 4 红；J 上一轮不再署名 `assistant` → 2 红；
  K PB 把源库原话洗成通用话术 → 7 红；L 模板不渲染失败的那句 SQL → 2 红；M 模板丢掉那句封顶声明 →
  精确 1 红。**没有绿格**，所以这一族不存在"用没红冒充不需要"的洞。
  A 格（"第二层一拒就早退、不回喂"）复现的正是**施工前那段实物**，所以"这一族新用例在 pre-fix 字节上
  见过红"这句话有两个读数：TDD 期间它们对着当时的 `ask()` 真红过（038-D 那一步的现场，没留独立
  artifact），而 A 格是同一件事**可重跑的版本**——要复核这句话就跑 A 格，不要引那个现场。
  **三跑的账要分开写**，因为读者会问"预测全中了吗"：第一跑跑到 H 格时被我作废——
  我看到后台任务报"完成"就断定进程已死、把 shipped 字节写了回去，而那通报的是包装 shell，
  `python` 子进程还活着（证据：junit XML 被反复重写、工作树里活着的就是 H 格那个变异体），
  H 格那一跑的输入不唯一，那份 artifact 对 H 不作数（留着当反面教材，`mutation_retry038_round1.txt`）。
  第二轮整电池重跑，并把 F/G/I 三格的**声明清单**按第一轮实测各补了几条既有用例
  （F 多两条第一层边界档、G 多六条、I 多三条"执行侧那类真故障照旧上抛"）。
  所以：**A/B/C/D/E/H/J/K/L/M 十格是盲测，F/G/I 三格证的是"每一红都有解释"**——这句话别缩成"预测全中"。
  第三跑锚的是双轴审查之后的 `pipeline.py 6ced9ab9…`（偏离在那枚 helper 那格，见上面第三条），
  **十三格红数与第二跑逐格相同**——这句话证的正是"动的只是写法"。
  I 那三红是 §3.5 那处收窄的**反证**：那一支若接住一切异常，第三层上抛的三条用例就红，
  所以"今天它只兜 `SqlGuardError`"是被测过的，不是注释里的一句声明。
- **验收 5 那句"重试路径不跑时必须零开销"要按实物收窄成"不多发一次往返"**：证得到的是
  `call_count == 1`（一次都不多发）＋ `guard_result` 全等（形状不变）＋ 第二层关掉时一次都不进这一路
  （`test_第一次就好时一次都不多发且retries键不出现`、`test_第二层关掉时坏sql不会触发回喂`）。
  每一次 `ask` 现在都多分配一个空 `list` 和一次 `len()` 判，那是字面"零成本"够不着的地方，
  所以这一格不写"零成本"。整轮的量级参照：037 那一轮 `1314 passed / 2 skipped` 用 542.10s，
  本片这一轮（锚审查后的字节，`evidence/gate038_post_review.txt`）`1328 passed / 2 skipped` 用 523.80s
  （收集 1316 → 1330，+14 条用例）——
  差值与本机负载同量级，这一句只用来证明"多 14 条用例没把闸拖出量级变化"，不当性能结论。
- **一处对文档半边的纠正**：§2.7 ⑦.3 原先把 `retries[].message` 写成"`[rule_id] …` 那句"，
  实物存的是**不带前缀**的正文，前缀由 `rule_id` 那一格兑换（`pipeline._retry_record`）。
  §3.5 自己要求"两处必须逐字对得上"，所以对不上时改的是**文档**不是形状：一栏存两遍正是这条口径
  要避免的"两处能错开"。读侧要复原给用户看的那句时自己拼——`rule_id` 是封闭词表，拼出来与生产者逐字相同，
  这一条的两半各由一发钉着、**不在同一条用例里**：不带前缀的正文是库里那一发
  （`test_第二次成功时执行的是重生成的sql且库里两次都在`），带前缀的整句是回喂那一发
  （`test_第二次生成时回喂段带着那句源库原话`）与终局那一发
  （`test_两次都坏时恰好两次生成且第三次绝不发` 断 `error_message` 就是那一句）。
- **本片动了 037 的一条断言，这笔账不只写在代码注释里**：
  `test_pipeline_orchestration.py::test_第二层拒绝时落早退而不是_500` 原断 `call_count == 1`、步梯 7 段，
  有回喂之后实物是 `== 2`、10 段。改的是**这一条既有用例的读数**，不是它的判据——它测的还是
  "第二层拒了要早退、不要 500"，只是"拒了以后现在会再试一次"是事实了。不写清的话，下一片看到
  "038 改了 037 的用例"会以为那是为了变绿而松的手。
- **`dry_run=False` 那一档用的是替身，替身写在 docstring 里**：工单字面要"第二层关掉时坏 SQL 不触发重试"。
  关掉之后 1054 那一类是**源库/第三层**抬的 `SQLAlchemyError`，`except SqlGuardError` 那一支根本收不到，
  所以 `test_第二层关掉时坏sql不会触发回喂` 用"同一个列名错误在第二层关掉时由源库当场抛"来签这一格。
  它证的是"关掉那一层 ⇒ 这一路一次都不进"（正交性），**不证**"关掉那一层还能造出第二层的拒绝"——
  后一句话在物理上就不成立，写在这儿防的是把它当成已交付。

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

  > **as-built(P5-040 文档半边)：本节的"两档"里，非只读那一边的证据今天只有一档半**。
  > 执行路（硬阻断）两档都有实物：`tests/unit/test_executor_grants.py::test_非只读账号硬阻断`
  > （`:33`，断 `exc.value.code == "readonly_capability_missing"`）+ `executor.py:314` 那个唯一调用点。
  > 登记路（200 + 可 switch 的码）只有**正半**真跑过：`tests/integration/test_datasource_live.py:80-81`
  > 用只读账号 `aiweb_ro` 断 `read_only is True` 且 `code is None`。**反半（拿非只读账号真连一次
  > `test_connection` → 200 且 `grants.code == "readonly_capability_missing"`）今天没有真跑记录**，
  > 单元判决用例不算它（那是纯函数，不证明 HTTP 那一档回 200 而不是 4xx）。
  > 凭证不再是障碍：用户 2026-10-20 授权用 `backend/.setup/my_login.cnf` 那把，本轮已按工单纪律
  > 先跑**只读** `show grants` 核实它确实是非只读那把（两行 grant、命中告警词 `all` 与 `grant option`、
  > `grants_verdict` 回 `read_only=false` + `code="readonly_capability_missing"`；
  > 读数 `evidence/grants_raw040.txt` 与 `evidence/grants_verdict040.txt`，口令一个字不入账），
  > 反半那一发由本片施工半边落。这一格在跑出来之前**不许写成"验收 5 已过"**。
- 取数：无缓冲 cursor + 逐 1000 行 `fetchmany`，累计到 `max_rows+1` 立即 `cursor.close()`。
  **不做 `KILL <connection_id>`**——`KILL` 是写操作，只读账号本来就无权限，依赖
  `MAX_EXECUTION_TIME` 自杀 + 连接归还前 `ROLLBACK`。这是 MVP 的诚实取舍，写进 known limitation。

  > **as-built(P2-011)：本条降级为"缓冲 `execute()` + `fetchall()`"**。理由不是"实现偷懒"，而是
  > 流式取数会让**超时分类废掉**：asyncmy 在 `stream(partitions(1000))` 下遇到服务端 3024
  > 不会把 3024 传给我们，而是先吐 2013（Lost connection）并挂一条
  > `coroutine '_finish_unbuffered_query' was never awaited` 警告；分类器拿不到 timeout code，
  > `QueryTimeout` 就翻不出来，验收 ② 直接假过。缓冲取回时 3024 到得了客户端，行为一致，
  > 而**内存上限由守卫的顶层 LIMIT 钉住**（缺则补、超则钳成 `row_limit+1`，见 §1.2 ⑦），与是否流式无关。
  > 钳位那半边是 2026-09-29 补的：当时守卫只补不钳，模型照模板写 `LIMIT 1000` 或写 `LIMIT 5000`
  > 两种形状都能把这句话的前提抽掉（§9.2 ⑧）。
  > 代价：绕过守卫直调 `execute_readonly` 的话没有那一行兜底，会退化成"整结果集进内存"——
  > 012 接线时必须保证 `sql_final` 只能来自 `sql_guard.check(max_rows=...)` 的返回值。
  > 等 asyncmy 修好流式模式下的 3024 传播再回到本条的原口径。

- `MAX_EXECUTION_TIME` **只在行边界检查**。这意味着：`count(*)` 从头到尾只吐一行，中间从不看时钟；
  单条 `SELECT SLEEP(n)` 同样只有一行，也查不到；两者都不会触发超时中断。演示库要测这个必须让
  **每一行都慢**（011 的 live 用例用 `select sleep(1) from order_item limit 5`，第一行结束即命中阈值）。

  > **as-built(P5-040)：本句的前半"只在行边界检查"是对的，后半对 `SELECT SLEEP(n)` 的推断实测半对半错**
  > （读数：`.scratch/p5-sql-guard-hardening/evidence/sleep_timeout_probe040.txt`，MySQL 5.7.17，
  > 只读账号 `aiweb_ro`，纯 CLI 四发，不经过本仓库任何代码）。
  > ① **不触发超时"错误"这一半成立**：`set session max_execution_time=1000; select sleep(20);`
  > 回 `rc=0`、一行、**没有 3024**；再加一列 `select sleep(20), 'reached-tail'` 两个表达式都求值并送达，
  > 所以不是"错误被客户端吞了"，是服务端在单行语句上真的没中止它。
  > ② **"中间从不看时钟"这一半不成立**：那一行回的是 **`1`** 而整体耗时 **≈1.315 s**（不是 20 s）。
  > 控制组不设上限跑 `select sleep(3)` 回 `0`、耗时 ≈3.318 s ⇒ `SLEEP()` 的返回值语义是
  > "睡眠有没有被叫醒"（0 睡满 / 1 被中断），所以 `MAX_EXECUTION_TIME` **叫停了计时的流逝**，
  > 只是没有把它表达成异常。原句"也查不到"要读成"查了，但单行语句没有可报错的行边界"。
  > ③ 结论落在归因上：**"清空第一层黑名单后由驱动 timeout 拦下"这件事，在 `SELECT SLEEP(20)` 这一条上
  > 既不是 `SqlGuardError` 也不是 `QueryTimeout`，而是一次成功的查询**（值 1、耗时 ~1s）。
  > 3024 那一档仍然有实物，但它的靶是"每一行都慢"那一族（011 的既有用例已在签），
  > **不许为了证明分层独立而把 SLEEP 换成那个形状**——换了就不是同一条 SQL 的两种归宿，
  > 而是两条不同 SQL 各自的归宿。工单 040 的"已定口径"正是为这一格写的。
  > ④ 本句举例里的 `order_item` 是**裸表名**，CLI 上要先限定库（第一次尝试因此回
  > `ERROR 1046 No database selected`，rc=1 但那是选库失败不是超时）；既有用例写的是
  > `ai_web_demo.order_item`。文档字面与用例字面在这一格上不等价，施工那发照用例的字面跑。

  > **as-built(P2-011)：源库不支持 `MAX_EXECUTION_TIME`（MySQL < 5.7.4，errno 1193 unknown sysvar）时
  > 降级靠驱动侧 `read_timeout`**：分类器 `_is_unsupported_max_execution_time` 命中 1193 后 warning +
  > `continue`，其余 SET 语句照旧执行、执行本身不阻断。演示库 5.7 支持该变量，这条**live 不测**——
  > 降级分支只在单测的 `session_statements` 顺序里存在，触发路径要真·5.7.3 才能演，P2 不设这个环境。
  >
  > **as-built(P5-040)：上面那句"降级靠驱动侧 `read_timeout`"里的 `read_timeout` 全仓一个都没设过。**
  > 实测（2026-10-26，本轮再 grep 一遍仍是这个结果）：`grep -rn "read_timeout" backend/app backend/scripts
  > backend/tests` → **零命中**，两处命中全在文档里（本节与 roadmap §P5 踩坑 ④）。
  > 驱动侧今天只有 `connect_timeout` 一格：`source_connect_args`（`services/source_manager.py:53-59`）
  > 把 `timeout_ms` 折算成**秒**落到 `connect_timeout` 上，`create_source_engine`（`:62-74`）的 docstring
  > 亲口写的就是这件事。而 `connect_timeout` 管的是**握手**，语句跑起来以后它不再在场。
  > 所以 1193 那一支的真实后果是：**降级之后没有任何东西在挡执行时长**，不是"靠 read_timeout 兜住"。
  > 这条不是 P5 漏做——设 `read_timeout` 属于新增能力（要定档位、还要定它是否覆盖 `connect_timeout`
  > 的现有语义），工单 040 格子 ① 由用户 2026-10-20 拍板**本轮不设**，要设另开工单。
  > 差额记在 §9.2 那一行，roadmap 踩坑 ④ 的答案按这个实测落。

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
>   由守卫的 `check(max_rows=)` 参数带进去（`sql_guard.guard()` 的 LIMIT 分支，见 §1.2 ⑦），
>   调用方当下只传全局 `row_limit`；
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

> **as-built(P2-012，2026-09-29)**：
> - **L4 的 `data_sources.row_limit` 已经接线**，上面那句"数据源级 `row_limit` 覆盖属 P3"作废。
>   012 的示踪弹把 `resolve_row_limit(ds)` 接进 pipeline，与 011 的 `resolve_timeout(ds)` 共用
>   同一条判定（列值 `> 0` 才覆盖全局）。为什么不写成 `ds.row_limit or settings.query.row_limit`：
>   `or` 只兜 `None`/`0`，而这一格是手插行也能填的整数列——`-5` 会一路进 `split_truncated`
>   渲染成 `rows[:-5]`，把**真结果**从尾部吃掉还不报截断。
> - **`truncated` 在真链路上恒为假**（012 接真 LLM 时发现）：下面那句"注入 `max_rows+1`"当时只在
>   **语句本来没有 LIMIT** 时发生，而 ④ 的 prompt 模板正是要模型写
>   `LIMIT {{ row_limit }}`（`nl2sql_user.j2:29`）。模型听话的那一刻守卫无事可做，
>   探针就不存在，执行侧拿到的行数不可能超过 `row_limit`。桩测全绿是因为手写草稿没带 LIMIT。
>   **同日拍板闭环（2026-09-29）**：钳位已落进 §1.2 ⑦，`truncated` 恢复可用；口径是
>   "上限之下尊重模型、上限之上与不可证一律钳到 `max_rows+1`"，不是"一律改写"。
>   **闭环只到"撞上限"这一支为止**：模型自己写 `LIMIT n`（`n < max_rows`）时仍然没有探针、
>   `truncated` 恒假，那是有意的语义而不是残留的洞（§1.2 ⑦ 末把这条边界写死了）。

- 截断探测：注入 `max_rows + 1`（模型自带的 `LIMIT n` 里，`n` 不小于上限时同样被钳回这个值），
  取到 `max_rows+1` 行即判定 `truncated=true` 并丢弃最后一行。
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
    **一律 `ValueError`**，路径拼出来必在 `result_dir.resolve()` 之内。
  - 下载侧防护 as-built(P2-013)：`resolve_result_file(result_dir, run_id)`（`app/services/nl2sql/executor.py`）
    是 §7 的另一半，三道门塌成**同一档** `ResultFileUnavailable`（`code=result_file_unavailable`，404）：
    ① 形状——**复用写入侧那一个 `result_csv_path`**，两边不许各写一份名字语法，它的 `ValueError` 在这里
    被翻掉（客户端传来的字符串不该让端点吐 500）；② realpath 落回目录内——`..` 与链接逃逸挡在这层；
    ③ 确实是个文件——目录、被清理作业收走的也算拿不到。三档的 `code`/`message`/`status_code` 逐字相同
    （`test_三种失败的对外说法逐字相同`，四个入参**逐字对上三道门**，门②由 junction 构造），原因
    **只进服务端日志**不进响应体。
    目录级链接**不算**逃逸：`result_dir` 自己指向别处时 root 跟着一起 resolve，那是运维挪盘的正当做法
    （`test_结果目录自己是指向别处的链接时照常放行` 钉反面）。
    **端点本体归 P8**，接线义务已写进 architecture §4.1（含"不是这个用户的文件也回这一档 404，
    不许换成 403"——403 本身就在宣称"文件存在"）。
  - `execute_readonly` 一次调用生成一个 `run_id`，落在 `ExecutionResult.run_id` 与 `.result_file` 两个字段，
    接口层（012）只把它们抄进响应体，没有任何入参能反向指定。

- 抽取侧同样保护：全部 IS 查询前 `SET SESSION max_execution_time` / `SET LOCAL statement_timeout`，
  批大小固定 200，批间 `await asyncio.sleep(EXTRACT__BATCH_INTERVAL_MS)`，单连接串行，不开并发打源库。

---

## 5. 权限与白名单的关系

- `allowed_tables` 的来源是 **检索结果 ∩ 该用户有权的表**，不是"全库"。执行前二次校验
  "SQL 引用的表 ⊆ 该用户可访问表"。
- member 直接 `POST /chat/validate {"sql":"SELECT * FROM secret_table"}` → `403 table_not_granted`。
  **as-built(P5-039)：这一行描述的是 grant 收紧之后的样子，今天不成立**。`datasource_grants` 至今没有
  读取点（§5 开头那句"检索结果 ∩ 该用户有权的表"里的"∩ 有权表"归 **P6 验收 2**），而 039 落地的 validate
  在这一格上给的是 **400 `table_not_allowed`**——因为它的白名单是"该源全部已同步表"（`meta_table` 里
  `is_stale=false` 的那些，与 `kb_service.required_card_count_stmt()` 同一条尺子；`is_hidden` 不是过滤器），
  里面**没有**"这张表点名给了谁"那一维。档位本身是有的：`datasource_service.get_authorized` 判的是
  "这个源你能不能读"（read 权，无档 → 403、软删 → 404），源级之下今天就到此为止。
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

> **as-built(P2-013，2026-09-29)：上面这两条与现状不符的那处已收口。**
> 012 的现场是：`ResultGroup.dir` 缺省为**相对** `Path("data/results")`，执行器按 `Path` 直接用，
> 落点取决于进程 cwd（示踪弹在 `backend/` 下跑，csv 就落在 `backend/data/results/`），
> 而 `scripts/check_env.py::check_dirs` 自己按 `BACKEND_ROOT.parent` 解析——"体检说可写"和
> "文件真写在哪"当时是两个目录。拍板（用户 2026-09-29）与落地：
> ① **基准只有一个地方**：`app/settings.py::_under_repo_root` 把相对目录按**仓库根**解析成绝对路径，
> `ResultGroup.dir` 与 `KbDocsGroup.dir` 共用它（`model_config = ConfigDict(validate_default=True)`
> 是必需的——pydantic 默认不校验 default，而那个 default 恰恰是元凶）。`check_dirs` 不再自己拼根。
> 钉它的是 `tests/unit/test_settings.py`（仓库根从测试文件自己的位置独立算出，不复用被测常量）+
> `tests/unit/test_check_env_secrets.py::test_目录体检报的就是执行器会写的那个目录`。
> ② `.gitignore` 的两条都留着：`data/*` 锚定仓库根、正好管住新落点；`backend/data/` 是 012 补的，
> 旧落点那几张历史 csv 仍在忽略范围内（**不迁移、不删除**——它们是 012 的现场证据，清理归
> `RETENTION_DAYS` 那个作业，而它本身还没有实现点）。
> ③ **符号链接逃逸这一门是真跑的，不是自跳**（双轴审查后补，2026-09-29）。原本以为本机
> （Windows 非管理员、未开开发者模式）建不出指向外面的链接——`os.symlink` 确实报 winerror 1314——
> 但 **junction 不需要特权**（`_winapi.CreateJunction`），而守卫判的是 `resolve()` 之后的**落点**，
> 与链接类型无关。于是 `test_目录里的链接指向外面时拒绝` 用 junction 造出同一条逃逸路径，
> 并用服务端日志的 `reason=realpath 逃出目录` 证明**走的确实是门②**（junction 的落点是目录，
> 只看异常种类的话，门②与门③会给出完全相同的 `ResultFileUnavailable`，换掉判定顺序用例照绿）；
> `test_结果目录自己是指向别处的链接时照常放行` 钉的是反面——目录级链接不算逃逸。
> 用例先试 `os.symlink`、失败才退 junction，两者都建不出来才 skip，skip 只可能意味着
> "这台机器造不出这个文件系统对象"，不会把真实失败吞成跳过。**仍未证明的只剩一格**：
> 指向**文件**的符号链接（不是目录 junction）这一具体形态在本机复现不出来；它过的是同一句
> `resolve()`，所以缺的是构造手段、不是代码分支。闸从"629 passed / 3 skipped"变成
> **631 passed / 2 skipped**，剩下的两格是守卫变异语料的空靶（与本工单无关）。

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
    assert 顶层 LIMIT 存在 and 顶层 LIMIT <= max_rows + 1     # §1.3 as-built，原写法被 `or` 掏空
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

> **as-built(P5-037)：这张表不加行，语料加 3 条**。037 带来的第一层新枚 `cartesian_product`
> （§1.2 ⑥.1）在本节没有对应编号——它防的不是"注入面"而是"代价面"，本节这张表从 #1 到 #34 讲的是
> "这条 SQL 会做坏事"，而笛卡尔积那条做的是**合法但会扫爆**的事。所以它按语料的实际形状走三条，
> 每条钉住判据的一格（口径见 §3.4 与 §1.2 ⑥.1，这里只记落点）：
> ① `SELECT * FROM customer, order_main` —— 本节原话那唯一一支（无条件的逗号连接）；
> ② `SELECT * FROM customer JOIN order_main` —— 显式 `JOIN` 不写 `ON`，就是 §3.4 说的那格"覆盖面比
> 本节原话宽"的偏差，它必须有一条自己的钉子，否则那一支哪天被"按字面收窄回去"没人会红；
> ③ `SELECT * FROM (SELECT c.id FROM customer c, order_main o) t` —— 判据是**逐 scope** 的，
> 内层那个作用域里的无条件逗号连接也要被看见；只测顶层那一条证不了"逐 scope"这三个字。
>
> 三条都是 `guard()` 层能拒的形状，所以**它们进语料**，与第二层那三枚"结构上不可能有语料"的码是两回事
> （§1.1.1 开头那条扫描根注）。语料期望值 54 → 57 的全部差额就是这三条。

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

> **as-built(P5-037)：+1 条 → 19**。新增的是 **#11 `SELECT c.id, o.id FROM customer c, order_main o WHERE c.id=o.customer_id`**
> ——老式逗号连接把连接条件写在 `WHERE` 里，这是 MySQL 自己都会改写成 join 的合法形状（§3.4 实测），
> 也是 §1.2 ⑥.1 那条判据里"**且该作用域没有 `WHERE`**"这半句存在的唯一理由。
> 它比新增的拒绝分支更需要钉子：拒绝分支红了只是"少挡一条"，这支被误伤等于**误伤一类合法 SQL**，
> 而且是模型最常见的一类写法。
>
> `NATURAL JOIN` / `CROSS JOIN` / `STRAIGHT_JOIN` 那三支的放行**不进本节**（语料仍 19 条），
> 钉在规则自己的用例里。理由是两问不同：本节那条只断"这条 SQL 合法"，红了答不出红在哪一格；
> 而这三支撑的恰好是 `method`/`kind`/`side` 三格 key 的判据本身（§1.2 ⑥.1 那条"`method` 是实测逼出来的"），
> 一对一钉在规则文件里才能指明是哪一格掉了。`NATURAL` 那一格尤其：它挡住的理由完全取决于
> sqlglot 把修饰词住在 `args["method"]`，**这条误伤是本轮"先实测再落规则"抓出来的**，
> 值得一条点名 `method` 的钉子，而不是一句"语料里有它"。

放行用例还要断言：`LIMIT 5 OFFSET 10` 不被改坏（幂等，不出现双 LIMIT）、
注入的 LIMIT 值是 `max_rows + 1`（`max_rows` 来自 `resolve_row_limit(ds)`，`HARD_LIMIT` 那个键不建，
见 §4.3 as-built）、`table_refs` 集合恰好等于 SQL 里的表（含别名解析后）。
钳位（§1.2 ⑦）另有四条各站一处：`LIMIT 5000` → `LIMIT 1001`、`LIMIT 999` 原样保留、
`LIMIT 4990, 5000` → `LIMIT 1001 OFFSET 4990`（只动 count）。

### 8.3 变异测试（一条 parametrize 自动扩到全语料）

`test_corpus_mutation.py`：对每条 ALLOW 用例施加 5 种变异并断言必须被拒 ——
追加 `; DROP TABLE x`、把某表名换成 `mysql.user`、在末尾加 `FOR UPDATE`、
把 `SELECT` 换成 `SELECT ... INTO OUTFILE '/tmp/a'`、在注释里塞 `/*!50100 ...*/`。
这是防"规则只挡顶层"的探针，价值高于再加 10 条手写用例。

### 8.4 三层独立性的验证方式

> **as-built(P5-040 文档半边)：本节这四条自 2026-09 起就是"手工操作说明"，本节先把它们各自的
> 真实档位写在纸上，再开工补证据**。三档的含义：**闸内** = 默认整轮闸真跑、不连源库；
> **live** = 挂在 `*_live.py` 上要真 MySQL 在场；**未证** = 文档字面到今天没有任何实物指着它。
> 措辞纪律：这一族的标题写的是"**三层**独立性"，而第二层的实物是 037 才有的（见本节末
> as-built(P5-037) 那条），所以任何一条在 037 之前签的字，签的都只有两层。
>
> | 本节条目 | 机制 | 文档字面那一发 | 档位在 040 开工前 |
> |---|---|---|---|
> | ① 不带 `LIMIT` → `LIMIT 1001` + 1000 行 + `truncated:true` | 在场（钳位 `tests/guard/test_guard_api.py:41` 一族、截断 `executor.py:154` `split_truncated` + `tests/unit/test_executor_truncation.py` 四条） | `order_item` + `row_limit=1000` **没跑过**；live 那发在 `tests/integration/test_execute_live.py:109`，用的是 `order_main` + **`row_limit=5`** | **机制已证、字面未跑**（闸内 + live 各一档，缺一格都不算这条签字） |
> | ② 前半：`SELECT SLEEP(20)` 被黑名单拒 | 在场（`sql_guard.py:112` `_SLEEP_FUNCTIONS`、`:363-366` 出 `sleep_function`） | 必拒语料 `tests/guard/corpus.py:107` 是 `SELECT SLEEP(30)`（**参数不同，同一条规则**） | **闸内已证** |
> | ② 后半：清空黑名单后被 `MAX_EXECUTION_TIME=1000` 截断，"错误来自驱动 timeout 而非守卫" | 在场（`executor.py:40-55` 第二条 SET、`:232` `classify_source_error`） | **实测这句话不成立**：见 §4.1 的 as-built(P5-040)，CLI 四发回的是"成功返回一行、值 1、~1.3s"，既不是 `SqlGuardError` 也不是 `QueryTimeout` | **未证，且字面待订正**（归因那一半由 040 施工那发在执行器上复现；换靶 = 洗证据） |
> | ③ validate 的 400 形状 | — | 已按实物订正，见本节末 as-built(P5-039) ①②④ | **闸内已证**（039 交付 13 条用例 + 七发真 HTTP） |
> | ④ ask 的 dry-run 分支返回 400 | 主语错位，实为 P8 | 见 as-built(P5-039) ③ | **本片不适用**（不属于 040 的证据族） |

三条"分层"这个词的**永久**证据（不靠人记得去手工点一遍）由 040 落在两处：闸内的
`tests/unit/test_layer_independence.py`（同一件 `sql_final` 分别喂三层、三层互不共享状态，
关键反证是清空第一层黑名单后 `session_statements` 那四条 SET 一个字不变）与
`tests/integration/test_layer_independence_live.py`（上面①的字面那一发 + ②那一对同一条 SQL 的两种归宿）。
**这一节原四条的下面就是它们的原文**，逐条保留不改——留着才看得出档位是从哪一句话改起的。

- 手工提交一条不带 `LIMIT` 的 `SELECT * FROM order_item` → 实际执行 SQL 日志里看到 `LIMIT 1001`
  且返回 1000 行 + `truncated:true`（第一层的强制 LIMIT 生效）。
- 在 MySQL 5.7 上提交 `SELECT SLEEP(20)` → 被黑名单拒；**把黑名单临时清空** →
  被 `SET SESSION MAX_EXECUTION_TIME=1000` 截断（错误来自驱动 timeout 而非守卫），
  证明第二/第三层独立有效。
- `POST /api/chat/validate {"sql":"SELECT 1; DROP TABLE users"}` →
  400 `{code:"sql_guard_rejected", rule_id:"multi_statement"}`
  （**这一行是简写**，实物形状见本节末 as-built(P5-039) 第 ① 点：判定看 `error.code` 与
  `error.detail.rule_id` 两格，不是顶层那两个键）。
- 即使模型产出了 `DROP`，`/api/chat/ask` 的 dry-run 分支也要返回 400 `sql_guard_rejected`，
  绝不落到执行层。（**这句的主语是 ask，不是 validate**，见 as-built(P5-039) 第 ③ 点。）

> **as-built(P5-039)：本节那两行 400 的形状订正**（文档对着实物改，不是实物迁就文档字面——
> 与 P4-032 抓 architecture §5.2 时同一条纪律）。五条：
> ① **全仓只有一个错误信封**：`app/core/errors.py` 的 `error_body()` 配 `register_exception_handlers`
> 里那条 `AppError` 处理器，回的是 `{"error": {"code", "message", "detail"}}`，HTTP 码取
> `exc.status_code`。所以 `sql_guard_rejected` 的实物是
> `400 {"error":{"code":"sql_guard_rejected","message":"…","detail":{"rule_id":"multi_statement",
> "field":null,"message":"…"}}}`。本行原写的扁平 `{code, rule_id}` **不是第二种形状**，它是同一件事的简写；
> 新开扁平形状等于给前端两条例外（roadmap §P7 验收 1 的 axios 拦截器按的就是 `code/message` 这一层）。
> 这一格由用户于 2026-10-20 拍板（工单 039 格子 ①）。
> ② **`detail` 的三格不是发明词**：守卫侧那枚 `Violation`（`sql_guard.py:128-133`）本来就是
> `code / message / node` 三格，`SqlGuardError` 又把它们原样带上身（`:141-143`）。对外的名是
> `rule_id ← Violation.code`（`rule_id` 是 roadmap 验收 2 的字面、也是 035 之后那枚封闭词表
> `GuardRuleId` 的枚举值），`field ← node`。
> **这一行原来还带着"只做在一处映射"半句，039 双轴审查时按实物删掉了**——那半句两点都不实：
> (a) 那次换名住在 `services/nl2sql/validate.py`，是**服务层**不是 API 层（`core/errors.py` 不 import
> services，所以映射放不进信封那一侧）；(b) 同一件事实今天有**三个**对外键名，不是一对一：走 HTTP 时叫
> `error.detail.rule_id`（039 新），落进 `chat_messages.guard_result.violations[]` 时叫 `code`
> （012 定的形状，`pipeline._guard_outcome`），落进同一格的 `retries[]` 时又叫 `rule_id`
> （038 在 pipeline 那一侧另起的）。三个名字、两处主人是**既有契约**，不是本片造成的；收拢它要动
> 012/038 的面，账记在工单 041。本节因此不许被读成"换名已经收拢过"——留着这种句子，下一片就会拿它
> 当凭据。
> ③ **最后那条 dry-run 的 400 讲的是 ask（P8）而不是本片**：`POST /api/chat/validate` **只跑第一层**，
> 它一次都不连源库，因此永远不会产出 dry-run 那一档的拒绝。把它照抄进 validate 的契约就是张冠李戴——
> 而且 037 之后第二层拒的是 `too_costly` / `dry_run_unreadable` / `dry_run_engine_rejected` 那三枚码
> （§1.1 那枚共用词表），038 之后它们走的是回喂重试那一路（§3.5），不是"原样 400 回给用户"。
> ④ **`detail.field` 是一枚"有主人的可空槽"，不是"定位到具体字段"**：`Violation.node` 的默认值是
> `None`（`sql_guard.py:133`），全仓写入点实测只有三族——`variable_access`（`:397`，`"、".join(sorted(hits))`）、
> `cartesian_product`（`:460`，那枚表名）、`regeneration_drift`（`:511-557`，036 那四格自校验）。
> 而 validate 最常拒的那四枚（`multi_statement` / `table_not_allowed` / `empty_statement` / `too_large`）
> 回的正是 `null`，真 HTTP 回显逐字可查（`.scratch/p5-sql-guard-hardening/evidence/out_real_http039.txt`
> 的 R1/R3/R4/R6）。所以这一格既不许写成"永远为空"（三族非空），也不许写成"能定位到具体字段"
> （今天只有三族有内容）。
> ⑤ **`default_schema=""` ⇒ 谁在 `check()` 外面调用守卫，谁就必须自带全名**：补全出来的是
> `QualifiedTable("", "", "order_main")`，白名单里是 `("", "ai_web_demo", "order_main")`，两枚三元组
> 不相等，所以 `SELECT id FROM order_main` 这类短表名**在 validate 与 ask 两条路上都拒**——同一条口径，
> 不是分叉。今天两个调用方传的都是空串（`validate.py:89`、`pipeline.py:328`），短表名在这条链路上
> **永不发生补全**。这不是 bug 是契约，钉子是
> `tests/integration/test_chat_validate_pg.py::test_短表名一律拒因为default_schema是空串`（电池的 B 格：
> 把空串换成库名 → 精确红这一条）。§9.2 那句"短表名由 `default_schema` 补全"描述的是**能力**，
> 不是今天的调用面，差额已记在那一行。
> 顺带一条本节管不到的事实：请求体本身不合法（缺字段、多字段、类型不对）出的是 **422 `invalid_request`**
> （`RequestValidationError` 处理器，`:235-247`），而"空语句 / 超长"出的是 **400 `sql_guard_rejected`**
> （守卫 `_preprocess`：`empty_statement`、`too_large`）——同一个"你的 SQL 不行"，两个主人。

> **as-built(P5-037)：上面第二条那句"证明第二/第三层独立有效"，在 037 之前是一句空头账**——
> 当时第二层零实现（§3 开头那条事实账），那条用例证的从头到尾只有第三层。现在第二层有了实物，
> 但要分清各层的反证长什么样，别让一条用例替两层签字：
> ① 第三层的独立反证仍是上面那一条（清空黑名单 → 驱动 timeout 拦下，错误正文来自源库而非守卫）；
> ② **第二层的独立反证是 `dry_run=False` 那一发**（§3.3）：断的是"源库一条 `EXPLAIN` 都没收到"
> 且真 SQL 照跑、结果一字不变——正向断"没发"，不是断"结果为空"。
> ③ 一条顺带的归因更正：`SELECT SLEEP(20)` 走不到第二层，第一层的函数规则先命中；
> 新增的 `cartesian_product` 同样在第一层命中（§3.4），**所以"第二层挡住笛卡尔积"这句以前就算实现也不成立**。
> 三层各自关掉时剩下的那两层要照样成立，这条验收的写法才是可测的。

---

## 9. Known limitations（诚实清单）

| 项 | 现状 |
|---|---|
| 服务端查询无法主动 `KILL` | 只读账号没有 `KILL` 权限；依赖 `MAX_EXECUTION_TIME` 自杀 + 归还前 `ROLLBACK` |
| **`SLEEP()` 这一类在"清空第一层"之后不产生超时错误**（as-built(P5-040 文档半边)） | roadmap §P5 验收 4 后半的原话是"清空黑名单后被 `SET SESSION MAX_EXECUTION_TIME=1000` **截断（错误来自驱动 timeout 而非守卫）**"。实测（`evidence/sleep_timeout_probe040.txt`，MySQL 5.7.17，CLI 四发）**后半个括号不成立**：`select sleep(20)` 在那一档上限下回 `rc=0`、一行 `1`、耗时 ≈1.3s——计时被叫停了（控制组不设上限跑 `sleep(3)` 回 `0`、耗时 ≈3.3s），但**没有异常可归因**，既不是 `SqlGuardError` 也不是 `QueryTimeout`。所以"**同一条 SQL 的两种归宿**"这一族反证，在 `SLEEP` 上能签的是"第一层在场→拒；第一层清空→**源库自己把它的 20s 砍成 1s**"，签不了"由驱动报错拦下"。3024 那一档只在**每一行都慢**的形状上出现（既有用例 `test_execute_live.py:177` 已在签那一族）。**不许**为了验收 4 变绿把这一条换成那个形状（工单 040 已定口径：换了就是把证据洗成结论）。执行器那一发的归因由 040 施工半边真跑后落在这里，跑出来之前本节只承诺"文档字面这句话要订正"，不承诺"第三层在这一类上独立有效" |
| 笛卡尔积检测 | **as-built(P5-037)：这一行整行重写**。原话"只对'无 join 条件的逗号连接'硬拒，其余放行（误报率高）"有两处与实物不符：① 落点不在第二层，第一层就能看见（`_join_violations`，§1.2 ⑥.1）；② 拒的**比原话宽**——`FROM a JOIN b`（不写 `ON`）与逗号连接在 AST 与重生成文本两侧都逐字同形，AST 分不开。今天真正挡住的是"某个 scope 里 `on`/`using`/`side`/`kind`/`method` 五格全空 **且该 scope 没有 `WHERE`**"；真正**仍放行**的是 `CROSS JOIN`、`STRAIGHT_JOIN`，以及"无条件笛卡尔积 + 一个无关 `WHERE`"（`FROM a, b WHERE a.x > 1`，实测与合法老式写法同构，再往下判要把连接图重建一遍）。交付记录里不许写"笛卡尔积已全面挡住" |
| 第二层估行是**引擎估算**不是真行数 | `EXPLAIN` 的 `rows` 来自统计信息。`ANALYZE` 没跑过、或统计过期时，阈值会按一个错的数裁（可能误拒也可能漏放）。这一层是**代价闸门**不是**正确性闸门**：它不改变第一层与第三层的任何判定，关掉它防御仍成立（§3.3） |
| 每条问数在源库上多一次往返 | `AIWEB_QUERY__DRY_RUN` 默认 `true`（2026-10-20 拍板），代价就是这一次 `EXPLAIN`。它和真 SQL 用同一条连接、同一个只读会话，所以不是"多建一次连接"；prod 被 `check_env` 锁死不能关，dev 想省这一次要自己改键（§3.3） |
| `dry_run_unreadable` 在演示源上今天不可达 | 全 `NULL` 估行且引擎没自证 `optimized away` 的那个形状，13 条实测（§3.2）里一条都没落到它——它是由**桩件**驱动的分支，不是真库证过的行为。同理 `EXPLAIN SHOW TABLES` 那一支（`1064`）在真链路上取不到：`guard()` 对 `EXPLAIN` 前缀先报 `top_level_not_select`。这两格**不许写成"真跑过"** |
| PG 那一支只有 SQL 形状、没有真库行为证据 | `explain_statements("postgres", …)` 四条文本由用例逐字断，但执行路对 PG 直接 501，语句发不出去（§3.1）。"PG 的 dry-run 行为正确"这句话今天没人能签 |
| 第二层的**形参名与传进来的值不是同一族** | `explain_statements(dialect, …)` 收的是守卫那侧的 `dialect` 词表（`mysql` / `postgres`），`executor.py:335` 传的是 `data_sources.kind` 列上的值（实物是 MySQL 那一支，`row.kind` 今天与 `dialect` 同词表）。两枚名字相同的一格在 PG 执行通路落地那天会不会分家（`kind='postgres'` vs 某个内部方言名）今天**没有证据**，`explain_statements` 对没登记过的值当场 `ValueError`（钉子：`test_dry_run_shape.py::test_没登记过的方言当场报错而不是默默当_mysql`），所以真分家时红的是用例而不是线上。**这一格不许写成"已对齐"** |
| PG 那一支的两个**只在 PG 上才现形**的后果（as-built(P5-037 施工半边)，双轴审查抓出来的） | ① `run_dry_run` 的 `except DBAPIError` 在**逐条发语句的循环内**且不判语句是不是 `EXPLAIN`，所以 PG 那支的 `BEGIN READ ONLY` / `SET LOCAL` 若失败，会被冠成"源库在执行 EXPLAIN 时就拒了这条 SQL"——把我们会话准备阶段的失败报成了用户的 SQL 违规，与 §1.1.1 那条"我们的 bug 不许报成用户的 SQL 违规"相反。② 同一条路径上抛异常即退出，`ROLLBACK` 发不出去，而 `dry_run.py` 自己的注释写的正是"不收回这一个事务，同一条连接下次用会带残留语义"。两支今天**都打不到**（`executor.py:302` 对 PG 直接 501，第二层在只读会话建好之后才跑），所以没有按"补一支分支"处理——为一个取不到的形状写分支，正是本节反对的那类代码。PG 执行通路落地那天这两支必须一起补，且要各配一条用例 |
| `explain_verdict` 里 `int(line["rows"])` 那一格**没有用例走 str** | 输入实测是 `int`（`probe_explain037.txt` 逐行读数），`int()` 是对驱动边界的归一：不归一时 `str > int` 抛 `TypeError`，那是 500 而不是一个裁决。str 那一支**真库取不到、也没有桩件用例**，所以"归一挡住了什么"这句话今天只有代码为证。它不进 §8 语料（语料驱动不到第二层，§3.2 第 4 条），要钉只能由假连接加发，那一格留给 038（回喂那一路的用例本来就要造第二层的失败） |
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
| ① "每条拒绝都带**枚举化**的 `rule_id`" | **as-built(P5-035)**：这半句在 035 之前一直是"文档有字、实物无枚举"——§1.1 的注解写的就是 `code: str`，17 枚码由 **20 处"首参是裸字符串"的调用点**（12 处 `SqlGuardError` ＋ 8 处 `Violation`）外加 `_scan_raw_text` 的 `(pattern, code, hint)` 三元组表里那 3 枚维持一致，没有任何一处能穷举出值域。035 落 `GuardRuleId(StrEnum)`（不是 `(str, Enum)`：后者的 `__str__` 出 `'GuardRuleId.multi_statement'`，而 `SqlGuardError` 的正文拼的是 `f"[{rule_id}] {message}"`，那是用户可见文案；`StrEnum` 的 `__str__` 就是值，ruff 的 `UP042` 也只认这一种写法）＋ 一条表驱动的封闭集合用例。**对外值一字未改**：54 条必拒语料改动前后逐条同码，对照表在 `.scratch/p5-sql-guard-hardening/evidence/ruleid_{before,after}035.txt`。工单 035 记的"22 个拒绝点"是它的计数口径（14 个 `SqlGuardError` 调用点 + 8 处 `Violation` 字面构造）；本轮 AST 实测调用点总数 **23**（14 处 `SqlGuardError` ＋ 9 处 `Violation`）、其中首参是裸串字面量的 **20** 处（12 raise ＋ 8 构造，另加 `_scan_raw_text` 三元组表那 3 枚字面量）。两个数说的是同一批点位，换算写在这里免得下人再数一遍：工单那 14 处 raise 含 `:223`（传变量 `code`）与 `check()`（把 `Violation` 翻成 raise）两处非字面量，22 减这两处就是本轮的 20；23 比 22 多的那一处是 `SqlGuardError.violation` 属性内部那次 `Violation(...)` 构造——不是拒绝点而是适配器 |
| ① 的**封闭集合判据**（035 落在 `test_rule_id_closed_set.py`）与 §8.1/§8.2 的**语料条数** | **as-built(P5-036)：两处各放宽了一格，按本节开头那条规矩先改本文再改实现**。① 判据从"每枚码都要出现在 54 条必拒语料的期望值里"放宽成"**语料期望值 ∪ `tests/guard/` 里以 `GuardRuleId.<成员>` 形式点名的 AST 扫描**"，因为 036 带来了第一枚**不可能有语料**的码 `regeneration_drift`（它防的是守卫自己的代码，实测 18 条必放行语料一条都触发不了它）。**代价要说清**：属性引用只证明"有用例点了这枚码的名"，不证明那条分支真被走过，所以这一档的码必须**另配一条失败路径用例**才算钉住（036 的那发是 `test_regeneration_invariant.py` 里手工造漂移那一支）；语料那一档保持 035 的原强度，期望值仍由 `test_reject_corpus` 逐条断言。② 工单 036 验收 4 的 (B) 支字面要求"补必拒语料"，实物**只补了规则与用例、没动 `corpus.py` 那 54/18 两条计数**——它自己的"已定口径"写着"语料的期望值不许动，本片只往新增文件里加用例"，两句话冲突时按工单自己那条口径走，hint 的三支形状钉子落在 `tests/guard/test_regeneration_invariant.py::test_hint_三种形状两条接缝同码`。 |
| `allowed_schemas` 单独入参 | 折进 `allowed`（表白名单里已带 `db`，短表名由 `default_schema` 补全）→ 效果等价且更严。**as-built(P5-039)：括号里那半句是能力，不是今天的调用面**——两个调用方（`validate.py:89`、`pipeline.py:328`）传的 `default_schema` 实测都是空串，于是短表名在两条路上**一律拒**、那个"补全"分支在整条链路上一次都没被走到（契约与钉子见 §8.4 as-built ⑤）。这一行原话留着不改，因为接口差额说的是"`allowed_schemas` 这个入参要不要立"，它的答案仍是"不立"；补的这一句只是别让下一片把"能补全"读成"会补全" |
| `forbid_files: bool = True` | 恒为真，不给关 |
| §0 表把第二层的位置写成"`sql_guard` 末尾"、拒因写成大写 `TOO_COSTLY` / `CARTESIAN_PRODUCT` | **as-built(P5-037)：两处都被实测改掉了**。① 位置：`guard()` 不碰库，第二层要在真源库连接上发 `EXPLAIN`，物理上住不进纯 AST 模块——落成 `app/services/nl2sql/dry_run.py`，由 `executor.py` 在第三层那条只读会话建好之后、取行之前调用。与第三层**分家成两个模块**也是刻意的（"分层独立有效"要求第二层能被单独关掉、单独反证）。② 字面：`TOO_COSTLY` 订正成小写 `too_costly`（落库那一侧为准）；`CARTESIAN_PRODUCT` **根本不住第二层**——逗号连接在 sqlglot 的 AST 上可见（§3.4），归第一层新枚 `cartesian_product`，第二层拿 SQL 文本再判一次是拿字符串重做 AST 已经做过的事 |
| §1.1 那句"`GuardRuleId` 是第一层的拒绝码枚举" | **as-built(P5-037)：这枚枚举现在是三层共用的拒绝词表**（18 → 22 枚）。名字保留、不重命名成 `RejectionCode` 之类：它的值已是对外契约（JSONB `chat_messages.guard_result`、语料期望值、roadmap §P5 验收 2 的字面），而"重命名"这件事本身会撞上 §1.1.1 那条 `value == name` 的机械断言——契约变了才算改，内部称呼不算。依赖方向单向：`dry_run.py → sql_guard.py`（第二层复用同一枚枚举与同一个 `SqlGuardError`，管线的 `guard_result` 与早退形状因此不用新写一套），**不是** `sql_guard` 认识 `dry_run` |
| ① 的封闭集合判据：**扫描根**（035 落、036 放宽成"语料或用例"） | **as-built(P5-037)：扫描根从"一个目录"变成"两枚显式列出"**——`tests/guard/test_*.py` ∪ `tests/unit/test_dry_run_*.py`。第二层那三枚码结构上不可能住在 `tests/guard/`（`guard()` 不碰库），把它们塞进去只是为了迁就一条机械判据的目录字面，那是让尺子决定地形。放宽的**只有位置**，强度一格没动：仍只认断言里的属性引用、仍不预先过滤成员名、三枚各配一条**失败路径用例**。036 那次放宽在这里第一次被用上，也顺手证了它当初是对的（细节见 §1.1.1 开头那条注） |
| `test_守卫源码里每一个拒绝点的首参都是枚举成员`（035 落的那条 AST 扫描；"守卫源码"当时的实物= `sql_guard.py` 一个文件） | **as-built(P5-037)：这条判据的扫描对象随实物扩了一格——不扩就会漏掉一整个模块**。037 在 `dry_run.py` 里新写 `SqlGuardError(...)` 调用点（第二层那三枚），而那条用例读的是 `Path(sql_guard.__file__)`：不改清单的话，"每一个拒绝点的首参都是枚举成员"这句话会在**我刚写下新拒绝点的那一天**变成假话，而且没人会红。所以扩成显式两枚文件清单（`sql_guard.py` + `dry_run.py`），判据本身一个字不动（`ast.Call` 首参为 str 字面量即红）。顺带一格：`test_码的字面量在源码里只出现在枚举手自己身上` 同样只扫一个文件，`dry_run.py` 里出现 `"too_costly"` 这样的裸串它看不见——本片一并把扫描范围对齐，不留两把尺子量同一件事 |
| §3.2 第 2 条那句"**每一行**的 `Extra` 都含 `optimized away` → 放行"在**零行**那一支上 | **as-built(P5-037 施工半边)：按动机实现，字面在这里有一个空洞**。同一条第 2 项把"`EXPLAIN` 回零行"列进"一行都读不出来"，紧接着那句"每一行都自证"在空序列上是 `all()` 的**空洞为真**——照字面实现会读成"引擎一个字都没说 → 放行"，第二层就变成一个永不为真的闸。实物把零行归第 3 档（`dry_run_unreadable`），钉子 `test_explain_回零行算读不出来而不是放行`，反证是电池的 E 格（翻成空洞放行 → 精确红那一条）。文档字面保留原样不改，因为要改的是**实现读它的方式**：那一支的枚举（全 `NULL`／没有 `rows` 列／零行）是"读不出来"的清单，不是"进入第 2 档之前先看 Extra"的条件 |
| §3.2 只列了三枚第二层的码，没有"超时"那一档 | **as-built(P5-037 施工半边)：`EXPLAIN` 自己超时归进 `dry_run_engine_rejected`，不开第四枚**。理由不是省事：`EXPLAIN` 不执行语句、扫不到数据，而第三层那条 `MAX_EXECUTION_TIME` 按 MySQL 文档只作用于**真正执行的只读 SELECT**，这一发在实机上取不到触发条件。真出现时正文带着引擎原话，用户读得到、038 回喂得到。第四枚码要在"它真的可能发生"之后再立 |
| 工单 037 格子 ① 的字面 `explain_statement(dialect, sql_final) -> str` | **as-built(P5-037 施工半边)：实物是 `explain_statements(...) -> tuple[str, ...]`**。§3.1.1 的 PG 一支是**四条一个序列**（`BEGIN READ ONLY` / `SET LOCAL` / `EXPLAIN` / `ROLLBACK`），单数签名装不下"顺序也是被钉对象"这件事——返回 `str` 就得在纯函数外面再写一层拼装，而那层没有自己的用例。形状钉子（四条的相对次序、`'3000ms'` 那个字面）因此能一条用例逐字对完 |
| ⑥ `CROSS_CATALOG` 独立规则 | 归 `table_not_allowed`（catalog 非空的白名单条目本来就不存在） |
| ③ `Union/Except/Intersect` 递归校验臂 | 未写：sqlglot 对 `SELECT 1 UNION DROP TABLE x` 直接 ParseError，规则跑不到那一层 |
| ⑤ `AIWEB_GUARD__DANGLING_EXTRA_RULES` 追加黑名单 | 未实现。"拒绝一切 Anonymous" 已经是超集，追加黑名单只在"想放开某个函数"时才有意义，而那不在 v1 计划里 |
| ⑦ `HARD_LIMIT` 收敛模型自写的 `LIMIT 999999` | **已收敛（2026-09-29 拍板并落地）**：`guard()` 的 LIMIT 分支从"缺了才补"改成"钳位 + 探针"，见 §1.2 ⑦。`HARD_LIMIT` 那个键**仍不建**——注入值就是 `resolve_row_limit(ds) + 1`，不需要第二个数 |
| ⑧ `truncated` 与内存上限在真链路上双双落空（**已闭环**） | 两个后果同源：守卫当时只补不钳。① prompt 模板要求模型写 `LIMIT {{ row_limit }}`（`nl2sql_user.j2:29`），模型一写守卫就不补 `+1` 探针，`split_truncated` 因此**恒报未截断**——"这 1000 行只是前 1000 行"这句话说不出来；② 模型写 `LIMIT 5000` 时那句**原样放行**，011 的"缓冲取回的内存上限由守卫注入的 `row_limit+1` 钉住"这个前提对它不成立。**拍板口径**：`n` 是确定的非负整数且小于 `max_rows` 时尊重模型自己按问题意图选的量，其余（缺 LIMIT、`n >= max_rows`、不可证如 `LIMIT ALL`/`LIMIT 1 + 1`/`LIMIT 1e3`/`LIMIT -1`）一律钳成 `max_rows+1`——"证不了它小"与"没有 LIMIT"同一档，不给它多一条出路。**闭环范围要说清**：`n < max_rows` 那一支仍然没有探针、`truncated` 恒假，这是有意的（上限没碰到就谈不上截断），别当残留的洞去修。钉子随之从"钉住现状"翻成"钉住钳位"：`tests/guard/test_guard_api.py` 五条（超限钳、上限之下保留、只动 count 不动 offset、不可证四形状、`<= max_rows+1` 不变式）+ `tests/integration/test_pipeline_orchestration.py`（编排侧那支现在断 `sql_raw != sql_final` 且结尾是探针） |
| §4.1 那句"1193 降级时**靠驱动侧 `read_timeout`** 兜住"（as-built(P2-011) 写的）＋ roadmap §P5 踩坑 ④"asyncmy 的 `read_timeout` 与 `MAX_EXECUTION_TIME` **谁先触发**要实测" | **as-built(P5-040 文档半边)：这个问题今天不是一个二选一，它是一个"只有一个在挡"**。实测 `grep -rn "read_timeout" backend/app backend/scripts backend/tests` → **零命中**（全仓两处命中都在文档里：本节 §4.1 与 roadmap 踩坑 ④）。驱动侧唯一被设过的一格是 `connect_timeout`：`source_connect_args`（`services/source_manager.py:53-59`）把 `timeout_ms` 折成秒落给它，`create_source_engine`（`:62-74`）的 docstring 明写这是为了"握手卡住别挂着请求"——**它管连接建立，不管语句执行**。所以：① 踩坑 ④ 的实测答案是"没有第二个竞争者，`MAX_EXECUTION_TIME` 是唯一在挡执行时长的那一格"；② 更要紧的是 1193（源库不支持该变量，MySQL < 5.7.4）那条降级支上**当前一个都不挡**——文档原句把不存在的兜底写成了存在的。设 `read_timeout` = 新增能力（要定档位、要定它与 `connect_timeout` 的关系），用户 2026-10-20 在工单 040 格子 ① 拍板**本轮不设**，要设另开工单。这一行留着就是那张开工单的门口 |
