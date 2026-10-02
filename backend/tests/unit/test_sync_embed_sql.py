"""embed 档的三样**形状**：向量 UPDATE 落在哪一列、回落帧还剩几支、期望宽度归谁。

接缝都选在不连库的地方（与 `test_kb_upsert_sql.py` 同一条理由）：这三处写错的后果都不是
"当场报错"，而是——① 新旧两套向量混在同一列里（ADR-0012 立论要防的就是它）、② 把"这台机器
没配端点"说成"P4 不实现向量化"、③ 拿 settings 的宽度去验档案的向量（reindex 的靶是档案那一行）。
每一条都有真跑用例兜着结果，但真跑用例看不见"顺手多写了一列"这种事。

期望值口径：`docs/metadata-model.md` §2.6 的两列（`embedding` / `embedding_1024`）与
`app/core/vector_columns.py` 那份映射；工单 029 已定口径「reindex 用 draft 档案自己的三元组」。
"""

from __future__ import annotations

import ast
import inspect
import re

import pytest
from sqlalchemy import BindParameter
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import visitors

from app.core.errors import AppError
from app.services import embedding_client, kb_service, sync_service


def _sql(stmt: object) -> str:
    rendered = str(stmt.compile(dialect=postgresql.dialect()))
    return re.sub(r"\s+", " ", rendered).strip()


def _assigned_columns(stmt: object) -> set[str]:
    """`SET a = :x, b = now()` 里被赋值的那几个列名。

    按名字拆而不是整串比文本：`updated_at` 那一段的渲染写法属于 SQLAlchemy，不属于本片。
    """
    tail = _sql(stmt).partition(" SET ")[2].partition(" WHERE ")[0]
    return {chunk.split("=")[0].strip() for chunk in tail.split(",") if "=" in chunk}


@pytest.mark.parametrize("column", ["embedding", "embedding_1024"])
def test_一张卡的向量只落在目标列与两个时间戳上(column: str) -> None:
    """ADR-0012 的写侧一半：换维度=换列，**不是**在同一列上覆盖。

    目标列写成另一列的话，旧那批向量会被逐张改写，而"重建中不影响线上检索"当场为假——
    库里两行都合法、没有任何一层会报错。所以"恰好这三个、不多一个"要钉在语句形状上。
    """
    assert _assigned_columns(kb_service.card_vector_update(column)) == {
        column,
        "embedded_at",
        "updated_at",
    }


def test_目标列之外的那一列不出现在语句里() -> None:
    """上一条的反证：`embedding_1024` 那一发不许连带写 `embedding`。

    子串检查会自己骗人（`embedding_1024` 本身就含 `embedding`），所以比的是列名集合。
    """
    assigned = _assigned_columns(kb_service.card_vector_update("embedding_1024"))
    assert "embedding" not in assigned, assigned


def test_向量与卡片_id_都走绑定参数() -> None:
    """一条 1536 维向量拼进字面量是几十 KB 的语句文本，而卡片 id 是内部值——都不该是文本。

    真会踩的坑是 `literal_binds`：同族别的语句为了钉形状会开它，这一条不许开。断在
    AST 的 `BindParameter` 节点上而不是渲染文本上：参数占位符的写法（`%(x)s` / `$1`）属于方言。
    """
    statement = kb_service.card_vector_update("embedding")
    keys = {bp.key for bp in visitors.iterate(statement) if isinstance(bp, BindParameter)}
    assert keys == {"card_id", "vector"}, keys


def _string_literals(module: object) -> list[str]:
    """模块里**参与运算**的字符串常量——注释与 docstring 不计。

    为什么要区分：下一片的历史说明允许写在 docstring 里（"随本片删除的那一支原文是……"），
    而一个还活着的字面量才是"那一档还在产"的证据。整份源码做子串 grep 会把前者也判成违规。
    """
    tree = ast.parse(inspect.getsource(module))
    skipped = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skipped
    ]


