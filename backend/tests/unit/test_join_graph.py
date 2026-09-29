"""JOIN 图（architecture §5.3 的图侧，工单 014）。

验收①~⑦ 全部在这里用**手算图**跑纯函数——verification §2.1 本行的口径就是"不依赖真库"。
真库那一半（读 `meta_relation` 的查询）钉在 `tests/integration/test_join_graph_rows.py`。

期望值口径：
- §5.3 的边权重 `manual=0.1`、`extracted=0.5`、`inferred=1/confidence`（confidence 只有
  0.85/1.00 两档，所以 inferred 的权重只会是 1.18 或 1.00）
- 开工前拍板 3：`hops` 数的是**桥表张数**，跳数定上限、权重只在上限内定胜负、权重再并列才算歧义
- 开工前拍板 4：图**存方向、按无向扩展**（真实 FK 恒为子→父，按有向则链走不通）
"""

from __future__ import annotations

import itertools

import networkx as nx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.nl2sql import join_graph as jg
from app.services.nl2sql.join_graph import Expansion, RelationEdge, build_graph, expand

# 门槛 8 出自 architecture §5.3 原文（roadmap 分组 9 的 MAX_JOIN_DEGREE 是它的落点）。
# 测试从 `Settings` 引默认值等于断"it 等于 it"，所以这里写字面量。
_MAX_DEGREE = 8


def _edge(
    from_uid: str,
    from_column: str,
    to_uid: str,
    to_column: str,
    *,
    kind: str = "extracted",
    confidence: float | None = None,
    from_name: str | None = None,
    to_name: str | None = None,
) -> RelationEdge:
    return RelationEdge(
        from_uid=from_uid,
        from_column=from_column,
        to_uid=to_uid,
        to_column=to_column,
        source_kind=kind,
        confidence=confidence,
        from_name=from_name or from_uid,
        to_name=to_name or to_uid,
    )


def _expand(edges: list[RelationEdge], uids: list[str], *, hops: int = 2) -> Expansion:
    """建图 + 扩展一次跑完，门槛统一走 `_MAX_DEGREE`。

    两个函数分开调是这个模块的真实接缝（`build_graph` 出的图会被缓存复用），但 ①~⑦ 每条验收
    关心的都是"给定这批边"，把建图摊到每条用例里只多出 14 处没人读的 `max_degree=`。
    """
    return expand(build_graph(edges), uids, hops=hops, max_degree=_MAX_DEGREE)


def _path_uids(result: Expansion, pair: tuple[str, str]) -> list[tuple[str, ...]]:
    """从 expand 的输出里取某一端点对的路径，按 uid 序列读出来好比对。"""
    return [p.uids for p in result.paths if {p.uids[0], p.uids[-1]} == set(pair)]


def _hub_with_other_tables(count: int) -> list[RelationEdge]:
    """造一张 `H`：它连 `count` 个**别在路径上碍事**的对端（`S0…`，彼此不相连）。"""
    return [_edge("H", "fk_id", f"S{i}", "id") for i in range(count)]


def _degree(graph: nx.MultiDiGraph, uid: str) -> int:
    """无向度数，与 `_bridge_banned` 同口径（出边 + 入边的**不同对端**）。

    先钉住"这张用例造的点确实是那个度"，否则门槛写成 `>=` 还是 `>` 用例区分不出来。
    `graph.neighbors()` 在有向图上只给后继，所以这里必须两边都取。
    """
    return len(set(graph.successors(uid)) | set(graph.predecessors(uid)))


def test_验收一_命名约定推出的边带_1_00_进图() -> None:
    """§5.3 写侧公式算出的 1.00 边（演示库的 `user_activity_log.product_id → product.id`）必须可用。

    门槛 ≥0.8 筛的是这份"本次可用于跨表的边清单"，不是"prompt 里不许出现这条边"——
    低置信那条照样在卡片【可关联】行里（010 拍板收窄的辖区），所以这里丢掉它不丢信息。
    """
    graph = build_graph(
        [
            _edge("u_activity", "product_id", "u_product", "id", kind="inferred", confidence=1.00),
            _edge("u_log", "user_id", "u_customer", "id", kind="inferred", confidence=0.7),
        ]
    )
    result = expand(graph, ["u_activity", "u_product"], hops=2, max_degree=_MAX_DEGREE)
    assert _path_uids(result, ("u_activity", "u_product")) == [("u_activity", "u_product")]
    assert all("u_customer" not in p.uids for p in result.paths)


