"""Tests for FastAPI server and WebSocket full-duplex communication."""

from starlette.testclient import TestClient
from agent.server import app


def test_server_health():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_server_demo_ui():
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "Kairos" in response.text