def test_embed_回落帧只剩未配置那一支() -> None:
    """工单 029 验收 11 的字面：`embedding_not_implemented` 那一档**随本片删除**。

    删它的理由是"它已经不是事实"：P4 之后配了端点就真灌，剩下唯一不灌的理由是这台机器没配。
    留着它的后果是那句 detail 变成假话，而这条 detail 是前端在 embed 档显示得出来的全部说明。
    """
    for module in (sync_service, kb_service):
        assert all(
            "embedding_not_implemented" not in literal for literal in _string_literals(module)
        ), f"{module.__name__} 里还留着一支已删的 skip"

    payload = sync_service._embed_skipped()
    assert payload["skipped"] is True
    assert payload["code"] == "embedding_not_configured"
    assert payload["detail"], "skip 要给一句人话，否则前端只能显示一个 code"


def test_期望宽度由调用方给客户端而不是客户端读_settings() -> None:
    """已定口径「reindex 用 draft 档案自己的三元组」在客户端签名上的落点。

    宽度若仍从 `settings.embedding.dimension` 取，这颗 reindex 作业会拿"这台机器现在打算用多宽"
    去验"那一套档案实际多宽"，于是 1024 的档案配 1536 的 .env 时整颗作业死于维度不符——
    而该改的是配置，不是这颗作业。签名收**必填**关键字参数是这条口径最便宜的证据。
    """
    param = inspect.signature(embedding_client.EmbeddingClient.embed).parameters[
        "expected_dimension"
    ]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, param
    assert param.default is inspect.Parameter.empty, '不许留"缺省就回到 settings"那条退路'


def test_档案维度没有预置列时抬可辨识的一档() -> None:
    """`vector_column_for` 抛 ValueError，而 `errors[]` 不收裸异常：非 AppError 会被翻成
    "源库返回了无法识别的错误"——主语错了（这是元数据库少一列，不是源库不通）。
    """
    with pytest.raises(AppError) as caught:
        kb_service.vector_target_for(dimensions=999, model="any")
    assert caught.value.code == "vector_column_missing", caught.value
    assert "999" in str(caught.value), str(caught.value)


def test_落点带的是档案那一行的模型名而不是调用方自己读的那一格() -> None:
    """已定口径"reindex 用 draft 档案**自己的三元组**"里 `model` 那一格的落点（双轴审查·规格轴）。

    只把 `dimensions` 从档案读、`model` 仍从 settings 拿的话，同一颗作业一半认档案一半认配置：
    问的是新模型、按旧模型的宽度校验，两边恰好同宽时静默灌进一批来自另一个语义空间的向量。
    所以 `VectorTarget` 必须有 `model` 这一格，且它逐字等于传进来的那一个。
    """
    target = kb_service.vector_target_for(dimensions=1024, model="bge-large-zh-v1.5")
    assert target.model == "bge-large-zh-v1.5", target
    assert (target.dimensions, target.column) == (1024, "embedding_1024"), target


def test_收尾那句只动_built_at_一格() -> None:
    """`built_at` 是这颗作业能给档案的唯一礼物（工单 029 已定口径）。

    `status` 不在射程内：draft→active 是 030 那道激活门槛的权力，同步链路自己改状态等于把
    "重建中不影响线上检索"变成"重建完自动上线"。多写一格列就是越权，所以钉在语句形状上。
    """
    assert _assigned_columns(kb_service.mark_profile_built(profile_id=7)) == {"built_at"}


def test_档案那一维落到它自己的列上() -> None:
    """两维各落各列。列名期望值来自 §2.6 与迁移 0008 的实物（不从 `vector_columns` 的 dict
    读回来比）。`model` 这一格不参与翻译，给个占位名即可。"""
    assert kb_service.vector_target_for(dimensions=1536, model="m").column == "embedding"
    assert kb_service.vector_target_for(dimensions=1024, model="m").column == "embedding_1024"
