"""L1 检索的第一块纯函数：把中文问句切成检索词。

oracle 来自 `docs/architecture.md` §5.1 L1 那一行（"对表名/列名/comment_raw/comment_zh/业务描述做
精确、前缀、ILIKE、pg_trgm 匹配"）与 §5.2 (2a)（标识符权重最高）。**期望值全部手算**：
每个词的该有不该有，都能拿"演示库里的注释字面"验一遍——比如 `订单` 要能在
`order_main.amount` 的注释"订单金额（元，优惠前）"里命中，`金额` 也是。
"""

from __future__ import annotations

from app.services.nl2sql.retriever import query_terms


def test_中文长串切成二字词且保序去重() -> None:
    # PG 侧没有中文分词器（zhcfg/pg_jieba 都不在），Python 侧也没引 jieba，
    # 所以 L1 用二字滑窗近似词：`订单` 与 `金额` 两个 gram 各自都能命中 order_main，
    # 排序主键（命中的**不同词数**，as-built 0009）就是靠这两个 gram 各计一次撑起来的。
    # 汉字串是 `年每个月的订单总金额`（10 字）→ 逐字滑窗 9 个 gram：
    # 年每/每个/个月/月的/的订/订单/单总/总金/金额。
    assert query_terms("2024 年每个月的订单总金额") == (
        "2024",
        "年每",
        "每个",
        "个月",
        "月的",
        "的订",
        "订单",
        "单总",
        "总金",
        "金额",
    )


def test_英文标识符整串保留并拆驼峰与下划线() -> None:
    """§5.2 (2)："terms = query_kw_terms + 从问题里正则抽出的英文标识符/驼峰拆词"。

    整串必须在子词**之前**：用户真写 `pay_amount` 时那是最强的信号（(2a) 精确/前缀权重 1.0），
    拆出来的 `pay`/`amount` 只是顺手兜住"列叫 amount 而用户写 pay_amount"的降级命中。
    """
    assert query_terms("payAmount / pay_amount 统计") == (
        "payAmount",
        "pay",
        "Amount",
        "pay_amount",
        "amount",
        "统计",
    )


def test_汉字段既出整段也出二字词() -> None:
    """短汉字段要**同时**给整段和二字滑窗，否则动词会把真词粘死。

    "查客户，手机号" 里的汉字段是 `查客户`（3 字，逗号只把它和 `手机号` 分开）：
    只发整段的话 ILIKE '%查客户%' 什么都命中不了，`客户` 这个真正的信号就丢了；
    只发二字词的话又丢掉 `手机号` 这种三字自足词。所以两条都发：真词（`客户`/`手机号`）
    各自算一票（§5.1 主键 = 命中的不同词数，as-built 0009），噪声词（`查客`）对不上注释就不占票。
    超过 4 字的段整段已经没有判别力，只发二字词。
    """
    assert query_terms("查客户，手机号") == ("查客户", "查客", "客户", "手机号", "手机", "机号")


def test_单字汉字段丢掉因为它是噪声放大器() -> None:
    """一个字的段（`查`）做 ILIKE 会把"查询口径""检查时间"这类注释全都捞进来，判别力是负的。"""
    assert query_terms("查,金额") == ("金额",)
