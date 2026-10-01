# 同步作业的队列介质是 PostgreSQL，接缝是一个 Protocol

`enqueue / subscribe / worker` 三个动作抽成一个 Protocol；P3 唯一的实现落在元数据库自己身上：
`sync_jobs` 行是队列元素（`pending → running` 靠抢 `ux_sync_running` 部分唯一索引），
`sync_job_event` 表是进度真相（append-only，带 `seq` 游标），PG 的 `LISTEN/NOTIFY` 只携带 `job_id`
当叫醒信号，不携带任何事实。

**为什么不 Redis**：本机没有 Redis（6379 无监听、无服务、无二进制），引入它等于给 P3 加一个
外部服务前置——它挂了同步就整体停摆，而 P3 的七条验收里没有一条需要吞吐能力。
文档里此前唯一一句"不引入 Redis"（`metadata-model §2.1`）管的是 token/会话存储，**不涉及作业队列**，
所以"队列用什么介质"在本 ADR 之前是一个没有决策的问题，不是一次对旧决定的推翻。

**为什么 NOTIFY 不当真相**：`NOTIFY` 在没有监听者的那一刻永久丢失，asyncpg 重连也不补。
所以推进度的每一个状态变更先落 `sync_job_event` 行，再 `NOTIFY job_id`；SSE 端点收到叫醒后按
`seq > cursor` 追读，客户端断开重连时从上次游标接着读，一条不丢。轮询 `sync_jobs.progress`
这一档被否掉了：真跑一次同步总耗时约 900ms，1s 轮询会把整段阶段序列读成"最后一次值"。

**为什么留 Protocol 这层抽象**：不是为将来换 Redis 预搭架子——是因为 worker 与 API 已经确定是
两个进程（见 ADR-0011），二者之间必须有一个明确的交接口，否则"入队"会退化成直接函数调用，
将来真要拆队列时没有可替换的位置。

**后果**：`sync_job_event` 是唯一一张确定会随时间线性增长的表，所以它的保留期（只清这张表，
不碰结果 csv）进了 P3 范围；`sync_jobs.progress` 那一列（实际类型是 `Numeric(5,2)`，不是文档某处
暗示的 JSONB）继续只做"一个百分比"，**阶段序列的真相只在事件表**——谁写它、按什么公式算，
归 P3 的 spec 定，不在这里承诺。

**as-built(P3 收口)**：上面那句"谁写它、按什么公式算"到 025 有了答案，答案是**本 ADR 的口径没动**——
`sync_service.progress_of()` 拿事件帧里的 `done/total` 算一个两位小数百分比，只有发帧的那几次才写这一格，
`total<=0` 时宁可留默认值 0 也不写一个假的 0%。它不是第二本账，因为它随时能由事件表重算；
而"轮询 `progress` 会把序列读成最后一次值"那句否决理由因此依旧成立，被否掉的这一档没有因为列活了而复活。
公式、三个接线用例与配置键的账都在 metadata-model §6 末 as-built(P3 收口)。
