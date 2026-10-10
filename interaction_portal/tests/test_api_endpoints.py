"""Tests for FastAPI endpoints and admin export security."""

import pytest
from starlette.testclient import TestClient
from app.config_loader import load_config
from app.event_store import EventStore
from app.main import create_app


@pytest.fixture
def client_env(tmp_path):
    cfg = load_config()
    cfg.standalone = True
    cfg.admin_token = "secret-test-token"
    store = EventStore(tmp_path / "api_test.sqlite3")
    app = create_app(cfg, store)
    return cfg, store, TestClient(app)


def test_healthz(client_env):
    _, _, client = client_env
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json()["ok"] is True


def test_standalone_launcher_and_entry(client_env):
    _, _, client = client_env
    # GET / in standalone mode shows launcher
    res = client.get("/")
    assert res.status_code == 200
    assert "launcher" in res.text.lower() or "standalone test launcher" in res.text.lower()

    # Valid entry link sets cookie and redirects to screen route
    res_entry = client.get("/?pid=pid_api_test&condition=EVA-TASK", follow_redirects=False)
    assert res_entry.status_code == 303
    assert res_entry.headers["location"] == "/session/pid_api_test/screen"
    assert "portal_session" in res_entry.cookies


def test_admin_export_token_protection(client_env):
    cfg, store, client = client_env
    # Without token: 403 Forbidden
    res_no_token = client.get("/admin/export/events.csv")
    assert res_no_token.status_code == 403

    # With valid token header: 200 OK
    res_header = client.get("/admin/export/events.csv", headers={"x-admin-token": "secret-test-token"})
    assert res_header.status_code == 200
    assert "pid,timestamp" in res_header.text

    # With valid token query param: 200 OK
    res_query = client.get("/admin/export/all.json?token=secret-test-token")
    assert res_query.status_code == 200
    assert "sessions" in res_query.json()
