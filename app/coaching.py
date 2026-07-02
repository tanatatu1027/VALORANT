"""コーチングレポート生成。

利用者の動画指標を「次のランク帯」のベンチマークと比較し、
ラウンドごとの分析と総合アドバイスを日本語で生成する。

- ルールベース分析: APIキー不要で常に動く
- AIコーチング: ANTHROPIC_API_KEY があれば Claude が指標と
  代表フレーム（実際のプレー画面）を見てより具体的な指導を生成する
"""

from __future__ import annotations

import json
import os

from .knowledge_base import METRIC_NOTES, get_focus_points
from .ranks import next_rank
from .video_analysis import MatchMetrics

# 上位帯と比べて「差がある」とみなす相対しきい値
DIFF_THRESHOLD = 0.15

CLAUDE_MODEL = "claude-opus-4-8"
MAX_KEYFRAME_ROUNDS = 6  # AIに画像を渡すラウンド数の上限（コスト・トークン対策）


# ---- ルールベース比較（テスト対象の純粋ロジック） -----------------------

def compare_metrics(user: dict, target: dict) -> list[dict]:
    """利用者指標とターゲットベンチマークを比較し、所見リストを返す。

    各所見: {metric, user_value, target_value, verdict, comment}
    verdict は "good" / "warn" のいずれか。
    """
    findings: list[dict] = []

    def add(metric: str, comment_bad: str, comment_good: str, lower_is_risky: bool):
        u = user.get(metric)
        t = target.get(metric)
        if u is None or t is None or t == 0:
            return
        diff = (u - t) / t
        # lower_is_risky=True の指標は「値が小さすぎる」ことが問題
        risky = diff < -DIFF_THRESHOLD if lower_is_risky else abs(diff) > DIFF_THRESHOLD
        findings.append({
            "metric": metric,
            "user_value": u,
            "target_value": t,
            "verdict": "warn" if risky else "good",
            "comment": comment_bad if risky else comment_good,
        })

    add(
        "avg_first_engagement_sec",
        "最初の交戦が上位帯より早すぎます。ラウンド開始直後のドライピークや無理な初動を減らし、"
        "セットアップ・情報収集を済ませてから撃ち合いましょう。",
        "初動交戦のタイミングは上位帯に近い水準です。",
        lower_is_risky=True,
    )
    add(
        "avg_engagements_per_round",
        "1ラウンドあたりの交戦回数が上位帯の水準から外れています。多い場合は不要な撃ち合い、"
        "少ない場合はラウンドへの関与不足（寄り遅れ）の可能性があります。",
        "ラウンドあたりの交戦回数は適正な範囲です。",
        lower_is_risky=False,
    )
    add(
        "avg_round_duration_sec",
        "平均ラウンド時間が上位帯の水準から外れています。極端に短いラウンドが多い場合、"
        "ラッシュ一辺倒か早期の人数不利でラウンドが崩壊している可能性があります。",
        "ラウンドの使い方（時間配分）は上位帯に近い水準です。",
        lower_is_risky=False,
    )
    return findings


def analyze_rounds(metrics: MatchMetrics, target: dict) -> list[dict]:
    """ラウンドごとの所見を生成する。"""
    results = []
    target_first = target.get("avg_first_engagement_sec", 30.0)
    for r in metrics.rounds:
        notes: list[str] = []
        if r.first_engagement_sec is not None and r.first_engagement_sec < target_first * 0.5:
            notes.append(
                f"開始{r.first_engagement_sec:.0f}秒で交戦が発生しています。"
                "初動が早すぎる可能性があります（上位帯の平均は"
                f"{target_first:.0f}秒前後）。"
            )
        if r.engagement_count == 0:
            notes.append(
                "交戦が検出されませんでした。ラウンドへの関与が薄い（寄り遅れ・芋り）か、"
                "早期に終了したラウンドの可能性があります。"
            )
        if r.engagement_count >= 8:
            notes.append(
                "交戦回数が非常に多いラウンドです。撃ち合いの選択（有利ポジション以外では"
                "引く判断）を見直す余地があります。"
            )
        if r.duration_sec < 50:
            notes.append("非常に短いラウンドです。ラッシュまたは早期崩壊の可能性があります。")
        if not notes:
            notes.append("特筆すべき問題は検出されませんでした。")
        results.append({
            "round": r.index,
            "start_sec": r.start_sec,
            "end_sec": r.end_sec,
            "duration_sec": r.duration_sec,
            "engagement_count": r.engagement_count,
            "first_engagement_sec": r.first_engagement_sec,
            "notes": notes,
        })
    return results


