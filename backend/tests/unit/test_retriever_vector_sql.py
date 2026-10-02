"""向量路那条 SQL 的**形状**，与 `ef_search` 的下发形状（工单 031 涉及层第 3、4 格）。

为什么形状也要不连库钉一条（与 029 的 `test_sync_embed_sql.py` 同一条理由）：`pg` 那一路的
行为用例看得见"召回对不对"，看不见"顺手漏了一格谓词"——而向量路漏掉 `index_profile_id`
照样返回结果，只是**新旧两套档案的向量混进同一个 ORDER BY**（ADR-0012 立论要防的正是它）。
形状错了但结果看着正常，正是这一族 bug 唯一能不靠读代码抓住的时刻。

期望值口径：`docs/architecture.md` §5.2 (1) 那段伪 SQL 逐字对照——
`<=>` 距离算子、`ORDER BY <列> <=> <查询向量>`、`LIMIT top_vector`、`deleted_at IS NULL`、
`index_profile_id = active`、`datasource_id` 的权限谓词，以及那句 `SET LOCAL hnsw.ef_search`。
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy.dialects import postgresql

from app.services.kb_service import vector_target_for
from app.services.nl2sql.retriever import hnsw_tuning_statement, vector_recall_statement


def _render(compiled: object) -> str:
    return re.sub(r"\s+", " ", str(compiled)).strip()


def _sql(stmt: object) -> str:
    return _render(stmt.compile(dialect=postgresql.dialect()))


def _inlined(stmt: object) -> str:
    """同一份语句，但把绑定值印回串里——只在"要断言的正是那个值"的用例里用。"""
    return _render(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


def _after(sql: str, marker: str, stop: str | None = None) -> str:
    """取 `marker` 之后（到 `stop` 为止）那一段，比全文子串检查更能指出"在哪一句里"。"""
    head = sql.split(marker, 1)[1]
    return (head if stop is None else head.split(stop, 1)[0]).strip()


@pytest.mark.parametrize(
    ("dimensions", "column"),
    [(1536, "embedding"), (1024, "embedding_1024")],
)
def test_向量路排在档案那一维的列上_另一列一字不现(dimensions: int, column: str) -> None:
    """「维度→列名」的那一次翻译发生在读侧，而且**只**翻这一列。

    反向的那一格（`embedding_1024` 那一发不许连带读 `embedding`）在这里同样是子串陷阱——
    `embedding_1024` 本身含 `embedding`——所以比的是"点名列的集合"而不是子串。
    """
    target = vector_target_for(dimensions=dimensions, model="text-embedding-3-small")
    sql = _sql(
        vector_recall_statement(
            column=target.column, profile_id=7, datasource_ids=(1, 2), top_vector=80
        )
    )
    named = set(re.findall(r"\bembedding(?:_1024)?\b", sql))
    assert named == {column}, f"点名了 {sorted(named)}，期望只有 {column}"
    # 排序与投影都打在**同一列**上：距离算子的左操作数换成另一列，就是拿 A 维的向量排 B 维的名。
    order = _after(sql, "ORDER BY", "LIMIT")
    assert re.fullmatch(rf"[\w.]*kb_card\.{column} <=> CAST\(.+ AS VECTOR\)", order), order


def test_向量路取的是名次次序里的距离与卡片原料_按距离升序() -> None:
    """§5.2 (1)：`ORDER BY <向量列> <=> $1`——余弦**距离**升序就是相似度降序，不再写一次 DESC。

    选出来的 `score` 是 `1 - 距离`（相似度），只给人看；名次由那条 ORDER BY 的次序决定，
    RRF 吃的是名次（ADR-0014），所以这里不许出现 `ORDER BY ... score`。
    （`1 -` 里的那个 1 会渲染成绑定参数，属 SQLA 的写法而非本片事实，所以断言打在
    "减数那一项正是这一列的距离"上。）
    """
    stmt = vector_recall_statement(
        column="embedding", profile_id=7, datasource_ids=(1, 2), top_vector=80
    )
    sql = _sql(stmt)
    order = _after(sql, "ORDER BY", "LIMIT")
    assert "<=>" in order
    assert "score" not in order, f"名次被分数排了：{order}"
    assert "DESC" not in order, f"距离升序就够了，写 DESC 会把次序整个反过来：{order}"
    # 投影里那一格是 `1 - 距离`，写成 score；参数样式（%(x)s / $1）归 SQLAlchemy 不归本片，
    # 所以比的是结构而不是整串。
    assert ".kb_card.embedding <=> CAST(" in sql, sql
    assert ") AS score" in sql, sql


def test_权限与档案两格谓词都在_where_里() -> None:
    """决策 ⑨/ADR-0008 落到向量路的那一句：过滤**进 SQL**，不是"先取全局 top-80 再筛"。

    先筛的失效形状很安静：召回窗口 80 张卡被无权源占满时返回空集，而库里明明有权可查的表。
    """
    sql = _sql(
        vector_recall_statement(
            column="embedding", profile_id=7, datasource_ids=(3, 4, 5), top_vector=80
        )
    )
    where = _after(sql, " WHERE", "ORDER BY")
    assert "deleted_at IS NULL" in where
    assert "index_profile_id =" in where
    assert "datasource_id IN (" in where
    # 三格都得在 WHERE，而不是靠调用方事后筛（§5.2 (5) 那一层只是二次保险）。
    for fragment in ("deleted_at IS NULL", "index_profile_id =", "datasource_id IN ("):
        assert where.count(fragment) == 1, f"{fragment} 出现了不止一次：{where}"


def test_还没灌向量的卡不进向量路() -> None:
    """ADR-0003「卡片先于向量」的读侧一半：NULL 向量排不出距离。

    不写这一格的话 NULL 会跟着 `<=>` 一起出来（PG 把 NULL 排在升序末尾），于是"这张表没向量"
    伪装成"这张表最相关"——一旦 `top_vector` 大于真有向量的卡数，那些卡就占名额进 RRF。
    """
    sql = _sql(
        vector_recall_statement(
            column="embedding", profile_id=7, datasource_ids=(1,), top_vector=80
        )
    )
    where = _after(sql, " WHERE", "ORDER BY")
    assert ".kb_card.embedding IS NOT NULL" in where, where


def test_top_vector_就是那条_limit_的值() -> None:
    """故事 22 的落点：`top_vector` 是向量路**自己**的召回窗口，且真的下发到 SQL。

    写死 `_RECALL_LIMIT`（关键词路那个 80）在这里过不了——两条路的数不共用常量（已定口径）。
    `LIMIT` 那一段本来渲染成绑定参数，所以这一条用 `literal_binds` 把值印回语句里再看数。
    """
    for top_vector in (13, 80, 200):
        compiled = vector_recall_statement(
            column="embedding", profile_id=7, datasource_ids=(1,), top_vector=top_vector
        ).compile(dialect=postgresql.dialect())
        # `LIMIT` 后面是那个具名参数（不是被写死的常数）……
        assert _after(_render(compiled), "LIMIT").startswith("%(top_vector)s"), _render(compiled)
        # ……而下发的值就是调用方给的那一个。
        assert compiled.params["top_vector"] == top_vector


def test_ef_search_以_set_local_下发且是绑定值不是拼接() -> None:
    """§5.2 (1) 括号里那一格 + 踩坑 ② 的写法。

    两个事实各自会被不同错法破坏：
    - 用 `SET`（不带 LOCAL）→ 设置漏到这条连接的下一次请求里，别的用户的查询被这次的 ef 影响；
      SQLAlchemy 的 `text()` 拼 `SET LOCAL x TO :v` 会被 PG 拒（`SET` 不接绑定参数），
      所以这里走 `set_config(name, value, is_local => true)` 那个函数形态。
    - 把数字拼进语句 → 参数来源是请求体（`ef_search` 由调用方给），拼接就是把注入面打开。
    """
    stmt = hnsw_tuning_statement(400)
    sql = _sql(stmt)
    assert "set_config('hnsw.ef_search'" in sql, sql
    assert "hnsw_ef_search" in sql, sql
    # is_local 那一格必须是 true：写成 false 就是全局 SET。
    assert "false" not in sql, sql
    # 值只能是绑定参数——400 出现在编译串里就意味着它被拼进去了。
    assert "400" not in sql, sql
    # 而绑过去的必须是**文本形态的数**：`set_config(text, text, bool)` 的第二个参数不收整数，
    # 传 int 会在真库上抬 `function set_config(unknown, integer, boolean) does not exist`。
    inlined = _inlined(stmt)
    assert "set_config('hnsw.ef_search', '400', true)" in inlined, inlined


def test_iterative_scan_与_ef_同一条语句下发() -> None:
    """§5.2 (1) 括号里那个 `iterative_scan`：HNSW 带过滤时靠它把被滤掉的邻居继续走完。

    与 ef 分两条语句也行，但同一条 = 同一个事务里一次往返，且两条设置不会有一个漏发。
    """
    sql = _sql(hnsw_tuning_statement(64))
    assert "set_config('hnsw.iterative_scan'" in sql, sql
    assert "hnsw_iterative_scan" in sql, sql
