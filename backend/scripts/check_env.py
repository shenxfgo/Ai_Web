"""开机体检：把"能不能跑起来"的前置条件一次打清楚，比等第一个 500 便宜。

用法：uv run python scripts/check_env.py
退出码非 0 表示有 fatal 项，先修再跑迁移。
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

if sys.platform == "win32":  # asyncpg 与 Proactor 事件循环不兼容
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.logging import reconfigure_std_streams  # noqa: E402
from app.settings import Settings, get_settings  # noqa: E402

Status = Literal["ok", "warn", "fail"]
_MARK = {"ok": "[ ok ]", "warn": "[warn]", "fail": "[FAIL]"}

# 体检报告是给人照着修的，驱动原文（英文 + 内部异常名）帮不上忙。
# 键取异常类名片段而不是类型本身：脚本不该为了报错文案去 import 驱动。
_PG_ERROR_HINTS: tuple[tuple[str, str, str], ...] = (
    (
        "InvalidPassword",
        "口令不对",
        "核对 AIWEB_PG__PASSWORD；应用账号口令由开通脚本生成，改过就要同步 .env",
    ),
    ("InvalidCatalogName", "库不存在", "AIWEB_PG__DATABASE 写错，或开通脚本没跑"),
    ("InvalidAuthorizationSpec", "角色不存在", "AIWEB_PG__USER 不是该实例上的角色"),
    ("InsufficientPrivilege", "账号权限不够", "元数据库要由该账号 own（要能 CREATE SCHEMA）"),
    ("Timeout", "连接超时", "核对 AIWEB_PG__HOST/PORT，或调大 AIWEB_PG__CONNECT_TIMEOUT_S"),
    ("refused", "端口没人监听", "PG 服务未启动，或 host/port 写错"),
    ("getaddrinfo", "主机名解析不了", "核对 AIWEB_PG__HOST"),
)


def _pg_failure(exc: Exception) -> Check:
    """把驱动异常翻成中文，并保留原文——只给结论不给线索，人就没办法自查。"""
    raw = f"{type(exc).__name__}: {exc}"
    text_blob = raw.lower()
    for needle, message, hint in _PG_ERROR_HINTS:
        if needle.lower() in text_blob:
            return Check("pg.connect", "fail", f"{message}（原文：{raw[:90]}）", hint)
    return Check("pg.connect", "fail", raw[:120], "看原文里的 SQLSTATE/异常名定位")


@dataclass(slots=True)
class Check:
    name: str
    status: Status
    message: str
    hint: str = ""

    def render(self) -> str:
        line = f"{_MARK[self.status]:6} {self.name:28} {self.message}"
        return line + (f"\n        ↳ {self.hint}" if self.hint and self.status != "ok" else "")


def _version_tuple(raw: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in raw.replace("PostgreSQL ", "").split("+")[0].split(".")[:3]:
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


def check_embedding_gate(settings: Settings, checks: list[Check], exts: dict[str, str]) -> None:
    """`vector` 扩展的判据是 **`DIMENSION>0`**，不是"启用向量路"（roadmap §5 第 2 行 as-built）。

    0005 起 `AIWEB_EMBEDDING__DIMENSION` 兼任 `kb_card.embedding` 的列宽：列类型 `vector(dim)`
    引用这个扩展，端点配没配都一样。所以两种坏情况要分开报——
    维度为 0 是**配置**问题（迁移第一步就拒绝），扩展缺失是**库**问题（要 DBA 出面）。
    """
    dim = settings.embedding.dimension
    if dim <= 0:
        checks.append(
            Check(
                "embedding.dimension",
                "fail",
                f"AIWEB_EMBEDDING__DIMENSION={dim}",
                "0005 起 AIWEB_EMBEDDING__DIMENSION 是 kb_card.embedding 的列宽，必须 >0"
                "（text-embedding-3-small 是 1536）。未启用向量端点也要填。",
            )
        )
        return
    has_vector = "vector" in exts
    checks.append(
        Check(
            "pg.ext.vector",
            "ok" if has_vector else "fail",
            exts.get("vector", "未安装"),
            # 0.8.6 的 vector.control 没有 trusted，应用账号自己建不了，只能 DBA 预建
            "请超级用户/DBA 执行 CREATE EXTENSION vector;（0.4+ 才支持 HNSW），再重跑迁移",
        )
    )


async def check_pg(settings: Settings, checks: list[Check]) -> None:
    pg = settings.pg
    min_pg = tuple(int(x) for x in settings.extract.min_pg_version.split("."))
    if not pg.configured:
        checks.append(
            Check("pg.configured", "warn", "未配置 AIWEB_PG__*", "填 backend/.env 后重跑")
        )
        return
    from app.core.db import create_engine

    engine = create_engine(pool_size=1, max_overflow=0)
    try:
        async with engine.connect() as conn:
            version = str((await conn.execute(text("select version()"))).scalar_one())
            ok = _version_tuple(version) >= min_pg
            checks.append(
                Check(
                    "pg.connect",
                    "ok" if ok else "fail",
                    version[:60],
                    f"要求 >= {min_pg[0]}.{min_pg[1] if len(min_pg) > 1 else 0}",
                )
            )
            exts = dict(
                (await conn.execute(text("select extname, extversion from pg_extension"))).all()
            )
            checks.append(
                Check(
                    "pg.ext.pg_trgm",
                    "ok" if "pg_trgm" in exts else "warn",
                    exts.get("pg_trgm", "未安装"),
                    "pg_trgm 是 trusted 扩展：CREATE EXTENSION IF NOT EXISTS pg_trgm;",
                )
            )
            check_embedding_gate(settings, checks, exts)
            schema_exists = (
                await conn.execute(
                    text("select to_regclass(:probe) is not null"),
                    {"probe": f"{pg.schema_name}.alembic_version"},
                )
            ).scalar_one()
            can_create = bool(
                (
                    await conn.execute(
                        text("select has_database_privilege(current_user, :db, 'CREATE')"),
                        {"db": pg.database},
                    )
                ).scalar_one()
            )
            state = "已建表" if schema_exists else ("可建" if can_create else "无权限")
            checks.append(
                Check(
                    "pg.schema",
                    "ok" if schema_exists or can_create else "fail",
                    f"{pg.schema_name} {state}",
                    f"CREATE SCHEMA {pg.schema_name} AUTHORIZATION {pg.user};",
                )
            )
    except Exception as exc:
        checks.append(_pg_failure(exc))
    finally:
        await engine.dispose()


def check_llm(settings: Settings, checks: list[Check]) -> None:
    llm = settings.llm
    if not llm.configured:
        checks.append(
            Check("llm.configured", "warn", "未配置", "P2 起需要真实端点；之前用 respx mock")
        )
        return
    url = f"{llm.base_url.rstrip('/')}/models"
    try:
        resp = httpx.get(url, headers={"authorization": f"Bearer {llm.api_key}"}, timeout=15)
        if resp.status_code != 200:
            checks.append(Check("llm.probe", "fail", f"HTTP {resp.status_code} @ {url}"))
            return
        ids = [m.get("id", "") for m in resp.json().get("data", [])]
        found = llm.model in ids or not ids
        checks.append(
            Check(
                "llm.probe",
                "ok" if found else "warn",
                f"{llm.model}" + (f"（共 {len(ids)} 个模型可选）" if ids else ""),
                "model 不在端点返回列表里：" + ", ".join(ids[:8]),
            )
        )
    except Exception as exc:
        checks.append(Check("llm.probe", "fail", f"{type(exc).__name__}: {exc}"[:120]))


def check_embedding(settings: Settings, checks: list[Check]) -> None:
    emb = settings.embedding
    if not emb.configured:
        checks.append(Check("embedding", "ok", "未启用，检索走结构化路径"))
        return
    url = f"{emb.base_url.rstrip('/')}/embeddings"
    try:
        resp = httpx.post(
            url,
            headers={"authorization": f"Bearer {emb.api_key}"},
            json={"model": emb.model, "input": ["ping"]},
            timeout=30,
        )
        resp.raise_for_status()
        actual = len(resp.json()["data"][0]["embedding"])
        if actual != emb.dimension:
            checks.append(
                Check(
                    "embedding.dimension",
                    "fail",
                    f"端点返回 {actual} 维，配置是 {emb.dimension} 维",
                    "维度是列类型的一部分，建卡后不能 ALTER",
                )
            )
        else:
            checks.append(Check("embedding.dimension", "ok", f"{actual} 维（HNSW 上限 2000）"))
    except Exception as exc:
        checks.append(Check("embedding.probe", "fail", f"{type(exc).__name__}: {exc}"[:120]))


def check_secrets(settings: Settings, checks: list[Check]) -> None:
    prod = settings.app.environment == "prod"
    checks.append(
        Check(
            "jwt.secret",
            "fail"
            if prod and settings.jwt.using_default_secret
            else "warn"
            if settings.jwt.using_default_secret
            else "ok",
            "使用默认值或过短" if settings.jwt.using_default_secret else "已自定义",
            'python -c "import secrets;print(secrets.token_urlsafe(48))"',
        )
    )
    keys = settings.fernet.key_list
    if not keys:
        checks.append(
            Check("fernet.keys", "warn" if not prod else "fail", "未配置", "数据源口令无法加密落库")
        )
        return
    try:
        from cryptography.fernet import Fernet

        for key in keys:
            Fernet(key)
        checks.append(Check("fernet.keys", "ok", f"{len(keys)} 把，可轮换"))
    except Exception as exc:
        checks.append(
            Check(
                "fernet.keys",
                "fail",
                f"{type(exc).__name__}: {exc}"[:100],
                'python -c "from cryptography.fernet import Fernet;'
                'print(Fernet.generate_key().decode())"',
            )
        )


def check_dirs(settings: Settings, checks: list[Check]) -> None:
    """可写性检查直接用 Settings 给出的绝对路径。

    基准不在这里判：012 之前这里是 `BACKEND_ROOT.parent / path`，而执行器拿的是原样的相对
    `Path`（跟着 cwd 走），于是"体检说可写"和"文件落在哪"是两个目录（safety §7 的 as-built）。
    现在两处读同一个已解析的值，报出来的路径就是真会写进去的那个。
    """
    for label, resolved in (
        ("result.dir", settings.result.dir),
        ("kb_docs.dir", settings.kb_docs.dir),
    ):
        try:
            resolved.mkdir(parents=True, exist_ok=True)
            probe = resolved / ".aiweb_write_probe"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            checks.append(Check(label, "ok", str(resolved)))
        except Exception as exc:
            checks.append(Check(label, "fail", f"{resolved}: {exc}"[:120]))


def check_sql_guard(checks: list[Check]) -> None:
    """守卫自测（roadmap §5 项 13，定级 warn）：sqlglot 的解析行为是整条只读链的地基。"""
    try:
        import sqlglot

        from app.services import sql_guard

        passed = sql_guard.guard("SELECT 1", allowed=frozenset())
        if not passed.ok or "LIMIT" not in (passed.sql_final or "").upper():
            checks.append(
                Check(
                    "sql_guard.smoke",
                    "warn",
                    f"SELECT 1 未通过：{passed.violations}",
                    "先跑 pytest tests/guard",
                )
            )
            return
        rejected = sql_guard.guard("SELECT /*!50100 1 */", allowed=frozenset())
        code = rejected.violations[0].code if rejected.violations else None
        if rejected.ok or code != "executable_comment":
            checks.append(
                Check(
                    "sql_guard.smoke",
                    "warn",
                    f"可执行注释未被拒（ok={rejected.ok}，归因={code}）",
                    "第一层防御已失效，别接执行链路",
                )
            )
            return
        checks.append(
            Check(
                "sql_guard.smoke",
                "ok",
                f"sqlglot {sqlglot.__version__}，强制 LIMIT 已注入（{passed.sql_final}）",
            )
        )
    except Exception as exc:
        # 体检要把任何异常降级成一行，不能炸栈
        msg = f"{type(exc).__name__}: {exc}"[:120]
        checks.append(Check("sql_guard.smoke", "warn", msg, "uv sync 是否完成"))


def check_config_shape(settings: Settings, checks: list[Check]) -> None:
    env_file = BACKEND_ROOT / ".env"
    checks.append(
        Check(
            "env.file",
            "ok" if env_file.exists() else "warn",
            "backend/.env 已就位" if env_file.exists() else "只有 .env.example",
            "copy backend\\.env.example backend\\.env",
        )
    )
    if settings.query.row_limit > 5000:
        checks.append(
            Check(
                "query.row_limit",
                "warn",
                f"{settings.query.row_limit} 行偏大",
                "结果集落文件也吃磁盘",
            )
        )
    if settings.extract.max_tables > 5000:
        checks.append(
            Check(
                "extract.max_tables",
                "warn",
                f"{settings.extract.max_tables} 表偏大",
                "同步时长与卡片量都会失控",
            )
        )


async def main() -> int:
    reconfigure_std_streams()
    settings = get_settings()
    checks: list[Check] = []
    check_config_shape(settings, checks)
    check_secrets(settings, checks)
    check_dirs(settings, checks)
    check_sql_guard(checks)
    await check_pg(settings, checks)
    check_llm(settings, checks)
    check_embedding(settings, checks)

    print(f"\n环境体检 · {settings.app.name} · {settings.app.environment}")
    print(f"元数据库 {settings.pg.masked_dsn()}\n")
    for check in checks:
        print(check.render())
    fatal = [c for c in checks if c.status == "fail"]
    warn = [c for c in checks if c.status == "warn"]
    passed = len(checks) - len(fatal) - len(warn)
    print(f"\n{len(checks)} 项：{passed} ok / {len(warn)} warn / {len(fatal)} fail")
    if fatal:
        print("先修 fail 项再跑 alembic upgrade head。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
