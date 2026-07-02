"""ラウンド分割・イベント集約ロジックのテスト。"""

from app.video_analysis import (
    build_match_metrics,
    group_events,
    segment_rounds,
)


def _make_signal(duration: int, boundaries: list[int]) -> tuple[list[float], list[float]]:
    """1秒間隔のタイムスタンプと、境界位置に大きなスコアを持つ差分列を作る。"""
    timestamps = [float(t) for t in range(duration + 1)]
    scores = [0.05] * len(timestamps)
    for b in boundaries:
        scores[b] = 0.9
    return scores, timestamps


def test_segment_rounds_detects_boundaries():
    # 300秒の動画、100秒と200秒に大きなシーンチェンジ
    scores, timestamps = _make_signal(300, [100, 200])
    segments = segment_rounds(scores, timestamps, min_len=45, max_len=200)
    assert len(segments) == 3
    assert segments[0][0] == 0.0
    assert abs(segments[0][1] - 100.0) < 1.0
    assert abs(segments[1][1] - 200.0) < 1.0
    assert segments[-1][1] == 300.0


def test_segment_rounds_ignores_too_close_boundaries():
    # 50秒と60秒の境界候補 → min_len=45 なので60秒側は捨てられる
    scores, timestamps = _make_signal(200, [50, 60])
    segments = segment_rounds(scores, timestamps, min_len=45, max_len=200)
    starts = [s for s, _ in segments]
    assert 50.0 in starts
    assert 60.0 not in starts


def test_segment_rounds_splits_overlong_segments():
    # 境界なしの500秒 → max_len=200 で機械分割される
    scores, timestamps = _make_signal(500, [])
    segments = segment_rounds(scores, timestamps, min_len=45, max_len=200)
    assert all(e - s <= 200.0 + 1e-6 for s, e in segments)
    assert segments[0][0] == 0.0
    assert segments[-1][1] == 500.0


def test_segment_rounds_short_video():
    scores, timestamps = _make_signal(30, [])
    segments = segment_rounds(scores, timestamps, min_len=45, max_len=200)
    assert segments == [(0.0, 30.0)]


def test_segment_rounds_empty():
    assert segment_rounds([], []) == []


def test_group_events_merges_within_gap():
    timestamps = [0.0, 1.0, 2.0, 3.0, 10.0, 11.0, 30.0]
    flags = [True, True, False, True, True, False, True]
    events = group_events(flags, timestamps, gap=4.0)
    # 0-3秒は1イベント、10-11秒は0-3から7秒空くので別イベント、30秒も別
    assert events == [0.0, 10.0, 30.0]


def test_group_events_no_activity():
    assert group_events([False, False], [0.0, 1.0]) == []


def test_build_match_metrics():
    segments = [(0.0, 100.0), (100.0, 200.0)]
    timestamps = [float(t) for t in range(200)]
    flags = [t in (20, 21, 150) for t in range(200)]
    events = group_events(flags, timestamps, gap=4.0)
    m = build_match_metrics(segments, events, flags, timestamps, 200.0)

    assert m.round_count == 2
    r1, r2 = m.rounds
    assert r1.engagement_count == 1
    assert r1.first_engagement_sec == 20.0
    assert r2.engagement_count == 1
    assert r2.first_engagement_sec == 50.0  # 150 - 100
    assert m.avg_first_engagement_sec == 35.0
    assert m.avg_engagements_per_round == 1.0
