"""AI edit plan: which video/photo goes under which narration sentence (and from which moment of a video)."""
import json
import re
import subprocess
from pathlib import Path

from services import llm

END_PUNCT = ("!", "?", "؟", ".", "؛", "…")
MIN_SENTENCE = 1.2   # shorter sentences are merged into the next one (no flash cuts)
MAX_IMAGES = 30      # frames sent to the model in total


WORDS_PER_SECOND = 2.6  # typical narration speed, for estimates before the voice exists


def _groups(narration: str) -> tuple[list[str], list[list[int]]]:
    """Tokens of the narration and the sentences as lists of token indexes; short sentences are merged.

    Merging uses an estimate from text length only, so the split is the same before and after the voice is made
    (the edit plan is approved before the voice is built, and must still fit afterwards).
    """
    tokens = [t for t in re.sub(r"[/]", " ", narration).split() if t]
    groups, cur = [], []
    for k, t in enumerate(tokens):
        cur.append(k)
        if t.endswith(END_PUNCT):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    def est(g):
        return len(g) / WORDS_PER_SECOND
    merged = []
    for g in groups:
        if merged and est(merged[-1]) < MIN_SENTENCE:
            merged[-1] = merged[-1] + g
        else:
            merged.append(g)
    if len(merged) > 1 and est(merged[-1]) < MIN_SENTENCE:
        last = merged.pop()
        merged[-1] = merged[-1] + last
    return tokens, merged


def _build(tokens, groups, starts, total):
    return [{"text": " ".join(tokens[k] for k in g), "start": 0.0 if i == 0 else starts[i],
             "end": starts[i + 1] if i + 1 < len(groups) else total} for i, g in enumerate(groups)]


def estimate(narration: str) -> list[dict]:
    """Sentences with estimated times (no voice yet)."""
    tokens, groups = _groups(narration)
    total = len(tokens) / WORDS_PER_SECOND + 0.5
    n_chars = sum(len(t) for t in tokens) or 1
    acc, starts = 0, []
    for g in groups:
        starts.append((total - 0.5) * acc / n_chars)
        acc += sum(len(tokens[k]) for k in g)
    return _build(tokens, groups, starts, total)


def sentences(narration: str, words: list[dict], total: float) -> list[dict]:
    """The same sentences as estimate(), timed from the real voice.

    `words` comes from stt.align_to_script: the script's words, in order, with times. The first sentence starts at
    0 and each one ends where the next begins, so the cuts land exactly on sentence starts and there are no gaps.
    """
    tokens, groups = _groups(narration)
    if len(words) == len(tokens) and words:
        starts = [words[g[0]]["start"] for g in groups]
    else:  # timings unusable: spread by text length
        n_chars = sum(len(t) for t in tokens) or 1
        acc, starts = 0, []
        for g in groups:
            starts.append(total * acc / n_chars)
            acc += sum(len(tokens[k]) for k in g)
    out = _build(tokens, groups, starts, total)
    for s in out:
        s["end"] = max(s["end"], s["start"] + 0.3)
    return out


def _frame(src: str, at: float | None, out: Path) -> bytes | None:
    cmd = ["ffmpeg", "-y"] + (["-ss", f"{at:.2f}"] if at is not None else []) + \
          ["-i", src, "-frames:v", "1", "-vf", "scale=384:-2", "-q:v", "5", str(out)]
    subprocess.run(cmd, capture_output=True)
    return out.read_bytes() if out.exists() else None


