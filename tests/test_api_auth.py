"""管理者認証まわりのAPIテスト。"""

from fastapi.testclient import TestClient

from app import main
from app.main import app

client = TestClient(app)


def test_reference_upload_requires_admin_token():
    r = client.post(
        "/api/reference/upload",
        files={"file": ("m.mp4", b"x", "video/mp4")},
        data={"rank": "ダイヤモンド"},
    )
    assert r.status_code == 401


def test_reference_upload_rejects_wrong_token():
    r = client.post(
        "/api/reference/upload",
        files={"file": ("m.mp4", b"x", "video/mp4")},
        data={"rank": "ダイヤモンド"},
        headers={"X-Admin-Token": "wrong-token"},
    )
    assert r.status_code == 401


def test_admin_check():
    assert client.get("/api/admin/check").status_code == 401
    r = client.get("/api/admin/check", headers={"X-Admin-Token": main.ADMIN_TOKEN})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_contributions_require_admin():
    assert client.get("/api/admin/contributions").status_code == 401
    assert client.post("/api/admin/contributions/1/approve").status_code == 401
    assert client.post("/api/admin/contributions/1/reject").status_code == 401


def test_contributions_list_with_token():
    r = client.get(
        "/api/admin/contributions", headers={"X-Admin-Token": main.ADMIN_TOKEN}
    )
    assert r.status_code == 200
    assert "contributions" in r.json()


def test_approve_missing_contribution_returns_404():
    r = client.post(
        "/api/admin/contributions/99999/approve",
        headers={"X-Admin-Token": main.ADMIN_TOKEN},
    )
    assert r.status_code == 404


def test_analyze_is_public():
    """コーチング依頼はトークン不要（不正ランクで400になることで確認）。"""
    r = client.post(
        "/api/analyze",
        files={"file": ("m.mp4", b"x", "video/mp4")},
        data={"rank": "xxx"},
    )
    assert r.status_code == 400  # 401ではない