def test_验收二_自环不进图() -> None:
    """`category.parent_id → category.id` 是真层级结构，但它不是"两张表之间"的可执行路径。

    钉的是两张事实，缺一张就只测到一半：① 图里压根没有这条边（不是"有边但不给路径"），
    ② 扩展对这张表什么也不报。只断 ② 的话，把筛边挪到 `expand` 里照样绿，而那条自环边
    会连带污染 `joinable_edges` 的【可 JOIN】行和 `_bridge_banned` 的度数。

    自环保留在哪：卡片的【可关联】行（008 从 `meta_relation` 渲染，不经本模块），
    那里才是"这张表自引用"该被看见的地方。
    """
    graph = build_graph([_edge("u_category", "parent_id", "u_category", "id")])
    assert graph.number_of_edges() == 0, "自环必须在建图时就被筛掉"
    result = expand(graph, ["u_category"], hops=2, max_degree=_MAX_DEGREE)
    assert result.paths == ()
    assert result.bridge_uids == ()
    # 连不上档也不报：候选只有一张表，没有"表对"可言（两两配对从略，此处 present 长度 1）。
    assert result.needs_cartesian == ()


def test_真外键边不受门槛管() -> None:
    """`extracted`/`manual` 全收（010 的口径）：外键是源库自己声明的，没有置信度可言。"""
    graph = build_graph([_edge("u_item", "order_id", "u_order", "id", kind="extracted")])
    assert expand(graph, ["u_item", "u_order"], hops=0, max_degree=_MAX_DEGREE).paths != ()


def _chain(*uids: str) -> list[RelationEdge]:
    """把一串表名连成 `t1.fk_id → t2.id` 的链（每段都是真外键，权重一律 0.5）。"""
    return [_edge(a, "fk_id", b, "id") for a, b in itertools.pairwise(uids)]


def test_验收五_桥表被拉进来且路径顺序可直接渲染() -> None:
    """§5.3 的招牌能力：只召回 A 和 D，也能经 B、C 把两表连起来。

    `path=[A,B,C,D]` 的顺序**就是** JOIN 的书写顺序，所以它必须逐表对应，不能是集合。
    """
    result = _expand(_chain("A", "B", "C", "D"), ["A", "D"])
    assert [p.uids for p in result.paths] == [("A", "B", "C", "D")]
    assert result.bridge_uids == ("B", "C")
    assert result.needs_cartesian == ()


def test_反向那一跳带回来的仍是外键侧的列() -> None:
    """演示库的真形状：`payment_record → order_main ← order_item → product`。

    中间那一跳只能**逆着存的方向**走（`order_main` 是被 `order_item` 指向的那一端），而逆着走时
    最容易犯的错是让列名跟着 uid 一起换边——那会渲染出 `order_main.order_id = order_item.id`，
    两张表都没有那一列，SQL 当场语法错。拍板 4 说"方向保留的唯一用途是渲染 ON 子句"，
    钉的就是这一条。

    期望值直接来自 `docs/metadata-model.md` §4 的存法：`from_*` 恒是外键侧。
    """
    result = _expand(
        [
            _edge("payment_record", "order_id", "order_main", "id"),
            _edge("order_item", "order_id", "order_main", "id"),
            _edge("order_item", "product_id", "product", "id"),
        ],
        ["payment_record", "product"],
    )

    path = result.paths[0]
    assert path.uids == ("payment_record", "order_main", "order_item", "product")
    assert [(s.from_uid, s.from_column, s.to_uid, s.to_column) for s in path.steps] == [
        ("payment_record", "order_id", "order_main", "id"),
        ("order_item", "order_id", "order_main", "id"),
        ("order_item", "product_id", "product", "id"),
    ]


def test_验收三_hops_数的是桥表张数不是边数() -> None:
    """A–B–C–D–E 这条链有 3 张桥表：hops=2 走不到，hops=3 才走到。

    这是拍板 3 的直接后果。按"边数"解释 hops=2 的话，本用例两种解释给的是同一个答案
    （2 条边 = 1 张桥表），钉不住口径；3 张桥表这一档两种解释会分叉（边数解释永远走不到 D 之后）。
    """
    graph = build_graph(_chain("A", "B", "C", "D", "E"))
    assert expand(graph, ["A", "E"], hops=2, max_degree=_MAX_DEGREE).paths == ()
    assert expand(graph, ["A", "E"], hops=2, max_degree=_MAX_DEGREE).needs_cartesian == (
        ("A", "E"),
    )
    assert expand(graph, ["A", "E"], hops=3, max_degree=_MAX_DEGREE).paths[0].uids == (
        "A",
        "B",
        "C",
        "D",
        "E",
    )


