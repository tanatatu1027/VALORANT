"""VALORANT AIコーチング Webアプリのエントリポイント。

起動方法:
    uvicorn app.main:app --host 0.0.0.0 --port 8000

管理者機能（学習動画の登録・プレイヤー提供動画の承認）は
環境変数 VALO_COACH_ADMIN_TOKEN のトークンで保護される。
未設定の場合は起動時に自動生成してコンソールに表示する。

エンドポイント:
- POST /api/analyze                       自分の動画をアップロードしてコーチングを依頼
- GET  /api/jobs/{job_id}                 解析ジョブの進捗・結果を取得
- GET  /videos/{video_name}               アップロードした動画の再生（解説と併用）
- GET  /api/benchmarks                    現在のベンチマークと学習本数を確認
- POST /api/reference/upload      [管理者] 学習用（プロ・上位ランク）動画を登録
- GET  /api/admin/check           [管理者] トークン確認
- GET  /api/admin/contributions   [管理者] プレイヤー提供動画（学習候補）の一覧
- POST /api/admin/contributions/{id}/approve  [管理者] 候補を学習に反映
- POST /api/admin/contributions/{id}/reject   [管理者] 候補を却下
- GET  /                                  Web UI
"""

from __future__ import annotations

import hmac
import os
import secrets
import shutil
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .benchmarks import BenchmarkStore
from .coaching import generate_full_report
from .ranks import RANKS, normalize_rank
from .video_analysis import analyze_video

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "benchmarks.db"
STATIC_DIR = BASE_DIR / "static"

DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)

# 管理者トークン: 環境変数がなければ起動ごとに自動生成して表示する
ADMIN_TOKEN = os.environ.get("VALO_COACH_ADMIN_TOKEN")
if not ADMIN_TOKEN:
    ADMIN_TOKEN = secrets.token_urlsafe(16)
    print(f"[VALORANT AIコーチ] 管理者トークン（自動生成）: {ADMIN_TOKEN}")
    print("  固定したい場合は環境変数 VALO_COACH_ADMIN_TOKEN を設定してください")

app = FastAPI(title="VALORANT AI Coach")
store = BenchmarkStore(DB_PATH)

# ジョブ管理（プロセス内メモリ）。プロダクションではRedis等に置き換える。
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}


def _require_admin(token: str | None) -> None:
    if not token or not hmac.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="管理者トークンが正しくありません")


def _update_job(job_id: str, **kwargs) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)


def _save_upload(file: UploadFile) -> Path:
    suffix = Path(file.filename or "video.mp4").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"対応していない動画形式です: {suffix}（対応: {', '.join(sorted(ALLOWED_EXTENSIONS))}）",
        )
    dest = UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return dest


def _run_analysis_job(job_id: str, video_path: Path, rank: str) -> None:
    """利用者動画の解析ジョブ（別スレッドで実行）。

    解析後も動画は残し、結果画面で再生しながら解説を確認できるようにする。
    解析結果は学習候補（contribution）として保存し、管理者の承認後に
    ベンチマーク学習へ反映される。
    """
    try:
        _update_job(job_id, status="analyzing", progress=0.0)

        def on_progress(p: float) -> None:
            # 解析が全体の80%、AIレポート生成が残り20%という配分で進捗表示
            _update_job(job_id, progress=round(p * 0.8, 3))

        metrics = analyze_video(str(video_path), progress_cb=on_progress)
        _update_job(job_id, status="coaching", progress=0.85)

        target_benchmark = store.get_target_benchmark(rank)
        report = generate_full_report(rank, metrics, target_benchmark)
        report["video_url"] = f"/videos/{video_path.name}"

        # 今後の学習用に候補として保存（管理者承認後に反映）
        contribution_id = store.add_contribution(rank, metrics, video_path.name)
        report["contribution_id"] = contribution_id

        _update_job(job_id, status="done", progress=1.0, result=report)
    except Exception as e:
        _update_job(job_id, status="error", error=str(e))
        video_path.unlink(missing_ok=True)


