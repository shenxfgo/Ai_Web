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


def _vector_verdict(dimension: int, exts: dict[str, str]) -> list[Any]:
    checks: list[Any] = []
    settings = Settings(_env_file=None, embedding={"dimension": dimension})
    CHECK_ENV.check_embedding_gate(settings, checks, exts)
    return checks


def test_向量扩展在维度已定时缺失判fail_并给出建扩展的那句话() -> None:
    """roadmap §5 自检表第 2 行 as-built(P2-0005)：判据是 `DIMENSION>0`，不是"启用向量路"。

    0005 之后列类型 `vector(dim)` 就引用着这个扩展，端点配没配都一样。
    """
    got = {c.name: c for c in _vector_verdict(1536, {})}
    assert got["pg.ext.vector"].status == "fail"
    assert "CREATE EXTENSION vector" in got["pg.ext.vector"].hint
    assert "超级用户" in got["pg.ext.vector"].hint or "DBA" in got["pg.ext.vector"].hint


def test_扩展在位时只报版本号不报问题() -> None:
    got = {c.name: c for c in _vector_verdict(1536, {"vector": "0.8.6"})}
    assert got["pg.ext.vector"].status == "ok"
    assert got["pg.ext.vector"].message == "0.8.6"


def test_维度为0时报的是配置而不是扩展() -> None:
    """`DIMENSION=0` 时扩展在不在都没意义——0005 第一步就拒绝，hint 必须把人指向那个键。"""
    got = {c.name: c for c in _vector_verdict(0, {"vector": "0.8.6"})}
    assert got["embedding.dimension"].status == "fail"
    assert "AIWEB_EMBEDDING__DIMENSION" in got["embedding.dimension"].hint
    assert "pg.ext.vector" not in got
