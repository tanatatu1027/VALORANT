"""学習データの出典・許諾管理のテスト。"""

import pytest
from fastapi.testclient import TestClient

from app import main
from app.benchmarks import BenchmarkStore
from app.main import app
from tests.test_benchmarks import _metrics

client = TestClient(app)
ADMIN = {"X-Admin-Token": main.ADMIN_TOKEN}


def test_add_reference_records_source_and_license(tmp_path):
    store = BenchmarkStore(tmp_path / "test.db")
    store.add_reference(
        "ダイヤモンド", _metrics(), label="プロA",
        source="youtube", source_url="https://youtu.be/abc", licensed=True,
    )
    store.add_reference("ダイヤモンド", _metrics(), label="開発用", licensed=False)

    refs = store.list_references()
    assert len(refs) == 2
    by_label = {r["label"]: r for r in refs}
    assert by_label["プロA"]["source"] == "youtube"
    assert by_label["プロA"]["source_url"] == "https://youtu.be/abc"
    assert by_label["プロA"]["licensed"] is True
    assert by_label["開発用"]["source"] == "upload"
    assert by_label["開発用"]["licensed"] is False


def test_delete_reference(tmp_path):
    store = BenchmarkStore(tmp_path / "test.db")
    rid = store.add_reference("ゴールド", _metrics())
    store.delete_reference(rid)
    assert store.list_references() == []
    assert store.reference_counts() == {}
    with pytest.raises(KeyError):
        store.delete_reference(rid)


def test_set_reference_licensed(tmp_path):
    store = BenchmarkStore(tmp_path / "test.db")
    rid = store.add_reference("ゴールド", _metrics(), licensed=False)
    store.set_reference_licensed(rid, True)
    assert store.list_references()[0]["licensed"] is True
    with pytest.raises(KeyError):
        store.set_reference_licensed(999, True)


def test_purge_unlicensed_keeps_licensed_data(tmp_path):
    store = BenchmarkStore(tmp_path / "test.db")
    store.add_reference("ダイヤモンド", _metrics(), label="開発用1", licensed=False)
    store.add_reference("ダイヤモンド", _metrics(), label="開発用2", licensed=False)
    kept = store.add_reference("ダイヤモンド", _metrics(), label="許諾済み", licensed=True)

    deleted = store.purge_unlicensed_references()
    assert deleted == 2
    refs = store.list_references()
    assert len(refs) == 1
    assert refs[0]["id"] == kept
    assert refs[0]["licensed"] is True
    # ベンチマークは許諾済みデータだけで再計算される
    assert store.get_benchmark("ダイヤモンド")["sample_count"] == 1


def test_approved_contribution_is_licensed_player_source(tmp_path):
    store = BenchmarkStore(tmp_path / "test.db")
    cid = store.add_contribution("ゴールド", _metrics(), "v.mp4")
    store.approve_contribution(cid)
    refs = store.list_references()
    assert refs[0]["source"] == "player"
    assert refs[0]["licensed"] is True
    # プレイヤー提供は許諾済みなので一括削除の対象外
    assert store.purge_unlicensed_references() == 0


def test_migration_adds_columns_to_old_db(tmp_path):
    """旧スキーマのDBを開いても列が追加されて動くこと。"""
    import sqlite3
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE reference_videos ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, rank TEXT NOT NULL, label TEXT,"
        " metrics_json TEXT NOT NULL, created_at REAL NOT NULL)"
    )
    conn.execute(
        "INSERT INTO reference_videos (rank, label, metrics_json, created_at)"
        " VALUES ('ゴールド', '旧データ', '{\"round_count\": 5}', 0)"
    )
    conn.commit()
    conn.close()

    store = BenchmarkStore(db)
    refs = store.list_references()
    assert refs[0]["label"] == "旧データ"
    assert refs[0]["source"] == "upload"   # 既存行はデフォルト値
    assert refs[0]["licensed"] is False


# ---- API ----

def test_references_endpoints_require_admin():
    assert client.get("/api/admin/references").status_code == 401
    assert client.delete("/api/admin/references/1").status_code == 401
    assert client.post(
        "/api/admin/references/1/licensed", json={"licensed": True}
    ).status_code == 401
    assert client.post("/api/admin/references/purge-unlicensed").status_code == 401


def test_references_list_with_token():
    r = client.get("/api/admin/references", headers=ADMIN)
    assert r.status_code == 200
    assert "references" in r.json()


def test_delete_missing_reference_returns_404():
    r = client.delete("/api/admin/references/99999", headers=ADMIN)
    assert r.status_code == 404


def test_purge_endpoint():
    r = client.post("/api/admin/references/purge-unlicensed", headers=ADMIN)
    assert r.status_code == 200
    assert "deleted" in r.json()
