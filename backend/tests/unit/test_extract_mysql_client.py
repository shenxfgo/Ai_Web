"""`MySQLExtractor` 的编排层：连接、五条 IS 查询的先后、规模保护与乱码告警。

这一层的期望值口径：
- `docs/metadata-model.md` §6：probe 阶段的行为（版本、`max_execution_time` 支持与否、
  超限拒绝 + 三个出路）
- 同文 §8.1 A–E：抽一个 schema 就是"先 B 拿表，再 C/D/E 拿细节"
- 同文 §10.1：中文注释整片回 `?` 时打 `CHARSET_SUSPECT` warning

真连库才有的形状（结果集键名大小写、`?` 乱码、CARDINALITY 是否真非空）不在这里验，
那是 integration 打演示库那一片的活。这里只验"发什么 SQL、按什么顺序、结果怎么归并"，
所以把 `_rows` 换成假数据——`_rows` 是这个模块唯一的 I/O 边界。
"""

from __future__ import annotations

from collections.abc import Sequence

import pymysql
import pytest
from sqlalchemy.exc import OperationalError as SourceOperationalError

from app.core.errors import ExtractScopeTooLarge
from app.extractor.base import ConnectionSpec, RawCatalog
from app.extractor.mysql import MySQLExtractor


class Stub(MySQLExtractor):
    """只替掉 `_rows` 这一颗缝：SQL 构造、执行顺序、映射、告警都是真代码。"""

    # collect 内部要 server_version，所以每个用例都得备这三条；用例自己给了就优先用用例的
    _PROBE = (
        ("select version()", [{"v": "5.7.17-log"}]),
        ("select @@character_set_results", [{"cs": "utf8mb4"}]),
        ("select @@max_execution_time", [{"max_execution_time": 0}]),
    )

    def __init__(self, responses: Sequence[tuple[str, object]]) -> None:
        super().__init__(ConnectionSpec(host="127.0.0.1", port=3306, user="aiweb_ro", password="p"))
        self.executed: list[tuple[str, dict[str, object]]] = []
        self._responses = [*responses, *self._PROBE]

    def _rows(self, sql: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
        # 换行和缩进是 SQL 文本的排版，不是语义；快照单测才管排版
        normalized = " ".join(sql.lower().split())
        self.executed.append((sql, dict(params or {})))
        for needle, value in self._responses:
            if needle in normalized:
                if isinstance(value, BaseException):
                    raise value
                assert isinstance(value, list)
                return value
        raise AssertionError(f"这条 SQL 没有准备假数据：{normalized}")


def _is_sources(stub: Stub) -> list[str]:
    """每条 §8.1 查询的主表（FROM 后第一个词）；JOIN 的部分不参与断言。

    probe 那三条（`SELECT VERSION()` 等）不带 information_schema，因此也被排除。
    """
    out = []
    for sql, _ in stub.executed:
        tokens = " ".join(sql.lower().split()).split()
        if not any("information_schema" in token for token in tokens):
            continue
        out.append(tokens[tokens.index("from") + 1])
    return out


def _statement_for(stub: Stub, needle: str) -> tuple[str, dict[str, object]]:
    for sql, params in stub.executed:
        if needle in sql.lower():
            return sql, params
    raise AssertionError(f"没有发过含 {needle!r} 的语句")


def _isv_error(errno: int, message: str) -> SourceOperationalError:
    """按真的驱动异常造一个：SQLAlchemy 会把 DBAPI 异常包一层，错误号在被包的那个身上。"""
    return SourceOperationalError(
        "SELECT @@max_execution_time", {}, pymysql.err.OperationalError(errno, message)
    )


async def test_probe_报版本字符集与_max_execution_time_支持情况() -> None:
    stub = Stub(
        [
            ("select version()", [{"v": "5.7.17-log"}]),
            ("select @@character_set_results", [{"cs": "utf8mb4"}]),
            ("select @@max_execution_time", [{"max_execution_time": 0}]),
        ]
    )
    info = await stub.probe()
    assert info.kind == "mysql"
    assert info.server_version == "5.7.17-log"
    # §10.1：注释能不能信取决于"到达客户端"的字符集，不是服务器默认字符集
    assert info.charset == "utf8mb4"
    assert info.supports_max_execution_time is True


async def test_probe_只把_1193_当成不支持_max_execution_time() -> None:
    """源库回"不支持这个变量"时要降级，回"连接断了"时必须继续抛。

    两类都吞成 supports=False 的话，网络故障会被报成"这个源不支持超时控制"，
    而 011 的超时策略正是拿这个布尔值决定的。
    """
    stub = Stub(
        [
            ("select version()", [{"v": "5.7.17-log"}]),
            ("select @@character_set_results", [{"cs": "utf8mb4"}]),
            ("select @@max_execution_time", _isv_error(1193, "Unknown system variable")),
        ]
    )
    assert (await stub.probe()).supports_max_execution_time is False


async def test_probe_不吞连接类错误() -> None:
    stub = Stub(
        [
            ("select version()", [{"v": "5.7.17-log"}]),
            ("select @@character_set_results", [{"cs": "utf8mb4"}]),
            ("select @@max_execution_time", _isv_error(2013, "Lost connection to server")),
        ]
    )
    with pytest.raises(SourceOperationalError):
        await stub.probe()


def _table_row(schema: str, name: str, *, comment: str = "订单主表") -> dict[str, object]:
    return {
        "TABLE_SCHEMA": schema,
        "TABLE_NAME": name,
        "table_type": "BASE TABLE",
        "TABLE_COMMENT": comment,
        "ENGINE": "InnoDB",
        "ROW_FORMAT": "Dynamic",
        "TABLE_COLLATION": "utf8mb4_general_ci",
        "TABLE_ROWS": 30000,
        "DATA_LENGTH": 1638400,
        "INDEX_LENGTH": 49152,
        "CREATE_TIME": None,
        "UPDATE_TIME": None,
    }


def _column_row(table: str, name: str) -> dict[str, object]:
    return {
        "TABLE_NAME": table,
        "COLUMN_NAME": name,
        "ORDINAL_POSITION": 1,
        "DATA_TYPE": "bigint",
        "COLUMN_TYPE": "bigint(20)",
        "IS_NULLABLE": "NO",
        "COLUMN_DEFAULT": None,
        "EXTRA": "",
        "COLUMN_COMMENT": "主键",
        "CHARACTER_MAXIMUM_LENGTH": None,
        "NUMERIC_PRECISION": 19,
        "NUMERIC_SCALE": 0,
        "COLLATION_NAME": None,
        "enum_def": None,
    }


def _index_row(table: str, index: str, column: str) -> dict[str, object]:
    return {
        "TABLE_NAME": table,
        "INDEX_NAME": index,
        "is_unique": 1,
        "is_primary": 1,
        "INDEX_TYPE": "BTREE",
        "NULLABLE": "",
        "COLUMN_NAME": column,
        "SEQ_IN_INDEX": 1,
        "CARDINALITY": 29874,
        "SUB_PART": None,
        "COLLATION": "A",
        "index_comment": "",
    }


def _fk_row(table: str, column: str, to_table: str) -> dict[str, object]:
    return {
        "TABLE_NAME": table,
        "CONSTRAINT_NAME": f"fk_{table}_{column}",
        "COLUMN_NAME": column,
        "ORDINAL_POSITION": 1,
        "REFERENCED_TABLE_SCHEMA": "ai_web_demo",
        "REFERENCED_TABLE_NAME": to_table,
        "REFERENCED_COLUMN_NAME": "id",
        "DELETE_RULE": "CASCADE",
        "UPDATE_RULE": "NO ACTION",
    }


def _catalog(schema: str = "ai_web_demo") -> RawCatalog:
    return RawCatalog(
        catalog_name="",
        schema_name=schema,
        charset="utf8mb4",
        collation="utf8mb4_general_ci",
        approx_size_bytes=1,
        visible_table_count=1,
    )


async def test_discover_把_catalogs_查询的结果摊成_rawcatalog() -> None:
    stub = Stub(
        [
            (
                "from information_schema.schemata",
                [
                    {
                        "SCHEMA_NAME": "ai_web_demo",
                        "DEFAULT_CHARACTER_SET_NAME": "utf8mb4",
                        "DEFAULT_COLLATION_NAME": "utf8mb4_general_ci",
                        "size_bytes": 2097152,
                        "approx_rows": 50000,
                        "visible_table_count": 10,
                    }
                ],
            )
        ]
    )
    catalogs = await stub.discover()
    assert [c.schema_name for c in catalogs] == ["ai_web_demo"]
    assert catalogs[0].catalog_name == "", "§1：MySQL 的 catalog 恒空"
    assert catalogs[0].visible_table_count == 10
    # 这条查询必须把系统库排除掉，否则 60+ 张 mysql.* 表会灌进卡片库（§8.1 A）
    assert "schema_name not in ('information_schema', 'mysql', 'performance_schema', 'sys')" in (
        " ".join(stub.executed[0][0].lower().split())
    )


def _type_counts(*pairs: tuple[str, int]) -> list[dict[str, object]]:
    """按 TABLE_TYPE 分组计数的假返回值（键名照 `build_sql_count_scope` 的别名）。"""
    return [{"table_type": t, "n": n} for t, n in pairs]


async def test_count_scope_按表类型数出_total_base_table_view() -> None:
    stub = Stub([("group by t.table_type", _type_counts(("BASE TABLE", 9), ("VIEW", 1)))])
    counts = await stub.count_scope([_catalog()])
    assert (counts.total, counts.base_table, counts.view) == (10, 9, 1), (
        "演示库 2026-09-27 拍板的分母就是这一组数（9 表 + 1 视图 = 10，不是 11）"
    )


async def test_count_scope_多库时逐库相加并且只发那一条计数() -> None:
    """一库一条而不是合并：范围条件是按 schema 渲染的，合并就得把 `:schema` 拆成列表。"""
    stub = Stub([("group by t.table_type", _type_counts(("BASE TABLE", 2), ("VIEW", 1)))])
    counts = await stub.count_scope(
        [_catalog("shop"), _catalog("crm")],
        table_sql="(t.table_name not like :exc_0)",
        table_params={"exc_0": r"\_%"},
    )
    assert (counts.total, counts.base_table, counts.view) == (6, 4, 2)
    assert len(stub.executed) == 2, "只有计数那两条：昂贵的 C/D/E 一条都不该发"
    assert [p["schema"] for _, p in stub.executed] == ["shop", "crm"], "每库一条、按库相加"
    assert [p["exc_0"] for _, p in stub.executed] == [r"\_%", r"\_%"], "范围片段原样带上"


async def test_collect_一个_schema_四条查询并把外键归并进来() -> None:
    stub = Stub(
        [
            ("from information_schema.tables t", [_table_row("ai_web_demo", "order_main")]),
            ("from information_schema.columns c", [_column_row("order_main", "id")]),
            ("from information_schema.statistics s", [_index_row("order_main", "PRIMARY", "id")]),
            ("from information_schema.key_column_usage k", []),
        ]
    )
    manifest = await stub.collect([_catalog()])
    assert manifest.kind == "mysql"
    assert manifest.server_version == "5.7.17-log"
    assert [t.table_name for t in manifest.tables] == ["order_main"]
    assert [c.column_name for c in manifest.columns] == ["id"]
    assert manifest.columns[0].is_primary_key is True, "索引先跑，flag 才回填得进列"
    assert manifest.indexes[0].cardinality == 29874
    assert manifest.foreign_keys == []
    assert manifest.catalogs[0].schema_name == "ai_web_demo"
    # §8.1 的整段前提：查询条数与表数量无关，一个 schema 就是 4 条
    # B 必须最先：拿到表数才能在昂贵的列/索引查询之前拒绝超限（§6）。
    # C/D 的先后是实现细节（这里索引先跑，主键标记才回填得进列），所以钉顺序而不是钉名字之外的一切
    assert _is_sources(stub) == [
        "information_schema.tables",
        "information_schema.statistics",
        "information_schema.columns",
        "information_schema.key_column_usage",
    ]


async def test_collect_把数据源配置的表范围原样带进_b() -> None:
    """范围条件由调用方渲染（006 的 `table_scope_filter`），方言层不自己发明口径。"""
    stub = Stub(
        [
            ("from information_schema.tables t", []),
            ("from information_schema.columns c", []),
            ("from information_schema.statistics s", []),
            ("from information_schema.key_column_usage k", []),
        ]
    )
    await stub.collect(
        [_catalog()],
        table_sql="(t.table_name not like :exc_0)",
        table_params={"exc_0": r"\_%"},
    )
    sql, params = _statement_for(stub, "information_schema.tables")
    assert "(t.table_name not like :exc_0)" in " ".join(sql.split())
    assert params == {"schema": "ai_web_demo", "exc_0": r"\_%"}


async def test_collect_范围里没有表时不发_in_空列表() -> None:
    """`IN ()` 是语法错误：没有表就没有细节可查，直接给空清单。"""
    stub = Stub([("from information_schema.tables t", [])])
    manifest = await stub.collect([_catalog()])
    assert manifest.tables == [] and manifest.columns == []
    assert _is_sources(stub) == ["information_schema.tables"], "只有 B 那一条"


async def test_collect_超限在昂贵的列查询之前就拒绝并给三个出路() -> None:
    """§6：>MAX_TABLES 拒绝，出路是结构化数据，不是让人猜的错误文案。"""
    stub = Stub(
        [
            (
                "from information_schema.tables t",
                [_table_row("ai_web_demo", f"t{i}") for i in range(3)],
            ),
        ]
    )
    with pytest.raises(ExtractScopeTooLarge) as caught:
        await stub.collect([_catalog()], max_tables=2)
    assert caught.value.detail == {
        "table_count": 3,
        "max_tables": 2,
        "remedies": [
            "配 include_tables 白名单",
            "只同步部分 schema（include_schemas）",
            "调高 AIWEB_EXTRACT__MAX_TABLES 上限（admin 改配置）",
        ],
    }
    assert _is_sources(stub) == ["information_schema.tables"], "拒绝之后不该再去打 C/D/E"


async def test_collect_中文注释整片回问号时打_charset_suspect() -> None:
    stub = Stub(
        [
            ("from information_schema.tables t", [_table_row("ai_web_demo", "t", comment="?????")]),
            ("from information_schema.columns c", [_column_row("t", "id")]),
            ("from information_schema.statistics s", []),
            ("from information_schema.key_column_usage k", []),
        ]
    )
    # 列注释是正常中文，表注释全是问号：整体占比 (5)/(5+2) > 0.3 才算乱码
    manifest = await stub.collect([_catalog()])
    assert [w.code for w in manifest.warnings] == ["CHARSET_SUSPECT"]


async def test_collect_注释正常时不乱打警告() -> None:
    stub = Stub(
        [
            ("from information_schema.tables t", [_table_row("ai_web_demo", "t")]),
            ("from information_schema.columns c", [_column_row("t", "id")]),
            ("from information_schema.statistics s", []),
            ("from information_schema.key_column_usage k", []),
        ]
    )
    assert (await stub.collect([_catalog()])).warnings == []


async def test_抽取全程只对源库发_select() -> None:
    """验收 6 的单元侧那一半：真库侧由 live 用例再钉一遍。

    账号是只读的（002 只给了 `ai_web_demo` 的 SELECT），发出任何写语句都是一次必然失败
    的往返，而且说明这一层的"只读"承诺是假的。
    """
    stub = Stub(
        [
            ("from information_schema.tables t", [_table_row("ai_web_demo", "order_main")]),
            ("from information_schema.columns c", [_column_row("order_main", "id")]),
            ("from information_schema.statistics s", [_index_row("order_main", "PRIMARY", "id")]),
            (
                "from information_schema.key_column_usage k",
                [_fk_row("order_item", "order_id", "order_main")],
            ),
        ]
    )
    await stub.collect([_catalog()])
    for sql, _ in stub.executed:
        normalized = " ".join(sql.split())
        assert normalized.upper().startswith("SELECT"), normalized
        # 尾随空格是刻意的：IS 里有 DELETE_RULE / UPDATE_TIME 这些**列名**，不是语句
        for banned in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "CREATE ", "TRUNCATE ", "GRANT "):
            assert banned not in normalized.upper(), f"{banned}出现在：{normalized}"
