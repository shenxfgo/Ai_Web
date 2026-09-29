"""结果落 csv：工单 011 的"写成 data/results/*.csv"。

口径来自工单验收 ③——"CSV 里是完整上限行"：截断只丢探针行，落盘的是截断后留下的那
`row_limit` 行全量，而不是接口返回给前端的 preview_rows 那一小截。
序列化在调用方已经做完（serialize_cell），这里只负责写成合法 CSV。
"""

from __future__ import annotations

import csv

from app.services.nl2sql.executor import write_result_csv


def test_表头加全量行落进_csv(tmp_path) -> None:
    path = tmp_path / "run1.csv"
    columns = ["id", "amount", "note"]
    rows = [("1", "0.30", "订单甲"), ("2", "12.50", None)]
    write_result_csv(path, columns=columns, rows=rows)

    with path.open(encoding="utf-8-sig", newline="") as fh:
        read = list(csv.reader(fh))
    assert read[0] == ["id", "amount", "note"]
    assert read[1] == ["1", "0.30", "订单甲"]
    assert read[2] == ["2", "12.50", ""]  # None → 空单元格


def test_父目录不存在时自动建(tmp_path) -> None:
    path = tmp_path / "results" / "run2.csv"
    write_result_csv(path, columns=["a"], rows=[("1",)])
    assert path.exists()


def test_逗号与引号按_csv_规则转义(tmp_path) -> None:
    path = tmp_path / "run3.csv"
    write_result_csv(path, columns=["a"], rows=[('含,逗号 和"引号"',)])
    with path.open(encoding="utf-8-sig", newline="") as fh:
        read = list(csv.reader(fh))
    assert read[1] == ['含,逗号 和"引号"']
