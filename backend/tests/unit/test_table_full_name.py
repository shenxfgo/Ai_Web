"""`meta_table` 三列 → 表全名那个**唯一纯函数**（工单 039 让它出生）。

接缝选在纯函数上，理由与 `test_vector_columns.py` / `test_sync_stage_vocabulary.py` 同一条：
验收 4 要钉的是"没有第二处拼法"，把它写在 `retriever` 或端点里，这条断言就只剩人肉 grep。

本片为什么非要有它：`sql_guard.check(allowed=...)` 那批 `QualifiedTable` 有两个出生地——
ask 那一路由检索出的表名喂（`retriever.load_schema_tables`），validate 这一路由 `meta_table`
直接喂。两边**各写一遍**"catalog 为空串时不打头"，就会有一种最坏的漂移：同一件 SQL 在 ask
被 `table_not_allowed` 拒、在 validate 放行（或反之），而"校验结果和真实问数一样"恰恰是本片
存在的全部理由。

期望值全部**手写**，来自 `docs/metadata-model.md` §1 那两行规范化列的口径
（MySQL 走 `('', <db>)`、PG 走 `(<db>, <schema>)`）与 `sql_guard.QualifiedTable.parse` 的
点分逆运算，不是从实现里读回来的。
"""

from __future__ import annotations

import pathlib

from app.core.table_names import table_full_name
from app.services.sql_guard import QualifiedTable

# 「三列拼成一串」这个习惯用法允许的落点：唯一主人自己。
# 两个 pattern 都要扫：施工前实物是三处拼法，而工单 039 验收 4 那条 grep
# （`join(part for part`）只抓得到其中两处——第三处 `join_graph._full_name` 多了 `str(...)`，
# 换的是另一个 pattern。只按工单字面设门会把"两处留下、一处主人"当成绿。
_ALLOWED_JOIN_FILES = {"app/core/table_names.py"}
_JOIN_IDIOMS = ("join(part for part", "join(str(part) for part")


def test_mysql的catalog恒为空串_不能打头() -> None:
    """`.ai_web_demo.order_main` 这种名字模型会原样抄进 SQL，而它在源库里不存在一张那样的表。"""
    assert table_full_name("", "ai_web_demo", "order_main") == "ai_web_demo.order_main"


def test_三段齐全时catalog打头() -> None:
    assert table_full_name("def", "ai_web_demo", "order_main") == "def.ai_web_demo.order_main"


def test_只有表名时不含点() -> None:
    assert table_full_name("", "", "orders") == "orders"


def test_三列全空串给出空串而不是一个点() -> None:
    """`".".join(())` 是空串——这一格看着无聊，它是"分隔符不能无条件拼"的钉子：
    写成 `f"{catalog}.{schema}.{table}"` 的实现今天在这件输入上会给出 `..`。
    """
    assert table_full_name("", "", "") == ""


def test_拼出来的全名能被守卫的parse原样读回() -> None:
    """两条路的**共同**逆运算：`allowed` 由 `QualifiedTable.parse(full_name)` 构造，
    拼法与切法只要不同源，白名单就是一批守卫永远匹配不上的表名（全部 `table_not_allowed`）。
    """
    assert QualifiedTable.parse(table_full_name("", "ai_web_demo", "order_main")) == QualifiedTable(
        catalog="", db="ai_web_demo", name="order_main"
    )
    assert QualifiedTable.parse(table_full_name("def", "public", "orders")) == QualifiedTable(
        catalog="def", db="public", name="orders"
    )


def test_三列拼法在_app_里只有主人一处() -> None:
    """验收 4 的机械版本：`grep` 这件事交给用例，而不是交给下一次人工检查。

    扫 `app/` 的 `.py`（测试文件里那些是**对结果的断言**，不是拼法，不在射程内）。
    allowlist 之外命中 = 有人又抄了一遍。
    """
    root = pathlib.Path(__file__).resolve().parents[2]
    hits: list[str] = []
    for path in sorted((root / "app").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if any(idiom in text for idiom in _JOIN_IDIOMS):
            hits.append(path.relative_to(root).as_posix())
    assert set(hits) == _ALLOWED_JOIN_FILES, (
        f"第二处拼法：{sorted(set(hits) - _ALLOWED_JOIN_FILES)}"
    )
