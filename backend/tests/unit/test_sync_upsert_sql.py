"""§3 三段式 upsert 的**语句形状**：冲突键、谁被覆盖、谁被保护。

接缝选在"编译出来的 SQL 文本"上，不选在"执行结果"上——`docs/metadata-model.md` §3 给的是
一段 SQL，把它逐句钉住是唯一能廉价做到的事：真跑出来的行为属于 integration 打元数据库那一片。
而这一片恰恰最需要一个廉价锚点：**写错 `ON CONFLICT` 的目标列或漏掉一个 `COALESCE`，
测试和真库都会同样"绿"**，代价是下一次同步覆盖掉人工补录的中文（§3 的全部存在理由）。

期望值口径：§3 的代码块原文 + §2.4 的自然键定义。
"""

from __future__ import annotations

import re

from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import visitors
from sqlalchemy.sql.elements import BindParameter

from app.services import sync_service


def _sql(stmt: object, *, literal_binds: bool = True) -> str:
    """编译成 PG 方言文本，压平排版，并去掉 schema 限定。

    conftest 把元数据库 schema 换成了每会话随机的 `aiweb_test_<8hex>`（集成用例要并发跑），
    所以钉死 `aiweb.` 前缀的用例会在换 schema 时假失败。schema 落点本身由
    `test_meta_models_ddl.py` 管，这里只管"哪一列被赋成什么表达式"。
    """
    kwargs: dict[str, object] = {}
    if literal_binds:
        kwargs["compile_kwargs"] = {"literal_binds": True}
    rendered = str(stmt.compile(dialect=postgresql.dialect(), **kwargs))  # type: ignore[attr-defined]
    rendered = re.sub(r"\b\w+\.(meta_\w+)", r"\1", rendered)
    return re.sub(r"\s+", " ", rendered).strip()


def _set_clause(stmt: object) -> dict[str, str]:
    """把 `DO UPDATE SET` 后面的赋值拆成 {列名: 表达式文本}。

    比正则匹配整句强：要断言的是"某一列被赋成什么"，而 COALESCE 里带逗号，
    所以按括号深度切分，不按逗号切分。
    """
    tail = _sql(stmt).split("DO UPDATE SET ", 1)[1]
    parts: list[str] = []
    depth, current = 0, []
    for ch in tail:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    out: dict[str, str] = {}
    for part in parts:
        name, _, expr = part.partition("=")
        out[name.strip().lower()] = " ".join(expr.split())
    return out


# 一条 `extracted` 关系的行形状（§2.4 的六列自然键 + §8.1 E 能拿到的四个属性）。
# 关系这条要带样本行才能编译：它是"insert + delete 合成一条语句"，
# VALUES 必须由行决定，做不到像前几条那样留一份无值模板。
_RELATION_ROW: dict[str, object] = {
    "datasource_id": 1,
    "source_kind": "extracted",
    "from_table_id": 10,
    "from_column_name": "order_id",
    "to_table_id": 20,
    "to_column_name": "id",
    "fk_name": "fk_order_main",
    "on_delete": "CASCADE",
    "on_update": "RESTRICT",
    "is_authors_enforced": True,
}


def test_meta_table_按四元组冲突() -> None:
    """§3 第 1 段 + §1：唯一性只在 `(datasource_id, catalog_name, schema_name, table_name)` 内成立。

    冲突键写成 `(datasource_id, table_name)` 的话，两个库各自的 `orders` 会互相覆盖（§1
    点名的陷阱），而 SQL 本身照样合法——所以这一条必须钉在文本上。
    """
    low = _sql(sync_service.meta_table_upsert()).lower()
    assert "on conflict (datasource_id, catalog_name, schema_name, table_name) do update set" in low


def test_meta_table_同步列吃新值_人工列保旧值() -> None:
    """§3 的 `COALESCE(现有值, 新值)`：库里已有人工值就保留，只有为空时才接受新值。

    这一条是"重复同步不覆盖人工补录"（验收 5）的根。写成 `excluded.comment_zh` 的话，
    第一次同步后用户填的中文注释会在第二次点"同步"时静默消失——测试、真库、UI 全都不报错。
    """
    set_ = _set_clause(sync_service.meta_table_upsert())
    for human in ("comment_zh", "business_desc", "granularity"):
        # 函数名小写：SQLAlchemy 按自己的大小写渲染，文档里的 COALESCE 与之等价
        assert set_[human] == (f"coalesce(meta_table.{human}, excluded.{human})"), human
    # comment_raw 是源库原文，属同步列：它必须跟着源库走，否则源库改了注释这边永远看不见
    for synced in ("comment_raw", "table_type", "approx_rows", "engine"):
        assert set_[synced] == f"excluded.{synced}", synced


