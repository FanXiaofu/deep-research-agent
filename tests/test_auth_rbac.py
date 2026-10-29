"""P1.3：账号/API token/RBAC/配额/thread 归属/审计。"""
import pytest
from fastapi.testclient import TestClient

import auth
import config
from app.main import app


def _principal(username="alice", quota=2, role="user"):
    auth.create_user(username, role=role, daily_quota=quota)
    token, _ = auth.issue_token(username, name="test")
    return auth.authenticate_token(token), token


def test_create_user_and_authenticate():
    user = auth.create_user("alice", role="user", daily_quota=2)
    token, token_id = auth.issue_token("alice", name="laptop")
    assert token_id > 0
    principal = auth.authenticate_token(token)
    assert principal is not None
    assert principal.user_id == str(user["id"])
    assert principal.username == "alice"
    assert principal.role == "user"
    assert not principal.is_admin
    assert auth.authenticate_token("bad-token") is None
    with pytest.raises(ValueError):
        auth.create_user("alice")


def test_daily_quota_and_admin_bypass():
    principal, _ = _principal(quota=2)
    assert auth.consume_quota(principal) == (True, 1)
    assert auth.consume_quota(principal) == (True, 0)
    assert auth.consume_quota(principal) == (False, 0)

    admin = auth.Principal("9", "root", "admin", 0)
    assert auth.consume_quota(admin) == (True, -1)


def test_thread_ownership_and_admin_bypass():
    alice, _ = _principal("alice")
    bob, _ = _principal("bob")
    auth.claim_thread(alice, "thread-1")
    auth.assert_thread_access(alice, "thread-1")
    with pytest.raises(PermissionError):
        auth.assert_thread_access(bob, "thread-1")
    auth.assert_thread_access(auth.Principal("9", "root", "admin", 0), "thread-1")


def test_audit_and_disabled_user():
    alice, token = _principal("alice")
    auth.audit(alice.user_id, "research_start", thread_id="t1", detail="topic")
    events = auth.list_audit()
    assert events[0]["action"] == "research_start"
    assert events[0]["thread_id"] == "t1"
    assert auth.stats()["users"] == 1

    auth.disable_user("alice")
    assert auth.authenticate_token(token) is None


def test_metrics_and_audit_require_admin():
    _, user_token = _principal("alice")
    _, admin_token = _principal("root", role="admin", quota=0)
    client = TestClient(app)
    user_headers = {"Authorization": f"Bearer {user_token}"}
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    assert client.get("/metrics", headers=user_headers).status_code == 403
    assert client.get("/metrics", headers=admin_headers).status_code == 200
    assert client.get("/api/audit", headers=user_headers).status_code == 403
    assert client.get("/api/audit", headers=admin_headers).status_code == 200
    # 带错误 token 的 loopback 请求不能回退成本机匿名 admin
    assert client.get(
        "/metrics", headers={"Authorization": "Bearer not-a-real-token"}
    ).status_code == 401


def test_report_is_scoped_to_thread_owner():
    alice, alice_token = _principal("alice")
    _, bob_token = _principal("bob")
    auth.claim_thread(alice, "owned-thread")
    client = TestClient(app)

    ok = client.get(
        "/api/report/owned-thread",
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    denied = client.get(
        "/api/report/owned-thread",
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    assert ok.status_code == 200
    assert denied.status_code == 403

    resume_denied = client.post(
        "/api/resume/stream",
        json={"thread_id": "owned-thread", "value": ""},
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    assert resume_denied.status_code == 403
