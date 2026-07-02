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
            # プレイヤーがコーチング依頼時にアップロードした動画の解析結果。
            # 管理者が承認すると reference_videos にコピーされ学習に使われる。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contributions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rank TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    video_name TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
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

    # ---- プレイヤー提供動画（学習候補）の管理 ----------------------------

    def add_contribution(
        self, rank: str, metrics: MatchMetrics, video_name: str | None = None
    ) -> int:
        """プレイヤーの解析結果を学習候補として保存する（status=pending）。"""
        r = normalize_rank(rank)
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO contributions (rank, metrics_json, video_name,"
                " status, created_at) VALUES (?, ?, ?, 'pending', ?)",
                (r, json.dumps(metrics.to_dict()), video_name, time.time()),
            )
            return int(cur.lastrowid)

    def list_contributions(self, status: str = "pending") -> list[dict]:
        """指定ステータスの学習候補を新しい順に返す。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, rank, video_name, status, created_at, metrics_json"
                " FROM contributions WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        result = []
        for row in rows:
            metrics = json.loads(row["metrics_json"])
            result.append({
                "id": row["id"],
                "rank": row["rank"],
                "video_name": row["video_name"],
                "status": row["status"],
                "created_at": row["created_at"],
                "round_count": metrics.get("round_count"),
                "avg_first_engagement_sec": metrics.get("avg_first_engagement_sec"),
                "avg_engagements_per_round": metrics.get("avg_engagements_per_round"),
            })
        return result

    def approve_contribution(self, contribution_id: int) -> dict:
        """学習候補を承認し、学習データ（reference_videos）に反映する。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT rank, metrics_json, status FROM contributions WHERE id = ?",
                (contribution_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"学習候補が見つかりません: {contribution_id}")
            if row["status"] != "pending":
                raise ValueError(f"この候補は処理済みです（status={row['status']}）")
            conn.execute(
                "INSERT INTO reference_videos (rank, label, metrics_json, created_at)"
                " VALUES (?, ?, ?, ?)",
                (row["rank"], "プレイヤー提供", row["metrics_json"], time.time()),
            )
            conn.execute(
                "UPDATE contributions SET status = 'approved' WHERE id = ?",
                (contribution_id,),
            )
        return {"id": contribution_id, "status": "approved", "rank": row["rank"]}

    def reject_contribution(self, contribution_id: int) -> dict:
        """学習候補を却下する。"""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE contributions SET status = 'rejected'"
                " WHERE id = ? AND status = 'pending'",
                (contribution_id,),
            )
            if cur.rowcount == 0:
                raise KeyError(f"未処理の学習候補が見つかりません: {contribution_id}")
        return {"id": contribution_id, "status": "rejected"}
