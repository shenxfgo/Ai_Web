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
    "③ 进 prompt 的表",
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
            error_message="没有匹配到任何表：请补录表注释 / 检查授权 / 点此同步",
        )
    )
    text = capsys.readouterr().out
    assert "error_code=no_schema_found" in text
    assert "补录表注释" in text
    assert "未到此步" in text  # 守卫没跑到，不能打 PASS/REJECT 假装跑过