def test_meta_table_人工开关原样保留且打本轮时间戳() -> None:
    """§3 的两处例外：`is_hidden` 连新值都不接受，`synced_at` 取库的 `now()` 而不是批参数。

    - `is_hidden` 是"人工排除某张表"的开关（§2.4）。它若参与 `COALESCE`，用户在源库
      改名重建后开关就被清回 false；若取 `excluded.is_hidden`，每轮同步都按默认值覆盖人工。
    - `synced_at` 是 §5 陈旧判定与 §4 delete-diff 的钟，必须由**同一条语句**里的 `now()`
      生成：拿 Python 侧的时间传进来，一批几千行会带几十个不同时间戳，
      "本轮同步开始时刻"这个比较基准就不成立了。
    """
    set_ = _set_clause(sync_service.meta_table_upsert())
    assert set_["is_hidden"] == "meta_table.is_hidden"
    assert set_["synced_at"] == "now()"


def test_meta_column_走自然键_upsert_不走_delete_then_insert() -> None:
    """§3 as-built(0007)：`meta_column` 带 `comment_zh`/`business_desc` 两个人工列，

    所以它必须与主表同构走 upsert。§3 原文写的是"子表无人工字段，delete-then-insert 最简"，
    那等于每点一次"同步"就把字段级的中文补录清空——文档已按这条修正。
    """
    low = _sql(sync_service.meta_column_upsert()).lower()
    assert "on conflict (table_id, column_name) do update set" in low
    set_ = _set_clause(sync_service.meta_column_upsert())
    for human in ("comment_zh", "business_desc"):
        assert set_[human] == f"coalesce(meta_column.{human}, excluded.{human})", human
    # 索引徽标与枚举值是同步算出来的，必须跟着源库走（验收 1 的"68 列注释非空"靠它）
    for synced in ("data_type", "raw_data_type", "comment_raw", "is_primary_key", "enum_values"):
        assert set_[synced] == f"excluded.{synced}", synced
    assert set_["synced_at"] == "now()"


def test_meta_column_清扫只删本轮没碰过的行() -> None:
    """§3 as-built(0007)：upsert 换不来"源库删掉的列消失"，靠 synced_at 比较补这一刀。

    两个致命写法：
    - 基准写成 `now()`：PG 的 `now()` 是事务时间戳，同事务内恒定，
      `synced_at < now()` 会把本轮刚 upsert 进去的列一起删掉。所以它是外传的绑定参数。
    - 不带 `table_id` 范围：就成了跨数据源全表删。
    """
    low = _sql(sync_service.meta_column_sweep(), literal_binds=False).lower()
    assert "delete from meta_column" in low
    assert "table_id in (" in low, "删除范围必须限定在本轮的表上"
    # 比较基准是绑定参数：只要它不是 now()，语义就对了（占位符写法属 paramstyle，不钉）
    assert "synced_at <" in low and "now(" not in low, "比较基准不能是 now()"


def test_meta_relation_删除差分只打_extracted() -> None:
    """§3 第 3 段 + §4："inferred 与 manual 不参与同步删除"。

    少了 `source_kind='extracted'` 这个谓词，用户手工补录的关系会在下一次点"同步"时
    全部消失；写成 `source_kind in ('extracted','inferred')` 也一样错——推断边是
    给人 accept 成 manual 的，删掉等于把待确认清单反复清空。

    形态是 §3 给的一条语句：`with keep as (insert ... returning id) delete ... not in (keep)`。
    拆成两条语句也"能跑"，但 upsert 与 diff 就不在同一语句快照里了——中途崩溃会留下
    一份"全被删掉、新关系没写进去"的关系表。

    库级作用域（`from_table_id in (… database_id in (…))`）是 review 抓出来的 bug 的闸：
    DELETE 原先只按 datasource_id 收口，而 keep 只装本 catalog 的 id——多 schema 时
    后写的库会把先写的库的 extracted 边全删光。
    """
    low = _sql(sync_service.meta_relation_replace([_RELATION_ROW]), literal_binds=False).lower()
    assert "with keep as (insert into meta_relation" in low
    assert "returning meta_relation.id" in low
    assert "delete from meta_relation" in low
    assert "source_kind = 'extracted'" in low, "删除差分的作用域就是这一个谓词"
    assert "not in (select keep.id from keep)" in low
    assert "from_table_id in (select" in low and "database_id in (" in low, (
        "删除范围必须限定在本轮写完的那些库，否则多 schema 同步会后写的库删光先写的库的边"
    )


def test_本轮这个库没有外键时走清扫语句() -> None:
    """§4 delete-diff 的另一半：外键为 0 也得把上一轮的 extracted 边收走。

    空 rows 走不了 replace（PG 的 executemany 空列表整条不执行），所以有专用的 prune；
    两条路缺一条，"源库删光外键"就变成永久残留。作用域与 replace 同一把尺。
    """
    low = _sql(sync_service.meta_relation_prune(), literal_binds=False).lower()
    assert "delete from meta_relation" in low
    assert "source_kind = 'extracted'" in low
    assert "database_id in (" in low
    assert "insert into" not in low, "prune 只收边，不写边"
    assert "keep" not in low, "没有 upsert 就没有 keep 这个 CTE"


