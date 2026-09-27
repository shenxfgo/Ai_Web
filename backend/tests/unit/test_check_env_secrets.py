"""`scripts/check_env.py` 的密钥判定：默认 JWT 密钥按环境分档。

判定本身早就写好了，这里补的是"它真的按环境分档"这件事的证据：prod 用默认密钥必须是
fail（非 0 退出码），local 只是 warn（否则谁都起不来）。

期望值口径：`docs/roadmap.md` 分组 5（JWT secret ≥32B → fatal）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.settings import Settings
from tests.conftest import load_script

BACKEND_ROOT = Path(__file__).resolve().parents[2]

CHECK_ENV = load_script("check_env", BACKEND_ROOT / "scripts" / "check_env.py")


def _jwt_verdict(environment: str) -> str:
    # 元素是 check_env.Check —— 跨模块的类不在这里重复声明
    checks: list[Any] = []
    # _env_file=None：不吃本机 .env，否则测的是"这台机器的密钥"而不是判定规则
    CHECK_ENV.check_secrets(Settings(_env_file=None, app={"environment": environment}), checks)
    return next(c.status for c in checks if c.name == "jwt.secret")


def test_默认jwt密钥在prod判fail() -> None:
    assert _jwt_verdict("prod") == "fail"


def test_默认jwt密钥在local只判warn() -> None:
    assert _jwt_verdict("local") == "warn"
