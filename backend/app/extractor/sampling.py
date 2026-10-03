"""枚举 NDV 采样的**纯 **函数层（工单 033；`docs/kb-workflow.md` §8）。

这一层不认识驱动、不认识 ORM、不发 SQL：它只回答两件事——"这一列值不值得去源库数一遍"
（§8 第一步）与"数回来的那些值能不能算值域"（§8 第二步的阈值）。放这里只有一个原因：
这两判据两方言共用，而跨方言互相 import 私有 helper 会让两个方言模块从此绑死
（同 `batching.py` 那条理由）。

文件末尾另有四个共用的**小工具**（`ndv_query_limit` / `quote_ident` / `render_sample_value`
/ `rows_to_sample`），它们是"发 SQL 之前必须算完、以及结果集回到 Python 之后必须收拢"那几件事：
外层 LIMIT 的表达式、标识符转义、值的文本化、行→列的组。它们**不**拼 SQL——SQL 文本的方言差异
（`information_schema` 还是 `pg_catalog`、`LIMIT` 写在哪儿）住在各自的抽取器里。

与 `batching.py` 同一条纪律：**这一层不认识 Settings**。三个键
（`sample_distinct` / `sample_distinct_max_distinct` / `sample_row_limit`）的口径住在
`Settings.extract`，由编排层（`sync_service`）读出来传进来——方言层或这里再写一遍数字
就是第二个定义点，而"改了配置不生效"正是本片要闭掉的那类错。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from app.extractor.base import ColumnSample, Row

# §8 第一步那份清单的字面（`information_schema.columns.data_type IN (...)`）。
# 里面的 `boolean` 与 `bool` 是**同一个东西的两种写法**：023 之后 `meta_column.data_type`
# 存的是归一值（PG 的 `boolean`、MySQL 的 `tinyint(1)` 都归成 `bool`），而 §8 那行写的是
# IS 原文。两个都收进清单，是为了让"文档的六类"与"库里那一格"在这一个地方对上——
# 对上这件事本身由 `tests/unit/test_extract_sample_distinct.py` 钉着。
DOC_CANDIDATE_BASES: Final[frozenset[str]] = frozenset(
    {"enum", "set", "varchar", "char", "boolean", "bool", "tinyint"}
)


def _base_name(data_type: str) -> str | None:
    """归一 `data_type` → 基名；带数组后缀时回 `None`（不是"这一列的取值"）。

    PG 的数组列有两种存法：`text[]`（023 的归一保留后缀）与 `_text`（IS 原文那层的写法，
    工单 024 之后 `raw_data_type` 里能看到）。两种都不是标量枚举列——对数组发
    `GROUP BY col` 数的是"整个数组有几种"，阈值判定当场失真。
    """
    name = data_type.strip().lower()
    if name.startswith("_") or "[]" in name:
        return None
    return name.split("(", 1)[0].strip()


def is_sample_candidate(data_type: str, *, is_generated: bool, has_declared_enum: bool) -> bool:
    """§8 第一步：按列类型筛候选（只挑 enum/短文本，且低基数）。

    判据读的是**库里那一格**（归一后的 `meta_column.data_type`）而不是 IS 原文：采样住在
    同步落库之后，那时手里只有 `meta_column` 的行。`char_length` 一类的"短"没有进判据——
    §8 把"低基数"这件事交给了第二步的 NDV 阈值，再加一道长度门槛就是第三个口径。

    PG 的真枚举类型归一后是 `user-defined`，不在清单内，因此**不会**被采样。这不是漏：
    那一类列的值域由 §8.2 C 从 `pg_enum` 直接拿（`RawColumn.enum_values` 非空），
    定义比观测准——同一条理由在下面 `has_declared_enum` 那一格。

    两处排除：
    - `is_generated`：生成列的值是别的列算出来的，扫它等于把同一份数据数第二遍。
    - `has_declared_enum`（MySQL 的 `enum_def` / PG 的 `pg_enum` 已经给了值域）：那一份来自
      **定义**，采样给的只是"本轮扫过的那 `row_limit` 行里出现过的值"。用一个合法但暂时
      没人用的取值会消失的形状去覆盖定义，是净损失。
    """
    if is_generated or has_declared_enum:
        return False
    base = _base_name(data_type)
    return base is not None and base in DOC_CANDIDATE_BASES


@dataclass(frozen=True, slots=True)
class SampledValues:
    """§8 第三步要写进 `meta_column` 的那两格。

    两格**今天同值同序**：§8 的原文只说"通过的列写入 `enum_values` 与 `sample_values`"，
    没说两者内容怎么不同；§2.4 给的差别是**读者**（前者进卡片「取值:」段，后者是给界面
    展示的样本）。"少量"是几条在 spec、§8、roadmap 里都没有出处，所以这里不发明一个
    截断常量——那份留白记在工单 033 的交付记录里，收口片再判要不要合掉一格。
    """

    enum_values: tuple[str, ...]
    sample_values: tuple[str, ...]


def adjudicate(
    rows: Sequence[tuple[str | None, int]], *, max_distinct: int
) -> SampledValues | None:
    """§8 第二步的裁决：数回来的 `(值, 次数)` 组能不能算值域。

    `None` 就是"这一列放弃"（§8 那句"一旦超过阈值即放弃该列"），调用方据此**什么都不写**——
    高基数列的阈值语义是"只记基数不记值域"，而 `meta_column` 没有存基数的那一格（本片不动迁移），
    所以"记基数"那一半今天**做不到**：组数只在这轮循环里从 `sample.groups` 手上过一遍，
    回 `None` 之后就没有任何地方留下它。可观测形状因此只有"值域不写"。

    两条判据分得很开，别合：
    - **阈值数的是不同值的个数**，不是组数也不是行数——所以先去重再比 `max_distinct`。
      写成 `len(rows)` 的实现等价于"把 SQL 的 `LIMIT` 当成阈值"，那是两件事。
    - **NULL 占一个分组但不进值域**：`enum_values` 的形状是 `list[str]`，而"这列能不能取 NULL"
      由 `meta_column.nullable` 那一格说，值域里再写一次就成了两份真相。
      全是 NULL 的列回 `None` 而不是空值域——写 `[]` 等于承诺"这列的取值有零个"。

    值的**文本化**不住这里：MySQL 回 bytes、PG 回 `bool`/`int`，那些差异归方言层
    （`rows_to_sample` 里逐值调用本文件末尾的 `render_sample_value`），这一层只接已经成串
    的东西，否则两方言的差别会漏进共用判据里。
    """
    if max_distinct < 1:
        raise ValueError(f"max_distinct 至少是 1，收到 {max_distinct}")
    counts: dict[str, int] = {}
    for value, count in rows:
        if value is None:
            continue
        counts[value] = counts.get(value, 0) + count
    if not counts or len(counts) > max_distinct:
        return None
    # 频次降序、同频次按字面升序：后者纯粹是为了 determinism——没有它，两个同频取值在
    # 卡片文本里的先后由源库返回顺序决定，golden 会随机漂。
    ordered = tuple(sorted(counts, key=lambda value: (-counts[value], value)))
    return SampledValues(enum_values=ordered, sample_values=ordered)


# ------------------------------------------------------- 两方言共用的采样小工具
#
# 下面三个函数**不是**方言无关的抽象 SQL 生成器：它们各管一件"两方言的做法恰好一样、
# 但都必须在发出 SQL 之前算完"的小事。放在这一层是为了让两处实现各自只剩引号字符与
# 类型清单那点真差异（同 `batching.py` 的 `slice_names` 那条理由）。

# §8 第二步那句 `LIMIT 40` 里"阈值以外多留的那一截"。
NDV_HEADROOM: Final = 10


def ndv_query_limit(max_distinct: int) -> int:
    """采样 SQL 外层那个 `LIMIT` 的**表达式**：阈值 + 余量。

    工单 033 的已定口径不许重开：文档 §8 那行的字面 `40` 是"默认阈值 30 + 余量 10"，
    阈值一改它就成了假话。为什么要留余量而不是 `LIMIT 阈值`：`adjudicate` 的判据是
    "不同值的**个数超过阈值**即放弃"，而 `LIMIT 阈值` 那一组永远最多回阈值个值——
    于是"这列其实有 500 个不同值"与"这列恰好有 30 个"在结果集上长得一模一样，
    高基数列会被误判成合法枚举列写进卡片。多出来的那 10 行只用来回答"有没有超"，
    不参与值域。
    """
    return max_distinct + NDV_HEADROOM


def quote_ident(name: str, *, quote: str) -> str:
    """把一个源库来的标识符包进 `quote`（MySQL 用 `` ` ``、PG 用 `"`），内嵌的出现成对写。

    表名/列名来自 `information_schema`，不是我们挑的字面，所以这里必须当成不可信输入：
    采样 SQL 的 `FROM`/`SELECT` 只能走文本拼接（值可以走 bindparam，名字不行），
    doubling 是这两家各自认的标准转义。**不做**"剥掉引号字符"那种写法——那样挡得住注入，
    但源库真有一个 ``weird`name`` 时会把数据采到**别的**表上，那是更坏的一种错。

    空串当场抛：`""` 在 PG 里是一个能被引号救活的真实表名，在 MySQL 里直接语法错，
    两种都不是"这一张表"。
    """
    if not name:
        raise ValueError("标识符不能是空串")
    return f"{quote}{name.replace(quote, quote * 2)}{quote}"


def render_sample_value(raw: object) -> str | None:
    """把驱动回的一个值文本化成**能写进 SQL 字面的那一份**（不是 Python 的 `repr`）。

    住在这一层是因为四条判据两方言共用，而分支本身按方言各有归属：
    - `bool → true` / `false`：只有 PG 走这一支（`boolean` 列回 Python `bool`），
      字面必须是小写——PG 的布尔字面就是这两个，`str(True)` 那一份源库不认。
      MySQL 没有布尔类型，`tinyint(1)` 回的是 int，走数字那一档。
      这一条必须排在数字之前判：`bool` 是 `int` 的子类，先转数字会把 `True` 变成 `"1"`。
    - `bytes → 按 UTF-8 解`：MySQL 在 `binary` 字符序的列上回 bytes，而候选清单里有
      `char`/`varchar`，同一列在不同表上字符序可以不同。解不开就让它抛——那一张表会走
      "单表采样失败 → `errors[]` + `partial`"那条既有通路（工单 033 的失败语义），
      比把一串乱码写进卡片诚实。
    - `None → None`：NULL 桶由 `adjudicate` 丢，这一层不许把它变成 `"None"`，
      那样"这列没值"就成了"这列能取字面 None"。
    - 其余（int/Decimal/str）走 `str`：值域那一格存的是给人和模型看的字面。
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return str(raw)


def rows_to_sample(rows: Sequence[Row], *, column_name: str) -> ColumnSample:
    """把一条采样语句回的行摊成 `ColumnSample`（§8 第二步与 `adjudicate` 之间的那道翻译）。

    两方言共用：它们对同一条语句用**同一对别名**（`sample_value` / `n`），这一点由
    `tests/unit/test_extract_sample_distinct.py` 里那两份手写的语句原文钉着——
    任何一边悄悄改了别名，这里会在另一边的用例里红，而不是安静地回一堆 `None`。

    只做三件事：逐行走 `render_sample_value`、`COUNT(*)` 收成 int（驱动可能回
    int/Decimal/数字串）、原样保留源库的返回次序（次序由 `adjudicate` 按频次重排，
    这里排一次等于给它一个"看起来已经排过"的假信号）。
    """
    return ColumnSample(
        column_name=column_name,
        groups=tuple(
            (render_sample_value(row["sample_value"]), int(str(row["n"]))) for row in rows
        ),
    )
