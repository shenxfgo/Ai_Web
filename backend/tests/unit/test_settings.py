from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.settings import DEFAULT_JWT_SECRET, EmbeddingGroup, JwtGroup, PgGroup, Settings


def test_nested_env_uses_double_underscore(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIWEB_PG__HOST", "pg.internal")
    monkeypatch.setenv("AIWEB_PG__SCHEMA_NAME", "aiweb_x")
    monkeypatch.setenv("AIWEB_QUERY__ROW_LIMIT", "500")
    settings = Settings(_env_file=None)
    assert settings.pg.host == "pg.internal"
    assert settings.pg.schema_name == "aiweb_x"
    assert settings.query.row_limit == 500


def test_dsn_quotes_credentials() -> None:
    # 口令里的 @ 不转义会被当成主机分隔符，报"未知主机"，极难查
    pg = PgGroup(host="h", user="u@site", password="p@ss#w/1", database="d")
    dsn = pg.dsn()
    assert "u%40site:p%40ss%23w%2F1" in dsn
    assert dsn.startswith("postgresql+asyncpg://")
    assert pg.masked_dsn() == "postgresql://u@site:***@h:5432/d"


def test_embedding_dimension_above_hnsw_limit_is_rejected() -> None:
    with pytest.raises(ValidationError, match="2000"):
        EmbeddingGroup(dimension=3072)


def test_embedding_disabled_when_partial_config() -> None:
    assert not EmbeddingGroup(base_url="u", api_key="k").configured
    assert EmbeddingGroup(base_url="u", api_key="k", model="m", dimension=1024).configured


def test_llm_configured_requires_all_three() -> None:
    assert not Settings(_env_file=None).llm.configured


def test_default_jwt_secret_is_flagged() -> None:
    assert JwtGroup().using_default_secret
    assert JwtGroup(secret=DEFAULT_JWT_SECRET).using_default_secret
    assert not JwtGroup(secret="x" * 48).using_default_secret
    assert JwtGroup(secret="short").using_default_secret


def test_app_defaults_are_local_dev() -> None:
    settings = Settings(_env_file=None)
    assert settings.app.host == "127.0.0.1"
    assert settings.query.allow_sql_edit == "admin"
    assert settings.extract.sample_distinct is True