def build_rule_based_report(
    user_rank: str,
    metrics: MatchMetrics,
    target_benchmark: dict,
) -> dict:
    """ルールベースのコーチングレポートを組み立てる。"""
    target = next_rank(user_rank)
    findings = compare_metrics(metrics.to_dict(), target_benchmark)
    rounds = analyze_rounds(metrics, target_benchmark)
    good = [f["comment"] for f in findings if f["verdict"] == "good"]
    warn = [f["comment"] for f in findings if f["verdict"] == "warn"]
    return {
        "user_rank": user_rank,
        "target_rank": target,
        "benchmark": target_benchmark,
        "match_summary": metrics.to_dict(),
        "findings": findings,
        "good_points": good,
        "improvement_points": warn,
        "focus_points": get_focus_points(user_rank),
        "rounds": rounds,
        "metric_notes": METRIC_NOTES,
    }


# ---- Claude によるAIコーチング -------------------------------------------

AI_SYSTEM_PROMPT = """\
あなたはVALORANTのプロコーチです。プレイヤーが自分の試合動画をアップロードし、
自動解析による指標と、実際のプレー画面のスクリーンショットが渡されます。

役割:
- プレイヤーの現在のランク帯と、目標である次のランク帯の差を埋める指導をする
- 良かった点と改善点の両方を必ず挙げる（改善点だけにしない）
- ラウンド番号を明示して指摘する（例: 「第3ラウンド: ...」）
- スクリーンショットからはクロスヘアの高さ、ポジション、ミニマップの人数配分、
  武器・アーマー・所持金（エコノミー判断）など読み取れる範囲で具体的に指摘する
- 抽象論ではなく「次の試合から実行できる」行動レベルのアドバイスにする
- プレイヤーは解説を読みながら同じ画面でアップロードした動画を再生できるため、
  ラウンド別コーチングでは動画内の時刻（例: 「3:20あたりを見返してください」）を添えて、
  動画のどこを見ればよいか分かるようにする（各ラウンドの start_sec が動画内の開始秒数）

出力は日本語のMarkdownで、以下の構成にすること:
## 総評
## 良かった点
## 改善点（優先度順）
## ラウンド別コーチング
## 次のランク帯に上がるための練習メニュー
"""


def _ai_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def generate_ai_report(
    user_rank: str,
    metrics: MatchMetrics,
    rule_report: dict,
) -> str | None:
    """Claude にコーチングレポートを生成させる。API未設定なら None。"""
    if not _ai_available():
        return None

    import anthropic

    client = anthropic.Anthropic()

    stats_payload = {
        "現在のランク": user_rank,
        "目標ランク": rule_report["target_rank"],
        "目標ランク帯のベンチマーク": rule_report["benchmark"],
        "試合全体の指標": {
            k: v for k, v in rule_report["match_summary"].items() if k != "rounds"
        },
        "ラウンド別指標": rule_report["rounds"],
        "自動検出された所見": rule_report["findings"],
    }

    content: list[dict] = [
        {
            "type": "text",
            "text": (
                "以下はプレイヤーの試合動画の自動解析結果です。\n\n"
                "```json\n"
                + json.dumps(stats_payload, ensure_ascii=False, indent=2)
                + "\n```\n\n"
                "続けて、いくつかのラウンドの実際のプレー画面を添付します。"
                "画像は「ラウンドN」のラベルの直後に対応するスクリーンショットが並びます。"
            ),
        }
    ]

    attached = 0
    for round_index in sorted(metrics.keyframes.keys()):
        if attached >= MAX_KEYFRAME_ROUNDS:
            break
        frames = metrics.keyframes[round_index]
        content.append({"type": "text", "text": f"ラウンド{round_index}のプレー画面:"})
        for b64 in frames:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": b64,
                },
            })
        attached += 1

    content.append({
        "type": "text",
        "text": "以上を踏まえて、コーチングレポートを作成してください。",
    })

    with client.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=AI_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        return None
    return "".join(block.text for block in message.content if block.type == "text")


def generate_full_report(
    user_rank: str,
    metrics: MatchMetrics,
    target_benchmark: dict,
) -> dict:
    """ルールベース + （可能なら）AIコーチングを合わせた最終レポート。"""
    report = build_rule_based_report(user_rank, metrics, target_benchmark)
    try:
        ai_text = generate_ai_report(user_rank, metrics, report)
    except Exception as e:  # AI失敗時もルールベース結果は返す
        ai_text = None
        report["ai_error"] = str(e)
    report["ai_report"] = ai_text
    report["ai_enabled"] = _ai_available()
    return report
