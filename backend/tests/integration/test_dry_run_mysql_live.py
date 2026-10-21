"""第二层在**真 MySQL** 上的那半边（工单 037 验收 1/2/3 的 live 部分）。

桩件（`tests/unit/test_dry_run_wiring.py`）证的是"这一层跑在花钱之前"与"关掉时一条 EXPLAIN
都不发"——那两句话在真库上**没有观察手段**（只能看见结果不对，看不见那一步有没有发生）。
这里补的是桩件给不了的另外三件事，一件都不能少：

1. 源库的**原话**长什么样。桩件里的 `Unknown column 'nope' in 'field list'` 是抄自实测文件的，
   真库今天仍然这么写，038 回喂模型吃的就是这句话；驱动/MySQL 升级改了措辞，这里先红。
2. 引擎的估行**真读得回来**，而且默认阈值下不会把好 SQL 拦掉。桩件的计划行是我造的，
   造得太宽松（比如全给 `[{"rows": 1}]`）就永远证不到"真实形状能过"这一格。
3. `rows` 全 `NULL` 那一档在真库上确实存在，而且它**放行**——工单格子 ③ 被实测顶回来之后，
   细则是靠"这一行自证 `optimized away`"成立的，只有真引擎会给这一行。

期望值口径：估行数值抄自 `.scratch/p5-sql-guard-hardening/evidence/probe_explain037.txt`
与 `probe_explain_null037.txt`（`order_main` 全表扫描 `30226`），表名列名抄自
`backend/scripts/init_demo_mysql.sql`；连接与口令同 011 的 live 用例（`.setup/aiweb_ro.cnf`）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.exc import DBAPIError

from app.models.datasource import DataSource
from app.services.nl2sql.executor import ExecutionResult, execute_readonly
from app.services.sql_guard import GuardRuleId, QualifiedTable, SqlGuardError, check
from app.settings import get_settings
from tests.integration.conftest import DbAccount

pytestmark = pytest.mark.live

DEMO_DB = "ai_web_demo"
# 演示库里这两张表都在白名单形状内（第一层只认表不认列，所以拼错的列名能走到第二层）
ALLOWED = frozenset(
    {QualifiedTable("", DEMO_DB, "order_main"), QualifiedTable("", DEMO_DB, "customer")}
)
# 默认阈值（safety §3.2 与 `settings.py` 的 50_000_000），这里显式钉住而不是"跟着默认值走"：
# 用例要断的是"这一档阈值下放行"，默认值哪天被改，这条要红在本用例上而不是红在一片 live 上。
_DEFAULT_MAX_ROWS = 50_000_000


@pytest.fixture(autouse=True)
def second_layer_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """这一族默认跑在"第二层开着 + 阈值取文档那个数"上，且**不跟着开发者机器的 .env 走**。

    走真实配置通道（环境变量 + 清缓存），理由同 `tests/unit/test_dry_run_wiring.py` 那条夹具：
    执行器里的读取点是 `get_settings().query`，给模块属性打桩盖不住它。
    """
    monkeypatch.setenv("AIWEB_QUERY__DRY_RUN", "true")
    monkeypatch.setenv("AIWEB_QUERY__MAX_EXPLAIN_ROWS", str(_DEFAULT_MAX_ROWS))
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def _row(account: DbAccount) -> DataSource:
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
        timeout_ms=15000,
    )


def _guard(sql: str, *, row_limit: int = 100) -> str:
    """过第一层拿 `sql_final`——第二层只跑在它上面（工单"已定口径"第一条）。

    `check()` 成功时才有 `sql_final`，类型上是 `str | None`。这里就地断言而不是把 `None` 往下传：
    传下去用例测的就成了"第二层拿到 `None` 怎么办"，那不是这一族要证的事。
    """
    out = check(sql, allowed=ALLOWED, dialect="mysql", default_schema=DEMO_DB, max_rows=row_limit)
    assert out.sql_final is not None
    return out.sql_final


async def _exec(
    account: DbAccount, sql_final: str, tmp_path: Path, *, row_limit: int = 100
) -> ExecutionResult:
    return await execute_readonly(
        _row(account),
        password=account.password,
        sql_final=sql_final,
        row_limit=row_limit,
        max_cell_chars=1000,
        result_dir=tmp_path,
    )


async def test_拼错的列名被第二层拒_正文里是源库原话(account: DbAccount, tmp_path: Path) -> None:
    """验收 1 的 live 半边：这一支**过了第一层**（表在白名单里、形状合法）、死在第二层。

    断的是三件事，缺一件就不算这一档的证据：
    ① 归因是 `dry_run_engine_rejected`（不是 `unsupported_construct`——那是 sqlglot 的解析失败，
       也不是我们的转述）；② 正文里有源库原话，**含错误号**（`1054` + `Unknown column`——038 要
       回喂的就是这一整串，模型按号改写法是确定的，按人话只能猜）；
    ③ 抛的是 `SqlGuardError` 而不是 `DBAPIError`——第二层把引擎的拒绝**翻译成守卫的词表**，
       HTTP 层才拿得到 400 而不是 500（safety §1.1.1 那条"我们的 bug 不许报成用户的 SQL 违规"
       的反面：用户的 SQL 违规也不许报成我们的错误）。
    """
    sql_final = _guard("select nope_from_customer from ai_web_demo.customer limit 5")
    with pytest.raises(SqlGuardError) as caught:
        await _exec(account, sql_final, tmp_path)
    assert caught.value.rule_id == GuardRuleId.dry_run_engine_rejected
    text = str(caught.value)
    assert "1054" in text and "Unknown column" in text, text


async def test_好_sql_在默认阈值下放行_真估行读得回来(account: DbAccount, tmp_path: Path) -> None:
    """默认开着这一层时，最常见的那类问句必须照跑——"分层独立有效"的反面是它把合法查询拦死。

    `order_main` 全表扫描的真估行是 30226（实测文件），远小于默认阈值，所以放行。
    这条同时证到 `run_dry_run` 在真连接上读得回 `rows`：读不回来会落 `dry_run_unreadable`，
    那是**另一种**失败，不会悄悄绿。
    """
    res = await _exec(
        account, _guard("select id, amount from ai_web_demo.order_main limit 3"), tmp_path
    )
    assert res.row_count == 3
    assert res.columns == ["id", "amount"]


async def test_引擎给_全NULL_但自证免扫时放行_这一档只有真库给得出(
    account: DbAccount, tmp_path: Path
) -> None:
    """工单格子 ③ 被实测顶回来之后定下的那一格：**真库**上今天仍然长这样。

    `select count(*)` 的 `EXPLAIN` 只有一行、`rows` 是 `NULL`，靠 `Extra`
    = `Select tables optimized away` 自证它不扫表。按工单原话"读不出来就拒"实现的话，
    第二层专门拦的就是"有多少订单"这类最常见问句，而它默认开、prod 锁死不能关。
    这一条是那一格在真链路上的存在性证明——桩件里的计划行是我造的，造不出"引擎真的这么说"。
    """
    sql_final = _guard("select count(*) as c from ai_web_demo.order_main")
    res = await _exec(account, sql_final, tmp_path)
    assert res.rows[0][0] == 30000, res.rows


async def test_阈值钉到_100_时同一条真sql翻成too_costly(
    account: DbAccount, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验收 3 的 live 半边：`max_explain_rows` 真被读，而且读的是**引擎给的数**。

    桩件那条用的是我造的 30226；这里同一个数由真引擎当场给出——`probe_explain037.txt` 里
    `select id from ai_web_demo.order_main limit 6` 那一发的 `rows` 实测就是 **30226**（`type=index`
    的全表索引扫描），这条用例发的是同表同形状的一句。阈值 `100` < 30226 → `too_costly`，
    正文里要同时出现这两个数，否则人不知道自己撞了哪一格。

    **要说清的口径**：工单验收 3 字面是"同一条 SQL 翻面"。这里翻面的是**同一张表的同一种计划**
    （放行那条用的是 `id, amount … limit 3`，与本条那句不是同一串字面），逐字"同一条 sql_final
    在两个阈值下放行/被拒"由桩件那条签
    （`tests/unit/test_dry_run_wiring.py::test_max_explain_rows_被真读_同一条_sql_从放行翻成拒`）。
    """
    monkeypatch.setenv("AIWEB_QUERY__MAX_EXPLAIN_ROWS", "100")
    get_settings.cache_clear()
    sql_final = _guard("select id from ai_web_demo.order_main limit 5")
    with pytest.raises(SqlGuardError) as caught:
        await _exec(account, sql_final, tmp_path)
    assert caught.value.rule_id == GuardRuleId.too_costly
    message = str(caught.value)
    assert "100" in message and "30226" in message, message


