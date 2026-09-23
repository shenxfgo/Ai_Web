# pgvector 是可插拔增强，不是检索主链路的依赖

检索主链路是四层阶梯（结构化关键词 → LLM 目录摘要 → 全卡片取回 → 判为不可答），
向量检索只是其中一档的加分项；`AIWEB_EMBEDDING__DIMENSION=0` 时整条向量路关闭，系统仍然可用。

**为什么**：目标 PG 上 `vector` 扩展非 trusted，建不建不由应用决定；把主链路压在一个我们控制不了的
扩展上，等于把可用性交给 DBA 的排期。**后果**：`retriever.search()` 是 `Protocol`，P2 的 `LikeRetriever`
与 P4 的 `HybridRetriever` 可互换，"向量挂了降级回关键词"因此是生产容错分支而不是临时补丁。