def test_验收四_环不死循环且路径不重复() -> None:
    """A–B–C–A 是三角形，X 挂在 A 上：从 X 走到 C 有两条路，短的那条赢且只出现一次。

    没访问集的话这条用例会**挂死**而不是失败——所以它同时是超时保护。
    """
    result = _expand([*_chain("A", "B", "C"), _edge("C", "fk_id", "A", "id")], ["X", "C"])
    assert result.paths == ()
    result = _expand(
        [*_chain("A", "B", "C"), _edge("C", "fk_id", "A", "id"), _edge("X", "fk_id", "A", "id")],
        ["X", "C"],
    )
    assert [p.uids for p in result.paths] == [("X", "A", "C")]
    assert result.bridge_uids == ("A",)


def test_验收六_连不上时说明白而不是硬连一条边() -> None:
    result = _expand([*_chain("A", "B"), *_chain("C", "D")], ["A", "C"])
    assert result.paths == ()
    assert result.needs_cartesian == (("A", "C"),)


def test_验收七_高表度的表不能当桥但可以当端点() -> None:
    """`H` 的无向度是 9（门槛 8），拍板 7 只禁它**当桥**：

    A、B 各自唯一能连到对方的路就是经 H，所以这一对要标"需笛卡尔积"；
    而 A 与 H 自己那一跳是直连，H 是端点，照旧给路径——否则就是惩罚点名问 H 的人。
    """
    spokes = [
        *_hub_with_other_tables(7),
        _edge("A", "fk_id", "H", "id"),
        _edge("B", "fk_id", "H", "id"),
    ]
    graph = build_graph(spokes)
    assert _degree(graph, "H") == 9, "先钉住度就是 9，否则门槛写成 >= 也照样绿"

    via_hub = expand(graph, ["A", "B"], hops=2, max_degree=_MAX_DEGREE)
    assert via_hub.paths == ()
    assert via_hub.needs_cartesian == (("A", "B"),)

    as_endpoint = expand(graph, ["A", "H"], hops=2, max_degree=_MAX_DEGREE)
    assert [p.uids for p in as_endpoint.paths] == [("A", "H")]


def test_度等于门槛不抑制_门槛是严格大于() -> None:
    """§5.3 原文是"度 > 8"，所以度 8 的表还能当桥。

    这一条与上一条配成一对：只有上一条在的话，门槛写成 `>=` 也照样绿（上一条造的点是度 9）。
    `H` 这里连 S0…S5 + A + B = 8 个对端。
    """
    spokes = [
        *_hub_with_other_tables(6),
        _edge("A", "fk_id", "H", "id"),
        _edge("B", "fk_id", "H", "id"),
    ]
    graph = build_graph(spokes)
    assert _degree(graph, "H") == 8
    result = expand(graph, ["A", "B"], hops=2, max_degree=_MAX_DEGREE)
    assert [p.uids for p in result.paths] == [("A", "H", "B")]


def test_同跳数内_manual_压过_extracted() -> None:
    """§5.3 的权重唯一的实际用途：0.1 < 0.5，人工确认过的边优先于源库抽出来的边。

    两条路跳数相同（都经 1 张桥表），只有权重能分胜负。
    """
    result = _expand(
        [
            _edge("A", "fk_id", "X", "id", kind="extracted"),
            _edge("X", "fk_id", "B", "id", kind="extracted"),
            _edge("A", "org_id", "Y", "id", kind="manual"),
            _edge("Y", "org_id", "B", "id", kind="manual"),
        ],
        ["A", "B"],
    )
    assert [p.uids for p in result.paths] == [("A", "Y", "B")]


def test_同一列对上_manual_压过_extracted() -> None:
    """`uq_meta_relation_key` 含 `source_kind`，所以同一条列对能同时有 extracted 与 manual 两边。

    这条钉的是**边的身份**而不是路径形状：`build_graph` 传给 networkx 的 `key` 少了 source_kind
    的话，后加的那条会原地覆盖前一条，赢家变成"插入顺序的最后一条"而不是权重最小的一条
    （双轴审查 014 抓出的严重项）。所以这里断两处：图里真有两条平行边，且走的是 manual 那条。
    """
    edges = [
        _edge("A", "order_id", "B", "id", kind="extracted"),
        _edge("A", "order_id", "B", "id", kind="manual"),
        _edge("B", "x_id", "C", "id"),
    ]
    graph = build_graph(edges)
    assert graph.number_of_edges("A", "B") == 2, "平行边被覆盖掉了"

    result = expand(graph, ["A", "C"], hops=2, max_degree=_MAX_DEGREE)
    assert [(s.from_uid, s.to_uid, s.source_kind) for s in result.paths[0].steps] == [
        ("A", "B", "manual"),
        ("B", "C", "extracted"),
    ]


