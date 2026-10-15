"""The account and learning routes must precede the static frontend mount."""
from fastapi.testclient import TestClient

from main import app


def test_account_api_is_mounted_before_static_frontend():
    with TestClient(app, base_url="http://localhost") as client:
        response = client.get("/api/v1/account")
        assert response.status_code == 200
        assert "configured" in response.json()


def test_library_and_learning_require_a_session():
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/api/v1/library").status_code == 401
        assert client.post("/api/v1/library/missing/notes").status_code == 401
