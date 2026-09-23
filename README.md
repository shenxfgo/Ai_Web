# Ai_Web · AI 问数

从已有业务库（MySQL / PostgreSQL）抽取表结构、注释、索引与表间关系，沉淀成可维护的知识库；
在界面上用自然语言提问，由 LLM 生成 SQL、经守卫校验后**只读执行**，返回表格 + 图表 + 结论。

当前进度：**P1 脚手架已完成**（前后端可跑通、体检脚本可用）；P2 起进入问数闭环。
设计文档见 [docs/](docs/README.md)，实施阶段见 [docs/roadmap.md](docs/roadmap.md)。

## 目录

```
backend/    FastAPI + SQLAlchemy(async) + Alembic，Python 3.12，包管理用 uv
frontend/   Vue 3 + TS + Vite + Element Plus + ECharts
docs/       架构 / 安全守卫 / 元数据模型 / 知识文件工作流 / 阶段计划 / 验收步骤
scripts/    bootstrap.py（装依赖 + 生成 .env）、dev.ps1（Windows 下的 make）
data/       查询结果集落盘目录（不进版本库）
knowledge/  人工维护的表知识 Markdown（进版本库，是知识的一部分）
```

## 快速开始

前置：Python 3.12、[uv](https://docs.astral.sh/uv/)、Node 20+、npm。

```bash
python scripts/bootstrap.py        # 装依赖 + 生成 backend/.env（随机 JWT/FERNET 密钥）
# 编辑 backend/.env：填 AIWEB_PG__*（元数据库）与 AIWEB_LLM__*（OpenAI 兼容端点）
make check-env                     # 体检：缺什么会直接说，含 fail 则退出码非 0
make migrate                       # alembic upgrade head
make dev-backend                   # 终端 A：127.0.0.1:8000
make dev-frontend                  # 终端 B：127.0.0.1:5173
```

Windows 上没有 `make` 时，把上面的 `make X` 换成 `powershell -File scripts\dev.ps1 X`。
`make dev` 在 Windows 上会转交 `dev.ps1 dev` 并行拉起前后端。

打开 http://127.0.0.1:5173 应看到"后端连通性"卡片显示 `status=ok`；
接口文档在 http://127.0.0.1:8000/api/docs。

## 配置

所有键都在 [backend/.env.example](backend/.env.example) 里，命名规则 `AIWEB_<组>__<键>`（双下划线表嵌套）。
四类配置的落点不同，不要混：

| 层 | 内容 | 存放位置 |
|---|---|---|
| L1 密钥 | `JWT__SECRET`、`FERNET__KEYS`、LLM/PG 口令 | 只放 `.env` / 进程环境变量，**禁止入库**（运行时 KV 接口会拒收疑似密钥的值） |
| L2 部署 | 数据库地址、目录、日志级别 | `.env` |
| L3 运行时 | 检索条数、超时、开关 | `app_settings` 表，界面可改（60s TTL 缓存） |
| L4 数据源级 | 每个源库的连接与同步策略 | 数据源表，口令 Fernet 加密 |

`backend/.env` 已在 `.gitignore` 中，仓库只提交 `.env.example`。

## 常用命令

| 命令 | 作用 |
|---|---|
| `make check-env` | 环境体检（配置形态、目录可写、元数据库连通与扩展、LLM 探活） |
| `make migrate` | 元数据库升级到最新 schema |
| `make lint` / `make fmt` | 后端 ruff 检查 / 格式化 |
| `make typecheck` | 前端 `vue-tsc --noEmit` |
| `make test` | 后端 pytest（未配 `AIWEB_PG_TEST_DSN` 时自动跳过需要真库的用例） |
| `make check` | lint + typecheck + test，提交前总闸 |
| `make clean` | 只清构建产物与缓存，**不动数据库、结果集和 knowledge/** |

## 两条硬约束

1. **源库只读**：连接用只读账号，会话级 `READ ONLY` + 语句超时 + 行数上限，SQL 先过白名单守卫再 `EXPLAIN` 试算。
   规则与被拒语料见 [docs/nl2sql-safety.md](docs/nl2sql-safety.md)。
2. **业务数据不进元数据库**：元数据库只存"关于表的知识"；查询结果写 `data/results/` 下的 CSV 供下载。

## 依赖版本的理由

前端 `typescript` 锁 `~5.9`（`overrides` 强制子依赖同版本）：TS 7 是分线大版本，`vue-tsc` 与
`@typescript-eslint` 的 peer range 尚未覆盖，混装会在类型检查阶段报难以定位的错。
`echarts` 先用 5.x、`vite` 需要能设 `server.compress=false`（SSE 反缓冲）。
