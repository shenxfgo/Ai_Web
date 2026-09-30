"""表卡片文本的构建契约（纯函数：元数据进、markdown 出）。

期望值全部来自 `docs/kb-workflow.md` §5 的模板原文与 §6 的切表口径，
以及 `docs/verification.md` §1 对演示库的实录描述，不是从实现反推的。
"""

from __future__ import annotations

from app.services.kb_service import (
    CARD_TOKEN_LIMIT,
    ColumnMeta,
    IndexMeta,
    RelationMeta,
    TableMeta,
    build_cards,
)
from app.services.token_estimate import estimate_tokens


def _table(*columns: ColumnMeta) -> TableMeta:
    return TableMeta(
        full_name="ai_web_demo.order_main",
        table_type="BASE TABLE",
        comment_zh="订单主表",
        columns=columns,
    )


def test_窄表出一张table卡_首行是表名且带说明() -> None:
    """kb-workflow §5：一表一卡，卡片以【表】行开头，【说明】行取表注释。"""
    table = TableMeta(
        full_name="ai_web_demo.order_main",
        table_type="BASE TABLE",
        comment_zh="订单主表",
        columns=(ColumnMeta(name="id", data_type="bigint"),),
    )

    cards = build_cards(table)

    assert [c.kind for c in cards] == ["table"]
    text = cards[0].text_md
    assert text.splitlines()[0] == "【表】ai_web_demo.order_main"
    assert "【说明】订单主表" in text


def test_字段清单每列一行_带类型可空性键标记和中文注释() -> None:
    """kb-workflow §5 的字段行：`- name type NOT NULL|可空 主键 唯一: 注释`。

    中英混排 + 标识符原样是刻意的——向量模型和 trgm 都要能命中"实付金额"和
    "pay_amount"两个入口（§5 设计要点第 1 条）。
    """
    cards = build_cards(
        _table(
            ColumnMeta(
                name="id", data_type="bigint", nullable=False, is_pk=True, comment_zh="订单ID"
            ),
            ColumnMeta(
                name="pay_amount",
                data_type="numeric(18,2)",
                nullable=False,
                comment_zh="实付金额",
            ),
            ColumnMeta(name="remark", data_type="varchar(255)"),
        )
    )

    text = cards[0].text_md
    assert "【字段】共 3 个" in text
    assert "- id bigint NOT NULL 主键: 订单ID" in text
    assert "- pay_amount numeric(18,2) NOT NULL: 实付金额" in text
    # 无注释列不能整行消失，否则模型不知道这列存在
    assert "- remark varchar(255) 可空: （无注释）" in text


def test_视图卡_缺注释缺行数时逐行降级不报错() -> None:
    """验收①：`v_daily_sales` 视图卡片在列注释缺失时降级不报错。

    视图在 `information_schema.TABLES` 里没有 TABLE_ROWS，所以【规模】必须整行降级，
    不能渲染成"约  行"这种半截话（kb-workflow §5 的"缺原料时的降级字面"）。
    """
    cards = build_cards(TableMeta(full_name="ai_web_demo.v_daily_sales", table_type="VIEW"))

    text = cards[0].text_md
    assert "【表】ai_web_demo.v_daily_sales（视图）" in text
    assert "【说明】（源库无表注释）" in text
    assert "【粒度】未知：一行代表一条记录" in text
    assert "【规模】行数未知（视图或未分析）" in text


def test_枚举取值进字段行_没有取值的列不凭空造取值段() -> None:
    """验收②③：`order_main.status` 的 6 个值必须出现在卡片里，`tags` 那种"看着像枚举
    其实不是"的列不能带出取值段。

    "已完成订单 → status='completed'" 的唯一来源就是这一段文本（kb-workflow §5 设计要点 2、
    §8 的中文枚举是问数准确率第一大坑）。工单口径修正 ①：本片只做**渲染**——
    取值只来自 `meta_column.enum_values`，NDV 采样属 P3。
    """
    cards = build_cards(
        _table(
            ColumnMeta(
                name="status",
                data_type="enum",
                comment_zh="订单状态",
                default="pending",
                enum_values=("pending", "paid", "shipped", "completed", "cancelled", "refunding"),
            ),
            ColumnMeta(name="tags", data_type="varchar(255)", comment_zh="标签"),
        )
    )

    lines = cards[0].text_md.splitlines()
    status_line = next(line for line in lines if line.startswith("- status"))
    tags_line = next(line for line in lines if line.startswith("- tags"))
    assert "取值: pending / paid / shipped / completed / cancelled / refunding" in status_line
    assert "[默认 pending]" in status_line
    assert "取值:" not in tags_line


