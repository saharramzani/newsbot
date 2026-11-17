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


# ---------- links that are not (only) a video: image links, photo posts, news pages ----------
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")
VIDEO_EXT = (".mp4", ".mov", ".m4v", ".webm", ".mkv")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}


def _is_image(data: bytes) -> bool:
    return (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))


def _ext_of(data: bytes) -> str:
    return ".png" if data[:4] == b"\x89PNG" else ".webp" if data[:4] == b"RIFF" else ".jpg"


def download_image(url: str, out_dir: Path) -> Path:
    import httpx
    r = httpx.get(url, headers=UA, follow_redirects=True, timeout=60)
    if r.status_code != 200 or not _is_image(r.content):
        raise RuntimeError(f"این لینک عکس قابل‌دانلود نبود ({r.status_code}, {r.headers.get('content-type', '?')})")
    path = out_dir / ("photo" + _ext_of(r.content))
    path.write_bytes(r.content)
    return path


def _gallery(url: str, out_dir: Path) -> list[Path]:
    """gallery-dl: photo posts and carousels (Instagram, X, ...) -> files in post order."""
    cmd = [sys.executable, "-m", "gallery_dl", "-D", str(out_dir), "--no-mtime"]
    cookies = Path(config.YTDLP_COOKIES)
    if cookies.is_file():
        cmd += ["--cookies", str(cookies)]
    r = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=300)
    files = sorted((f for f in out_dir.iterdir() if f.suffix.lower() in IMAGE_EXT + VIDEO_EXT), key=lambda f: f.name)
    if not files:
        raise RuntimeError("gallery-dl: " + (r.stderr or r.stdout)[-400:])
    return files


def _page_image(url: str, out_dir: Path) -> Path:
    """A news page without a video: its main picture (og:image / twitter:image)."""
    import html as html_mod
    import re

    import httpx
    r = httpx.get(url, headers=UA, follow_redirects=True, timeout=30)
    if _is_image(r.content):  # the link itself is an image (no file extension in the URL)
        path = out_dir / ("photo" + _ext_of(r.content))
        path.write_bytes(r.content)
        return path
    page = r.text
    for prop in ("og:image", "twitter:image", "og:image:url"):
        m = (re.search(rf'<meta[^>]+(?:property|name)=["\']{prop}["\'][^>]+content=["\']([^"\']+)', page, re.I)
             or re.search(rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{prop}["\']', page, re.I))
        if m:
            img = html_mod.unescape(m.group(1))
            if img.startswith("//"):
                img = "https:" + img
            return download_image(img, out_dir)
    raise RuntimeError("این صفحه نه ویدئو داشت نه عکس اصلی.")


def fetch_link(url: str, out_dir: Path) -> list[tuple[Path, str]]:
    """Any link -> [(file, 'video' | 'photo'), ...] in order.

    Direct image link -> that image. Otherwise the video (yt-dlp). If the link has no video: the photos/videos of
    the post (Instagram carousel etc., via gallery-dl), else the main picture of the page.
    """
    url = clean_url(url)
    fresh_dir(out_dir)
    if url.lower().split("?", 1)[0].endswith(IMAGE_EXT):
        return [(download_image(url, out_dir), "photo")]
    try:
        return [(download_video(url, out_dir), "video")]
    except RuntimeError as video_err:
        if "429" in str(video_err) or "ffmpeg پیدا نشد" in str(video_err):
            raise
        tries = []
        for step in (_gallery_step, _page_step):
            try:
                found = step(url, fresh_dir(out_dir))
                if found:
                    return found
            except Exception as e:
                tries.append(str(e)[:200])
        raise RuntimeError(f"{video_err}\n\n(عکس هم پیدا نشد: {' | '.join(tries)})")


def _gallery_step(url, out_dir):
    if "instagram.com" in url:
        with _insta_lock:  # same spacing as video downloads; Instagram rate-limits bursts
            wait = _insta_last[0] + INSTA_GAP - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                files = _gallery(url, out_dir)
            finally:
                _insta_last[0] = time.monotonic()
    else:
        files = _gallery(url, out_dir)
    return [(f, "video" if f.suffix.lower() in VIDEO_EXT else "photo") for f in files]


def _page_step(url, out_dir):
    if any(s in url for s in ("instagram.com", "x.com", "twitter.com", "t.me")):
        return []  # social posts: gallery-dl is the right tool; their pages carry only a logo
    return [(_page_image(url, out_dir), "photo")]
