"""Tests for FastAPI server and WebSocket full-duplex communication."""

import pytest
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
    assert "Interruptible Real-Time Agent Demo" in response.text