def test_索引段列出唯一性_类型_列清单和前缀长度() -> None:
    """kb-workflow §5 的【索引】段。

    前缀长度（`SUB_PART`）必须带上：007 专门给演示库补了前缀索引就是为了这个锚点，
    卡片里丢掉它，模型就会以为能对整列 `varchar(255)` 做等值命中。
    """
    cards = build_cards(
        TableMeta(
            full_name="ai_web_demo.order_main",
            columns=(ColumnMeta(name="id", data_type="bigint", nullable=False, is_pk=True),),
            indexes=(
                IndexMeta(name="PRIMARY", unique=True, type="BTREE", columns=("id",)),
                IndexMeta(name="idx_tags", type="FULLTEXT", columns=("tags",), sub_part=100),
            ),
        )
    )

    text = cards[0].text_md
    assert "【索引】" in text
    assert "- PRIMARY（唯一 BTREE）: id" in text
    assert "- idx_tags（FULLTEXT）: tags 前缀 100" in text


def test_可关联段区分外键抽取_命名推断和人工确认() -> None:
    """kb-workflow §5 的【可关联】段：`[推断,置信 x]` 与 `[人工确认]` 是两种来源标记。

    模型必须能分辨"库里有约束"和"只是名字像"，否则 JOIN 条件会被当成事实。
    """
    cards = build_cards(
        TableMeta(
            full_name="ai_web_demo.order_main",
            relations=(
                RelationMeta(
                    from_column="user_id",
                    to_table_full="ai_web_demo.user",
                    to_column="id",
                    kind="extracted",
                ),
                RelationMeta(
                    from_column="sku_code",
                    to_table_full="ai_web_demo.product",
                    to_column="code",
                    kind="inferred",
                    confidence=0.7,
                ),
                RelationMeta(
                    from_column="order_id",
                    to_table_full="ai_web_demo.coupon_record",
                    to_column="order_id",
                    via="order_item",
                    kind="manual",
                ),
            ),
        )
    )

    text = cards[0].text_md
    assert "【可关联】" in text
    assert "- user_id → ai_web_demo.user.id" in text
    assert "- sku_code → ai_web_demo.product.code [推断,置信 0.7]" in text
    # §5 原文的 `' [人工确认]'` 自带前导空格（它要接在列名后面），所以带 via 的那条形成
    # "中转） [人工确认]"——照文档字面渲染，不顺手"美化"掉这个空格。
    assert (
        "- order_id → ai_web_demo.coupon_record.order_id（经 order_item 中转） [人工确认]" in text
    )


def test_方言行只在_mysql_57_上带_cte_与窗口函数告警() -> None:
    """kb-workflow §5：方言约束写在卡片尾部比只写在 system prompt 里对 5.7 更稳。

    文案是 §5 设计要点第 3 条的原文（无 CTE / 无窗口函数 / 默认 ONLY_FULL_GROUP_BY），
    且只有 5.7 才带——8.0 支持 CTE，误标会让模型放弃本来能用的写法。
    """
    mysql57 = build_cards(TableMeta(full_name="db.t", dialect_name="mysql", server_major="5.7"))[
        0
    ].text_md
    mysql80 = build_cards(TableMeta(full_name="db.t", dialect_name="mysql", server_major="8.0"))[
        0
    ].text_md

    assert "【方言】mysql 5.7 — 不支持 CTE 与窗口函数，且默认 ONLY_FULL_GROUP_BY" in mysql57
    assert "【方言】mysql 8.0" in mysql80
    assert "不支持 CTE" not in mysql80


def test_title_是_full_name_带中文说明() -> None:
    """metadata-model §2.6：`title` 的形状就是 `'db.orders（订单表）'`。"""
    cards = build_cards(_table(ColumnMeta(name="id", data_type="bigint")))

    assert cards[0].title == "ai_web_demo.order_main（订单主表）"


def test_search_text_抹掉排版噪声但保留标识符与中文值() -> None:
    """kb-workflow §5：`search_text` = 去掉【】和连接符的扁平拼接。

    【】和 `- ` 会稀释 trigram 且吃 token，所以它们只留在 `text_md`；
    而"实付金额""pay_amount""completed"这三类入口一个都不能少，
    否则关键词路（trgm / simple FTS）就命中不了。
    """
    cards = build_cards(
        _table(
            ColumnMeta(
                name="pay_amount",
                data_type="numeric(18,2)",
                comment_zh="实付金额",
            ),
            ColumnMeta(
                name="status",
                data_type="enum",
                comment_zh="订单状态",
                enum_values=("completed",),
            ),
        )
    )

    search_text = cards[0].search_text
    assert "【" not in search_text and "】" not in search_text
    assert "- " not in search_text
    for needle in ("order_main", "pay_amount", "实付金额", "status", "completed", "订单状态"):
        assert needle in search_text


