# pgvector 是可插拔增强，不是检索主链路的依赖

检索主链路是四层阶梯（结构化关键词 → LLM 目录摘要 → 全卡片取回 → 判为不可答），
向量检索只是其中一档的加分项；`AIWEB_EMBEDDING__DIMENSION=0` 时整条向量路关闭，系统仍然可用。

**为什么**：目标 PG 上 `vector` 扩展非 trusted，建不建不由应用决定；把主链路压在一个我们控制不了的
扩展上，等于把可用性交给 DBA 的排期。**后果**：`retriever.search()` 是 `Protocol`，P2 的 `LikeRetriever`
与 P4 的 `HybridRetriever` 可互换，"向量挂了降级回关键词"因此是生产容错分支而不是临时补丁。

> **可插拔的边界（as-built，P2-0005）**：免掉的是**运行期对 embedding 端点的依赖**，
> 免不掉的是**建库期对 `vector` 类型的依赖**——`kb_card.embedding` 的列宽来自
> `AIWEB_EMBEDDING__DIMENSION`，列类型里带维度是 pgvector 的硬约束（`vector(0)` 非法、
> 无界 `vector` 又建不了普通 HNSW），所以 `0005` 在维度为 `0` 时直接拒绝建表并给中文 hint，
> 而不是猜一个默认维度。向量路是否真的走向量由 `settings.embedding.configured`
> （base_url/key/model 齐备）决定，与列宽是两个正交的开关。
