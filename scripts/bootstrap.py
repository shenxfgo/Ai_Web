"""一次性装好依赖并生成本地 .env。

Windows 上 uv 直接 `uv sync` 会在写包内 .exe 的 PE 资源时失败
（os error -2147024786）。把临时目录指到仓库内并改用 copy 安装可绕开，
这段环境变量准备放在脚本里做，Makefile 与 PowerShell 的展开规则不一致。
"""

from __future__ import annotations

import base64
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
UVTMP = BACKEND / ".uvtmp"

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        # Windows 控制台默认 cp936，中文提示会乱码
        _stream.reconfigure(encoding="utf-8", errors="replace")


def _resolve(cmd: list[str]) -> list[str]:
    """npm 在 Windows 上是 .cmd 垫片，CreateProcess 不认，得经 cmd /c 转发。"""
    exe = shutil.which(cmd[0])
    if exe is None:
        raise SystemExit(f"找不到命令：{cmd[0]}")
    if exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe, *cmd[1:]]
    return [exe, *cmd[1:]]


def run(cmd: list[str], cwd: pathlib.Path, env: dict[str, str] | None = None) -> None:
    print(f"+ {' '.join(cmd)}  ({cwd.name})")
    subprocess.run(_resolve(cmd), cwd=cwd, env=env, check=True)


def sync_backend() -> None:
    UVTMP.mkdir(exist_ok=True)
    env = dict(os.environ)
    tmp = str(UVTMP)
    env.update(TMPDIR=tmp, TMP=tmp, TEMP=tmp, UV_LINK_MODE="copy")
    run(["uv", "sync", "--all-groups"], BACKEND, env)


def install_frontend() -> None:
    run(["npm", "install", "--no-fund", "--no-audit"], FRONTEND)


def write_env() -> None:
    """从 .env.example 复制并把两个密钥键换成随机值，已存在则不动。"""
    target = BACKEND / ".env"
    if target.exists():
        print("backend/.env 已存在，不改写")
        return
    text = (BACKEND / ".env.example").read_text(encoding="utf-8")
    replacements = {
        "AIWEB_JWT__SECRET": secrets.token_urlsafe(48),
        # Fernet 要求"32 字节做 url-safe base64"，直接 token_urlsafe(32) 长度对不上
        "AIWEB_FERNET__KEYS": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
    }
    for key, value in replacements.items():
        text = re.sub(rf"(?m)^{key}=.*$", f"{key}={value}", text, count=1)
    target.write_text(text, encoding="utf-8")
    print("已生成 backend/.env，JWT_SECRET 与 FERNET_KEYS 为随机值")


def main() -> int:
    missing = [tool for tool in ("uv", "npm") if shutil.which(tool) is None]
    if missing:
        print(f"缺少命令：{'、'.join(missing)}，先装好并加入 PATH", file=sys.stderr)
        return 1
    sync_backend()
    install_frontend()
    write_env()
    print("\n下一步：填 backend/.env 的 AIWEB_PG__* 与 AIWEB_LLM__*，再跑 check-env")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
