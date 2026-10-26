"""三层独立性的 **live** 那半边（工单 040 涉及层第 2 格；roadmap §P5 验收 2/3/4 的真跑靶）。

与闸内那半边的分工要说死：`tests/unit/test_layer_independence.py` 证的是**代码之间没有共享
状态**（三层各自吃什么、吐什么，一个字节都不串），本文件证的是**源库真的按这一层的位置在挡**。
两边不许互相冒充——闸内那十一发绿的不代表源库拦下了什么，本文件这三发绿的也不代表代码不耦合。

为什么必须 live（验收 2 的字面）：`SELECT * FROM order_item` 不带 `LIMIT` 时，那个 `+1` 的
**两半**分别只有真库能答——守卫补的那一格进不进 SQL 由 AST 决定（闸内已证），而"第 1001 行
真的存在、且它是探针"由源库的行数决定。order_item 有 8.8 万行（`scripts/init_demo_mysql.sql:226`
的注释就是它自己的建档理由），所以 `row_limit=1000` 那一发必然撞上上限。

关于第二层（工单 037）：本文件整体把 `query.dry_run` 关着，理由与 `test_execute_live.py` 同一套
——③ 那一条问的是"第一层不在场时**谁**挡住了 `SLEEP(20)`"，第二层在场会多一个候选答案，
而那一格已由 `test_dry_run_mysql_live.py` 签过。两档不合并。

③ 的期望值抄自 `.scratch/p5-sql-guard-hardening/evidence/sleep_timeout_probe040.txt`（P1/P2/P4/P3r
四发，2026-10-26 09:37~09:38，直接在 mysql CLI 上跑、不经过本仓库任何代码）：**不是**"实现现在
怎么写就怎么断"，也**不是**为了验收 4 变绿挑的形状。工单 040 明令"③ 的两种实测结果都是交付"，
而实测结果是：清空第一层整条函数规则之后，这条 SQL 既不抬 `SqlGuardError`、也不抬
`QueryTimeout`——它成功返回一行（值为 1 = 睡被叫停），耗时从 20s 量级降到 1s 量级。
**严禁**把它换成 P3r 那一发"每一行都慢"的形状（`select sleep(1) from order_item limit 5`，
那条会抬 3024 并翻成 `QueryTimeout`）：3024 这一档继续由 011 的既有用例
`test_execute_live.py:164` 签，两件事各记各的。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.models.datasource import DataSource
from app.services import sql_guard
from app.services.nl2sql.executor import execute_readonly
from app.services.sql_guard import GuardRuleId, QualifiedTable, SqlGuardError, check
from app.settings import get_settings
from tests.integration.conftest import DbAccount

pytestmark = pytest.mark.live

DEMO_DB = "ai_web_demo"
# order_item 8.8 万行（探针那一发要必然撞上上限）、customer 只有 600 行（不参与）
ALLOWED = frozenset({QualifiedTable("", DEMO_DB, "order_item")})


@pytest.fixture(autouse=True)
def second_layer_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """整族跑在"第二层关着"上——本文件的靶子是第一层与第三层之间有没有耦合。

    走真实配置通道（环境变量 + 清缓存），理由同 `test_execute_live.py` 的同名夹具：执行器里的
    读取点是 `get_settings().query`，给模块属性打桩盖不住它。
    **这一份与 `test_execute_live.py:40` 那一份是逐字相同的第二份**，没上提到 `conftest.py`：
    上提要动 011 那张底的文件，而工单 040 的涉及层只点到本文件（同 `_row`/`_guard` 各文件自抄
    的既定风格，仓内已有三份）。真正的收拢归 041 那一族"顺手清账"，不在本片偷做。
    """
    monkeypatch.setenv("AIWEB_QUERY__DRY_RUN", "false")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def _row(account: DbAccount, *, timeout_ms: int = 15000) -> DataSource:
    """内存里拼一个指向演示库的 DataSource（不落元数据库，execute 只读它的连接字段）。"""
    _user, _pw, host, port = account
    return DataSource(
        id=1,
        name="demo-live",
        kind="mysql",
        host=host,
        port=port,
        catalog_name="",
        connect_user=_user,
        secret_enc=b"",
        row_limit=1000,
        timeout_ms=timeout_ms,
    )


def _guard(sql: str, *, row_limit: int = 1000) -> str:
    """过一遍第一层拿 `sql_final`——钳位那一格就是在这一路里加上去的。

    就地断 `sql_final is not None` 而不是把 `None` 往下传（同 `test_dry_run_mysql_live.py:80-85`
    那条纪律）：传下去的话用例测成的就是"执行器拿到 `None` 怎么办"，那不是这一族要证的事。
    """
    out = check(
        sql,
        allowed=ALLOWED,
        dialect="mysql",
        default_schema=DEMO_DB,
        max_rows=row_limit,
    )
    assert out.sql_final is not None
    return out.sql_final


async def test_不带_limit_的宽表查询_加_one_由第一层做_丢探针由第三层做(
    account: DbAccount, tmp_path: Path
) -> None:
    """验收 2：四件事要在同一条链上同时成立，缺一件就说明那个 `+1` 有两个主人。

    ① `sql_final` 里是 `LIMIT 1001`（第一层补的，字面 ① 那"一个都不带"到这儿才变成数字）；
    ② `row_count` 是 1000（第三层把多出来的那一行扔了，没把真数据少切一格）；
    ③ `truncated is True`（探针行存在的唯一意义就是当证据）；
    ④ csv 里是 1 行表头 + 1000 行数据（落盘的是完整上限行，不是预览那一小截）。
    闸内那半边（`test_钳位在第一层_丢探针行与_truncated_判定在第三层`）喂的是手搓的 1001 行，
    证的是"两处数值必须同源"；这里喂的是**真 8.8 万行**，证的是"源库确实会回第 1001 行"。
    """
    sql_final = _guard("select * from ai_web_demo.order_item")
    assert sql_final.upper().endswith("LIMIT 1001"), sql_final

    res = await execute_readonly(
        _row(account),
        password=account.password,
        sql_final=sql_final,
        row_limit=1000,
        max_cell_chars=1000,
        result_dir=tmp_path,
    )
    assert res.row_count == 1000
    assert res.truncated is True
    # order_item 七列全数值/日期，单元格不会带换行，所以按行数得按物理行数
    lines = Path(res.result_file).read_text(encoding="utf-8-sig").splitlines()
    assert len(lines) == 1001, "csv 必须是表头 + 完整上限行"
    assert lines[0] == "id,order_id,product_id,qty,unit_price,is_gift,created_at"


def test_同一条_sleep_默认被第一层拒_归因是_sleep_function() -> None:
    """验收 3 的**前半**（归因互斥的第一支）：这一发连源库都不该碰到。

    它不连库，住在这个文件里只有一个理由：它必须与 ③ **成对读**才叫"互斥且可区分"——
    同一条 `SELECT SLEEP(20)`，第一层在场时得到的是**我们自己的** `SqlGuardError`，
    带着 `rule_id=sleep_function`；第一层不在场时得到的是**源库的**行为（见 ③）。
    两支的归因没有任何一种情况会重合：一支根本没有源库参与，另一支没有守卫参与。
    """
    with pytest.raises(SqlGuardError) as caught:
        _guard("select sleep(20)")
    assert caught.value.rule_id is GuardRuleId.sleep_function


async def test_摘掉第一层整条函数规则之后_同一条_sleep_由计时器叫停而不是由错误中断(
    account: DbAccount, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验收 3 后半 + 验收 4：**清空第一层整条函数规则**之后，谁在挡这条 SQL？

    为什么清的是整条 `_function_violations` 而不是 `_SLEEP_FUNCTIONS`：实测清空后者之后
    `sleep()` **照样被第一层拒**，只是归因换成 `danger_function`（`_function_violations` 的
    else 分支拒绝一切 `exp.Anonymous`，那才是正向白名单那把刀）。工单 040 与 safety §8.4
    原来那句"清空黑名单 → 语句走到源库"的前提是**错的**，闸内那条
    `test_清空黑名单只换归因不换结论_正向白名单才是那把刀` 把这件事永久钉着。

    实测结果（三档都断，缺一档就说明这一格在挑好看的答案）：
    - **不抬 `SqlGuardError`**：守卫整条函数规则不在场，SQL 出得了第一层；
    - **不抬 `QueryTimeout`**：MySQL 5.7 在单行语句上叫停计时器却**不报错**（P1/P4 的 rc=0），
      `classify_source_error` 无从分档——"被 `MAX_EXECUTION_TIME` 截断"发生在**计时器**上，
      不发生在**错误归因**上；
    - **成功一行、值为 1**：`SLEEP()` 的返回值就是"睡眠是否被中断"的标志（0 睡满 / 1 被叫醒），
      这一格是源库自己声明"叫停发生了"；耗时同步断言远小于 20s。
    """
    monkeypatch.setattr(sql_guard, "_function_violations", lambda stmt: [])
    sql_final = _guard("select sleep(20)")
    assert sql_final.upper().endswith("LIMIT 1001"), sql_final

    started = time.perf_counter()
    res = await execute_readonly(
        _row(account, timeout_ms=1000),
        password=account.password,
        sql_final=sql_final,
        row_limit=1000,
        max_cell_chars=1000,
        result_dir=tmp_path,
    )
    elapsed = time.perf_counter() - started

    assert res.row_count == 1
    assert res.truncated is False
    assert res.rows[0] == [1], "SLEEP 的返回标志：1 = 被 MAX_EXECUTION_TIME 叫停，0 = 睡满 20 秒"
    assert elapsed < 10.0, f"耗时 {elapsed:.1f}s：计时器没叫停的话这里是 20s 量级"
