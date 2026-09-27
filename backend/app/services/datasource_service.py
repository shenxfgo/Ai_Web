"""数据源登记：CRUD + Fernet 口令 + 可见性判定 + 测试连接。

口令在这里进、在这里出明文，别处只准拿到密文或掩码。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from sqlalchemy import Select, bindparam, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    Conflict,
    Forbidden,
    NotFound,
    NotImplementedSource,
    SourceUnreachable,
)
from app.core.security import decrypt_secret, encrypt_secret
from app.models.datasource import DataSource
from app.models.user import User
from app.schemas.datasource import (
    PASSWORD_MASK,
    Access,
    ConnectionTestOut,
    DataSourceCreate,
    DataSourceOut,
)
from app.services.source_manager import create_source_engine

# 这三个不来自表的列：一个是权限判定结果，两个是口令的替身表示
_DERIVED = {"access", "password_masked", "has_secret"}


def render(row: DataSource, access: Access) -> DataSourceOut:
    """ORM → DTO。按 DataSourceOut 的字段名逐个取值，不是把 ORM 整体倒出来。

    方向是刻意的：以后给表加一列不会自动出现在响应里，想露脸必须先进输出模型——
    那一步才会被"口令不落响应体"的用例拦住。列名写错则当场 AttributeError，不会静默少字段。
    """
    fields: dict[str, Any] = {
        name: getattr(row, name) for name in DataSourceOut.model_fields if name not in _DERIVED
    }
    return DataSourceOut(**fields, access=access, password_masked=PASSWORD_MASK, has_secret=True)


def access_of(row: DataSource, actor: User) -> Access | None:
    """这个人对这个源是什么档；None 表示压根不该看见它（列表里不出现、详情 403）。

    "谁能看什么"只写在这一处，列表和详情都调它。同一套规则分两处表达迟早漂移，
    而漂移的后果恰好是"列表里看不见，但拿着 id 能读到"。

    ADR-0008 定的粒度就是数据源级。'granted' 档要等 datasource_grants（metadata-model
    §2.3）落地才可能返回，那张表目前没有工单认领；在此之前非 owner 只能靠
    allow_global_access 看见。admin 恒为 'owner'：他要管授权，UI 上需要的能力和 owner
    一模一样，多一档只会让前端多一个分支。
    """
    if actor.role == "admin" or row.created_by == actor.id:
        return "owner"
    if row.allow_global_access:
        return "global"
    return None


async def get_authorized(
    session: AsyncSession, actor: User, ds_id: int
) -> tuple[DataSource, Access]:
    """按 id 取，并且当场判权。

    软删过的算不存在（404）：它已经不在任何列表里了，这时候回 403 等于替调用者确认
    "这个 id 下曾经有过一个源"。
    """
    row = await session.get(DataSource, ds_id)
    if row is None or row.deleted_at is not None:
        raise NotFound(f"数据源 {ds_id} 不存在")
    tier = access_of(row, actor)
    if tier is None:
        raise Forbidden("无权访问该数据源")
    return row, tier


async def get_owned(session: AsyncSession, actor: User, ds_id: int) -> DataSource:
    """读权限之上再要 owner 档：DELETE 与 test 这两个写/连的入口共用这一句判定。

    owner 规则如果分两个端点各写一遍，'granted' 档落地那天必然只改到其中一处。
    """
    row, tier = await get_authorized(session, actor, ds_id)
    if tier != "owner":
        raise Forbidden("只有数据源的 owner 或管理员能做这件事")
    return row


async def list_visible(session: AsyncSession, actor: User) -> list[tuple[DataSource, Access]]:
    """捞全表再在内存里判权，不在 SQL 里把 access_of 重抄一遍 WHERE。

    data_sources 是个位数到几十行的表；那样写换来的只有"两处规则可以不一致"这个长期风险。
    """
    stmt: Select[tuple[DataSource]] = (
        select(DataSource).where(DataSource.deleted_at.is_(None)).order_by(DataSource.id)
    )
    rows = (await session.execute(stmt)).scalars()
    return [(row, tier) for row in rows if (tier := access_of(row, actor)) is not None]


async def create(
    session: AsyncSession, *, actor: User, payload: DataSourceCreate
) -> tuple[DataSource, Access]:
    """明文口令在这一行被 encrypt_secret 吃掉，之后任何路径都只见密文或掩码。"""
    row = DataSource(
        name=payload.name,
        kind=payload.kind,
        host=payload.host,
        port=payload.port,
        catalog_name=payload.catalog_name,
        connect_user=payload.connect_user,
        secret_enc=encrypt_secret(payload.connect_password),
        params=payload.params,
        include_schemas=payload.include_schemas,
        include_tables=payload.include_tables,
        exclude_tables=payload.exclude_tables,
        row_limit=payload.row_limit,
        timeout_ms=payload.timeout_ms,
        allow_global_access=payload.allow_global_access,
        created_by=actor.id,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        # 先探测再插入的话，两个请求同时进来仍会有一个撞在 UNIQUE 上——所以以库的判决为准。
        # 23505 是"撞了某个唯一约束"，这里只可能是 name（uq_data_sources_name）：
        # created_by 走的是 FK，那是另一个 sqlstate 23503，不在这里冒充。
        await session.rollback()
        if getattr(exc.orig, "sqlstate", None) == "23505":
            raise Conflict(f"数据源名称 {payload.name!r} 已被占用") from exc
        raise
    await session.refresh(row)
    # 建源人必然命中 access_of 的 owner 分支；那个 `or` 只是给类型层收口，不是第二处判权
    return row, access_of(row, actor) or "owner"


async def soft_delete(session: AsyncSession, row: DataSource) -> None:
    """只盖 deleted_at，不删行。

    物理删的代价不在那一行本身，而在连带：meta_* 六张快照表对 data_sources 是
    ON DELETE CASCADE（metadata-model §2.4），一次误删会连已建好的知识库一起清掉。
    """
    row.deleted_at = datetime.now(UTC)
    await session.commit()


# ------------------------------------------------------------------ 测试连接

# MySQL 的 information_schema.schemata 会把系统库也列出来；它们不是给用户"探规模"的对象。
# 叫 DATABASES 不叫 SCHEMAS：这几颗是 MySQL 的**库**，而 CONTEXT.md 禁止裸用"schema"一词。
_SYSTEM_DATABASES = frozenset({"information_schema", "mysql", "performance_schema", "sys"})
# 出现任意一个就不再是只读账号。判定按"整词"匹配（两侧空格或尾随逗号），所以库名叫
# `ai_web_create_demo` 不会被误判成持有 CREATE——误判的代价是把可用账号报成不安全，
# 用户下次就把警告一起忽略掉。
_PRIVILEGE_ALARM = (
    "all",
    "insert",
    "update",
    "delete",
    "create",
    "drop",
    "alter",
    "index",
    "execute",
    "references",
    "grant option",
    "super",
    "file",
    "trigger",
    "event",
    "shutdown",
    "reload",
    "process",
    "replication slave",
    "replication client",
)
# 权限词之外还要看范围：`GRANT SELECT ON *.*` 里没有写权限词，但它能读 mysql.user
_ALL_SCOPE = re.compile(r"\bon\s+\*\.\*\s+to\b", re.IGNORECASE)
_USAGE_ON_ALL = re.compile(r"^grant\s+usage\s+on\s+\*\.\*", re.IGNORECASE)


def grants_verdict(grant_lines: list[str]) -> dict[str, Any]:
    """`SHOW GRANTS` 的原文 → {read_only, code, warnings}。

    判"只读"不能靠 readonly_enforced 那个自报家门列：它是用户在表单里勾的，
    源库账号真实有什么权限只有源库知道。这条检测按 nl2sql-safety §4.1 的说法属于
    **登记阶段的前置校验**，不在三层防御之内（第一层 AST 白名单、第二层 EXPLAIN dry-run、
    第三层会话级只读）——§6 也要求"数据源表单提示请建只读账号"。

    `code` 是 §4.1 点名要 test_connection 报的那个机读标记
    （`readonly_capability_missing`），但它**不是** HTTP 错误：同一条 §4.1 写着
    "黄色警告不阻断，admin 可确认"。所以这里回 200 + 一个可 switch 的码，
    阻断留给 011 的执行前检查。
    """
    warnings: list[str] = []
    for line in grant_lines:
        lowered = f" {line.lower()} "
        hit = [k for k in _PRIVILEGE_ALARM if f" {k} " in lowered or f"{k}," in lowered]
        if not hit and _ALL_SCOPE.search(line) and not _USAGE_ON_ALL.match(line):
            # USAGE ON *.* 是 002 给的那条"账号能登录但不带任何库表权限"，不算越界
            hit = ["ON *.* 的全库范围"]
        if hit:
            warnings.append(f"账号持有 {', '.join(hit)}：不是只读账号（{line}）")
    return {
        "read_only": not warnings,
        "code": None if not warnings else "readonly_capability_missing",
        "warnings": warnings,
    }


def root_cause(exc: BaseException) -> BaseException:
    """SQLAlchemy 会把 DBAPI 异常包一层，错误号在被包的那个身上。

    SQLAlchemyError 本身没有 .args[0]=errno，直接读会永远拿到 None，然后所有源库错误
    都归到"未知错误"——错误映射看着写了其实一条都没命中过。
    """
    orig = getattr(exc, "orig", None)
    return orig if isinstance(orig, BaseException) else exc


# asyncmy 实际会抛的连通类错误号是 2003/2006/2013（CR_CONN_HOST_ERROR / CR_SERVER_GONE_ERROR
# / CR_SERVER_LOST，见其 connection.pyx）；2002/2005 属于 libmysqlclient，这里没有它们。
_ERROR_HINTS: dict[int, str] = {
    1045: "connect_user 或 connect_password 不对（源库拒绝：1045）",
    1049: "库不存在或该账号看不见它（源库：1049）",
    2003: "host/port 连不上：地址或端口不对、源库没监听、或被防火墙丢包（源库：2003）",
    2006: "连上之后源库断开了：源库重启、连接被掐或超时（源库：2006）",
    2013: "查询中途连接丢失：多半是 timeout_ms 太短或源库侧被砍断（源库：2013）",
}


def describe_source_error(exc: BaseException) -> str:
    """把驱动的错误号翻成"该改哪一格"。

    原样抛栈有三个坏处：变 500、异常链里带连接串、用户不知道该改 host 还是改口令。
    这里只回我们自己拼的文案，不回驱动原文。
    """
    args = getattr(exc, "args", ())
    errno = args[0] if args and isinstance(args[0], int) else None
    if errno is None:
        # args[0] 不是整数（asyncmy 有 `errors.Error("Already closed")` 这类抛点）时，
        # 拼进文案就等于把驱动原文回给用户，所以只回固定一句
        return "源库返回了无法识别的错误，请检查连接参数"
    return _ERROR_HINTS.get(errno, f"源库返回错误（错误号 {errno}）")


def table_scope_filter(row: DataSource) -> tuple[str, dict[str, str]]:
    r"""include_tables / exclude_tables → (SQL 片段, 绑定参数)。

    片段里的 `table_name` 是裸列名，只在外层那条 `from information_schema.tables` 的查询
    里没有别名时才成立；007 同步侧复用它之前，先确认外层形状一致。

    口径是**源原生 LIKE**，不是正则：metadata-model §2.2 那两列写的是"正则/通配"，而两边
    都不支持正则（MySQL 5.7 的 information_schema 查询里没有 regexp 算子，PG 得换 ~ 运算符），
    选 LIKE 才能让同一段条件在两种源上都成立。`%` 和 `_` 因此是通配符，`\_` 是字面下划线。
    这个歧义已经记进工单 006 的偏差清单。另注意口径依赖源会话的默认 LIKE 转义符：
    MySQL 开了 `sql_mode=NO_BACKSLASH_ESCAPES` 时 `\_%` 变成字面反斜杠前缀，范围会静默归零。

    表名是用户填的，一律走绑定参数：元数据这条路不经过 SQL 守卫（003 只管问数）。
    """

    def clean(patterns: list[str] | None) -> list[str]:
        return [p for p in patterns or [] if p and p.strip()]

    parts: list[str] = []
    params: dict[str, str] = {}
    for field, names, joiner in (
        ("inc", clean(row.include_tables), " OR "),
        ("exc", clean(row.exclude_tables), " AND "),
    ):
        if not names:
            continue
        op = "LIKE" if field == "inc" else "NOT LIKE"
        conds = []
        for i, pattern in enumerate(names):
            key = f"{field}_{i}"
            params[key] = pattern
            conds.append(f"table_name {op} :{key}")
        parts.append("(" + joiner.join(conds) + ")")
    return " AND ".join(parts), params


# include_schemas 为空 = 自动发现全部非系统库；真一个都没有时用这个哨兵，
# 因为 `in ()` 是语法错误，而传空列表会被 expanding bindparam 展开成它
_NO_TARGET_DATABASE = "__none__"


async def test_connection(
    session: AsyncSession, row: DataSource, *, temporary_password: str | None = None
) -> ConnectionTestOut:
    """§7 的 test 端点：连一次源库报规模，并把探到的版本号写回那一列。

    口令的取用规则也在这里（而不是端点）：临时口令优先，否则解库里那份。端点负责 HTTP，
    不该同时知道"哪份口令生效"和"明文从密文里怎么出来"。
    """
    password = temporary_password or decrypt_secret(row.secret_enc)
    result = await _connect_and_describe(row, password)
    # metadata-model §2.2：server_version 这一列存在的唯一理由就是"test_connection 时探测写入"
    row.server_version = result.server_version
    await session.commit()
    return result


async def _connect_and_describe(row: DataSource, password: str) -> ConnectionTestOut:
    """真连一次源库并回一份 ConnectionTestOut。一次性 NullPool 连接，不进任何缓存。"""
    if row.kind != "mysql":
        raise NotImplementedSource(f"kind={row.kind} 的连接探测尚未实现")
    started = monotonic()
    engine = create_source_engine(row, password, timeout_ms=row.timeout_ms)
    scope_sql, scope_params = table_scope_filter(row)
    try:
        async with engine.connect() as conn:
            version = (await conn.execute(text("select version()"))).scalar_one()
            visible = list(
                (
                    await conn.execute(text("select schema_name from information_schema.schemata"))
                ).scalars()
            )
            databases = row.include_schemas or [s for s in visible if s not in _SYSTEM_DATABASES]
            counts = (
                await conn.execute(
                    text(
                        "select table_type, count(*) from information_schema.tables "
                        "where table_schema in :ts"
                        # scope_sql 只含 :inc_n / :exc_n 占位符，用户填的表名一律走绑定参数
                        + (f" and {scope_sql}" if scope_sql else "")
                        + " group by table_type"
                    ).bindparams(bindparam("ts", expanding=True)),
                    {"ts": databases or [_NO_TARGET_DATABASE], **scope_params},
                )
            ).all()
            lines = list((await conn.execute(text("show grants"))).scalars())
            # max_execution_time 是 5.7.8 才有的会话变量；不支持时源库报 1193，
            # 这决定了 011 的超时是"源库兜底"还是只能靠应用侧砍断
            try:
                await conn.execute(text("select @@max_execution_time"))
                supports_timeout = True
            except DBAPIError:
                supports_timeout = False
    except SQLAlchemyError as exc:
        raise SourceUnreachable(describe_source_error(root_cause(exc))) from exc
    finally:
        await engine.dispose()

    by_type = {str(t).upper(): int(c) for t, c in counts}
    tables = by_type.get("BASE TABLE", 0)
    views = by_type.get("VIEW", 0)
    return ConnectionTestOut(
        ok=True,
        server_version=str(version),
        visible_schemas=visible,
        est_table_count=tables + views,
        table_count=tables,
        view_count=views,
        grants=grants_verdict(list(lines)),
        supports_max_execution_time=supports_timeout,
        latency_ms=round((monotonic() - started) * 1000),
    )
