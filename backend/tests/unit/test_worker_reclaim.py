"""worker 心跳循环三条语句的**形状**：刷哪个钟、凭什么判僵尸、保留期删哪张表。

接缝选在编译出来的 SQL 文本上，与 `test_job_queue.py` 同一条理由（verification §2.1 的
`job_queue` 行）：工单 018 的三条已定口径——"心跳走独立连接""`pending` 行也算僵尸候选"
"回收只清 `sync_job_event`"——全是"语句长什么样"的事，写错一个谓词在真库上照样可能绿。
阈值边界（179s/181s）判的是行为，归 `tests/integration/test_worker_heartbeat_pg.py`。
形状之外还钉两格小的：`cadence()` 的"键→入参"映射，和"阈值必须是整数"这道闸门——两者都在
语句文本里看不见（串位与截断都渲染得出一句合法 SQL），只能在构造方当场断。

期望值口径：docs/metadata-model.md §6（回收那行的 SQL 原文与 `heartbeat 超时` 字面）
+ §6 as-built(P3 开工前拍板) 第 4/9 条 + 工单 018「已定口径」。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.dialects import postgresql

from tests.conftest import load_script

# worker 的循环体按文件路径加载（`scripts/` 不是包，理由见 tests/conftest.py 的 load_script）
RUN_WORKER = load_script(
    "run_worker_for_reclaim_tests",
    Path(__file__).resolve().parents[2] / "scripts" / "run_worker.py",
)


def _sql(stmt: object) -> str:
    """编译成 PG 方言文本（字面量全渲染），压平排版，去掉 schema 与表限定。

    与 `test_job_queue._sql` 同一套手法：三条语句的入参都在**构造方**手里（job_id、阈值秒数、
    保留天数由 worker 从 `Settings` 传入，回收条目是本模块常量），所以 `literal_binds` 渲染得动，
    而"阈值不硬编码"这件事恰好能渲染成断言——换一个入参重编译，文本里的数字必须跟着换。
    """
    rendered = str(
        stmt.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True, "render_postcompile": True},
        )
    )
    rendered = re.sub(r"\b\w+\.sync_job_event\b", "sync_job_event", rendered)
    rendered = re.sub(r"\b\w+\.sync_jobs\b", "sync_jobs", rendered)
    flattened = re.sub(r"\s+", " ", rendered)
    return flattened.replace("sync_jobs.", "").replace("sync_job_event.", "").strip()


def test_刷心跳只动钟_并且只碰未结束的rows() -> None:
    """心跳那一格只许写 `heartbeat_at`：它是"这个作业还在动"的声明，不是进度也不是终局。

    `status IN ('pending','running')` 的守卫是竞态的兜底：`run_sync` 的收尾与心跳驱动
    分属两个事务，收尾先落定时这一句必须打不中——给已结束的作业刷钟，等于用一条 UPDATE
    把"什么时候结束的"这件事再搅浑一次（工单 018 验收 1 的独立连接路径靠的就是这条语句）。
    """
    sql = _sql(RUN_WORKER.heartbeat_update_stmt(7))
    assert sql.startswith("UPDATE sync_jobs SET heartbeat_at=now()"), sql
    assert "id = 7" in sql, sql
    assert "status IN ('pending', 'running')" in sql, sql
    # 反断言：心跳语句不许变成第二个写终局的地方（`errors`/`finished_at`/`status` 的赋值都不该在）
    assert "finished_at" not in sql and "errors" not in sql and "failed" not in sql, sql


def test_回收扫描的谓词与终局写法逐字对上文档那条SQL() -> None:
    """metadata-model §6"进程崩溃留下僵尸 running"那一行的原文就是这条语句的规格：

    `UPDATE sync_jobs SET status='failed', errors = errors || '[{"code":"reclaimed",
    "detail":"heartbeat 超时"}]'::jsonb WHERE status IN ('pending','running') AND
    heartbeat_at < now() - interval '180 seconds'`。

    三处缺一不可：① `pending` 也算僵尸候选（已定口径；NULL 心跳的排队行匹配不上比较，
    天然排除在"超时"之外——判据是心跳而不是存在时长）；② 追加走 `errors` jsonb 数组拼接，
    **没有** `error` 这一列；③ `RETURNING id` 让"我回收了哪些"由这一条语句自己回答，
    终局事件只补给真正被改写的行（两个 worker 同扫时后到的那个拿到空列表，不会重复补写）。
    """
    sql = _sql(RUN_WORKER.reclaim_update_stmt(180))
    assert "SET status = 'failed'" in sql or "SET status='failed'" in sql, sql
    # 追加写法照文档：`errors = errors || '…'::jsonb`，是 jsonb 拼接而不是覆盖
    assert "errors = (errors || CAST(" in sql or "errors=(errors || CAST(" in sql, sql
    assert "AS JSONB)" in sql, sql
    assert '[{"code":"reclaimed","detail":"heartbeat 超时"}]' in sql, sql
    assert "status IN ('pending', 'running')" in sql, sql
    assert "heartbeat_at <" in sql and "make_interval" in sql, sql
    assert "RETURNING id" in sql, sql


def test_回收条目的形状逐字对上文档那一格() -> None:
    """§6 那一格给的是**字面量**：`[{"code":"reclaimed","detail":"heartbeat 超时"}]`。

    期望值直接抄文档原文而不是 `json.loads` 再比回去——后者只能证明"序列化没变形"，
    证明不了 code 与 detail 这两个词没被改写（口径第 4 条：不许从实现反推）。
    """
    assert RUN_WORKER.RECLAIM_ENTRY_JSON == '[{"code":"reclaimed","detail":"heartbeat 超时"}]'
    assert json.loads(RUN_WORKER.RECLAIM_ENTRY_JSON) == [
        {"code": "reclaimed", "detail": "heartbeat 超时"}
    ]


def test_阈值是入参不是写在SQL里的字面量() -> None:
    """已定口径"阈值与间隔全部来自 Settings，键不许硬编码"在语句层的投影。

    换一个入参重编译，文本里的秒数必须跟着换——这一条钉的是"builder 吃参数"，
    而"参数来自 `Settings.extract.stale_job_reclaim_s`"由 worker 循环本体与验收 6 的
    grep 各自负责；179s/181s 的**行为**边界归集成用例，这里编译不出真库时钟。
    """
    assert "42" in _sql(RUN_WORKER.reclaim_update_stmt(42))
    assert "180" not in _sql(RUN_WORKER.reclaim_update_stmt(42))
    assert "42" not in _sql(RUN_WORKER.reclaim_update_stmt(180))


def test_保留期只删事件表_天数走参数() -> None:
    """已定口径"回收只清 `sync_job_event`，`data/results/` 的 csv 一个字节都不动"。

    语句层能钉的是这条 DELETE 的**目标表**：编译文本里只许出现 `sync_job_event`，
    不许出现 `sync_jobs`（删作业行等于连终局一起销毁，那不是保留期而是灭迹）。
    天数与阈值同一条理由必须是入参（`Settings.result.retention_days`，30）。
    """
    sql = _sql(RUN_WORKER.retention_delete_stmt(30))
    assert sql.startswith("DELETE FROM sync_job_event"), sql
    assert "created_at <" in sql, sql
    assert "sync_jobs" not in sql, sql
    assert "30" in sql and "31" not in sql, sql
    assert "31" in _sql(RUN_WORKER.retention_delete_stmt(31)), sql


def test_cadence_把三个键各喂给对的参数() -> None:
    """已定口径第 2 条的最后一米："哪个键进哪个入参"得有地方可钉。

    三条语句各自都只证到"builder 吃参数"，`loop()` 里那三行赋值落在它们之外。三个键给成互不
    相同的值（11 / 181 / 31）断一次映射。本轮实测过这句话：把 `interval_s` 与 `stale_seconds`
    两个键互换后跑完整轮用例，短摘要里只红这一条——也就是说没有它，串位是无人看守的，而真效果
    是刷钟每 181s 一次、回收阈值 11s：正在跑的作业会被**自己的** worker 判成僵尸。
    """
    settings = SimpleNamespace(
        extract=SimpleNamespace(heartbeat_interval_s=11, stale_job_reclaim_s=181),
        result=SimpleNamespace(retention_days=31),
    )
    assert RUN_WORKER.cadence(settings) == {
        "interval_s": 11,
        "stale_seconds": 181,
        "retention_days": 31,
    }


def test_阈值不是整数就当场抛_不许拼出一条静默少一秒的语句() -> None:
    """`make_interval` 把数**拼进 SQL 文本**，所以"它是不是整数"是这条语句自己的责任。

    原来的 `int()` 闸门不够：实测 `secs=180.7` 被截成 180、`secs=True` 变成 1，而截断后的文本
    跟本来就该是 180 长得一模一样（阈值少一秒这种事在编译文本里看不出来）。字符串那条是
    `int()` 顺手挡的（`ValueError`），这里要的判据是"任何非 int 都不许走到渲染那一步"。
    """
    for bad in (180.7, True, "7; DROP TABLE x", None):
        with pytest.raises((TypeError, ValueError)):
            RUN_WORKER.reclaim_update_stmt(cast("int", bad))
    # 反向证据：整数照旧渲染得出那个数（闸门不是把入参全毙了）
    assert "180" in _sql(RUN_WORKER.reclaim_update_stmt(180))
