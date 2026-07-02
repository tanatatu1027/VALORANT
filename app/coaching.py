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

from .knowledge_base import EVALUATION_AXES, METRIC_NOTES, get_focus_points
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


# ラウンド簡易評価の記号と意味
RATING_LABELS = {
    "◎": "とても良い",
    "○": "良い",
    "△": "微妙",
    "✖": "要改善",
}


def _rate_round(issue_count: int, first_engagement_sec: float | None,
                target_first: float) -> str:
    """検出された問題数と初動タイミングからラウンドを◎○△✖で簡易評価する。"""
    if issue_count >= 2:
        return "✖"
    if issue_count == 1:
        return "△"
    # 問題なし: 初動が上位帯の水準以上に規律的なら◎、それ以外は○
    if first_engagement_sec is not None and first_engagement_sec >= target_first * 0.8:
        return "◎"
    return "○"


def analyze_rounds(metrics: MatchMetrics, target: dict) -> list[dict]:
    """ラウンドごとの所見と簡易評価（◎○△✖）を生成する。"""
    results = []
    target_first = target.get("avg_first_engagement_sec", 30.0)
    for r in metrics.rounds:
        issues: list[str] = []
        if r.first_engagement_sec is not None and r.first_engagement_sec < target_first * 0.5:
            issues.append(
                f"開始{r.first_engagement_sec:.0f}秒で交戦が発生しています。"
                "初動が早すぎる可能性があります（上位帯の平均は"
                f"{target_first:.0f}秒前後）。"
            )
        if r.engagement_count == 0:
            issues.append(
                "交戦が検出されませんでした。ラウンドへの関与が薄い（寄り遅れ・芋り）か、"
                "早期に終了したラウンドの可能性があります。"
            )
        if r.engagement_count >= 8:
            issues.append(
                "交戦回数が非常に多いラウンドです。撃ち合いの選択（有利ポジション以外では"
                "引く判断）を見直す余地があります。"
            )
        if r.duration_sec < 50:
            issues.append("非常に短いラウンドです。ラッシュまたは早期崩壊の可能性があります。")

        rating = _rate_round(len(issues), r.first_engagement_sec, target_first)
        notes = issues if issues else ["特筆すべき問題は検出されませんでした。"]
        results.append({
            "round": r.index,
            "start_sec": r.start_sec,
            "end_sec": r.end_sec,
            "duration_sec": r.duration_sec,
            "engagement_count": r.engagement_count,
            "first_engagement_sec": r.first_engagement_sec,
            "rating": rating,
            "rating_label": RATING_LABELS[rating],
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
        "evaluation_axes": EVALUATION_AXES,
        "rounds": rounds,
        "metric_notes": METRIC_NOTES,
    }


# ---- Claude によるAIコーチング -------------------------------------------

AI_SYSTEM_PROMPT = """\
あなたはVALORANTのプロコーチです。プレイヤーが自分の試合動画をアップロードし、
自動解析による指標と、実際のプレー画面のスクリーンショットが渡されます。

# 基本方針
- プレイヤーの現在のランク帯と、目標である次のランク帯の差を埋める指導をする
- 撃ち合い（エイム）だけでなく立ち回りを同等以上に重視して評価する
- 良かった点と改善点の両方を必ず挙げる（改善点だけにしない）
- ラウンド番号を明示して指摘する（例: 「第3ラウンド: ...」）
- 抽象論ではなく「次の試合から実行できる」行動レベルのアドバイスにする
- プレイヤーは解説を読みながら同じ画面でアップロードした動画を再生できるため、
  ラウンド別コーチングでは動画内の時刻（例: 「3:20あたりを見返してください」）を添える
  （各ラウンドの start_sec が動画内の開始秒数）

# 評価軸（必ずこの観点で評価する）

## 立ち回り
1. ミニマップを見た動き:
   スクリーンショットの画面左上のミニマップから、味方の位置・人数配分・
   取れているエリアを読み取り、プレイヤーの立ち位置がその情報を反映した
   動きになっているかを評価する。味方から孤立していないか、人数不利で
   無理に前へ出ていないかを具体的に指摘する。
2. カバーライン:
   味方とトレードキルを取り合える距離・角度を保てているか。ミニマップ上の
   味方との間隔から、先頭が倒された時にカバーできる位置取りかを評価する。
3. スキルの使用方法:
   画面下部のHUDからスキルの残数を読み取り、使用状況を評価する。
   目的（情報取得・エリア確保・撃ち合い支援）が明確か、順番が正しいか
   （フラッシュ→ピーク、スモーク→展開、索敵→エントリー）、
   終盤・リテイク用に温存できているか。
4. エージェントの強みを活かす:
   HUD（左下のアビリティアイコン・体力バー付近の立ち絵）から使用エージェントを
   特定し、そのロールと固有の強みに沿った動きができているかを評価する。
   デュエリストならエントリー、イニシエーターなら索敵と味方支援、
   コントローラーなら射線管理、センチネルなら裏取り警戒と拠点維持。

## 撃ち合い
5. ピークの仕方とタイミング:
   情報取りのジグル/ショルダーピークと倒し切るワイドピークを使い分けているか。
   同じ角を同じ方法でリピークしていないか。スキルや味方の射線と同期して
   ピークできているか。
6. 撃ち方:
   クロスヘアの高さ（ヘッドラインに置けているか）、ストッピング、
   距離に応じたタップ/バースト/スプレーの使い分け、外した後の引き判断。
   スクリーンショットのクロスヘア位置と敵との距離感から具体的に指摘する。

# スクリーンショットの読み取り
各画像から読み取れる範囲で以下を確認し、指摘の根拠にする:
- 画面左上: ミニマップ（味方配置・エリア状況）
- 画面中央: クロスヘアの高さ・置き場所
- 画面下部: 体力・アーマー・スキル残数・弾数
- 画面上部: ラウンドスコア・時間・両チームの生存人数
- 武器と所持金が見える場面ではエコノミー判断も評価する
読み取れないことを推測で断定しない。画像から確認できた事実と、
指標からの推測を区別して書く。

# 出力形式
日本語のMarkdownで、以下の構成にすること:
## 総評
## 評価軸別の分析
（立ち回り: ミニマップ/カバーライン/スキル/エージェント適性、
  撃ち合い: ピーク/撃ち方 の6観点それぞれに ◎○△ の評価と根拠を1-3行）
## 良かった点
## 改善点（優先度順）
## ラウンド別コーチング
（各ラウンドの指標には自動解析による簡易評価 rating（◎○△✖）が付いている。
  評価の低いラウンド（△・✖）を優先して詳しく分析し、見出しに評価記号を付ける。
  例: 「第3ラウンド ✖ (3:20〜)」。自動評価が実際のプレー内容と食い違うと
  判断した場合は、根拠を添えて自分の評価に修正してよい）
## 次のランク帯に上がるための練習メニュー
（改善点に対応させ、デスマッチ・射撃場・カスタムなど具体的な練習方法で）
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
