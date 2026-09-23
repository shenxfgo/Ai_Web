"""SQL 守卫：三层只读防御的第一层（正向 AST 白名单）。

契约见 docs/nl2sql-safety.md §1。本模块只做"允许什么"的枚举——任何未列举的结构一律拒绝，
所以新增语法形态的默认结果是拒，不是放。

规则有先后：预处理 → 裸文本（注释/锁定/INTO）→ 解析 → 单语句 → 顶层类型 → 子结构
（CTE 内 DML、SELECT INTO、变量）→ 函数 → 表白名单 → 强制 LIMIT → 重生成。
`check()` 命中第一条即抛错，`guard()` 收集全部违规（HTTP 层要一次展示所有问题）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import sqlglot
from sqlglot import exp

Dialect = Literal["mysql", "postgres"]

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

    code: str
    message: str
    node: str | None = None


class SqlGuardError(Exception):
    """拒绝执行。`rule_id` 与 `violation` 同源，抛/不抛两条接缝共用一套规则。"""

    def __init__(self, rule_id: str, message: str, node: str | None = None) -> None:
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
        raise SqlGuardError("empty_statement", "语句为空，没有可执行的查询")
    if len(stripped) > _MAX_SQL_LEN:
        raise SqlGuardError("too_large", f"SQL 长度 {len(stripped)} 超过上限 {_MAX_SQL_LEN}")
    # \t \n \r 之外的一切 C0 控制字符都是注入手法（\x00 截断、\x1a 反向转义）
    if any(ord(c) < 0x20 and c not in "\t\n\r" for c in stripped):
        raise SqlGuardError("unsupported_construct", "SQL 含控制字符")
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
                raise SqlGuardError("unsupported_construct", "块注释没有闭合，无法判断其内容")
            body = sql[i + 2 : end]
            if body.startswith("+"):
                # 超时靠 SET SESSION MAX_EXECUTION_TIME，不靠 hint：hint 一旦放行就等于开了
                # "注释里能塞指令"的口子，而重生成又会把注释抹掉，两者不能兼得。
                raise SqlGuardError("hint_not_allowed", f"禁止优化器 hint：{body[:40]}")
            if body.startswith("!"):
                raise SqlGuardError("executable_comment", f"禁止可执行注释：{body[:40]}")
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
        raise SqlGuardError("empty_statement", "注释之外没有内容")
    for pattern, code, hint in (
        (_PROCEDURE_ANALYSE, "procedure_analyse", "禁止 PROCEDURE ANALYSE"),
        (_LOCKING, "locking_clause", "禁止锁定子句"),
        (_INTO_TARGET, "into_outfile", "禁止 INTO 导出"),
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
        raise SqlGuardError("unsupported_construct", f"无法解析：{exc}") from exc
    if len(stmts) != 1 or stmts[0] is None:
        raise SqlGuardError("multi_statement", "一次只允许一条语句")
    return stmts[0]


def _check_top_level(stmt: exp.Expression) -> None:
    if isinstance(stmt, _TOP_LEVEL):
        return
    if isinstance(stmt, exp.Command) and stmt.name.upper() in _PREPARED_COMMANDS:
        raise SqlGuardError("prepared_statement", "预编译语句的正文绕过静态分析，不予执行")
    raise SqlGuardError("top_level_not_select", f"只允许 SELECT，当前是 {type(stmt).__name__}")


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
            out.append(Violation("pg_file_access", f"禁止访问服务器文件/时钟的函数 {name}()"))
        elif name in _SLEEP_FUNCTIONS:
            out.append(Violation("sleep_function", f"禁止时间函数 {name}()，它让语句超时形同虚设"))
        else:
            out.append(Violation("danger_function", f"白名单外的函数 {name}()，无法判定其副作用"))
    return out


def _shape_violations(stmt: exp.Expression) -> list[Violation]:
    """顶层是 SELECT 不代表整棵树只读：CTE 里能藏 DML，SELECT 后能挂 INTO。"""
    out = [
        Violation("dml_in_cte", f"子查询/CTE 里禁止 {type(dml).__name__}")
        for dml in stmt.find_all(exp.Insert, exp.Update, exp.Delete, exp.Merge)
    ]
    if stmt.find(exp.Into) is not None:
        out.append(Violation("into_outfile", "禁止 SELECT INTO 建表"))
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
            "variable_access",
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
                    "table_not_allowed",
                    f"系统字典表 {ref.catalog}.{ref.db}.{ref.name} 禁止访问（即使已授权）",
                )
            )
        elif ref not in allowed:
            out.append(
                Violation(
                    "table_not_allowed",
                    f"表 {ref.catalog}.{ref.db}.{ref.name} 不在白名单内"
                    + ("" if default_schema else "（未提供 default_schema，短表名无法补全）"),
                )
            )
    return out


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
    ]
    if violations:
        return GuardResult(ok=False, sql_final=None, tables=refs, violations=tuple(violations))

    if stmt.args.get("limit") is None:
        # 不包一层子查询：MySQL 5.7 优化器会丢掉子查询里的 ORDER BY，直接加在最外层更稳
        stmt = stmt.limit(max_rows + 1)

    return GuardResult(
        ok=True,
        sql_final=stmt.sql(dialect=dialect, comments=False),
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
