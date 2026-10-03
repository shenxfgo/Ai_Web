"""枚举 NDV 采样（工单 033）：候选筛选、阈值裁决、两方言的 SQL 形状。

接缝与口径：
- `app/extractor/sampling.py` 是纯函数层——它不连库、不认识 ORM，工单 033 验收 1 里
  "阈值 +1 / -1 两个边界"与"NULL 桶不算一个值"这种事在这里钉，因为它们与发几条 SQL 无关。
- 两方言的采样 SQL 钉在**假连接**上（`_rows` 是方言层唯一的 I/O 出口，同
  `test_extract_mysql_batching.py` 那条理由），真库的形状交给 live 那两条。
- 期望值出处：`docs/kb-workflow.md` §8 那三步的原文（第一步的类型清单、第二步的
  `GROUP BY … LIMIT`、第三步"通过的列写 `enum_values` 与 `sample_values`"）、
  `docs/metadata-model.md` §2.4 那两格的注释、工单 033 的已定口径（默认关、
  不进 `collect()`、`LIMIT` 不许写成字面量 40、只记基数不记值域）。
"""

from __future__ import annotations

import re

import pytest

from app.extractor.base import ColumnSample, ConnectionSpec, Row, SampleTarget
from app.extractor.mysql import MySQLExtractor
from app.extractor.mysql import build_sql_sample as build_mysql_sample
from app.extractor.postgres import PostgresExtractor
from app.extractor.postgres import build_sql_sample as build_pg_sample
from app.extractor.sampling import (
    adjudicate,
    is_sample_candidate,
    ndv_query_limit,
    quote_ident,
    render_sample_value,
    rows_to_sample,
)

# 两方言的采样语句构造器：与 `test_sync_dialect_dispatch.TABLES_SQL` 同一条纪律——
# 测试自己按 kind 查表，不从 `_DIALECTS` 里拿（从被测那张表读出的构造器去验同一张表，
# 红了也只是"两格一起改错了"）。
DIALECT_BUILDERS: dict[str, object] = {
    "mysql": build_mysql_sample,
    "postgres": build_pg_sample,
}

# 三条名字各自带引号出现在语句里的样子——写死两份而不是从 `quote_ident` 算，
# 是为了让"名字进了文本"这件事在两方言里各有实物可对。
SAMPLE_FRAGMENTS: dict[str, tuple[str, ...]] = {
    "mysql": ("`ai_web_demo`.`order_main`", "`status` AS v"),
    "postgres": ('"ai_web_demo"."order_main"', '"status" AS v'),
}

# §8 第一步的六个 `data_type` 字面。写在这里而不是从实现里读，是因为这一份的**出处是文档**：
# 实现把 `char` 换成 `bpchar` 之类"看着等价"的改动，必须在这里红，而不是跟着实现一起漂。
DOC_CANDIDATES = ("enum", "set", "varchar", "char", "boolean", "tinyint")

# 两个排除位**故意不给默认值**（同 020 给 `batch_size` 不留默认的那条纪律）：给了默认就是
# "忘了传的调用点在扫生成列 / 在覆盖定义给的值域"，而这两种都是安静错。类型清单那几条用例
# 想说的只有"这一列是什么类型"，所以从这里取一份显式的普通列——这份是测试自己写的字面，
# 不是从实现里读出来的。
PLAIN: dict[str, bool] = {"is_generated": False, "has_declared_enum": False}


@pytest.mark.parametrize("doc_name", DOC_CANDIDATES)
def test_第8条第一步点名的六类都是候选(doc_name: str) -> None:
    """文档说挑这六类，实现就得认这六类。

    `boolean` 传的是**归一前**那个字面：023 之后 `meta_column.data_type` 里存的是归一值
    （`bool`），而 §8 那行写的是 IS 的 `DATA_TYPE`。这一条把两边对上——认不上就是翻译丢了。
    """
    assert is_sample_candidate(doc_name, **PLAIN)


def test_bool_是_boolean_归一后的写法_所以同样认() -> None:
    """工单 033 的候选判据读的是**库里那一格**（归一后的 `data_type`），不是 IS 原文。

    MySQL 的 `tinyint(1)` 与 PG 的 `boolean` 在 023 之后都归成 `bool`（§9 拍板），
    只认 `boolean` 那个字面的话，演示库里 `order_main.paid` 这一类列就一个都进不了候选——
    而 §8 第一步明明把它算在"低基数短值"里。
    """
    assert is_sample_candidate("bool", **PLAIN)


