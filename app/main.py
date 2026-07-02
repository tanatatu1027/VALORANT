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
- POST /api/feedback                      プレイヤーからのご意見・ご要望を投稿
- POST /api/reference/upload      [管理者] 学習用（プロ・上位ランク）動画を登録
- POST /api/reference/youtube     [管理者] YouTubeのURLから学習動画を取り込む
- GET  /api/admin/check           [管理者] トークン確認
- GET  /api/admin/contributions   [管理者] プレイヤー提供動画（学習候補）の一覧
- POST /api/admin/contributions/{id}/approve  [管理者] 候補を学習に反映
- POST /api/admin/contributions/{id}/reject   [管理者] 候補を却下
- GET  /api/admin/feedback        [管理者] コメント一覧
- POST /api/admin/feedback/{id}/done          [管理者] コメントを対応済みにする
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
from pydantic import BaseModel

from .benchmarks import BenchmarkStore
from .coaching import generate_full_report
from .ranks import RANKS, normalize_rank
from .video_analysis import analyze_video
from .youtube import download_youtube_video, is_youtube_url

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


def _run_reference_job(
    job_id: str, video_path: Path, rank: str, label: str, licensed: bool
) -> None:
    """学習用動画の取り込みジョブ（別スレッドで実行）。"""
    try:
        _update_job(job_id, status="analyzing", progress=0.0)

        def on_progress(p: float) -> None:
            _update_job(job_id, progress=round(p, 3))

        # 学習用はキーフレーム不要（統計だけ取る）
        metrics = analyze_video(
            str(video_path), progress_cb=on_progress, collect_keyframes=False
        )
        ref_id = store.add_reference(
            rank, metrics, label=label, source="upload", licensed=licensed
        )
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
    licensed: bool = Form(False),
    x_admin_token: str | None = Header(default=None),
):
    """[管理者] 学習用のプロ・上位ランク動画を登録する（ダイヤ帯は多めに推奨）。

    licensed=True は権利者の許諾を得た動画。開発用の仮データは False のまま登録し、
    公開前に一括削除できる。
    """
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
        target=_run_reference_job,
        args=(job_id, video_path, rank_norm, label, licensed),
        daemon=True,
    ).start()
    return {"job_id": job_id}


def _run_youtube_reference_job(
    job_id: str, url: str, rank: str, label: str, licensed: bool
) -> None:
    """YouTube動画をダウンロードして学習に取り込むジョブ。"""
    video_path: Path | None = None
    try:
        _update_job(job_id, status="downloading", progress=0.0)

        def on_download(p: float) -> None:
            # ダウンロードが全体の40%、解析が残り60%
            _update_job(job_id, progress=round(p * 0.4, 3))

        video_path, title = download_youtube_video(url, UPLOAD_DIR, on_download)
        _update_job(job_id, status="analyzing", progress=0.4)

        def on_analyze(p: float) -> None:
            _update_job(job_id, progress=round(0.4 + p * 0.6, 3))

        metrics = analyze_video(
            str(video_path), progress_cb=on_analyze, collect_keyframes=False
        )
        ref_label = label or title
        ref_id = store.add_reference(
            rank, metrics, label=ref_label,
            source="youtube", source_url=url, licensed=licensed,
        )
        _update_job(
            job_id,
            status="done",
            progress=1.0,
            result={
                "reference_id": ref_id,
                "rank": rank,
                "title": title,
                "metrics": metrics.to_dict(),
                "reference_counts": store.reference_counts(),
            },
        )
    except Exception as e:
        _update_job(job_id, status="error", error=str(e))
    finally:
        if video_path is not None:
            video_path.unlink(missing_ok=True)


class YoutubeReferenceRequest(BaseModel):
    url: str
    rank: str
    label: str = ""
    licensed: bool = False


@app.post("/api/reference/youtube")
async def reference_from_youtube(
    req: YoutubeReferenceRequest,
    x_admin_token: str | None = Header(default=None),
):
    """[管理者] YouTubeのURLから学習動画を取り込む。"""
    _require_admin(x_admin_token)
    try:
        rank_norm = normalize_rank(req.rank)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not is_youtube_url(req.url):
        raise HTTPException(
            status_code=400,
            detail="YouTubeのURLを入力してください（例: https://www.youtube.com/watch?v=...）",
        )

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "type": "youtube_reference",
            "status": "queued",
            "progress": 0.0,
            "rank": rank_norm,
            "url": req.url,
        }
    threading.Thread(
        target=_run_youtube_reference_job,
        args=(job_id, req.url, rank_norm, req.label, req.licensed),
        daemon=True,
    ).start()
    return {"job_id": job_id}


@app.get("/api/admin/references")
async def list_references(x_admin_token: str | None = Header(default=None)):
    """[管理者] 学習データ一覧（出典・許諾状態付き）。"""
    _require_admin(x_admin_token)
    return {"references": store.list_references()}


@app.delete("/api/admin/references/{reference_id}")
async def delete_reference(
    reference_id: int,
    x_admin_token: str | None = Header(default=None),
):
    """[管理者] 学習データを1件削除する。"""
    _require_admin(x_admin_token)
    try:
        store.delete_reference(reference_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True, "reference_counts": store.reference_counts()}


class LicensedRequest(BaseModel):
    licensed: bool


@app.post("/api/admin/references/{reference_id}/licensed")
async def set_reference_licensed(
    reference_id: int,
    req: LicensedRequest,
    x_admin_token: str | None = Header(default=None),
):
    """[管理者] 学習データの許諾状態を変更する（後から許可が取れた場合など）。"""
    _require_admin(x_admin_token)
    try:
        store.set_reference_licensed(reference_id, req.licensed)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True, "id": reference_id, "licensed": req.licensed}


@app.post("/api/admin/references/purge-unlicensed")
async def purge_unlicensed(x_admin_token: str | None = Header(default=None)):
    """[管理者] 未許諾（開発用）の学習データを一括削除する。公開前のリセット用。"""
    _require_admin(x_admin_token)
    deleted = store.purge_unlicensed_references()
    return {"deleted": deleted, "reference_counts": store.reference_counts()}


class FeedbackRequest(BaseModel):
    message: str
    rank: str = ""


@app.post("/api/feedback")
async def post_feedback(req: FeedbackRequest):
    """プレイヤーからのご意見・ご要望を受け付ける。"""
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="コメントを入力してください")
    if len(message) > 2000:
        raise HTTPException(status_code=400, detail="コメントは2000文字以内でお願いします")
    feedback_id = store.add_feedback(message, req.rank or None)
    return {"id": feedback_id, "ok": True}


@app.get("/api/admin/feedback")
async def list_feedback(
    status: str = "new",
    x_admin_token: str | None = Header(default=None),
):
    """[管理者] プレイヤーからのコメント一覧（status=new/done/all）。"""
    _require_admin(x_admin_token)
    return {"feedback": store.list_feedback(None if status == "all" else status)}


@app.post("/api/admin/feedback/{feedback_id}/done")
async def mark_feedback_done(
    feedback_id: int,
    x_admin_token: str | None = Header(default=None),
):
    """[管理者] コメントを対応済みにする。"""
    _require_admin(x_admin_token)
    try:
        return store.mark_feedback_done(feedback_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


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
