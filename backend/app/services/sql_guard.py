"""SQL 守卫：三层只读防御的第一层（正向 AST 白名单）。

契约见 docs/nl2sql-safety.md §1。本模块只做"允许什么"的枚举——任何未列举的结构一律拒绝，
所以新增语法形态的默认结果是拒，不是放。

规则有先后：预处理 → 裸文本（注释/锁定/INTO）→ 解析 → 单语句 → 顶层类型 → 子结构
（CTE 内 DML、SELECT INTO、变量）→ 函数 → 表白名单 → **连接条件**（037 起）→ 强制 LIMIT → 重生成
→ **重生成等价自校验**。
最后那一步防的不是模型而是本模块自己：它红了就拒答（§1.3）。
`check()` 命中第一条即抛错，`guard()` 收集全部违规（HTTP 层要一次展示所有问题）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, cast

import sqlglot
from sqlglot import exp

Dialect = Literal["mysql", "postgres"]


class GuardRuleId(StrEnum):
    """拒绝词表——**对外契约**，不是内部名册。

    值是接口：它落进 `chat_messages.guard_result`（JSONB）、被 `tests/guard/corpus.py` 每一条必拒
    语料点名、被 roadmap §P2/§P3/P5 的验收字面引用。所以成员一律**显式赋值**（不用
    `AUTO`），且成员名与值保持一字不差——`tests/guard/test_rule_id_closed_set.py` 钉住这点。
    新增拒绝分支只能加在这里；改名或改值都要先改 `docs/nl2sql-safety.md` §1.1.1 那张表。
    as-built(P5-037)：第二层那三枚（`too_costly`/`dry_run_unreadable`/`dry_run_engine_rejected`）
    已随 037 加入，所以这一枚枚举今天是**三层共用**的词表，不再只住第一层；名字保留 `GuardRuleId`
    是因为对外那一格（`chat_messages.guard_result`）从第一天就叫 `guard_result`。

    继承 `str` 是硬要求：否则 JSONB 序列化变、`assert ei.value.rule_id == "multi_statement"`
    那一族断言整片红。选 `StrEnum` 而不是 `(str, Enum)` 是为了 `__str__`：后者的 `__str__` 出
    `'GuardRuleId.multi_statement'`，而 `SqlGuardError` 的正文拼的是 `f"[{rule_id}] {message}"`——
    纯类型重构会顺手改掉用户可见文案。`StrEnum` 的 `__str__` 就是值，
    ruff 的 UP042 也只要这一种写法。
    """

    empty_statement = "empty_statement"
    too_large = "too_large"
    unsupported_construct = "unsupported_construct"
    hint_not_allowed = "hint_not_allowed"
    executable_comment = "executable_comment"
    procedure_analyse = "procedure_analyse"
    locking_clause = "locking_clause"
    into_outfile = "into_outfile"
    multi_statement = "multi_statement"
    prepared_statement = "prepared_statement"
    top_level_not_select = "top_level_not_select"
    dml_in_cte = "dml_in_cte"
    variable_access = "variable_access"
    pg_file_access = "pg_file_access"
    sleep_function = "sleep_function"
    danger_function = "danger_function"
    table_not_allowed = "table_not_allowed"
    cartesian_product = "cartesian_product"
    regeneration_drift = "regeneration_drift"
    # 下面三枚属**第二层**（引擎 `EXPLAIN` dry-run，`app/services/nl2sql/dry_run.py`）。
    # 它们与上面那些共用一枚枚举是刻意的：落进 `chat_messages.guard_result` 的是同一种东西，
    # 词表开两处就等于对外契约开两处。依赖方向单向 `dry_run.py → sql_guard.py`。
    too_costly = "too_costly"
    dry_run_unreadable = "dry_run_unreadable"
    dry_run_engine_rejected = "dry_run_engine_rejected"


_MAX_SQL_LEN = 8_000

# 顶层只认这四种查询表达式；SHOW/DESCRIBE 等在 sqlglot 里落成 Command，天然被挡
_TOP_LEVEL = (exp.Select, exp.Union, exp.Except, exp.Intersect)

# 服务端预编译语句即便内容无害也不放行：它的正文可以完全绕过本守卫的静态分析
_PREPARED_COMMANDS = frozenset({"PREPARE", "EXECUTE", "DEALLOCATE", "DISCARD"})

_CONTROL_CHARS = frozenset({"\x00", "\x1a"})

# 锁定子句在 sqlglot 里多数能落成 exp.Lock，但 `FOR SHARE o.id` 这种带目标的写法直接解析失败，
# 所以统一走裸文本：FOR 是保留字，掩码后不可能落在字面量里。
_LOCKING = re.compile(
    r"\bFOR\s+(?:NO\s+KEY\s+|KEY\s+)?(?:UPDATE|SHARE)\b|\bLOCK\s+IN\s+SHARE\s+MODE\b",
    re.IGNORECASE,
)

_FENCE = re.compile(r"^```[^\n]*\n([\s\S]*?)\n?```$")

# 5.7 的 PROCEDURE ANALYSE() 会让 parser 直接失败，只能靠裸文本兜住
_PROCEDURE_ANALYSE = re.compile(r"\bPROCEDURE\s+ANALYSE\s*\(", re.IGNORECASE)

# INTO 的三种导出目标。INSERT INTO <表> 不在此列——那是顶层类型规则管的事。
_INTO_TARGET = re.compile(r"\bINTO\s+(?:OUTFILE\b|DUMPFILE\b|@@?[A-Za-z_])", re.IGNORECASE)

# 具名危险函数只用于细分归因；真正的防线是"Unknown 函数一律拒"。
# pg 侧的文件与时钟函数归一组：它们都是"只有超级用户才该有"的能力。
_PG_PRIVILEGED_FUNCTIONS = frozenset(
    {
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_ls_logdir",
        "pg_ls_waldir",
        "pg_ls_tmpdir",
        "pg_sleep",
        "pg_advisory_lock",
        "pg_cancel_backend",
        "pg_terminate_backend",
    }
)
_SLEEP_FUNCTIONS = frozenset({"sleep"})

# 即便用户被授予了这些 schema 也一律拒：元数据从我们自己的库里读，不靠源库的字典表
_SYSTEM_SCHEMAS = frozenset(
    {
        "information_schema",
        "mysql",
        "performance_schema",
        "sys",
        "pg_catalog",
        "pg_toast",
    }
)


@dataclass(frozen=True)
class Violation:
    """一条拒绝理由。`code` 可枚举，用于统计模型最常踩哪条规则。"""

    code: GuardRuleId
    message: str
    node: str | None = None


class SqlGuardError(Exception):
    """拒绝执行。`rule_id` 与 `violation` 同源，抛/不抛两条接缝共用一套规则。"""

    def __init__(self, rule_id: GuardRuleId, message: str, node: str | None = None) -> None:
        super().__init__(f"[{rule_id}] {message}")
        self.rule_id = rule_id
        self.message = message
        self.violation = Violation(rule_id, message, node)


@dataclass(frozen=True)
class QualifiedTable:
    """跨方言归一后的表引用。MySQL 的 catalog 恒为空串。"""

    catalog: str
    db: str
    name: str

    @staticmethod
    def parse(dotted: str) -> QualifiedTable:
        parts = [p.lower() for p in dotted.split(".") if p]
        if len(parts) == 3:
            return QualifiedTable(parts[0], parts[1], parts[2])
        if len(parts) == 2:
            return QualifiedTable("", parts[0], parts[1])
        return QualifiedTable("", "", parts[0])


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    sql_final: str | None
    tables: tuple[QualifiedTable, ...]
    violations: tuple[Violation, ...]


def _unfence(sql: str) -> str:
    """剥掉模型回答里的 ``` 代码块围栏——这是输入卫生，不是安全规则。"""
    match = _FENCE.match(sql.strip())
    return match.group(1) if match else sql


def _preprocess(sql: str) -> str:
    stripped = _unfence(sql).strip().rstrip(";").strip()
    if not stripped:
        raise SqlGuardError(GuardRuleId.empty_statement, "语句为空，没有可执行的查询")
    if len(stripped) > _MAX_SQL_LEN:
        raise SqlGuardError(
            GuardRuleId.too_large, f"SQL 长度 {len(stripped)} 超过上限 {_MAX_SQL_LEN}"
        )
    # \t \n \r 之外的一切 C0 控制字符都是注入手法（\x00 截断、\x1a 反向转义）
    if any(ord(c) < 0x20 and c not in "\t\n\r" for c in stripped):
        raise SqlGuardError(GuardRuleId.unsupported_construct, "SQL 含控制字符")
    return stripped


def _is_line_comment(sql: str, i: int, ch: str) -> bool:
    if ch == "#":
        return True
    # MySQL 要求 `--` 后跟空白，否则 `a--b` 里的两个减号是运算符不是注释
    return ch == "-" and sql.startswith("--", i) and (i + 2 >= len(sql) or sql[i + 2] in " \t\r\n")


def _strip_comments(sql: str, *, escape_backslash: bool) -> tuple[str, str]:
    """抹掉注释，返回 (送解析的文本, 用于裸文本扫描的掩码文本)。

    必须自己扫：sqlglot 的 tokenizer 直接丢弃注释内容，我们无从判断里面写了什么，
    而 `/*!50100 ... */` 在 MySQL 里是真会执行的。

    掩码文本把引号内的内容换成空格（保留引号本身）：`WHERE remark = 'a INTO OUTFILE b'`
    里的字面量是数据不是指令，不掩码就会误拒。掩码只用于扫描，解析仍用原文。

    `escape_backslash` 必须按方言传对：PG 标准字符串里 `\'` 不转义，若在 PG 侧按 MySQL 语义
    把它当转义，`SELECT 'a\\' … FROM t FOR UPDATE` 的尾巴会被吞进"字符串"，锁定子句就漏检。
    """
    clean: list[str] = []
    masked: list[str] = []
    i, n = 0, len(sql)
    quote = ""
    while i < n:
        ch = sql[i]
        if quote:
            clean.append(ch)
            if escape_backslash and ch == "\\" and quote in "'\"" and i + 1 < n:
                clean.append(sql[i + 1])
                masked.append("  ")
                i += 2
                continue
            masked.append(ch if ch == quote else " ")
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
            clean.append(ch)
            masked.append(ch)
            i += 1
            continue
        if ch == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end < 0:
                raise SqlGuardError(
                    GuardRuleId.unsupported_construct, "块注释没有闭合，无法判断其内容"
                )
            body = sql[i + 2 : end]
            if body.startswith("+"):
                # 超时靠 SET SESSION MAX_EXECUTION_TIME，不靠 hint：hint 一旦放行就等于开了
                # "注释里能塞指令"的口子，而重生成又会把注释抹掉，两者不能兼得。
                raise SqlGuardError(GuardRuleId.hint_not_allowed, f"禁止优化器 hint：{body[:40]}")
            if body.startswith("!"):
                raise SqlGuardError(GuardRuleId.executable_comment, f"禁止可执行注释：{body[:40]}")
            clean.append(" ")
            masked.append(" ")
            i = end + 2
            continue
        if _is_line_comment(sql, i, ch):
            line = sql.find("\n", i)
            i = n if line < 0 else line  # 保留换行，免得把下一行粘进本行
            clean.append(" ")
            masked.append(" ")
            continue
        clean.append(ch)
        masked.append(ch)
        i += 1
    return "".join(clean), "".join(masked)


def _scan_raw_text(sql: str, *, dialect: Dialect) -> str:
    """AST 之前先看裸文本：sqlglot 解析不了的结构，不能等到 AST 再管。"""
    cleaned, masked = _strip_comments(sql, escape_backslash=dialect == "mysql")
    if not cleaned.strip():
        raise SqlGuardError(GuardRuleId.empty_statement, "注释之外没有内容")
    for pattern, code, hint in (
        (_PROCEDURE_ANALYSE, GuardRuleId.procedure_analyse, "禁止 PROCEDURE ANALYSE"),
        (_LOCKING, GuardRuleId.locking_clause, "禁止锁定子句"),
        (_INTO_TARGET, GuardRuleId.into_outfile, "禁止 INTO 导出"),
    ):
        match = pattern.search(masked)
        if match:
            raise SqlGuardError(code, f"{hint}：{masked[match.start() :][:40]}")
    return cleaned


def parse(sql: str, *, dialect: Dialect) -> exp.Expression:
    """严格解析并保证单语句：不宽容、不降级。"""
    try:
        stmts = sqlglot.parse(sql, read=dialect, error_level=sqlglot.ErrorLevel.RAISE)
    except sqlglot.errors.SqlglotError as exc:
        # 不只是 ParseError：未闭合字符串抛 TokenError、部分方言语法抛 UnsupportedError。
        # 任何一条漏出去就是 HTTP 500 而不是 400 sql_guard_rejected。
        raise SqlGuardError(GuardRuleId.unsupported_construct, f"无法解析：{exc}") from exc
    if len(stmts) != 1 or stmts[0] is None:
        raise SqlGuardError(GuardRuleId.multi_statement, "一次只允许一条语句")
    stmt = stmts[0]
    if not isinstance(stmt, exp.Expression):
        # sqlglot 的类型签名是 Expr，比 Expression 更宽；后面的规则全部按 Expression 走
        raise SqlGuardError(
            GuardRuleId.unsupported_construct, f"解析结果不是 AST 表达式：{type(stmt).__name__}"
        )
    return stmt


def _check_top_level(stmt: exp.Expression) -> None:
    if isinstance(stmt, _TOP_LEVEL):
        return
    if isinstance(stmt, exp.Command) and stmt.name.upper() in _PREPARED_COMMANDS:
        raise SqlGuardError(
            GuardRuleId.prepared_statement, "预编译语句的正文绕过静态分析，不予执行"
        )
    raise SqlGuardError(
        GuardRuleId.top_level_not_select, f"只允许 SELECT，当前是 {type(stmt).__name__}"
    )


def _ident(node: exp.Expression, key: str) -> str:
    """取一个标识符部分并按引用方式归一。

    未加引号：折叠小写（PG 未引号标识符本来就是小写，MySQL 靠 lower 比对回避
    `lower_case_table_names` 的差异）。加引号：保留原样——PG 里 `"Customer"` 和
    `customer` 是两张表，一并折叠就等于放过一张没授权的表。
    """
    arg = node.args.get(key)
    if arg is None:
        return ""
    if isinstance(arg, exp.Identifier) and arg.args.get("quoted"):
        return arg.name
    return arg.name.lower()


def _table_refs(stmt: exp.Expression, default_schema: str) -> tuple[QualifiedTable, ...]:
    """收集语句引用的所有实表。

    CTE 名要剔除（它们是查询内部临时名）；三段式列前缀也要收，否则
    `SELECT mysql.user.User` 这种不带 FROM 的绕过会漏网。
    """
    cte_names = {cte.alias_or_name.lower() for cte in stmt.find_all(exp.CTE)}
    default = default_schema.lower()
    refs: list[QualifiedTable] = []
    for table in stmt.find_all(exp.Table):
        catalog, db, name = _ident(table, "catalog"), _ident(table, "db"), _ident(table, "this")
        if name.lower() in cte_names and not catalog and not db:
            continue
        refs.append(QualifiedTable(catalog, db or default, name))
    for col in stmt.find_all(exp.Column):
        catalog, db = _ident(col, "catalog"), _ident(col, "db")
        table_part = _ident(col, "table")
        if not (catalog or db) or not table_part:
            continue
        refs.append(QualifiedTable(catalog, db or default, table_part))
    return tuple(dict.fromkeys(refs))


def _function_violations(stmt: exp.Expression) -> list[Violation]:
    """函数黑名单。

    sqlglot 认得的函数会落成具体 `exp.Func` 子类，认不出的全部落成 `exp.Anonymous`——
    所以"拒绝所有 Anonymous"就是正向白名单：数据库新增的危险函数无需改这里就已经被挡。
    具名集合只用来把归因拆细（sleep / pg 文件与时钟 / 其余危险函数）。
    """
    out: list[Violation] = []
    for fn in stmt.find_all(exp.Anonymous):
        name = fn.name.lower()
        if name in _PG_PRIVILEGED_FUNCTIONS:
            out.append(
                Violation(GuardRuleId.pg_file_access, f"禁止访问服务器文件/时钟的函数 {name}()")
            )
        elif name in _SLEEP_FUNCTIONS:
            out.append(
                Violation(
                    GuardRuleId.sleep_function, f"禁止时间函数 {name}()，它让语句超时形同虚设"
                )
            )
        else:
            out.append(
                Violation(GuardRuleId.danger_function, f"白名单外的函数 {name}()，无法判定其副作用")
            )
    return out


def _shape_violations(stmt: exp.Expression) -> list[Violation]:
    """顶层是 SELECT 不代表整棵树只读：CTE 里能藏 DML，SELECT 后能挂 INTO。"""
    out = [
        Violation(GuardRuleId.dml_in_cte, f"子查询/CTE 里禁止 {type(dml).__name__}")
        for dml in stmt.find_all(exp.Insert, exp.Update, exp.Delete, exp.Merge)
    ]
    if stmt.find(exp.Into) is not None:
        out.append(Violation(GuardRuleId.into_outfile, "禁止 SELECT INTO 建表"))
    return out


def _variable_violations(stmt: exp.Expression) -> list[Violation]:
    hits = {
        type(node).__name__
        for node in stmt.find_all(
            exp.Parameter, exp.SessionParameter, exp.Placeholder, exp.PropertyEQ
        )
    }
    if not hits:
        return []
    return [
        Violation(
            GuardRuleId.variable_access,
            "禁止变量/占位符/`:=` 赋值：取值要到运行时才知道，静态分析覆盖不到",
            "、".join(sorted(hits)),
        )
    ]


def _table_violations(
    refs: tuple[QualifiedTable, ...],
    allowed: frozenset[QualifiedTable],
    *,
    default_schema: str,
) -> list[Violation]:
    # `SELECT 1` 无表是合法只读查询，没有可越权的对象，不必强求引用一张表
    out: list[Violation] = []
    for ref in refs:
        if ref.db in _SYSTEM_SCHEMAS or ref.catalog in _SYSTEM_SCHEMAS:
            out.append(
                Violation(
                    GuardRuleId.table_not_allowed,
                    f"系统字典表 {ref.catalog}.{ref.db}.{ref.name} 禁止访问（即使已授权）",
                )
            )
        elif ref not in allowed:
            out.append(
                Violation(
                    GuardRuleId.table_not_allowed,
                    f"表 {ref.catalog}.{ref.db}.{ref.name} 不在白名单内"
                    + ("" if default_schema else "（未提供 default_schema，短表名无法补全）"),
                )
            )
    return out


def _join_violations(stmt: exp.Expression) -> list[Violation]:
    """§1.2 ⑥.1 的连接条件扫描（新枚 `cartesian_product`，工单 037 实测定的落点）。

    判据逐 scope：一个 `exp.Join` 的 `on`/`using`/`side`/`kind`/`method` **五格全空**，
    且它所在的那个 `Select` 没有 `WHERE` → 连接条件无从推出，结果行数是两表之积。
    口径与每条实测（`FROM a JOIN b` 不写 `ON` 与逗号连接逐字同形、`NATURAL` 住在 `method`、
    派生表内层可见）都在 `docs/nl2sql-safety.md` §3.4 与 §1.2 ⑥.1，证据文件
    `.scratch/p5-sql-guard-hardening/evidence/probe_cartesian_ast037.txt`。

    `method` 那一格是实测逼出来的：sqlglot 把 `NATURAL` 放进了 `args["method"]`，而 `side`/`kind`
    都是空串——不查它就会把**自带连接条件**的 `NATURAL JOIN` 一起误拒。

    这一支今天挡住的只有"完全没条件"那一档。`FROM a, b WHERE a.x > 1`（笛卡尔积 + 无关条件）
    照样放行：实测它与合法的老式写法在 AST 上同构，再往下判要把连接图重建一遍（§9 已知限制）。
    """
    out: list[Violation] = []
    for join in stmt.find_all(exp.Join):
        args = join.args
        if any(args.get(key) for key in ("on", "using", "side", "kind", "method")):
            continue
        scope = join.find_ancestor(exp.Select)
        # 找不到 `Select` 祖先这一支今天不可达，`_check_top_level` 已把语句限定在 SELECT/UNION 族。
        # 但"不可达"不等于"放行"：这一模块的默认结果是拒（见文件头第 4 行），
        # 判不出来就按最严的一档——与 §1.2 ⑦"证不了它小就按最严"同一条纪律。
        if scope is not None and scope.args.get("where") is not None:
            continue
        table = join.this.sql()
        out.append(
            Violation(
                GuardRuleId.cartesian_product,
                f"表 {table} 与前面的表没有任何连接条件"
                "（没有 ON/USING，所在作用域也没有 WHERE），会做笛卡尔积",
                table,
            )
        )
    return out


def regeneration_violation(
    final: exp.Expression,
    sql_final: str,
    *,
    dialect: Dialect,
    default_schema: str,
    refs: tuple[QualifiedTable, ...],
) -> Violation | None:
    """§1.3 的重生成等价自校验：守卫端出去的那句话，必须**还是它判过的那句话**。

    比对的左侧是 `final`（钳位**之后**那棵树）与白名单判过的那批表 `refs`，右侧是
    `parse(sql_final)`——不是模型原文那棵 `stmt`：`guard()` 自己会注入/改写顶层 LIMIT，
    "重生成 ≠ 原文"是设计而不是漂移。

    跑**一条不动点 + 三格语义承重**，每条都有自己的红法：

    ① **不动点**：`sql_final` 再解析、再生成，两次文本逐字相等；
    ② **顶层形状**：`type(final) is type(parse(sql_final))`；
    ③ **表引用**：重 parse 回来的那棵树引用的表集合，与白名单判过的 `refs` **逐字相同**
       （比集合不比顺序：生成器换 JOIN 写法不是换表）；
    ④ **顶层行数**：两边用同一个 `_limit_row_count` 读出的那个数一致。

    > **为什么不是 `Expression.__eq__`**（工单 036 已定口径按实测改的形，证据
    > `.scratch/p5-sql-guard-hardening/evidence/drift_diagnosis036.txt`）：
    > `__eq__` 在实物上**两头都不对**。它过火——
    > `SELECT id FROM customer LIMIT 5000 UNION ALL SELECT id FROM payment_record LIMIT 3`
    > 今天合法放行，而生成器会给带 LIMIT 的那条臂加括号、重 parse 回来是 `Subquery`
    > （树差 `.this: Select vs Subquery`），语义一字未变却判成漂移，等于误杀；
    > 它又不及物——sqlglot 30.19 下 `__eq__` **看不见注释的丢失**：PG 那支 `/*+ SeqScan(t1) */`
    > 重 parse 回来树上已经没有那个节点（探针里写的是"重 parse 之后 hint 还在树上？ False"），
    > 而 `tree == reparsed` 仍是 `True`（`.scratch/.../evidence/hint_probe036.txt`）。
    > ②③④ 这三格挑的是"守卫判过的那些**承重事实**在执行的那句话里还在不在"，
    > 括号、`AS`、空白这类渲染形状不进判定。

    红了按**拒答**处理（不降级成 warning）：这一条红了我们连"执行的是哪句话"都不知道。
    它是**内部一致性故障**，所以挂 `regeneration_drift` 而绝不挂 `unsupported_construct`——
    后者等于把我们的 bug 报成用户的 SQL 违规。
    """
    try:
        stmts = sqlglot.parse(sql_final, read=dialect, error_level=sqlglot.ErrorLevel.RAISE)
    except sqlglot.errors.SqlglotError as exc:
        return Violation(
            GuardRuleId.regeneration_drift,
            f"重生成不等价：端出去的 SQL 连再解析一次都过不去（{sql_final[:80]!r}）",
            type(exc).__name__,
        )
    if len(stmts) != 1 or stmts[0] is None:
        return Violation(
            GuardRuleId.regeneration_drift,
            f"重生成不等价：端出去的 SQL 不再是一条语句（{sql_final[:80]!r}）",
            f"语句数={len(stmts)}",
        )
    again = stmts[0]

    fixed_point = again.sql(dialect=dialect, comments=False)
    if fixed_point != sql_final:
        return Violation(
            GuardRuleId.regeneration_drift,
            "重生成不等价：不动点那一格红——再解析再生成之后文本变了"
            f"（{sql_final[:80]!r} → {fixed_point[:80]!r}）",
            "不动点",
        )

    if type(final) is not type(again):
        return Violation(
            GuardRuleId.regeneration_drift,
            "重生成不等价：顶层形状那一格红——守卫那棵树是 "
            f"{type(final).__name__}，端出去的 SQL 解析回来是 {type(again).__name__}"
            f"（{sql_final[:80]!r}）",
            "顶层形状",
        )

    judged = set(refs)
    reparsed_refs = set(_table_refs(again, default_schema))
    if reparsed_refs != judged:
        only_new = sorted(f"{r.catalog}.{r.db}.{r.name}" for r in reparsed_refs - judged)
        only_old = sorted(f"{r.catalog}.{r.db}.{r.name}" for r in judged - reparsed_refs)
        return Violation(
            GuardRuleId.regeneration_drift,
            "重生成不等价：表引用那一格红——白名单判过 "
            f"{len(refs)} 张表，端出去的 SQL 引用的是另一批（多出 {only_new}／少了 {only_old}）",
            "表引用",
        )

    final_rows = _limit_row_count(final)
    again_rows = _limit_row_count(again)
    if final_rows != again_rows:
        return Violation(
            GuardRuleId.regeneration_drift,
            f"重生成不等价：顶层行数那一格红——守卫那棵树是 {final_rows}，"
            f"端出去的 SQL 读出来是 {again_rows}（{sql_final[:80]!r}）",
            "顶层行数",
        )
    return None


def _limit_row_count(stmt: exp.Expression) -> int | None:
    """顶层 LIMIT 那个**确定的**非负行数；不是这个形状就返回 None。

    None 不等于"没有 LIMIT"，而是"证不了它小"：`LIMIT ALL`（pg 语法，mysql 方言下落成标识符）、
    `LIMIT 1 + 1`（表达式）、`LIMIT 1e3`（`int()` 认不出的数字写法）都会落成 None。
    负数也归这一档：sqlglot 把 `LIMIT -1` 落成 `Neg` 而不是数字 `Literal`，所以这里读不到数——
    钳位顺带把它从"源库报 1210 Incorrect arguments"变成一条能跑的截断查询。
    """
    limit = stmt.args.get("limit")
    if limit is None:
        return None
    expr = limit.expression
    if isinstance(expr, exp.Literal) and expr.is_number:
        try:
            rows = int(str(expr.this))
        except ValueError:
            return None
        return rows if rows >= 0 else None
    return None


def guard(
    sql: str,
    *,
    allowed: frozenset[QualifiedTable],
    dialect: Dialect = "mysql",
    default_schema: str = "",
    max_rows: int = 1000,
) -> GuardResult:
    """校验并重生成；不抛异常，一次收集全部违规（前端要一并展示）。"""
    try:
        stmt = parse(_scan_raw_text(_preprocess(sql), dialect=dialect), dialect=dialect)
        _check_top_level(stmt)
    except SqlGuardError as exc:
        # 解析失败之前拿不到 AST，后面的规则无从跑起，只能报这一条
        return GuardResult(ok=False, sql_final=None, tables=(), violations=(exc.violation,))

    refs = _table_refs(stmt, default_schema)
    violations = [
        *_shape_violations(stmt),
        *_variable_violations(stmt),
        *_function_violations(stmt),
        *_table_violations(refs, allowed, default_schema=default_schema),
        *_join_violations(stmt),
    ]
    if violations:
        return GuardResult(ok=False, sql_final=None, tables=refs, violations=tuple(violations))

    final: exp.Expression = stmt
    rows = _limit_row_count(stmt)
    if rows is None or rows >= max_rows:
        # 不包一层子查询：MySQL 5.7 优化器会丢掉子查询里的 ORDER BY，直接加在最外层更稳。
        # limit() 只长在 exp.Query 上，而 _check_top_level 已经保证 stmt 是它的后代。
        # 只在"读得出一个确定且小于上限的行数"时保留模型自己写的数（"前 5 名"不该被抬到上限）；
        # 其余一律钳成 max_rows+1：它既是缺 LIMIT 时的探针，也是超限/不可证时的内存上限。
        # 外面那层 cast 是类型桩的事：sqlglot 的 `Query` 在桩里不是 `Expression` 的后代，
        # 运行时是（`SELECT` 那棵树同时是两者），所以钳位回来的那棵树要按 Expression 记。
        final = cast("exp.Expression", cast("exp.Query", stmt).limit(max_rows + 1))

    sql_final = final.sql(dialect=dialect, comments=False)
    drift = regeneration_violation(
        final, sql_final, dialect=dialect, default_schema=default_schema, refs=refs
    )
    if drift is not None:
        # 自校验红 = 我们不知道执行的是哪句话。按拒答处理，不降级成 warning（§1.3）。
        return GuardResult(ok=False, sql_final=None, tables=refs, violations=(drift,))

    return GuardResult(
        ok=True,
        sql_final=sql_final,
        tables=refs,
        violations=(),
    )


def check(
    sql: str,
    *,
    allowed: frozenset[QualifiedTable],
    dialect: Dialect = "mysql",
    default_schema: str = "",
    max_rows: int = 1000,
) -> GuardResult:
    """`guard()` 的抛异常版本：命中第一条违规即 `SqlGuardError`，供执行链路用。"""
    out = guard(
        sql, allowed=allowed, dialect=dialect, default_schema=default_schema, max_rows=max_rows
    )
    if not out.ok:
        first = out.violations[0]
        raise SqlGuardError(first.code, first.message, first.node)
    return out
