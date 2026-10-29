"""P1.4：/readyz 就绪检查（数据库失败 503，Redis 降级仍 200）。"""
from fastapi.testclient import TestClient

import config
import health
from app.main import app


def test_readyz_ok_with_sqlite(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "REDIS_URL", "")
    monkeypatch.setattr(config, "CHECKPOINT_DB", str(tmp_path / "checkpoints.db"))
    monkeypatch.setattr(config, "AUTH_DB", str(tmp_path / "auth.db"))
    response = TestClient(app).get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["redis"] == "disabled"


def test_readyz_reports_database_failure(monkeypatch):
    def broken():
        raise RuntimeError("db down")

    monkeypatch.setattr(health, "_check_database", broken)
    response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"]["database"].startswith("error:")
