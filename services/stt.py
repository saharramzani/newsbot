"""Whisper (faster-whisper) word-level timestamps for accurate subtitles."""
import re
import time

import config

_model = None


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        try:  # already downloaded: load from disk without contacting huggingface.co
            _model = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8", local_files_only=True)
        except Exception:
            last = None
            for attempt in range(3):  # first run: download the model (~500 MB), retrying on network errors
                try:
                    _model = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")
                    break
                except Exception as e:
                    last = e
                    time.sleep(10 * (attempt + 1))
            if _model is None:
                raise RuntimeError(f"دانلود مدل Whisper از huggingface.co نشد: {last}")
    return _model


def transcribe_words(audio_path, language="fa") -> list[dict]:
    """Returns [{'word': str, 'start': float, 'end': float}, ...]"""
    segments, _ = _get_model().transcribe(str(audio_path), language=language, word_timestamps=True)
    words = []
    for seg in segments:
        for w in seg.words or []:
            words.append({"word": w.word.strip(), "start": float(w.start), "end": float(w.end)})
    return words


def align_to_script(script: str, words: list[dict]) -> list[dict]:
    """Display the approved script words (correct spelling) with Whisper's timings.
    If counts match, map 1:1; otherwise spread script words over the spoken span by character length."""
    script_words = [w for w in re.sub(r"[/]", " ", script).split() if w]
    if not words or not script_words:
        return words
    if len(script_words) == len(words):
        return [{"word": s, "start": w["start"], "end": w["end"]} for s, w in zip(script_words, words)]
    t0, t1 = words[0]["start"], words[-1]["end"]
    total = sum(len(s) for s in script_words)
    out, acc = [], 0
    for s in script_words:
        a = t0 + (t1 - t0) * acc / total
        acc += len(s)
        b = t0 + (t1 - t0) * acc / total
        out.append({"word": s, "start": a, "end": b})
    return out


def preload():
    """Load (and on first run download) the Whisper model ahead of time, so the first video is not slowed down."""
    _get_model()
