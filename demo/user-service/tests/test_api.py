"""
API-layer tests for the user-service FastAPI application.

These tests use FastAPI's TestClient (synchronous httpx transport) and
mock out the service layer, so no live Redis or Postgres is needed.
"""
from __future__ import annotations

import json
import unittest.mock as mock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """
    Import the app fresh for each test so startup side-effects don't
    leak between tests.  We patch out asyncpg and Redis before importing.
    """
    # Patch infrastructure initialisers so startup doesn't fail
    with mock.patch("database.init_pool", new_callable=mock.AsyncMock):
        with mock.patch("cache.init_cache", new_callable=mock.AsyncMock):
            with mock.patch("main._warm_cache", new_callable=mock.AsyncMock):
                with mock.patch("main._push_metrics_loop", new_callable=mock.AsyncMock):
                    import main as svc_main
                    with TestClient(svc_main.app, raise_server_exceptions=False) as c:
                        yield c


class TestHealthEndpoint:
    def test_returns_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["service"] == "user-service"

    def test_returns_failure_mode(self, client):
        r = client.get("/health")
        assert "failure_mode" in r.json()


class TestMetricsRaw:
    def test_returns_metrics_shape(self, client):
        r = client.get("/metrics/raw")
        assert r.status_code == 200
        body = r.json()
        assert "metrics" in body
        assert "cache_hit_rate_pct" in body["metrics"]
        assert "db_queries_per_sec" in body["metrics"]


class TestAdminFailureMode:
    def test_set_none_mode(self, client):
        with mock.patch("main._warm_cache", new_callable=mock.AsyncMock):
            r = client.post("/admin/failure?mode=none")
        assert r.status_code == 200
        assert r.json()["failure_mode"] == "none"

    def test_set_regression_mode(self, client):
        r = client.post("/admin/failure?mode=cache_key_regression")
        assert r.status_code == 200
        body = r.json()
        assert body["failure_mode"] == "cache_key_regression"
        assert body["version"] == "1.4.0"

    def test_invalid_mode_returns_400(self, client):
        r = client.post("/admin/failure?mode=explode_everything")
        assert r.status_code == 400


class TestProfileEndpoint:
    def test_invalid_user_id_returns_400(self, client):
        r = client.get("/users/not-a-number/profile")
        assert r.status_code == 400

    def test_service_error_returns_503(self, client):
        with mock.patch(
            "main.get_user_profile",
            new_callable=mock.AsyncMock,
            side_effect=RuntimeError("db down"),
        ):
            r = client.get("/users/1/profile")
        assert r.status_code == 503

    def test_user_not_found_returns_404(self, client):
        with mock.patch(
            "main.get_user_profile",
            new_callable=mock.AsyncMock,
            return_value=(None, False),
        ):
            r = client.get("/users/999/profile")
        assert r.status_code == 404

    def test_cache_hit_returns_profile(self, client):
        profile = {
            "id": 1, "username": "alice", "email": "alice@example.com",
            "full_name": "Alice Anderson", "created_at": "2024-01-01T00:00:00",
        }
        with mock.patch(
            "main.get_user_profile",
            new_callable=mock.AsyncMock,
            return_value=(profile, True),
        ):
            r = client.get("/users/1/profile")
        assert r.status_code == 200
        body = r.json()
        assert body["data"]["username"] == "alice"
        assert body["meta"]["cache_hit"] is True
        assert body["meta"]["source"] == "redis"

    def test_cache_miss_returns_profile_from_db(self, client):
        profile = {
            "id": 2, "username": "bob", "email": "bob@example.com",
            "full_name": "Bob Brown", "created_at": "2024-01-01T00:00:00",
        }
        with mock.patch(
            "main.get_user_profile",
            new_callable=mock.AsyncMock,
            return_value=(profile, False),
        ):
            r = client.get("/users/2/profile")
        assert r.status_code == 200
        body = r.json()
        assert body["meta"]["cache_hit"] is False
        assert body["meta"]["source"] == "postgresql"
