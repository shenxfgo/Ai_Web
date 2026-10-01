"""MySQL 读侧真分批（工单 020）：切片的边界、按批发的那几条查询、批间的让气。

接缝与口径：
- `slice_names` 是纯函数，工单 020 验收 2 点名的三个边界（整除、末批只剩一张、`batch > N`）
  全在这里钉，因为它们与"发几条 SQL"无关，红起来只可能是切片本身错了。
- `collect()` 的批语义钉在**假连接**上：`_rows` 是方言层唯一的 I/O 出口（同
  `test_extract_mysql_client.py` 那条理由），所以"每条昂贵查询收到哪几个表名"这件事
  在这里看得见，在真库上只能靠抓语句。
- 期望值出处：`docs/metadata-model.md` §8.1（B/C/D/E 四条的分工）、§6 的 `MAX_TABLES`
  "在昂贵的列/索引查询之前判"、工单 020 的已定口径（默认 200/100 不改、具名绑定参数不许
  换成拼接、分批不改变提交边界）。真库那 4 批的实样归 `test_extract_mysql_live_batches.py`。
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence

import pytest

from app.core.errors import ExtractScopeTooLarge
from app.extractor.base import ConnectionSpec, SourceManifest
from app.extractor.mysql import (
    MySQLExtractor,
    slice_names,
)
from tests.unit.test_extract_mysql_client import _catalog, _column_row, _index_row, _table_row

# 演示库排除下划线前缀后**可见的 10 个业务对象**，名单按 `docs/verification.md` §1/§1.2 的
# as-built 逐字抄（9 张 BASE TABLE + 1 张 VIEW，另有内部标记表 `_aiweb_demo_marker` 被
# `exclude_tables=["\\_%"]` 排除）。字典序取自 `SHOW FULL TABLES` 的实测顺序，不是这里排的——
# §8.1 B 没有 `ORDER BY`，交付顺序不是承诺，所以这份名单只用来给假件一个稳定的输入。
VISIBLE = (
    "category",
    "customer",
    "order_item",
    "order_main",
    "payment_record",
    "product",
    "product_stats_wide",
    "refund_record",
    "user_activity_log",
    "v_daily_sales",
)


def test_整除时不产生空末批() -> None:
    assert slice_names(list("abcdef"), 3) == [["a", "b", "c"], ["d", "e", "f"]]


def test_末批只剩一张也要单独成批() -> None:
    """`batch=3`、N=7 时最后那一张不许被并进上一批，也不许被丢掉。

    丢掉它就是"抽了 7 张表只报 6 张"，而这一份名单同时是事件负载里"这批抽了谁"的原文。
    """
    assert slice_names(list("abcdefg"), 3) == [["a", "b", "c"], ["d", "e", "f"], ["g"]]


def test_批大小大于对象数时退化成一批而不是空批() -> None:
    """默认 `batch_size=200` 在演示库上就是这个分支：一批、不是 200 批，也不是 0 批。"""
    assert slice_names(["a", "b"], 200) == [["a", "b"]]
    assert slice_names([], 3) == [], "空范围由调用方提前短路，这里不许造出一个空批"


def test_批大小不是正整数就当场抛() -> None:
    """`batch_size=0` 会让切片变成死循环或空清单，两种都比抛错难查。"""
    for bad in (0, -3):
        with pytest.raises(ValueError):
            slice_names(["a"], bad)


# ------------------------------------------------------------------ collect 的批语义


class BatchStub(MySQLExtractor):
    """按**收到的表名**现算假数据的桩件。

    和 `test_extract_mysql_client.Stub` 的分别很重要：那一份按语句匹配、每条语句永远回同一份
    假数据，用它测分批看不出"第二批发的是哪些表"。这一份从 `:tbl_N` 绑定参数里读回调用方
    自己声明的范围，所以"批与批的 IN 名单互不重叠、并起来是全集"这句话才真的被测到——
    而它同时是验收 5（分批不改变落库结果）在这一层的等价物。
    """

    def __init__(self, names: Sequence[str] = VISIBLE) -> None:
        super().__init__(ConnectionSpec(host="127.0.0.1", port=3306, user="aiweb_ro", password="p"))
        self._names = list(names)
        self.executed: list[tuple[str, dict[str, object]]] = []

    def _rows(self, sql: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
        normalized = " ".join(sql.lower().split())
        p = dict(params or {})
        self.executed.append((sql, p))
        if normalized.startswith("select version()"):
            return [{"v": "5.7.17-log"}]
        if normalized.startswith("select @@character_set_results"):
            return [{"cs": "utf8mb4"}]
        if normalized.startswith("select @@max_execution_time"):
            return [{"max_execution_time": 0}]
        if "information_schema.tables t" in normalized:
            # B 不认识表名清单（它按 schema + 范围过滤拿全量），这正是它不能被"分批"的原因
            return [_table_row("ai_web_demo", name) for name in self._names]
        # 按**绑定参数的序号**还原调用方声明的顺序（`:tbl_0` 在前），不按名字排序：
        # 批与批之间唯一有保证的是"这一批问了哪几张"，把它们排成字典序就等于替被测方
        # 发明了它并没有的顺序。
        asked = [
            v for _, v in sorted((int(k[4:]), str(v)) for k, v in p.items() if k.startswith("tbl_"))
        ]
        if "information_schema.statistics s" in normalized:
            return [_index_row(name, "PRIMARY", "id") for name in asked]
        if "information_schema.columns c" in normalized:
            return [_column_row(name, "id") for name in asked]
        if "information_schema.key_column_usage k" in normalized:
            return []
        raise AssertionError(f"这条 SQL 没有准备假数据：{normalized}")

    def batched_names(self, needle: str) -> list[list[str]]:
        """含 `needle` 的每条语句各自被要求了哪些表名（按发出顺序）。"""
        out: list[list[str]] = []
        for sql, params in self.executed:
            if needle in sql.lower():
                out.append(sorted(str(v) for k, v in params.items() if k.startswith("tbl_")))
        return out


async def _collect(stub: BatchStub, **kw: object) -> SourceManifest:
    return await stub.collect([_catalog()], **kw)  # type: ignore[arg-type]


async def test_三张一批时列索引外键各发四条_且名单互不重叠() -> None:
    """工单 020 验收 3 的形状：三条昂贵查询的**发数**跟着批数走，收到的名单跟着批走。

    只把表清单分批、列查询仍一把 `IN` 全量，是本片最容易假绿的形状（已定口径原文），
    所以这里断的不是"总共发了 12 条"，而是每一条各自问了哪三张表。
    """
    stub = BatchStub()
    await _collect(stub, batch_size=3, batch_interval_ms=0)

    # 四刀逐个写死，不写 `slice_names(VISIBLE, 3)`：用被测的那把刀去算刀口，
    # 红了也只是"刀和期望值一起改错了"。
    expected = [
        ["category", "customer", "order_item"],
        ["order_main", "payment_record", "product"],
        ["product_stats_wide", "refund_record", "user_activity_log"],
        ["v_daily_sales"],
    ]
    assert [len(b) for b in expected] == [3, 3, 3, 1], "10 / 3 = 4 批，末批只剩 1 张"
    for needle in (
        "information_schema.columns c",
        "information_schema.statistics s",
        "information_schema.key_column_usage k",
    ):
        assert stub.batched_names(needle) == expected, needle


async def test_昂贵查询仍然只用具名绑定参数() -> None:
    """已定口径：把表名拼进 SQL 文本是注入面，直接判不合格。

    断两面：占位符与这一批的表名**逐一对应**（多一个少一个都不行），而且语句文本里
    **一个真表名都不出现**——只断前者的话，"`IN (:` + 拼接)"这种形状照样能过。
    B 那条不在射程内：它按 schema + 范围过滤拿全量，本来就没有 `IN` 清单
    （这也是它不能被"分批"的原因）。
    """
    stub = BatchStub()
    await _collect(stub, batch_size=3, batch_interval_ms=0)
    expensive = [
        (sql, params)
        for sql, params in stub.executed
        if any(t in sql.lower() for t in ("statistics s", "columns c", "key_column_usage k"))
    ]
    assert len(expensive) == 12, "4 批 × 三条昂贵查询，一条都不许多"
    for sql, params in expensive:
        low = " ".join(sql.lower().split())
        names = [v for k, v in params.items() if k.startswith("tbl_")]
        # 占位符**逐一对应**这一批的表名：多一个是"问了没打算抽的表"，少一个是"这一批漏了表"。
        # 末批只有 2 张，所以这里跟着 params 算而不是写死 :tbl_2。
        assert set(re.findall(r":tbl_\d+", low)) == {f":tbl_{i}" for i in range(len(names))}, low
        for name in VISIBLE:
            assert name not in low, f"表名被拼进了语句文本：{name}"
        assert len(names) in (3, 1), params


async def test_一批装得下时昂贵查询各发一条_与分批前的形状相同() -> None:
    """`batch_size=200`（默认值）在 10 个对象上必须退回 P2 的形状：一 schema 四条。

    这一条是防"分批把自己变成永远多发"的：退化路径不批处理，就不该有第 5 条语句。
    """
    stub = BatchStub()
    await _collect(stub, batch_size=200, batch_interval_ms=0)
    sources = [
        " ".join(sql.lower().split()).split(" from ")[1].split()[0]
        for sql, _ in stub.executed
        if "information_schema" in sql.lower()
    ]
    assert sources == [
        "information_schema.tables",
        "information_schema.statistics",
        "information_schema.columns",
        "information_schema.key_column_usage",
    ]


async def test_分批不改变归并结果_列索引一条都不多也不丢() -> None:
    """验收 5 在方言层的等价物：批是**发送方式**，不是第二套账。

    同一份假数据，批大小 3 与 200 各自抽出来的对象必须逐个相同——顺序也要相同，因为
    下游的 `meta_column` 行数与卡片文本都要跟着它走（真库那一份比对在 live 用例里）。
    """
    small = BatchStub()
    m_small = await _collect(small, batch_size=3, batch_interval_ms=0)
    big = BatchStub()
    m_big = await _collect(big, batch_size=200, batch_interval_ms=0)
    assert [t.table_name for t in m_small.tables] == [t.table_name for t in m_big.tables]
    assert [(c.table_name, c.column_name) for c in m_small.columns] == [
        (c.table_name, c.column_name) for c in m_big.columns
    ]
    assert [i.table_name for i in m_small.indexes] == [i.table_name for i in m_big.indexes]


async def test_批次信息随_manifest_交回调用方() -> None:
    """事件负载里"这一批抽了哪些表"的原料就是这一格（工单 020 的 涉及层·事件侧）。

    方言层不写事件（它在 services），所以它交出的必须是**结构化的批名单**而不是一个数：
    只交 4 的话，前端那一帧就没法说清"这 4 批里到底哪张表在哪一批"。
    """
    stub = BatchStub()
    manifest = await _collect(stub, batch_size=4, batch_interval_ms=0)
    assert manifest.batches == [
        ["category", "customer", "order_item", "order_main"],
        ["payment_record", "product", "product_stats_wide", "refund_record"],
        ["user_activity_log", "v_daily_sales"],
    ]
    assert [len(b) for b in manifest.batches] == [4, 4, 2]


async def test_超限判断仍在昂贵的列查询之前_分批不许把它挪后() -> None:
    """已定口径：`MAX_TABLES` 是给源库省力的，批大小不是把它推后的理由。"""
    stub = BatchStub()
    with pytest.raises(ExtractScopeTooLarge):
        await _collect(stub, batch_size=2, batch_interval_ms=0, max_tables=5)
    sources = [sql.lower() for sql, _ in stub.executed if "information_schema" in sql.lower()]
    assert len(sources) == 1, "只有 B 那一条：C/D/E 一条都不该发出去"


async def test_批间隔真的让了一口气_而且第一批之前不让() -> None:
    """验收 4：`batch_interval_ms` 有读取点且真等待——用**耗时下限**断，不 grep 键名。

    10 个 / 批 3 = 4 批 → 批间有 3 个间隔。取 120ms 是为了让下限（360ms）离 CI 的抖动
    足够远，同时又不至于把整轮闸拖慢；第一批之前不许睡——那是"作业开始了但什么都没发"
    的空白，进度条会先卡住再动。
    """
    stub = BatchStub()
    started = time.perf_counter()
    await _collect(stub, batch_size=3, batch_interval_ms=120)
    elapsed = time.perf_counter() - started
    assert elapsed >= 0.36, f"4 批应有 3 个 120ms 间隔，实得 {elapsed:.3f}s"
    assert elapsed < 0.6, f"间隔数失控（是不是每批之前都睡了一次？）：{elapsed:.3f}s"
