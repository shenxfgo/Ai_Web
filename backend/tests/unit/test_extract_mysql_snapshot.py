"""MySQL 5.7 抽取 SQL 的文本快照（不连库，纯字符串断言）。

为什么按 §2.2 第 4 项用"SQL 文本快照"而不是打真库：`verification.md` §2.2 说得很直白——
单测里对抽取 SQL 的验证方式就是对生成的 SQL 文本做快照，
防的是 `IN (…)` 参数占位写错、防 `information_schema` 查询漏了 `table_schema` 条件。
这两个错误在 mock 出来的连接上都不会显形，在真库上又要等到第一次同步才炸。

期望值口径全部来自 `docs/metadata-model.md` §8.1（A–E 五条 SQL 原文）与 §10（两个 5.7 坑）。
"""

from __future__ import annotations

import re

from app.extractor import mysql as mx
from app.services.datasource_service import table_scope_filter as ds_table_scope_filter

SRC_SCHEMA = "ai_web_demo"
TABLES = ["order_main", "order_item", "customer"]


def _norm(sql: str) -> str:
    return " ".join(sql.split()).lower()


def test_a_条_catalogs_排除四个系统库并按库聚合尺寸() -> None:
    """§8.1 A：`SCHEMATA LEFT JOIN TABLES`，系统库不能进来。"""
    sql = mx.SQL_CATALOGS
    low = _norm(sql)
    assert "from information_schema.schemata" in low
    assert "left join information_schema.tables" in low
    for sysdb in ("information_schema", "mysql", "performance_schema", "sys"):
        assert f"'{sysdb}'" in low, f"{sysdb} 必须出现在排除清单里"
    # 尺寸与行数是卡片"这表多大"的依据，必须在这一条里一次拿完
    assert "sum(t.data_length + t.index_length)" in low
    assert "sum(t.table_rows)" in low
    assert "group by" in low


def test_b_条_tables_必须带_schema_条件与表类型过滤() -> None:
    """§8.1 B：漏了 `TABLE_SCHEMA = %s` 会把整个实例抽进来。"""
    sql, params = mx.build_sql_tables(schema="ai_web_demo", scope_sql=None, scope_params={})
    low = _norm(sql)
    assert "from information_schema.tables" in low
    assert "table_schema = :schema" in low, "必须按库过滤"
    assert "table_type in ('base table','view')" in low
    assert params == {"schema": "ai_web_demo"}
    # 视图列注释在 5.7 全空，卡片走降级模板——所以视图必须和表一起被抽到（§10 末）
    assert "view" in low


def test_b_条把源配置的_scope_过滤拼进来而不是自己发明() -> None:
    """include/exclude 的口径与 006 的 `test_connection` 同源（verification §1.2 as-built 注）。

    两处各写一套的话，探测报"9 表 1 视图"而同步抽出 11 张，SSE 的 total 与界面数字就打架。
    共享的是 `datasource_service.table_scope_filter`，它按调用方给的列写法渲染
    （006 无别名、这边带 `t.` 前缀），所以抽取侧不做任何字符串手术。
    """
    from app.models.datasource import DataSource

    row = DataSource(include_tables=["order_%"], exclude_tables=[r"\_%"])
    scope_sql, scope_params = ds_table_scope_filter(row, column="t.table_name")
    sql, params = mx.build_sql_tables("ai_web_demo", scope_sql, scope_params)
    assert "(t.table_name NOT LIKE :exc_0)" in sql
    assert "(t.table_name LIKE :inc_0)" in sql
    assert params["exc_0"] == r"\_%" and params["inc_0"] == "order_%"
    assert params["schema"] == "ai_web_demo"


def _where_of(sql: str) -> str:
    """WHERE 那一段（到 GROUP BY 之前，小写、压掉排版空白）：两条查询只在投影上分开。"""
    low = " ".join(sql.lower().split())
    body = low[low.index("where") :]
    return (body[: body.index("group by")] if "group by" in body else body).rstrip()


def test_范围计数的_where_与_b_条逐字相同_分母不会和抽取分叉() -> None:
    """SSE 每帧的分母来自 `build_sql_count_scope`，它必须和抽表那条用同一段过滤。

    两处各写一套 WHERE 的话，`total` 说的是"我以为有 10 个"，实际落库 11 个，
    而进度条永远停在 9/10 或冲到 11/10——这种错在真机上不会报错，只会一直难看。
    """
    from app.models.datasource import DataSource

    row = DataSource(include_tables=["order_%"], exclude_tables=[r"\_%"])
    scope_sql, scope_params = ds_table_scope_filter(row, column="t.table_name")
    extract = mx.build_sql_tables(SRC_SCHEMA, scope_sql, scope_params)
    count = mx.build_sql_count_scope(SRC_SCHEMA, scope_sql, scope_params)
    assert _where_of(count[0]) == _where_of(extract[0])
    assert count[1] == extract[1], "绑定参数也要一致：少带一个 :inc_0 就等于没过滤"
    low = " ".join(count[0].lower().split())
    assert "group by t.table_type" in low
    # 这一条的全部意义是便宜：昂贵的 C/D/E 一条都不能混进来
    assert "columns" not in low and "statistics" not in low