def test_跳数并列且权重也并列时才标歧义() -> None:
    """两条一样长、一样权重的路 = 替用户选一条是他没做过的决定 → 不编路径，标 `ambiguous`。

    权重能分胜负的时候不算歧义（上面两条用例钉的正是"分得出就不标"）。
    """
    result = _expand(
        [
            _edge("A", "fk_id", "X", "id"),
            _edge("X", "fk_id", "B", "id"),
            _edge("A", "ref_id", "Y", "id"),
            _edge("Y", "ref_id", "B", "id"),
        ],
        ["A", "B"],
    )
    assert result.paths == ()
    assert result.ambiguous == (("A", "B"),)


def test_结构提示按起点筛不按终点筛() -> None:
    """010 那条口径原样搬到图这一侧（拍板 8）：候选表**自己声明**的边全列，对端没被召回也列。

    反向筛（对端不在候选就不给这一条）会让"这张表能连谁"在 prompt 里凭空少一半，
    而那句标题本来就写着"目标表没出现在【候选表】里时只是结构提示，不要写进 SQL"。
    """
    graph = build_graph(
        [
            _edge("uid_a", "fk_id", "uid_x", "id"),
            _edge("uid_b", "ref_id", "uid_x", "id"),
            _edge("uid_y", "fk_id", "uid_a", "id"),  # 起点不是候选：这一条不给
        ]
    )
    steps = jg.joinable_edges(graph, ["uid_a", "uid_b"])
    assert [(s.from_uid, s.from_column, s.to_uid) for s in steps] == [
        ("uid_a", "fk_id", "uid_x"),
        ("uid_b", "ref_id", "uid_x"),
    ]


def test_结构提示不受表度门槛管() -> None:
    """度 9 的表作为**起点**时，它自己声明的边照旧进【可 JOIN】。

    门槛只有一个住处（`_bridge_banned`，扩展期），这里钉的是"它没有偷偷管到结构提示头上"——
    真把它也管了，用户点名问一张泛列宽表时连结构提示都看不到，而那张表仍然可以被 SELECT。
    """
    graph = build_graph([*_hub_with_other_tables(8), _edge("A", "fk_id", "H", "id")])
    assert _degree(graph, "H") == 9
    assert [(s.from_uid, s.to_uid) for s in jg.joinable_edges(graph, ["H"])] == [
        ("H", f"S{i}") for i in range(8)
    ]


def test_图带表全名_渲染路径时不该再回库查名() -> None:
    """`Expansion.names` 是给 prompt 渲染用的：**未被召回的桥表也有名字**，而它没有卡片。

    010 那一路是把目标表全名 JOIN 出来放在边上的（`load_schema_tables`）；图接管【可 JOIN】之后
    同一件事只在建图时做一次，否则渲染一条路径要回库查三次名。
    """
    edges = [
        _edge(
            "uid_a",
            "fk_id",
            "uid_b",
            "id",
            from_name="ai_web_demo.t_a",
            to_name="ai_web_demo.t_b",
        ),
        _edge(
            "uid_b",
            "ref_id",
            "uid_c",
            "id",
            from_name="ai_web_demo.t_b",
            to_name="ai_web_demo.t_c",
        ),
    ]
    result = _expand(edges, ["uid_a", "uid_c"])
    assert result.names == {
        "uid_a": "ai_web_demo.t_a",
        "uid_b": "ai_web_demo.t_b",
        "uid_c": "ai_web_demo.t_c",
    }
    assert [result.names[u] for u in result.paths[0].uids] == [
        "ai_web_demo.t_a",
        "ai_web_demo.t_b",
        "ai_web_demo.t_c",
    ]


# ---- 缓存与失效（拍板 2：键 (datasource_id, database_id)，同步终局后按库清）----
def _stub_loader(
    monkeypatch: pytest.MonkeyPatch,
    built: list[int],
    edges_by_db: dict[int, list[RelationEdge]],
) -> None:
    """把 `load_relations` 换成"按库返回一批边"的桩，只留缓存与建图这一段是真的。

    桩的是这一层唯一的 IO 出口，不是缓存本身——`graph_for` 的查/存/清三步都走真实代码。
    `session` 传 None 也是同一个理由：真实实现里它只往 `load_relations` 透传，而那里是桩。
    """

    async def fake(
        session: AsyncSession, *, datasource_id: int, database_id: int
    ) -> list[RelationEdge]:
        built.append(database_id)
        return edges_by_db[database_id]

    monkeypatch.setattr(jg, "load_relations", fake)


