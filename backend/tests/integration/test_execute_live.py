"""真连演示库跑只读执行：工单 011 验收 ①③④ 的 live 半边。

为什么必须 live：验收 ①（引擎拒写 ≠ 应用拒写）、会话级只读是否真的生效、超时是否被源库自杀——
这些都是**源库**的行为，桩不出来。离线那半边（序列化/截断/路径/守卫交互/SET 序列）已经在
`tests/unit/test_executor_*.py` 钉死，这里只补"真 MySQL 才答得出"的问题。

期望值口径：
- 演示库表名/列名/行数抄自 `backend/scripts/init_demo_mysql.sql`
  （order_main 30000 行、amount DECIMAL、created_at DATETIME）
- 会话只读用的账号是只读的 `aiweb_ro`（工单 002），口令从 gitignored 的 `.setup/aiweb_ro.cnf` 读
- 结果落盘目录用 pytest 的 tmp_path，绝不写进仓库的 `data/results/`
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.exc import DBAPIError

from app.core.errors import QueryTimeout
from app.models.datasource import DataSource
from app.services.nl2sql.executor import execute_readonly
from app.services.sql_guard import QualifiedTable, SqlGuardError, check
from tests.integration.conftest import DbAccount

pytestmark = pytest.mark.live

DEMO_DB = "ai_web_demo"
ALLOWED = frozenset({QualifiedTable("", DEMO_DB, "order_main")})


def _row(account: DbAccount, *, timeout_ms: int = 15000) -> DataSource:
    """在内存里拼一个指向演示库的 DataSource（不落元数据库，execute 只读它的连接字段）。"""
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


def _guard(sql: str, *, row_limit: int) -> str:
    """过一遍守卫拿到 sql_final（守卫在这里注入 LIMIT row_limit+1）。"""
    return check(
        sql, allowed=ALLOWED, dialect="mysql", default_schema=DEMO_DB, max_rows=row_limit
    ).sql_final


async def test_只读查询跑通且_decimals_datetime_序列化落_csv(
    account: DbAccount, tmp_path: Path
) -> None:
    """验收 ④ + 落盘：DECIMAL 走字符串、DATETIME 走 ISO，csv 落在服务端拼的目录里。"""
    res = await execute_readonly(
        _row(account),
        password=account.password,
        sql_final=_guard(
            "select id, amount, created_at from ai_web_demo.order_main limit 3", row_limit=100
        ),
        row_limit=100,
        timeout_ms=15000,
        timeout_source="AIWEB_QUERY__TIMEOUT_MS",
        max_cell_chars=1000,
        result_dir=tmp_path,
    )
    assert res.columns == ["id", "amount", "created_at"]
    assert res.row_count == 3
    assert res.truncated is False
    # amount 是 DECIMAL(12,2) → 字符串且带小数；created_at DATETIME → ISO
    first = res.rows[0]
    assert isinstance(first[1], str) and first[1].count(".") == 1
    assert "T" in first[2]
    # csv 落在 tmp_path 内、文件名是服务端生成的 run_id
    assert Path(res.result_file).parent == tmp_path.resolve()
    assert Path(res.result_file).read_text(encoding="utf-8-sig").splitlines()[0] == (
        "id,amount,created_at"
    )


async def test_行数超上限_truncated_true_且_csv_是完整上限行(
    account: DbAccount, tmp_path: Path
) -> None:
    """验收 ③：order_main 有 3 万行，row_limit=5 时应截断、丢探针行、csv 里正好 5 行数据。"""
    res = await execute_readonly(
        _row(account),
        password=account.password,
        sql_final=_guard("select id from ai_web_demo.order_main", row_limit=5),
        row_limit=5,
        timeout_ms=15000,
        timeout_source="AIWEB_QUERY__TIMEOUT_MS",
        max_cell_chars=1000,
        result_dir=tmp_path,
    )
    assert res.truncated is True
    assert res.row_count == 5
    csv_lines = Path(res.result_file).read_text(encoding="utf-8-sig").splitlines()
    assert len(csv_lines) == 6  # 1 表头 + 5 数据（完整上限行，不是预览那一小截）


async def test_会话只读生效_引擎拒绝写_而这不是应用拒的(account: DbAccount, tmp_path: Path) -> None:
    """验收 ①：两层分开断言。

    应用层：守卫看到 UPDATE 直接 SqlGuardError（第一层 AST 白名单）。
    引擎层：把守卫当空气、硬把 UPDATE 塞进 execute_readonly，源库自己会拒绝它——
    报的是驱动原文的 DBAPIError（不是我们的任何 AppError），证明拦下来的是 MySQL 不是我们。
    """
    # 应用层拒
    with pytest.raises(SqlGuardError):
        check(
            "update ai_web_demo.order_main set amount = 1 where id = 1",
            allowed=ALLOWED,
            dialect="mysql",
            default_schema=DEMO_DB,
            max_rows=100,
        )
    # 引擎层拒（绕过守卫直接把写语句交给执行器）。
    # 钉的是源库自己报的拒绝码：1792=会话 READ ONLY 挡住、1142=账号无写权限挡住。
    # 刻意不收 1046（No database selected）——那说明是连接没选库的配置错，不是"写被拦"，
    # 混进来的话这条验收就假过了。
    with pytest.raises(DBAPIError) as caught:
        await execute_readonly(
            _row(account),
            password=account.password,
            sql_final="update ai_web_demo.order_main set amount = 1 where id = 1",
            row_limit=100,
            timeout_ms=15000,
            timeout_source="AIWEB_QUERY__TIMEOUT_MS",
            max_cell_chars=1000,
            result_dir=tmp_path,
        )
    errno = caught.value.orig.args[0]  # type: ignore[union-attr]
    assert errno in (1792, 1142), f"不是引擎级写拒绝，errno={errno}"


async def test_慢查询被超时真中断_错误点名配置来源(account: DbAccount, tmp_path: Path) -> None:
    """验收 ②：`SET SESSION MAX_EXECUTION_TIME` 到点由源库自杀，翻成 QueryTimeout 并带键名。

    这里绕过守卫直接给执行器喂 SQL（守卫本来就禁 sleep；这条测执行层超时中断，不是守卫拦不拦）。
    用 `sleep(1)` **每行**都睡 1 秒而不是 `select sleep(2)` 一次：
    MySQL 的 MAX_EXECUTION_TIME 只在**行边界**检查——`count(*)` 从头到尾只吐一行，中间从不看时钟；
    单条 `select sleep(2)` 也只有一行，同样查不到。所以要让**每一行都慢**，第一条完成就超预算，
    行边界检查当场命中 300ms 阈值，报 3024（本坑第一次踩就交过学费，注释钉在这里免得再翻车）。
    """
    with pytest.raises(QueryTimeout) as caught:
        await execute_readonly(
            _row(account, timeout_ms=300),
            password=account.password,
            sql_final="select sleep(1) from ai_web_demo.order_item limit 5",
            row_limit=100,
            timeout_ms=300,
            timeout_source="data_sources.timeout_ms",
            max_cell_chars=1000,
            result_dir=tmp_path,
        )
    # detail 必须点名"上限来自哪一格"，这是验收 ② 的全部意义
    assert "data_sources.timeout_ms" in str(caught.value.detail)
    assert "300" in str(caught.value.detail)