@pytest.mark.parametrize(
    "data_type",
    ["text", "longtext", "int", "bigint", "numeric(10,2)", "datetime", "json", "bytea", "uuid"],
)
def test_清单之外的类型一概不认(data_type: str) -> None:
    """§8 那句"只挑 enum/短文本，且低基数"里的**排除**那一半。

    `text`/`longtext` 是"短文本"的反面，`int`/`numeric` 是"低基数枚举"的反面（它们是量度），
    `datetime`/`json` 同。这一条红了意味着采样开始扫全表能扫的东西，那是同步时长的事故。
    """
    assert not is_sample_candidate(data_type, **PLAIN)


def test_带长度的归一写法按基名判() -> None:
    """023 之后 `data_type` 存的是 `varchar(64)` 这种**带修饰**的归一值。

    不做这一步的话，演示库里绝大多数短文本列都在清单之外（它们的存法就是 `varchar(64)`），
    而 §8 第一步写的是 `varchar`——两个说法要靠这一格对上，且只能在这一格对。
    """
    assert is_sample_candidate("varchar(64)", **PLAIN)
    assert is_sample_candidate("char(2)", **PLAIN)


def test_归一值里带数组后缀的不认() -> None:
    """PG 的 `text[]` / `varchar(64)[]` 归一后带 `[]`：那是数组，不是"这一列的取值"。

    对数组列发 `GROUP BY col` 会把整个数组当一个值，NDV 判定当场失真。
    """
    assert not is_sample_candidate("character varying[]", **PLAIN)
    assert not is_sample_candidate("varchar(64)[]", **PLAIN)
    assert not is_sample_candidate("_varchar", **PLAIN)


def test_生成列不扫() -> None:
    """生成列的值是**别的列算出来的**，扫它等于把同一份数据数第二遍。

    更实际的一条：MySQL 的 STORED 生成列在源库上可能带 `GENERATED ALWAYS AS` 表达式，
    对它 `GROUP BY` 会表达式求值，而这一片要的是"这列本来存着什么值"。
    """
    assert not is_sample_candidate("varchar(32)", is_generated=True, has_declared_enum=False)


def test_已经从定义拿到值域的列不覆盖() -> None:
    """真 ENUM/Set 列的值域来自**定义**（§8.1 C 的 `enum_def` / §8.2 C 的 `pg_enum`），
    来自观测的那一份永远更弱：它只覆盖本轮扫过的那 `row_limit` 行，一个合法但暂时没人用的
    取值就会从卡片上消失。所以这一格不参与采样——它本来就该在候选之外（工单 033 的字面只说
    "非 ENUM 的短文本列首次出现值域"，这一条是它反过来那半）。
    """
    assert not is_sample_candidate("enum", is_generated=False, has_declared_enum=True)
    assert not is_sample_candidate("varchar(32)", is_generated=False, has_declared_enum=True)


# ------------------------------------------------------------ §8 第二步：阈值裁决


def test_阈值之内写值域_按频次降序同频按字面升序() -> None:
    """§8 第二步"一旦超过阈值即放弃该列"的反面：没超就写，写出来的次序是**频次**。

    高频在前对 prompt 更有用（模型先看常见取值）；同频按字面升序是为了 determinism——
    没有这一条时"两个值各出现 3 次"的次序由源库返回顺序决定，卡片 golden 会随机漂。
    """
    result = adjudicate([("待支付", 9), ("已完成", 30), ("已取消", 30)], max_distinct=30)
    assert result is not None
    # 同频那一档比的是**码位**：`已取消` 的第二字 U+53D6 在 `已完成` 的 U+5B8C 前面，
    # 所以它是这一档的第一名——次序由规则算得出，不是跟着实现跑。
    assert result.enum_values == ("已取消", "已完成", "待支付")


def test_恰好等于阈值仍写_超一个就不写() -> None:
    """验收 4 点名的那两个边界：**阈值本身是"含"**（§2.4 的 `NDV ≤ 该值才视为枚举列`）。

    两档各一条而不是只测超出的那一档，是因为"写成 `<`"这种差一错误只会红在这里。
    """
    at = adjudicate([("a", 1), ("b", 1), ("c", 1)], max_distinct=3)
    over = adjudicate([("a", 1), ("b", 1), ("c", 1), ("d", 1)], max_distinct=3)
    assert at is not None
    assert at.enum_values == ("a", "b", "c")
    assert over is None


