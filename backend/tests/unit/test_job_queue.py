"""`JobQueue` 两条语句的**形状**：谁被写进去、谁被 claim 走、凭什么判定抢到了。

接缝选在编译出来的 SQL 文本上，与 `test_sync_upsert_sql.py` 同一个理由：工单 016 的三条
已定口径（互斥靠 `ux_sync_running`、claim 靠**条件更新行数**、排队中的 pending 也算未结束）
全是"语句长什么样"的事，写错一个谓词在真库上照样可能绿——比如 claim 漏掉外层
`status = 'pending'`，两个 worker 就会同时跑同一个作业，而这条要到 018 的僵尸回收
真跑起来才看得见。行为本身归 `tests/integration/test_sync_enqueue_pg.py`。

期望值口径：docs/adr/0011（作业在独立 worker 进程）+ docs/metadata-model.md §6
+ 工单 016「已定口径」。
"""

from __future__ import annotations

import re

from sqlalchemy.dialects import postgresql

from app.services import job_queue


def _sql(stmt: object) -> str:
    """编译成 PG 方言文本，压平排版，去掉 schema 与表限定（conftest 的 schema 每会话随机）。

    只编译不执行，所以带 Python 侧列默认的那几格（phase/progress/counters/warnings/errors）
    在这里渲染成 NULL——它们是执行期由列默认补进去的，P2 的 `_open_job`（`enqueue_stmt`
    的前身）一直是这么写的。
    于是这一片能钉的是"**赋值的是哪几列**"，那几列的名字必须留在断言里。
    """
    rendered = str(
        stmt.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    rendered = re.sub(r"\b\w+\.(sync_jobs)\b", r"\1", rendered)
    return re.sub(r"\s+", " ", rendered.replace("sync_jobs.", "")).strip()


def test_入队写的是_pending_行而且不许顺手打_running() -> None:
    """API 进程只许写 pending：写了 running 就等于"请求内等 run_sync"换个地方继续。

    `started_at` 不在这里写——它是 claim 那一步的钟，也是"响应返回时这一轮还没开始跑"
    的可断言证据（工单 016 验收 1）。
    """
    sql = _sql(job_queue.enqueue_stmt(7, 3))
    assert sql.startswith("INSERT INTO sync_jobs (datasource_id, triggered_by, status,"), sql
    assert "VALUES (7, 3, 'pending'" in sql, sql
    assert "running" not in sql, sql
    assert "started_at" not in sql and "heartbeat_at" not in sql, sql
    assert sql.endswith("RETURNING id"), sql


def test_入队把请求那一刻的两个事实一起写进行() -> None:
    """工单 021 的 `force` 与工单 029 的 `reindex_profile_id`：都是"请求读过、worker 读不到"的事。

    断形状而不是断默认值：`enqueue_stmt(7, 3)` 不带这两个关键字时，语句里照样得出现这两列
    （渲染成 NULL/false）——列从 `values` 里漏掉，worker 那一侧拿到的是**列默认值**，
    而"这颗作业没有靶"与"这颗作业的靶没人写"在库里长得一模一样，事后翻 job 行分不出来。
    """
    sql = _sql(job_queue.enqueue_stmt(7, 3, reindex_profile_id=42))
    assert "reindex_profile_id" in sql, sql
    assert "VALUES (7, 3, 'pending'" in sql, sql
    assert "42" in sql, sql

    plain = _sql(job_queue.enqueue_stmt(7, 3))
    assert "reindex_profile_id" in plain, plain


def test_claim_的互斥由_skip_locked_与_pending_条件共同保证() -> None:
    """两个 worker 各拿一个作业靠 `FOR UPDATE SKIP LOCKED`，"我抢到了"这件事靠**这一语句
    真正改到了几行**（`claim` 读的是 `RETURNING` 带回的行，PG 保证带回的就是被改写的行）。

    外层那句 `status = 'pending'` 是这个判据的依据，不是重复劳动：子查询拿到的锁只覆盖
    语句执行的瞬间，018 的僵尸回收会把僵尸改成终局，那时如果这里没带条件，判据就退化成
    "我动了一行"而不是"我把这个作业从 pending 抢到了 running"——两个 worker 会同时抽同一个源。
    """
    sql = _sql(job_queue.claim_stmt())
    assert sql.startswith("UPDATE sync_jobs SET status='running', phase='connect'"), sql
    # SET 里各项的先后由 SQLAlchemy 按列序排，而不是赋值顺序；`列=值` 两边也不留空格。
    # 这两处排版不是这一片要说的事，所以按项分开断言。
    assert "started_at=now()" in sql and "heartbeat_at=now()" in sql, sql
    assert "FOR UPDATE SKIP LOCKED" in sql, sql
    assert "WHERE id = (SELECT id FROM sync_jobs WHERE status = 'pending'" in sql, sql
    assert "ORDER BY id LIMIT 1" in sql, sql
    # 外层条件必须在 UPDATE 自己的 WHERE 里，而不是只在子查询里
    assert re.search(r"WHERE status = 'pending'.*FOR UPDATE", sql), sql
    assert "AND status = 'pending' RETURNING" in sql, sql
    assert "finished_at" not in sql and "counters" not in sql, sql


def test_claim_把靶随行带回但绝不在_claim_里改写它() -> None:
    """工单 029 验收 2 的第一跳：`reindex_profile_id` 得出现在 `RETURNING` 里。

    为什么在 SET 那一侧再断一次"没有它"：这一列的生命周期只到入队为止（靶是请求那一刻定的
    事实，worker 只是转手），而"顺手在 claim 里改靶"能把一颗普通同步偷偷变成 reindex。
    它与 `force` 同形，所以写法也同形——两列都只出现在带回的那一半。
    """
    sql = _sql(job_queue.claim_stmt())
    set_clause, _, returning = sql.partition(" WHERE ")
    assert "reindex_profile_id" in returning, sql
    assert "force" in returning, sql
    assert "reindex_profile_id" not in set_clause, set_clause
