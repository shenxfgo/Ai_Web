"""第二层拒绝后的回喂内容（工单 038）：纯函数，不碰 DB、不碰网络。

接缝口径住 `docs/nl2sql-safety.md` §3.5：回喂的**只有文本**——原提示消息列表、模型上一轮的原文、
那一次端出去的 `sql_final`、以及那句"我们本来要说给用户"的错误原文。措辞住本模块（010 起
`prompt_builder` 是 prompt 的唯一主人），不在编排里拼字符串。
期望值不反推实现：错误原文的字面形状（`[rule_id] …源库在执行 EXPLAIN 时就拒了这条 SQL：1054: …`）
是从 `dry_run.run_dry_run` 与 `pipeline._rejected_by_guard` 的既有实物抄来的常量。
"""

from __future__ import annotations

from typing import Any

from app.services.nl2sql.prompt_builder import build_prompt, build_retry_messages

FAILED_SQL = "SELECT `nope` FROM `order_main` LIMIT 1000"
ENGINE_TEXT = (
    "[dry_run_engine_rejected] 源库在执行 EXPLAIN 时就拒了这条 SQL："
    "1054: Unknown column 'nope' in 'field list'"
)
PREVIOUS_DRAFT = '{"sql": "SELECT nope FROM order_main", "explanation": "", "clarify": ""}'


def _base_messages() -> list[dict[str, Any]]:
    """第一次生成的那份 messages：形状出自 `build_prompt`，不另造一套。"""
    return build_prompt(question="上月订单量是多少", tables=())


def test_回喂内容同时含失败的_SQL_与那句错误原话() -> None:
    """少任何一个，模型就是在猜（工单 038 涉及层"外加一条纯函数"那一格）。"""
    messages = build_retry_messages(
        _base_messages(),
        previous_draft=PREVIOUS_DRAFT,
        sql_final=FAILED_SQL,
        error_message=ENGINE_TEXT,
    )
    fed = messages[-1]["content"]
    assert FAILED_SQL in fed, fed
    assert ENGINE_TEXT in fed, fed


def test_原提示逐字保留再挂两轮() -> None:
    """形状是 `[*原 messages, assistant 上一轮原文, user 回喂段]`。

    原 messages 一字不动是承重的：schema 卡片与那四条硬约束全在里面，回喂时把它换掉或裁掉，
    模型就是在对着一句错误消息重写一条它看不见表结构的 SQL——那已经不是重试，是第二次盲猜。
    """
    base = _base_messages()
    messages = build_retry_messages(
        base,
        previous_draft=PREVIOUS_DRAFT,
        sql_final=FAILED_SQL,
        error_message=ENGINE_TEXT,
    )
    assert len(messages) == len(base) + 2
    assert messages[: len(base)] == base
    assert messages[-2] == {"role": "assistant", "content": PREVIOUS_DRAFT}
    assert messages[-1]["role"] == "user"


def test_传入的列表不被修改() -> None:
    """调用方（pipeline）拿着那份 messages 还要留作它用，回喂不能是原地改。"""
    base = _base_messages()
    snapshot = [dict(m) for m in base]
    build_retry_messages(
        base,
        previous_draft=PREVIOUS_DRAFT,
        sql_final=FAILED_SQL,
        error_message=ENGINE_TEXT,
    )
    assert base == snapshot
    assert len(base) == 2


def test_措辞里那句封顶声明逐字在场() -> None:
    """封顶那一格只在措辞里对外可见：代码里是常量、配置里没有键，所以这句被逐字钉死。"""
    messages = build_retry_messages(
        _base_messages(),
        previous_draft=PREVIOUS_DRAFT,
        sql_final=FAILED_SQL,
        error_message=ENGINE_TEXT,
    )
    assert "最后一次机会" in messages[-1]["content"]


def test_第二层那三枚码的正文都不被加工() -> None:
    """`too_costly` 与 `dry_run_unreadable` 的正文是我们按读数拼的、不含源库原话，同样逐字进。

    这三枚都要能原样过去，是因为"改写就是把证据洗成猜测"这一条不挑来源：把估行那句改成
    "这条 SQL 太慢，请缩小范围"，模型就再也读不到 `peak` 与上限这两个数，只能猜要往哪一列加条件。
    长文本那一发同理——截断也是一种改写。
    """
    long_tail = "（后面还跟着两百个字符的驱动原文，截掉就没人知道是哪张表）" + "表" * 200
    bodies = (
        "[too_costly] 引擎预估单次扫描 30226 行，超过上限 100",
        "[dry_run_unreadable] EXPLAIN 没有给出可用估行（3 行全读不出来，"
        "引擎也未自证免扫），无法判定代价",
        "[dry_run_engine_rejected] 源库在执行 EXPLAIN 时就拒了这条 SQL："
        "1054: Unknown column 'nope' in 'field list'" + long_tail,
    )
    for body in bodies:
        messages = build_retry_messages(
            _base_messages(),
            previous_draft=PREVIOUS_DRAFT,
            sql_final=FAILED_SQL,
            error_message=body,
        )
        assert body in messages[-1]["content"], body
