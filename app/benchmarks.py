"""リファレンス動画（学習用動画）の管理とランク帯ベンチマーク。

プロ・上位ランクの動画を解析した結果をランクラベル付きでSQLiteに保存し、
ランク帯ごとの平均的な指標（ベンチマーク）を計算する。

利用者のランクに対しては「次のランク帯」のベンチマークと比較して
コーチングする。ゴールド帯の利用者が多い想定のため、README の通り
ダイヤモンド帯の動画を多めに学習させることを推奨している。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .ranks import normalize_rank, next_rank
from .video_analysis import MatchMetrics

# 学習動画が1本もないランク帯でもコーチングが機能するように、
# 上位帯の一般的なプレー傾向から置いた初期値。
# リファレンス動画を追加していくと実測値がこれを上書きする。
DEFAULT_BENCHMARKS: dict[str, dict[str, float]] = {
    "ゴールド":     {"avg_first_engagement_sec": 22.0, "avg_engagements_per_round": 4.5, "avg_round_duration_sec": 95.0},
    "プラチナ":     {"avg_first_engagement_sec": 26.0, "avg_engagements_per_round": 4.2, "avg_round_duration_sec": 100.0},
    "ダイヤモンド": {"avg_first_engagement_sec": 30.0, "avg_engagements_per_round": 4.0, "avg_round_duration_sec": 105.0},
    "アセンダント": {"avg_first_engagement_sec": 32.0, "avg_engagements_per_round": 3.8, "avg_round_duration_sec": 108.0},
    "イモータル":   {"avg_first_engagement_sec": 34.0, "avg_engagements_per_round": 3.6, "avg_round_duration_sec": 110.0},
    "レディアント": {"avg_first_engagement_sec": 35.0, "avg_engagements_per_round": 3.5, "avg_round_duration_sec": 110.0},
}
# 下位帯は隣接帯から補間
for _rank, _src in [("アイアン", "ゴールド"), ("ブロンズ", "ゴールド"), ("シルバー", "ゴールド")]:
    DEFAULT_BENCHMARKS[_rank] = dict(DEFAULT_BENCHMARKS[_src])

BENCHMARK_KEYS = [
    "avg_first_engagement_sec",
    "avg_engagements_per_round",
    "avg_round_duration_sec",
]


class BenchmarkStore:
    """リファレンス動画の解析結果を保存し、ランク帯ベンチマークを提供する。"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reference_videos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rank TEXT NOT NULL,
                    label TEXT,
                    metrics_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )

    def add_reference(self, rank: str, metrics: MatchMetrics, label: str = "") -> int:
        """学習動画の解析結果を保存する。"""
        r = normalize_rank(rank)
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO reference_videos (rank, label, metrics_json, created_at)"
                " VALUES (?, ?, ?, ?)",
                (r, label, json.dumps(metrics.to_dict()), time.time()),
            )
            return int(cur.lastrowid)

    def reference_counts(self) -> dict[str, int]:
        """ランク帯ごとの学習動画本数。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT rank, COUNT(*) AS n FROM reference_videos GROUP BY rank"
            ).fetchall()
        return {row["rank"]: row["n"] for row in rows}

    def get_benchmark(self, rank: str) -> dict:
        """指定ランク帯のベンチマーク指標を返す。

        学習動画があれば実測平均、なければ初期値を返す。
        戻り値には出典（learned/default）と本数も含む。
        """
        r = normalize_rank(rank)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT metrics_json FROM reference_videos WHERE rank = ?", (r,)
            ).fetchall()

        if rows:
            metrics_list = [json.loads(row["metrics_json"]) for row in rows]
            bench = {
                key: round(
                    sum(m.get(key, 0.0) for m in metrics_list) / len(metrics_list), 2
                )
                for key in BENCHMARK_KEYS
            }
            return {"rank": r, "source": "learned", "sample_count": len(rows), **bench}

        default = DEFAULT_BENCHMARKS.get(r, DEFAULT_BENCHMARKS["ダイヤモンド"])
        return {"rank": r, "source": "default", "sample_count": 0, **default}

    def get_target_benchmark(self, user_rank: str) -> dict:
        """利用者のランクに対する比較対象（次のランク帯）のベンチマーク。"""
        return self.get_benchmark(next_rank(user_rank))
