"""演示库建库脚本的静态闸——不连 MySQL 就能拦住的那部分验收。

三个信息源必须同源，这里逐对锁住：
  docs/verification.md §1（对象与行数表） / init_demo_mysql.sql / tests.guard.corpus.DEMO_TABLES
语料白名单一旦和真实演示库对不上，守卫测试就是在验一个不存在的库。

MySQL 本身才验得了的部分（68 列注释是否真落库、行数、只读授权）在脚本末尾的自检段里，
由 scripts/demo_db.ps1 真跑时裁决；本文件只拦"还没连上去就能确定错"的那些。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent
SQL_PATH = BACKEND_ROOT / "scripts" / "init_demo_mysql.sql"
VERIFICATION_DOC = REPO_ROOT / "docs" / "verification.md"

DEMO_DB = "ai_web_demo"
RO_PASSWORD_PLACEHOLDER = "__AIWEB_RO_PASSWORD__"
# 脚本内部对象，不算业务表：_seq 造完数据即回收，_aiweb_demo_marker 是"本脚本建的"凭证
INTERNAL_OBJECTS = {"_aiweb_demo_marker", "_seq"}
_CONSTRAINT_PREFIXES = ("PRIMARY", "UNIQUE", "KEY", "CONSTRAINT", "FOREIGN")
_CJK = re.compile(r"[一-鿿]")


@pytest.fixture(scope="module")
def sql() -> str:
    return SQL_PATH.read_text(encoding="utf-8")


def _table_body(sql: str, table: str) -> str:
    """取 CREATE TABLE 的列定义部分（不含表选项行）。"""
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {re.escape(table)} \((.*?)\n\) ENGINE", sql, re.S
    )
    assert match, f"脚本里没有建表语句：{table}"
    return match.group(1)


def _doc_expectations() -> dict[str, int | None]:
    """解析 verification.md §1 的表格：对象名 -> 期望行数（视图没有行数，给 None）。

    只取 §1 那一张表——文档后面还有几表格子格式相同但讲的是代码标识符。
    """
    section = re.search(
        r"^## 1\. .*?(?=^## )", VERIFICATION_DOC.read_text(encoding="utf-8"), re.S | re.M
    )
    assert section, "verification.md 里没有 §1，口径来源被搬走了"
    rows: dict[str, int | None] = {}
    for line in section.group(0).splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        name = re.fullmatch(r"`([a-z_][a-z0-9_]*)`.*", cells[0])
        if not name:
            continue
        # 取单元格开头的数字串：文档里同一格里还会跟"（>50k 例外…）"和"行 × 68 列"这类别的
        # 话。上一版用 isdigit() 判定，product_stats_wide 那格因此解析成 None，
        # 于是它的行数从来没被拿来跟文档比过——静默降级成"表名在就行"。
        digits = re.match(r"[\d,]+", cells[1])
        rows[name.group(1)] = None if digits is None else int(digits.group(0).replace(",", ""))
    return rows


def test_file_is_utf8_without_bom(sql: str) -> None:
    """BOM 会跑到第一条语句前面，MySQL 当场语法错；cp936 存盘则中文注释全废。"""
    raw = SQL_PATH.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "脚本带 BOM，MySQL 客户端会在首行报语法错"
    assert raw.decode("utf-8") == sql


def test_charset_and_seed_are_set_before_any_ddl(sql: str) -> None:
    prologue = sql.split("CREATE DATABASE", 1)[0]
    assert "SET NAMES utf8mb4;" in prologue, "缺 SET NAMES，中文注释会以乱码落库"
    assert "SET @seed" in prologue, "缺种子，数据不可复现"


def test_script_never_drops_the_database(sql: str) -> None:
    """只建不删：重建是人的决定，脚本拒绝代劳。

    注释里也不许出现那四个字——验收口径是"grep 不到"，写在说明里一样算命中。
    """
    assert "DROP DATABASE" not in sql.upper()
    assert re.search(r"CREATE DATABASE IF NOT EXISTS `?ai_web_demo`?", sql)


def test_no_8_0_only_collation_and_no_naked_rand(sql: str) -> None:
    # 从 8.0 dump 过来最常踩的一条：5.7 不认 utf8mb4_0900_ai_ci
    assert "0900" not in sql, "出现了 8.0 专属 collation，5.7 会直接报错"
    assert "utf8mb4_general_ci" in sql
    assert "DEFAULTCHARSET=utf8mb4" in sql.replace(" ", ""), "有表没带 utf8mb4 字符集"
    # 裸 RAND() 让两次执行结果不同，端到端手测就核不出数字
    assert not re.search(r"\bRAND\s*\(", sql, re.I), "用了不可复现的 RAND()"


def test_object_names_match_guard_corpus(sql: str) -> None:
    from tests.guard.corpus import DEMO_TABLES

    expected = {t.name for t in DEMO_TABLES}
    created = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql))
    created |= set(re.findall(r"CREATE OR REPLACE VIEW (\w+)", sql))
    assert created - INTERNAL_OBJECTS == expected
    assert all(t.db == DEMO_DB for t in DEMO_TABLES), "语料里的库名与建库脚本不一致"


def test_row_expectations_match_verification_doc(sql: str) -> None:
    """文档表格里的行数与脚本自检的期望值必须逐条一致，否则那句 PASS 毫无意义。"""
    checks = sql.split("check_name", 1)[1]
    tables = _doc_expectations()
    assert len(tables) == 10, f"§1 的对象数变了（现在 {len(tables)}），先对齐口径再改脚本"
    # 只有视图允许没有行数。谁要是把文档表格的写法改得解析不出来，这里必须响，
    # 不能像上一版那样退化成"只检查表名在不在"。
    uncounted = {name for name, rows in tables.items() if rows is None}
    assert uncounted == {"v_daily_sales"}, f"这些对象没解析出行数，断言会静默失效：{uncounted}"
    for table, rows in tables.items():
        if rows is None:
            assert table in sql, f"文档声明的对象 {table} 在脚本里找不到"
            continue
        hit = re.search(rf"'{table}'[^\n]*?\b{rows}\b", checks)
        assert hit, f"{table} 的自检期望值与文档要求的 {rows} 行不一致"


def test_wide_table_has_68_commented_columns(sql: str) -> None:
    body = _table_body(sql, "product_stats_wide")
    defs = [
        line
        for line in body.splitlines()
        if re.match(r"^  \w+ ", line) and not line.strip().upper().startswith(_CONSTRAINT_PREFIXES)
    ]
    assert len(defs) == 68, f"宽表列数是 {len(defs)}，文档要求 68（token 预算裁切按它校准）"
    assert body.count("COMMENT '") == 68, "有列没写注释，卡片模板会退化成裸列名"


def test_every_column_comment_is_chinese(sql: str) -> None:
    comments = re.findall(r"COMMENT '([^']*)'", sql)
    assert len(comments) > 120, f"列注释只有 {len(comments)} 条，建表语句漏写 COMMENT 了"
    bare = [c for c in comments if not _CJK.search(c)]
    assert not bare, f"这些注释没有中文：{bare[:3]}"


def test_activity_log_declares_no_foreign_key(sql: str) -> None:
    """埋点表故意不建外键，考 FORCE_FK_INFER；建库脚本自己补上就把考点抹平了。"""
    body = _table_body(sql, "user_activity_log").upper()
    assert "FOREIGN KEY" not in body
    assert "REFERENCES" not in body


def test_category_self_reference_exists(sql: str) -> None:
    body = _table_body(sql, "category")
    assert re.search(r"parent_id INT NULL", body)
    assert "REFERENCES category (id)" in body, "自引用外键没了，JOIN 图自环的考点就测不到"


def test_ro_password_only_reaches_sql_through_placeholder(sql: str) -> None:
    identified = re.findall(r"IDENTIFIED BY '([^']*)'", sql)
    assert identified == [RO_PASSWORD_PLACEHOLDER] * len(identified), "CREATE USER 里出现了真实口令"
    # 本机 TCP 连接在 MySQL 侧可能被认成 localhost 也可能被认成 127.0.0.1，两个都要有
    assert len(identified) == 2, "aiweb_ro 的 host 只建了一个"


def test_ro_grant_is_select_on_one_database_only(sql: str) -> None:
    grants = re.findall(r"^GRANT ([^;]+);", sql, re.M)
    assert grants, "没有 GRANT 语句"
    for line in grants:
        assert re.fullmatch(
            r"SELECT ON `ai_web_demo`\.\* TO 'aiweb_ro'@'(?:localhost|127\.0\.0\.1)'", line
        ), f"授权超出预期：GRANT {line}（只给一个库的 SELECT，不给 ON *.*）"


def test_every_create_and_insert_is_idempotent(sql: str) -> None:
    """可重复执行：第二遍跑完流程但不产生新行，也不报"已存在"。"""
    bare_create = re.findall(r"^CREATE (?:TABLE|USER) (?!IF NOT EXISTS)\w+", sql, re.M)
    assert not bare_create, f"这些语句重复执行会报已存在：{bare_create}"
    inserts = re.findall(r"^INSERT[^\n]*", sql, re.M)
    assert inserts
    loose = [line for line in inserts if not line.startswith("INSERT IGNORE")]
    assert not loose, f"这些 INSERT 不带 IGNORE，重复执行会撞主键：{loose[:2]}"
    assert not re.search(r"^UPDATE ", sql, re.M), "回填式 UPDATE 会让第二遍跑出不同结果"


def test_seq_table_is_cleaned_up(sql: str) -> None:
    """_seq 只为造数存在；留在库里会污染元数据抽取的对象清单。"""
    assert re.search(r"^DROP TABLE IF EXISTS _seq;$", sql, re.M)
    assert "information_schema.tables" in sql, "缺少对象计数自检，P3 的同步 total 没有同源断言"
