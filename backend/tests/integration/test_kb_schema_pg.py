"""0005 落进真库之后的事：列齐、`vector(1536)` 真的是那个类型、四条索引真的建得起来。

期望值口径：
- `docs/metadata-model.md` §2.6（列 + 四条索引 + profile）
- ADR-0003 的"可插拔边界"：**建库期**依赖 `vector` 类型，**运行期**不依赖 embedding 端点，
  所以"未配 embedding 时卡片照常入库、embedding 为 NULL"必须是真库里的行为
- `docs/verification.md` §2.2：HNSW 真能建、trgm 可用这类 10% 只能在真 PG 上验

单测那半边用 DDL 字符串钉过形状；这里钉的是"PG 收到那些语句之后剩下什么"。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.core.security import encrypt_secret, hash_password
from app.models.datasource import DataSource
from app.models.kb import KbCard, KbIndexProfile
from app.models.meta import MetaDatabase, MetaTable
from app.models.user import User

pytestmark = pytest.mark.pg

_CARD_COLUMNS = {
    "id",
    "datasource_id",
    "kind",
    "table_id",
    "seq",
    "doc_uid",
    "title",
    "text_md",
    "search_text",
    "token_count",
    "meta",
    "index_profile_id",
    "embedding",
    "embedded_at",
    "sync_job_id",
    "created_at",
    "updated_at",
    "deleted_at",
}
_PROFILE_COLUMNS = {
    "id",
    "name",
    "provider",
    "model",
    "dimensions",
    "card_template_version",
    "distance_fn",
    "is_active",
    "created_at",
    "built_at",
}


def _schema() -> str:
    return os.environ["AIWEB_PG__SCHEMA_NAME"]


async def _seed(session: AsyncSession) -> tuple[int, int, int]:
    """建 (datasource, meta_table, profile)——卡片的三根外键各有存在的理由，一次建齐。

    每个用例只调一次：`users.username` 与 `kb_index_profile.name` 都是唯一的，
    调第二次不是"幂等"而是撞约束。
    """
    user = User(username="Owner", password_hash=hash_password("x"), role="admin")
    session.add(user)
    await session.flush()
    src = DataSource(
        name="demo-mysql",
        kind="mysql",
        host="127.0.0.1",
        port=3306,
        connect_user="aiweb_ro",
        secret_enc=encrypt_secret("x"),
        created_by=user.id,
    )
    session.add(src)
    await session.flush()
    db_row = MetaDatabase(datasource_id=src.id, catalog_name="", schema_name="ai_web_demo")
    session.add(db_row)
    await session.flush()
    tbl = MetaTable(
        datasource_id=src.id,
        database_id=db_row.id,
        catalog_name="",
        schema_name="ai_web_demo",
        table_name="orders",
        table_type="BASE TABLE",
    )
    session.add(tbl)
    profile = KbIndexProfile(
        name="text-embedding-3-small@1536@tplv1",
        provider="openai_compatible",
        model="text-embedding-3-small",
        dimensions=1536,
        card_template_version=1,
    )
    session.add(profile)
    await session.flush()
    return src.id, tbl.id, profile.id


async def _card(
    session: AsyncSession,
    *,
    ds: int,
    profile_id: int,
    doc_uid: str,
    table_id: int | None = None,
    kind: str = "table",
    **kw: object,
) -> KbCard:
    card = KbCard(
        datasource_id=ds,
        kind=kind,
        table_id=table_id,
        doc_uid=doc_uid,
        text_md="【表】ai_web_demo.orders",
        search_text="ai_web_demo.orders 订单主表",
        token_count=12,
        index_profile_id=profile_id,
        **kw,
    )
    session.add(card)
    await session.flush()
    return card


async def test_kb_两张表的迁移列与_orm_一一对应(session_factory) -> None:
    """0005 也是手写的：少一列不会让单测变红，只会在第一次真写卡片时炸。"""
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = :s AND table_name IN ('kb_card','kb_index_profile')"
                ),
                {"s": _schema()},
            )
        ).all()
    in_db: dict[str, set[str]] = {}
    for table_name, column_name in rows:
        in_db.setdefault(table_name, set()).add(column_name)

    assert in_db.get("kb_card", set()) == _CARD_COLUMNS
    assert in_db.get("kb_index_profile", set()) == _PROFILE_COLUMNS
    # 迁移 ↔ ORM 也要对上：手写迁移最容易漏的是"模型有列、迁移没建"
    assert {c.name for c in KbCard.__table__.columns} == _CARD_COLUMNS
    assert {c.name for c in KbIndexProfile.__table__.columns} == _PROFILE_COLUMNS


async def test_embedding_列在真库里就是_vector_带维度(session_factory) -> None:
    """`format_type` 是唯一能说清 `vector(1536)` 的口径——information_schema 只给 udt_name。

    维度是列类型的一部分：换 provider 换维度时 PG 不支持 ALTER 改它，只能新 profile + 新列，
    所以这一列必须钉死（§2.6 末）。
    """
    async with session_factory() as session:
        got = (
            await session.execute(
                text(
                    "SELECT pg_catalog.format_type(atttypid, atttypmod) FROM pg_attribute "
                    "WHERE attrelid = (:s || '.kb_card')::regclass AND attname = 'embedding'"
                ),
                {"s": _schema()},
            )
        ).scalar_one()
    assert got == "vector(1536)"


async def test_四条索引在真库里各自成形(session_factory) -> None:
    """§2.6 的 SQL 块；这条用例是"HNSW 真的建在空表上"的唯一证据。

    选 HNSW 而非 IVFFlat 的确定理由就是空表能建（同步是先建空卡再灌），只有真 PG 能验。
    """
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE schemaname = :s AND tablename = 'kb_card'"
                ),
                {"s": _schema()},
            )
        ).all()
    defs = dict(rows)

    assert "ix_kb_card_emb" in defs, "HNSW 没建出来：pgvector 扩展或维度配置有问题"
    assert "USING hnsw" in defs["ix_kb_card_emb"]
    assert "vector_cosine_ops" in defs["ix_kb_card_emb"]
    # PG 回显 reloptions 时给值带上引号（`WITH (m='16', ef_construction='64')`），而
    # verification §2.2 的 DDL 字面是**输入**形态；这里去空格去引号，比的是同一组数。
    emb = defs["ix_kb_card_emb"].replace(" ", "").replace("'", "").lower()
    assert "with(m=16,ef_construction=64)" in emb
    assert "gin_trgm_ops" in defs["ix_kb_card_search_trgm"]
    assert "to_tsvector('simple'" in defs["ix_kb_card_search_fts"]
    assert "search_text" in defs["ix_kb_card_search_fts"]
    assert "deleted_at IS NULL" in defs["ix_kb_card_ds_kind"], "WHERE 丢了就会扫到软删卡"
    assert "(datasource_id, kind)" in defs["ix_kb_card_ds_kind"]


async def test_未配_embedding_时卡片照常入库且向量为_null(session_factory) -> None:
    """验收④（ADR-0003）：向量是增强，缺它不走红。`embedded_at` 同样留空，库里看得出没向量过。"""
    async with session_factory() as session:
        ds, tbl_id, profile_id = await _seed(session)
        card = await _card(session, ds=ds, profile_id=profile_id, doc_uid="d" * 32, table_id=tbl_id)
        assert card.id is not None
        assert card.embedding is None and card.embedded_at is None
        assert card.seq == 0 and card.meta == {} and card.deleted_at is None


async def test_term_卡没有_table_id_也插得进_别的_kind_被_check_挡住(session_factory) -> None:
    """§2.6：term 卡是人工录入的口径，不挂在任何表上；CHECK 不给它之外的第四种 kind。"""
    async with session_factory() as session:
        ds, _, profile_id = await _seed(session)
        card = await _card(session, ds=ds, profile_id=profile_id, doc_uid="e" * 32, kind="term")
        assert card.table_id is None

        session.add(
            KbCard(
                datasource_id=ds,
                kind="column",
                doc_uid="f" * 32,
                text_md="x",
                search_text="x",
                token_count=1,
                index_profile_id=profile_id,
            )
        )
        with pytest.raises(IntegrityError) as exc:
            await session.flush()
        assert "kind_allowed" in str(exc.value)


async def test_doc_uid_重复时第二次插入被挡(session_factory) -> None:
    """幂等键：重建卡片靠它 upsert，不挡住就会越建越多同内容的卡。"""
    async with session_factory() as session:
        ds, tbl_id, profile_id = await _seed(session)
        await _card(session, ds=ds, profile_id=profile_id, doc_uid="a" * 32, table_id=tbl_id)
        session.add(
            KbCard(
                datasource_id=ds,
                kind="table",
                table_id=tbl_id,
                doc_uid="a" * 32,
                text_md="x",
                search_text="x",
                token_count=1,
                index_profile_id=profile_id,
            )
        )
        with pytest.raises(IntegrityError) as exc:
            await session.flush()
        assert "uq_kb_card_doc_uid" in str(exc.value)


async def test_删数据源把卡片一起带走_profile_留着(session_factory) -> None:
    """卡片 CASCADE 到 data_sources；profile 不属于某个源，删源不该抹掉历史 profile。"""
    schema = _schema()
    async with session_factory() as session:
        ds, tbl_id, profile_id = await _seed(session)
        await _card(session, ds=ds, profile_id=profile_id, doc_uid="b" * 32, table_id=tbl_id)
        await session.execute(text(f'DELETE FROM "{schema}".data_sources'))
        cards = (
            await session.execute(
                text(f'SELECT count(*) FROM "{schema}".kb_card WHERE datasource_id = :d'),
                {"d": ds},
            )
        ).scalar_one()
        profiles = (
            await session.execute(
                text(f'SELECT count(*) FROM "{schema}".kb_index_profile WHERE id = :p'),
                {"p": profile_id},
            )
        ).scalar_one()
    assert cards == 0
    assert profiles == 1
