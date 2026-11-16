"""Basic smoke test for FastAPI and database initialization."""
from starlette.testclient import TestClient
from app.main import app

def test_api_status():
    client = TestClient(app)
    response = client.get("/api/status")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "online"
    assert "mock_mode" in data
    assert "llm_mode" in data
    assert "is_demo" in data
    assert "subjects_count" in data
    assert "topics_count" in data


def test_static_files():
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "AdaptivePlan" in response.text
