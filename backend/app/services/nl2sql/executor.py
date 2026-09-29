"""源库只读执行切片（工单 011）：会话级只读 + 超时 + 行数截断探测 + 结果落 data/results/*.csv。

接缝口径见 docs/nl2sql-safety.md §4、docs/architecture.md §4.1 步骤 ⑦，工单里的「开工前拍板」。
本模块把"连上之后查什么、查到怎么落地"这条窄路径穿起来；"怎么连"复用 source_manager。
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from app.core.errors import (
    NotImplementedSource,
    QueryTimeout,
    ReadonlyCapabilityMissing,
)
from app.core.logging import get_logger
from app.models.datasource import DataSource
from app.services.datasource_service import grants_verdict, root_cause
from app.services.source_manager import create_source_engine

_logger = get_logger(__name__)

# §4.1：会话建好后在建 SQL 之前依次跑的 SET 序列，MySQL 与 PG 两套。
# 抽成纯函数是为了"顺序/单位"这两件事离线就能钉死；真中断与引擎拒写留给 live。


def session_statements(kind: str, *, timeout_ms: int) -> list[str]:
    """返回某 kind 的会话级只读/超时 SET 语句（原文顺序，调用方依次执行）。

    MySQL 的 READ ONLY 必须排在最前——§4.1 明写它"前不能在已有事务里"。
    MAX_EXECUTION_TIME 只作用于只读 SELECT，是这条链最可靠的超时路径；PG 用 statement_timeout。
    """
    if kind == "mysql":
        return [
            "SET SESSION TRANSACTION READ ONLY",
            f"SET SESSION MAX_EXECUTION_TIME = {timeout_ms}",
            # wait_timeout 单位是秒：向上取整再加 10 秒余量，别让会话比语句先被踢
            f"SET SESSION wait_timeout = {timeout_ms // 1000 + 10}",
            "SET SESSION autocommit = 1",
        ]
    if kind == "postgres":
        return [
            "SET default_transaction_read_only = on",
            f"SET statement_timeout = '{timeout_ms}ms'",
        ]
    raise ValueError(f"kind={kind} 的会话级只读尚未实现")


def _bytes_placeholder(value: bytes) -> str:
    """二进制列不能直接进 JSON，回一个可读、定长的占位。"""
    size = len(value)
    if size < 1024:
        return f"<binary {size}B>"
    return f"<binary {round(size / 1024, 1)}KB>"


def serialize_cell(value: Any, *, max_cell_chars: int) -> Any:
    """把驱动返回的一个单元格转成 JSON 安全值（safety §4.3 的单元格保护）。

    口径都来自文档而非"代码顺手怎么写"：Decimal 走字符串保精度、日期走 ISO、bytes 走占位、
    超长 str 按 `ResultGroup.max_cell_chars` 截断。整型/浮点/None 原样透传。
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _bytes_placeholder(bytes(value))
    if isinstance(value, str) and len(value) > max_cell_chars:
        return value[:max_cell_chars]
    return value


# 服务端生成的 run_id 的形状：只有字母数字，没有分隔符/点/空字节，因而拼不出目录穿越。
# 校验和生成共用这一个式子——两者不一致的话，生成的名字反而过不了自己的门。
_RUN_ID = re.compile(r"[A-Za-z0-9]+")


def new_run_id() -> str:
    """一条结果的身份，由服务端拼（UTC 时间戳 + 随机段），接口层拿不到任何入参能指定它。"""
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"{stamp}{uuid4().hex}"


def result_csv_path(result_dir: Path, run_id: str) -> Path:
    """把结果目录 + run_id 拼成一个必然落在目录内的 csv 路径。

    §7 的写入侧一半：run_id 只可能是我们生成过的那种形状，任何带 `..`/绝对路径/空字节/
    分隔符的输入都当场 ValueError，而不是悄悄写到家目录外面去。
    """
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError(f"非法的 result run_id：{run_id!r}")
    return Path(result_dir).resolve() / f"{run_id}.csv"


