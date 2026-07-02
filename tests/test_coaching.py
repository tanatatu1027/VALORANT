"""コーチングロジック（ルールベース部分）のテスト。"""

from app.coaching import analyze_rounds, build_rule_based_report, compare_metrics
from app.video_analysis import MatchMetrics, RoundMetrics


TARGET = {
    "avg_first_engagement_sec": 30.0,
    "avg_engagements_per_round": 4.0,
    "avg_round_duration_sec": 105.0,
}


def _metrics(first=30.0, engagements=4.0, duration=105.0) -> MatchMetrics:
    rounds = [
        RoundMetrics(
            index=1, start_sec=0.0, end_sec=duration, duration_sec=duration,
            engagement_count=int(engagements), first_engagement_sec=first,
            activity_ratio=0.1,
        ),
        RoundMetrics(
            index=2, start_sec=duration, end_sec=duration * 2, duration_sec=duration,
            engagement_count=0, first_engagement_sec=None, activity_ratio=0.0,
        ),
    ]
    m = MatchMetrics(duration_sec=duration * 2, round_count=2, rounds=rounds)
    m.avg_first_engagement_sec = first
    m.avg_engagements_per_round = engagements
    m.avg_round_duration_sec = duration
    return m


def test_compare_metrics_flags_early_engagement():
    user = {"avg_first_engagement_sec": 12.0, "avg_engagements_per_round": 4.0,
            "avg_round_duration_sec": 105.0}
    findings = compare_metrics(user, TARGET)
    by_metric = {f["metric"]: f for f in findings}
    assert by_metric["avg_first_engagement_sec"]["verdict"] == "warn"
    assert by_metric["avg_engagements_per_round"]["verdict"] == "good"


def test_compare_metrics_all_good_when_close():
    user = dict(TARGET)
    findings = compare_metrics(user, TARGET)
    assert all(f["verdict"] == "good" for f in findings)


def test_compare_metrics_later_engagement_is_not_flagged():
    # 初動が「遅い」のは慎重なだけなので warn にしない
    user = {"avg_first_engagement_sec": 45.0, "avg_engagements_per_round": 4.0,
            "avg_round_duration_sec": 105.0}
    findings = compare_metrics(user, TARGET)
    by_metric = {f["metric"]: f for f in findings}
    assert by_metric["avg_first_engagement_sec"]["verdict"] == "good"


def test_analyze_rounds_flags_no_engagement():
    rounds = analyze_rounds(_metrics(), TARGET)
    assert len(rounds) == 2
    # 2ラウンド目は交戦なし
    assert any("交戦が検出されませんでした" in n for n in rounds[1]["notes"])


def test_analyze_rounds_flags_early_fight():
    m = _metrics(first=5.0)
    rounds = analyze_rounds(m, TARGET)
    assert any("初動が早すぎる" in n for n in rounds[0]["notes"])


def test_build_rule_based_report_structure():
    report = build_rule_based_report("ゴールド", _metrics(first=10.0), TARGET | {
        "rank": "プラチナ", "source": "default", "sample_count": 0,
    })
    assert report["user_rank"] == "ゴールド"
    assert report["target_rank"] == "プラチナ"
    assert report["improvement_points"]  # 初動10秒はwarnになる
    assert report["focus_points"]
    assert len(report["rounds"]) == 2