def test_null_桶占一个分组但不进值域() -> None:
    """`GROUP BY col` 会为 NULL 单独返一组，它数进 NDV（那是真实分组数），但**不是取值**。

    `enum_values` 的形状是 `list[str]`，卡片模板把它拼成「取值: a、b」；NULL 进去会变成
    "这一列能取 NULL" 这种对写 SQL 毫无帮助的话——能不能取 NULL 由 `nullable` 那一格说。
    """
    result = adjudicate([("已完成", 12), (None, 4)], max_distinct=30)
    assert result is not None
    assert result.enum_values == ("已完成",)


def test_全是_null_时不写空值域() -> None:
    """扫窗内全是 NULL ⇒ 值域是空串那一种"没采到"，写 `[]` 等于承诺"这列没有取值"。

    留 NULL 才分得开"这列观测不到值"与"这列的取值就是零个"——后者在源库里不存在，
    所以那种形状一进卡片就只能是假的。
    """
    assert adjudicate([(None, 7)], max_distinct=30) is None


def test_去重后才算阈值() -> None:
    """方言按 §8 那句 `GROUP BY col` 返的是一组一值，但裁决不依赖它一定去过重。

    这一条钉的是"阈值数的是**不同值个数**，不是行数"：`[("a",1),("a",1)]` 是 NDV 1。
    写成 `len(rows)` 的实现会在这里红，而那正是"把 LIMIT 当成阈值"那类混法。
    """
    result = adjudicate([("a", 1), ("a", 1), ("b", 2)], max_distinct=2)
    assert result is not None
    # 两条 `("a",1)` 是同一个分组被重复交回：合并后 a 与 b 各 2 次，NDV 仍是 2；
    # 同频 → 按字面升序，所以是 a 在前。
    assert result.enum_values == ("a", "b")


def test_两格今天写的是同一份值域() -> None:
    """§8 第三步原文："通过的列写入 `enum_values` 与 `sample_values`"——没说两者怎么不同。

    §2.4 那两格的注释给的差别是**读者**（前者进卡片「取值:」段，后者是界面展示的样本），
    不是内容。截断的数（"少量"是几条）在 spec、§8、roadmap 里都没有出处，所以这一片
    **不发明**一个常量：两格同值同序。已记入工单 033 交付记录的留白，034 收口时再判。
    """
    result = adjudicate([("已完成", 30), ("待支付", 9)], max_distinct=30)
    assert result is not None
    assert result.sample_values == result.enum_values == ("已完成", "待支付")


@pytest.mark.parametrize("max_distinct", [0, -1])
def test_阈值非正数当场抛而不是安静不写(max_distinct: int) -> None:
    """`MAX_DISTINCT=0` 的两种"合理"落地都坏：全列放弃（看起来同步成功、什么都没采）或全列通过。

    同 `slice_names` 对 `batch_size < 1` 的处理：这是配置写错了，不是"这一列不合适"。
    """
    with pytest.raises(ValueError):
        adjudicate([("a", 1)], max_distinct=max_distinct)


# ------------------------------------------------------------ 两方言共用的采样小工具


def test_ndv_那条_limit_是阈值加余量而不是字面量() -> None:
    """工单 033 的已定口径：§8 第二步原文那个 `LIMIT 40` 不许写成字面量。

    `40` 在默认阈值 30 下恰好等于"阈值 + 10"，但 `MAX_DISTINCT` 一改它就成了假话。
    期望值 40 是**从 §8 那行字面抄来的**（阈值 30 时它就是 40），不是 `30 + 10` 现算——
    现算等于拿被测的那把刀量刀口。
    """
    assert ndv_query_limit(30) == 40


def test_阈值一改_limit_跟着改() -> None:
    """上面那条钉住"默认值下等于文档字面 40"，这一条钉住"它不是 40"。

    两条一起才抓得住"把 40 抄成常量"那种写法：只写第二条的话，第一条会跟着一起漂。
    """
    assert ndv_query_limit(3) == 13
    assert ndv_query_limit(3) != ndv_query_limit(30)


@pytest.mark.parametrize("quote,expected", [("`", "`order_main`"), ('"', '"order_main"')])
def test_标识符按各自方言的引号包住(quote: str, expected: str) -> None:
    assert quote_ident("order_main", quote=quote) == expected


