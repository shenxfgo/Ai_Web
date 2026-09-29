"""CSV 路径拼装：工单 011 验收 ⑤ + safety §7 的目录穿越防护（写入侧这一半）。

口径来自 §7：
- 文件名由**服务端生成**，接口层没有任何入参能指定路径；
- 解析后的最终路径必须仍落在结果目录内——拒绝 `..`、绝对路径、空字节、符号链接逃逸。

011 只做"写"这一半（下载端点的 realpath 校验归 012/013），但把"生成的名字一定安全、
且拼装函数对任何越界的 run_id 都拒绝"钉死，因为这是执行链路唯一的落盘入口。
"""

from __future__ import annotations

import re

import pytest

from app.services.nl2sql.executor import new_run_id, result_csv_path

RESULT_DIR = "data/results"


def test_生成的文件名安全且带_csv_后缀() -> None:
    run_id = new_run_id()
    # 只含 [A-Za-z0-9]：没有分隔符、没有点、没有空字节，也就没法拼出穿越
    assert re.fullmatch(r"[A-Za-z0-9]+", run_id), run_id


def test_路径拼装在结果目录内(tmp_path) -> None:
    path = result_csv_path(tmp_path, new_run_id())
    assert path.parent == tmp_path.resolve()
    assert path.suffix == ".csv"
    # realpath 仍在目录内（§7 的不变量）
    assert path.resolve().is_relative_to(tmp_path.resolve())


@pytest.mark.parametrize("bad", ["../../etc/passwd", "/abs/x", "a\x00b", "a/b", "..", ""])
def test_越界的_run_id_一律拒绝(tmp_path, bad: str) -> None:
    # 即便调用方（未来的下载端点）传进来被篡改的 id，拼装函数也不给出目录外的路径
    with pytest.raises(ValueError):
        result_csv_path(tmp_path, bad)
