"""`.env.example` 与 Settings 字段必须一一对应。

`extra="ignore"` 会静默吞掉拼错的键，体检脚本也跟着显示"未配置"，
这类错位只能靠断言拦住。
"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = BACKEND_ROOT / ".env.example"

_KEY = re.compile(r"^(AIWEB_[A-Z0-9_]+__[A-Z0-9_]+)", re.M)


def _env_keys() -> list[str]:
    return _KEY.findall(ENV_EXAMPLE.read_text(encoding="utf-8"))


def _field_paths() -> set[str]:
    from app.settings import Settings

    def walk(model: type, prefix: str) -> set[str]:
        found: set[str] = set()
        for name, field in model.model_fields.items():
            path = f"{prefix}{name}"
            annotation = field.annotation
            if hasattr(annotation, "model_fields"):
                found |= walk(annotation, f"{path}.")
            else:
                found.add(path)
        return found

    return walk(Settings, "")


def test_env_example_keys_are_recognised() -> None:
    keys = _env_keys()
    assert keys, ".env.example 没解析出任何 AIWEB_* 键"
    unknown = [
        key.removeprefix("AIWEB_").replace("__", ".").lower()
        for key in keys
        if key.removeprefix("AIWEB_").replace("__", ".").lower() not in _field_paths()
    ]
    assert not unknown, f".env.example 里的键在 Settings 中不存在：{unknown}"


def test_settings_have_documented_fields() -> None:
    documented = {key.removeprefix("AIWEB_").replace("__", ".").lower() for key in _env_keys()}
    missing = sorted(_field_paths() - documented)
    assert not missing, f"Settings 有字段但 .env.example 未列出：{missing}"