def test_引号出现在名字里时按_doubling_转义() -> None:
    """表名/列名是从源库的 IS 读回来的，不是我们挑的字面：里面出现引号字符就得成对写。

    这一条的存在理由不是"演示库里有这种表"，而是"另一种写法就是注入出口"——
    只剥掉引号字符也能挡住，但那样会在源库真有一个 ``weird`name`` 时把数据采到**别的**表上。
    """
    assert quote_ident("we`ird", quote="`") == "`we``ird`"
    assert quote_ident('we"ird', quote='"') == '"we""ird"'


def test_空标识符当场抛而不是发出_两个引号() -> None:
    """`""` 在 PG 里是一个真实存在的表名（空标识符被引号救活），MySQL 直接语法错。

    两种都不是"这一张表"，所以这里就抛。
    """
    with pytest.raises(ValueError):
        quote_ident("", quote='"')


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("已完成", "已完成"),
        (None, None),
        (True, "true"),
        (False, "false"),
        (1, "1"),
        (b"\xe5\xb7\xb2\xe5\xae\x8c\xe6\x88\x90", "已完成"),
    ],
)
def test_值文本化按_sql_字面那一份来(raw: object, expected: str | None) -> None:
    """`enum_values` 存的是**能被写进 SQL 的字面**，不是 Python 的 `repr`。

    三档各有出处：
    - `bool → true/false`：PG 的布尔字面就是这两个（写 `True` 源库不认），
      而 MySQL 根本没有布尔类型（`tinyint(1)` 回的是 int 1/0，走数字那一档）。
    - `bytes → 按 UTF-8 解`：MySQL 在 `binary`/`varbinary` 字符序下回 bytes，
      而候选清单里有 `char`——同一列在不同表上字符序可以不同。
    - `None → None`：§8 的 NULL 桶由 `adjudicate` 丢，文本化这一层不许把它变成 `"None"`，
      那样它就从"这一列没值"变成了"这一列能取字面 None"。
    """
    assert render_sample_value(raw) == expected


# ------------------------------------------------------------ 两方言的采样 SQL 形状
#
# 这里的期望值是**手写的语句原文**，出处两条：§8 第二步那句
# `SELECT col, COUNT(*) ... GROUP BY col LIMIT` 的字面，加上工单 033 的已定口径
# "内层还得有 `SAMPLE_ROW_LIMIT` 那道扫描上限"（否则第三键就没有读取点）。
# 两方言的差别只有引号字符——这一点本身也要被看见，所以两份literal 都写全而不是差分断言。

MYSQL_SAMPLE_SQL = (
    "SELECT win.v AS sample_value, COUNT(*) AS n "
    "FROM (SELECT `status` AS v FROM `ai_web_demo`.`order_main` LIMIT :row_limit) AS win "
    "GROUP BY win.v "
    "LIMIT :ndv_limit"
)

PG_SAMPLE_SQL = (
    "SELECT win.v AS sample_value, COUNT(*) AS n "
    'FROM (SELECT "status" AS v FROM "public"."order_main" LIMIT :row_limit) AS win '
    "GROUP BY win.v "
    "LIMIT :ndv_limit"
)

# (方言, 构造器, 表所在 schema, 手写好的语句原文)
SAMPLE_DIALECTS = [
    ("mysql", build_mysql_sample, "ai_web_demo", MYSQL_SAMPLE_SQL),
    ("postgres", build_pg_sample, "public", PG_SAMPLE_SQL),
]


def _norm(sql: str) -> str:
    return " ".join(sql.split())


@pytest.mark.parametrize(("kind", "build", "schema", "expected"), SAMPLE_DIALECTS)
def test_采样语句逐字对得上_先扫窗再分组(
    kind: str, build: object, schema: str, expected: str
) -> None:
    """验收 7 的"两方言各自一条 SQL 形状用例"：整条语句逐字钉，不做差分断言。

    为什么内层要套一个带 `LIMIT` 的派生表而不是直接 `GROUP BY col LIMIT 40`：
    §8 那行的字面确实没有内层，但那样 `SAMPLE_ROW_LIMIT` 就没有落点了（验收 1 要它
    "改配置 → 行为变"）。内层是**扫描窗口**、外层是**分组上限**，两个数各管一件事，
    合不起来——写成 `GROUP BY col LIMIT :row_limit` 那种"看着也像"的形状会当场红在这里。
    """
    sql, _ = build(schema, "order_main", "status", max_distinct=30, row_limit=1000)
    assert _norm(sql) == expected, kind


