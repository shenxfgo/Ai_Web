"""`scripts/seed_admin.py` 的口令来源。

口令两条通道都认：进程环境变量（一次性，推荐）或 backend/.env。谁优先正是
"空口令拒绝执行"这条保护的要害——把"显式给空"判成"没给"，脚本就会从 .env 里捡起
另一份口令继续建号，于是保护只在没配过 .env 的机器上生效。

期望值口径：`docs/roadmap.md` 分组 5（AIWEB_BOOTSTRAP_ADMIN_PASSWORD 空则拒绝执行）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import load_script

BACKEND_ROOT = Path(__file__).resolve().parents[2]
SEED = load_script("seed_admin", BACKEND_ROOT / "scripts" / "seed_admin.py")
PWD_KEY = SEED.PWD_KEY


def _env_file_holds(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> None:
    """把 .env 换成一份受控内容：不能真去改开发者机器上的 backend/.env。"""
    monkeypatch.setattr(SEED, "dotenv_values", lambda _path: values)


def test_环境变量显式给空串时不从env文件捡起口令(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PWD_KEY, "")
    _env_file_holds(monkeypatch, {PWD_KEY: "留在.env里的口令"})
    assert SEED._config(PWD_KEY) == ""


def test_环境变量里没出现过该项时才读env文件(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PWD_KEY, raising=False)
    _env_file_holds(monkeypatch, {PWD_KEY: "留在.env里的口令"})
    assert SEED._config(PWD_KEY) == "留在.env里的口令"
