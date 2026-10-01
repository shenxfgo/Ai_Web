"""`sync_jobs.progress` 那一格的算法（§2.5 的 `numeric(5,2)` 百分比）。

为什么单独一张文件而不塞进 `test_sync_rows.py`：那张钉的是"行字典的形状"（哪些键能进
upsert 的 values），这一张钉的是"进度条折算"，两者唯一的共同点是都住在 `sync_service`。

**这一格与 `sync_job_event` 的分工要守住**（§2.8 决策 3）：事件表是进度的**序列真相**
（哪一帧、什么 stage、游标能不能续），`progress` 只是一个**派生百分比**，不承载序列。
所以这里的算法只吃 `done`/`total` 两个数，不许有人将来把 phase 塞进来。

期望值全部手算（`2/3=66.666…→66.67`、`1/8=12.5`），不是拿实现里的算式重跑一遍。
"""

from __future__ import annotations

from app.services.sync_service import progress_of


def test_进度百分比按对象级done与total折算() -> None:
    assert progress_of({"done": 3, "total": 3}) == 100.0
    assert progress_of({"done": 2, "total": 3}) == 66.67
    assert progress_of({"done": 1, "total": 8}) == 12.5
    assert progress_of({"done": 0, "total": 10}) == 0.0


def test_分母还没定出来时不写这一格() -> None:
    """discover 之前 `total` 是 0：这一格必须**不回值**而不是回 0.0。

    0.0 与"还不知道分母"在两处会被读成不同的话：SQL 侧那一列本来有
    `DEFAULT 0`（§2.5），回 0.0 等于替它写一次"我确定现在是 0%"；而不回值是"这一帧
    没有进度可宣布"，那一行留在它原来的值上。`total` 为负是桩件写错，同样不许折算。
    """
    assert progress_of({"done": 0, "total": 0}) is None
    assert progress_of({"done": 5, "total": -1}) is None
    assert progress_of({}) is None


def test_事件负载里那五个键之外的东西不参与折算() -> None:
    """`_Progress.as_dict()` 交回的是五个键（§2.8 的 counters），折算只认其中两个。

    这一条挡的是"顺手把 cards/base_table 也算进百分比"——那三个是**另一本账**
    （卡片数与两类对象数），拿它们去除分母会画出一条根本不存在曲线。
    """
    frame = {"done": 4, "total": 10, "base_table": 9, "view": 1, "cards": 0}
    assert progress_of(frame) == 40.0
