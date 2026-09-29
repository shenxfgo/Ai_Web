"""`demo_ask.py` 的打印面（工单 012 验收 2 的机械钉子）。

验收 2 说的是"屏幕上出现的是守卫重生成后的那句"，而这一条**只有真跑一次才看得见**的风险最高：
脚本里 `print(out.sql_raw)` 与 `print(out.sql_final)` 写反，肉眼在"两句长得几乎一样"的日常里
根本分不出来。所以这里造一个两句**明显不同**的 outcome，把打印逐行验一遍。

按文件路径 import（`scripts/` 不是包，见 conftest 的 `load_script`）：验收项是"打印什么"，
起子进程就得连带把真 LLM、真源库一起拖进来，那不再是这一条的价钱。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.nl2sql import pipeline
from tests.conftest import load_script

BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEMO_ASK = BACKEND_ROOT / "scripts" / "demo_ask.py"


def _outcome(**overrides: object) -> pipeline.AskOutcome:
    base: dict[str, object] = {
        "question": "2024 年每个月的订单总金额是多少",
        "chat_session_id": 1,
        "message_id": 42,
        "model": "deepseek-chat",
        "sql_raw": "SELECT dt, SUM(amount) FROM ai_web_demo.order_main GROUP BY dt",
        "sql_final": ("SELECT dt, SUM(amount) FROM ai_web_demo.order_main GROUP BY dt LIMIT 1001"),
        "guard_result": {"ok": True, "violations": []},
        "executed": True,
        "columns": ["dt", "金额"],
        "rows": [["2024-01", "100.5"]],
        "row_count": 12,
        "run_id": "20260929abc",
        "result_file": "data/results/20260929abc.csv",
        "chart_spec": {"type": "line"},
        "conclusion": "全年共 12 个月有数据。",
        "steps": [("retrieve", 10), ("execute", 200)],
    }
    base.update(overrides)
    return pipeline.AskOutcome(**base)  # type: ignore[arg-type]


def test_屏幕上那句_sql_是守卫重生成后的而不是模型原文(
    capsys: pytest.CaptureFixture[str],
) -> None:
    demo = load_script("demo_ask_print", DEMO_ASK)
    out = _outcome()

    demo._print_outcome(out)
    text = capsys.readouterr().out
    lines = text.splitlines()

    raw_line = next(line for line in lines if "⑤ 模型给的 SQL" in line)
    final_line = next(line for line in lines if "sql_final" in line)
    assert "LIMIT 1001" not in raw_line  # ⑤ 那行是模型原文，探针不该在里面
    assert final_line == f"[⑥ sql_final] {out.sql_final}"


_MARKERS = (
    "② 检索",
    "③ JOIN 图",
    "④ 进 prompt 的表",
    "⑤ 模型给的 SQL",
    "⑥ 守卫判定",
    "⑦ 执行",
    "⑧ 图表选型",
    "⑨ 结论",
)


def test_每一步的产物都有一行_包括耗时合计(capsys: pytest.CaptureFixture[str]) -> None:
    """roadmap §P2 踩坑 ②：示踪弹的价值是"卡在哪一步"，所以步名与耗时都得在屏上。"""
    demo = load_script("demo_ask_print", DEMO_ASK)
    demo._print_outcome(_outcome())
    text = capsys.readouterr().out
    for marker in _MARKERS:
        assert marker in text, marker
    assert "retrieve 10ms → execute 200ms" in text
    assert "20260929abc.csv" in text  # 结果文件引用要能顺着查到那张 csv


def test_JOIN_图那两栏原样端出来_桥表路径与降级说明各一行(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """工单 014 的人工实录就看这几行：跨表路径到底进没进 prompt，屏幕上要一眼看得出。

    断"原样"而不是"有几行"：`join_lines` 的文本由 pipeline 的渲染器给出（桥表名在里面），
    脚本再格式化一次，实录读到的就和真 prompt 里的【可 JOIN】不是同一句话了。
    """
    demo = load_script("demo_ask_print", DEMO_ASK)
    demo._print_outcome(
        _outcome(
            join_lines=[
                "- ai_web_demo.payment_record.order_id → ai_web_demo.order_main.id，"
                "ai_web_demo.order_item.product_id → ai_web_demo.product.id"
            ],
            join_notes=["本次候选表之间没有可执行的跨表关联路径：只按单表回答，"],
        )
    )
    text = capsys.readouterr().out
    assert "ai_web_demo.order_item.product_id → ai_web_demo.product.id" in text
    assert "只按单表回答" in text


def test_早退时把_error_code_与那句下一步打在终局行(capsys: pytest.CaptureFixture[str]) -> None:
    demo = load_script("demo_ask_print", DEMO_ASK)
    demo._print_outcome(
        _outcome(
            sql_raw=None,
            sql_final=None,
            guard_result=None,
            executed=False,
            columns=[],
            rows=[],
            row_count=0,
            run_id=None,
            result_file=None,
            chart_spec=None,
            conclusion=None,
            steps=[("retrieve", 3)],
            error_code="no_schema_found",
            error_message="没有匹配到任何表：请补录表注释 / 检查授权 / 触发一次同步",
        )
    )
    text = capsys.readouterr().out
    assert "error_code=no_schema_found" in text
    assert "补录表注释" in text
    assert "未到此步" in text  # 守卫没跑到，不能打 PASS/REJECT 假装跑过


_EARLY_EXITS = {
    "no_schema_found": "没有匹配到任何表：请补录表注释 / 检查授权 / 触发一次同步",
    "sql_guard_rejected": "[top_level_not_select] 只允许 SELECT",
    "llm_bad_response": "模型返回无法解析：模型返回的 JSON 被截断（大括号不闭合），不做补全",
}


@pytest.mark.parametrize("code", sorted(_EARLY_EXITS))
def test_每一种拒答都跟一行下一步动作(capsys: pytest.CaptureFixture[str], code: str) -> None:
    """工单 013 验收 ①：`demo_ask.py` 把拒绝原因翻成人能读的**下一步动作**。

    "没查到"这三个字不是动作。这一格钉的是终局行之后必须还有一行"那我现在该做什么"，
    而且三档各说各的——同一句万能安慰对三种故障里的两种都是错的指引。
    """
    demo = load_script("demo_ask_print", DEMO_ASK)
    demo._print_outcome(
        _outcome(
            sql_raw="DROP TABLE orders" if code == "sql_guard_rejected" else None,
            sql_final=None,
            guard_result=(
                {
                    "ok": False,
                    "violations": [
                        {
                            "code": "top_level_not_select",
                            "field": "statement",
                            "message": "只允许 SELECT",
                        }
                    ],
                }
                if code == "sql_guard_rejected"
                else None
            ),
            executed=False,
            columns=[],
            rows=[],
            row_count=0,
            run_id=None,
            result_file=None,
            chart_spec=None,
            conclusion=None,
            steps=[("retrieve", 3)],
            error_code=code,
            error_message=_EARLY_EXITS[code],
        )
    )
    text = capsys.readouterr().out
    next_line = next((line for line in text.splitlines() if "下一步" in line), None)
    assert next_line is not None, text
    assert len(next_line) > len("       下一步：")


def test_守卫拒绝那一步的下一步说清了没执行也没落文件(capsys: pytest.CaptureFixture[str]) -> None:
    """验收 ②的接口层那一半：用户最怕的是"是不是已经删了"，第一句就要把这件事回答掉。"""
    demo = load_script("demo_ask_print", DEMO_ASK)
    demo._print_outcome(
        _outcome(
            sql_raw="DROP TABLE orders",
            sql_final=None,
            guard_result={
                "ok": False,
                "violations": [
                    {
                        "code": "top_level_not_select",
                        "field": "statement",
                        "message": "只允许 SELECT",
                    }
                ],
            },
            executed=False,
            columns=[],
            rows=[],
            row_count=0,
            run_id=None,
            result_file=None,
            chart_spec=None,
            conclusion=None,
            steps=[("retrieve", 3), ("schema", 1), ("prompt", 2), ("generate", 800), ("guard", 1)],
            error_code="sql_guard_rejected",
            error_message=_EARLY_EXITS["sql_guard_rejected"],
        )
    )
    text = capsys.readouterr().out
    assert "没有执行" in text
    assert "结果文件" in text
    assert "chat_messages.sql_raw" in text  # 模型原文去哪查，得说格子而不是"日志里看看"


def test_无匹配表那一步给的是今天真能走的三条动作(capsys: pytest.CaptureFixture[str]) -> None:
    demo = load_script("demo_ask_print", DEMO_ASK)
    demo._print_outcome(
        _outcome(
            sql_raw=None,
            sql_final=None,
            guard_result=None,
            executed=False,
            columns=[],
            rows=[],
            row_count=0,
            run_id=None,
            result_file=None,
            chart_spec=None,
            conclusion=None,
            steps=[("retrieve", 3)],
            error_code="no_schema_found",
            error_message=_EARLY_EXITS["no_schema_found"],
        )
    )
    text = capsys.readouterr().out
    # "点此同步"是界面话，CLI 这一层必须换成今天存在的入口（007 交付的同步端点）
    assert "/api/sync/jobs" in text
    assert "注释" in text
    assert "可见" in text
