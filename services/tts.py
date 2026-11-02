"""ElevenLabs text-to-speech. (Whisper cannot synthesize speech; it is used in stt.py.)"""
from pathlib import Path

import httpx

import config


def clean_for_tts(text: str) -> str:
    # '/' is only a pause marker for the human reader; turn it into a short pause.
    return text.replace("/", "...").strip()


def synthesize(text: str, out_path: Path) -> Path:
    if not (config.ELEVENLABS_API_KEY and config.ELEVENLABS_VOICE_ID):
        raise RuntimeError("ELEVENLABS_API_KEY / ELEVENLABS_VOICE_ID not set")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{config.ELEVENLABS_VOICE_ID}"
    r = httpx.post(
        url,
        params={"output_format": "mp3_44100_128"},
        headers={"xi-api-key": config.ELEVENLABS_API_KEY},
        json={"text": clean_for_tts(text), "model_id": config.ELEVENLABS_MODEL},
        timeout=120,
    )
    r.raise_for_status()
    out_path.write_bytes(r.content)
    return out_path
