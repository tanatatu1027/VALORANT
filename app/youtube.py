"""YouTubeからの学習動画取得。

管理者がURLを指定すると yt-dlp で動画をダウンロードし、
通常のアップロード動画と同じ解析パイプラインに流す。
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Callable

# 対応するYouTube URL（動画・ショート・ライブアーカイブ・短縮URL）
YOUTUBE_URL_RE = re.compile(
    r"^https?://(www\.|m\.)?(youtube\.com/(watch\?|shorts/|live/)|youtu\.be/)",
    re.IGNORECASE,
)

# 解析には1080pあれば十分。単一ファイル形式を選び、ffmpeg依存の結合を避ける。
YTDLP_FORMAT = "best[height<=1080][ext=mp4]/best[height<=1080]/best"

MAX_DURATION_SEC = 2 * 60 * 60  # 2時間を超える動画は拒否（1試合分を想定）


def is_youtube_url(url: str) -> bool:
    return bool(YOUTUBE_URL_RE.match(url.strip()))


def download_youtube_video(
    url: str,
    dest_dir: str | Path,
    progress_cb: Callable[[float], None] | None = None,
) -> tuple[Path, str]:
    """YouTube動画をダウンロードし、(保存パス, 動画タイトル) を返す。

    progress_cb には 0.0-1.0 のダウンロード進捗が渡される。
    """
    if not is_youtube_url(url):
        raise ValueError("YouTubeのURLではありません")

    import yt_dlp

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    basename = f"yt_{uuid.uuid4().hex}"

    def hook(d: dict) -> None:
        if progress_cb and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes")
            if total and downloaded:
                progress_cb(min(downloaded / total, 1.0))

    opts = {
        "format": YTDLP_FORMAT,
        "outtmpl": str(dest_dir / f"{basename}.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
        "match_filter": yt_dlp.utils.match_filter_func(
            f"duration <= {MAX_DURATION_SEC}"
        ),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                raise ValueError("動画情報を取得できませんでした（動画が長すぎるか非公開の可能性）")
            path = Path(ydl.prepare_filename(info))
    except yt_dlp.utils.DownloadError as e:
        raise ValueError(
            "YouTubeから動画を取得できませんでした。URLが正しいか、公開動画かを確認してください"
            f"（詳細: {str(e)[:200]}）"
        ) from e

    if not path.exists():
        # match_filter でスキップされた場合など
        raise ValueError("動画をダウンロードできませんでした（2時間以内の公開動画か確認してください）")

    if progress_cb:
        progress_cb(1.0)
    return path, str(info.get("title") or "")
