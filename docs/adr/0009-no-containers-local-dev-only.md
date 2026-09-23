# 只做本地开发，不容器化

没有 Docker、没有 compose、没有 CI 镜像。后端 `uv run uvicorn`、前端 `npm run dev`，
统一入口是 `Makefile` 与 `scripts/dev.ps1`。

**为什么**：单人本机开发，容器只会把 Windows 上那些真实约束（控制台 cp936、PowerShell 5.1 要 UTF-8 BOM、
`npm` 是 `.cmd` shim 对 `CreateProcess` 不可见、`uv sync` 要本地 TMP + `UV_LINK_MODE=copy`、
asyncpg 需 Selector 事件循环）藏进镜像里，出问题时更难查。**后果**：所有脚本必须同时给 bash 和
PowerShell 两条路（Makefile 的 recipe 只能是纯 bash 命令，复杂逻辑下沉到 `scripts/bootstrap.py`），
且这些平台约束是代码里的一等公民，不是"环境问题"。
