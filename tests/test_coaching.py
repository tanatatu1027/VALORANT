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


def test_report_includes_evaluation_axes():
    report = build_rule_based_report("ゴールド", _metrics(), TARGET | {
        "rank": "プラチナ", "source": "default", "sample_count": 0,
    })
    axes = report["evaluation_axes"]
    ids = {a["id"] for a in axes}
    # 立ち回り4観点 + 撃ち合い2観点
    assert ids == {"minimap", "cover_line", "skill_usage", "agent_fit", "peek", "shooting"}
    for a in axes:
        assert a["category"] in ("立ち回り", "撃ち合い")
        assert a["check_items"]


def test_round_rating_symbols():
    """簡易評価: 問題なし+規律的な初動=◎、問題なし=○、問題1件=△、2件以上=✖。"""
    from app.video_analysis import RoundMetrics

    def one_round(first, engagements, duration):
        m = MatchMetrics(duration_sec=duration, round_count=1, rounds=[
            RoundMetrics(index=1, start_sec=0.0, end_sec=duration,
                         duration_sec=duration, engagement_count=engagements,
                         first_engagement_sec=first, activity_ratio=0.1),
        ])
        return analyze_rounds(m, TARGET)[0]

    # 初動30秒(基準通り)・交戦4回・通常の長さ → ◎
    assert one_round(30.0, 4, 100.0)["rating"] == "◎"
    # 初動20秒(基準30秒の0.8倍未満だが早すぎ判定の0.5倍以上) → 問題0件で○
    assert one_round(20.0, 4, 100.0)["rating"] == "○"
    # 初動10秒(早すぎ=問題1件) → △
    assert one_round(10.0, 4, 100.0)["rating"] == "△"
    # 初動10秒 + 交戦8回(問題2件) → ✖
    assert one_round(10.0, 8, 100.0)["rating"] == "✖"


def test_round_rating_included_in_report():
    report = build_rule_based_report("ゴールド", _metrics(), TARGET | {
        "rank": "プラチナ", "source": "default", "sample_count": 0,
    })
    for r in report["rounds"]:
        assert r["rating"] in ("◎", "○", "△", "✖")
        assert r["rating_label"]


def test_ai_prompt_covers_requested_axes():
    """AIコーチのプロンプトが指定された評価観点を網羅していること。"""
    from app.coaching import AI_SYSTEM_PROMPT
    for keyword in [
        "ミニマップ", "カバーライン", "スキル", "エージェント",
        "ピーク", "撃ち方", "立ち回り", "ストッピング", "トレード",
    ]:
        assert keyword in AI_SYSTEM_PROMPT, f"プロンプトに「{keyword}」がありません"
