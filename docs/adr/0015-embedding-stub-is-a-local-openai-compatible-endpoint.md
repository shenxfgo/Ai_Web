# 向量路的闸内替身是一个本地 OpenAI 兼容假端点（不是第二个实现，也不是 transport 注入）

**状态**：accepted（P4 开工前拍板，2026-10-02）

P4 需要向量路能"真跑"，而这台机器的 LLM 端点是 DeepSeek（`api.deepseek.com`，2 个模型），
**不提供 embedding 模型**，`AIWEB_EMBEDDING__{BASE_URL,API_KEY,MODEL}` 三键全空
（`check_env` 那一格打的是"未启用，检索走结构化路径"；pgvector 0.8.6 与 `vector(1536)` 列和 HNSW
索引倒是早在 0005 就建好了）。决定的形状：交付一个脚本 `scripts/fake_embedding_server.py`，
它是真的 HTTP 服务、真的 `/v1/embeddings`、真按 model 名返回定长向量（1536 与 1024 都能报），
向量内容由问句/卡片的**字符 n-gram 哈希**确定性生成。`.env` 把 embedding 三键指到它，
于是 `settings.embedding.configured` 变真、`/healthz` 的 `retrieval_mode` 变 `vector+keyword`、
维度校验与非 200 分支全部走**同一条生产代码**。

**为什么不是"第二个 `EmbeddingClient` 实现 + `AIWEB_EMBEDDING__PROVIDER=local_hash` 新键"**：
那要在生产代码里留一条只有开发机会走到的分支，还会翻掉 `metadata-model §2.6` as-built ④
"`provider` 恒写 `openai_compatible`"——architecture §5 只认 OpenAI 兼容端点，没有第二家可填。
假端点方案下这一条口径一个字不动。

**为什么不是"只在 conftest 里注入伪实现"**：那样 `dev.ps1 dev` 起的那个真进程跑不了向量路，
验收 1 的 reindex 进度 SSE 与验收 2 的三列排名就没有真跑证据，替身退化成"只证单测"。

**为什么只 subprocess 一层**（用户拍板，代价一并记下）：所有需要 embedding 客户端的用例都真起进程
——随机端口、等启动、`finally` 杀干净。换来的是 `base_url` 拼接、超时、非 200、维度不符这四件事
在闸里可达；付出的是闸内多一个进程的生命周期管理，Windows 上还叠着两条本机事实：
`psycopg` 3.3.6 async 在默认 `ProactorEventLoop` 上抛 `InterfaceError`（子进程要 `SelectorEventLoop`），
控制台 cp936（子进程要 `PYTHONIOENCODING=utf-8`）。纯函数的 RRF/融合/维度映射用例不在此列，
它们不碰 HTTP。`addopts` 不动（仍是 `-q --strict-markers`），向量 HTTP 用例挂 `live`，默认闸继续整轮真跑。

**语义边界（不许越界声称）**：字符 n-gram 哈希是**词面**相似，不是语义。所以
"销售额→`payment_record`"这类**语义命中**只有配了真端点才能签；真端点由用户自行配置
（密钥不经对话），到位前那一格在收口里写"只有用例、没有真跑"，与 P3 那四格同形。
