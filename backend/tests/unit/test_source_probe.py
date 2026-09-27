"""探测里的四个纯函数：表范围片段、只读结论、错误号翻译。

连接本身要真 MySQL（见 integration/test_datasource_live.py），但这四块逻辑不需要：
它们吃的是 SHOW GRANTS 的原文、驱动异常和源配置，可以按文档口径造样本。

期望值口径：
- `docs/nl2sql-safety.md` §4.1 + §6：账号必须真的只读，UI 检测 `SHOW GRANTS`；
  `readonly_enforced` 那一列是用户在表单里自报的（metadata-model §2.2），不作数。
  这**不属于**三层防御的任何一层：第一层是 AST 白名单（§1）、第二层是 `EXPLAIN` dry-run（§3）、
  第三层是会话级只读与超时（§4）；`SHOW GRANTS` 检测是登记阶段的**前置**校验，
  三层防御管的是 SQL 已经生成之后
- 工单 006 验收 4：结构化错误要说清"哪个字段错了"
"""

from __future__ import annotations

import pytest

from app.models.datasource import DataSource
from app.services.datasource_service import (
    describe_source_error,
    grants_verdict,
    root_cause,
    table_scope_filter,
)


def _row(**overrides: object) -> DataSource:
    base: dict[str, object] = {
        "name": "demo",
        "kind": "mysql",
        "host": "127.0.0.1",
        "port": 3306,
        "catalog_name": "",
        "connect_user": "aiweb_ro",
        "secret_enc": b"x",
        "created_by": 1,
    }
    return DataSource(**{**base, **overrides})  # type: ignore[arg-type]


# ------------------------------------------------------- include/exclude_tables 的落地
# 口径：metadata-model §2.2 把这两列写成"正则/通配"二选一，工单 006 的探测要按源原生
# LIKE 解释（007 同步沿用同一口径）。空列表 = 不过滤，不是"匹配零张表"。


def test_没填范围时不追加任何条件() -> None:
    assert table_scope_filter(_row()) == ("", {})
    assert table_scope_filter(_row(include_tables=[], exclude_tables=[])) == ("", {})


def test_include_tables是多条LIKE取并() -> None:
    """填了两张就两张都要，取 OR；填一张精确名时 LIKE 退化成等值。"""
    sql, params = table_scope_filter(_row(include_tables=["order_main", "v_daily%"]))
    assert sql == "(table_name LIKE :inc_0 OR table_name LIKE :inc_1)"
    assert params == {"inc_0": "order_main", "inc_1": "v_daily%"}


def test_exclude_tables用NOT_LIKE() -> None:
    """演示库的建库脚本自检口径是"下划线开头的表不算业务表"，源原生写法就是 `\\_%`。"""
    sql, params = table_scope_filter(_row(exclude_tables=["\\_%"]))
    assert sql == "(table_name NOT LIKE :exc_0)"
    assert params == {"exc_0": "\\_%"}


def test_多条排除是AND不是OR() -> None:
    """一张表要"哪一条都不像"才被留下；用 OR 的话它等于只排掉了最后一条。"""
    sql, _ = table_scope_filter(_row(exclude_tables=["\\_%", "_tmp%"]))
    assert sql == "(table_name NOT LIKE :exc_0 AND table_name NOT LIKE :exc_1)"


def test_同时有include和exclude时用AND串起来() -> None:
    """排除不能盖掉包含：先圈定范围再剔掉噪声表，两个条件都得在场。"""
    sql, params = table_scope_filter(
        _row(include_tables=["order_%"], exclude_tables=["order_draft"])
    )
    assert sql == "(table_name LIKE :inc_0) AND (table_name NOT LIKE :exc_0)"
    assert params == {"inc_0": "order_%", "exc_0": "order_draft"}


def test_表名留在绑定参数里不进SQL() -> None:
    """表名是用户填的。拼进 SQL 片段就等于给知识库开了一道注入门——
    守卫（003）管的是问数那条路，元数据这条路不经过它。"""
    evil = "x'; drop table users--"
    sql, params = table_scope_filter(_row(include_tables=[evil]))
    assert evil not in sql
    assert params["inc_0"] == evil


def test_空表名条目被丢掉而不是匹配零张表() -> None:
    """`include_tables=[""]` 会让每条 LIKE 都不命中，整个源的表静默消失；
    列里有空串（前端多剪了一格）时应当视同没填。"""
    assert table_scope_filter(_row(include_tables=["", "  "])) == ("", {})


def test_只有select和usage时判定为只读() -> None:
    """演示库的 aiweb_ro 就是这两行（002 的建库脚本），它必须被判成 OK。"""
    verdict = grants_verdict(
        [
            "GRANT USAGE ON *.* TO `aiweb_ro`@`localhost`",
            "GRANT SELECT ON `ai_web_demo`.* TO `aiweb_ro`@`localhost`",
        ]
    )
    assert verdict == {"read_only": True, "code": None, "warnings": []}


def test_混进写权限就报出是哪一条() -> None:
    """只报"不是只读"没用——用户要去源库改，得知道是哪一行授权多出来的。"""
    verdict = grants_verdict(
        [
            "GRANT SELECT ON `ai_web_demo`.* TO `aiweb_ro`@`localhost`",
            "GRANT SELECT, INSERT, UPDATE ON `ai_web_demo`.* TO `aiweb_ro`@`localhost`",
        ]
    )
    assert verdict["read_only"] is False
    # §4.1 点名的机读标记：前端要按它挂红字，而不是去 parse 我们拼的中文警告
    assert verdict["code"] == "readonly_capability_missing"
    assert len(verdict["warnings"]) == 1
    assert "insert" in verdict["warnings"][0].lower()
    assert "UPDATE" in verdict["warnings"][0], "警告里要带上原文那一行"