def _cache_clearing(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例前后都清一次进程级缓存。

    与 `tests/integration/test_join_graph_rows.py` 共用同一个办法（那里是 autouse 夹具），
    所以这里也走 monkeypatch 而不是手写 `jg._CACHE.clear()`：模块是进程级单例，手工清一次会
    被后来者的失败用例跳过清理那一步。
    """
    monkeypatch.setattr(jg, "_CACHE", {})


async def test_缓存按库分键_同源不同库不共用(monkeypatch: pytest.MonkeyPatch) -> None:
    """一个数据源可以抽多个库，而推断与建图都不跨库。

    共用一个键会让第二个库拿到第一个库的边——那种错法不报错，只表现为"问对了表却连不上"。
    """
    _cache_clearing(monkeypatch)
    built: list[int] = []
    _stub_loader(monkeypatch, built, {10: _chain("A", "B"), 20: _chain("C", "D")})

    first = await jg.graph_for(None, datasource_id=1, database_id=10)
    again = await jg.graph_for(None, datasource_id=1, database_id=10)
    other = await jg.graph_for(None, datasource_id=1, database_id=20)

    assert built == [10, 20], "同键第二次必须命中缓存"
    assert again is first
    assert set(first.nodes) == {"A", "B"}
    assert set(other.nodes) == {"C", "D"}, "10 号库的边漏进了 20 号库的图"


async def test_失效只清那一个库(monkeypatch: pytest.MonkeyPatch) -> None:
    """同步是按库提交完的，失效也按库清；把同源的别的库一起丢掉是白丢一次建图。"""
    _cache_clearing(monkeypatch)
    built: list[int] = []
    _stub_loader(monkeypatch, built, {10: _chain("A", "B"), 20: _chain("C", "D")})

    for db in (10, 20):
        await jg.graph_for(None, datasource_id=1, database_id=db)
    assert built == [10, 20]

    jg.invalidate(1, 10)
    await jg.graph_for(None, datasource_id=1, database_id=10)
    await jg.graph_for(None, datasource_id=1, database_id=20)
    assert built == [10, 20, 10], "只有 10 号库的图该被重建"


async def test_缓存有条数上限_不会只涨不落(monkeypatch: pytest.MonkeyPatch) -> None:
    """长驻进程里源库越接越多，图必须按 FIFO 淘汰，而不是攒成只涨不落的内存。

    上限存在的全部理由就是"没人会为此报错"，所以它必须有用例；造 4 个库、上限设 3，
    最旧那个键应当已经不在。上限值本身不是契约（它只是个兜底），所以测试改它不改生产默认。
    """
    _cache_clearing(monkeypatch)
    monkeypatch.setattr(jg, "_CACHE_MAX_ENTRIES", 3)
    built: list[int] = []
    _stub_loader(monkeypatch, built, {db: _chain("A", "B") for db in (1, 2, 3, 4)})

    for db in (1, 2, 3, 4):
        await jg.graph_for(None, datasource_id=1, database_id=db)

    assert list(jg._CACHE) == [(1, 2), (1, 3), (1, 4)], "该按插入顺序淘汰最旧的那个"
    assert built == [1, 2, 3, 4]


async def test_缓存命中时不再查库_但边变了必须靠失效(monkeypatch: pytest.MonkeyPatch) -> None:
    """把"缓存不感知库的变化"这件事钉成一句事实：不清缓存就永远看见旧图。

    真库那一半（`invalidate` 由同步终局调）钉在 `tests/integration/test_sync_cache_pg.py`；
    这里只钉缓存自身的语义，好让那条 pg 用例能区分"没清"和"清了但清错了键"。
    """
    _cache_clearing(monkeypatch)
    graph: nx.MultiDiGraph = nx.MultiDiGraph()
    graph.add_node("A", name="A")
    monkeypatch.setattr(jg, "_CACHE", {(1, 10): graph})
    built: list[int] = []
    _stub_loader(monkeypatch, built, {10: _chain("A", "B")})

    stale = await jg.graph_for(None, datasource_id=1, database_id=10)
    assert stale is graph and built == [], "命中缓存时不许查库"

    jg.invalidate(1, 10)
    fresh = await jg.graph_for(None, datasource_id=1, database_id=10)
    assert set(fresh.nodes) == {"A", "B"} and built == [10]
