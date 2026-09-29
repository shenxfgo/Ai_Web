"""JOIN 图：把 `meta_relation` 的边连成可渲染进 prompt 的跨表路径（architecture §5.3，工单 014）。

roadmap 第 165 行钉的形状：`networkx` 只用于**建图与缓存**，BFS/最短路自己实现以便单测。
本模块因此分两截：上半是纯函数（①~⑦ 全在手算图上跑），下半是读 `meta_relation` 的那三条查询。

三条口径来自开工前拍板，读代码前要先知道：

- **存方向、按无向扩展**：真实 FK 恒为子→父，`payment_record → order_main ← order_item → product`
  这条链必须沿一条反向边走；按有向可达的话，除了"子表→父表"这一跳以外一律不可达。
  方向保留的唯一用途是渲染 `ON` 子句时知道哪列等哪列。
- **跳数定上限、权重定胜负**：`hops` 数的是**桥表张数**（不是边数）；同一跳数内才用 §5.3 的
  边权重分高下；跳数并列且权重也并列，才是 §5.3 那句"多路径歧义"。
- **门槛只有一处**：一条边"能不能进图"全由 `_admissible` 判（自环、inferred < 0.8 都在那里筛掉）。
  图建出来之后的东西（路径、结构提示、表度门槛）都不再判一遍 confidence——门槛散在两处时
  迟早分叉，010 的双轴审查就是在收它的辖区（只管【可 JOIN】、卡片【可关联】那一半不管）。

自环（`category.parent_id → category.id`）**根本不进图**，而不是进图后被特殊对待：它是真实结构，
所以照常渲染进卡片【可关联】（008 的 `full_text` 走的是卡片，不经本模块）；但它给不出跨表路径，
留在图里只会在遍历开头多出一个"自己到自己"的分支要解释。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

import networkx as nx
from sqlalchemy import Select, Table, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.meta import MetaRelation, MetaTable

# §5.3 的边权重。inferred 一档不在这里，它是 1/confidence（confidence 只有 0.85/1.00 两值，
# 所以权重只会是 1.18 或 1.00）——换句话说权重真正的用处是让 manual 压过 extracted。
_KIND_WEIGHT = {"manual": 0.1, "extracted": 0.5}
# 010 拍板收窄后的辖区：这道门槛只管"本次可用于跨表的边清单"，不管 prompt 里能不能出现这条边
# （低置信那条照样在卡片【可关联】行里，那是 008 渲染器的结构描述）。
_MIN_INFERRED_CONFIDENCE = 0.8


@dataclass(slots=True, frozen=True)
class RelationEdge:
    """一条边，方向按 §5.3 的写法：`from_column` 这一侧是外键侧，`to_column` 是主键/唯一侧。

    两个 `*_name` 是**表全名**（`schema.table`，MySQL 侧 catalog 恒空所以不打头），
    建图时挂在节点上。渲染一条路径要知道每一跳的表名，而桥表**没有卡片**（它没被召回，
    卡片是 pipeline 后面补的事），所以名字必须随边一起进来，而不是渲染时回库查。
    """

    from_uid: str
    from_column: str
    to_uid: str
    to_column: str
    from_name: str
    to_name: str
    source_kind: str
    confidence: float | None = None

    @property
    def weight(self) -> float:
        if self.source_kind == "inferred":
            # 走到这里时 confidence 恒不为 None：`_admissible` 把 inferred 且缺分/低于门槛的边挡在了
            # 图外。兜底值只对直接 new 边对象的单测有意义，生产路径不经过它。
            return 1.0 / (self.confidence or _MIN_INFERRED_CONFIDENCE)
        return _KIND_WEIGHT.get(self.source_kind, 1.0)


@dataclass(slots=True, frozen=True)
class JoinStep:
    """渲染 `ON` 子句要的四个字段，方向**按边上存的样子**（`from_*` 恒是外键侧）。

    路径的先后顺序在 `JoinPath.uids` 里，这里不重复表达一遍——两处各说一次方向，
    就会出现"遍历方向"和"存储方向"不一致时该信谁的歧义。
    """

    from_uid: str
    from_column: str
    to_uid: str
    to_column: str
    source_kind: str
    confidence: float | None


@dataclass(slots=True, frozen=True)
class JoinPath:
    uids: tuple[str, ...]
    steps: tuple[JoinStep, ...]
    weight: float

    @property
    def bridge_uids(self) -> tuple[str, ...]:
        return self.uids[1:-1]


@dataclass(slots=True, frozen=True)
class Expansion:
    """一次两两扩展的结果。只出事实，不出文案——措辞归 `pipeline`（§5.3 的 as-built）。

    `names` 是这批候选涉及到的每张表的全名（渲染要用的第三个事实）。它单独一份而不是挂在
    `JoinPath` 里，是因为**未被召回的对端也要有名字**——那种表不会出现在任何路径上，
    却是【可 JOIN】的结构提示行的一部分。
    """

    paths: tuple[JoinPath, ...]
    ambiguous: tuple[tuple[str, str], ...]
    needs_cartesian: tuple[tuple[str, str], ...]
    bridge_uids: tuple[str, ...]
    names: Mapping[str, str]


def _admissible(edge: RelationEdge) -> bool:
    """全模块唯一的门槛：自环不进图，低置信推断边不进图。

    缺 confidence 的 inferred 按 0.0 处理（挡住）而不是放行：那一档的语义是"打分丢了"，
    而"宁缺毋滥"（roadmap 验收 6）在这种时候指的就是宁可少一条边。
    """
    if edge.from_uid == edge.to_uid:
        return False  # 自环不是"两张表之间"的可执行路径（验收②）
    return not (
        edge.source_kind == "inferred"
        and (edge.confidence if edge.confidence is not None else 0.0) < _MIN_INFERRED_CONFIDENCE
    )


def build_graph(edges: Iterable[RelationEdge]) -> nx.MultiDiGraph:
    """门槛筛边（`_admissible`）+ 连成图：节点上挂表全名，边上挂渲染与权重要用的字段。

    表度门槛**不在这里**算，它住在 `expand`（见 `_bridge_banned`）：度是"这张表在图里连了多少个
    对端"，而图本身会按 `(datasource_id, database_id)` 长期缓存，把某个调用方的阈值烧进缓存里的
    节点属性，等于让第一个建图者的阈值对之后所有人永久生效。
    """
    graph = nx.MultiDiGraph()
    for edge in edges:
        if not _admissible(edge):
            continue
        graph.add_node(edge.from_uid, name=edge.from_name)
        graph.add_node(edge.to_uid, name=edge.to_name)
        graph.add_edge(
            edge.from_uid,
            edge.to_uid,
            from_uid=edge.from_uid,
            from_column=edge.from_column,
            to_uid=edge.to_uid,
            to_column=edge.to_column,
            source_kind=edge.source_kind,
            confidence=edge.confidence,
            weight=edge.weight,
            # key 必须含 source_kind：`uq_meta_relation_key`（models/meta.py）就是
            # (datasource_id, source_kind, from_table_id, from_column_name, to_table_id,
            # to_column_name)，所以同一对列上的 extracted 边与 manual 边**能同时落库**。
            # 少了这一档，后加入的那条会原地覆盖前一条的属性，"manual 压过 extracted"就变成
            # "谁最后插入谁赢"。
            key=(edge.source_kind, edge.from_column, edge.to_column),
        )
    return graph


def _bridge_banned(graph: nx.MultiDiGraph, *, max_degree: int) -> set[str]:
    """无向度数 > 门槛的表：不能当桥，但**仍可以是端点**（拍板 7）。

    只禁当桥不禁当端点的理由：组合爆炸发生在扩展，不发生在直连；把用户点名要问的那张表整个
    禁掉，等于让抑制反过来惩罚提问的人。

    度数按**不同对端表**计（`set` 去重，所以复合键的多条边、同一对表的双向边都只算一个对端）。
    图里没有自环——`_admissible` 在建图时就把它筛掉了。
    """
    neighbors: dict[str, set[str]] = {}
    for a, b in graph.edges():
        neighbors.setdefault(a, set()).add(b)
        neighbors.setdefault(b, set()).add(a)
    return {uid for uid, pairs in neighbors.items() if len(pairs) > max_degree}


def _step(data: dict) -> JoinStep:
    """边上存的四个字段原样带回来，**不随遍历方向翻转**。

    路径的顺序在 `JoinPath.uids` 里，`steps` 的职责只有一个：给出可执行的等值条件。
    翻转 uid 而不翻转列名会拼出 `order_main.order_id = order_item.id` 这种两边都没这列的条件；
    一起翻转则等于没有方向概念。两者都比"照存的样子渲染"更糟。
    """
    return JoinStep(
        from_uid=data["from_uid"],
        from_column=data["from_column"],
        to_uid=data["to_uid"],
        to_column=data["to_column"],
        source_kind=data["source_kind"],
        confidence=data["confidence"],
    )


def _adjacency(graph: nx.MultiDiGraph) -> dict[str, dict[str, dict]]:
    """无向邻接表，每对节点只留权重最小的那条边。

    同一对表可能有多条边（复合键、FK 与推断边并存、两个方向各建了一次 FK）；
    扩展时它们互相竞争，赢家由 §5.3 的权重决定，`manual` 因此压过 `extracted` 压过 `inferred`。
    """
    adj: dict[str, dict[str, dict]] = {uid: {} for uid in graph}
    for a, b, data in graph.edges(data=True):
        for src, dst in ((a, b), (b, a)):
            current = adj[src].get(dst)
            if current is None or data["weight"] < current["weight"]:
                adj[src][dst] = data
    return adj


def _walk(
    adj: dict[str, dict[str, dict]],
    banned: set[str],
    start: str,
    goal: str,
    max_steps: int,
) -> list[JoinPath]:
    """简单路径全枚举（深度上限 = 桥表数 + 1 条边），环靠 `visited` 天然挡住。

    `banned` 里的表**不能当桥但可以是端点**——`goal` 那一支因此不查它。
    """
    found: list[JoinPath] = []
    path = [start]
    steps: list[JoinStep] = []
    visited = {start}

    def dfs(node: str, weight: float) -> None:
        if len(steps) == max_steps:
            return
        for other, data in sorted(adj.get(node, {}).items()):
            if other in visited or (other != goal and other in banned):
                continue
            step = _step(data)
            steps.append(step)
            path.append(other)
            visited.add(other)
            if other == goal:
                found.append(JoinPath(tuple(path), tuple(steps), weight + data["weight"]))
            else:
                dfs(other, weight + data["weight"])
            visited.discard(other)
            path.pop()
            steps.pop()

    dfs(start, 0.0)
    return found


def expand(
    graph: nx.MultiDiGraph, candidate_uids: Sequence[str], *, hops: int, max_degree: int
) -> Expansion:
    """候选表两两之间求路径：跳数定上限，上限内权重定胜负，权重也并列才算歧义。

    三种结果各自对应 §5.3 的一句话：`paths` 是可执行承诺（进【可 JOIN】）、
    `ambiguous` 要用户澄清（**不**给路径，二选一是替用户做主）、
    `needs_cartesian` 是"连不上就明说"，而不是硬编一条把两表笛卡尔积起来的假边。

    `hops` 数的是**桥表张数**（拍板 3），所以深度上限是 `hops + 1` 条边：0 跳 = 只准直连。
    """
    adj = _adjacency(graph)
    banned = _bridge_banned(graph, max_degree=max_degree)
    present = list(dict.fromkeys(candidate_uids))
    paths: list[JoinPath] = []
    ambiguous: list[tuple[str, str]] = []
    cartesian: list[tuple[str, str]] = []
    for index, start in enumerate(present):
        for goal in present[index + 1 :]:
            candidates = _walk(adj, banned, start, goal, max_steps=hops + 1)
            if not candidates:
                cartesian.append((start, goal))
                continue
            shortest = min(len(p.steps) for p in candidates)
            same_length = [p for p in candidates if len(p.steps) == shortest]
            best_weight = min(p.weight for p in same_length)
            winners = [p for p in same_length if p.weight == best_weight]
            if len(winners) > 1:
                ambiguous.append((start, goal))
                continue
            paths.append(winners[0])
    bridges = sorted({uid for path in paths for uid in path.bridge_uids if uid not in set(present)})
    # 名字给全图而不是只给路径上的：结构提示行要写"这张候选表指向了谁"，而那些"谁"多半没被召回、
    # 也因此不在任何路径上。渲染时再回库查名，就是把图这一层已经带着的信息丢了。
    names = {uid: attrs["name"] for uid, attrs in graph.nodes(data=True)}
    return Expansion(
        paths=tuple(paths),
        ambiguous=tuple(ambiguous),
        needs_cartesian=tuple(cartesian),
        bridge_uids=tuple(bridges),
        names=names,
    )


def joinable_edges(
    graph: nx.MultiDiGraph, candidate_uids: Sequence[str]
) -> tuple[RelationEdge, ...]:
    """010 那份【可 JOIN】直连清单的图侧版本：**按起点筛不按终点筛**。

    口径不许重开（010 拍板）：候选表自己声明的边全部列出，哪怕对端表没被召回——
    那一对端是结构提示，模板标题已写明"边的目标表若没出现在【候选表】里，不要写进 SQL"。

    返回 `RelationEdge` 而不是 `JoinStep`：渲染这一行要有对端的**表名**（它没卡片，名字只能来自
    建图时挂在节点上的那一份），而 `JoinStep` 只有 uid。边上带的档位/confidence/weight 在这条
    路上用不上，但形状完全一致，为四个字段再造一个类只会多出一份要对齐的口径。
    """
    wanted = set(candidate_uids)
    edges: list[RelationEdge] = []
    seen: set[tuple[str, str, str, str]] = set()
    ordered = sorted(
        graph.edges(data=True),
        key=lambda row: (row[0], row[1], row[2]["from_column"], row[2]["to_column"]),
    )
    for a, b, data in ordered:
        if a not in wanted:
            continue
        identity = (a, data["from_column"], b, data["to_column"])
        if identity in seen:
            continue
        seen.add(identity)
        edges.append(
            RelationEdge(
                from_uid=a,
                from_column=data["from_column"],
                to_uid=b,
                to_column=data["to_column"],
                from_name=graph.nodes[a]["name"],
                to_name=graph.nodes[b]["name"],
                source_kind=data["source_kind"],
                confidence=data["confidence"],
            )
        )
    return tuple(edges)


# 缓存条目上限：超了就按插入顺序淘汰最旧的一个（dict 保序，所以 `next(iter(...))` 就是最旧）。
# 有了上限，长驻进程里"源库越接越多 → 图越攒越大"就不会变成只涨不落的内存；上限本身不追求精确。
_CACHE_MAX_ENTRIES = 32

# ---- 缓存（roadmap 第 165 行的"只用于建/缓存图"那一半）----

_CACHE: dict[tuple[int, int], nx.MultiDiGraph] = {}


async def graph_for(
    session: AsyncSession, *, datasource_id: int, database_id: int
) -> nx.MultiDiGraph:
    """取这个库的图，命中缓存就不查库。

    键是 `(datasource_id, database_id)`（拍板 3）：图不跨库，缓存也就不能跨库共用——
    同源两个库共用一个键，第二个库会拿到第一个库的边，表现是"另一套 schema 的表全都连不上"。
    键里**故意没有** `max_degree`：那是扩展期的策略，不烧进缓存（见 `build_graph`）。
    """
    key = (datasource_id, database_id)
    graph = _CACHE.get(key)
    if graph is None:
        edges = await load_relations(session, datasource_id=datasource_id, database_id=database_id)
        graph = build_graph(edges)
        while len(_CACHE) >= _CACHE_MAX_ENTRIES:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = graph
    return graph


def invalidate(datasource_id: int, database_id: int) -> None:
    """同步终局之后清掉那个库的图。

    为什么必须由同步来清：缓存分不清"同步前有的边"和"刚刚被删掉的边"，
    而用户点完"同步"就期待新结构生效——011 对只读判定定过同一条口径（判定不进缓存）。

    只有"按库精确清"这一档，没有"清整个数据源"的档：删源目前走 ORM 级联，既不在这个进程里
    留下可清的东西，也没有调用方需要它；留着一条没人走的分支，就等于让"漏清"看起来像已被覆盖。
    """
    _CACHE.pop((datasource_id, database_id), None)


# ============================================================ 真 IO：读 meta_relation

_META_TABLE = cast("Table", MetaTable.__table__)
_META_RELATION = cast("Table", MetaRelation.__table__)


def _full_name(catalog: object, schema: object, table: object) -> str:
    """MySQL 的 catalog 恒为空串，不能打头（`retriever.load_schema_tables` 内联的同一条规则）。

    写成 `.` 打头的话，模型会把带点开头的名字抄进 SQL，而那条表名在源库里根本不存在。
    """
    return ".".join(str(part) for part in (catalog, schema, table) if part)


def _relations_statement(datasource_id: int, database_id: int) -> Select:
    """一个库的边全集，两端都钉在同一个 `database_id` 上。

    只筛 from 端的话，"from 在本库、to 在别的库"的边会进来：图上多出一条谁也走不到的假边
    （目标表的卡片不在这批候选里，桥表补查也补不到它），而它恰恰是最像能 JOIN 的那种边。
    `is_stale` 的排除与 `kb_service.sync_cards` 同一条理由：源库里已经没有实体了，
    还把它当可执行路径的一环，等于让 AI 照一张不存在的表写 SQL。

    排序是**契约的一部分**不是装饰：`_adjacency` 在同一对表的多条边里取权重最小者，权重并列时
    **先插入的赢**，而插入顺序由这里的 ORDER BY 决定。补上 `source_kind` 是为了让排序成为全序——
    `(from_uid, from_column, to_uid, to_column)` 恰好就是 `uq_meta_relation_key` 去掉 source_kind
    的形状，也就是"同一列对上 manual 与 extracted 并存"那种场合，少了这一档两次问数的胜者可能不同。
    `retriever.rank_candidates` 为排序键写过同一条理由（"`table_uid` 收尾保证并列时不抖"），
    这里同构：并列的胜者必须由**声明的顺序**决定，不能由存储引擎的心情决定。
    """
    target = _META_TABLE.alias("meta_table_join_target")
    return (
        select(
            _META_TABLE.c.table_uid.label("from_uid"),
            _META_TABLE.c.catalog_name.label("from_catalog"),
            _META_TABLE.c.schema_name.label("from_schema"),
            _META_TABLE.c.table_name.label("from_table"),
            _META_RELATION.c.from_column_name,
            target.c.table_uid.label("to_uid"),
            target.c.catalog_name.label("to_catalog"),
            target.c.schema_name.label("to_schema"),
            target.c.table_name.label("to_table"),
            _META_RELATION.c.to_column_name,
            _META_RELATION.c.source_kind,
            _META_RELATION.c.confidence,
        )
        .join(_META_RELATION, _META_RELATION.c.from_table_id == _META_TABLE.c.id)
        .join(target, target.c.id == _META_RELATION.c.to_table_id)
        .where(
            _META_RELATION.c.datasource_id == datasource_id,
            _META_TABLE.c.database_id == database_id,
            target.c.database_id == database_id,
            _META_TABLE.c.is_stale.is_(False),
            target.c.is_stale.is_(False),
        )
        .order_by(
            _META_TABLE.c.table_uid,
            _META_RELATION.c.from_column_name,
            target.c.table_uid,
            _META_RELATION.c.to_column_name,
            _META_RELATION.c.source_kind,
        )
    )


async def load_relations(
    session: AsyncSession, *, datasource_id: int, database_id: int
) -> list[RelationEdge]:
    """一个库的边原料，**不做任何筛选**。

    门槛（inferred ≥0.8、自环）一律留给 `build_graph`：策略只在一处，改门槛不用碰 SQL。

    表全名在这里拼出来（`kb_service._full_name` 的同一规则：MySQL 的 catalog 恒空串，
    不能打头），因为渲染路径时要它，而**桥表没有卡片**——它是图自己长出来的中间节点，
    检索没召回它，pipeline 补查的也只是它的卡片段，不是它的名字。
    """
    rows = (
        (await session.execute(_relations_statement(datasource_id, database_id))).mappings().all()
    )
    return [
        RelationEdge(
            from_uid=str(row["from_uid"]),
            from_column=str(row["from_column_name"]),
            to_uid=str(row["to_uid"]),
            to_column=str(row["to_column_name"]),
            from_name=_full_name(row["from_catalog"], row["from_schema"], row["from_table"]),
            to_name=_full_name(row["to_catalog"], row["to_schema"], row["to_table"]),
            source_kind=str(row["source_kind"]),
            confidence=None if row["confidence"] is None else float(row["confidence"]),
        )
        for row in rows
    ]


async def _uid_groups(
    session: AsyncSession, table_uids: Sequence[str]
) -> dict[tuple[int, int], list[str]]:
    """把候选表按 `(datasource_id, database_id)` 分组，组内保持传入顺序（= 相关度顺序）。

    查不到的 uid 直接跳过而不抬错：与 `load_schema_tables` 同一条口径——一次问数不该因为
    一张表在两步之间被同步 prune 掉就整条链路失败。
    """
    rows = (
        (
            await session.execute(
                select(
                    _META_TABLE.c.table_uid,
                    _META_TABLE.c.datasource_id,
                    _META_TABLE.c.database_id,
                ).where(_META_TABLE.c.table_uid.in_(table_uids))
            )
        )
        .mappings()
        .all()
    )
    where_used = {
        str(row["table_uid"]): (int(row["datasource_id"]), int(row["database_id"])) for row in rows
    }
    groups: dict[tuple[int, int], list[str]] = {}
    for uid in dict.fromkeys(table_uids):
        key = where_used.get(uid)
        if key is not None:
            groups.setdefault(key, []).append(uid)
    return groups


async def structure_hints(
    session: AsyncSession, *, table_uids: Sequence[str]
) -> tuple[RelationEdge, ...]:
    """候选表**自己声明**的边（含对端没被召回的那些），010 那份【可 JOIN】直连清单的图侧版本。

    与 `expansion_for` 分开两个入口而不是并成一个返回值：`Expansion` 是"两两扩展的事实"
    （路径/歧义/连不上），结构提示不是它的一次遍历算出来的东西，硬塞进去会让那五条验收
    读起来多一列没人解释的字段。

    两个入口各自发一次 `_uid_groups` 的 SELECT（这张表在哪个库），不共享——省掉那一次查询的
    代价是把"分组"这件事变成两个入口之间的隐式约定。图本身命中缓存，所以这里通常不再读
    `meta_relation`。**不接 `max_degree`**：高表度只影响"能不能当桥"（`expand` 的事），
    不影响一条直连边能不能作为结构提示出现。
    """
    edges: list[RelationEdge] = []
    seen: set[tuple[str, str, str, str]] = set()
    for (datasource_id, database_id), uids in (await _uid_groups(session, table_uids)).items():
        graph = await graph_for(session, datasource_id=datasource_id, database_id=database_id)
        for edge in joinable_edges(graph, uids):
            identity = (edge.from_uid, edge.from_column, edge.to_uid, edge.to_column)
            if identity not in seen:
                seen.add(identity)
                edges.append(edge)
    return tuple(edges)


async def expansion_for(
    session: AsyncSession, *, table_uids: Sequence[str], hops: int, max_degree: int
) -> Expansion:
    """候选表的跨表事实：逐库建图扩展，跨库/跨源的表对一律按"连不上"报。

    分组住在这一层而不是 pipeline，是因为"不跨库建图"（拍板 3）是图的事实而不是编排的口味：
    一条连接串连不了两个库，所以两个库的候选之间根本没有边可言，`needs_cartesian` 就是它的答案。

    同库内的表对由 `expand` 判"连不上"，跨库/跨源的表对在这里判——两档合流成同一个
    `needs_cartesian`，因为对 pipeline 来说它们是同一句"这两张表之间没有可执行路径"。
    """
    groups = await _uid_groups(session, table_uids)
    paths: list[JoinPath] = []
    ambiguous: list[tuple[str, str]] = []
    cartesian: list[tuple[str, str]] = []
    bridges: set[str] = set()
    names: dict[str, str] = {}

    for (datasource_id, database_id), uids in groups.items():
        graph = await graph_for(session, datasource_id=datasource_id, database_id=database_id)
        one = expand(graph, uids, hops=hops, max_degree=max_degree)
        paths.extend(one.paths)
        ambiguous.extend(one.ambiguous)
        cartesian.extend(one.needs_cartesian)
        bridges.update(one.bridge_uids)
        names.update(one.names)

    keys = list(groups.items())
    for index, (_, left) in enumerate(keys):
        for _, right in keys[index + 1 :]:
            cartesian.extend((a, b) for a in left for b in right)

    return Expansion(
        paths=tuple(paths),
        ambiguous=tuple(ambiguous),
        needs_cartesian=tuple(cartesian),
        bridge_uids=tuple(sorted(bridges)),
        names=names,
    )
