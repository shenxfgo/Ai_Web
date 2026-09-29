"""`chart_advisor` 的规则决策表（architecture §4.2 + verification §2.1 那一行）。

**输入形态是 executor 序列化之后的值**，不是数据库原始值：`datetime` 已成 ISO 字符串、
`Decimal` 已成字符串（safety §4.3 的 `serialize_cell`）。所以"数值列""时间列"的判定
必须在字符串上做——拿原始类型判会全线判错。

期望值全部手算，不从实现反推。
"""

from __future__ import annotations

from app.services.chart_advisor import advise_chart


def test_单行单列_出大数字卡而不是图() -> None:
    spec = advise_chart(columns=["总金额"], rows=[("1234.56",)])
    assert spec.type == "kpi"
    assert spec.series == ("总金额",)


def test_行数超过两百_不画图只给表() -> None:
    # §4.2 的下线条件：点太密时图比表更难读。
    rows = [(str(i), "1") for i in range(201)]
    spec = advise_chart(columns=["dt", "金额"], rows=rows)
    assert spec.type == "table"


def test_列数达到八列_不画图() -> None:
    # verification §2.1 的"全 NULL 列→table only"与 §4.2 规则 5 的"列数 ≥8"是同一家族：
    # 宽结果集上没有哪两列值得优先画出来，猜哪一对都是噪声。
    columns = [f"c{i}" for i in range(8)]
    rows = [("1", "2", "3", "4", "5", "6", "7", "8"), ("9", "8", "7", "6", "5", "4", "3", "2")]
    spec = advise_chart(columns=columns, rows=rows)
    assert spec.type == "table"


def test_数值列全是_NULL_不拿它当系列() -> None:
    spec = advise_chart(columns=["dt", "金额"], rows=[("2024-01-01", None), ("2024-02-01", None)])
    assert spec.type == "table"


def test_每一种建议都留着_table_逃生门() -> None:
    # §4.2 规则 6：判断永远会错，UI 的手动切换 tab 不是可选项。
    assert advise_chart(columns=["总金额"], rows=[("1",)]).fallback == "table"
    assert advise_chart(columns=["dt", "金额"], rows=[("2024-01-01", "1")]).fallback == "table"


def test_时间首列加一个数值列_出折线() -> None:
    # §4.2 规则 2 的时间那一支。行是序列化后的形态：datetime 已成 ISO 字符串。
    rows = [("2024-01-31", "10.50"), ("2024-02-29", "20.00"), ("2024-03-31", "31.25")]
    spec = advise_chart(columns=["月份", "金额"], rows=rows)
    assert spec.type == "line"
    assert spec.x == "月份"
    assert spec.series == ("金额",)
    assert spec.top_n is None  # 时间轴不做"top10 + 其他"的折叠


def test_时间列认_ISO_带时刻的那一型() -> None:
    # created_at 序列化后是 "2024-01-31T09:00:00"（safety §4.3），不能被当成文本类别。
    rows = [("2024-01-31T09:00:00", "1"), ("2024-02-01T09:00:00", "2")]
    assert advise_chart(columns=["created_at", "订单数"], rows=rows).type == "line"


def test_低基数类别首列加数值_出柱状() -> None:
    rows = [("华东", "100.00"), ("华南", "80.00"), ("华北", "60.00")]
    spec = advise_chart(columns=["区域", "金额"], rows=rows)
    assert spec.type == "bar"
    assert spec.x == "区域"
    assert spec.series == ("金额",)
    assert spec.top_n is None  # NDV≤12 不需要折叠


def test_类别超过十二个_仍是柱状但只取前十其余归其他() -> None:
    # §4.2 规则 3 与 verification §2.1 的"NDV>12 + 数值 → bar（取 top10 + 其他）"。
    # 折叠本身是渲染层干的（ChartSpec 只说"折成几段"），本模块不改写行。
    rows = [(f"渠道{i}", str(i)) for i in range(15)]
    spec = advise_chart(columns=["渠道", "金额"], rows=rows)
    assert spec.type == "bar"
    assert spec.top_n == 10
    assert spec.other_label == "其他"


