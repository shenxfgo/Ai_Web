"""`PostgresExtractor` 的编排层：连接、五条 catalog 查询的先后、分批、规模保护。

与 `test_extract_mysql_client.py` 同一颗缝：`_rows` 是模块唯一的 I/O 出口，把假数据从这里
灌进去，"发什么 SQL、按什么顺序、结果怎么归并"就是真代码在跑。真库形状（结果集键名、
演示库的对象数）归 `tests/integration/test_extract_pg_live.py`。

期望值口径：
- `docs/metadata-model.md` §8.2 A–E：抽一个 schema 就是"先 B 拿表，再 C/D/E 拿细节"
- 同文 §6：probe 阶段的行为（版本、`statement_timeout` 支持与否、超限拒绝 + 三个出路）
- 工单 020 的已定口径（批是发送切片不是提交单元、具名绑定参数不许换成拼接、让气只在批间）
- 工单 024 的已定口径（PG 的 `catalog_name` 是真库名、PG 侧不打 `CHARSET_SUSPECT`）
- `docs/verification.md` §1.5 的十个对象名（9 表 + 1 视图）给分批一个稳定输入
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence

import psycopg
import pytest
from sqlalchemy.exc import OperationalError as SourceOperationalError
from sqlalchemy.exc import ProgrammingError

from app.core.errors import ExtractScopeTooLarge
from app.extractor.base import ConnectionSpec, RawCatalog
from app.extractor.postgres import PostgresExtractor
from tests.unit.test_extract_pg_map import (
    _column_row,
    _fk_row,
    _index_row,
    _table_row,
)

# §1.5 那十个对象（`_aiweb_demo_marker` 被 exclude_tables 的 `\\_%` 排除，不在名单里）。
# 顺序取自 §1.5 表格逐字抄的表名按字典序排——B 那条没有 ORDER BY，交付顺序不是承诺，
# 这份名单只是给假件一个稳定输入。
VISIBLE = (
    "category",
    "customer",
    "order_item",
    "order_main",
    "payment_record",
    "product",
    "product_stats_wide",
    "t_no_comment",
    "user_activity_log",
    "v_daily_sales",
)


class Stub(PostgresExtractor):
    """只替掉 `_rows` 这一颗缝：SQL 构造、执行顺序、映射、告警都是真代码。"""

    _PROBE = (
        ("current_setting('server_version')", [{"v": "18.6"}]),
        ("show client_encoding", [{"client_encoding": "UTF8"}]),
        ("show statement_timeout", [{"statement_timeout": "0"}]),
    )

    def __init__(self, responses: Sequence[tuple[str, object]]) -> None:
        super().__init__(
            ConnectionSpec(
                host="127.0.0.1",
                port=5432,
                user="demo_pg_ro",
                password="p",
                database="ai_web_demo_pg",
            )
        )
        self.executed: list[tuple[str, dict[str, object]]] = []
        self._responses = [*responses, *self._PROBE]

    def _rows(self, sql: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
        normalized = " ".join(sql.lower().split())
        self.executed.append((sql, dict(params or {})))
        for needle, value in self._responses:
            if needle in normalized:
                if isinstance(value, BaseException):
                    raise value
                assert isinstance(value, list)
                return value
        raise AssertionError(f"这条 SQL 没有准备假数据：{normalized}")


def _catalog(schema: str = "demo", catalog: str = "ai_web_demo_pg") -> RawCatalog:
    return RawCatalog(
        catalog_name=catalog,
        schema_name=schema,
        charset=None,
        collation=None,
        approx_size_bytes=None,
        visible_table_count=None,
    )


def _pg_sources(stub: Stub) -> list[str]:
    """每条发出去的 §8.2 查询各自主表（按发出顺序）。

    不能简单地取"FROM 后第一个词"：B 那条的投影里有一个 `IN (SELECT oid FROM pg_am ...)`
    子查询，按词取会把它数成 `pg_am`。所以这里按各条查询的**身份证据**匹配，探针与
    `Stub._rows` 用的是同一套 needles——两边判据一致，红了才知道是发错了而不是数错了。
    """
    labels = [
        ("from pg_catalog.pg_namespace n", "pg_catalog.pg_namespace"),
        ("left join pg_catalog.pg_stat_user_tables", "pg_catalog.pg_class"),  # B 与计数条
        ("from pg_catalog.pg_index i", "pg_catalog.pg_index"),
        ("from pg_catalog.pg_attribute a", "pg_catalog.pg_attribute"),
        ("from pg_catalog.pg_constraint con", "pg_catalog.pg_constraint"),
    ]
    out = []
    for sql, _ in stub.executed:
        normalized = " ".join(sql.lower().split())
        for needle, label in labels:
            if needle in normalized:
                out.append(label)
                break
    return out


def _statement_for(stub: Stub, needle: str) -> tuple[str, dict[str, object]]:
    for sql, params in stub.executed:
        if needle in sql.lower():
            return sql, params
    raise AssertionError(f"没有发过含 {needle!r} 的语句")


def _pg_error(cls: type[psycopg.Error]) -> SourceOperationalError:
    """按真的驱动异常造一个：SQLAlchemy 会包一层，SQLSTATE 在被包的那颗身上。"""
    if cls is psycopg.errors.UndefinedObject:
        return ProgrammingError(
            "SHOW statement_timeout", {}, cls("unrecognized configuration parameter")
        )
    return SourceOperationalError("SHOW statement_timeout", {}, cls("server closed the connection"))


async def test_probe_报版本编码与_statement_timeout_支持情况() -> None:
    stub = Stub(
        [
            ("current_setting('server_version')", [{"v": "18.6"}]),
            ("show client_encoding", [{"client_encoding": "UTF8"}]),
            ("show statement_timeout", [{"statement_timeout": "0"}]),
        ]
    )
    info = await stub.probe()
    assert info.kind == "postgres"
    assert info.server_version == "18.6"
    # §10.1 的对应物：中文注释能不能信取决于**到达客户端**的编码，libpq 这一侧就叫 client_encoding
    assert info.charset == "UTF8"
    assert info.supports_max_execution_time is True


async def test_probe_只把_42704_当成不支持_statement_timeout() -> None:
    """变量不存在才降级；连接类错误必须继续抛（与 MySQL 那侧只认 1193 同一个理由）。"""
    stub = Stub(
        [
            ("current_setting('server_version')", [{"v": "18.6"}]),
            ("show client_encoding", [{"client_encoding": "UTF8"}]),
            ("show statement_timeout", _pg_error(psycopg.errors.UndefinedObject)),
        ]
    )
    assert (await stub.probe()).supports_max_execution_time is False


async def test_probe_不吞连接类错误() -> None:
    """一次网络抖动被报成"这个源不支持超时控制"的话，011 的超时策略就建在谎话上。"""
    stub = Stub(
        [
            ("current_setting('server_version')", [{"v": "18.6"}]),
            ("show client_encoding", [{"client_encoding": "UTF8"}]),
            ("show statement_timeout", _pg_error(psycopg.OperationalError)),
        ]
    )
    with pytest.raises(SourceOperationalError):
        await stub.probe()


async def test_discover_用_current_database_填_catalog_其余系统格留空() -> None:
    stub = Stub(
        [
            (
                "from pg_catalog.pg_namespace n",
                [
                    {
                        "catalog_name": "ai_web_demo_pg",
                        "schema_name": "demo",
                        "schema_comment": "演示库",
                    }
                ],
            )
        ]
    )
    catalogs = await stub.discover()
    assert [(c.catalog_name, c.schema_name) for c in catalogs] == [("ai_web_demo_pg", "demo")], (
        "与 MySQL 相反：PG 的 catalog 是 current_database() 的真库名，不是空串"
    )
    assert (catalogs[0].charset, catalogs[0].collation) == (None, None), (
        "PG 的编码在库级、排序规则在列级，没有 MySQL 那种每库 DEFAULT_CHARSET 的对应物"
    )
    assert (
        catalogs[0].approx_size_bytes,
        catalogs[0].visible_table_count,
        catalogs[0].approx_rows,
    ) == (None, None, None), "偏离 6/A 条不 JOIN pg_class：填它就得在发现阶段全库逐表算尺寸"
    low = " ".join(stub.executed[0][0].lower().split())
    # §8.2 A 的排除名单，一个都不能少：漏掉 pg_toast 之类会把系统对象当业务表抽进卡片库
    for needle in (
        "'pg_catalog'",
        "'information_schema'",
        "'pg_toast'",
        "pg\\_temp",
        "has_schema_privilege",
    ):
        assert needle.lower() in low, needle


def _type_counts(*pairs: tuple[str, int]) -> list[dict[str, object]]:
    return [{"table_type": t, "n": n} for t, n in pairs]


async def test_count_scope_按表类型数出_total_base_table_view() -> None:
    stub = Stub([("group by 1", _type_counts(("BASE TABLE", 9), ("VIEW", 1)))])
    counts = await stub.count_scope([_catalog()])
    assert (counts.total, counts.base_table, counts.view) == (10, 9, 1), (
        "§1.5 的四个数就是这一组：10 / 9 / 1（11 是含下划线标记对象的裸计数）"
    )


async def test_count_scope_多库时逐库相加并且只发那一条计数() -> None:
    stub = Stub([("group by 1", _type_counts(("BASE TABLE", 2), ("VIEW", 1)))])
    counts = await stub.count_scope(
        [_catalog("shop"), _catalog("crm")],
        table_sql="(c.relname not like :exc_0)",
        table_params={"exc_0": r"\_%"},
    )
    assert (counts.total, counts.base_table, counts.view) == (6, 4, 2)
    assert len(stub.executed) == 2, "只有计数那两条：昂贵的 C/D/E 一条都不该发"
    assert [p["schema"] for _, p in stub.executed] == ["shop", "crm"], "每库一条、按库相加"
    assert [p["exc_0"] for _, p in stub.executed] == [r"\_%", r"\_%"], "范围片段原样带上"


async def test_collect_一个_schema_四条查询并把外键归并进来() -> None:
    stub = Stub(
        [
            ("from pg_catalog.pg_class c", [_table_row("order_main")]),
            (
                "from pg_catalog.pg_attribute a",
                [_column_row("order_main", "id", typname="int4", raw="integer")],
            ),
            (
                "from pg_catalog.pg_index i",
                [_index_row("order_main", "pk_order_main", "id", 1, unique=True, primary=True)],
            ),
            ("from pg_catalog.pg_constraint con", []),
        ]
    )
    manifest = await stub.collect([_catalog()], batch_size=200, batch_interval_ms=0)
    assert manifest.kind == "postgres"
    assert manifest.server_version == "18.6"
    assert [t.table_name for t in manifest.tables] == ["order_main"]
    assert [c.column_name for c in manifest.columns] == ["id"]
    assert manifest.columns[0].is_primary_key is True, "索引先跑，flag 才回填得进列"
    assert manifest.foreign_keys == []
    assert manifest.catalogs[0].schema_name == "demo"
    # B 必须最先：拿到表数才能在昂贵的列/索引查询之前拒绝超限（§6）。
    # C/D 的先后不是口径而是形状约束：`apply_index_flags` 要索引先到才回填得进列，
    # PG 这一串与 mysql.py:546（indexes）→:550（columns）同形，所以出处是那两行不是本模块自己
    assert _pg_sources(stub) == [
        "pg_catalog.pg_class",
        "pg_catalog.pg_index",
        "pg_catalog.pg_attribute",
        "pg_catalog.pg_constraint",
    ]


async def test_collect_把数据源配置的表范围原样带进_b() -> None:
    """范围条件由调用方渲染（006 的 `table_scope_filter`），方言层不自己发明口径。

    列写法是 PG 的 `c.relname` 而不是 MySQL 的 `t.table_name`——这一段必须在编排层就能看见，
    否则接线的错会推迟到真库上换一个 `42703 undefined column` 出来。
    """
    stub = Stub([("from pg_catalog.pg_class c", [])])
    await stub.collect(
        [_catalog()],
        table_sql="(c.relname not like :exc_0)",
        table_params={"exc_0": r"\_%"},
        batch_size=200,
        batch_interval_ms=0,
    )
    sql, params = _statement_for(stub, "pg_catalog.pg_class")
    assert "(c.relname not like :exc_0)" in " ".join(sql.split())
    assert params == {"schema": "demo", "exc_0": r"\_%"}


async def test_collect_范围里没有表时不发_in_空列表() -> None:
    """`IN ()` 在 PG 里同样是语法错误：没有表就没有细节可查，直接给空清单。"""
    stub = Stub([("from pg_catalog.pg_class c", [])])
    manifest = await stub.collect([_catalog()], batch_size=200, batch_interval_ms=0)
    assert manifest.tables == [] and manifest.columns == []
    assert _pg_sources(stub) == ["pg_catalog.pg_class"], "只有 B 那一条"
    assert manifest.batches == []


async def test_collect_超限在昂贵的列查询之前就拒绝并给三个出路() -> None:
    stub = Stub([("from pg_catalog.pg_class c", [_table_row(f"t{i}") for i in range(3)])])
    with pytest.raises(ExtractScopeTooLarge) as caught:
        await stub.collect([_catalog()], max_tables=2, batch_size=200, batch_interval_ms=0)
    assert caught.value.detail == {
        "table_count": 3,
        "max_tables": 2,
        "remedies": [
            "配 include_tables 白名单",
            "只同步部分 schema（include_schemas）",
            "admin 用 ?force=true 覆盖上限",
        ],
    }
    assert _pg_sources(stub) == ["pg_catalog.pg_class"], "拒绝之后不该再去打 C/D/E"


async def test_collect_注释是中文时也不打_charset_suspect() -> None:
    """libpq 转不动时会**报错**而不是把中文换成问号，§10.1 那个形态在 PG 这一路不成立。

    这条断言的意义是"别顺手把 MySQL 的告警抄过来"：抄过来就会在一个不可能出现的分支上
    给运维一条假线索。演示库的编码由 015 的建库脚本用 `ENCODING 'UTF8'` 钉住。
    """
    stub = Stub(
        [
            ("from pg_catalog.pg_class c", [_table_row("t", comment="?????")]),
            ("from pg_catalog.pg_attribute a", []),
            ("from pg_catalog.pg_index i", []),
            ("from pg_catalog.pg_constraint con", []),
        ]
    )
    manifest = await stub.collect([_catalog()], batch_size=200, batch_interval_ms=0)
    assert manifest.warnings == []


# ------------------------------------------------------------------ 分批（工单 020 的形状）


class BatchStub(PostgresExtractor):
    """按**收到的表名**现算假数据的桩件。

    与上面 `Stub` 的分别很重要：那一份按语句匹配、每条永远回同一份假数据，用它测分批看不出
    "第二批发的是哪三张"（同 MySQL 那份 batch 桩件的理由）。
    """

    def __init__(self, names: Sequence[str] = VISIBLE) -> None:
        super().__init__(
            ConnectionSpec(
                host="127.0.0.1",
                port=5432,
                user="demo_pg_ro",
                password="p",
                database="ai_web_demo_pg",
            )
        )
        self._names = list(names)
        self.executed: list[tuple[str, dict[str, object]]] = []

    def _rows(self, sql: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
        normalized = " ".join(sql.lower().split())
        p = dict(params or {})
        self.executed.append((sql, p))
        if "current_setting('server_version')" in normalized:
            return [{"v": "18.6"}]
        if normalized.startswith("show client_encoding"):
            return [{"client_encoding": "UTF8"}]
        if normalized.startswith("show statement_timeout"):
            return [{"statement_timeout": "0"}]
        if "from pg_catalog.pg_class c" in normalized:
            # B 不认识表名清单（它按 schema + 范围过滤拿全量），这正是它不能被"分批"的原因
            return [_table_row(name) for name in self._names]
        asked = [
            v for _, v in sorted((int(k[4:]), str(v)) for k, v in p.items() if k.startswith("tbl_"))
        ]
        if "from pg_catalog.pg_index i" in normalized:
            return [
                _index_row(name, "pk_" + name, "id", 1, unique=True, primary=True) for name in asked
            ]
        if "from pg_catalog.pg_attribute a" in normalized:
            return [
                _column_row(name, "id", typname="int8", raw="bigint", comment="主键")
                for name in asked
            ]
        if "from pg_catalog.pg_constraint con" in normalized:
            return [_fk_row(name, "category_id", "category") for name in asked]
        raise AssertionError(f"这条 SQL 没有准备假数据：{normalized}")

    def batched_names(self, needle: str) -> list[list[str]]:
        out: list[list[str]] = []
        for sql, params in self.executed:
            if needle in sql.lower():
                out.append(sorted(str(v) for k, v in params.items() if k.startswith("tbl_")))
        return out


async def test_三张一批时列索引外键各发四条_且名单互不重叠() -> None:
    stub = BatchStub()
    await stub.collect([_catalog()], batch_size=3, batch_interval_ms=0)
    expected = [
        ["category", "customer", "order_item"],
        ["order_main", "payment_record", "product"],
        ["product_stats_wide", "t_no_comment", "user_activity_log"],
        ["v_daily_sales"],
    ]
    assert [len(b) for b in expected] == [3, 3, 3, 1], "10 / 3 = 4 批，末批只剩 1 张"
    for needle in (
        "from pg_catalog.pg_attribute a",
        "from pg_catalog.pg_index i",
        "from pg_catalog.pg_constraint con",
    ):
        assert stub.batched_names(needle) == expected, needle


async def test_昂贵查询仍然只用具名绑定参数() -> None:
    """把表名拼进 SQL 文本是注入面，直接判不合格（占位符逐一对应 + 文本里不出现真表名）。"""
    stub = BatchStub()
    await stub.collect([_catalog()], batch_size=3, batch_interval_ms=0)
    expensive = [
        (sql, params)
        for sql, params in stub.executed
        if any(t in sql.lower() for t in ("pg_attribute a", "pg_index i", "pg_constraint con"))
    ]
    assert len(expensive) == 12, "4 批 × 三条昂贵查询，一条都不许多"
    for sql, params in expensive:
        low = " ".join(sql.lower().split())
        names = [v for k, v in params.items() if k.startswith("tbl_")]
        assert set(re.findall(r":tbl_\d+", low)) == {f":tbl_{i}" for i in range(len(names))}, low
        for name in VISIBLE:
            assert name not in low, f"表名被拼进了语句文本：{name}"
        assert len(names) in (3, 1), params


async def test_分批不改变归并结果_列索引外键一条都不多也不丢() -> None:
    """批是**发送方式**，不是第二套账（020 验收 5 在 PG 这一侧的同一句话）。"""
    small = BatchStub()
    m_small = await small.collect([_catalog()], batch_size=3, batch_interval_ms=0)
    big = BatchStub()
    m_big = await big.collect([_catalog()], batch_size=200, batch_interval_ms=0)
    assert [t.table_name for t in m_small.tables] == [t.table_name for t in m_big.tables]
    assert [(c.table_name, c.column_name) for c in m_small.columns] == [
        (c.table_name, c.column_name) for c in m_big.columns
    ]
    assert [i.table_name for i in m_small.indexes] == [i.table_name for i in m_big.indexes]
    assert [(f.table_name, f.from_column) for f in m_small.foreign_keys] == [
        (f.table_name, f.from_column) for f in m_big.foreign_keys
    ], "外键也要分批：漏了这条就会每一批把整个 schema 的外键重取一遍"
    assert [len(b) for b in m_small.batches] == [3, 3, 3, 1]


async def test_批间隔真的让了一口气_而且第一批之前不让() -> None:
    stub = BatchStub()
    started = time.perf_counter()
    await stub.collect([_catalog()], batch_size=3, batch_interval_ms=120)
    elapsed = time.perf_counter() - started
    assert elapsed >= 0.36, f"4 批应有 3 个 120ms 间隔，实得 {elapsed:.3f}s"
    assert elapsed < 0.6, f"间隔数失控（是不是每批之前都睡了一次？）：{elapsed:.3f}s"


async def test_抽取全程只对源库发_select() -> None:
    """§4.1 的只读承诺在 PG 这一侧同样成立：源账号只有 SELECT（015 的 `demo_pg_ro`）。"""
    stub = Stub(
        [
            ("from pg_catalog.pg_class c", [_table_row("order_main")]),
            (
                "from pg_catalog.pg_attribute a",
                [_column_row("order_main", "id", typname="int4", raw="integer")],
            ),
            ("from pg_catalog.pg_index i", [_index_row("order_main", "pk_order_main", "id", 1)]),
            (
                "from pg_catalog.pg_constraint con",
                [_fk_row("order_item", "order_id", "order_main")],
            ),
        ]
    )
    await stub.collect([_catalog()], batch_size=200, batch_interval_ms=0)
    for sql, _ in stub.executed:
        normalized = " ".join(sql.split())
        assert normalized.upper().startswith(("SELECT", "SHOW")), normalized
        # 词首判定而不是子串判定：§8.2 E 的投影里有 `AS on_update` / `AS on_delete` 这两个**别名**，
        # 拿 `"UPDATE " in sql` 去查会把自己人判成凶手（MySQL 那侧的别名是 UPDATE_RULE，没这个问题）
        # `INTO` 是那道 startswith 挡不住的唯一写形状：`SELECT … INTO 新表` 以 SELECT 开头
        for banned in ("INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "TRUNCATE", "GRANT", "INTO"):
            assert not re.search(rf"(?<![A-Za-z_]){banned}\s", normalized.upper(), re.I), (
                f"{banned} 出现在：{normalized}"
            )
