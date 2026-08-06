"""Tests for the LDR Date Planner API skeleton."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_endpoint() -> None:
    """GET /health returns status ok with version info."""
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == "0.1.0"
    assert "database" in body
    assert "checked_at" in body


def test_health_database_disconnected() -> None:
    """Database shows disconnected when no Postgres is running."""
    response = client.get("/health")
    body = response.json()
    assert body["database"] == "disconnected"


def test_openapi_schema() -> None:
    """OpenAPI schema is generated correctly."""
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "LDR Date Planner API"
    assert schema["info"]["version"] == "0.1.0"
    assert "/health" in schema["paths"]