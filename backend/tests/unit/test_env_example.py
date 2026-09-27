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


def test_empty_values_have_no_inline_comment() -> None:
    """`KEY=    # 说明` 里那段说明会被当成值读进来。

    python-dotenv 不剥行内注释（它只认整行注释），pydantic-settings 也跟着错。
    真炸过的三处：`AIWEB_JWT__SECRET` 变成模板里的生成命令（于是 check_env 报
    "已自定义"，而实际是所有仓库克隆者都知道的公开串）、`AIWEB_FERNET__KEYS` 变成
    一串非空垃圾（于是"未配置"判定失效）、`AIWEB_BOOTSTRAP_ADMIN_PASSWORD` 变成
    "seed_admin 用；留空则该脚本拒绝执行"（于是 seed_admin 拿这句垃圾建出 admin）。
    """
    offenders = [
        line.split("=", 1)[0]
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if re.match(r"^AIWEB_[A-Z0-9_]+= *#", line)
    ]
    assert not offenders, f"这些键值为空却带了行内注释，注释会被读成值：{offenders}"