def test_c_条_columns_带注释_枚举定义_与生成列标记() -> None:
    """§8.1 C：`COLUMN_COMMENT`/`EXTRA`/enum 定义三条都在，缺一条卡片就瞎一块。"""
    sql, params = mx.build_sql_columns(SRC_SCHEMA, TABLES)
    low = _norm(sql)
    for col in (
        "column_comment",
        "extra",
        "ordinal_position",
        "character_maximum_length",
        "numeric_precision",
        "numeric_scale",
        "column_type",
        "is_nullable",
        "column_default",
    ):
        assert col in low
    assert "case when c.data_type='enum' or c.data_type='set' then c.column_type end" in low
    assert "c.table_schema = :schema" in low
    assert params["tbl_0"] == "order_main" and params["tbl_2"] == "customer"


def test_d_条_indexes_保住_cardinality_与_sub_part() -> None:
    """§8.1 D + §10 末验收锚点：这两列非空 = 没退回逐表 `SHOW CREATE TABLE` 方案。"""
    sql, params = mx.build_sql_indexes(SRC_SCHEMA, TABLES)
    low = _norm(sql)
    assert "from information_schema.statistics" in low
    assert "cardinality" in low
    assert "sub_part" in low
    # 复合索引的列顺序是 008 卡片的考点，必须按 SEQ_IN_INDEX 排
    assert "order by s.table_name, s.index_name, s.seq_in_index" in low
    # §8.1 的自连接固定取 SEQ_IN_INDEX=1 那行的索引注释：COMMENT 是索引级属性，
    # 不这么钉住就会随列数翻倍（本机 5.7.17 实测这一列存在，不必退回 SHOW CREATE TABLE）
    assert "ifnull(it.comment,'')" in low
    assert "andit.seq_in_index=1" in low.replace(" ", "")
    assert "s.table_schema=:schema" in low.replace(" ", "")
    assert len([k for k in params if k.startswith("tbl_")]) == len(TABLES)


def test_e_条外键走_key_column_usage_连_referential_constraints() -> None:
    """§8.1 E：只查 KEY_COLUMN_USAGE 拿不到 ON DELETE/UPDATE 规则。"""
    sql, params = mx.build_sql_foreign_keys(SRC_SCHEMA, TABLES)
    low = _norm(sql)
    assert "information_schema.key_column_usage k" in low
    assert "join information_schema.referential_constraints r" in low
    assert "r.constraint_schema = k.constraint_schema" in low
    assert "k.referenced_table_name is not null" in low, "不排掉就会把普通索引当外键"
    assert "r.delete_rule" in low and "r.update_rule" in low
    assert "k.table_schema=:schema" in low.replace(" ", "")
    # 外键的表名同样只走绑定参数，E 条的入参形状与 C/D 一致
    assert set(params) == {"schema", *(f"tbl_{i}" for i in range(len(TABLES)))}


def test_in_列表的占位符数量与表数量一致() -> None:
    """§2.2 点名的第一个防呆点：`IN (...)` 写死一个占位符 → 只抽得到第一张表，全绿。"""
    for build in (mx.build_sql_columns, mx.build_sql_indexes, mx.build_sql_foreign_keys):
        sql, params = build(SRC_SCHEMA, TABLES)
        placeholders = set(re.findall(r":(tbl_\d+)", sql))
        assert placeholders == {f"tbl_{i}" for i in range(len(TABLES))}, build
        assert set(params) - placeholders == {"schema"}, build


def test_五条都是只读语句_且没有任何字面量拼接() -> None:
    """工单 007 验收 6 + nl2sql-safety §4：抽取路径出现非 SELECT 即失败。

    口令只读账号能跑通是一回事，代码里写着 `SET GLOBAL` 是另一回事——这条闸不靠人盯。
    字面量一律走绑定参数，表名/库名来自用户填的数据源配置。
    """
    statements = [
        mx.SQL_CATALOGS,
        mx.build_sql_tables("ai_web_demo", None, {})[0],
        mx.build_sql_columns(SRC_SCHEMA, TABLES)[0],
        mx.build_sql_indexes(SRC_SCHEMA, TABLES)[0],
        mx.build_sql_foreign_keys(SRC_SCHEMA, TABLES)[0],
    ]
    for sql in statements:
        head = sql.strip().split(None, 1)[0].upper()
        assert head == "SELECT", f"抽取只允许 SELECT，拿到 {head}"
        for banned in (
            "INSERT ",
            "UPDATE ",
            "DELETE ",
            "ALTER ",
            "DROP ",
            "CREATE ",
            "TRUNCATE ",
            "CALL ",
            "GRANT ",
        ):
            assert banned not in sql.upper()
        # §10.2：IS 查询里手写 COLLATE 会撞 Illegal mix of collations
        assert "COLLATE" not in sql.upper()
    # 表名不能靠 f-string 拼进 IN 列表
    assert "'order_main'" not in mx.build_sql_columns(SRC_SCHEMA, TABLES)[0]
