# 同步作业在独立的 worker 进程里执行，API 只入队

`POST /api/sync/jobs` 只做权限校验 + 写一行 `pending` + 返回 202 与 `job_id`
（`schemas/sync.py:16` 那句"P2 是同步执行、200 直接带计数；P3 加 SSE 时改成 202 + job_id，路径不动"
至此兑现）。真正跑 `run_sync` 的是 `scripts/run_worker.py`：一个常驻进程，从队列里抢行、
分批读源库、逐表提交卡片、在独立连接上刷心跳、顺手扫僵尸。

**为什么不放在 API 进程内**：`AIWEB_RELOAD=true` 是本机默认（roadmap 分组 1），进程内跑作业意味着
**存一次盘就打断一次同步**——而被打断的那行 `running` 会占住 `ux_sync_running` 部分唯一索引，
该数据源此后再也点不动（这正是 007 的双轴审查花力气堵住的那类故障）。拆成两个进程之后，
`--reload` 与作业生命周期脱钩。

**为什么不用 FastAPI 的 BackgroundTasks**：它没有一个可以被 `GET` 的句柄。取消、重连、
"这个 job 现在在不在这"这些验收项都要查作业本身，那还是一张自己维护的表，不如直接用队列行。

**互斥靠数据库，不靠进程数**：互斥有两处，各管一件事——同一数据源只允许一个未结束作业，
由 `ux_sync_running`（部分唯一索引）保证；同一个作业只会被一个 worker 拿到，由 claim 那次
条件更新（`WHERE status='pending'`）的行数判定保证，确切 SQL 归 spec 定。
反过来也成立——**多开 API 进程不会多跑作业**，队列才是唯一的执行入口。

**后果**：
- 验收 4 的 kill 对象从 uvicorn 变成 worker 进程，"重启后回收 N 个僵尸 job"这句要在 worker 的启动日志里看。
- 心跳循环每轮都扫一次僵尸，而不是只在启动时扫：worker 长驻不重启是常态，只扫一次等于那个坑还在。
- P2 交付的 `test_sync_live.py` / `test_sync_pg.py` / `test_sync_cache_pg.py` 三张都在请求内等 `run_sync`
  返回，P3 必须改成"POST 拿 job_id → await worker 的循环体 → 照旧断言"。
  用例**不真起子进程**：SSE 与进程边界另有真进费用例钉，其余用例要的是可调试的堆栈。
- 本机跑起来是两条命令（`dev.ps1 up` 之外再加 worker），文档与脚本必须一起改，否则用户会遇到
  "点了同步但进度永远不动"——那正是入队成功而没人消费的形状。
  **as-built(P3 收口)**：本条的"一起改"已改完，但这行字面里的 `dev.ps1 up` 从来不是真目标名——
  `scripts/dev.ps1` 的清单里没有 `up`，起 API 的目标叫 **`dev`**（`dev-backend` / `dev-frontend` 是它的两个
  分件），worker 那一半是 **`worker`**（`make worker` 同义，`run_worker.py` 常驻）。两个命令名逐字核对过，
  记在这里是因为"文档承诺的命令本机跑不出来"正是本条要避免的那类故障。