def test_库名里带create这类词不会被误判() -> None:
    """按子串匹配权限词，会把 `GRANT SELECT ON \\`create_log\\`.*` 判成有 CREATE。

    误判的代价不对称：把一个能用的只读账号报成不安全，用户就会去把警告关掉——
    然后真的不安全那条也一起被关掉。
    """
    verdict = grants_verdict(["GRANT SELECT ON `ai_web_create_demo`.`order_main` TO x@y"])
    assert verdict["read_only"] is True, verdict["warnings"]


def test_all_privileges被判为非只读() -> None:
    verdict = grants_verdict(["GRANT ALL PRIVILEGES ON *.* TO `root`@`localhost`"])
    assert verdict["read_only"] is False


def test_全库授权即使只有SELECT也算越界() -> None:
    """`GRANT SELECT ON *.*` 里没有任何写权限词，但它能读 `mysql.user`——口令散列与账号清单。

    工单 002 与 verification.md §1.2 因此把"只授 `ON <库>.*`、不给 `ON *.*`"定成硬口径。
    只看权限词而不看授权范围，这条口径就永远测不出真拦截。USAGE ON *.* 是例外：
    它不含任何库表权限，只是"账号能登录"。
    """
    verdict = grants_verdict(["GRANT SELECT ON *.* TO `aiweb_ro`@`localhost`"])
    assert verdict["read_only"] is False
    assert "*.*" in verdict["warnings"][0]


def test_库级的select仍然算合格() -> None:
    """收紧范围判定不能把合规账号一起判死：ON `ai_web_demo`.* 是 002 给的那一条。"""
    verdict = grants_verdict(["GRANT SELECT ON `ai_web_demo`.* TO `aiweb_ro`@`localhost`"])
    assert verdict == {"read_only": True, "code": None, "warnings": []}


def test_能触发写入的权限词也算非只读() -> None:
    """TRIGGER/EVENT 都不叫"写"，但一个建触发器、一个建定时任务，都能改数据。

    `read_only` 这个名字给出的保证比"没有 INSERT 权限"宽，所以词表要按能否改数据收。
    """
    for priv in ("trigger", "event", "shutdown"):
        verdict = grants_verdict([f"GRANT SELECT, {priv.upper()} ON `db`.* TO u@h"])
        assert verdict["read_only"] is False, priv


@pytest.mark.parametrize(
    ("errno", "needle"),
    [
        (1045, "connect_password"),
        (2003, "host"),
        (1049, "库不存在"),
    ],
)
def test_错误号翻成的文案里点出该改哪一格(errno: int, needle: str) -> None:
    class _DrvError(Exception):
        pass

    raw = "Access denied for user 'aiweb_ro'@'127.0.0.1'"
    message = describe_source_error(_DrvError(errno, raw))
    assert needle in message


def test_翻译不回吐驱动原文() -> None:
    """驱动原文会带上账号与主机；那已经是半个连接串了。"""
    raw = "Access denied for user 'aiweb_ro'@'10.0.0.7' (using password: YES)"

    class _DrvError(Exception):
        pass

    message = describe_source_error(_DrvError(1045, raw))
    assert raw not in message
    assert "aiweb_ro" not in message


def test_错误号不是整数时既不崩也不带原文() -> None:
    """asyncmy 有 `raise errors.Error("Already closed")` 这类抛点：args[0] 是字符串。

    原来的写法 `args[0] if args else None` 之外还要防形状：无 args 的异常会让映射器自己
    抛 IndexError，而它是在 `except SQLAlchemyError` 处理器里被调用的——那次"连不上"
    就顶成了裸 500。
    """

    class _DrvError(Exception):
        pass

    for exc in (_DrvError(), _DrvError("Already closed"), _DrvError(None, "x")):
        message = describe_source_error(exc)
        assert "Already closed" not in message
        assert message  # 不许回空串：前端要显示它


@pytest.mark.parametrize(("errno", "needle"), [(2013, "timeout_ms"), (2006, "源库重启")])
def test_连接中途断开也有文案(errno: int, needle: str) -> None:
    """2013/2006 是 asyncmy 实际会抛的两个"连上之后断了"（它不抛 2002/2005）。

    没文案的话它们落到兜底档，用户看到的还是那句没有信息量的"源库返回错误"。
    """

    class _DrvError(Exception):
        pass

    assert needle in describe_source_error(_DrvError(errno, "Lost connection"))


def test_错误号在sqlalchemy包装的那一层() -> None:
    """SQLAlchemyError.args[0] 是语句字符串，不是错误号。

    不剥这层的话映射一条都不会命中，所有源库错误都变成"错误号 <整条SQL>"。
    """
    from sqlalchemy.exc import OperationalError

    class _DrvError(Exception):
        pass

    wrapped = OperationalError("SELECT 1", {}, _DrvError(1045, "Access denied"))
    assert isinstance(root_cause(wrapped), _DrvError)
    assert "connect_password" in describe_source_error(root_cause(wrapped))