@pytest.mark.parametrize("kind", sorted(DIALECT_BUILDERS))
def test_两个键各自真的进语句_阈值那一边是表达式(kind: str) -> None:
    """验收 1 在方言层的那一半：行数上限与阈值**分别**能被动出来。

    `(1000, 30) → (25, 13)`：`25` 是 `row_limit` 原样进内层，`13` 是 `ndv_query_limit(3)`
    算进外层。如果实现把 `40` 抄成字面量，第二档就还是 40（工单里点名的那条假话）；
    如果它把两个数写成同一个，`25 != 13` 也会红。
    """
    build = DIALECT_BUILDERS[kind]
    _, params = build("ai_web_demo", "order_main", "status", max_distinct=30, row_limit=1000)
    assert params == {"row_limit": 1000, "ndv_limit": 40}
    _, params = build("ai_web_demo", "order_main", "status", max_distinct=3, row_limit=25)
    assert params == {"row_limit": 25, "ndv_limit": 13}


@pytest.mark.parametrize("kind", sorted(DIALECT_BUILDERS))
def test_标识符在文本里_数值走绑定参数(kind: str) -> None:
    """工单 033 的注入面口径：名字**不能**当绑定参数（源库不认），所以只能转义后进文本。

    这一条把两半都钉住：三个名字各自带着引号出现在语句里，而 `params` 的键**只有**那两个数
    ——多一个键就是有人开始把名字往参数里塞（那反而是另一种坏：`LIMIT` 位上的参数会被源库拒）。
    """
    build = DIALECT_BUILDERS[kind]
    sql, params = build("ai_web_demo", "order_main", "status", max_distinct=30, row_limit=1000)
    low = _norm(sql)
    for fragment in SAMPLE_FRAGMENTS[kind]:
        assert fragment in low, fragment
    assert set(params) == {"row_limit", "ndv_limit"}


@pytest.mark.parametrize(
    ("kind", "raw_table", "quoted"),
    [
        # 每一家拿**自己那个引号字符**去做名字：MySQL 的反引号出现在 PG 的表名里是无害的，
        # 反过来也是——所以只测一家测不出另一家漏了转义。
        ("mysql", "we`ird", "`we``ird`"),
        ("postgres", 'we"ird', '"we""ird"'),
    ],
)
def test_名字里带引号时成对写进语句而不是被剥掉(kind: str, raw_table: str, quoted: str) -> None:
    """表名来自 `information_schema`，不是我们挑的字面：这里必须是 doubling。

    与上面 `quote_ident` 那条的分别是"到没到语句里"——那一验纯函数，这一验采样 SQL 真的走了它。
    写成"剥掉引号字符"的话这条红，而它红的那一刻正是"采到别的表上"的那一刻。
    """
    build = DIALECT_BUILDERS[kind]
    sql, _ = build("ai_web_demo", raw_table, "status", max_distinct=30, row_limit=1000)
    assert quoted in _norm(sql)


@pytest.mark.parametrize("kind", sorted(DIALECT_BUILDERS))
def test_采样语句只有读_一个会话变量都不碰(kind: str) -> None:
    """工单 033 的已定口径两条一起钉：不新增写源库的路径、也不许顺手加 `SET max_execution_time`。

    后一条为什么要在形状上钉：抽取侧那条超时护栏至今无实现点（P3 无主账 ①），
    在采样这里"顺手补一个 `SET`"看起来是加固，实际是把无主账变成有主的一半——
    而会话级 SET 会对**同一条连接上后面的所有查询**生效，那是另一片的地盘。
    """
    build = DIALECT_BUILDERS[kind]
    sql, _ = build("ai_web_demo", "order_main", "status", max_distinct=30, row_limit=1000)
    low = _norm(sql).lower()
    assert low.startswith("select")
    for forbidden in ("insert", "update", "delete", "drop", "alter", "truncate", "set "):
        assert forbidden not in low, forbidden


# ---------------------------------------------------------------- 逐列发包与行→组