def test_占比列名带_pct_出饼图_系列封顶六片() -> None:
    rows = [("货到付款", "40.0"), ("在线支付", "35.0"), ("余额", "25.0")]
    spec = advise_chart(columns=["类型", "占比_pct"], rows=rows)
    assert spec.type == "pie"
    assert spec.top_n == 6
    assert spec.other_label == "其他"


def test_合计接近一百时也认成占比_哪怕列名没带_pct() -> None:
    rows = [("A", "55.5"), ("B", "44.5")]
    assert advise_chart(columns=["分组", "份额"], rows=rows).type == "pie"


def test_类别列的合计明显不是占比_就还是柱状() -> None:
    # 手算：55.5 + 44.5 = 100 → pie；这里 100+80+60=240，不是"份额"。
    rows = [("A", "100"), ("B", "80"), ("C", "60")]
    assert advise_chart(columns=["分组", "份额"], rows=rows).type == "bar"


def test_两个数值列_没有类别轴_行数够三十_出散点() -> None:
    rows = [(str(i), str(i * 2)) for i in range(1, 31)]
    spec = advise_chart(columns=["客单价", "成本"], rows=rows)
    assert spec.type == "scatter"
    assert spec.x == "客单价"
    assert spec.series == ("成本",)


def test_散点要满三十行_二十行不配() -> None:
    # §4.2 规则 4 的"行数 ≥30"是硬条件：20 个点看不出相关性，只会看出噪声。
    rows = [(str(i), str(i * 2)) for i in range(1, 21)]
    assert advise_chart(columns=["客单价", "成本"], rows=rows).type == "table"


def test_整数序数首列可以当月份轴_出柱状() -> None:
    # §4.2 规则 2 的"整数序数"那一支：12 行 12 个不同值，distinct/rows=1.0 但样本太小，
    # 不构成"每行唯一所以没得聚合"那个信号（比值下线只在行数 ≥20 时参与，见实现的注释）。
    rows = [(str(m), str(m * 100)) for m in range(1, 13)]
    spec = advise_chart(columns=["月份", "金额"], rows=rows)
    assert spec.type == "bar"
    assert spec.x == "月份"
    assert spec.series == ("金额",)
    assert spec.top_n is None


def test_多数值列给多条系列_但绝不重塑数据行() -> None:
    # §4.2 规则 2 的 stacked bar / multi-line 需要先"转长表"，而 ChartSpec 是**建议不是变换**：
    # 本模块只说"这几列都画出来"，转长表归渲染层（工单 012 拍板：P2 不做 pivot）。
    rows = [("2024-01-31", "10", "1"), ("2024-02-29", "20", "2")]
    spec = advise_chart(columns=["月份", "金额", "订单数"], rows=rows)
    assert spec.type == "line"
    assert spec.series == ("金额", "订单数")


def test_列名重复时不出图_ChartSpec_的列名形状表达不出哪一列() -> None:
    # `SELECT SUM(a) AS 金额, SUM(b) AS 金额 FROM t` 是合法 SQL。而 ChartSpec 的 x/series
    # 存列名（§2.7），重名时"series=金额"指的是第几列没有答案——这不是保守，是形状无定义。
    rows = [("2024-01-31", "10", "1"), ("2024-02-29", "20", "2")]
    spec = advise_chart(columns=["月份", "金额", "金额"], rows=rows)
    assert spec.type == "table"
    assert spec.fallback == "table"


def test_非有限值不是数值_一格就能把整列的合计毒掉() -> None:
    # 两条来路都到得了这里：FLOAT 列里的 `nan` 经 `serialize_cell` **原样透传**（它只处理
    # Decimal/日期/bytes/超长 str），以及文本列里存着的 `"NaN"` 字面量——`float()` 两个都认。
    # 认它作数值的话，整列合计变 NaN，"合计≈100 判饼图"和摘要里的 sum/avg 全废，
    # 而废的形式是"那一格照样显示成数字"，看不出来。
    for poison in ("NaN", "Infinity", "-Infinity", float("nan"), float("inf")):
        rows = [("2024-01", poison), ("2024-02", "5")]
        assert advise_chart(columns=["dt", "金额"], rows=rows).type == "table"
