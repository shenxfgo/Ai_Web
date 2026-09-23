# 元数据库复用用户已有的远端 PostgreSQL，用 schema 隔离

项目自身的全部结构化状态（用户、数据源、元数据快照、知识卡片、同步作业）落在远端 PG 的一个独立
`aiweb` schema 里，不独占数据库、不自建 PG 实例。

**后果**：`alembic_version` 也在 `aiweb` 内（`version_table_schema` 必须显式传）；`CREATE EXTENSION vector`
非 trusted，只能由 DBA 预建，因此向量能力是"检测到就用、没有就降级"（见 ADR-0003）；
同一实例上别人的对象与我们无关，迁移脚本永不碰 `public`。