def test_关系替换语句要求调用方给_datasource_id() -> None:
    """insert 半边由 rows 喂参数，delete 半边的作用域是**独立 bindparam**——漏传就当场红。

    这不是假想的坑：live 第一次真跑就死在这里（`StatementError: A value is required for
    bind parameter 'datasource_id'`，整轮同步变 `partial`、一条关系都没落）。
    "哪些值是语句自带的"和"哪些要调用方补"在编译文本里长得一模一样，所以能钉住这个事实的
    只有语句对象上的 bindparam 集合——它同时就是调用方的参数契约。
    """
    stmt = sync_service.meta_relation_replace([_RELATION_ROW])
    required = {
        node.key
        for node in visitors.iterate(stmt, {})
        # rows 里每个值都编译成匿名参数（`%(<id> param)s`），它们由 executemany 喂；
        # 契约只管**显式命名**的那几个——匿名参数名会随对象地址漂
        if isinstance(node, BindParameter) and not node.key.startswith("%(")
    }
    assert required == {"datasource_id", "database_ids"}, f"调用方必须补的绑定参数变了：{required}"


def test_meta_index_这一对才真无人工字段_整批替换() -> None:
    """§3 第 2b 段：`meta_index` / `meta_index_column` 走 delete-then-insert。

    判据不是"它是子表"，而是"它没有人工列"：`comment`/`is_visible`/`cardinality`/`sub_part`
    全部来自 `information_schema.STATISTICS`（§8.1 D），每轮都能原样重算，所以全删重插最简。

    子表 `meta_index_column` 这里**不单独发 DELETE**：FK 是 `ON DELETE CASCADE`
    （`test_meta_models_ddl.py` 钉过），删父行就带走子行。多写一条 DELETE 不是错，
    但会让人以为 CASCADE 可以不成立——而 `meta_column` 恰恰不能靠它（它不能删）。
    """
    low = _sql(sync_service.meta_index_purge(), literal_binds=False).lower()
    assert "delete from meta_index" in low
    assert "table_id in (" in low
    assert "meta_index_column" not in low, "子表靠 CASCADE 带走，不在此处重复删"


def test_inferred_关系只_upsert_不带删除差分() -> None:
    """§4：inferred 与 manual 各自走自己的重建逻辑，**不参与同步删除**。

    自然键里带 `source_kind`（§2.4 的六列 UNIQUE），所以同一条边的 extracted 与 inferred
    是两个行，"两者可区分"（验收 4）靠的就是这个键，而不是靠 confidence 是否为空。
    """
    stmt = sync_service.meta_relation_inferred_upsert()
    low = _sql(stmt).lower()
    assert "delete from" not in low, "推断边不参与删除差分"  # 注意别用裸 "delete"：on_delete 里也有
    assert (
        "on conflict (datasource_id, source_kind, from_table_id, from_column_name,"
        " to_table_id, to_column_name) do update set" in low
    )
    # confidence 是这一条的全部产出，必须跟着本轮打分走（不是人工列，不参与 COALESCE）
    assert _set_clause(stmt)["confidence"] == "excluded.confidence"


def test_stale_标记按同步时刻打且限定本轮确认过的库() -> None:
    """§5 + §6 末条：本轮没出现的表打 `is_stale=true`，**不物理删**。

    - `set is_stale = false` 藏在 `meta_table_upsert()` 里（验收 5 的幂等靠它），
      这一条只负责打 true，两条合起来才是"来回同步不抖动"。
    - 比较基准同样是 `:synced_before`：与 §3 的列清扫共用一个时钟，
      两处用不同的钟就会出现"列被删了但表还在"的中间态。
    - 范围是 `database_id IN (:database_ids)`，而调用方只准把 **`known_complete=True`
      （§6：该库确认成功枚举完整）** 的库放进来。账号权限看不到的库不进这个集合，
      于是"看不到"永远不会变成"被标陈旧"——这是 §6 那条"绝不允许因为看不到就删元数据"
      在语句层面的落点。
    """
    low = _sql(sync_service.meta_table_mark_stale(), literal_binds=False).lower()
    assert re.search(r"is_stale\s*=\s*true", low), "只打 true，反向清零在 upsert 里"
    assert "database_id in (" in low
    assert "synced_at <" in low and "now(" not in low


def test_meta_database_按三元组冲突且回吐_id() -> None:
    """§2.4 + §1：`meta_table.database_id` 要指向这一行的 id，所以 upsert 必须 RETURNING。

    MySQL 侧走 `('' , <db>)`、PG 侧走 `(<db>, <schema>)`（§1 的规范化列）——冲突键写成
    `(datasource_id, schema_name)` 的话，PG 的两个库里同名 schema 会互相覆盖。
    """
    stmt = sync_service.meta_database_upsert()
    low = _sql(stmt).lower()
    assert "on conflict (datasource_id, catalog_name, schema_name) do update set" in low
    assert "returning meta_database.id" in low
    set_ = _set_clause(stmt)
    for synced in ("table_count", "approx_rows", "approx_size_bytes", "is_visible"):
        assert set_[synced] == f"excluded.{synced}", synced
