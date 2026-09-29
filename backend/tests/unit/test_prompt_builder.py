"""prompt 组装（工单 010 / 012）：段落顺序、两条硬约束、预算裁切、⑨ 的结果摘要。

接缝按 architecture §4.1 ④⑨ 与 verification §2.1 的 `prompt_builder` 行：
输入是构造出来的卡片对象，不碰 DB、不碰凭据，输出直接就是 `llm_client` 吃的 messages。
期望值口径全部来自文档：段落顺序出自 architecture §4.1，两句硬约束出自 roadmap §P2 踩坑 ④。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.services.nl2sql.prompt_builder import (
    Example,
    JoinLine,
    SchemaTable,
    Term,
    _example_line,
    _term_line,
    build_conclusion_prompt,
    build_prompt,
    result_brief,
)
from app.services.token_estimate import estimate_tokens


def _table(
    full_name: str = "ai_web_demo.order_main",
    *,
    uid: str | None = None,
    segments: tuple[str, ...] = ("【表】order_main",),
) -> SchemaTable:
    return SchemaTable(uid=uid or full_name, full_name=full_name, segments=segments)


def _join(*requires: str, text: str = "") -> JoinLine:
    """一条【可 JOIN】的行：`requires` 是它引用到的表 uid，`text` 是渲染好的那一行。

    默认文本只给一个可辨认的形状（`- uid.col，uid.col`），凡是断言字面的用例都显式传 `text`：
    builder 只该判"这行引用了谁还在不在"，把 pipeline 的渲染器拉进来就成了自证。
    """
    default = "- " + "，".join(f"{uid}.col" for uid in requires)
    return JoinLine(requires=requires, text=text or default)


# 两跳路径的渲染文本，形状出自 pipeline 的 `_join_lines`（这里当输入常量用，不调渲染器）。
ORDER_TO_CUSTOMER = "- ai_web_demo.order_main.customer_id → ai_web_demo.customer.id"
CUSTOMER_TO_BRIDGE = "ai_web_demo.customer.tenant_id → ai_web_demo.t_bridge.id"


def test_system_里两条硬约束原句在场() -> None:
    """缺这两句时守卫拒绝率会飙升（roadmap §P2 踩坑 ④），所以措辞被逐字钉死。"""
    messages = build_prompt(question="上月订单量是多少", tables=(_table(),))
    assert messages[0]["role"] == "system"
    system = messages[0]["content"]
    assert "必须带 LIMIT" in system
    assert "只能引用给定的表" in system


def test_段落顺序按架构_4_1() -> None:
    """schema → JOIN → 关联说明 → 术语 → 示例 → 问题 → 输出格式（architecture §4.1 ④）。

    顺序是结构断言而不是快照：模型对"约束离问题更近"这件事敏感，段落一挪位置就可能
    不复现，而快照 diff 里看不出"只是挪了位置"。
    """
    messages = build_prompt(
        question="每个客户的订单数",
        tables=(_table(),),
        joins=(_join("ai_web_demo.order_main", text=ORDER_TO_CUSTOMER),),
        notes=("本次候选表之间没有可执行的跨表关联路径。",),
        terms=(Term(term="GMV", definition="已支付订单金额合计", synonyms=("成交额",)),),
        examples=(Example(question="上月订单量", sql="SELECT 1 LIMIT 1"),),
    )
    user = messages[1]["content"]
    markers = [
        "【候选表】",
        "【可 JOIN】",
        "【关联说明】",
        "【术语口径】",
        "【参考示例】",
        "【问题】",
        "【输出格式】",
    ]
    positions = [user.index(m) for m in markers]
    assert positions == sorted(positions), markers


# 【可 JOIN】一行的两种形状（pipeline 的渲染器给出，这里当输入用）
ORDER_TO_CUSTOMER = "- ai_web_demo.order_main.customer_id → ai_web_demo.customer.id"


def test_本模块不重筛边的门槛_一处判定一处只搬运() -> None:
    """inferred ≥0.8 的门槛 014 起住在 `join_graph`（单测钉），这里不再判第二遍。

    这条用例钉的是"为什么少了那道筛子"：如果哪天有人在 builder 里补回一次 confidence 判定，
    就会出现两套"为什么这条 JOIN 没了"——而低置信边按 010 的口径本来还要作为卡片【可关联】
    行的结构描述留在 prompt 里，筛两遍会把那条诊断线索一起筛掉。
    """
    low_confidence = JoinLine(
        requires=("ai_web_demo.order_main",),
        text="- ai_web_demo.order_main.user_id → ai_web_demo.customer.id [推断,置信 0.7]",
    )
    user = build_prompt(question="q", tables=(_table(),), joins=(low_confidence,))[1]["content"]
    assert "[推断,置信 0.7]" in user


def test_起点在场对端被裁_结构提示行仍留_路径行整条撤() -> None:
    """两种行的 `requires` 不同，被预算裁掉时的结局也就不同（拍板 9 与 010 的按起点筛）。

    - **结构提示**（单边）只带起点：外键指向谁本身是结构事实，悄悄删掉会让"为什么这条 JOIN
      没了"无从查起。代价是【可 JOIN】的标题不能说"这些表都可以用"——它改口成"某张表没出现在
      【候选表】里时只是结构提示，不要写进 SQL"，与 system 的"只能引用给定的表"对齐。
    - **多跳路径**带着桥表：桥表的卡片被裁，经过它的路径就整体消失——留半条等于要求模型
      JOIN 一张没给卡片的表，而那正是守卫必拒的 SQL。
    """
    a = _table(segments=(_words(50),))
    bridge = _table("ai_web_demo.t_bridge", segments=("独有标记 " + _words(50),))
    hint = _join(a.uid, text="- ai_web_demo.order_main.customer_id → ai_web_demo.t_cut.id")
    path = _join(a.uid, bridge.uid, text=f"{ORDER_TO_CUSTOMER}，{CUSTOMER_TO_BRIDGE}")
    user = build_prompt(question="q", tables=(a, bridge), joins=(hint, path), token_budget=100)[1][
        "content"
    ]

    assert "独有标记" not in user  # 桥表的卡片确实被裁掉了
    assert "ai_web_demo.t_cut.id" in user  # 指向未召回表的单边提示还写着
    assert "不要写进 SQL" in user  # 而标题已经把这句话说明白了
    assert CUSTOMER_TO_BRIDGE not in user  # 经过桥表的那条路径整条没了


def test_卡片全文原样进_prompt_所以枚举取值在场() -> None:
    """工单验收"枚举取值出现在 prompt 里"（用户说"已完成" → `status='completed'` 的依据）。

    这一条不另写渲染逻辑：卡片全文（008 的 `text_md`）原样进 prompt，取值行是卡片自己带的。
    断言"原样"而不是"有取值行"，是为了防止将来有人为了省 token 在 builder 里二次加工卡片。
    """
    card = "【表】order_main\n【字段】共 1 个：\n- status enum 可空: 状态 取值: completed / paid"
    user = build_prompt(question="q", tables=(_table(segments=(card,)),))[1]["content"]
    assert card in user


def _words(n: int) -> str:
    """n 个 ASCII 词——`estimate_tokens` 给的就是 `ceil(n × 1.3)`，预算用例要能手算。"""
    return " ".join(f"w{i}" for i in range(n))


def test_预算不足时后面的表整张让位并记下表名(caplog: pytest.LogCaptureFixture) -> None:
    """roadmap §P4 验收 6：预算变小时送进 prompt 的表数显著变少**且不报错**，日志记下被裁的表名。

    50 个 ASCII 词 = 65 token（手算 `ceil(50×1.3)`），预算 100 只装得下第一张表。
    """
    caplog.set_level("INFO")
    a = _table("ai_web_demo.t_a", segments=(_words(50),))
    b = _table("ai_web_demo.t_b", segments=(_words(50),))
    user = build_prompt(question="q", tables=(a, b), token_budget=100)[1]["content"]
    assert "w0" in user
    assert "ai_web_demo.t_b" not in user
    assert "ai_web_demo.t_b" in caplog.text, (
        "被裁的表名要可查，否则无法回答'我的表为什么没进 prompt'"
    )


def test_宽表只丢列切片不丢主卡() -> None:
    """工单验收："68 列宽表进 prompt 时按列切片裁剪，不整段塞爆；裁剪后仍保留表名与主键列"。

    裁的粒度是**卡片段**而不是字符：008 的主卡带着表头 + 全部 PK/索引/外键列，
    切片只是第 26 列往后的补充。所以"保住主卡"这句话在段这一层才成立。
    """
    main = "【表】wide 主卡\n【字段】- id int 主键: 主键\n" + _words(50)
    shard = "【表】wide 续（第 26-55 个）\n- extra_1 varchar: 补充列\n" + _words(50)
    user = build_prompt(
        question="q",
        tables=(_table("ai_web_demo.wide", segments=(main, shard)),),
        token_budget=100,
    )[1]["content"]
    assert "【字段】- id int 主键" in user  # 表名与主键列都还在
    assert "第 26-55 个" not in user


def test_预算不足时参考示例整段丢弃() -> None:
    """verification §2.1 的"few-shot 段在预算不足时被**整段**丢弃"。

    手算：一张表 50 个 ASCII 词 = 65 token；一条示例 `问：…` + 30 词 ≈ 47 token。
    预算 100 时剩余 35 < 47 → 整段不留；预算 200 时回来。
    半条示例会把截断的写法当范本教给模型，比没有示例更糟，所以丢就丢整段。
    """
    table = _table(segments=(_words(50),))
    examples = (Example(question="上月订单量", sql=_words(30)),)
    tight = build_prompt(question="q", tables=(table,), examples=examples, token_budget=100)[1][
        "content"
    ]
    assert "【参考示例】" not in tight
    assert "问：上月订单量" not in tight

    loose = build_prompt(question="q", tables=(table,), examples=examples, token_budget=200)[1][
        "content"
    ]
    assert "【参考示例】" in loose
    assert "问：上月订单量" in loose


def test_术语与示例的行形状与预算估算式同源() -> None:
    """预算按 `_term_line` / 示例串算，模板按 j2 渲染——两边各写一份就会悄悄算错账。

    期望值是**手写**的（模板 §4.1 的行形状），两边都跟它比，而不是互相调一遍。
    """
    term = Term(term="GMV", definition="已支付订单金额合计", synonyms=("成交额", "成交金额"))
    example = Example(question="上月订单量", sql="SELECT COUNT(*) LIMIT 10")
    user = build_prompt(question="q", tables=(_table(),), terms=(term,), examples=(example,))[1][
        "content"
    ]

    expected_term_line = "- GMV（同义词：成交额、成交金额）: 已支付订单金额合计"
    expected_example_line = "问：上月订单量\nSQL：SELECT COUNT(*) LIMIT 10"
    assert expected_term_line in user
    assert expected_example_line in user
    assert _term_line(term) == expected_term_line
    assert _example_line(example) == expected_example_line


# ---- golden 快照（工单验收 1："改模板必须显式更新 golden，防止无感漂移"）----

GOLDEN = (
    Path(__file__).resolve().parents[1] / "fixtures" / "prompts" / "nl2sql__order_main.expected.txt"
)

_GOLDEN_TABLES = (
    SchemaTable(
        uid="uid_order",
        full_name="ai_web_demo.order_main",
        segments=(
            "【表】ai_web_demo.order_main\n"
            "【字段】共 2 个：\n"
            "- id int NOT NULL 主键: 订单ID\n"
            "- customer_id int NOT NULL: 客户ID",
        ),
    ),
    SchemaTable(
        uid="uid_customer",
        full_name="ai_web_demo.customer",
        segments=("【表】ai_web_demo.customer\n【字段】共 1 个：\n- id int NOT NULL 主键: 客户ID",),
    ),
)
_GOLDEN_JOINS = (_join("uid_order", "uid_customer", text=ORDER_TO_CUSTOMER),)


def test_golden_整段_user_message() -> None:
    """期望值按 verification §2.1 的段落顺序 + §5.3 的边标签**手写**，不是从渲染器 dump。

    dump 出来的快照只能证明"以后没变"；手写才能证明渲染结果就是文档那一份（008 的教训：
    文档围栏与模板各写一份时，golden 会悄悄跟着代码走）。
    空白口径单独钉这一条：渲染结果以最后一行的行末换行收尾，fixture 作为文本文件也这样收尾，
    所以逐字比对即可——`【问题】` 与 `【输出格式】` 之间那一个空行是模板有意留的。
    """
    user = build_prompt(
        question="每个客户最近 30 天的 GMV",
        tables=_GOLDEN_TABLES,
        joins=_GOLDEN_JOINS,
        terms=(Term(term="GMV", definition="已支付订单的金额合计", synonyms=("成交额", "交易额")),),
        examples=(Example(question="上月成交额", sql="SELECT 1 LIMIT 10"),),
        row_limit=1000,
    )[1]["content"]
    assert user == GOLDEN.read_text(encoding="utf-8")


# ---------------------------------------------------------------- ⑨：结果摘要进 prompt（工单 012）


def test_50行以内给整表_一行都不许少() -> None:
    """§4.1 ⑨ 的"前 50 行整表"分支：小结果集再做摘要纯属自毁——模型看不到月份对不上号。"""
    assert (
        result_brief(
            columns=["月份", "金额"],
            rows=[["2024-01", "10.5"], ["2024-02", "20.25"], ["2024-03", None]],
        )
        == "| 月份 | 金额 |\n"
        "| --- | --- |\n"
        "| 2024-01 | 10.5 |\n"
        "| 2024-02 | 20.25 |\n"
        "| 2024-03 |  |"
    )


def test_正好50行仍走整表分支_51行才切摘要() -> None:
    """阈值是"超过 50"而不是"达到 50"：边界钉一条，否则下次有人把 `<=` 改成 `<` 没人知道。"""
    full = [[f"2024-{i:02d}", str(i)] for i in range(1, 51)]
    assert "| 月份 | 金额 |" in result_brief(columns=["月份", "金额"], rows=full)
    assert "只给摘要" not in result_brief(columns=["月份", "金额"], rows=full)

    over = [*full, ["2025-01", "51"]]
    assert "只给摘要" in result_brief(columns=["月份", "金额"], rows=over)


def _60_rows() -> list[list[str]]:
    """60 行：`得分=2i` 单调升、`金额=100-i` 单调降，两个数值列的排序方向相反。

    故意让"第一个数值列"与"最后一个数值列"排出完全不同的头尾，这样"取哪一列当排序键"
    就不是一个能被巧合蒙过去的断言。
    """
    return [[f"c{i}", str(i * 2), str(100 - i)] for i in range(1, 61)]


def test_超50行只给摘要_没列出的行不许出现() -> None:
    """摘要分支的全部意义是"不整表喂进去"，所以数一行首格出现的次数，不看内容看数量。

    期望值手算：得分=2i 合计 2×(1+…+60)=3660、均值 61；金额=100−i 合计 6000−1830=4170、均值 69.5。
    """
    out = result_brief(columns=["名称", "得分", "金额"], rows=_60_rows())
    assert "（结果共 60 行，只给摘要，未列出的行不在这里）" in out
    assert "| 数值列 | 合计 | 均值 | 最小 | 最大 |" in out
    assert "| 得分 | 3660.00 | 61.00 | 2.00 | 120.00 |" in out
    assert "| 金额 | 4170.00 | 69.50 | 40.00 | 99.00 |" in out
    # 首格出现的行数 = 前 5 + 后 5：文本列（cN）不进度量表，所以它只可能来自那两张表
    assert out.count("| c") == 10


def test_摘要的头尾按最后一个数值列排() -> None:
    """排序键取**最后一个**数值列（§4.1 ⑨ 的口径：度量常写在维度之后）。

    按"得分"排的话 `c60`（得分 120）会进前 5；按"金额"排它落在最后一名。两者只可能有一个成立。
    """
    out = result_brief(columns=["名称", "得分", "金额"], rows=_60_rows())
    top, bottom = out.split("按 金额 取后 5 行：")
    head = top.split("按 金额 取前 5 行：")[1]
    assert "| c1 | 2 | 99 |" in head
    assert "| c5 | 10 | 95 |" in head
    assert "| c60 | 120 | 40 |" in bottom
    assert "| c56 | 112 | 44 |" in bottom
    assert "| c60 |" not in head


def test_超预算时只告警不改道_小窄结果的摘要比整表还长() -> None:
    """预算的语义是"看一眼并报一声"，不是"换个分支"。

    换分支那一刀会反噬：三列 2 行的整表只有几十个字，同数据的摘要要开 stats 表加两张头尾表，
    **更长**。所以这里把预算压得比整表还小，断的是"回的还是整表"——
    它同时也是"摘要没被当成兜底"的证据。
    """
    rows = [["2024-01", "10.5"], ["2024-02", "20.25"]]
    table = result_brief(columns=["月份", "金额"], rows=rows, token_budget=1)
    assert "| 月份 | 金额 |" in table and "只给摘要" not in table


def test_典型60行的摘要远在预算之内_预算不是摆设() -> None:
    """verification §2.1 的 `token_estimate` ⑥ 写的是"摘要路径的预算 ≤TOKEN_BUDGET"。

    1500 是缺省预算的字面量，不跟 `Settings` 走：跟着配置断等于断"它不小于自己"。
    """
    out = result_brief(columns=["名称", "得分", "金额"], rows=_60_rows(), token_budget=1500)
    assert estimate_tokens(out) <= 1500


def test_摘要装不下预算时留下告警_而不是静默把上下文顶爆(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """几百个数值列的摘要确实能长过预算；那一刀不裁（再裁就只剩"共 N 行"），但必须可查。"""
    columns = ["名称"] + [f"m{i}" for i in range(600)]
    rows = [[f"c{r}"] + [str(i + r) for i in range(600)] for r in range(60)]
    with caplog.at_level(logging.WARNING):
        out = result_brief(columns=columns, rows=rows, token_budget=1500)
    assert "超出预算" in caplog.text
    assert "只给摘要" in out  # 报了警，但一个字没裁


def test_结论prompt是两条消息且素材来自摘要() -> None:
    """⑨ 走第二次 LLM 调用：角色顺序与"共 N 行"取真实 row_count 而不是 len(rows)。

    executor 会丢探针行也可能截断，两者可以不相等——模板里那句"共 N 行"必须是真实行数，
    所以这里故意传 60 行而只给 2 行数据。截断的话术出自 conclusion_user.j2。
    """
    messages = build_conclusion_prompt(
        question="2024 年订单总金额是多少",
        columns=["月份", "金额"],
        rows=[["2024-01", "10.5"], ["2024-02", "20.25"]],
        row_count=60,
        truncated=True,
    )
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "结论生成器" in messages[0]["content"]
    user = messages[1]["content"]
    assert "【问题】2024 年订单总金额是多少" in user
    assert "【结果】共 60 行（已被行数上限截断）" in user
    assert "| 2024-02 | 20.25 |" in user
    assert "不要输出 markdown 表格" in user
