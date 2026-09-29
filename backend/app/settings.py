"""唯一配置入口。L1 密钥与 L2 部署参数走 .env / 进程环境变量，L3 运行参数在数据库里覆盖。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_JWT_SECRET = "dev-insecure-jwt-secret"

# asyncpg 的 ssl 参数认这套（与 libpq 同名），别的名会在 connect 时才炸
_PG_SSL_MODES = frozenset({"", "disable", "allow", "prefer", "require", "verify-ca", "verify-full"})

# `backend/app/settings.py` 往上三层是仓库根。
REPO_ROOT = Path(__file__).resolve().parents[2]


def _under_repo_root(value: Path) -> Path:
    """目录类配置的绝对路径基准，唯一在这一处判定（safety §7 的拍板）。

    不这么做的话 `data/results` 的落点取决于进程从哪个目录启动：012 的示踪弹在 `backend/` 下跑，
    csv 就落进了 `backend/data/results/`，而体检脚本按仓库根检查的是另一个目录——"目录可写"和
    "文件写在哪"变成两句话，下载侧的 realpath 校验更是在查一个根本没被写入过的目录。
    绝对路径原样保留：把结果盘挪到大容量卷上是正当需求。
    """
    return value if value.is_absolute() else (REPO_ROOT / value).resolve()


class AppGroup(BaseModel):
    name: str = "AI 问数"
    environment: Literal["local", "staging", "prod"] = "local"
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )
    timezone: str = "Asia/Shanghai"
    request_id_header: str = "X-Request-Id"


class PgGroup(BaseModel):
    host: str = ""
    port: int = 5432
    user: str = ""
    password: str = ""
    database: str = ""
    schema_name: str = "aiweb"
    # 键名沿用 libpq 的 SSLMODE（运维熟），取值是 asyncpg 认的那一套；空 = 不传该参数
    sslmode: str = "prefer"
    connect_timeout_s: int = 10
    pool_size: int = 5
    max_overflow: int = 10
    echo: bool = False

    @field_validator("sslmode")
    @classmethod
    def _sslmode_known(cls, v: str) -> str:
        if v not in _PG_SSL_MODES:
            raise ValueError(
                "AIWEB_PG__SSLMODE 取值只能是 "
                + " | ".join(sorted(_PG_SSL_MODES - {""}))
                + "（留空表示不启用 SSL）"
            )
        return v

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and self.database)

    def dsn(self, driver: str = "postgresql+asyncpg") -> str:
        # 口令里的 @ # % / 不转义会被解析成主机名，报错还很难查
        auth = f"{quote_plus(self.user)}:{quote_plus(self.password)}"
        query = f"?ssl={self.sslmode}" if self.sslmode else ""
        # 这里是 app 与 alembic 唯一的连接参数出口：参数名必须是 asyncpg 的 ssl，
        # 写成 libpq 的 sslmode 会让驱动在 connect() 当场 TypeError。
        return f"{driver}://{auth}@{self.host}:{self.port}/{self.database}{query}"

    def connect_args(self) -> dict[str, object]:
        # asyncpg 的超时键叫 timeout；DSN 里没有对应写法，只能由调用方带进 connect_args
        return {"timeout": self.connect_timeout_s}

    def masked_dsn(self) -> str:
        if not self.configured:
            return "<未配置>"
        return f"postgresql://{self.user}:***@{self.host}:{self.port}/{self.database}"


class LlmGroup(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.0
    max_tokens: int = 2000
    timeout_s: int = 60
    # 只对 429/5xx/超时重试：生成 SQL 与分类都是纯函数调用，重试幂等
    max_retries: int = 2

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


class EmbeddingGroup(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    dimension: int = 0

    @field_validator("dimension")
    @classmethod
    def _check_dimension(cls, v: int) -> int:
        # pgvector 的 HNSW/IVFFlat 索引上限是 vector 2000 维
        if v > 2000:
            raise ValueError("embedding 维度超过 2000，pgvector 无法建 HNSW 索引")
        return v

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model and self.dimension > 0)


class JwtGroup(BaseModel):
    secret: str = DEFAULT_JWT_SECRET
    access_ttl_s: int = 900
    refresh_ttl_s: int = 86400

    @property
    def using_default_secret(self) -> bool:
        return self.secret == DEFAULT_JWT_SECRET or len(self.secret) < 32


class FernetGroup(BaseModel):
    keys: str = ""

    @property
    def key_list(self) -> list[str]:
        return [k.strip() for k in self.keys.split(",") if k.strip()]


class ExtractGroup(BaseModel):
    max_tables: int = 2000
    batch_size: int = 200
    batch_interval_ms: int = 100
    heartbeat_interval_s: int = 10
    stale_job_reclaim_s: int = 180
    sample_distinct: bool = True
    sample_distinct_max_distinct: int = 30
    min_mysql_version: str = "5.7"
    min_pg_version: str = "12"
    # kb-workflow §5：卡片模板本身改了要升它，它写进 kb_index_profile（§2.6 的 int 列），
    # 于是 profile 名里的 tplv{version} 跟着变，新旧卡片因此分属两套可回滚的索引。
    card_template_version: int = 1


class RetrievalGroup(BaseModel):
    final_tables_k: int = 5
    token_budget: int = 12000
    catalog_digest_max_tables: int = 1000
    trgm_threshold: float = 0.08
    enable_rewrite: bool = False
    few_shot_examples: int = 3
    few_shot_token_budget: int = 1500
    # JOIN 桥表扩展深度。**数的是桥表张数而不是边数**
    # （口径见 architecture §5.3 的 as-built(P2-0014)）：
    # 按边数截的话 hops=2 只能穿 1 张桥表，§5.3 承诺的"拉进 B/C"就装不出来。
    join_hops: int = 2
    # §5.3 的"候选度 > 8"。一个键同时管两侧：写侧的同名字段度（只告警不丢边）
    # 与图侧的表度（只禁当桥不禁当端点）。roadmap 分组 9 原本没有这一键，键名由工单 014 拍板。
    max_join_degree: int = 8


class QueryGroup(BaseModel):
    row_limit: int = 1000
    timeout_ms: int = 15000
    concurrency_per_ds: int = 2
    rate_limit_per_user_per_min: int = 20
    max_explain_rows: int = 50_000_000
    allow_sql_edit: Literal["admin", "all", "none"] = "admin"


class ResultGroup(BaseModel):
    # 缺省值也要过 validator：pydantic 默认不校验 default，而 `Path("data/results")` 这个
    # 缺省恰恰是 012 那两次落在 `backend/data/` 的元凶——不校验它，收了基准也白收。
    model_config = ConfigDict(validate_default=True)

    dir: Path = Path("data/results")
    retention_days: int = 30
    preview_rows: int = 200
    max_cell_chars: int = 1000
    max_payload_mb: int = 2

    @field_validator("dir")
    @classmethod
    def _dir_is_absolute(cls, value: Path) -> Path:
        return _under_repo_root(value)


class KbDocsGroup(BaseModel):
    # 同 ResultGroup：`knowledge/` 的读写也走 §7 那条"目录根来自配置"的约束，基准只判一次。
    model_config = ConfigDict(validate_default=True)

    dir: Path = Path("knowledge")
    auto_import: bool = True

    @field_validator("dir")
    @classmethod
    def _dir_is_absolute(cls, value: Path) -> Path:
        return _under_repo_root(value)


class LoggingGroup(BaseModel):
    level: str = "INFO"
    json_output: bool = True


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AIWEB_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app: AppGroup = Field(default_factory=AppGroup)
    pg: PgGroup = Field(default_factory=PgGroup)
    llm: LlmGroup = Field(default_factory=LlmGroup)
    embedding: EmbeddingGroup = Field(default_factory=EmbeddingGroup)
    jwt: JwtGroup = Field(default_factory=JwtGroup)
    fernet: FernetGroup = Field(default_factory=FernetGroup)
    extract: ExtractGroup = Field(default_factory=ExtractGroup)
    retrieval: RetrievalGroup = Field(default_factory=RetrievalGroup)
    query: QueryGroup = Field(default_factory=QueryGroup)
    result: ResultGroup = Field(default_factory=ResultGroup)
    kb_docs: KbDocsGroup = Field(default_factory=KbDocsGroup)
    logging: LoggingGroup = Field(default_factory=LoggingGroup)


@lru_cache
def get_settings() -> Settings:
    return Settings()
