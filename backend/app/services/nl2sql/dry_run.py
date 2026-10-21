"""第二层：引擎 `EXPLAIN` dry-run（工单 037，safety §3 整块第一次有实物）。

为什么单独成模块而不塞进 `executor.py`：「分层独立有效」（roadmap §P5 验收 4 的立论）要求第二层
能被**单独关掉**、单独反证。塞进第三层那段会话代码里，"关掉第二层"这种用例就写不出来——
它只能断"没发语句"，而不能断"发的是第三层、没发的是第二层"。

接缝口径：
- 只跑在 `sql_final` 上（§3.1 第一条）。036 那条等价断言钉的是"守卫看到的 = 执行的那条"，
  第二层必须钉同一件东西，否则两层各验各的。
- 纯文本与真发语句分开：`explain_statements` / `explain_verdict` 不连库，`run_dry_run` 才碰连接。
  形状与裁决离线都能钉死，PG 那一支尤其需要这一点——它今天**发不出去**。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.services.datasource_service import root_cause
from app.services.sql_guard import GuardRuleId, SqlGuardError


def explain_statements(dialect: str, sql_final: str) -> tuple[str, ...]:
    """返回某方言的 dry-run 语句序列（原文顺序，调用方依次在同一条连接上发）。

    MySQL 一支只有一条：5.7 的 `EXPLAIN <select>` 自己就不执行，不需要包事务。
    PG 一支是四条（`BEGIN READ ONLY` / `SET LOCAL` / `EXPLAIN` / `ROLLBACK`）——
    **今天只有形状**：执行路径对 PG 直接 501，这四条一条都没发过，行为用例等 PG 只读执行通路。
    """
    if dialect == "mysql":
        return (f"EXPLAIN {sql_final}",)
    if dialect == "postgres":
        # 那个 `'3000ms'` 是 §3.1.1 的字面，刻意不跟 `query.timeout_ms` 走：第三层那条才是
        # 给真查询用的预算，dry-run 只估行不取数。今天没有真 PG 证据能说明这个数合适。
        return (
            "BEGIN READ ONLY",
            "SET LOCAL statement_timeout='3000ms'",
            f"EXPLAIN {sql_final}",
            "ROLLBACK",
        )
    raise ValueError(f"dialect={dialect} 的 dry-run 语句尚未登记")


def _self_proves_no_scan(line: Mapping[str, Any]) -> bool:
    """引擎有没有在这一行里**自证它不扫表**（实测原文 `Select tables optimized away`）。

    认这个串而不是认"`rows` 是 `NULL`"是整条口径的要害：同一格 `NULL` 有两种意思——
    引擎算不出来，和引擎证明了不需要算。`SELECT count(*) FROM t` 那一发是后者
    （`FORMAT=JSON` 里连一个 cost/rows 键都没有），按字面拒它等于让第二层专门拦最便宜的那种计划。
    """
    return "optimized away" in str(line.get("Extra") or "")


def explain_verdict(lines: Sequence[Mapping[str, Any]], *, max_explain_rows: int) -> None:
    """按 `EXPLAIN` 的逐行输出裁决第二层：放行则返回，拒则抛 `SqlGuardError`（与 `check()` 同形）。

    尺子是**逐行最大值**（"单次扫描的最大估行"），不是乘积也不是求和：`UNION` 的两臂是并列的
    两次扫描，实测 `[30226, 87912, None]` 相乘得 2.66e9、相加得 118138，
    都会把一条合法 UNION 判成 `too_costly`（safety §3.2 那张表）。
    它是**代价闸不是正确性闸**——估行是引擎的估算，不是真行数。

    四档口径（§3.2；工单 037 格子 ③ 的细化）：
    1. 读得出数就按数裁：最大值 `>` 阈值 → `too_costly`（严格大于，等号放行）。
       混合形状（一部分有数一部分 `NULL`）**按有数那些行裁**——已经读出来那一格能证大小。
    2. 一行都读不出来时，**每一行**都自证 `optimized away` → 放行（引擎证得了它小）。
    3. 读不出来又没有自证 → `dry_run_unreadable`。§1.2 ⑦ 那条"证不了它小就按最严的一档"
       在这一档成立。
    4. 零行归第 3 档而不是第 2 档：`all()` 在空序列上是**空洞为真**，照 §3.2 字面实现会读成
       "引擎一个字都没说 → 放行"。这一处是对文档字面的补细，记在工单 037 交付记录。
    """
    # `int()` 是输入侧的归一，不是防御性兜底：这一格的输入来自驱动 `result.mappings()`，
    # 实测（`probe_explain037.txt`）回的是 `int`，但比较 `peak > max_explain_rows` 落在 `str` 上
    # 会抛 `TypeError`——那是 500，不是一个裁决。str 那一支今天没有用例（真库里取不到那个形状），
    # 这一格因此记在 safety §9 的诚实清单里，不写成"真库证过"。
    readable = [int(line["rows"]) for line in lines if line.get("rows") is not None]
    if readable:
        peak = max(readable)
        if peak > max_explain_rows:
            raise SqlGuardError(
                GuardRuleId.too_costly,
                f"引擎预估单次扫描 {peak} 行，超过上限 {max_explain_rows}"
                "（AIWEB_QUERY__MAX_EXPLAIN_ROWS）",
            )
        return
    if lines and all(_self_proves_no_scan(line) for line in lines):
        return
    raise SqlGuardError(
        GuardRuleId.dry_run_unreadable,
        f"EXPLAIN 没有给出可用估行（{len(lines)} 行全读不出来，引擎也未自证免扫），无法判定代价",
    )


async def run_dry_run(
    conn: AsyncConnection,
    *,
    dialect: str,
    sql_final: str,
    max_explain_rows: int,
) -> None:
    """在**已经建好的只读会话**上发 dry-run 并裁决；放行则返回，拒则抛 `SqlGuardError`。

    调用方是 `executor.execute_readonly`，位置在第三层那四条 SET 之后、发真 SQL 取行之前
    （§3.1）。连接由调用方给：这一层不建连接、不关连接，它只是那条会话上多出来的一次往返。

    `EXPLAIN` 语句**自己**被源库拒 → `dry_run_engine_rejected`，正文带**源库原话**（§3.2 第 3 档）：
    错误号与那句话两半都在。三支实测在探针口径（`probe_explain037.txt`）——
    `1054 Unknown column`／`1146 Table doesn't exist`／`1221 Incorrect usage of UNION and LIMIT`，
    但**走我们这一层的真链路上今天只签了 `1054` 那一支**（live 一条 + 假连接一条，
    safety §3.6"用例的分配"那格），另两支只有探针读数。
    038 要把源库原话回喂模型，所以这里不转述、不美化。

    源库的超时错误今天也落这一档：`EXPLAIN` 不执行语句、扫不到数据，语句超时对它在实机上取不到
    触发条件（第三层那条 `MAX_EXECUTION_TIME` 只作用于真正执行的只读 SELECT）。真出现了，正文里
    带着引擎原话，用户读得到——这一格的选择记在工单 037 交付记录，不是漏掉的分支。

    **实物比"只有 `EXPLAIN` 会被拒"宽一格**：那个 `except` 在逐条发语句的循环里、不判失败的是
    哪一条。MySQL 一支今天只有一条语句，所以看不出来；PG 那支的 `BEGIN READ ONLY` / `SET LOCAL`
    若失败，正文同样会写成"源库在执行 EXPLAIN 时就拒了这条 SQL"，而且抛异常即退出、`ROLLBACK`
    发不出去。两支今天都打不到（执行路径对 PG 直接 501，第二层在只读会话建好之后才跑），
    所以没有为取不到的形状先写分支——这一格记在 safety §9，PG 执行通路落地那天连用例一起补。
    """
    lines: Sequence[Mapping[str, Any]] = ()
    for statement in explain_statements(dialect, sql_final):
        try:
            result = await conn.execute(text(statement))
        except DBAPIError as exc:
            cause = root_cause(exc)
            # 源库原话是「错误号 + 那句话」两半，只留后半句就丢了最有用的那一半：038 回喂模型时，
            # 模型按 `1054` 这种号改写法是确定的，按人话只能猜。号取不到（`args` 不足两元）
            # 才退回整串。
            detail = f"{cause.args[0]}: {cause.args[1]}" if len(cause.args) >= 2 else str(cause)
            raise SqlGuardError(
                GuardRuleId.dry_run_engine_rejected,
                f"源库在执行 EXPLAIN 时就拒了这条 SQL：{detail}",
                statement[:80],
            ) from exc
        if statement.startswith("EXPLAIN"):
            # 拷成普通 dict：`explain_verdict` 的输入是"行 → 映射"这个协议，不是 SQLAlchemy 的
            # `RowMapping` 类。绑上后者，离线用例就得靠真结果对象才能造数据。
            lines = tuple(dict(line) for line in result.mappings())
    explain_verdict(lines, max_explain_rows=max_explain_rows)
