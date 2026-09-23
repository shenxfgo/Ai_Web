"""唯一配置入口。L1 密钥与 L2 部署参数走 .env / 进程环境变量，L3 运行参数在数据库里覆盖。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_JWT_SECRET = "dev-insecure-jwt-secret"


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
    sslmode: str = "prefer"
    connect_timeout_s: int = 10
    pool_size: int = 5
    max_overflow: int = 10
    echo: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and self.database)

    def dsn(self, driver: str = "postgresql+asyncpg") -> str:
        # 口令里的 @ # % / 不转义会被解析成主机名，报错还很难查
        auth = f"{quote_plus(self.user)}:{quote_plus(self.password)}"
        return f"{driver}://{auth}@{self.host}:{self.port}/{self.database}?sslmode={self.sslmode}"

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


class RetrievalGroup(BaseModel):
    final_tables_k: int = 5
    token_budget: int = 12000
    catalog_digest_max_tables: int = 1000
    trgm_threshold: float = 0.08
    enable_rewrite: bool = False
    few_shot_examples: int = 3
    few_shot_token_budget: int = 1500


class QueryGroup(BaseModel):
    row_limit: int = 1000
    timeout_ms: int = 15000
    concurrency_per_ds: int = 2
    rate_limit_per_user_per_min: int = 20
    max_explain_rows: int = 50_000_000
    allow_sql_edit: Literal["admin", "all", "none"] = "admin"


class ResultGroup(BaseModel):
    dir: Path = Path("data/results")
    retention_days: int = 30
    preview_rows: int = 200
    max_cell_chars: int = 1000
    max_payload_mb: int = 2


class KbDocsGroup(BaseModel):
    dir: Path = Path("knowledge")
    auto_import: bool = True


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
