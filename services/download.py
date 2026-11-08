"""Download a source video from a link with yt-dlp."""
import shutil
import subprocess
import sys
from pathlib import Path

from services.video import duration

SKIP_SUFFIXES = {".part", ".ytdl", ".temp", ".json"}


def fresh_dir(out_dir: Path) -> Path:
    """Empty the per-chat download folder so an old video is never reused."""
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def check_video(path: Path) -> float:
    """Raise if the file is not a playable video; return its duration in seconds."""
    if not path.exists() or path.stat().st_size < 10_000:
        raise RuntimeError("فایل ویدئو خالی یا ناقص است.")
    try:
        secs = duration(path)
    except Exception as e:
        raise RuntimeError(f"فایل ویدئو قابل خواندن نیست: {e}")
    if secs <= 0:
        raise RuntimeError("طول ویدئو صفر است؛ فایل ناقص دانلود شده.")
    return secs


def download_video(url: str, out_dir: Path) -> Path:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg پیدا نشد؛ yt-dlp بدون آن نمی‌تواند صدا و تصویر را یکی کند.")
    fresh_dir(out_dir)
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--merge-output-format", "mp4",
        "--no-playlist", "--no-part", "--force-overwrites",
        "-o", str(out_dir / "source.%(ext)s"), url,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError("yt-dlp failed:\n" + r.stderr[-800:])
    # the merged file is named source.<ext>; per-format pieces (source.f137.mp4) are leftovers
    files = [f for f in out_dir.glob("source.*")
             if f.suffix not in SKIP_SUFFIXES and f.name.count(".") == 1]
    if not files:
        raise RuntimeError("yt-dlp produced no file:\n" + (r.stderr or r.stdout)[-800:])
    path = max(files, key=lambda f: f.stat().st_size)
    check_video(path)
    return path
