"""守卫语料：测试 oracle，逐条抄自 docs/nl2sql-safety.md §8。

改这个文件等于改安全契约——只允许从文档同步，不允许就地发明。
"""

from __future__ import annotations

from app.services.sql_guard import QualifiedTable
from app.services.sql_guard import check as guard_check

# 演示库的 9 表 + 1 视图（docs/verification.md §1）。全部限定在 ai_web_demo 下。
DEMO_TABLES = frozenset(
    QualifiedTable.parse(f"ai_web_demo.{name}")
    for name in (
        "category",
        "product",
        "customer",
        "order_main",
        "order_item",
        "payment_record",
        "refund_record",
        "product_stats_wide",
        "user_activity_log",
        "v_daily_sales",
    )
)

DEFAULT_SCHEMA = "ai_web_demo"

# §8.2 必须被放行
ALLOW_CASES: tuple[str, ...] = (
    "SELECT o.id, SUM(o.amount) AS total FROM order_main o "
    "JOIN customer c ON c.id=o.customer_id "
    "WHERE o.created_at >= '2024-01-01' GROUP BY o.id ORDER BY total DESC LIMIT 10",
    "WITH monthly AS (SELECT DATE_FORMAT(created_at,'%Y-%m') m, SUM(amount) s "
    "FROM order_main GROUP BY 1) "
    "SELECT * FROM monthly ORDER BY s DESC LIMIT 20",
    "SELECT status, COUNT(*) c FROM payment_record GROUP BY status "
    "HAVING COUNT(*) > 5 ORDER BY c DESC",
    "SELECT c.id, c.name FROM customer c LEFT JOIN order_main o ON o.customer_id=c.id "
    "WHERE o.id IS NULL LIMIT 50",
    "SELECT 1 AS a UNION SELECT 2",
    "SELECT id, ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY created_at) rn "
    "FROM order_item LIMIT 100",
    "SELECT DATE(created_at) d, COUNT(*) FROM order_main GROUP BY 1 ORDER BY 1 LIMIT 400",
    "SELECT json_extract(extra_col,'$.city') FROM customer LIMIT 10",
    "SELECT o.id FROM order_main o "
    "WHERE o.id IN (SELECT om.order_id FROM order_item om WHERE om.qty>3) "
    "EXCEPT SELECT r.order_id FROM refund_record r",
    "SELECT p.name, s.* FROM product_stats_wide s JOIN product p ON p.id=s.product_id "
    "ORDER BY s.gmv DESC LIMIT 1",
    "SELECT id FROM customer LIMIT 5 OFFSET 10",
    "SELECT ai_web_demo.customer.name FROM customer LIMIT 5",
    # 行首注释是模型最常见的输出形态之一，不能被当成"整条都是注释"
    "-- 口径说明\nSELECT id FROM customer LIMIT 5",
    "/* 文件头 */ SELECT count(*) FROM order_main",
    # 普通注释不是攻击：必须被抹掉后放行（可执行注释/hint 另有专门规则）。
    # 字符串字面量里的 INTO OUTFILE 是数据不是指令，正则兜底必须跳过引号内内容。
    "SELECT id FROM customer LIMIT 5 -- 口径说明",
    "SELECT /* 块注释 */ id FROM customer LIMIT 5",
    "SELECT id FROM customer LIMIT 5 # 尾注",
    "SELECT 'a INTO OUTFILE b' AS s FROM customer LIMIT 5",
)

