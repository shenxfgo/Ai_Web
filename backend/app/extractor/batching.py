"""按批发 `IN` 的三件套：切刀、占位符、`IN` 片段。

从 `mysql.py` 搬过来只有一个原因：工单 024 的 PG 抽取器要用**同一套**批切分与同一套
`IN (:tbl_N)` 写法（020 定的形状），而跨方言互相 import 私有 helper 会让两个方言模块从此
绑死——改 MySQL 那一侧能碰坏 PG 这一侧。搬到这里之后，"批"的定义只有一个地方：
`slice_names` 那三个边界（整除不留空末批、末批只剩一张也成批、空名单回空列表）在 020 的
`tests/unit/test_extract_mysql_batching.py` 里钉着，两方言共用那份用例。

这一层不认识 Settings：批大小与批间隔的口径住在 `Settings.extract`（200 / 100），
由编排层（`sync_service`）读出来传进来——方言层再写一遍数字就是第二个定义点。
"""

from __future__ import annotations

from collections.abc import Sequence


def in_params(tables: Sequence[str], schema: str) -> dict[str, object]:
    if not tables:
        # `IN ()` 在 MySQL 里是语法错误；空范围必须由调用方（sync_service）提前短路，
        # 而不是让这条 SQL 发到源库去换一个 1064。
        raise ValueError("抽取范围里没有表：不该发这条查询")
    params: dict[str, object] = {"schema": schema}
    params.update({f"tbl_{i}": name for i, name in enumerate(tables)})
    return params


def in_list(tables: Sequence[str], column: str) -> str:
    return f"{column} IN ({', '.join(f':tbl_{i}' for i in range(len(tables)))})"


def slice_names(names: Sequence[str], batch_size: int) -> list[list[str]]:
    """把抽取范围内的表名切成"每批一次 `IN`"的那几刀（工单 020 验收 2）。

    单列成函数是因为那三个边界跟"发几条 SQL"无关：整除时不许有空末批、末批只剩一张也要
    单独成批、`batch_size > N` 退化成一批。混在 `collect` 里就得靠假连接才看得见。

    空名单回**空列表**而不是 `[[]]`：`collect` 靠"没有表就不发 C/D/E"躲开 `IN ()` 这个
    语法错误（见 `in_params`），一个空批会把它推回那条路上。
    """
    if batch_size < 1:
        raise ValueError(f"batch_size 至少是 1，收到 {batch_size}")
    return [list(names[i : i + batch_size]) for i in range(0, len(names), batch_size)]
