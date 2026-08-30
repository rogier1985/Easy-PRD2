from pathlib import Path

from fastapi.testclient import TestClient

from easy_prd2.app import create_app
from easy_prd2.history import HistoryStore


def test_home_has_security_headers_and_product_surface(tmp_path: Path):
    app = create_app(history=HistoryStore(tmp_path))
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "Easy PRD2" in response.text
    assert "Create-only by design" in response.text
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_mutation_requires_csrf(tmp_path: Path):
    app = create_app(history=HistoryStore(tmp_path))
    with TestClient(app) as client:
        response = client.delete("/api/history")
        assert response.status_code == 403
        response = client.delete("/api/history", headers={"X-Easy-PRD2-CSRF": app.state.csrf})
        assert response.status_code == 200


def test_rejects_non_local_host(tmp_path: Path):
    app = create_app(history=HistoryStore(tmp_path))
    with TestClient(app, base_url="http://evil.example") as client:
        response = client.get("/")
    assert response.status_code == 400