class _SampleStubMySQL(MySQLExtractor):
    """按**被问的那一列**现算假数据的桩件（同 `test_extract_mysql_batching.BatchStub` 的写法）。

    假数据必须跟着语句走而不是跟着入参走，否则"第二列的组挂到第一列上"这种错就看不见。
    """

    def __init__(self) -> None:
        super().__init__(ConnectionSpec(host="127.0.0.1", port=3306, user="aiweb_ro", password="p"))
        self.executed: list[tuple[str, dict[str, object]]] = []

    def _rows(self, sql: str, params: dict[str, object] | None = None) -> list[Row]:
        self.executed.append((sql, dict(params or {})))
        asked = re.search(r"SELECT `([^`]+)` AS v FROM", sql)
        assert asked is not None, f"语句里认不出被问的列：{sql}"
        return [{"sample_value": f"{asked.group(1)}-值", "n": 7}]


class _SampleStubPG(PostgresExtractor):
    def __init__(self) -> None:
        super().__init__(
            ConnectionSpec(
                host="127.0.0.1",
                port=5432,
                user="aiweb_ro",
                password="p",
                database="ai_web_demo_pg",
            )
        )
        self.executed: list[tuple[str, dict[str, object]]] = []

    def _rows(self, sql: str, params: dict[str, object] | None = None) -> list[Row]:
        self.executed.append((sql, dict(params or {})))
        asked = re.search(r'SELECT "([^"]+)" AS v FROM', sql)
        assert asked is not None, f"语句里认不出被问的列：{sql}"
        return [{"sample_value": f"{asked.group(1)}-值", "n": 7}]


STUBS: dict[str, type[_SampleStubMySQL] | type[_SampleStubPG]] = {
    "mysql": _SampleStubMySQL,
    "postgres": _SampleStubPG,
}


@pytest.mark.parametrize("kind", sorted(STUBS))
async def test_一个候选列发一条_而且组挂回自己那一列(kind: str) -> None:
    """§8 第二步的字面是**按列**的（一次一条 `GROUP BY col`），所以发包数=候选列数。

    两件事分开断：发了几条（源库压力口径）、每条的结果挂在哪个列名上（落库正确性）。
    后者是本片最容易错的一格——把上一列的组交给下一列，卡片上就会写着
    「取值: 已完成」的是 `pay_status` 而实际采的是 `order_status`。
    """
    stub = STUBS[kind]()
    samples = await stub.sample_distinct(
        SampleTarget(
            schema_name="ai_web_demo",
            table_name="order_main",
            columns=("pay_status", "order_status"),
        ),
        max_distinct=30,
        row_limit=1000,
    )
    assert len(stub.executed) == 2
    assert [(s.column_name, s.groups) for s in samples] == [
        ("pay_status", (("pay_status-值", 7),)),
        ("order_status", (("order_status-值", 7),)),
    ]


@pytest.mark.parametrize("kind", sorted(STUBS))
async def test_没有候选列时一条都不发(kind: str) -> None:
    """一张全是 int/时间列的表：候选清单筛完是空集，这时源库一条采样语句都不该收到。

    这一格同时是编排层"整表跳过"的方言层证据——空清单去发 SQL 会渲染成
    `SELECT ... FROM ...` 缺列名的坏语句，而那正是"什么都不采"变成"抽库失败"的形状。
    """
    stub = STUBS[kind]()
    assert (
        await stub.sample_distinct(
            SampleTarget(schema_name="ai_web_demo", table_name="t", columns=()),
            max_distinct=30,
            row_limit=1000,
        )
        == []
    )
    assert stub.executed == []


def test_行回组成组_null_留成_none_次数收成_int() -> None:
    """`rows_to_sample` 是 SQL 行与 `adjudicate` 之间的那道翻译，它只做三件事。

    期望值出处：§8 第二步的投影（`sample_value` / `n` 两个别名是两方言共同的契约，
    上面那两份手写语句原文就是它的定义）、`render_sample_value` 那三档、
    以及"`COUNT(*)` 到驱动手里可能是 int/Decimal/数字串"（`_int` 那条既有口径）。
    """
    sample = rows_to_sample(
        [
            {"sample_value": "已完成", "n": 30},
            {"sample_value": None, "n": 4},
            {"sample_value": b"\xe5\xbe\x85\xe6\x94\xaf\xe4\xbb\x98", "n": "9"},
        ],
        column_name="order_status",
    )
    assert sample == ColumnSample(
        column_name="order_status",
        groups=(("已完成", 30), (None, 4), ("待支付", 9)),
    )
