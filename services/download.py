"""Download a source video from a link with yt-dlp."""
import shutil
import threading
import time
import subprocess
import sys
from pathlib import Path

import config
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


_insta_lock = threading.Lock()
_insta_last = [0.0]
INSTA_GAP = 8          # seconds between two Instagram downloads (bursts trigger HTTP 429)
RETRY_WAITS = (30, 90)  # waits before retrying after a 429


def clean_url(url: str) -> str:
    """Instagram share links carry tracking parameters (?igsh=, ?xtok=...) that are not needed."""
    if "instagram.com" in url:
        return url.split("?", 1)[0].split("#", 1)[0]
    return url


def download_video(url: str, out_dir: Path) -> Path:
    url = clean_url(url)
    if "instagram.com" not in url:
        return _download(url, out_dir)
    # one Instagram download at a time, spaced out; on 429 wait and try again
    last = None
    for attempt in range(len(RETRY_WAITS) + 1):
        with _insta_lock:
            wait = _insta_last[0] + INSTA_GAP - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                return _download(url, out_dir)
            except RuntimeError as e:
                last = e
                if "429" not in str(e) and "too many requests" not in str(e).lower():
                    raise
            finally:
                _insta_last[0] = time.monotonic()
        if attempt < len(RETRY_WAITS):
            time.sleep(RETRY_WAITS[attempt])
    raise last


def _download(url: str, out_dir: Path) -> Path:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg پیدا نشد؛ yt-dlp بدون آن نمی‌تواند صدا و تصویر را یکی کند.")
    fresh_dir(out_dir)
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--merge-output-format", "mp4",
        "--no-playlist", "--no-part", "--force-overwrites", "--extractor-retries", "2",
        "-o", str(out_dir / "source.%(ext)s"), url,
    ]
    cookies = Path(config.YTDLP_COOKIES)
    if cookies.is_file():  # logged-in browser cookies: Instagram (and YouTube on servers) need them
        cmd[3:3] = ["--cookies", str(cookies)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(_hint(url, r.stderr) + "yt-dlp failed:\n" + r.stderr[-600:])
    # the merged file is named source.<ext>; per-format pieces (source.f137.mp4) are leftovers
    files = [f for f in out_dir.glob("source.*")
             if f.suffix not in SKIP_SUFFIXES and f.name.count(".") == 1]
    if not files:
        raise RuntimeError("yt-dlp produced no file:\n" + (r.stderr or r.stdout)[-800:])
    path = max(files, key=lambda f: f.stat().st_size)
    check_video(path)
    return path


LOGIN_SIGNS = ("login required", "rate-limit reached", "empty media response", "sign in to confirm",
               "not a bot", "cookies", "private", "401", "403")


def _hint(url: str, stderr: str) -> str:
    """A Persian explanation for the common 'needs login' failures, shown above yt-dlp's own message."""
    err = stderr.lower()
    if "429" in err or "too many requests" in err:
        return ("👉 اینستاگرام موقتاً جلوی درخواست‌های سرور را گرفته (درخواست زیاد در زمان کوتاه). ربات چند بار با فاصله "
                "دوباره امتحان کرد. چند دقیقه بعد دوباره بفرستید، یا ویدئو را خودتان دانلود و در تلگرام آپلود کنید.\n\n")
    if not any(s in err for s in LOGIN_SIGNS):
        return ""
    site = "اینستاگرام" if "instagram" in url else "یوتیوب" if ("youtu" in url) else "این سایت"
    have = Path(config.YTDLP_COOKIES).is_file()
    if have:
        return (f"👉 {site} دانلود را رد کرد، با اینکه فایل کوکی هست؛ احتمالاً کوکی‌ها منقضی شده‌اند یا حساب محدود شده. "
                "کوکی‌ها را دوباره از مرورگر بگیرید (راهنما در README).\n\n")
    return (f"👉 {site} برای دانلود لاگین می‌خواهد. باید فایل کوکی یک حساب را به ربات بدهید (راهنما در README، بخش "
            "«دانلود از اینستاگرام»). تا آن موقع می‌توانید خود ویدئو را دانلود و در تلگرام آپلود کنید.\n\n")
