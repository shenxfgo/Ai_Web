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
from sqlalchemy import Table, bindparam, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import MappingResult, RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    NotFound,
    ReindexEmbeddingMissing,
    ReindexMatchesActive,
    VectorColumnMissing,
)
from app.core.vector_columns import ALL_VECTOR_COLUMNS, vector_column_for
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
from app.services.embedding_client import EMBEDDING_PROVIDER
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


# 档案身份的那三格（§2.6 的 name 就由它们拼成）。ADR-0013 之后它是**比对单位**：
# "配置里这一套" 与 "库里 active 那一套" 是不是同一套，只看这三格。
Triplet = tuple[str, int, int]

# 逐格报出来时的名字，顺序就是三元组的顺序（模型 → 维度 → 卡片模板版本）。
# 顺序写死在常量上：这条文案会进 `sync_jobs.warnings` 与事件流，两次同步要能逐字对比。
_TRIPLET_LABELS: Final = ("模型", "维度", "卡片模板版本")


def configured_triplet() -> Triplet:
    """settings 那一侧的三元组——"这台机器现在打算用哪套档案"。"""
    settings = get_settings()
    return (
        settings.embedding.model,
        settings.embedding.dimension,
        settings.extract.card_template_version,
    )


def triplet_drift(*, configured: Triplet, active: Triplet) -> list[str]:
    """逐格比对配置与 active 档案，返回"哪几格对不上"的说明；全一致返回空表。

    空表是同步链路上唯一"这轮可以写卡"的信号（工单 027：任何一格不一致都不写卡）。
    每条文案必须同时点出**两个值**（配置的与档案的）：只说"不一致"的告警等于让运维再跑一次
    SQL 去查是哪个配置改歪了，而 spec 故事 9 要的是当场看得出来。

    逐格报而不是一条汇总：三格里错一格和错三格是两种不同的处置（改 `.env` 一个键 vs 装错端点），
    合成一句话就分不出来了。
    """
    return [
        f"{label}：配置为 {cfg_value}，当前生效档案为 {active_value}"
        for label, cfg_value, active_value in zip(_TRIPLET_LABELS, configured, active, strict=True)
        if cfg_value != active_value
    ]


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
    ① **全部向量列**与 `embedded_at` 被显式清成 NULL——卡片文本变了，旧向量就不再描述它了
       （§5 的"编辑后触发该表 embedding 重算"是同一条语义）。留着不洗是最坏的选择，
       它会让检索拿旧向量匹配新内容，而且从结果里看不出过期。
       清单从 `core/vector_columns.py` 那份映射投影（`ALL_VECTOR_COLUMNS`）而不是在这里点名：
       自 0008 起这根柱子有两根向量列，**只洗第一列等于对第二列说"过期也算有效"**——
       而 029 之前第二列从没有写入点，那条不对称一直藏着没现形（工单 029 双轴审查·规格轴第一条）。
       列名不写在这里还有第二个理由：`test_vector_columns.py` 那条 allowlist 只让三个文件出现字面。
    ② `meta` 整值替换：卡片没有人工列（术语卡的人工性在 `term` 那一行，P2 不构建），
       所以不需要 §3 那套 COALESCE 保护。
    """
    stmt = pg_insert(_CARD)
    return stmt.on_conflict_do_update(
        index_elements=[_CARD.c.doc_uid],
        set_={
            **_card_new(stmt, _CARD_CONTENT_COLS),
            **dict.fromkeys(ALL_VECTOR_COLUMNS, literal_column("NULL")),
            "embedded_at": literal_column("NULL"),
            "updated_at": func.now(),
        },
    )


def _card_new(stmt: Any, names: tuple[str, ...]) -> dict[str, Any]:
    return {name: getattr(stmt.excluded, name) for name in names}


# 这个入口只读四列：id 用来挂卡片，其余三格就是 §2.6 的三元组。`name` 不读——它是给人看的
# 拼写，而这里的消费者要比的是那三格的值；把 name 拿来比等于间接依赖它的拼法。
_ACTIVE_PROFILE_FIELDS: Final = ("id", "model", "dimensions", "card_template_version")


@dataclass(frozen=True, slots=True)
class ActiveProfile:
    """库里"当前生效的那一套"档案：一行 `kb_index_profile` 的 id 加它的三元组。

    三元组住在对象上而不是摊成三个参数：比对（`triplet_drift`）和使用（卡片挂 id）读的是
    同一份，拆开传就会有人只往下传 id，把"这套档案是哪三格"留在身后。
    """

    index_profile_id: int
    model: str
    dimensions: int
    card_template_version: int

    @property
    def triplet(self) -> Triplet:
        return (self.model, self.dimensions, self.card_template_version)


async def read_active_profile(session: AsyncSession) -> ActiveProfile | None:
    """全仓唯一把 `status='active'` 翻成"该用哪套档案"的地方（ADR-0013 / 工单 027）。

    读不到就回 `None`，**不建**：建档案的权力在 029 的 reindex 手上，同步链路只读。
    工单 027 删掉的正是那条后门（原 `kb_service.ensure_profile()`：按 settings 算一套、库里
    没有就建一套）——spec §1 问题 3 的"改一行 `.env` 再跑一次普通同步，全库检索基准就绕过
    激活机制换了"，从这一步就没有入口了。

    `one_or_none()` 而不是 `limit(1)`：库里真出现两行 active 时（partial unique 之外只剩直接
    DDL 绕约束），`limit(1)` 会安静地任选一套继续跑，而两列分家那种"看起来在服务"恰恰是最难
    查的状态。让它当场 `MultipleResultsFound` 才是诚实的失败方式。
    """
    row = (
        (
            await session.execute(
                select(*[_PROFILE.c[name] for name in _ACTIVE_PROFILE_FIELDS]).where(
                    _PROFILE.c.status == "active"
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return ActiveProfile(
        index_profile_id=int(row["id"]),
        model=str(row["model"]),
        dimensions=int(row["dimensions"]),
        card_template_version=int(row["card_template_version"]),
    )


# ============================================================ 档案的出生（工单 029 / 决策 5）
#
# ADR-0013 之后这张表的 INSERT 点**只有这里**：027 关掉了"读不到就顺手建一套"那条后门，
# 于是"该用哪套基准"只剩库里 active 那一行能答，而新基准的出生必须由人显式发起。
# 这就是为什么 400/409 都在这一步判：出生一个空壳、或出生到线上那套头上，都比不出生更糟。


async def create_draft_profile(session: AsyncSession) -> int:
    """按 settings 的三元组出生（或复用）一行档案，返回它的 id。

    三条口径都在这一句里，各自有用例钉着（工单 029 验收 1/8/9）：

    1. **四键没配齐 → 400 且不建空壳**（spec 故事 16）。留一行永远不会被灌完的 draft 的后果比
       报错重：030 会看到它并诱导人去激活一套零结果的基准。
    2. **撞名复用**（拆工时补的口径）：`profile_name()` 里没有 datasource，档案是**全局**的，
       两个源同三元组各发一次 reindex 必然撞 `uq_kb_index_profile_name`。复用那一行、回同一个
       id、不建第二行、不抬 409——"两个源共用一套基准"是正常状态，不是事故。
    3. **撞到的那行是 active → 409**（撞名口径的补集）：复用它等于往线上基准上原地改向量，
       spec 故事 3 那句"重建中不影响线上检索"当场为假。

    `is_active` 不写：027 之后读写两侧都只认 `status`，那一列 031 要删。
    `provider` 读 `embedding_client.EMBEDDING_PROVIDER`，不在这再抄一遍字面（§2.6 注④）。

    **必须 commit**：worker 是另一个进程（ADR-0011），未提交的档案行在它眼里不存在，
    而它拿 `sync_jobs.reindex_profile_id` 去读的就是这一行。
    """
    settings = get_settings()
    if not settings.embedding.configured:
        raise ReindexEmbeddingMissing(
            "embedding 四键（AIWEB_EMBEDDING__BASE_URL / __API_KEY / __MODEL / __DIMENSION）"
            "没配齐，reindex 建不出灌得进向量的档案",
            detail="出路两条：把这四键配成真端点，或起闸内替身 "
            "backend/scripts/fake_embedding_server.py 并把 AIWEB_EMBEDDING__BASE_URL 指向它。",
        )
    model, dimension, tplv = configured_triplet()
    name = profile_name(model, dimension=dimension, card_template_version=tplv)

    existing = await _profile_by_name(session, name)
    if existing is not None:
        return _accept_existing_profile(existing, name=name)

    row = KbIndexProfile(
        name=name,
        provider=EMBEDDING_PROVIDER,
        model=model,
        dimensions=dimension,
        card_template_version=tplv,
        status="draft",
    )
    session.add(row)
    try:
        # id 在 flush 之后就拿下了：`commit()` 会让 ORM 对象过期，之后再读 `row.id`
        # 是一次隐式 IO（async 会话上直接 MissingGreenlet）。与 conftest 的
        # `insert_active_profile` 同写法。
        await session.flush()
        profile_id = int(row.id)
        await session.commit()
    except IntegrityError as exc:
        # 只有并发会走到这里（两个请求同三元组同时进来）：唯一索引替我们决出胜负，
        # 输的一方复用的就是赢的那一行。与 `PostgresJobQueue.enqueue` 认 `ux_sync_running`
        # 同一条纪律——只认这个约束名，FK/CHECK 之类的失败不许被说成"撞名"。
        await session.rollback()
        if "uq_kb_index_profile_name" not in str(getattr(exc, "orig", exc)):
            raise
        raced = await _profile_by_name(session, name)
        if raced is None:
            raise
        return _accept_existing_profile(raced, name=name)
    return profile_id


async def _profile_by_name(session: AsyncSession, name: str) -> RowMapping | None:
    stmt = select(_PROFILE.c.id, _PROFILE.c.status).where(_PROFILE.c.name == name)
    return (await session.execute(stmt)).mappings().one_or_none()


def _accept_existing_profile(existing: RowMapping, *, name: str) -> int:
    """撞名那一行的去向：active 拒绝，其余（draft / retired）复用。

    `status` 的词表由库的 CHECK 守住（§2.6 / 迁移 0008），所以这里只分"是不是 active"，
    不再为第四种状态写分支——真出现了，上面那句 `status == "active"` 为假、复用它的 id，
    而 030 的激活与检索两侧都会各自撞上那一态，比在这里再判一次诚实。
    """
    if str(existing["status"]) == "active":
        raise ReindexMatchesActive(
            f"要重建的这套基准与当前生效的档案是同一套（{name}）：reindex 会原地改线上检索的向量，"
            "而同一套三元组没有新基准可建",
            detail="想换基准就改 AIWEB_EMBEDDING__MODEL / __DIMENSION（档案名跟着变）；"
            "想给现有卡片补向量就跑一次普通同步（四键配齐且三元组一致时它会真写完 embed 档）。",
        )
    return int(existing["id"])


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
    index_profile_id: int,
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

    `index_profile_id` 由调用方从 `read_active_profile()` 传入——本函数**没有**建档案的权力
    （工单 027 关掉的就是这里原来的 `ensure_profile()`：按 settings 算一套、库里没有就建一套，
    于是改一行 `.env` 再跑一次普通同步就绕过了激活机制）。id 会拌进 `doc_uid`（§2.6），
    所以"换一套档案"落在库里是新一批行，旧那批原样留着，这才是故事 6 能回滚的原因。

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
                    "index_profile_id": index_profile_id,
                    "doc_uid": card_doc_uid(
                        doc.kind,
                        str(table["table_uid"]),
                        seq=doc.seq,
                        index_profile_id=index_profile_id,
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


# ============================================================ 向量的落点与灌法（工单 029）
#
# 这一节只回答两件事：**往哪一列**、**灌哪些卡**。发 HTTP 的不在这里（`embedding_client`），
# 逐表推进与事务边界也不在这里（`sync_service` 的 embed 段）——它跟 `meta_table_mark_stale()`
# 是同一族：语句在这里造好，执行与提交在编排层，测试直接编译语句钉它的形状。
#
# 为什么"往哪一列"要经过这里而不是让编排层自己拼：`vector_column_for()` 是纯映射，它抬
# `ValueError`；而 `sync_jobs.errors[]` 只分得出 `AppError`。翻译只许有一次，所以放在离库
# 最近的这一层，编出来的 `VectorTarget` 已经是"这一颗作业能用的落点"。


@dataclass(frozen=True, slots=True)
class VectorTarget:
    """一次 embed 的落点：档案那一维 + 它对应的那一列 + 该问端点哪个模型。

    三格永远一起用（写向量的语句要列名，客户端校验要宽度，请求体要 model 名），所以不拆成
    三个返回值。没有 `profile_id`：调用方本来就知道靶是哪一行，带上只是多一个可能被读错的字段。

    `model` 为什么在这而不是让编排层自己读 settings：已定口径是"reindex 用 draft 档案**自己
    的三元组**"，而三元组里 `dimensions` 走这条路之后，`model` 若还从 settings 拿，同一颗作业
    就会**一半认档案、一半认配置**——改过 `.env` 的那一轮实际问的是新模型、验的却是旧模型的宽度，
    两边恰好对上时静默写进一批来自另一个语义空间的向量（工单 029 双轴审查·规格轴）。
    """

    dimensions: int
    model: str
    column: str


def vector_target_for(*, dimensions: int, model: str) -> VectorTarget:
    """档案的维度 → 落点；没有预置列时抬可辨识的一档。`model` 原样带着走，不参与翻译。"""
    try:
        column = vector_column_for(dimensions)
    except ValueError as exc:
        raise VectorColumnMissing(str(exc)) from exc
    return VectorTarget(dimensions=dimensions, model=model, column=column)


async def read_vector_target(session: AsyncSession, *, index_profile_id: int) -> VectorTarget:
    """按 id 读那一行档案的 `dimensions` 与 `model` 并翻成落点。

    **只读这一行，不读 settings**（工单 029 已定口径"reindex 用自己的三元组"）：
    档案那两格是历史事实，`.env` 后来改成多宽、换成哪个模型都不改这颗作业该怎么问、该灌多宽。
    也不读 `status`：这里不问"这套档案能不能上线"（那是 030 的门槛），只问"它要哪一列"。

    行读不到时 `scalar_one()`/`one()` 当场抬 `NoResultFound`。调用方**不需要**为这一格写分支：
    `kb_card.index_profile_id` 是 ON DELETE RESTRICT，而这一句只在"本轮真有卡片"时才发——
    有卡就没有、也不可能有"档案已删"的那种行。
    """
    row = (
        await session.execute(
            select(_PROFILE.c.dimensions, _PROFILE.c.model).where(_PROFILE.c.id == index_profile_id)
        )
    ).one()
    return vector_target_for(dimensions=int(row.dimensions), model=str(row.model))


@dataclass(frozen=True, slots=True)
class EmbedBatch:
    """一张表的待灌卡片。`card_ids[i]` 与 `texts[i]` 一一对应，顺序就是写回的顺序。

    表全名（`db.t` 形状）跟着走：单表失败要进 `sync_jobs.errors[]`，而那里要的是一句人话
    读得出的"哪张表"（§2.8 的 `card_build_failed` 同一条口径），table_id 到了库里没人认得。
    """

    table_full_name: str
    card_ids: tuple[int, ...]
    texts: tuple[str, ...]


async def embed_batches(
    session: AsyncSession, *, index_profile_id: int, table_ids: Sequence[int]
) -> list[EmbedBatch]:
    """本轮这几张表的、挂在目标档案下的卡片，按表分成批。

    三个筛选条件各有各的理由：
    - `index_profile_id`：**只灌这套基准的卡**。同一张表在两套档案下各有一批行（§2.6 末），
      漏掉这一格就会把新向量写到旧档案的卡上，而旧档案还在被线上检索用着。
    - `table_id = ANY(本轮落成的表)`：陈旧表（源库已无实体）不参与，与 card_build 同一把尺。
    - `deleted_at IS NULL`：软删的卡不会再被检索命中，给它取向量是白烧额度。

    **不按"向量列还是 NULL"筛**：看着像省一次调用，实际是把"上一轮灌过、这一轮文本没变"
    的卡当成已完成——而 `kb_card_upsert()` 每轮都把两个向量列显式清成 NULL，这一轮真该重灌的
    卡恰好在 NULL 那一侧；靠它筛等于给未来的改道人留一条"漏灌也不红"的路。

    排序按 `(table_id, seq)`：`seq` 是同一张表的段号，卡片文本按段递增，批次内容因此与
    库里行序无关——两次跑出同一份请求体，替身才回得出同一批向量。
    """
    ids = sorted(set(table_ids))
    if not ids:
        return []
    rows = (
        await session.execute(
            select(
                _CARD.c.id,
                _CARD.c.table_id,
                _CARD.c.text_md,
                _META_TABLE.c.catalog_name,
                _META_TABLE.c.schema_name,
                _META_TABLE.c.table_name,
            )
            .join(_META_TABLE, _META_TABLE.c.id == _CARD.c.table_id)
            .where(
                _CARD.c.index_profile_id == index_profile_id,
                _CARD.c.table_id.in_(ids),
                _CARD.c.deleted_at.is_(None),
            )
            .order_by(_CARD.c.table_id, _CARD.c.seq)
        )
    ).mappings()
    grouped: dict[int, list[tuple[int, str, str]]] = {}
    for row in rows:
        table_id = int(row["table_id"])
        grouped.setdefault(table_id, []).append(
            (
                int(row["id"]),
                str(row["text_md"]),
                _full_name(
                    str(row["catalog_name"]), str(row["schema_name"]), str(row["table_name"])
                ),
            )
        )
    return [
        EmbedBatch(
            table_full_name=items[0][2],
            card_ids=tuple(card_id for card_id, _, _ in items),
            texts=tuple(text for _, text, _ in items),
        )
        for items in grouped.values()
    ]


def card_vector_update(column: str) -> Any:
    """一张卡的向量落库：`SET <档案那一维的列>=:vector, embedded_at=now(), updated_at=now()`。

    `column` 只准来自 `vector_column_for()` 那两格受控字面（`test_vector_columns.py` 的 allowlist
    钉着"除映射与列定义本身之外没有第四处落点"），**不是**用户输入。列名也不是拼进 SQL 文本的：
    这里取的是 `_CARD.c[column]` 这个**列对象**，编译时由 SQLAlchemy 按方言 quoting 输出，
    传一个不存在的列名会在属性查找上就 `KeyError`，而不是一句能跑的 SQL。
    向量和卡片 id 走绑定参数：一条 1536 维向量拼成字面量是几十 KB 的语句文本。

    写哪一列由档案的维度决定，另一列一个字都不动（ADR-0012）：同一张卡的两根向量列不在
    同一个语义空间里，覆盖另一列等于把线上检索的向量换成另一套基准的数，而库里两行都合法、
    没有任何一层会报错。这里不写第二列的名字是故意的——`test_vector_columns.py` 那条 allowlist
    要的是"只有映射那一处知道维度翻成哪个列名"，注释里多点一次名就在多点一份会过期的账。
    形状由 `test_sync_embed_sql.py` 钉。

    只写 `embedded_at`，不写 `text_md`/`search_text`：embed 档不改文本，文本没变向量才有效
    （改文本洗 NULL 的那条在 `kb_card_upsert()`，两边不许互相代劳）。
    """
    return (
        update(_CARD)
        .where(_CARD.c.id == bindparam("card_id"))
        .values(
            {
                _CARD.c[column]: bindparam("vector"),
                "embedded_at": func.now(),
                "updated_at": func.now(),
            }
        )
    )


def mark_profile_built(profile_id: int) -> Any:
    """作业终局时给目标档案盖上 `built_at`（工单 029 已定口径：success 与 partial 都盖）。

    **只有这一格**：`status` 不在射程内。draft→active 是 030 那道激活门槛的权力，同步链路
    自己改状态等于把"重建中不影响线上检索"做成"重建完自动上线"。只动这一格由
    `test_sync_embed_sql.py` 钉在语句形状上。

    执行点在 `run_sync` 的 finally 里、终局那次 `_set_phase` **之前**：`_set_phase` 末尾自己
    commit，这一句就搭同一次车。分开的两次提交会留出一刻"status='success' 而 built_at 还空着"
    （或反过来），而 030 读的就是这两格的合取。
    """
    return update(_PROFILE).where(_PROFILE.c.id == profile_id).values(built_at=func.now())


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
