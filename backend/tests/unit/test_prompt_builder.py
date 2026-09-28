"""prompt 组装（工单 010）：段落顺序、两条硬约束、预算裁切。

接缝按 architecture §4.1 ④ 与 verification §2.1 的 `prompt_builder` 行：
输入是构造出来的卡片对象，不碰 DB、不碰凭据，输出直接就是 `llm_client` 吃的 messages。
期望值口径全部来自文档：段落顺序出自 architecture §4.1，两句硬约束出自 roadmap §P2 踩坑 ④。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.nl2sql.prompt_builder import (
    Example,
    RelationEdge,
    SchemaTable,
    Term,
    _example_line,
    _term_line,
    build_prompt,
)


def _table(
    full_name: str = "ai_web_demo.order_main",
    *,
    segments: tuple[str, ...] = ("【表】order_main",),
    relations: tuple[RelationEdge, ...] = (),
) -> SchemaTable:
    return SchemaTable(full_name=full_name, segments=segments, relations=relations)


def _rel(
    *,
    to_table: str = "ai_web_demo.customer",
    from_column: str = "customer_id",
    kind: str = "extracted",
    confidence: float | None = None,
) -> RelationEdge:
    return RelationEdge(
        from_column=from_column,
        to_table_full=to_table,
        to_column="id",
        kind=kind,
        confidence=confidence,
    )


def test_system_里两条硬约束原句在场() -> None:
    """缺这两句时守卫拒绝率会飙升（roadmap §P2 踩坑 ④），所以措辞被逐字钉死。"""
    messages = build_prompt(question="上月订单量是多少", tables=(_table(),))
    assert messages[0]["role"] == "system"
    system = messages[0]["content"]
    assert "必须带 LIMIT" in system
    assert "只能引用给定的表" in system


def test_段落顺序按架构_4_1() -> None:
    """schema → JOIN → 术语 → 示例 → 问题 → 输出格式（architecture §4.1 ④）。

    顺序是结构断言而不是快照：模型对"约束离问题更近"这件事敏感，段落一挪位置就可能
    不复现，而快照 diff 里看不出"只是挪了位置"。
    """
    messages = build_prompt(
        question="每个客户的订单数",
        tables=(_table(relations=(_rel(),)),),
        terms=(Term(term="GMV", definition="已支付订单金额合计", synonyms=("成交额",)),),
        examples=(Example(question="上月订单量", sql="SELECT 1 LIMIT 1"),),
    )
    user = messages[1]["content"]
    markers = [
        "【候选表】",
        "【可 JOIN】",
        "【术语口径】",
        "【参考示例】",
        "【问题】",
        "【输出格式】",
    ]
    positions = [user.index(m) for m in markers]
    assert positions == sorted(positions), markers


def test_推断边的读侧门槛是_0_8() -> None:
    """§5.3："只有 ≥0.8 才进 prompt，且带 `[推断,置信 x]` 标签"。

    写侧（`relation_infer`）今天最低给 0.85，但 007 时代按常数 `0.7` 落库的行还在库里——
    这道门槛现在筛的是历史行。真外键（extracted）与人工确认（manual）不打折、不过门槛。

    门槛的管辖区是**【可 JOIN】这份可执行边清单**（拍板：§5.3 的"不进 prompt"是这个意思）：
    同一张低置信边仍会作为**结构描述**出现在卡片全文的【可关联】行里
    （008 的渲染器不看 confidence），那里带的 `[推断,置信 0.7]` 字样正是诊断线索。
    """
    below = build_prompt(
        question="q",
        tables=(_table(relations=(_rel(kind="inferred", confidence=0.7),)),),
    )
    assert "【可 JOIN】" not in below[1]["content"]

    above = build_prompt(
        question="q",
        tables=(_table(relations=(_rel(kind="inferred", confidence=0.85),)),),
    )[1]["content"]
    assert (
        "- ai_web_demo.order_main.customer_id → ai_web_demo.customer.id [推断,置信 0.85]" in above
    )
    extracted = build_prompt(question="q", tables=(_table(relations=(_rel(),)),))[1]["content"]
    assert "【可 JOIN】" in extracted
    assert "置信" not in extracted  # 真外键不带推断标签


def test_目标表被预算裁掉后边仍在可_join_段_但标题已声明不可引用() -> None:
    """门槛与裁切只管"这张表的卡片在不在"，不管"边的目标在不在"（§4.1 ④ as-built）。

    刻意不按目标表筛边：外键指向谁本身是结构事实，悄悄删掉会让"为什么这条 JOIN 没了"无从查起。
    代价是【可 JOIN】的标题不能说"这些表都可以用"——它改口成"目标表没出现在【候选表】里就是
    结构提示，不要写进 SQL"，让这句声明和 system 的"只能引用给定的表"对齐。
    """
    a = _table(segments=(_words(50),), relations=(_rel(to_table="ai_web_demo.t_cut"),))
    b = _table("ai_web_demo.t_cut", segments=("独有标记 " + _words(50),))
    user = build_prompt(question="q", tables=(a, b), token_budget=100)[1]["content"]

    assert "独有标记" not in user  # t_cut 的卡片确实被裁掉了
    assert "ai_web_demo.t_cut.id" in user  # 指向它的边还写着
    assert "不要写进 SQL" in user  # 而标题已经把这句话说明白了


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
        full_name="ai_web_demo.order_main",
        segments=(
            "【表】ai_web_demo.order_main\n"
            "【字段】共 2 个：\n"
            "- id int NOT NULL 主键: 订单ID\n"
            "- customer_id int NOT NULL: 客户ID",
        ),
        relations=(_rel(),),
    ),
    SchemaTable(
        full_name="ai_web_demo.customer",
        segments=("【表】ai_web_demo.customer\n【字段】共 1 个：\n- id int NOT NULL 主键: 客户ID",),
    ),
)


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
        terms=(Term(term="GMV", definition="已支付订单的金额合计", synonyms=("成交额", "交易额")),),
        examples=(Example(question="上月成交额", sql="SELECT 1 LIMIT 10"),),
        row_limit=1000,
    )[1]["content"]
    assert user == GOLDEN.read_text(encoding="utf-8")