# §8.1 必须被拒：(SQL, 期望 rule_id)
REJECT_CASES: tuple[tuple[str, str], ...] = (
    ("SELECT 1; DROP TABLE kb_card", "multi_statement"),
    ("DROP TABLE order_main", "top_level_not_select"),
    ("UPDATE customer SET level='vip' WHERE 1=1", "top_level_not_select"),
    ("DELETE FROM order_item WHERE 1=1", "top_level_not_select"),
    ("INSERT INTO category VALUES (99,'x','y')", "top_level_not_select"),
    ("CREATE TABLE tmp_x AS SELECT * FROM customer", "top_level_not_select"),
    ("TRUNCATE TABLE payment_record", "top_level_not_select"),
    ("CALL sp_purge()", "top_level_not_select"),
    # sqlglot 30 解析不了 HANDLER / LOAD DATA：解析失败即拒，归因是 unsupported_construct
    # （文档 §8.1 原本标 top_level_not_select，那是假设解析成功的结果——拒绝结论不变）
    ("HANDLER customer OPEN", "unsupported_construct"),
    ("HANDLER customer OPEN; READ customer FIRST", "unsupported_construct"),
    ("LOAD DATA INFILE '/etc/passwd' INTO TABLE customer", "unsupported_construct"),
    ("SET GLOBAL general_log = 'ON'", "top_level_not_select"),
    ("SELECT id FROM customer WHERE name = '' OR 1=1; -- \nDROP TABLE customer", "multi_statement"),
    # 规则有先后：先判单语句，所以两条 Command 语料归 multi_statement
    ("PREPARE s FROM 'SELECT 1'; EXECUTE s", "multi_statement"),
    ("PREPARE s FROM 'SELECT 1'", "prepared_statement"),
    ("EXECUTE s", "prepared_statement"),
    ("SELECT $$\n DROP TABLE x\n $$", "unsupported_construct"),
    ("", "empty_statement"),
    ("-- hi", "empty_statement"),
    ("SELECT * FROM mysql.user", "table_not_allowed"),
    ("SELECT name FROM information_schema.columns WHERE table_name='user'", "table_not_allowed"),
    (
        "SELECT * FROM (SELECT 1) t WHERE (SELECT COUNT(*) FROM customer) > 0 "
        "UNION SELECT user,password FROM mysql.user",
        "table_not_allowed",
    ),
    ("SELECT * FROM orders LIMIT 1", "table_not_allowed"),  # §1.2 ⑥ 派生：短表名无权
    ("SELECT * FROM other_db.order_main LIMIT 1", "table_not_allowed"),  # §1.2 ⑥ 跨库
    ("SELECT mysql.user.User FROM customer LIMIT 1", "table_not_allowed"),  # §1.2 ⑥ 三段式列
    ("SELECT * FROM ai_web_demo.information_schema.tables LIMIT 1", "table_not_allowed"),
    # §1.2 ⑥ 派生：三段式里塞系统 schema
    # §8.1 函数黑名单：sqlglot 不认识的函数一律落成 exp.Anonymous，正向白名单即拒
    ("SELECT LOAD_FILE('/etc/passwd')", "danger_function"),
    ("SELECT BENCHMARK(50000000, MD5('a'))", "danger_function"),
    ("SELECT SLEEP(30)", "sleep_function"),
    ("SELECT pg_read_file('/etc/passwd')", "pg_file_access"),
    ("SELECT * FROM pg_ls_dir('/')", "pg_file_access"),
    ("SELECT pg_sleep(10)", "pg_file_access"),
    # 函数扫描不能只看 SELECT 列表：WHERE 里藏一样要拒。
    # 文档 §8.1 #22 标 danger_function，但它与 #16 是同一个函数，归因取 pg_file_access
    ("SELECT 1 WHERE pg_catalog.pg_sleep(1) IS NULL", "pg_file_access"),
    # §8.1 导出到文件 / 变量承接：sqlglot 根本解析不了，所以必须有正则级兜底
    ("SELECT id, name FROM customer INTO OUTFILE '/tmp/c.csv'", "into_outfile"),
    ("SELECT id FROM customer INTO DUMPFILE '/tmp/x'", "into_outfile"),
    ("SELECT * FROM customer LIMIT 1 INTO @v", "into_outfile"),
    # §8.1 锁定子句：只读会话里加锁等于把 SELECT 变成写操作的入口
    ("SELECT * FROM order_main FOR UPDATE", "locking_clause"),
    ("SELECT * FROM order_main LOCK IN SHARE MODE", "locking_clause"),
    ("SELECT * FROM customer c JOIN order_main o ON c.id=o.cid FOR SHARE o.id", "locking_clause"),
    # §8.1 可执行注释与优化器 hint：注释里的 SQL 会被 MySQL 真的执行，
    # 而 comments=False 的重生成会把它整个抹掉——两道防线都不能只靠 AST
    ("SELECT /*!50100 DROP TABLE user */ FROM dual", "executable_comment"),
    ("SELECT /*!32302 1/0, */ 1 AS x", "executable_comment"),
    ("SELECT 1 /*+ MAX_EXECUTION_TIME(1) */", "hint_not_allowed"),
    # §8.1 CTE 里塞 DML：顶层是 SELECT，白名单也全过，只有递归检查能抓到（PG 的写法）
    (
        "WITH x AS (UPDATE customer SET level='vip' RETURNING id) SELECT * FROM x",
        "dml_in_cte",
    ),
    # PROCEDURE ANALYSE() 解析不了，必须正则级兜底
    ("SELECT * FROM order_main GROUP BY id PROCEDURE ANALYSE()", "procedure_analyse"),
    # 超长语句直接拒，不进 parser
    ("SELECT 1 -- " + "x" * 20_000, "too_large"),
    # 文档 §8.1 #33 还把 PG 的 E'\x2d\x2d' 列为必拒。sqlglot 把它解析成普通字符串字面量、
    # 重生成后仍是字面量，不构成注入面，所以不收进语料——已记在 §9 已知限制里。
    # §1.2 ④：变量/占位符的取值只有运行时才知道，静态分析对它失效
    ("SELECT @@version", "variable_access"),
    ("SELECT @x", "variable_access"),
    ("SELECT :p", "variable_access"),
    # §1.2 ④：`:=` 是赋值，右侧写什么都不改变它会产生副作用这件事
    ("SELECT @x := 1", "variable_access"),
    ("SELECT count(*) := 1 FROM customer", "variable_access"),
    # §1.2 ①：控制字符只能到 \t \n \r 为止，混进别的就是注入手法
    ("SELECT id FROM\x00customer LIMIT 5", "unsupported_construct"),
    # 工单验收点名的 ALTER / GRANT
    ("ALTER TABLE customer ADD COLUMN x int", "top_level_not_select"),
    ("GRANT ALL ON customer TO aiweb_ro", "top_level_not_select"),
    # §1.2 ②：MySQL 语义下 `\'` 把字符串吞到语句末尾，sqlglot 抛的是 TokenError。
    # 必须是 SqlGuardError，否则 HTTP 层拿到 500 而不是 400 sql_guard_rejected
    (r"SELECT 'a\' AS x, id FROM ai_web_demo.customer FOR UPDATE", "unsupported_construct"),
)


def run(sql: str, *, dialect: str = "mysql"):
    """语料统一入口：白名单固定为演示库，免得每条用例重复一遍。"""
    return guard_check(sql, allowed=DEMO_TABLES, dialect=dialect, default_schema=DEFAULT_SCHEMA)
