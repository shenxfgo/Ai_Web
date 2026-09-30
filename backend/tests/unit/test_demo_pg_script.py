"""PG 演示源建库脚本的静态闸——不连 PostgreSQL 就能拦住的那部分验收（工单 015）。

同源三件，逐对锁住：
  docs/verification.md §1.5（对象枚举表 + 自检清单）
  backend/scripts/init_demo_pg.sql
  scripts/demo_pg.ps1（外层的 PASS 行数下限）

MySQL 与 PG 都能验的部分（注释真落库、行数、只读授权）在 SQL 末尾的自检段里，由
`make demo-db-pg` 真跑时裁决；本文件只拦"还没连上去就能确定错"的那些——包括最要命的一条：
本机开发环境里我没有 PG 口令（按约定也不该有），所以这份夹具在真跑之前**一次都没执行过**，
静态闸是它唯一的离线证据。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent
SQL_PATH = BACKEND_ROOT / "scripts" / "init_demo_pg.sql"
WRAPPER_PATH = REPO_ROOT / "scripts" / "demo_pg.ps1"
VERIFICATION_DOC = REPO_ROOT / "docs" / "verification.md"

DEMO_DB = "ai_web_demo_pg"
SCHEMA = "demo"
RO_ROLE = "demo_pg_ro"
RO_PASSWORD_PLACEHOLDER = "__AIWEB_PG_RO_PASSWORD__"
# 脚本内部对象与"隔壁 schema"的用例表：都不算业务对象
INTERNAL_OBJECTS = {"_aiweb_demo_marker"}
_OUTSIDE_OBJECTS = {"secret_table"}
_CONSTRAINT_PREFIXES = ("PRIMARY", "UNIQUE", "CONSTRAINT", "FOREIGN", "CHECK")
# 造数唯一允许的"不确定"来源是时间轴（口径见 §1.5.2）：now() 在场，随机函数不许在场
_FORBIDDEN_RANDOM = (r"\brandom\s*\(", r"\bgen_random_uuid\s*\(", r"\bclock_timestamp\s*\(")


@pytest.fixture(scope="module")
def sql() -> str:
    return SQL_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def doc() -> str:
    return VERIFICATION_DOC.read_text(encoding="utf-8")


def _table_body(sql: str, table: str) -> str:
    """取 `CREATE TABLE demo.<table>` 的列定义部分（到本语句的 `);` 为止）。"""
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {SCHEMA}\.{re.escape(table)} \((.*?)\n\);", sql, re.S
    )
    assert match, f"脚本里没有建表语句：{table}"
    return match.group(1)


def _column_defs(body: str) -> list[str]:
    return [
        line.strip()
        for line in body.splitlines()
        if re.match(r"^\s+\w+\s+\S", line)
        and not line.strip().upper().startswith(_CONSTRAINT_PREFIXES)
    ]


def _doc_objects(doc: str) -> dict[str, tuple[int | None, int]]:
    """解析 verification.md §1.5 的对象枚举表：对象名 -> (期望行数或 None, 期望列数)。

    只取那一张表——§1.5 里后面还有格式相同的表格（登记参数、自检清单），按小节标题定位后
    顺着 `|` 行吃到头，避免把 `demo` 这种参数值当成对象名。
    """
    header = doc.index("| 表（schema `demo`） | 行数 | 列数 |")
    lines = doc[header:].splitlines()
    rows: dict[str, tuple[int | None, int]] = {}
    for line in lines[2:]:  # 跳过表头与 |---| 分隔行
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip("|").split("|")]
        name = re.fullmatch(r"`([a-z_][a-z0-9_]*)`.*", cells[0])
        assert name, f"§1.5 枚举表有一行的首格不是反引号对象名：{cells[0]!r}"
        # 视图那格写的是"随时间轴分布而变，只核对非空"——开头没有数字，解析成 None
        digits = re.match(r"[\d,]+", cells[1])
        rows_expected = None if digits is None else int(digits.group(0).replace(",", ""))
        cols = re.fullmatch(r"\**(\d+)\**", cells[2])
        assert cols, f"§1.5 枚举表的列数格解析失败：{cells[2]!r}"
        rows[name.group(1)] = (rows_expected, int(cols.group(1)))
    return rows


def _doc_pass_lines(doc: str) -> list[str]:
    """§1.5.4 那几个代码块里的 PASS 行——外层行数下限与实跑输出的对账清单。"""
    section = re.search(r"^#### 1\.5\.4.*?(?=^#### |\Z)", doc, re.S | re.M)
    assert section, "verification.md 里没有 §1.5.4，行数下限的口径来源被搬走了"
    return [
        line.strip() for line in section.group(0).splitlines() if line.strip().startswith("PASS ")
    ]


# ------------------------------------------------------------------ 编码与密钥


def test_file_is_utf8_without_bom(sql: str) -> None:
    """BOM 会跑到第一条语句前面，psql 当场语法错；cp936 存盘则中文注释全废。"""
    raw = SQL_PATH.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "脚本带 BOM，psql 会在首行报语法错"
    assert raw.decode("utf-8") == sql


def test_ro_password_only_reaches_sql_through_placeholder(sql: str) -> None:
    """本文件进 git，口令只能以占位符形态出现。

    建角色走 `format('... PASSWORD %L', '<占位符>')`，所以断的是**每一处** PASSWORD 字面量
    都恰好是占位符——只数一次的话，第二处真口令会顺着同一个形状混过去。
    """
    assert RO_PASSWORD_PLACEHOLDER in sql, "占位符没了，外层的替换会静默失效"
    created = re.findall(r"LOGIN PASSWORD %L',\s*'([^']*)'", sql)
    assert created == [RO_PASSWORD_PLACEHOLDER], f"建角色那处的口令字面量不是占位符：{created}"
    # 除此之外，任何 `PASSWORD '<字面量>'` 的写法都不许出现（那是真口令唯一的另一个入口）
    assert not re.search(r"PASSWORD\s+'[^']*'", sql), "有语句把口令写成内联字面量"


# ------------------------------------------------------------------ 安全边界


def test_script_never_drops_anything(sql: str) -> None:
    """只建不删：重建是人的决定，脚本拒绝代劳。

    注释里也不许出现那些关键字——验收口径是"grep 不到"，写在说明里一样算命中。
    """
    upper = sql.upper()
    for keyword in ("DROP DATABASE", "DROP SCHEMA", "DROP TABLE", "DROP VIEW", "DROP ROLE"):
        assert keyword not in upper, f"脚本里出现了 {keyword}"
    assert not re.search(r"^\s*DROP\b", sql, re.M), "有 DROP 语句"


def test_metadata_databases_are_never_named(sql: str) -> None:
    """`aiweb` / `aiweb_test` 一个字都不出现（标记表名里那段带下划线，不算独立词）。

    这条把"会不会动元数据库"变成 grep 就能证伪的问题——不必通读 640 行才敢登记数据源。
    """
    assert not re.search(r"\baiweb\b", sql), "出现了元数据库的独立词名"
    assert "aiweb_test" not in sql


def test_wrong_database_and_wrong_encoding_both_abort(sql: str) -> None:
    """两道守卫：连错库中止；库编码不是 UTF8 也中止（中文注释会安静地写成乱码）。"""
    assert re.search(r"current_database\(\)\s*<>\s*'" + re.escape(DEMO_DB) + "'", sql)
    assert "pg_encoding_to_char(encoding)" in sql
    assert re.search(r"IF enc <> 'UTF8' THEN\s+RAISE EXCEPTION", sql)
    assert "SET client_encoding = 'UTF8';" in sql


def test_existing_schema_without_marker_is_refused(sql: str) -> None:
    """接管判断：schema 在但没有标记表 ⇒ 不是我们建的，中止而不是往里插东西。"""
    guard = _takeover_guard(sql)
    assert "has_schema" in guard and "has_marker" in guard
    assert "RAISE EXCEPTION" in guard
    assert "_aiweb_demo_marker" in guard


def _takeover_guard(sql: str) -> str:
    blocks = re.findall(r"DO \$blk\$(.*?)\$blk\$;", sql, re.S)
    hits = [b for b in blocks if "has_marker" in b]
    assert hits, "脚本里没有接管判断那段 DO 块"
    assert len(hits) == 1, "接管判断出现多段，外层 marker 探测与它对不上"
    return hits[0]


def test_grants_are_read_only_and_scoped_to_one_schema(sql: str) -> None:
    """授权面：只给 `demo_pg_ro`，只给这个库的 demo schema，只有 CONNECT/USAGE/SELECT。"""
    grants = [line.strip() for line in sql.splitlines() if re.search(r"\bGRANT\b", line)]
    assert grants, "没有 GRANT 语句"
    shapes = {
        rf"GRANT CONNECT ON DATABASE {DEMO_DB} TO {RO_ROLE}",
        rf"GRANT USAGE\s+ON SCHEMA {SCHEMA}\s+TO {RO_ROLE}",
        rf"GRANT SELECT\s+ON ALL TABLES IN SCHEMA {SCHEMA} TO {RO_ROLE}",
        rf"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} GRANT SELECT ON TABLES TO {RO_ROLE}",
    }
    for line in grants:
        stripped = re.sub(r"\s+", " ", line).rstrip(";").strip()
        assert any(re.fullmatch(shape, stripped) for shape in shapes), f"授权超出预期：{stripped}"
    # 序列不授：只读账号不该有 nextval
    assert not re.search(r"GRANT .*(USAGE|SELECT) ON .*SEQUENCE", sql, re.I)
    # other_app 一个权限都不给——那是"越权读别人的表必须被拒"的用例本体
    assert not re.search(r"GRANT[^;\n]*other_app", sql, re.I)
    assert "TO PUBLIC" not in sql.upper()


# ------------------------------------------------------------------ 与文档同源


def test_objects_and_row_expectations_match_verification_doc(sql: str, doc: str) -> None:
    """§1.5 枚举表的每个对象都要在脚本里建、行数与列数期望要和文档逐字一致。"""
    objects = _doc_objects(doc)
    assert len(objects) == 10, f"§1.5 的对象数变了（现在 {len(objects)}），先对齐口径再改脚本"
    created = set(re.findall(rf"CREATE TABLE IF NOT EXISTS {SCHEMA}\.(\w+)", sql))
    created |= set(re.findall(rf"CREATE OR REPLACE VIEW {SCHEMA}\.(\w+)", sql))
    created -= INTERNAL_OBJECTS | _OUTSIDE_OBJECTS
    assert created == set(objects), f"脚本建的对象与 §1.5 不一致：{created ^ set(objects)}"

    rows_block = _rows_assertion_block(sql)
    uncounted = {name for name, (rows, _) in objects.items() if rows is None}
    assert uncounted == {"v_daily_sales"}, f"只有视图允许不断精确行数：{uncounted}"
    for name, (rows, cols) in objects.items():
        if name == "v_daily_sales":
            body = re.search(rf"CREATE OR REPLACE VIEW {SCHEMA}\.{name} AS(.*?);", sql, re.S)
            assert body, f"脚本里没有视图 {name}"
            got = len(re.findall(r"\bAS\b", body.group(1)))
        else:
            got = len(_column_defs(_table_body(sql, name)))
        assert got == cols, f"{name} 的列数是 {got}，§1.5 要求 {cols}"
        if rows is None:
            assert f"rows:{name}>0" in sql, f"{name} 没有'只断非空'那一行"
            continue
        assert re.search(rf"'{name}'[^\n]*?\b{rows}\b", rows_block), (
            f"{name} 的行数期望值与文档要求的 {rows} 行不一致"
        )


def _rows_assertion_block(sql: str) -> str:
    match = re.search(r"WITH rows_\(t, got, want\) AS \(\s*VALUES(.*?)ORDER BY 1;", sql, re.S)
    assert match, "脚本里没有行数断言段（只核对结构会假绿）"
    return match.group(1)


def test_pass_floor_matches_doc_checklist_and_wrapper(sql: str, doc: str) -> None:
    """文档列了几行 PASS，外层下限就必须是几，脚本里就得真有几行断言。

    三处数字各抄一份就会漂：015 初稿写的 19 后来补了行数与枚举两段变成 34，
    少改一处就是"日志被截断也判绿"或"正常跑完判不通过"。
    """
    listed = _doc_pass_lines(doc)
    assert len(listed) == 34, f"§1.5.4 列了 {len(listed)} 行 PASS，与口径的 34 不符"
    floor = re.search(r"\$passes\s+-lt\s+(\d+)", WRAPPER_PATH.read_text(encoding="utf-8"))
    assert floor, "外层脚本没有 PASS 行数下限那道闸"
    assert int(floor.group(1)) == len(listed), (
        f"外层下限 {floor.group(1)} 与 §1.5.4 的 {len(listed)} 行对不上"
    )
    for line in listed:
        # 文档每行 PASS 后面都带一段 ← 注解，先切掉再解析
        body = line.removeprefix("PASS ").split("←")[0].strip()
        if body.startswith("rows:"):
            name = re.match(r"rows:([a-z_][a-z0-9_]*)", body)
            assert name, f"文档里的行数行形状不认识：{body}"
            table = name.group(1)
            assert f"FROM demo.{table}" in _rows_assertion_block(sql), (
                f"§1.5.4 承诺断 {table} 的行数，脚本的行数段里没有它"
            )
            want = re.search(r"=(\d+)$", body)
            if want:  # 视图那行只断非空，没有精确值
                assert re.search(
                    rf"'{table}'[^\n]*?\b{want.group(1)}\b", _rows_assertion_block(sql)
                )
            continue
        if body.startswith("enum:"):
            enum = re.match(r"enum:([a-z_.]+)=(\d+)", body)
            assert enum, f"文档里的枚举行形状不认识：{body}"
            block = _enum_assertion_block(sql)
            # 首行写成 `count(...) AS got, 6 AS want`，UNION 那几行是位置值，所以按行取
            hit = [ln for ln in block.splitlines() if f"'{enum.group(1)}'" in ln]
            assert hit, f"§1.5.4 承诺断 {enum.group(1)}，脚本的枚举段里没有它"
            assert len(hit) == 1, f"{enum.group(1)} 在枚举段里出现 {len(hit)} 次，期望值会判不准"
            assert re.search(rf"\b{enum.group(2)}\b", hit[0]), (
                f"{enum.group(1)} 的期望值与文档要求的 {enum.group(2)} 不一致：{hit[0]}"
            )
            continue
        key = re.split(r"[=<>]", body)[0].strip()
        assert key in sql, f"文档承诺的断言 {key} 在脚本里找不到"


def _enum_assertion_block(sql: str) -> str:
    match = re.search(r"WITH e AS \((.*?)\n\)\nSELECT", sql, re.S)
    assert match, "脚本里没有枚举值覆盖断言段"
    return match.group(1)


def test_comment_coverage_matches_the_two_downgrade_objects(sql: str) -> None:
    """有表注释的对象必须是 8 个：`t_no_comment` 与视图故意不带（降级分支的活体考点）。"""
    commented = set(re.findall(rf"COMMENT ON TABLE\s+{SCHEMA}\.(\w+)", sql)) - INTERNAL_OBJECTS
    created = set(re.findall(rf"CREATE TABLE IF NOT EXISTS {SCHEMA}\.(\w+)", sql))
    objects = created - INTERNAL_OBJECTS
    views = set(re.findall(rf"CREATE OR REPLACE VIEW {SCHEMA}\.(\w+)", sql))
    assert commented == objects - {"t_no_comment"}, "带注释的表与 §1.5 的 8 张对不上"
    assert not (commented & views), "视图不该有注释"
    assert not re.search(rf"COMMENT ON .*\.{SCHEMA}\.v_daily_sales", sql)
    assert not re.search(rf"COMMENT ON .*\.{SCHEMA}\.t_no_comment", sql)


def test_downgrade_objects_have_no_primary_key(sql: str) -> None:
    """无注释 + 无主键两条分支合在 t_no_comment 上，是刻意把对象总数压在 10 的。"""
    assert "PRIMARY KEY" not in _table_body(sql, "t_no_comment").upper()


def test_activity_log_declares_no_foreign_key(sql: str) -> None:
    """埋点表不建外键，考 FORCE_FK_INFER；脚本自己补上就把考点抹平了。"""
    assert "FOREIGN KEY" not in _table_body(sql, "user_activity_log").upper()
    add_constraints = re.findall(r"ADD CONSTRAINT (\w+) FOREIGN KEY \((\w+)\) ", sql)
    assert len(add_constraints) == 6, "真外键数变了（应为 6），自检的 fk_constraints 会 FAIL"
    assert not [c for c in add_constraints if "user_activity_log" in c[0]]


def test_category_self_reference_is_a_real_constraint(sql: str) -> None:
    """MySQL 版只有列名关系，PG 侧真建一条自引用外键——JOIN 图的自环两边都可测。"""
    assert "fk_category_parent" in sql
    assert re.search(r"FOREIGN KEY \(parent_id\) REFERENCES demo\.category \(id\)", sql)


def test_normalization_material_is_all_present(sql: str) -> None:
    """§9 映射清单点名的类型一个都不能少（023/024 的归一化用例按这张单子写）。"""
    for fragment in (
        "tags       text[]",
        "hit_ids        int4[]",
        "scores         numeric(10, 2)[]",
        "occurred_at    timestamptz[]",
        "top_keywords      varchar(64)[],",
        "attrs      jsonb",
        "serial PRIMARY KEY",
        "GENERATED ALWAYS AS IDENTITY PRIMARY KEY",
        "((lower(name)))",
        "WHERE status = 'on_sale'",
    ):
        assert fragment in sql, f"缺归一化原料：{fragment}"
    # 数组必须保住 atttypmod，否则 varchar(64) 的修饰符就丢了、归一化无从断言
    assert "atttypmod <> -1" in sql


# ------------------------------------------------------------------ 幂等与可复现


def test_every_create_is_idempotent(sql: str) -> None:
    """可重复执行：第二遍不报错、不产生新行。视图必须走 CREATE OR REPLACE。"""
    bare = re.findall(r"^\s*CREATE (TABLE|SCHEMA|INDEX|ROLE|VIEW)\b(?!\s+IF NOT EXISTS)", sql, re.M)
    assert not bare, f"这些语句重复执行会报已存在：{bare}"
    assert not re.search(r"^\s*CREATE VIEW", sql, re.M), "视图没用 CREATE OR REPLACE"


def test_every_insert_is_idempotent(sql: str) -> None:
    """每条 INSERT ... SELECT 都要带 ON CONFLICT 或 WHERE NOT EXISTS。

    `t_no_comment` 没主键，靠不了 ON CONFLICT，用的是 NOT EXISTS——这条就是为了让那种
    写法不被后来的改动悄悄删掉。
    """
    inserts = re.findall(r"INSERT INTO [^;]+;", sql, re.S)
    assert inserts, "脚本里没有插数语句"
    loose = [i for i in inserts if "ON CONFLICT" not in i and "NOT EXISTS" not in i]
    firsts = [i.splitlines()[0] for i in loose]
    assert not loose, f"这些 INSERT 重复执行会撞主键或产生新行：{firsts}"


def test_no_random_source_of_data(sql: str) -> None:
    """造数只用下标同余：随机函数让两次执行的行数/金额/枚举分布不一致，端到端手测核不掉数字。"""
    for pattern in _FORBIDDEN_RANDOM:
        assert not re.search(pattern, sql, re.I), f"用了不可复现的 {pattern}"


def test_serial_sequence_is_bumped_after_seeding(sql: str) -> None:
    """serial 建完数据要把序列推到 max，否则人工插行会撞主键。

    走 DO/PERFORM 而不是裸 SELECT——后者往 stdout 吐一行数字，会混进外层的 PASS/FAIL 计数里。
    """
    assert re.search(r"PERFORM setval\(pg_get_serial_sequence\('demo\.customer', 'id'\)", sql)
    assert not re.search(r"^\s*SELECT setval\(", sql, re.M), "裸 SELECT 版 setval 会污染裁决计数"