def split_truncated(rows: list[Any], *, row_limit: int) -> tuple[list[Any], bool]:
    """按 §4.3 的探针法判截断：取到 `row_limit+1` 行即"还有更多"，丢探针行、报 truncated。

    守卫注入的 LIMIT 已经是 `row_limit+1`，所以这里最多会拿到那么多；第 `row_limit+1` 行
    存在的唯一意义就是当探针，不进结果。
    """
    truncated = len(rows) > row_limit
    return (rows[:row_limit] if truncated else rows), truncated


def write_result_csv(path: Path, *, columns: list[str], rows: list[tuple[Any, ...]]) -> None:
    """把列名 + 全量行写成 csv（§4.1 步骤 ⑦ 的落盘，ADR-0004：只落文件不进元数据库）。

    编码用 utf-8-sig：演示库列注释与数据都是中文，裸 utf-8 在 Excel 里会乱码，
    而 BOM 是唯一让"下载下来直接能看"成立的做法。行里的值已由 serialize_cell 转好，
    None 写成空单元格。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for row in rows:
            writer.writerow(["" if c is None else c for c in row])


def enforce_readonly_grants(grant_lines: list[str]) -> None:
    """执行路径开头的只读硬阻断（工单 011 拍板）：判定复用 grants_verdict，不重造、不缓存。

    verdict 的 code 非 None → 抛 ReadonlyCapabilityMissing。登记时那条同样的 code 是
    "黄色警告回 200"，到了要跑 SQL 的这一层必须翻成拦——一个能写的账号进了执行链路，
    最外面的会话只读就可能被绕掉。
    """
    verdict = grants_verdict(grant_lines)
    if verdict["code"] is not None:
        raise ReadonlyCapabilityMissing("源账号不是只读账号，拒绝执行", detail=verdict["warnings"])


# 源库到点自杀的错误码：MySQL 是 3024（MAX_EXECUTION_TIME），PG 是 SQLSTATE 57014。
# 只有这两种翻成 QueryTimeout；其余错误原样抛，让调用方按 §4.1 分档处理。
_TIMEOUT_CODES = frozenset({3024, "57014"})


def classify_source_error(exc: Exception, *, timeout_ms: int, timeout_source: str) -> NoReturn:
    """源库错误的分档：超时翻成 QueryTimeout（detail 点名上限来自哪个配置项），其余原样抛。

    MySQL 的 `MAX_EXECUTION_TIME` 到点和 PG 的 `statement_timeout` 到点是两种错误码，
    用户自救都要改同一格（timeout_ms），所以归一档。非超时错误绝不吞——重抛原异常，
    让调用链按其它档（连通/语法/引擎拒写）处理。
    """
    cause = root_cause(exc)
    args = getattr(cause, "args", ())
    code = args[0] if args and len(args) >= 1 else None
    if code in _TIMEOUT_CODES:
        raise QueryTimeout(
            "源库执行超时被中断",
            detail=f"超时上限 {timeout_ms}ms，来自 {timeout_source}（改这一格或把问句缩小）",
        )
    raise exc


@dataclass(frozen=True)
class ExecutionResult:
    """一次只读执行的产物（§4.1 步骤 ⑦ 的返回，也是 012 接线时往接口层回的东西）。

    `rows` 是截断到 `row_limit` 后、每格过 `serialize_cell` 的 JSON 安全值；
    `result_file` 是服务端拼好的 csv 绝对路径，接口层拿不到它，只能拿 `run_id`（012 的事）。
    """

    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    row_count: int
    run_id: str
    result_file: str


# MySQL 5.7.8 以下没有 MAX_EXECUTION_TIME 会话变量，SET 时报 1193。
# §4.3 的 USE_SESSION_MAX_EXEC_TIME 拍板：无条件试 SET，失败降级靠驱动超时。
_UNSUPPORTED_SYSVAR = 1193


async def execute_readonly(
    row: DataSource,
    *,
    password: str,
    sql_final: str,
    row_limit: int,
    timeout_ms: int,
    timeout_source: str,
    max_cell_chars: int,
    result_dir: Path,
) -> ExecutionResult:
    """在源库上以只读会话跑一条守卫放行的 SQL，落 csv 并回截断后的行。

    口径都在 safety §4 / 工单「开工前拍板」里钉死，这里只把它们串起来：
    ① 建只读会话**之前**先 `SHOW GRANTS` 复用 `grants_verdict` 硬阻断（判定不缓存）；
    ② 依次执行 §4.1 的 SET 序列（READ ONLY 在前；MAX_EXECUTION_TIME 不支持则降级靠驱动超时）；
    ③ 一次 execute 缓冲取回——**这是 §4.1 "无缓冲 cursor + fetchmany 逐 1000" 的诚实降级**：
      守卫注入的 LIMIT 就是 `row_limit+1`，内存上限由那一行钉住，与是否流式无关；
      asyncmy 在流式游标下遇到服务端 3024 会先吐 2013（丢连接）而不是把 3024 传给我们，
      超时分类就废了。缓冲取回 3024 到得了客户端，截断行为一样。
    ④ split_truncated 丢探针行、serialize_cell 逐格转 JSON 安全值、write_result_csv 落盘。

    一次性 NullPool engine（source_manager 口径：011 不缓存连接池，缓存与并发闸同属 P3）。
    """
    if row.kind != "mysql":
        raise NotImplementedSource(f"kind={row.kind} 的只读执行尚未实现")

    engine = create_source_engine(row, password, timeout_ms=timeout_ms)
    try:
        async with engine.connect() as raw_conn:
            # AUTOCOMMIT：让每条 SET/SELECT 各自即时提交，不进 SQLAlchemy 的事务包裹。
            # 不这么做的后果是 `show grants` 先把事务开了，紧随其后的
            # `SET SESSION TRANSACTION READ ONLY` 会被 MySQL 拒（§4.1 的"前不能在已有事务里"）。
            conn = await raw_conn.execution_options(isolation_level="AUTOCOMMIT")
            grant_lines = list((await conn.execute(text("show grants"))).scalars())
            enforce_readonly_grants(grant_lines)
            for stmt in session_statements(row.kind, timeout_ms=timeout_ms):
                try:
                    await conn.execute(text(stmt))
                except DBAPIError as exc:
                    if _is_unsupported_max_execution_time(stmt, exc):
                        _logger.warning(
                            "源库不支持 MAX_EXECUTION_TIME，降级靠驱动侧连接超时", exc_info=exc
                        )
                        continue
                    classify_source_error(exc, timeout_ms=timeout_ms, timeout_source=timeout_source)

            result = await conn.execute(text(sql_final))
            columns = list(result.keys())
            fetched = list(result.fetchall())
            kept, truncated = split_truncated(fetched, row_limit=row_limit)
            rows = [
                [serialize_cell(cell, max_cell_chars=max_cell_chars) for cell in kept_row]
                for kept_row in kept
            ]
            run_id = new_run_id()
            path = result_csv_path(Path(result_dir), run_id)
            write_result_csv(path, columns=columns, rows=[tuple(r) for r in rows])
            return ExecutionResult(
                columns=columns,
                rows=rows,
                truncated=truncated,
                row_count=len(rows),
                run_id=run_id,
                result_file=str(path),
            )
    except SQLAlchemyError as exc:
        classify_source_error(exc, timeout_ms=timeout_ms, timeout_source=timeout_source)
    finally:
        await engine.dispose()


def _is_unsupported_max_execution_time(stmt: str, exc: DBAPIError) -> bool:
    """只在"设 MAX_EXECUTION_TIME 且报 1193（变量不存在）"时降级，其余错误照旧分档。"""
    if "MAX_EXECUTION_TIME" not in stmt.upper():
        return False
    cause = root_cause(exc)
    args: tuple[Any, ...] = getattr(cause, "args", ())
    return len(args) >= 1 and args[0] == _UNSUPPORTED_SYSVAR
