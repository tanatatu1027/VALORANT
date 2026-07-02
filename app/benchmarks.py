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
                    created_at REAL NOT NULL,
                    source TEXT NOT NULL DEFAULT 'upload',
                    source_url TEXT,
                    licensed INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # 既存DBへのマイグレーション（列がなければ追加）
            cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(reference_videos)")
            }
            if "source" not in cols:
                conn.execute(
                    "ALTER TABLE reference_videos ADD COLUMN"
                    " source TEXT NOT NULL DEFAULT 'upload'"
                )
            if "source_url" not in cols:
                conn.execute("ALTER TABLE reference_videos ADD COLUMN source_url TEXT")
            if "licensed" not in cols:
                conn.execute(
                    "ALTER TABLE reference_videos ADD COLUMN"
                    " licensed INTEGER NOT NULL DEFAULT 0"
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
            # プレイヤーからのご意見・ご要望。サイト改善の材料として管理者が確認する。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message TEXT NOT NULL,
                    rank TEXT,
                    status TEXT NOT NULL DEFAULT 'new',
                    created_at REAL NOT NULL
                )
                """
            )

    def add_reference(
        self,
        rank: str,
        metrics: MatchMetrics,
        label: str = "",
        source: str = "upload",
        source_url: str | None = None,
        licensed: bool = False,
    ) -> int:
        """学習動画の解析結果を保存する。

        source: 'upload'（管理者アップロード） / 'youtube' / 'player'（プレイヤー提供）
        licensed: 権利者の許諾を得ているか（開発用の仮データは False）
        """
        r = normalize_rank(rank)
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO reference_videos"
                " (rank, label, metrics_json, created_at, source, source_url, licensed)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    r, label, json.dumps(metrics.to_dict()), time.time(),
                    source, source_url, 1 if licensed else 0,
                ),
            )
            return int(cur.lastrowid)

    def list_references(self) -> list[dict]:
        """学習データ一覧（新しい順、出典・許諾状態付き）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, rank, label, created_at, source, source_url, licensed,"
                " metrics_json FROM reference_videos ORDER BY created_at DESC"
            ).fetchall()
        result = []
        for row in rows:
            metrics = json.loads(row["metrics_json"])
            result.append({
                "id": row["id"],
                "rank": row["rank"],
                "label": row["label"],
                "created_at": row["created_at"],
                "source": row["source"],
                "source_url": row["source_url"],
                "licensed": bool(row["licensed"]),
                "round_count": metrics.get("round_count"),
            })
        return result

    def delete_reference(self, reference_id: int) -> None:
        """学習データを1件削除する。"""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM reference_videos WHERE id = ?", (reference_id,)
            )
            if cur.rowcount == 0:
                raise KeyError(f"学習データが見つかりません: {reference_id}")

    def set_reference_licensed(self, reference_id: int, licensed: bool) -> None:
        """学習データの許諾状態を変更する（後から許可が取れた場合など）。"""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE reference_videos SET licensed = ? WHERE id = ?",
                (1 if licensed else 0, reference_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"学習データが見つかりません: {reference_id}")

    def purge_unlicensed_references(self) -> int:
        """未許諾（開発用）の学習データを一括削除し、削除件数を返す。

        公開前に開発用の仮データだけを消して、許諾済みデータを残すために使う。
        """
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM reference_videos WHERE licensed = 0")
            return cur.rowcount

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
            # プレイヤーはアップロード時に学習利用へ同意しているため許諾済み扱い
            conn.execute(
                "INSERT INTO reference_videos"
                " (rank, label, metrics_json, created_at, source, source_url, licensed)"
                " VALUES (?, ?, ?, ?, 'player', NULL, 1)",
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

    # ---- プレイヤーからのご意見・ご要望 -----------------------------------

    def add_feedback(self, message: str, rank: str | None = None) -> int:
        """プレイヤーのコメントを保存する。rank は任意（正規化できなければ捨てる）。"""
        rank_norm: str | None = None
        if rank:
            try:
                rank_norm = normalize_rank(rank)
            except ValueError:
                rank_norm = None
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO feedback (message, rank, status, created_at)"
                " VALUES (?, ?, 'new', ?)",
                (message, rank_norm, time.time()),
            )
            return int(cur.lastrowid)

    def list_feedback(self, status: str | None = "new") -> list[dict]:
        """コメント一覧（新しい順）。status=None で全件。"""
        with self._connect() as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM feedback ORDER BY created_at DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM feedback WHERE status = ? ORDER BY created_at DESC",
                    (status,),
                ).fetchall()
        return [dict(row) for row in rows]

    def mark_feedback_done(self, feedback_id: int) -> dict:
        """コメントを対応済みにする。"""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE feedback SET status = 'done' WHERE id = ? AND status = 'new'",
                (feedback_id,),
            )
            if cur.rowcount == 0:
                raise KeyError(f"未対応のコメントが見つかりません: {feedback_id}")
        return {"id": feedback_id, "status": "done"}
