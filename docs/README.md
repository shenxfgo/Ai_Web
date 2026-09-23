# 文档索引

Ai_Web（AI 问数）的设计参考文档。内容以实施方案为准，规划阶段的讨论性文字已剔除。

| 文件 | 一句话内容 |
|---|---|
| [architecture.md](./architecture.md) | 系统上下文、前后端组件拆分、三种存储（元数据库 / `knowledge/` markdown / `data/results/` 文件）、NL2SQL 流水线、四层检索阶梯、SSE 事件契约与完整 API 端点表。 |
| [nl2sql-safety.md](./nl2sql-safety.md) | 三层只读防御全文：sqlglot 正向 AST 白名单、`comments=False` 重生成不变式与 hint 拒绝、引擎 EXPLAIN dry-run、会话只读与超时、行上限、凭据 Fernet 处理、结果文件目录穿越防护，以及作为测试 oracle 的 SQL 攻击语料。 |
| [metadata-model.md](./metadata-model.md) | 元数据 DDL 与逐表语义：`table_uid` md5 唯一性模型、`UNIQUE(datasource_id, catalog_name, schema_name, table_name)` 与 MySQL/PG 的 catalog/schema 映射、人工列与同步列分离（COALESCE 保护）、`meta_relation.source_kind` 与删除差分、同步任务/心跳/僵尸回收，以及各方言批量抽取 SQL 原文。 |
| [kb-workflow.md](./kb-workflow.md) | `knowledge/<ds>/<schema>.<table>.md` markdown 覆盖层：front-matter、`## 自动生成` 与 `## 人工维护` 的覆写契约、单向回写、表级知识卡片模板与分块策略、枚举 distinct 值采样策略。 |
| [roadmap.md](./roadmap.md) | P1–P10 阶段拆分（目标、涉及文件、验收标准、踩坑预警、人日与依赖图）、配置四层归属（L1 密钥 / L2 部署 / L3 运行 `app_settings` / L4 数据源级）与 env 键清单、启动自检项、Makefile/dev.ps1 目标表。 |
| [verification.md](./verification.md) | 演示库 `ai_web_demo`（9 张业务表 + 1 视图、只建不删守卫、`aiweb_ro` SELECT-only 账号）、测试分层与无 PG 时的 skip 策略、15 步端到端手测清单、SSE 冒烟步骤。 |
| [ui-design.md](./ui-design.md) | 前端设计契约（专业工具风）：`--aw-*` 令牌唯一来源与裸值禁令、Element Plus 按需接入（`base.css` + `--el-*` 覆盖，不引 `dist/index.css`、不用 sass）、外壳与页面骨架、空/加载/失败三态文案规范、图表色板与可访问性、每页验收清单。 |
| [adr/](./adr/) | 9 条不可逆决策，每条 1 段：元数据库复用远端 PG 用 schema 隔离、只读三层防御与正向白名单、pgvector 可插拔、结果集落文件、`table_uid` 指纹算法、`knowledge/` 覆盖层单向回写、Fernet 而非 KMS、权限止于数据源级、不容器化。 |

术语以仓库根的 [CONTEXT.md](../CONTEXT.md) 为准——它是纯词汇表，实现细节不写进去。
文档里出现的词若与它冲突，以 `CONTEXT.md` 为准并回来修文档。

阅读顺序建议：先 architecture.md 建立整体图景 → 改安全相关代码前必须读 nl2sql-safety.md →
动元数据结构时读 metadata-model.md → 补录业务语义时读 kb-workflow.md →
写前端页面前读 ui-design.md →
排期与配置问题看 roadmap.md → 自测与验收看 verification.md。
