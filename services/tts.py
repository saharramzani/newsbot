"""ElevenLabs text-to-speech. (Whisper cannot synthesize speech; it is used in stt.py.)"""
import hashlib
import shutil
import time
from pathlib import Path

import httpx

import config

CACHE_DIR = config.WORK_DIR / "tts_cache"


def clean_for_tts(text: str) -> str:
    # '/' is only a pause marker for the human reader; turn it into a short pause.
    return text.replace("/", "...").strip()


def synthesize(text: str, out_path: Path) -> Path:
    if not (config.ELEVENLABS_API_KEY and config.ELEVENLABS_VOICE_ID):
        raise RuntimeError("ELEVENLABS_API_KEY / ELEVENLABS_VOICE_ID not set")
    # the same text with the same voice is never paid for twice (rebuilds, retries, a re-cut with another plan)
    key = hashlib.sha1(f"{config.ELEVENLABS_VOICE_ID}|{config.ELEVENLABS_MODEL}|{text}".encode()).hexdigest()
    cached = CACHE_DIR / f"{key}.mp3"
    if cached.exists() and cached.stat().st_size > 1000:
        shutil.copyfile(cached, out_path)
        return out_path
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{config.ELEVENLABS_VOICE_ID}"
    for attempt in range(4):  # network hiccups and ElevenLabs overloads are common; retry with back-off
        try:
            r = httpx.post(
                url,
                params={"output_format": "mp3_44100_128"},
                headers={"xi-api-key": config.ELEVENLABS_API_KEY},
                json={"text": clean_for_tts(text), "model_id": config.ELEVENLABS_MODEL},
                timeout=httpx.Timeout(180, connect=30),
            )
        except (httpx.TransportError, OSError):
            if attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            time.sleep(5 * (attempt + 1))
            continue
        break
    if r.status_code >= 400:
        raise RuntimeError(_explain(r))
    out_path.write_bytes(r.content)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(out_path, cached)
    for old in sorted(CACHE_DIR.glob("*.mp3"), key=lambda f: f.stat().st_mtime)[:-50]:  # keep the last 50
        old.unlink(missing_ok=True)
    return out_path


HINTS = {
    "quota_exceeded": "اعتبار (کاراکتر) حساب ElevenLabs تمام شده؛ پلن را شارژ/ارتقا دهید یا تا تمدید ماهانه صبر کنید.",
    "invalid_api_key": "کلید ELEVENLABS_API_KEY در فایل .env اشتباه است یا حذف شده؛ یک کلید جدید بسازید و جایگزین کنید.",
    "missing_permissions": "کلید ElevenLabs اجازه‌ی Text to Speech ندارد؛ در تنظیمات کلید، دسترسی Text to Speech را روشن کنید.",
    "detected_unusual_activity": "ElevenLabs حساب رایگان را به خاطر «فعالیت غیرعادی» (مثلاً VPN) محدود کرده؛ بدون VPN امتحان کنید یا پلن پولی بگیرید.",
    "voice_not_found": "صدای ELEVENLABS_VOICE_ID پیدا نشد؛ شناسه‌ی صدا در .env را چک کنید.",
}


def _explain(r) -> str:
    """ElevenLabs' own reason for an error, plus what to do about it."""
    try:
        detail = r.json().get("detail", {})
    except Exception:
        detail = {}
    if isinstance(detail, str):
        detail = {"message": detail}
    status, msg = detail.get("status", ""), detail.get("message", "") or r.text[:300]
    hint = HINTS.get(status, "")
    return f"ElevenLabs خطای {r.status_code} داد ({status or 'بدون کد'}): {msg}" + (f"\n👉 {hint}" if hint else "")