def _run_reference_job(job_id: str, video_path: Path, rank: str, label: str) -> None:
    """学習用動画の取り込みジョブ（別スレッドで実行）。"""
    try:
        _update_job(job_id, status="analyzing", progress=0.0)

        def on_progress(p: float) -> None:
            _update_job(job_id, progress=round(p, 3))

        # 学習用はキーフレーム不要（統計だけ取る）
        metrics = analyze_video(
            str(video_path), progress_cb=on_progress, collect_keyframes=False
        )
        ref_id = store.add_reference(rank, metrics, label=label)
        _update_job(
            job_id,
            status="done",
            progress=1.0,
            result={
                "reference_id": ref_id,
                "rank": rank,
                "metrics": metrics.to_dict(),
                "reference_counts": store.reference_counts(),
            },
        )
    except Exception as e:
        _update_job(job_id, status="error", error=str(e))
    finally:
        video_path.unlink(missing_ok=True)


@app.post("/api/analyze")
async def analyze(
    file: UploadFile = File(...),
    rank: str = Form(...),
):
    """自分の試合動画（1試合分）とランクを送ってコーチングを依頼する。"""
    try:
        rank_norm = normalize_rank(rank)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    video_path = _save_upload(file)
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "type": "analyze",
            "status": "queued",
            "progress": 0.0,
            "rank": rank_norm,
        }
    threading.Thread(
        target=_run_analysis_job, args=(job_id, video_path, rank_norm), daemon=True
    ).start()
    return {"job_id": job_id}


@app.post("/api/reference/upload")
async def upload_reference(
    file: UploadFile = File(...),
    rank: str = Form(...),
    label: str = Form(""),
    x_admin_token: str | None = Header(default=None),
):
    """[管理者] 学習用のプロ・上位ランク動画を登録する（ダイヤ帯は多めに推奨）。"""
    _require_admin(x_admin_token)
    try:
        rank_norm = normalize_rank(rank)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    video_path = _save_upload(file)
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "type": "reference",
            "status": "queued",
            "progress": 0.0,
            "rank": rank_norm,
        }
    threading.Thread(
        target=_run_reference_job, args=(job_id, video_path, rank_norm, label), daemon=True
    ).start()
    return {"job_id": job_id}


@app.get("/api/admin/check")
async def admin_check(x_admin_token: str | None = Header(default=None)):
    """[管理者] トークンの有効性チェック（管理UIのログインに使う）。"""
    _require_admin(x_admin_token)
    return {"ok": True}


@app.get("/api/admin/contributions")
async def list_contributions(
    status: str = "pending",
    x_admin_token: str | None = Header(default=None),
):
    """[管理者] プレイヤー提供動画（学習候補）の一覧。"""
    _require_admin(x_admin_token)
    return {"contributions": store.list_contributions(status)}


@app.post("/api/admin/contributions/{contribution_id}/approve")
async def approve_contribution(
    contribution_id: int,
    x_admin_token: str | None = Header(default=None),
):
    """[管理者] 学習候補を承認してベンチマーク学習に反映する。"""
    _require_admin(x_admin_token)
    try:
        result = store.approve_contribution(contribution_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    result["reference_counts"] = store.reference_counts()
    return result


@app.post("/api/admin/contributions/{contribution_id}/reject")
async def reject_contribution(
    contribution_id: int,
    x_admin_token: str | None = Header(default=None),
):
    """[管理者] 学習候補を却下する。"""
    _require_admin(x_admin_token)
    try:
        return store.reject_contribution(contribution_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="ジョブが見つかりません")
    return JSONResponse(job)


@app.get("/api/benchmarks")
async def get_benchmarks():
    """全ランク帯のベンチマークと学習動画本数。"""
    counts = store.reference_counts()
    return {
        "ranks": RANKS,
        "reference_counts": counts,
        "benchmarks": {r: store.get_benchmark(r) for r in RANKS},
    }


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


# アップロード動画の再生（Rangeリクエスト対応のためStaticFilesで配信）
app.mount("/videos", StaticFiles(directory=UPLOAD_DIR), name="videos")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
