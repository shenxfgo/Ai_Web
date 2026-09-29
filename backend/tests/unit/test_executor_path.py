"""CSV 路径拼装：工单 011 验收 ⑤ + safety §7 的目录穿越防护（写入侧与下载侧）。

口径来自 §7：
- 文件名由**服务端生成**，接口层没有任何入参能指定路径；
- 解析后的最终路径必须仍落在结果目录内——拒绝 `..`、绝对路径、空字节、符号链接/junction 逃逸。

011 钉的是"生成的名字一定安全、且拼装函数对任何越界的 run_id 都拒绝"；013 补上下载侧那一半
（`resolve_result_file`）——那里输入来自客户端，所以形状门、realpath 落回目录内、文件在不在
三件事必须塌成**同一档**错误。端点本体归 P8。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from app.core.errors import ResultFileUnavailable
from app.services.nl2sql.executor import (
    new_run_id,
    resolve_result_file,
    result_csv_path,
)


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


# ============================ 下载侧（safety §7 的另一半，工单 013）


def _write(tmp_path: Path, run_id: str) -> Path:
    path = result_csv_path(tmp_path, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("a,b\n1,2\n", encoding="utf-8-sig")
    return path


def test_下载侧拿得到真写下去的那个文件(tmp_path: Path) -> None:
    run_id = new_run_id()
    written = _write(tmp_path, run_id)
    got = resolve_result_file(tmp_path, run_id)
    assert got == written
    # 独立字面量再钉一次：`written` 是 `result_csv_path` 算出来的，拿它断同一个函数只证明自洽。
    assert got == tmp_path.resolve() / f"{run_id}.csv"
    assert got.read_text(encoding="utf-8-sig") == "a,b\n1,2\n"


@pytest.mark.parametrize(
    "bad",
    ["../../etc/passwd", "/abs/x", "a\x00b", "a/b", "..", "", "notmine"],
    ids=["穿越", "绝对路径", "空字节", "带斜杠", "上级", "空串", "没生成过的名字"],
)
def test_越界与不存在回的是同一档(tmp_path: Path, bad: str) -> None:
    """§7 那句"不区分'不存在'与'无权限'"的本体：**code 相同**。

    分档就等于给探测者一个 oracle——"这个名字在这台机上存在过"和"没这回事"是两个可数的事件。
    写入侧那一道 `ValueError` 在这里也必须被翻掉：客户端传来的字符串进不了 500。
    """
    with pytest.raises(ResultFileUnavailable) as ei:
        resolve_result_file(tmp_path, bad)
    assert ei.value.code == "result_file_unavailable"


def test_存在但读不到文件时也归同一档(tmp_path: Path) -> None:
    """合法名字、目录里却没有这个文件（清理作业收走了它）——同样不许换一种错误说法。"""
    with pytest.raises(ResultFileUnavailable) as ei:
        resolve_result_file(tmp_path, new_run_id())
    assert ei.value.code == "result_file_unavailable"


def test_三种失败的对外说法逐字相同(tmp_path: Path) -> None:
    """code 相同还不够，`message` 也不许带出"是哪一道门拦的"。

    这一档唯一被允许泄露的是"这个 id 拿不到文件"，具体为什么只有服务端日志知道。
    四个入参**逐字对上三道门**：`../x` 撞①形状门、`escape` 撞②realpath 门（目录里那条
    指向外面的链接，构造方式见 `_link_to_outside`）、没生成过的 id 与 `adir` 撞③"不是文件"门
    （后者是同名目录）。②和③各给两个入参是因为②最容易造假——只断"它也在同一档"而不证明
    它真被撞过，绿灯就是假的（双轴审查抓出的正是这一格）。
    """
    (tmp_path / "adir.csv").mkdir()
    _link_to_outside(tmp_path / "escape.csv", _secret_dir(tmp_path))
    caught = []
    for bad in ("../x", "escape", new_run_id(), "adir"):
        with pytest.raises(ResultFileUnavailable) as ei:
            resolve_result_file(tmp_path, bad)
        caught.append(ei.value)
    assert {(e.code, e.message, e.status_code) for e in caught} == {
        ("result_file_unavailable", "结果文件不可用", 404)
    }
    assert all(e.detail is None for e in caught)


def _secret_dir(tmp_path: Path) -> Path:
    """链接指向的目标：一个**目录**，里面放着不属于本结果集的同名文件。

    必须是目录是因为 Windows 上无特权可建的链接只有 junction（只能指目录）；
    POSIX 的符号链接指目录同样成立，所以两个平台走同一个形状。
    """
    secret = tmp_path.parent / f"{tmp_path.name}_outside"
    secret.mkdir(exist_ok=True)
    (secret / "escape.csv").write_text("secret\n", encoding="utf-8")
    return secret


def _link_to_outside(link: Path, target: Path) -> str:
    """在 `link` 位置建一条指向 `target` 的链接，返回实际用到的机制名。

    先试 POSIX 符号链接（在 Windows 上非管理员会报 winerror 1314），再退到 junction——
    Windows 建 junction **不需要特权**，所以"realpath 逃出目录"这一门在这台机器上是能真跑的。
    两者都失败才 skip，且只 skip 在"建不出链接"这一件事上（不把真实失败吞成跳过）。
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return "符号链接"
    except OSError as symlink_error:
        try:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]
            return "junction"
        except Exception as junction_error:
            pytest.skip(
                f"本机建不出指向外面的链接：symlink={symlink_error} junction={junction_error}"
            )


def test_目录里的链接指向外面时拒绝(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """§7 明写要拒绝"符号链接逃逸"，这一格就钉它——门②真被执行过，不是靠自跳蒙过去。

    逃逸的形态是 `data/results/<run_id>.csv` 这个位置本身是一条指向目录外的链接
    （符号链接或 junction 都算：守卫看的是 `resolve()` 之后的真实落点，不是链接类型）。

    "真的走到门②"由**服务端日志**证明，不靠"按代码顺序应该是它"——junction 的落点是目录，
    若哪天把两道门的顺序换掉，同一个 `ResultFileUnavailable` 会从门③出来，用例照样绿。
    """
    mechanism = _link_to_outside(tmp_path / "leak.csv", _secret_dir(tmp_path))
    resolved = (tmp_path / "leak.csv").resolve()
    assert not resolved.is_relative_to(tmp_path.resolve()), f"{mechanism} 没真的指到外面去"
    with caplog.at_level("WARNING"), pytest.raises(ResultFileUnavailable) as ei:
        resolve_result_file(tmp_path, "leak")
    assert "reason=realpath 逃出目录" in caplog.text
    assert ei.value.code == "result_file_unavailable"


def test_结果目录自己是指向别处的链接时照常放行(tmp_path: Path) -> None:
    """门②的**反面**：`result_dir` 本身是链接不算逃逸，root 跟着一起 resolve 到新位置。

    这是运维把结果盘挪去另一块盘的正当做法（§7 的 as-built 明写了这条），没有用例的话
    "跟着 resolve"随时可能被改成"只 resolve 文件"，把正当部署形态判成越界。
    """
    real = tmp_path.parent / f"{tmp_path.name}_real"
    real.mkdir(exist_ok=True)
    run_id = new_run_id()
    _write(real, run_id)
    as_link = tmp_path / "moved"
    _link_to_outside(as_link, real)
    assert resolve_result_file(as_link, run_id).is_file()