def test_token_count_是这张卡自己文本的估算值() -> None:
    """§6 的写入路径口径：卡片带 `token_count`，切片和裁预算都读它。

    这里不是同义反复——公式本身由 `test_token_estimate.py` 用字面量钉死，
    这条只钉"卡片确实把**自己这段文本**的估算值带出来了"（漏赋值、
    赋成 title 或 search_text 都会红）。
    """
    cards = build_cards(_table(ColumnMeta(name="id", data_type="bigint")))

    assert cards[0].token_count == estimate_tokens(cards[0].text_md)
    assert cards[0].token_count > 0


def _wide_table(n: int) -> TableMeta:
    cols = [
        ColumnMeta(
            name=f"c{i}",
            data_type="numeric(18,4)",
            comment_zh=f"指标{i}的中文口径说明",
        )
        for i in range(n)
    ]
    cols[0] = ColumnMeta(name="product_id", data_type="bigint", nullable=False, is_pk=True)
    return TableMeta(full_name="ai_web_demo.product_stats_wide", columns=tuple(cols))


def test_宽表按列切片_主卡加table_columns_每卡都在预算内() -> None:
    """§6 的分块口径：列数 > 40 → 主卡（表头 + 前 25 列 + 全部 PK/索引/FK 列）
    + `kind='table_columns'` 每卡 30 列。演示库的 `product_stats_wide` 正好 68 列。

    切片是为了"单块不超 token 预算"（§6 的 900），不是为了好看：超预算的卡在
    prompt 组装时会被整段裁掉，那张表就等于从知识库消失了。
    """
    cards = build_cards(_wide_table(68))

    assert [c.kind for c in cards] == ["table", "table_columns", "table_columns"]
    assert [c.seq for c in cards] == [0, 1, 2]
    assert all(c.token_count <= CARD_TOKEN_LIMIT for c in cards), [
        (c.kind, c.seq, c.token_count) for c in cards
    ]
    # 列不重不漏：切片卡之间不重叠，合起来正好覆盖 68 列
    listed = [name for card in cards for name in _column_names_in(card.text_md)]
    assert len(set(listed)) == 68
    assert "product_id" in cards[0].text_md  # 主键列必须在主卡
    assert "c25" not in cards[0].text_md  # 第 26 列起归切片


def _column_names_in(text: str) -> list[str]:
    """卡片里字段行的形状是 `- <列名> <类型> ...`（§5 模板）。"""
    return [line.split()[1] for line in text.splitlines() if line.startswith("- ")]


def test_切片卡重复表头块并写明本卡只覆盖第几到第几列() -> None:
    """§6：切片卡**重复表头块**，否则"切片自身无判别力"（原文理由）。

    一张只有 30 个字段清单、不知道属于哪张表的卡片，检索命中了也没法写 SQL。
    【字段】那行的"共 N 个"必须是**全表**列数，不是本卡列数——模型据此判断
    还有多少列在这张卡之外。
    """
    shard = build_cards(_wide_table(68))[1]

    assert shard.kind == "table_columns"
    assert "【表】ai_web_demo.product_stats_wide" in shard.text_md
    assert "【字段】共 68 个（本卡仅列出第 26-55 个，完整清单见主卡）：" in shard.text_md


def test_meta里的降权系数只有视图带() -> None:
    """§6 末行：视图"与表同构，`meta.weight=0.6`（检索后降权，物理表优先）。

    权重放在 `meta` 而不是写进文本：它给检索排序用，给 LLM 读没有任何意义，
    塞进 `text_md` 只会让"这张视图不太重要"这种话混进卡片素材里。
    """
    view = build_cards(TableMeta(full_name="ai_web_demo.v_daily_sales", table_type="VIEW"))
    base = build_cards(_table(ColumnMeta(name="id", data_type="bigint")))

    assert view[0].meta["weight"] == 0.6
    assert "weight" not in base[0].meta


def test_meta带列数与行数_行数是未知时不带这个键() -> None:
    """metadata-model §2.6：`meta` 的键例子就是 `{approx_rows, column_count, tags, confidence}`。

    `column_count` 恒带（宽表的主卡只有 25 列，检索侧要能看出这张表到底多宽）。
    `approx_rows` 未知时**缺键**而不是存 `null`：视图在 `information_schema.TABLES`
    里就是没有行数（verification §1 的 `v_daily_sales`），"没有这个信息"和
    "这个表有 0 行"必须能分开——存 null 会让两者在 JSONB 里长得一样。
    """
    wide = build_cards(_wide_table(68))[0]
    with_rows = build_cards(
        TableMeta(
            full_name="ai_web_demo.order_main",
            columns=(ColumnMeta(name="id", data_type="bigint"),),
            approx_rows=12345,
        )
    )
    without_rows = build_cards(TableMeta(full_name="ai_web_demo.v_daily_sales", table_type="VIEW"))

    assert wide.meta["column_count"] == 68
    assert with_rows[0].meta == {"column_count": 1, "approx_rows": 12345}
    assert without_rows[0].meta == {"column_count": 0, "weight": 0.6}
