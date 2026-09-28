"""卡片身份：`doc_uid` 与 profile `name` 的算法（metadata-model §2.6 的两行注释）。

期望值是**手算的 md5 字面量**，不是调实现算出来的——这两个字符串一旦变化，库里已有的
卡片就成了对不上号的孤儿：幂等 upsert、以及"换 profile 等于换一套文档"全都靠
"同样的输入永远得到同样的 id"。
"""

from __future__ import annotations

from app.services.kb_service import card_doc_uid, profile_name

# meta_table.table_uid 的形状是 md5 的 32 位十六进制，这里借一个已知的空串 md5 当样本
TABLE_UID = "d41d8cd98f00b204e9800998ecf8427e"


def test_doc_uid_是四元组的md5_分隔符是chr31() -> None:
    """§2.6：`doc_uid char(32) UNIQUE -- md5(card_kind|table_uid|seq|index_profile_id)`。

    分隔符取 chr(31) 而不是文档示意里的 `|`：与 ADR-0005 给 `table_uid` 定的同一条规矩，
    变长字段（seq、profile id）直接相接时 `1|23` 和 `12|3` 会撞，谁也不会想到是 uid 干的。
    """
    assert card_doc_uid("table", TABLE_UID, seq=0, index_profile_id=1) == (
        "f0353ccf55c3d195ef5ce5e945a4b97c"
    )


def test_切片与换profile都会生成另一个doc_uid() -> None:
    """宽表的第 2 张卡、以及换 embedding profile 后的同一张卡，都必须是**新文档**。

    §2.6 末：换模型/改模板要新建 profile 全量重建，新旧两套向量不能混在同一个索引里。
    doc_uid 把 `index_profile_id` 拌进去，正是让"重建"落成插入新行而不是覆盖旧行——
    覆盖就等于把旧 profile 的卡片就地改成新向量，回滚时无从回滚。
    """
    shard = card_doc_uid("table_columns", TABLE_UID, seq=1, index_profile_id=1)
    same_card_other_profile = card_doc_uid("table", TABLE_UID, seq=0, index_profile_id=2)

    assert shard == "77f659cf8c72b9394597808394959a04"
    assert same_card_other_profile == "115c68dc10de7f4210fb8121fd4e8b27"


def test_profile_name_的三段形状() -> None:
    """§2.6 给的字面例子就是 `text-embedding-3-small@1536@tplv1`：模型@维度@模板版本。

    这套 name 是 `uq_kb_index_profile_name` 的冲突键，所以三段一个都不能少——
    少了维度，换维度不换 name；少了模板版本，改模板不换 name，于是"模板变更触发重算"
    在库里连一行新记录都不产生。
    """
    assert (
        profile_name("text-embedding-3-small", dimension=1536, card_template_version=1)
        == "text-embedding-3-small@1536@tplv1"
    )


def test_未配embedding模型时name用no_embedding占位() -> None:
    """ADR-0003：不配向量端点也要建卡片，此时 profile 依然要有一行、name 依然要唯一。

    留空串会渲染成 `@1536@tplv1`——它在 `psql` 里看着像一条坏数据，而"没配模型"是
    一种正常状态，该有个读得出来的字面。
    """
    assert profile_name("", dimension=1536, card_template_version=1) == "no-embedding@1536@tplv1"
