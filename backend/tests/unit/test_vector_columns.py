""" "维度 → 向量列名"那个**唯一纯函数**（工单 029 让它出生，spec 故事 18）。

接缝选在纯函数上，理由与 `test_sync_stage_vocabulary.py` 同一条：验收 11 要钉的是"没有第二处
映射"，把它写在 `sync_service` 或 `retriever` 里，这条断言就只剩人肉 grep。
写侧（029 的 embed 档往哪一列 UPDATE）与读侧（031 的向量路 ORDER BY 哪一列）**必须**读同一份，
否则一侧改了列名，另一侧就检索出一堆 NULL 而不报错。

**机械门只扫 `embedding_1024` 这一个字面**（见 `test_列名字面在源码里只出现在映射与列定义本身`），
`1536 → embedding` 那一半不在 grep 射程内：`embedding` 与 ORM 的属性名同形，裸扫会在每一处
`card.embedding` 上误报，allowlist 就退化成"把所有相关文件都放行"。那一半靠三条别的钉子：
本文件那两条**手写**映射断言、`test_映射给出的列名在_ORM_上真的存在` 的漂移门，以及
`test_kb_upsert_sql.py` 里"改文本要把**全部**向量列洗成 NULL"那条——它的期望值来自 ORM 那张表，
不是来自这份映射，所以"映射长了第三格而写入没跟上"照样红。

期望值都是**手写**的字面，来自 `docs/metadata-model.md` §2.6 与迁移 0008 的实物
（真库侧的实物在 `test_kb_schema_pg.py::test_两套维度的列在真库里各自是那个类型`），
不是从这个模块的 dict 里读回来的——读回来就成同义反复，改错映射时它照样绿。
"""

from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import inspect

from app.core import vector_columns
from app.models.kb import KbCard

# 「1024 这个维度的列名字面」在源码里允许的落点：唯一映射 + 列定义本身（ORM 与迁移 0008）。
# 这一份 allowlist 是验收 11 的字面翻译，不是给实现开的后门：新增一个落点就得同时新增一句
# "为什么这一处也允许"的说明，否则它就是把映射散开的第一个口子。
_ALLOWED_COLUMN_NAME_FILES = {
    "app/core/vector_columns.py",  # 唯一的映射
    "app/models/kb.py",  # ORM 的列与索引定义本身
    "alembic/versions/0008_embedding_columns.py",  # 迁移的加列 DDL 本身
}


def test_已预置的两维各自落到哪一列是逐条写死的() -> None:
    assert vector_columns.vector_column_for(1536) == "embedding"
    assert vector_columns.vector_column_for(1024) == "embedding_1024"


@pytest.mark.parametrize("dimensions", [999, 768, 3072, 0, -1024])
def test_未预置的第三维显式报错_不猜也不静默回落(dimensions: int) -> None:
    """静默回落到"第一列"的代价是：向量落进宽度不符的列，PG 当场报维度错，或者更坏——
    列宽恰好兼容而写进了一堆语义无关的数，检索返回"看起来正常"的错结果（§5.2 (1) 那一格）。
    """
    with pytest.raises(ValueError) as exc:
        vector_columns.vector_column_for(dimensions)
    # 报错要说得出是**哪个维度**没预置，否则运维只能再来一次同步去猜
    assert str(dimensions) in str(exc.value), str(exc.value)


def test_报错文案列出预置了哪几维() -> None:
    """只说"不支持"等于让人去翻代码；把支持的维度印出来，出路才自明（要么建新列，要么换模型）。"""
    with pytest.raises(ValueError) as exc:
        vector_columns.vector_column_for(999)
    detail = str(exc.value)
    assert "1536" in detail and "1024" in detail, detail


def test_映射给出的列名在_ORM_上真的存在() -> None:
    """漂移门：列改名而映射没跟上，这里当场红——而不是到运行时渲染 SQL 才炸。

    断言打在 `KbCard.__table__` 的编译期对象上（不连库），所以它验的是**schema 里有没有这一列**，
    跟"库里现在有没有"是两件事；后者的证据在 `test_kb_schema_pg.py`。
    """
    columns = {c.key for c in inspect(KbCard).mapper.column_attrs}
    for dimensions in vector_columns.PREPROVISIONED_DIMENSIONS:
        name = vector_columns.vector_column_for(dimensions)
        assert name in columns, f"{dimensions} → {name}，但 kb_card 上没有这一列"


def test_列名字面在源码里只出现在映射与列定义本身() -> None:
    """验收 11 的机械版本：`grep` 这件事交给用例，而不是交给下一次人工检查。

    扫的是 `app/` 与 `alembic/versions/` 两棵树的 `.py`（测试文件里那些是**对字段的断言**，
    不是映射，所以不在射程内）。命中 allowlist 之外的文件 = 有人写了第二处映射。
    """
    root = pathlib.Path(__file__).resolve().parents[2]
    hits: list[str] = []
    for base in (root / "app", root / "alembic" / "versions"):
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if "embedding_1024" in path.read_text(encoding="utf-8"):
                hits.append(rel)
    assert set(hits) == _ALLOWED_COLUMN_NAME_FILES, (
        f"第二处映射：{sorted(set(hits) - _ALLOWED_COLUMN_NAME_FILES)}"
    )
