from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_healthz_does_not_touch_database() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["retrieval_mode"] in {"keyword", "vector+keyword"}


def test_request_id_header_present() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/healthz")
    assert len(resp.headers["X-Request-Id"]) >= 8


def test_unknown_path_uses_error_envelope() -> None:
    with TestClient(app) as client:
        resp = client.get("/api/nope")
    assert resp.status_code == 404
