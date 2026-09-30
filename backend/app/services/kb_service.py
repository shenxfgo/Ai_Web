"""知识库卡片：模板见 `docs/kb-workflow.md` §5，逐段实现。

分三截读：
- **生成**（纯函数）：元数据进、markdown 出，不碰数据库、不碰网络（工单 008 的"纯函数"约束）。
- **写入**（"写入路径"一节）：只碰元数据库的 `kb_card`/`kb_index_profile`，不碰源库。
- **读取**（"读取路径"一节）：接口层按 `table_uid` 取段，顺带当场判 read 权。

卡片文本是后续 prompt 的唯一素材，所以前两截之间的边界就是 `CardDoc`：换落库方式不动生成，
改模板不动落库。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from jinja2 import Environment, FileSystemLoader
from sqlalchemy import Table, func, literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import MappingResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.models.kb import KbCard, KbIndexProfile
from app.models.meta import (
    MetaColumn,
    MetaIndex,
    MetaIndexColumn,
    MetaRelation,
    MetaTable,
)
from app.models.user import User
from app.schemas.kb import KbCardOut, KbCardProfileOut
from app.services import datasource_service
from app.services.token_estimate import estimate_tokens
from app.settings import get_settings

PROMPT_DIR: Final = Path(__file__).resolve().parent.parent / "prompts"

# §6 分块策略的四个常数（原文："列数 ≤ 40 且 token_count ≤ 900 → 一张卡；
# 列数 > 40 → 主卡（表头 + 前 25 列 + 全部 PK/索引列/外键列）+ 每卡 30 列的 table_columns 切片"）。
# 写成常数而不是设置键：§6 没给对应的 `AIWEB_*` 键，`.env.example` 里也没有，
# 真要调的时候再一起补键，别先造一个没人读的开关。
MAX_COLUMNS_PER_CARD: Final = 40
CARD_TOKEN_LIMIT: Final = 900
MAIN_CARD_COLUMNS: Final = 25
SHARD_COLUMNS: Final = 30
# §6 表格末行："视图 | 与表同构，meta.weight=0.6（检索后降权，物理表优先）"。
# 这是检索层的乘法系数，不是卡片文本的一部分。
VIEW_WEIGHT: Final = 0.6


@dataclass(frozen=True)
class ColumnMeta:
    name: str
    data_type: str
    nullable: bool = True
    is_pk: bool = False
    is_unique: bool = False
    comment_zh: str | None = None
    comment_raw: str | None = None
    default: str | None = None
    enum_values: tuple[str, ...] = ()


@dataclass(frozen=True)
class IndexMeta:
    name: str
    type: str
    columns: tuple[str, ...] = ()
    unique: bool = False
    sub_part: int | None = None


@dataclass(frozen=True)
class RelationMeta:
    from_column: str
    to_table_full: str
    to_column: str
    kind: str = "extracted"
    via: str | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class TableMeta:
    full_name: str
    table_type: str = "BASE TABLE"
    comment_zh: str | None = None
    comment_raw: str | None = None
    granularity: str | None = None
    approx_rows: int | None = None
    last_update: str | None = None
    columns: tuple[ColumnMeta, ...] = ()
    indexes: tuple[IndexMeta, ...] = ()
    relations: tuple[RelationMeta, ...] = ()
    dialect_name: str = "mysql"
    server_major: str = ""


@dataclass(frozen=True)
class CardDoc:
    kind: str
    seq: int
    title: str
    text_md: str
    search_text: str
    token_count: int
    # metadata-model §2.6 的 jsonb 旁证位：给检索排序/过滤用的机器字段都在这里，
    # 不进 text_md——那些文本是给 LLM 读的素材，两种受众混在一起会同时毁掉两边。
    meta: dict[str, Any]


def _meta(table: TableMeta) -> dict[str, Any]:
    """§6 末行给视图的 `weight=0.6`（物理表优先），加上 §2.6 点到的两个规模键。

    未知量走**缺键**而不是 `None`：JSONB 里 `null` 和"没有这个键"是可区分的两种事实，
    而"约 0 行"和"没分析过、不知道行数"对检索是两个不同信号（视图恒为后者）。
    """
    meta: dict[str, Any] = {"column_count": len(table.columns)}
    if table.approx_rows is not None:
        meta["approx_rows"] = table.approx_rows
    if table.table_type == "VIEW":
        meta["weight"] = VIEW_WEIGHT
    return meta


# 身份算法（metadata-model §2.6）：doc_uid 与 profile name 都在这层算，
# 因为同步侧和重建脚本都要能独立算出同一个值——放在 SQL 里算就只能连库才能算。
_UID_SEP: Final = "\x1f"

# §2.6 的 name 形状 `text-embedding-3-small@1536@tplv1`；未配模型时模型段落的占位字面。
NO_EMBEDDING_MODEL: Final = "no-embedding"


def card_doc_uid(kind: str, table_uid: str, *, seq: int, index_profile_id: int) -> str:
    """`md5(card_kind|table_uid|seq|index_profile_id)`（§2.6）。

    分隔符用 chr(31) 而不是文档示意里的 `|`：与 ADR-0005 给 `table_uid` 定的同一条规矩——
    seq 和 profile id 都是变长的，直接相接时 `1|23` 与 `12|3` 会撞成同一个 md5，
    而撞车表现为"另一张表的卡片覆盖了这张表"，排查起来毫无线索。
    """
    parts = (kind, table_uid, str(seq), str(index_profile_id))
    return hashlib.md5(_UID_SEP.join(parts).encode()).hexdigest()


def profile_name(model: str, *, dimension: int, card_template_version: int) -> str:
    """§2.6 的 profile `name`：`{model}@{dimension}@tplv{card_template_version}`。

    三段全进 `uq_kb_index_profile_name` 的冲突键：少一段，对应那类变更（换维度、改模板）
    就不会产生新 profile，于是"重建"原地变成"覆盖"。
    """
    return f"{model or NO_EMBEDDING_MODEL}@{dimension}@tplv{card_template_version}"


def _human_int(value: int | None) -> str:
    """`12345` → `12,345`：卡片里的行数是给 LLM 读的，千分位比"约 1.2 万"少一层歧义。"""
    return f"{value:,}" if value is not None else ""


# search_text 的噪声口径（kb-workflow §5："去掉【】和连接符的扁平拼接"）：
# 【】标签整段抹掉（它们每张卡都一样，留着只会稀释 trigram），行首 `- ` 项目符也抹掉，
# 但标识符、中文注释、枚举取值必须原样留下——那是关键词路唯一的入口。
_NOISE_RE: Final = re.compile(r"【[^】]*】|^[ \t]*- ", re.M)


def _search_text(text_md: str) -> str:
    return " ".join(_NOISE_RE.sub(" ", text_md).split())


def _title(table: TableMeta) -> str:
    """metadata-model §2.6 的 title 形状：`db.orders（订单表）`；没注释就只留全名。"""
    comment = table.comment_zh or table.comment_raw
    return f"{table.full_name}（{comment}）" if comment else table.full_name


_ENV: Final = Environment(
    loader=FileSystemLoader(str(PROMPT_DIR)),
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=False,
    keep_trailing_newline=False,
)
_ENV.filters["human_int"] = _human_int


def _segments(table: TableMeta) -> list[tuple[str, list[ColumnMeta], int, int]]:
    """§6 的切表口径：窄表一张卡，宽表主卡 + 每 30 列一张切片卡。

    主卡除了前 25 列，还要带上**全部** PK/索引/外键列——模型判断 JOIN 走哪条路时
    靠的就是这几列，把它们切进第 3 张卡等于让它猜。

    返回的四元组里带切片在**全表**里的首/末列序号：被提进主卡的键列会让切片不连续，
    所以序号按原表位置算，不按切片内的下标算。
    """
    columns = list(table.columns)
    if len(columns) <= MAX_COLUMNS_PER_CARD:
        return [("table", columns, 1, len(columns))]

    position = {c.name: i + 1 for i, c in enumerate(columns)}
    key_names = {c.name for c in columns if c.is_pk}
    key_names |= {name for idx in table.indexes for name in idx.columns}
    key_names |= {rel.from_column for rel in table.relations}

    head, tail = columns[:MAIN_CARD_COLUMNS], columns[MAIN_CARD_COLUMNS:]
    main = head + [c for c in tail if c.name in key_names]
    rest = [c for c in tail if c.name not in key_names]

    segments: list[tuple[str, list[ColumnMeta], int, int]] = [("table", main, 1, len(main))]
    for start in range(0, len(rest), SHARD_COLUMNS):
        chunk = rest[start : start + SHARD_COLUMNS]
        segments.append(("table_columns", chunk, position[chunk[0].name], position[chunk[-1].name]))
    return segments


def build_cards(table: TableMeta) -> list[CardDoc]:
    """一张表 → 一到多张卡片（`docs/kb-workflow.md` §5 模板 + §6 切表口径）。

    纯函数：元数据进、markdown 出。落库在下面的"写入路径"一节，两节之间只隔 `CardDoc`。
    """
    docs: list[CardDoc] = []
    for seq, (kind, seg, seg_from, seg_to) in enumerate(_segments(table)):
        text_md = _ENV.get_template("card_template.j2").render(
            full_name=table.full_name,
            table_type=table.table_type,
            table_comment=table.comment_zh or table.comment_raw,
            granularity=table.granularity,
            approx_rows=table.approx_rows,
            last_update=table.last_update,
            columns=seg,
            column_count=len(table.columns),
            shard=kind == "table_columns",
            seg_from=seg_from,
            seg_to=seg_to,
            indexes=list(table.indexes) if kind == "table" else [],
            relations=list(table.relations) if kind == "table" else [],
            dialect_name=table.dialect_name,
            server_major=table.server_major,
        )
        docs.append(
            CardDoc(
                kind=kind,
                seq=seq,
                title=_title(table),
                text_md=text_md,
                search_text=_search_text(text_md),
                token_count=estimate_tokens(text_md),
                meta=_meta(table),
            )
        )
    return docs


# ============================================================ 写入路径
#
# architecture.md 的模块表把"卡片生成（纯函数）+ 建索引 + 检索"都放在这一层。
# 上面那半截不碰数据库；下面这半截只碰元数据库，不碰源库——卡片原料永远来自 `meta_*`。


# 没启用 sqlalchemy 的 mypy 插件时 `DeclarativeBase.__table__` 被标成 `FromClause`，
# 这里的 cast 与 sync_service 同一条理由。
_CARD = cast("Table", KbCard.__table__)
_PROFILE = cast("Table", KbIndexProfile.__table__)
_META_TABLE = cast("Table", MetaTable.__table__)
_META_COLUMN = cast("Table", MetaColumn.__table__)
_META_INDEX = cast("Table", MetaIndex.__table__)
_META_INDEX_COLUMN = cast("Table", MetaIndexColumn.__table__)
_META_RELATION = cast("Table", MetaRelation.__table__)

# 每轮同步都重写的正文列。`index_profile_id` **有意不在这里**：doc_uid 里已经拌了它，
# 放进 set_ 就等于允许一条 upsert 把卡片从旧 profile 搬到新 profile，
# 而 §2.6 要的是"两套 profile 各一批行、可回滚"。`doc_uid`/`created_at` 同理不进。
_CARD_CONTENT_COLS = (
    "datasource_id",
    "kind",
    "table_id",
    "seq",
    "title",
    "text_md",
    "search_text",
    "token_count",
    "meta",
    "sync_job_id",
)


def kb_card_upsert() -> Any:
    """§2.6 的 `doc_uid` 唯一键 upsert：同一轮同步跑两遍，卡片不翻倍。

    两个细节是有意的：
    ① `embedding`/`embedded_at` 被显式清成 NULL——卡片文本变了，旧向量就不再描述它了
       （§5 的"编辑后触发该表 embedding 重算"是同一条语义）。留着不洗是最坏的选择，
       它会让检索拿旧向量匹配新内容，而且从结果里看不出过期。
    ② `meta` 整值替换：卡片没有人工列（术语卡的人工性在 `term` 那一行，P2 不构建），
       所以不需要 §3 那套 COALESCE 保护。
    """
    stmt = pg_insert(_CARD)
    return stmt.on_conflict_do_update(
        index_elements=[_CARD.c.doc_uid],
        set_={
            **_card_new(stmt, _CARD_CONTENT_COLS),
            "embedding": literal_column("NULL"),
            "embedded_at": literal_column("NULL"),
            "updated_at": func.now(),
        },
    )


def _card_new(stmt: Any, names: tuple[str, ...]) -> dict[str, Any]:
    return {name: getattr(stmt.excluded, name) for name in names}


# 未配置向量端点时 model 列为空，name 里用同一个占位字面（与 profile_name 一致）
EMBEDDING_PROVIDER: Final = "openai_compatible"


def profile_row(*, is_active: bool = True) -> dict[str, Any]:
    """`kb_index_profile` 的一行：`(model, dim, tplv)` 三元组的物化（§2.6）。

    P2 的口径（工单 008 修正 ③）：只建表 + 落这一条行，`draft→全量重建→原子切 active`
    那套机制属 P4，所以这里直接 `is_active=true`——它一落库就是当前生效的那套。
    """
    settings = get_settings()
    emb, version = settings.embedding, settings.extract.card_template_version
    return {
        "name": profile_name(emb.model, dimension=emb.dimension, card_template_version=version),
        "provider": EMBEDDING_PROVIDER,
        "model": emb.model,
        "dimensions": emb.dimension,
        "card_template_version": version,
        "is_active": is_active,
    }


async def ensure_profile(session: AsyncSession) -> int:
    """取回当前配置的 profile id，没有就建一条（幂等）。

    name 是 §2.6 的 UNIQUE 键，所以"已存在"必须原样复用：每次同步都新建一个 profile，
    等于每轮都把全部卡片重插一遍成新 doc_uid，旧的那套永远留在库里。
    """
    name = profile_row()["name"]
    existing = await session.execute(select(_PROFILE.c.id).where(_PROFILE.c.name == name))
    found = existing.scalar_one_or_none()
    if found is not None:
        return int(found)
    created = await session.execute(
        pg_insert(_PROFILE).values(**profile_row()).returning(_PROFILE.c.id)
    )
    return int(created.scalar_one())


# ================================================== meta_* 行 → TableMeta
#
# 这半截是"读元数据库"与"生成卡片"之间唯一的映射层：SQL 负责把该读的都读齐（含 JOIN
# 出目标表全名），这里负责把它们摆成模板要的形状。分开是因为两边出错的样子完全不同——
# 前者是漏了列，后者是注释优先级搞反。


def _full_name(catalog_name: str, schema_name: str, table_name: str) -> str:
    """`§1 的规范化列` → §5 的 `full_name`：MySQL 的 catalog 恒空串，不能打头。

    拼成 `.ai_web_demo.order_main` 的话，模型会把带点开头的名字抄进 SQL，
    而那条表名在它自己的库里根本不存在。
    """
    return ".".join(part for part in (catalog_name, schema_name, table_name) if part)


def _server_major(server_version: str) -> str:
    """`5.7.44-log` → `5.7`。§5 的方言条件比的是两段式主版本。"""
    parts = server_version.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else server_version.strip()


def table_meta_from_rows(
    *,
    dialect_name: str,
    server_version: str,
    table: Mapping[str, Any],
    columns: Sequence[Mapping[str, Any]],
    indexes: Sequence[Mapping[str, Any]] = (),
    index_columns: Sequence[Mapping[str, Any]] = (),
    relations: Sequence[Mapping[str, Any]] = (),
) -> TableMeta:
    """§2.4 的行 → §5 模板的变量。

    注释的三级降级（`comment_zh or comment_raw or '（无注释）'`）是模板的职责，
    这里一个都不兜：兜了就把源库的英文字面注释压成了占位符，而 §5 要它进文本。
    """
    by_index: dict[int, list[Mapping[str, Any]]] = {}
    for child in index_columns:
        by_index.setdefault(int(child["index_id"]), []).append(child)

    index_metas: list[IndexMeta] = []
    for idx in indexes:
        members = sorted(by_index.get(int(idx["id"]), []), key=lambda c: int(c["seq_in_index"]))
        index_metas.append(
            IndexMeta(
                name=str(idx["index_name"]),
                type=str(idx["index_type"]),
                columns=tuple(str(m["column_name"]) for m in members),
                unique=bool(idx["is_unique"]),
                sub_part=next(
                    (int(m["sub_part"]) for m in members if m["sub_part"] is not None), None
                ),
            )
        )

    return TableMeta(
        full_name=_full_name(
            str(table["catalog_name"]), str(table["schema_name"]), str(table["table_name"])
        ),
        table_type=str(table["table_type"]),
        comment_zh=table["comment_zh"],
        comment_raw=table["comment_raw"],
        granularity=table["granularity"],
        approx_rows=None if table["approx_rows"] is None else int(table["approx_rows"]),
        last_update=(
            None if table["last_analyze_at"] is None else str(table["last_analyze_at"])[:10]
        ),
        columns=tuple(
            ColumnMeta(
                name=str(c["column_name"]),
                data_type=str(c["data_type"]),
                nullable=bool(c["nullable"]),
                is_pk=bool(c["is_primary_key"]),
                is_unique=bool(c["is_unique"]),
                comment_zh=c["comment_zh"],
                comment_raw=c["comment_raw"],
                default=c["default_value"],
                # §8 的采样（P3）还没落之前，这里是空——模板据此不出「取值:」行
                enum_values=tuple(c["enum_values"] or ()),
            )
            for c in columns
        ),
        indexes=tuple(index_metas),
        relations=tuple(
            RelationMeta(
                from_column=str(r["from_column_name"]),
                to_table_full=_full_name(
                    str(r["to_catalog_name"]), str(r["to_schema_name"]), str(r["to_table_name"])
                ),
                to_column=str(r["to_column_name"]),
                kind=str(r["source_kind"]),
                # 库里的列是 numeric(4,3)，读回来是 Decimal；`float` 之后模板里的
                # `[推断,置信 0.7]` 才是 §5 的字面，而不是 `Decimal('0.700')`
                confidence=None if r["confidence"] is None else float(r["confidence"]),
            )
            for r in relations
        ),
        dialect_name=dialect_name,
        server_major=_server_major(server_version),
    )


# ============================================================ 一次卡片构建
#
# 读料 → 装配 → 生成 → 落库。四步只在 `sync_cards` 里串起来，各步都能单独测：
# 装配的错在 `table_meta_from_rows` 的单测里，落库的错在 pg 用例里。


# §2.4 的列名清单。写成显式清单而不是 `select(MetaTable)`：这张清单同时是
# "读料 SQL 的列" 与 "纯函数的入参键" 两份契约的唯一交接面——少一列的话，
# 装配那一步 KeyError，而不是安静地渲染出半张卡。
_TABLE_FIELDS: Final = (
    "id",
    "table_uid",
    "catalog_name",
    "schema_name",
    "table_name",
    "table_type",
    "comment_raw",
    "comment_zh",
    "granularity",
    "approx_rows",
    "last_analyze_at",
)
_COLUMN_FIELDS: Final = (
    "table_id",
    "column_name",
    "data_type",
    "nullable",
    "is_primary_key",
    "is_unique",
    "comment_raw",
    "comment_zh",
    "default_value",
    "enum_values",
)
_INDEX_FIELDS: Final = ("id", "table_id", "index_name", "index_type", "is_unique")
_INDEX_COLUMN_FIELDS: Final = ("index_id", "seq_in_index", "column_name", "sub_part")


def _group(rows: Sequence[Mapping[str, Any]], key: str) -> dict[Any, list[Mapping[str, Any]]]:
    out: dict[Any, list[Mapping[str, Any]]] = {}
    for row in rows:
        out.setdefault(row[key], []).append(row)
    return out


def _as_dicts(mappings: MappingResult) -> list[dict[str, Any]]:
    """`mappings()` 的行落成普通 dict。

    两个理由：① `RowMapping.__getitem__` 在类型层收 `int|str|slice`，不是
    `Mapping[str, Any]`，而 `_group`/`table_meta_from_rows` 是纯函数，它们的入参契约
    不该让 SQLAlchemy 的行类型渗进去（mypy 正是报在这里）；② 卡片生成要按表遍历这些行
    好几遍，落成 list 之后才不必担心游标只走一次。
    """
    return [dict(row) for row in mappings]


@dataclass(frozen=True, slots=True)
class CardBuildFailure:
    """一张表的卡片构建失败：表全名（`db.t` 形状）+ 原始异常。

    表名跟着失败走而不是编号：`sync_jobs.errors` 要带的是人读得出"哪张表"的名字
    （CONTEXT.md 的 partial 词条："`errors` 里带表名"），而 table_id 到了库里就没人认得了。
    """

    table_full_name: str
    error: Exception


@dataclass(frozen=True, slots=True)
class CardBuildOutcome:
    """一轮卡片构建的收成：写出的卡片条数 + 逐表失败清单。

    `cards` 只数**已提交**的那些（工单 019 已定口径：`counters.cards` 的语义仍是
    "本轮写出的卡片条数"，逐表提交只改事务边界、不改计数）。
    """

    cards: int
    failures: tuple[CardBuildFailure, ...] = ()


async def sync_cards(
    session: AsyncSession,
    *,
    datasource_id: int,
    job_id: int | None,
    table_ids: Sequence[int],
    dialect_name: str,
    server_version: str,
    card_build_hook: Callable[[str], None] | None = None,
) -> CardBuildOutcome:
    """给"本轮确认过的"表重建卡片；**一张表一个事务**，一张炸了不回滚已成功的那些张。

    `table_ids` 只准传本轮同步落成的表：陈旧表（`is_stale`）源库里已经没有实体了，
    还给它刷卡片等于让 AI 照一张不存在的表写 SQL。

    逐表提交（工单 019，roadmap P3 验收 5 的前提）：每张表的卡片 upsert 自成一次
    commit，下一张表的失败只作废它自己。事务边界挪到这里之后，调用方**不再**为卡片
    统一提交——`_set_phase` 自己会 commit，卡片提交与 phase 提交不许并进同一个事务。

    `card_build_hook` 是 **test-only** 的注入点（工单 019 已定口径）：由参数一路穿进来、
    在每张表的卡片构建**起点**调用（表全名入参），抛错就是"这张表构建失败"。
    生产路径永远传 `None`；生产代码里不存在"如果这是测试就抛错"的分支。
    """
    ids = sorted(set(table_ids))
    if not ids:
        return CardBuildOutcome(cards=0)
    profile_id = await ensure_profile(session)
    # profile 单独钉成事务：逐表失败的那次 rollback 不许把它一起带走——它若没落定，
    # 之后每张表的成功提交都会在 `kb_card.index_profile_id` 的外键上当场拒收。
    await session.commit()

    tables = _as_dicts(
        (
            await session.execute(
                select(*[_META_TABLE.c[name] for name in _TABLE_FIELDS])
                .where(_META_TABLE.c.id.in_(ids))
                .order_by(_META_TABLE.c.id)
            )
        ).mappings()
    )
    columns = _group(
        _as_dicts(
            (
                await session.execute(
                    select(*[_META_COLUMN.c[name] for name in _COLUMN_FIELDS])
                    .where(_META_COLUMN.c.table_id.in_(ids))
                    # 按 ordinal_position 而不是列名：§5 的字段清单要跟源库定义顺序一致，
                    # 否则"前 25 列"切的是随机 25 列，宽表重跑一次切片内容就变了
                    .order_by(_META_COLUMN.c.table_id, _META_COLUMN.c.ordinal_position)
                )
            ).mappings()
        ),
        "table_id",
    )
    indexes = _group(
        _as_dicts(
            (
                await session.execute(
                    select(*[_META_INDEX.c[name] for name in _INDEX_FIELDS])
                    .where(_META_INDEX.c.table_id.in_(ids))
                    # 主键排最前，其余按名字：顺序不稳会让同一张表两次同步产出不同文本
                    .order_by(
                        _META_INDEX.c.table_id,
                        _META_INDEX.c.is_primary.desc(),
                        _META_INDEX.c.index_name,
                    )
                )
            ).mappings()
        ),
        "table_id",
    )
    index_columns = _group(
        _as_dicts(
            (
                await session.execute(
                    select(*[_META_INDEX_COLUMN.c[name] for name in _INDEX_COLUMN_FIELDS])
                    .where(
                        _META_INDEX_COLUMN.c.index_id.in_(
                            select(_META_INDEX.c.id).where(_META_INDEX.c.table_id.in_(ids))
                        )
                    )
                    .order_by(_META_INDEX_COLUMN.c.index_id, _META_INDEX_COLUMN.c.seq_in_index)
                )
            ).mappings()
        ),
        "index_id",
    )
    target = _META_TABLE.alias("meta_table_target")
    relations = _group(
        _as_dicts(
            (
                await session.execute(
                    select(
                        _META_RELATION.c.from_table_id,
                        _META_RELATION.c.from_column_name,
                        _META_RELATION.c.source_kind,
                        _META_RELATION.c.confidence,
                        _META_RELATION.c.to_column_name,
                        target.c.catalog_name.label("to_catalog_name"),
                        target.c.schema_name.label("to_schema_name"),
                        target.c.table_name.label("to_table_name"),
                    )
                    .join(target, target.c.id == _META_RELATION.c.to_table_id)
                    .where(_META_RELATION.c.from_table_id.in_(ids))
                    .order_by(
                        _META_RELATION.c.from_table_id,
                        _META_RELATION.c.source_kind,
                        _META_RELATION.c.from_column_name,
                        target.c.table_name,
                        _META_RELATION.c.to_column_name,
                    )
                )
            ).mappings()
        ),
        "from_table_id",
    )

    cards = 0
    failures: list[CardBuildFailure] = []
    for table in tables:
        full_name = _full_name(
            str(table["catalog_name"]), str(table["schema_name"]), str(table["table_name"])
        )
        try:
            if card_build_hook is not None:
                card_build_hook(full_name)
            meta = table_meta_from_rows(
                dialect_name=dialect_name,
                server_version=server_version,
                table=table,
                columns=columns.get(table["id"], []),
                indexes=indexes.get(table["id"], []),
                index_columns=[
                    child
                    for idx in indexes.get(table["id"], [])
                    for child in index_columns.get(idx["id"], [])
                ],
                relations=relations.get(table["id"], []),
            )
            rows = [
                {
                    "datasource_id": datasource_id,
                    "kind": doc.kind,
                    "table_id": table["id"],
                    "seq": doc.seq,
                    "index_profile_id": profile_id,
                    "doc_uid": card_doc_uid(
                        doc.kind, str(table["table_uid"]), seq=doc.seq, index_profile_id=profile_id
                    ),
                    "title": doc.title,
                    "text_md": doc.text_md,
                    "search_text": doc.search_text,
                    "token_count": doc.token_count,
                    "meta": doc.meta,
                    "sync_job_id": job_id,
                }
                for doc in build_cards(meta)
            ]
            if rows:
                await session.execute(kb_card_upsert(), rows)
            await session.commit()
            cards += len(rows)
        except Exception as exc:
            # 一表一事务的收口：回滚只作废**这一张**（前面已 commit 的卡片不在射程内），
            # 而驱动/语句级的异常在这里必须拦下——放行就是把整轮卡片构建按在一颗雷上。
            await session.rollback()
            failures.append(CardBuildFailure(table_full_name=full_name, error=exc))
    return CardBuildOutcome(cards=cards, failures=tuple(failures))


# ============================================================ 读取路径
#
# 接口层（`GET /api/kb/cards?table_uid=`）用的一条查询。单独一节是因为它的关注点和写入
# 那半截正交：写入管"文本怎么造出来"，这里只管"造出来的段按表取回、并且当场判权"。

# 显式列而不是 `select(KbCard)`：整行会把 `embedding`（1536 个浮点）和 `search_text`
# 一起捞进内存再丢掉，而 DTO 白名单是唯一那道"以后加列不自动漏出去"的门。
_CARD_READ_FIELDS: Final = (
    "id",
    "kind",
    "seq",
    "doc_uid",
    "title",
    "text_md",
    "token_count",
    "meta",
    "embedded_at",
    "index_profile_id",
)
_PROFILE_READ_FIELDS: Final = ("id", "name", "model", "dimensions", "card_template_version")


async def cards_of_table(session: AsyncSession, *, actor: User, table_uid: str) -> list[KbCardOut]:
    """按 `table_uid` 取回一张表的全部卡片段（主卡在前，`seq` 升序）。

    权限走 `datasource_service.get_authorized`：§7 给 `/kb/cards` 的是 **read** 权，
    不是 owner 权——开了 `allow_global_access` 的源，别人的账号也读得到卡片。
    uid 是对外标识、不是能力凭证，所以要先找到表再判权，不能拿 uid 直接捞卡片。

    表存在但一张卡都没有时回空列表而不是 404：那是"同步跑过了、卡片还没建"的真实中间态
    （工单 008 修正 ④ 的现状），UI 要据此提示去点同步，而不是说这张表不存在。
    """
    target = (
        await session.execute(
            select(_META_TABLE.c.id, _META_TABLE.c.datasource_id).where(
                _META_TABLE.c.table_uid == table_uid
            )
        )
    ).first()
    if target is None:
        raise NotFound(f"表 {table_uid} 不存在（从未同步过？）")
    table_id, datasource_id = int(target[0]), int(target[1])
    await datasource_service.get_authorized(session, actor, datasource_id)

    rows = list(
        (
            await session.execute(
                select(*[_CARD.c[name] for name in _CARD_READ_FIELDS])
                .where(_CARD.c.table_id == table_id)
                # 段号而不是 id：identity 是按插入顺序长的，重排一轮 profile 后 id 顺序会变，
                # 而"主卡在前"是 §6 的语义，不是"最早插入的在前"
                .order_by(_CARD.c.seq)
            )
        ).mappings()
    )
    if not rows:
        return []
    # 一张表的段通常共用一个 profile，所以第二条查询只捞这几个 id
    profile_ids = {int(row["index_profile_id"]) for row in rows}
    profiles = {
        int(row["id"]): row
        for row in (
            await session.execute(
                select(*[_PROFILE.c[name] for name in _PROFILE_READ_FIELDS]).where(
                    _PROFILE.c.id.in_(profile_ids)
                )
            )
        ).mappings()
    }
    return [
        KbCardOut(
            id=int(card["id"]),
            kind=str(card["kind"]),
            seq=int(card["seq"]),
            doc_uid=str(card["doc_uid"]),
            title=card["title"],
            text_md=str(card["text_md"]),
            token_count=int(card["token_count"]),
            meta=dict(card["meta"]),
            embedded_at=card["embedded_at"],
            index_profile=KbCardProfileOut(**dict(profiles[int(card["index_profile_id"])])),
        )
        for card in rows
    ]
