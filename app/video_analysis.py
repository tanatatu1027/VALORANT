"""動画解析パイプライン。

1試合分（30分〜1時間程度）のVALORANTプレイ動画から、
ラウンド区切りとラウンドごとの指標を抽出する。

方針:
- 1fps程度のサンプリングで軽量に処理する（60分動画で約3600フレーム）
- ラウンド検出はシーンチェンジ（バイフェーズ遷移・スコア画面）を
  ヒストグラム差分で検出し、VALORANTのラウンド長の制約
  （最短~45秒、最長~200秒）でフィルタする
- キルフィード領域（画面右上）の赤/緑ハイライトから交戦イベントを推定する
- ラウンドごとに代表フレームを保存し、AIコーチング（画像解析）に使う
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field, asdict
from typing import Callable

import cv2
import numpy as np

# ---- チューニング用定数 -------------------------------------------------

SAMPLE_FPS = 1.0            # 解析時のサンプリングレート
MIN_ROUND_SEC = 45.0        # ラウンドの最短長（バイフェーズ含む）
MAX_ROUND_SEC = 200.0       # ラウンドの最長長（スパイク設置込み）
SCENE_CHANGE_PERCENTILE = 92  # シーンチェンジとみなす差分スコアの百分位
SCENE_CHANGE_MIN_SCORE = 0.35  # 通常のプレー中の画面変化を境界候補にしないための下限
EVENT_GAP_SEC = 4.0         # キルフィードイベントを同一交戦とみなす間隔
MAX_KEYFRAMES_PER_ROUND = 2  # AIに渡す代表フレーム数/ラウンド
KEYFRAME_MAX_WIDTH = 960    # 代表フレームの最大幅（トークン節約のため縮小）


# ---- データ構造 ---------------------------------------------------------

@dataclass
class RoundMetrics:
    """1ラウンド分の指標。"""
    index: int                  # 1始まりのラウンド番号
    start_sec: float
    end_sec: float
    duration_sec: float
    engagement_count: int       # 検出した交戦（キルフィード活動）の回数
    first_engagement_sec: float | None  # ラウンド開始からの最初の交戦までの秒数
    activity_ratio: float       # 交戦フレーム比率（0-1）

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MatchMetrics:
    """1試合分の指標。"""
    duration_sec: float
    round_count: int
    rounds: list[RoundMetrics] = field(default_factory=list)
    avg_round_duration_sec: float = 0.0
    avg_engagements_per_round: float = 0.0
    avg_first_engagement_sec: float = 0.0
    # ラウンド番号 -> base64 JPEGのリスト（AIコーチング用、保存対象外にできる）
    keyframes: dict[int, list[str]] = field(default_factory=dict)

    def to_dict(self, include_keyframes: bool = False) -> dict:
        d = {
            "duration_sec": self.duration_sec,
            "round_count": self.round_count,
            "rounds": [r.to_dict() for r in self.rounds],
            "avg_round_duration_sec": self.avg_round_duration_sec,
            "avg_engagements_per_round": self.avg_engagements_per_round,
            "avg_first_engagement_sec": self.avg_first_engagement_sec,
        }
        if include_keyframes:
            d["keyframes"] = self.keyframes
        return d


# ---- 純粋ロジック（テスト対象） -----------------------------------------

def segment_rounds(
    change_scores: list[float],
    timestamps: list[float],
    min_len: float = MIN_ROUND_SEC,
    max_len: float = MAX_ROUND_SEC,
    percentile: float = SCENE_CHANGE_PERCENTILE,
) -> list[tuple[float, float]]:
    """シーンチェンジスコアからラウンド区間 (start_sec, end_sec) を推定する。

    大きなシーンチェンジ（バイフェーズ遷移・リザルト画面）を境界候補とし、
    ラウンド長の制約で候補を間引く。境界が少なすぎる場合は
    max_len ごとの機械分割でフォールバックする。
    """
    if not timestamps:
        return []
    total = timestamps[-1]
    if total <= min_len:
        return [(0.0, total)]

    scores = np.asarray(change_scores, dtype=np.float64)
    # パーセンタイル閾値と絶対下限の両方を満たすフレームだけを境界候補にする。
    # 平坦な信号（通常プレーのみ）でパーセンタイル閾値が下がりすぎるのを防ぐ。
    pct = np.percentile(scores, percentile) if len(scores) else 0.0
    threshold = max(pct, SCENE_CHANGE_MIN_SCORE)
    candidates = [
        timestamps[i] for i in range(len(scores)) if scores[i] >= threshold
    ]

    boundaries = [0.0]
    for t in candidates:
        if t - boundaries[-1] >= min_len:
            boundaries.append(t)
    if total - boundaries[-1] >= min_len:
        boundaries.append(total)
    else:
        boundaries[-1] = total

    # 長すぎる区間は max_len で機械分割（境界検出漏れへの保険）
    segments: list[tuple[float, float]] = []
    for start, end in zip(boundaries, boundaries[1:]):
        span = end - start
        if span <= max_len:
            segments.append((start, end))
        else:
            n = int(np.ceil(span / max_len))
            step = span / n
            for k in range(n):
                segments.append((start + k * step, start + (k + 1) * step))
    return segments


def group_events(
    flags: list[bool],
    timestamps: list[float],
    gap: float = EVENT_GAP_SEC,
) -> list[float]:
    """交戦フラグの列を、gap秒以内をまとめてイベント開始時刻のリストにする。"""
    events: list[float] = []
    last: float | None = None
    for flag, t in zip(flags, timestamps):
        if not flag:
            continue
        if last is None or t - last > gap:
            events.append(t)
        last = t
    return events


def build_match_metrics(
    segments: list[tuple[float, float]],
    event_times: list[float],
    activity_flags: list[bool],
    timestamps: list[float],
    duration_sec: float,
) -> MatchMetrics:
    """区間・イベント情報から MatchMetrics を組み立てる。"""
    rounds: list[RoundMetrics] = []
    for i, (start, end) in enumerate(segments, start=1):
        in_round_events = [t for t in event_times if start <= t < end]
        in_round_flags = [
            f for f, t in zip(activity_flags, timestamps) if start <= t < end
        ]
        first = (in_round_events[0] - start) if in_round_events else None
        ratio = (sum(in_round_flags) / len(in_round_flags)) if in_round_flags else 0.0
        rounds.append(
            RoundMetrics(
                index=i,
                start_sec=round(start, 1),
                end_sec=round(end, 1),
                duration_sec=round(end - start, 1),
                engagement_count=len(in_round_events),
                first_engagement_sec=round(first, 1) if first is not None else None,
                activity_ratio=round(ratio, 3),
            )
        )

    m = MatchMetrics(
        duration_sec=round(duration_sec, 1),
        round_count=len(rounds),
        rounds=rounds,
    )
    if rounds:
        m.avg_round_duration_sec = round(
            float(np.mean([r.duration_sec for r in rounds])), 1
        )
        m.avg_engagements_per_round = round(
            float(np.mean([r.engagement_count for r in rounds])), 2
        )
        firsts = [r.first_engagement_sec for r in rounds if r.first_engagement_sec is not None]
        if firsts:
            m.avg_first_engagement_sec = round(float(np.mean(firsts)), 1)
    return m


# ---- OpenCVを使う実処理 -------------------------------------------------

def _histogram_diff(prev_hist: np.ndarray, cur_hist: np.ndarray) -> float:
    """2フレームのHSVヒストグラム距離（0=同一、大きいほど変化大）。"""
    return float(cv2.compareHist(prev_hist, cur_hist, cv2.HISTCMP_BHATTACHARYYA))


def _frame_hist(frame: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    return hist


def _killfeed_activity(frame: np.ndarray) -> bool:
    """画面右上のキルフィード領域に赤/緑のハイライトがあるか判定する。"""
    h, w = frame.shape[:2]
    region = frame[int(h * 0.03):int(h * 0.30), int(w * 0.70):int(w * 0.99)]
    if region.size == 0:
        return False
    hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
    # 赤系（キルフィードの敵ハイライト）
    red1 = cv2.inRange(hsv, (0, 120, 90), (8, 255, 255))
    red2 = cv2.inRange(hsv, (170, 120, 90), (180, 255, 255))
    # 緑〜シアン系（味方側ハイライト）
    green = cv2.inRange(hsv, (55, 100, 90), (95, 255, 255))
    total = region.shape[0] * region.shape[1]
    ratio = (int(np.count_nonzero(red1)) + int(np.count_nonzero(red2))
             + int(np.count_nonzero(green))) / total
    return ratio > 0.004


def _encode_keyframe(frame: np.ndarray) -> str:
    h, w = frame.shape[:2]
    if w > KEYFRAME_MAX_WIDTH:
        scale = KEYFRAME_MAX_WIDTH / w
        frame = cv2.resize(frame, (KEYFRAME_MAX_WIDTH, int(h * scale)))
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
    if not ok:
        return ""
    return base64.standard_b64encode(buf.tobytes()).decode("ascii")


def analyze_video(
    video_path: str,
    sample_fps: float = SAMPLE_FPS,
    progress_cb: Callable[[float], None] | None = None,
    collect_keyframes: bool = True,
) -> MatchMetrics:
    """動画を解析して試合指標を返す。

    progress_cb には 0.0-1.0 の進捗が渡される。
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"動画を開けませんでした: {video_path}")

    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration_sec = frame_count / src_fps if frame_count else 0.0
    step = max(1, int(round(src_fps / sample_fps)))

    timestamps: list[float] = []
    change_scores: list[float] = []
    activity_flags: list[bool] = []

    prev_hist: np.ndarray | None = None
    idx = 0
    while True:
        ret = cap.grab()
        if not ret:
            break
        if idx % step == 0:
            ret, frame = cap.retrieve()
            if not ret:
                break
            t = idx / src_fps
            timestamps.append(t)

            small = cv2.resize(frame, (320, 180))
            hist = _frame_hist(small)
            change_scores.append(
                _histogram_diff(prev_hist, hist) if prev_hist is not None else 0.0
            )
            prev_hist = hist

            activity_flags.append(_killfeed_activity(frame))
            if progress_cb and frame_count:
                progress_cb(min(idx / frame_count, 1.0))
        idx += 1
    cap.release()

    if not timestamps:
        raise ValueError("動画からフレームを読み取れませんでした")
    if duration_sec <= 0:
        duration_sec = timestamps[-1]

    segments = segment_rounds(change_scores, timestamps)
    event_times = group_events(activity_flags, timestamps)
    metrics = build_match_metrics(
        segments, event_times, activity_flags, timestamps, duration_sec
    )

    if collect_keyframes:
        _collect_keyframes(video_path, src_fps, metrics, timestamps, activity_flags)

    if progress_cb:
        progress_cb(1.0)
    return metrics


def _collect_keyframes(
    video_path: str,
    src_fps: float,
    metrics: MatchMetrics,
    timestamps: list[float],
    activity_flags: list[bool],
) -> None:
    """2回目のパスで、各ラウンドの代表フレームだけをシークして取得する。

    1回目のパスで全フレームを保持するとメモリを圧迫するため、
    必要な時刻にだけシークして読み出す。
    """
    # ラウンドごとに取得したい時刻を決める（交戦フレーム優先）
    wanted: list[tuple[int, float]] = []  # (round_index, sec)
    for r in metrics.rounds:
        in_round = [
            t for t, f in zip(timestamps, activity_flags)
            if r.start_sec <= t < r.end_sec and f
        ]
        picks = in_round[:MAX_KEYFRAMES_PER_ROUND]
        if not picks:
            picks = [(r.start_sec + r.end_sec) / 2]
        wanted.extend((r.index, sec) for sec in picks)

    if not wanted:
        return
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return
    try:
        for round_index, sec in wanted:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(sec * src_fps))
            ret, frame = cap.read()
            if not ret:
                continue
            encoded = _encode_keyframe(frame)
            if encoded:
                metrics.keyframes.setdefault(round_index, []).append(encoded)
    finally:
        cap.release()
