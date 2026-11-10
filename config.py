import os
from pathlib import Path

def _load_env_file(p=".env"):
    f = Path(p)
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_env_file()
get = os.environ.get

TELEGRAM_BOT_TOKEN = get("TELEGRAM_BOT_TOKEN", "")
ELEVENLABS_API_KEY = get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = get("ELEVENLABS_VOICE_ID", "")
ELEVENLABS_MODEL = get("ELEVENLABS_MODEL", "eleven_v3")
LLM_PROVIDER = get("LLM_PROVIDER", "gemini")
GEMINI_API_KEY = get("GEMINI_API_KEY", "")
GEMINI_MODEL = get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_RPM = int(get("GEMINI_RPM", "9"))  # requests per minute we allow ourselves (free tier: ~10)
SCOUT_MODEL = get("SCOUT_MODEL", "claude-opus-5-5")
WRITER_MODEL = get("WRITER_MODEL", "claude-opus-5-5")
WHISPER_MODEL = get("WHISPER_MODEL", "small")
# main voice: "elevenlabs" (paid, best) or "edge" (free Microsoft voice; e.g. for a day of testing)
TTS_ENGINE = get("TTS_ENGINE", "elevenlabs").lower()
# free fallback voice when ElevenLabs credits run out: "edge" (Microsoft Edge TTS, no key) or "none"
FALLBACK_TTS = get("FALLBACK_TTS", "edge").lower()
EDGE_VOICE = get("EDGE_VOICE", "fa-IR-FaridNeural")  # or fa-IR-DilaraNeural (female)
EDGE_RATE = get("EDGE_RATE", "+5%")
LOGO_PATH = get("LOGO_PATH", "assets/logo.png")
SUBTITLE_FONT = get("SUBTITLE_FONT", "Vazirmatn")
NEWS_WINDOW_HOURS = int(get("NEWS_WINDOW_HOURS", "12"))
NEWS_MIN_IMPORTANCE = int(get("NEWS_MIN_IMPORTANCE", "4"))  # /news shows items scored at least this (1-5)
BULLETIN_HOURS = int(get("BULLETIN_HOURS", "24"))     # news window of the /bulletin command
BULLETIN_FONT = get("BULLETIN_FONT", "B Nazanin")     # font of the Word bulletin
ALLOWED_USER_IDS = {int(x) for x in get("ALLOWED_USER_IDS", "").split(",") if x.strip()}
WORK_DIR = Path(get("WORK_DIR", "work"))


def _ensure_ffmpeg_on_path():
    """Windows: winget installs ffmpeg but a window opened earlier may not see it. Find it and add it to PATH."""
    import glob
    import shutil
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    candidates = []
    if get("FFMPEG_DIR"):
        candidates.append(get("FFMPEG_DIR"))
    local = get("LOCALAPPDATA")
    if local:
        candidates.append(os.path.join(local, "Microsoft", "WinGet", "Links"))
        candidates += glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "*", "bin"))
    candidates += [r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin", "/opt/homebrew/bin", "/usr/local/bin"]
    for d in candidates:
        if os.path.isfile(os.path.join(d, "ffmpeg.exe")) or os.path.isfile(os.path.join(d, "ffmpeg")):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            return


_ensure_ffmpeg_on_path()
WORK_DIR.mkdir(parents=True, exist_ok=True)