async def test_dry_run_关掉时第二层不在链路上_坏sql一路走到真执行被引擎拒(
    account: DbAccount, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验收 2 的 live 半边（**只能证到其中一半，另一半归桩件**）。

    关掉这一层之后，同一条 `sql_final`（上面那条拼错列名的）不再被 `SqlGuardError` 拒，
    而是走到真执行、由源库报 `DBAPIError`——这是"第二层整层从链路上消失"的正证：
    它要是还在，翻出来的就是我们那枚码而不是驱动异常。第三层那四条 SET 一个字不动，
    所以写语句仍会被引擎拒（011 验收 ① 那条），两层的独立性就落在这里。

    "源库一条 `EXPLAIN` 都没收到"那半句**在这里证不了**：真库没有"这条连接收到过哪些语句"
    的可观察面（`performance_schema` 要额外权限且是全局聚合，不是证据）。那一半住
    `tests/unit/test_dry_run_wiring.py::test_dry_run_关掉时源库一条_explain_都没收到而真_sql_照跑`，
    两边不许互相冒充。
    """
    monkeypatch.setenv("AIWEB_QUERY__DRY_RUN", "false")
    get_settings.cache_clear()
    sql_final = _guard("select nope_from_customer from ai_web_demo.customer limit 5")
    with pytest.raises(DBAPIError) as caught:
        await _exec(account, sql_final, tmp_path)
    errno = caught.value.orig.args[0]  # type: ignore[union-attr]
    assert errno == 1054, f"不是源库报的 Unknown column，errno={errno}"


async def test_关掉第二层时好sql结果与开着一致(
    account: DbAccount, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验收 2 的后半句"真 SQL 照跑、结果不变"：同一条语句在开关两侧给出同样的行。

    这一条单独存在而不是并进上一条：上一条证的是"消失的是第二层"，这一条证的是
    "留下来的两层没被顺手改动"。两次跑之间只动那一个键。
    """
    sql_final = _guard("select id, amount from ai_web_demo.order_main limit 3")
    on = await _exec(account, sql_final, tmp_path)
    monkeypatch.setenv("AIWEB_QUERY__DRY_RUN", "false")
    get_settings.cache_clear()
    off = await _exec(account, sql_final, tmp_path)
    assert off.rows == on.rows and off.columns == on.columns
    assert off.row_count == on.row_count == 3
