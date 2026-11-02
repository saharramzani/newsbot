"""Download a source video from a link with yt-dlp."""
import subprocess
import sys
from pathlib import Path


def download_video(url: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    tmpl = str(out_dir / "source.%(ext)s")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--merge-output-format", "mp4",
        "--no-playlist", "-o", tmpl, url,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError("yt-dlp failed:\n" + r.stderr[-800:])
    files = sorted(out_dir.glob("source.*"))
    if not files:
        raise RuntimeError("yt-dlp produced no file")
    return files[0]
