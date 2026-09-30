"""卡片模板 golden 快照：`docs/verification.md` §2.1 的"卡片模板 golden | 快照"那一行。

期望值住在 `tests/fixtures/prompts/kb_card__*.expected.txt`，**按 kb-workflow §5 的模板逐行手写**，
不是从渲染器 dump 出来的：dump 只能证明"以后没变"，手写才证明"渲染出来的就是文档那一份"。

§2.1 点名的六个覆盖场景一一对应：无注释表 / 60 列宽表 / 纯视图 / 含 enum 列 / 无 PK 表 /
中文·反引号·空格表名。另加 §2.1 括号里那句"`{{ }}` 空白控制要断言（不然 diff 全是空白）"——
快照文件对不上时最难查的就是多一个空行，所以单独钉一条。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.kb_service import (
    ColumnMeta,
    IndexMeta,
    RelationMeta,
    TableMeta,
    build_cards,
)
from tests.unit.test_type_domain import assert_in_value_domain

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "prompts"
TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "app" / "prompts" / "card_template.j2"

# §6 的"单块 token ≤ 900"。这里写文档里的字面量，不从 kb_service 导入 CARD_TOKEN_LIMIT：
# 断言被测模块自己的常数只会证明"代码等于代码"，改数时两边一起动、测试永远不会红。
_TOKEN_BUDGET_PER_CARD = 900

# 工单 023 之后，卡片里出现的 `data_type` 是**归一值**（`meta_column.data_type` 现在存归一结果、
# 方言原文在 `raw_data_type`）。下面的夹具因此按归一口径取值：`varchar` 带上了长度
# （`varchar(32)`，与 `information_schema` 的 `COLUMN_TYPE` 同源）、整型不带显示宽度、
# `enum` 塌成基名。归一映射本身由 `test_pg_type_normalize.py` / `test_mysql_type_normalize.py`
# 和共享断言表 `test_type_domain.py` 钉；这里只保证"渲染器把归一后的 data_type 原样打进正文"。
_MYSQL_57 = "不支持 CTE 与窗口函数，且默认 ONLY_FULL_GROUP_BY"


def _fixture(name: str) -> str:
    """快照文件按 text 文件的惯例以换行结尾，而 `text_md` 不以换行结尾——砍掉那一个。"""
    return (FIXTURES / f"kb_card__{name}.expected.txt").read_text(encoding="utf-8").rstrip("\n")


def _no_comment_table() -> TableMeta:
    """§5 的三级降级全走左端：表注释、粒度、行数、列注释一律缺失。"""
    return TableMeta(
        full_name="ai_web_demo.t_no_comment",
        columns=(
            ColumnMeta(name="id", data_type="bigint", nullable=False, is_pk=True),
            ColumnMeta(name="note", data_type="text"),
        ),
        server_major="5.7",
    )


def _no_pk_table() -> TableMeta:
    """无 PK 表：一个都不许被凭空标成"主键"；同时覆盖【规模】的完整形态（行数 + 最近更新）。"""
    return TableMeta(
        full_name="ai_web_demo.t_no_pk",
        comment_zh="人工核对过的无主键表",
        granularity="一行 = 一个埋点事件",
        approx_rows=12345,
        last_update="2026-09-20",
        columns=(
            ColumnMeta(name="event_type", data_type="varchar(32)", comment_zh="事件类型"),
            ColumnMeta(name="ts", data_type="datetime", comment_zh="事件时间"),
        ),
        server_major="5.7",
    )


def _enum_table() -> TableMeta:
    """验收 ② 的那六个值 + 默认值 + 唯一列 + 索引段 + 关系段（extracted 在前）。"""
    return TableMeta(
        full_name="ai_web_demo.order_main",
        comment_zh="订单主表",
        granularity="一行 = 一笔订单",
        approx_rows=8642,
        columns=(
            ColumnMeta(
                name="id", data_type="bigint", nullable=False, is_pk=True, comment_zh="订单ID"
            ),
            ColumnMeta(
                name="order_no",
                data_type="varchar(32)",
                nullable=False,
                is_unique=True,
                comment_zh="订单号",
            ),
            ColumnMeta(
                name="status",
                data_type="enum",
                nullable=False,
                comment_zh="订单状态",
                default="pending",
                enum_values=(
                    "pending",
                    "paid",
                    "shipped",
                    "completed",
                    "cancelled",
                    "refunding",
                ),
            ),
            ColumnMeta(name="user_id", data_type="bigint", nullable=False, comment_zh="下单用户"),
            ColumnMeta(name="product_id", data_type="bigint", comment_zh="商品ID"),
        ),
        indexes=(
            IndexMeta(name="PRIMARY", type="BTREE", columns=("id",), unique=True),
            IndexMeta(name="uk_order_no", type="BTREE", columns=("order_no",), unique=True),
            IndexMeta(name="idx_status", type="BTREE", columns=("status",)),
        ),
        relations=(
            RelationMeta(from_column="user_id", to_table_full="ai_web_demo.user", to_column="id"),
            RelationMeta(
                from_column="product_id",
                to_table_full="ai_web_demo.product",
                to_column="id",
                kind="inferred",
                confidence=0.7,
            ),
        ),
        server_major="5.7",
    )


def _view_table() -> TableMeta:
    """verification §1 的考点：视图没有 TABLE_ROWS，【规模】整行降级，表名带（视图）。"""
    return TableMeta(
        full_name="ai_web_demo.v_daily_sales",
        table_type="VIEW",
        comment_zh="每日销售汇总",
        granularity="一行 = 一天",
        columns=(
            ColumnMeta(name="day", data_type="date", comment_zh="统计日"),
            ColumnMeta(name="order_count", data_type="bigint", comment_zh="订单数"),
            ColumnMeta(name="gmv", data_type="numeric(18,2)", comment_zh="成交额"),
        ),
        server_major="5.7",
    )


def _odd_identifier_table() -> TableMeta:
    """中文 + 空格表名 + 反引号列名：标识符必须**原样**出现在文本里，转义是模型的事，
    但前提是它看得见原始名字。顺带盖住非 mysql 5.7 的【方言】分支（不拼那句约束）。
    """
    return TableMeta(
        full_name="public.订单 a b",
        dialect_name="postgresql",
        comment_zh="中文带空格的表名",
        approx_rows=1_000_000,
        columns=(
            ColumnMeta(
                name="id", data_type="bigint", nullable=False, is_pk=True, comment_zh="主键"
            ),
            ColumnMeta(name="`折扣`", data_type="numeric", comment_zh="折扣率"),
        ),
        server_major="15",
    )


def _wide_table() -> TableMeta:
    """60 列 > 40 → §6 的主卡（前 25 列 + 全部 PK/索引/外键列）+ 每 30 列一张切片。

    `c60` 既是索引列又是外键列，所以它会**跳过**中间 34 列被提进主卡末尾；
    切片序号因此是 26-55 / 56-59，而不是"每 30 列连续切"。
    """
    columns = (
        ColumnMeta(name="c1", data_type="int", nullable=False, is_pk=True, comment_zh="主键"),
        *(ColumnMeta(name=f"c{i}", data_type="int", comment_zh=f"指标{i}") for i in range(2, 61)),
    )
    return TableMeta(
        full_name="ai_web_demo.product_stats_wide",
        comment_zh="商品统计宽表",
        granularity="一行 = 一个商品的汇总",
        approx_rows=1024,
        columns=columns,
        indexes=(IndexMeta(name="idx_c60", type="BTREE", columns=("c60",)),),
        relations=(
            RelationMeta(from_column="c60", to_table_full="ai_web_demo.product", to_column="id"),
        ),
        server_major="5.7",
    )


_NARROW: list[tuple[str, TableMeta]] = [
    ("t_no_comment", _no_comment_table()),
    ("t_no_pk", _no_pk_table()),
    ("order_main", _enum_table()),
    ("v_daily_sales", _view_table()),
    ("odd_identifier_pg", _odd_identifier_table()),
]


@pytest.mark.parametrize("name,table", _NARROW, ids=[name for name, _ in _NARROW])
def test_窄表一张卡正文与golden逐字符相同(name: str, table: TableMeta) -> None:
    docs = build_cards(table)
    assert [d.kind for d in docs] == ["table"], "≤40 列不该切片（§6 第一行）"
    assert docs[0].text_md == _fixture(name)


# 快照那份是手写的，类型字面只可能从**夹具取值**这一侧漂走：漏改一个 `decimal(18,2)`，
# 正文就渲染出一个 023 之后库里再也不可能出现的字面，而逐字符比对照样是绿的（两侧一起错）。
# 所以把共享断言表直接套到夹具上——测试 id 给表名，断言消息给类型字面。
_ALL_CASES: list[tuple[str, TableMeta]] = [*_NARROW, ("product_stats_wide", _wide_table())]


@pytest.mark.parametrize("name,table", _ALL_CASES, ids=[name for name, _ in _ALL_CASES])
def test_夹具里的每个类型字面都在归一值域内(name: str, table: TableMeta) -> None:
    for col in table.columns:
        assert_in_value_domain(col.data_type)


def test_宽表主卡与两张切片卡各自对golden() -> None:
    docs = build_cards(_wide_table())
    assert [(d.kind, d.seq) for d in docs] == [
        ("table", 0),
        ("table_columns", 1),
        ("table_columns", 2),
    ]
    assert [d.text_md for d in docs] == [
        _fixture("product_stats_wide"),
        _fixture("product_stats_wide__seq1"),
        _fixture("product_stats_wide__seq2"),
    ]
    # §6 的预算是"每块"的，不是整张表的：任何一块超 900 都会被 prompt 组装裁掉半张卡
    assert all(d.token_count <= _TOKEN_BUDGET_PER_CARD for d in docs)
    # §6 末：切片必须重复表头块，否则切片自身无判别力
    assert all(d.text_md.startswith("【表】ai_web_demo.product_stats_wide") for d in docs)


_CASES: list[tuple[str, TableMeta]] = [*_NARROW, ("product_stats_wide", _wide_table())]


@pytest.mark.parametrize("table", [table for _, table in _CASES], ids=[name for name, _ in _CASES])
def test_渲染文本里没有空行与行首尾空白(table: TableMeta) -> None:
    """§2.1 括号里那句"`{{ }}` 空白控制要断言"。

    Jinja 的块标签自带换行，`trim_blocks`/`lstrip_blocks`/`-%}` 少配一个就多一个空行；
    这种差异在逐行断言下全是噪声，只在快照对比时表现为"整段偏移"，最难查。
    """
    for doc in build_cards(table):
        assert doc.text_md == doc.text_md.strip("\n"), "正文首尾不许有裸换行"
        lines = doc.text_md.splitlines()
        assert all(line.strip() for line in lines), [line for line in lines if not line.strip()]
        assert all(line == line.rstrip() for line in lines), "行尾空白会污染 search_text"
        assert all(not line.startswith((" ", "\t")) for line in lines)


def test_docs里那段jinja模板与渲染用的模板文件逐行相同() -> None:
    """kb-workflow §5 的围栏块声明自己是 `card_template.j2`，那就必须真的是它。

    双轴审查一次抓出三处字面漂移（视图标记前的空格、`共 N 个` 取哪一层的列数、
    `{% if %}` 与 `~` 拼出来的空白差别），而三份手写 golden 全都跟着**代码**那份写，
    于是"按文档手写"的 oracle 悄悄退化成了"按代码手写"。这条用例把退化堵死：
    模板改了文档没改，这里先红。
    """
    doc = (Path(__file__).resolve().parents[3] / "docs" / "kb-workflow.md").read_text("utf-8")
    lines = doc.splitlines()
    start = lines.index("```jinja") + 1
    block = lines[start : lines.index("```", start)]
    template = TEMPLATE_PATH.read_text("utf-8").rstrip("\n").splitlines()
    assert len(block) == len(template), f"行数就不等：文档 {len(block)} / 模板 {len(template)}"
    for i, (doc_line, tpl_line) in enumerate(zip(block, template, strict=True), 1):
        assert doc_line == tpl_line, f"第 {i} 行不一致\n文档：{doc_line}\n模板：{tpl_line}"


def test_方言约束句只挂在mysql57上() -> None:
    """§5 最后一条设计要点：把 5.7 的约束带进卡片。非 5.7 不许带上这句假话。"""
    assert _MYSQL_57 in build_cards(_no_pk_table())[0].text_md
    pg = build_cards(_odd_identifier_table())[0].text_md
    assert pg.splitlines()[-1] == "【方言】postgresql 15"
