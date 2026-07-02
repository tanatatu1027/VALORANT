"""ベンチマークストアとランクロジックのテスト。"""

import pytest

from app.benchmarks import BenchmarkStore
from app.ranks import next_rank, normalize_rank
from app.video_analysis import MatchMetrics, RoundMetrics


def _metrics(first=25.0, engagements=4.0, duration=100.0) -> MatchMetrics:
    m = MatchMetrics(
        duration_sec=2400.0,
        round_count=20,
        rounds=[
            RoundMetrics(
                index=1, start_sec=0, end_sec=duration, duration_sec=duration,
                engagement_count=int(engagements), first_engagement_sec=first,
                activity_ratio=0.1,
            )
        ],
    )
    m.avg_first_engagement_sec = first
    m.avg_engagements_per_round = engagements
    m.avg_round_duration_sec = duration
    return m


def test_normalize_rank():
    assert normalize_rank("ゴールド") == "ゴールド"
    assert normalize_rank("gold") == "ゴールド"
    assert normalize_rank("Diamond") == "ダイヤモンド"
    with pytest.raises(ValueError):
        normalize_rank("unknown")


def test_next_rank():
    assert next_rank("ゴールド") == "プラチナ"
    assert next_rank("ダイヤモンド") == "アセンダント"
    assert next_rank("レディアント") == "レディアント"


def test_default_benchmark(tmp_path):
    store = BenchmarkStore(tmp_path / "test.db")
    b = store.get_benchmark("ダイヤモンド")
    assert b["source"] == "default"
    assert b["sample_count"] == 0
    assert b["avg_first_engagement_sec"] > 0


def test_learned_benchmark_overrides_default(tmp_path):
    store = BenchmarkStore(tmp_path / "test.db")
    store.add_reference("ダイヤモンド", _metrics(first=28.0))
    store.add_reference("ダイヤモンド", _metrics(first=32.0))
    b = store.get_benchmark("ダイヤモンド")
    assert b["source"] == "learned"
    assert b["sample_count"] == 2
    assert b["avg_first_engagement_sec"] == 30.0


def test_target_benchmark_uses_next_rank(tmp_path):
    store = BenchmarkStore(tmp_path / "test.db")
    store.add_reference("プラチナ", _metrics(first=26.0))
    b = store.get_target_benchmark("ゴールド")
    assert b["rank"] == "プラチナ"
    assert b["source"] == "learned"


def test_contribution_flow(tmp_path):
    store = BenchmarkStore(tmp_path / "test.db")
    cid = store.add_contribution("ゴールド", _metrics(first=20.0), "abc.mp4")

    pending = store.list_contributions("pending")
    assert len(pending) == 1
    assert pending[0]["id"] == cid
    assert pending[0]["rank"] == "ゴールド"
    assert pending[0]["video_name"] == "abc.mp4"

    # 承認すると学習データに反映される
    result = store.approve_contribution(cid)
    assert result["status"] == "approved"
    assert store.reference_counts() == {"ゴールド": 1}
    assert store.get_benchmark("ゴールド")["source"] == "learned"
    assert store.list_contributions("pending") == []

    # 二重承認はエラー
    with pytest.raises(ValueError):
        store.approve_contribution(cid)


def test_contribution_reject(tmp_path):
    store = BenchmarkStore(tmp_path / "test.db")
    cid = store.add_contribution("シルバー", _metrics(), None)
    result = store.reject_contribution(cid)
    assert result["status"] == "rejected"
    # 却下されたものは学習に反映されない
    assert store.reference_counts() == {}
    with pytest.raises(KeyError):
        store.reject_contribution(cid)


def test_contribution_not_found(tmp_path):
    store = BenchmarkStore(tmp_path / "test.db")
    with pytest.raises(KeyError):
        store.approve_contribution(999)


def test_reference_counts(tmp_path):
    store = BenchmarkStore(tmp_path / "test.db")
    store.add_reference("ダイヤモンド", _metrics())
    store.add_reference("ダイヤモンド", _metrics())
    store.add_reference("プラチナ", _metrics())
    counts = store.reference_counts()
    assert counts == {"ダイヤモンド": 2, "プラチナ": 1}
