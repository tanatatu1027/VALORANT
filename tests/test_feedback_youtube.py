"""コメント機能とYouTube学習まわりのテスト。"""

import pytest
from fastapi.testclient import TestClient

from app import main
from app.benchmarks import BenchmarkStore
from app.main import app
from app.youtube import is_youtube_url

client = TestClient(app)


# ---- ストア: フィードバック ----

def test_feedback_store_flow(tmp_path):
    store = BenchmarkStore(tmp_path / "test.db")
    fid = store.add_feedback("ラウンド検出がずれていました", "ゴールド")

    items = store.list_feedback("new")
    assert len(items) == 1
    assert items[0]["id"] == fid
    assert items[0]["message"] == "ラウンド検出がずれていました"
    assert items[0]["rank"] == "ゴールド"

    store.mark_feedback_done(fid)
    assert store.list_feedback("new") == []
    assert len(store.list_feedback("done")) == 1
    assert len(store.list_feedback(None)) == 1

    with pytest.raises(KeyError):
        store.mark_feedback_done(fid)  # 二重処理はエラー


def test_feedback_store_invalid_rank_is_dropped(tmp_path):
    store = BenchmarkStore(tmp_path / "test.db")
    store.add_feedback("コメント", "unknown-rank")
    assert store.list_feedback("new")[0]["rank"] is None


# ---- API: フィードバック ----

def test_post_feedback_public():
    r = client.post("/api/feedback", json={"message": "テストコメント", "rank": "ゴールド"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_post_feedback_empty_rejected():
    r = client.post("/api/feedback", json={"message": "   "})
    assert r.status_code == 400


def test_post_feedback_too_long_rejected():
    r = client.post("/api/feedback", json={"message": "あ" * 2001})
    assert r.status_code == 400


def test_admin_feedback_requires_token():
    assert client.get("/api/admin/feedback").status_code == 401
    assert client.post("/api/admin/feedback/1/done").status_code == 401


def test_admin_feedback_flow():
    headers = {"X-Admin-Token": main.ADMIN_TOKEN}
    r = client.post("/api/feedback", json={"message": "対応フローのテスト"})
    fid = r.json()["id"]

    items = client.get("/api/admin/feedback", headers=headers).json()["feedback"]
    assert any(f["id"] == fid for f in items)

    r = client.post(f"/api/admin/feedback/{fid}/done", headers=headers)
    assert r.status_code == 200
    items = client.get("/api/admin/feedback", headers=headers).json()["feedback"]
    assert not any(f["id"] == fid for f in items)


# ---- YouTube ----

def test_is_youtube_url():
    assert is_youtube_url("https://www.youtube.com/watch?v=abc123")
    assert is_youtube_url("https://youtube.com/watch?v=abc123")
    assert is_youtube_url("https://m.youtube.com/watch?v=abc123")
    assert is_youtube_url("https://youtu.be/abc123")
    assert is_youtube_url("https://www.youtube.com/shorts/abc123")
    assert is_youtube_url("https://www.youtube.com/live/abc123")
    assert not is_youtube_url("https://example.com/watch?v=abc123")
    assert not is_youtube_url("https://vimeo.com/12345")
    assert not is_youtube_url("not a url")


def test_youtube_reference_requires_admin():
    r = client.post(
        "/api/reference/youtube",
        json={"url": "https://www.youtube.com/watch?v=abc", "rank": "ダイヤモンド"},
    )
    assert r.status_code == 401


def test_youtube_reference_rejects_non_youtube_url():
    r = client.post(
        "/api/reference/youtube",
        json={"url": "https://example.com/video.mp4", "rank": "ダイヤモンド"},
        headers={"X-Admin-Token": main.ADMIN_TOKEN},
    )
    assert r.status_code == 400
    assert "YouTube" in r.json()["detail"]


def test_youtube_reference_rejects_invalid_rank():
    r = client.post(
        "/api/reference/youtube",
        json={"url": "https://www.youtube.com/watch?v=abc", "rank": "xxx"},
        headers={"X-Admin-Token": main.ADMIN_TOKEN},
    )
    assert r.status_code == 400
