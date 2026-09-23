"""Contract tests for GET /health."""
from __future__ import annotations


def test_health_returns_ok(api_client) -> None:
    response = api_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "campusnexus-api"