def frames(media: list[dict], work: Path) -> list[list[tuple[float | None, bytes]]]:
    """A few small frames per item: evenly spread over each video, the picture itself for photos."""
    work.mkdir(parents=True, exist_ok=True)
    n_videos = sum(1 for m in media if m["kind"] == "video") or 1
    per_video = max(1, min(4, (MAX_IMAGES - (len(media) - n_videos)) // n_videos))
    out = []
    for i, m in enumerate(media):
        shots = []
        if m["kind"] == "photo":
            data = _frame(m["path"], None, work / f"f{i}.jpg")
            if data:
                shots.append((None, data))
        else:
            dur = m.get("dur") or 1
            for k in range(per_video):
                at = round(dur * (0.1 + 0.8 * (k + 0.5) / per_video), 1)
                data = _frame(m["path"], at, work / f"f{i}_{k}.jpg")
                if data:
                    shots.append((at, data))
        out.append(shots)
    return out


SYSTEM = """You are the video editor of a Persian-language news channel. You get the narration of a short news video,
split into numbered sentences with their durations, and the numbered footage items the user supplied (videos and
photos), each shown as a few frames (video frames are labelled with their time in seconds) plus the user's own
description of the item, if any.

Assign exactly one footage item to EVERY sentence so the picture matches what the narrator is saying at that moment
(the person, place, object, map or event mentioned). Rules:
- The user's descriptions are the most reliable information about an item; use them.
- Use every item at least once if that is reasonable; avoid the same item on two consecutive sentences when another
  item fits nearly as well.
- For a video, set "at" to the time (seconds) of the most relevant frame shown, or null to continue where the previous
  use of that video stopped. For a photo "at" is null.
- "why": a very short Persian reason (a few words).
- If the user gave instructions about the edit, follow them exactly; they override the rules above.

Reply with ONLY a JSON array (no markdown fence), one element per sentence, in order:
[{"sentence": 1, "item": 2, "at": 12.0, "why": "..."}]"""


def plan(sentences_: list[dict], media: list[dict], shots, feedback: str | None = None,
         previous: list[dict] | None = None) -> list[dict]:
    parts = ["Narration sentences:\n" + "\n".join(
        f"{i}. ({s['end'] - s['start']:.1f}s) {s['text']}" for i, s in enumerate(sentences_, 1)), "\nFootage items:"]
    for i, (m, ss) in enumerate(zip(media, shots), 1):
        kind = f"video, {m.get('dur', 0):.0f}s long" if m["kind"] == "video" else "photo"
        parts.append(f"\nItem {i} ({kind})" + (f" — user's description: {m['note']}" if m.get("note") else ""))
        for at, data in ss:
            if at is not None:
                parts.append(f"frame at {at}s:")
            parts.append((data, "image/jpeg"))
    if previous:
        parts.append("\nCurrent edit plan:\n" + json.dumps(previous, ensure_ascii=False))
    if feedback:
        parts.append("\nThe user's instructions for the edit (follow them):\n" + feedback)
    text = llm.generate_parts(SYSTEM, parts, 3000)
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError("edit plan: no JSON in the reply:\n" + text[:300])
    return normalise(json.loads(m.group(0)), len(sentences_), media)


def normalise(raw: list, n_sentences: int, media: list[dict]) -> list[dict]:
    """One valid entry per sentence; anything missing or invalid is filled round-robin."""
    by_sentence = {}
    for p in raw if isinstance(raw, list) else []:
        try:
            s, item = int(p.get("sentence")), int(p.get("item"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 1 <= s <= n_sentences and 1 <= item <= len(media):
            at = p.get("at")
            at = float(at) if isinstance(at, (int, float)) and media[item - 1]["kind"] == "video" else None
            by_sentence[s] = {"sentence": s, "item": item, "at": at, "why": str(p.get("why", ""))[:80]}
    return [by_sentence.get(s, {"sentence": s, "item": (s - 1) % len(media) + 1, "at": None, "why": ""})
            for s in range(1, n_sentences + 1)]


def simple_plan(sentences_: list[dict], media: list[dict]) -> list[dict]:
    """No AI: items in the order they were sent, spread over the sentences."""
    n, k = len(sentences_), len(media)
    return [{"sentence": s, "item": (s - 1) * k // n + 1 if n >= k else s, "at": None, "why": "ترتیب ارسال"}
            for s in range(1, n + 1)]


def segments(plan_: list[dict], sentences_: list[dict], media: list[dict]) -> list[dict]:
    """Turn the plan into montage segments [{'item': index0, 'start': s, 'dur': d}].

    A video used again without an explicit time continues where it stopped, so repeats show new footage.
    """
    cursor = {}
    out = []
    for p, s in zip(plan_, sentences_):
        i = p["item"] - 1
        m, dur = media[i], s["end"] - s["start"]
        start = 0.0
        if m["kind"] == "video":
            length = m.get("dur") or dur
            start = p["at"] if p.get("at") is not None else cursor.get(i, min(1.0, max(0.0, length - dur)))
            start = max(0.0, min(start, length - dur)) if length > dur else 0.0
            cursor[i] = start + dur if start + dur < length - 0.5 else 0.0
        out.append({"item": i, "start": start, "dur": dur})
    return out
