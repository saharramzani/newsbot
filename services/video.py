"""Subtitles (ASS, burned in) + branded Instagram video assembly (9:16 reel or 4:5 post) with ffmpeg."""
import os
import subprocess
from pathlib import Path

import config

W, H = 1080, 1920

# Brand "uniform": a full-frame PNG overlay (transparent middle, gradients, logo, yellow band).
# sub_bottom = distance of the subtitle baseline from the bottom edge, so text sits in the blue area above the logo.
FORMATS = {
    "9x16": {"w": 1080, "h": 1920, "template": "assets/brand/template_9x16.png", "sub_bottom": 400, "label": "۹:۱۶ ریلز"},
    "4x5": {"w": 1080, "h": 1350, "template": "assets/brand/template_4x5.png", "sub_bottom": 250, "label": "۴:۵ پست"},
}
WHITE, YELLOW = "&H00FFFFFF&", "&H0016B8FB&"  # ASS colours are &HAABBGGRR; brand yellow = #FBB816


def _ts(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def build_ass(words: list[dict], path: Path, group: int = 3, fmt: str = "9x16") -> Path:
    f = FORMATS[fmt]
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {f["w"]}
PlayResY: {f["h"]}
WrapStyle: 2

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,{config.SUBTITLE_FONT},78,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,2,80,110,{f["sub_bottom"]},1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    lines = []
    for i in range(0, len(words), group):
        chunk = words[i:i + group]
        clean = [w["word"].replace("{", "(").replace("}", ")") for w in chunk]
        end = chunk[-1]["end"]
        if i + group < len(words):  # no gaps between subtitle groups
            end = max(end, words[i + group]["start"])
        # one event per word: the word being spoken is yellow, the rest white
        for k, w in enumerate(chunk):
            start = chunk[0]["start"] if k == 0 else w["start"]
            stop = chunk[k + 1]["start"] if k + 1 < len(chunk) else end
            if stop <= start:
                continue
            body = " ".join(f"{{\\c{YELLOW}}}{t}{{\\c{WHITE}}}" if j == k else t for j, t in enumerate(clean))
            # U+202B (RLE) + U+200F (RLM) force a right-to-left base direction so word order is correct
            text = "\u202b\u200f" + body + "\u200f\u202c"
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(stop)},Default,,0,0,0,,{text}")
    path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return path


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def _filter_path(p: Path) -> str:
    """Path for use inside an ffmpeg filter option (e.g. ass=...).

    Windows backslashes are filter escape characters and get swallowed, and a drive letter's ':' splits
    options. Use a forward-slash path (relative when possible), escape ':' and quote it.
    """
    try:
        s = os.path.relpath(p)
    except ValueError:  # Windows: different drive than the working directory
        s = str(Path(p).resolve())
    s = s.replace("\\", "/").replace(":", "\\:").replace("'", "")
    return f"'{s}'"


def build_video(src: Path, narration: Path, ass: Path, out: Path, logo: Path | None = None,
                fmt: str = "9x16") -> Path:
    f = FORMATS[fmt]
    w, h = f["w"], f["h"]
    dur = duration(narration) + 0.5
    cmd = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(src), "-i", str(narration)]
    fonts = Path("assets/fonts")
    ass_filter = f"ass={_filter_path(ass)}" + (f":fontsdir={_filter_path(fonts)}" if fonts.exists() else "")
    base = f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps=30[bg]"
    template = Path(f["template"])
    if template.exists():  # brand uniform already contains the logo
        cmd += ["-loop", "1", "-i", str(template)]
        fc = f"{base};[2:v]scale={w}:{h},format=rgba[tp];[bg][tp]overlay=0:0:shortest=0[ov];[ov]{ass_filter}[vout]"
    elif logo and logo.exists():
        cmd += ["-i", str(logo)]
        fc = (f"{base};[2:v]scale=220:-1[lg];[bg][lg]overlay=W-w-50:70[ov];[ov]{ass_filter}[vout]")
    else:
        fc = f"{base};[bg]{ass_filter}[vout]"
    cmd += ["-filter_complex", fc, "-map", "[vout]", "-map", "1:a",
            "-t", f"{dur:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + r.stderr[-1200:])
    return out


# ---------- montage: several videos / photos -> one background clip ----------
PHOTO_MIN, PHOTO_MAX = 2.5, 6.0  # seconds a photo stays on screen


def plan_durations(media: list[dict], total: float) -> list[float]:
    """Share `total` seconds fairly between the items, in order.

    Photos get an equal share (clamped); a video never gets more than its own length, and what a short
    video cannot use goes to the longer ones. If everything together is still shorter, the montage loops.
    """
    n = len(media)
    share = total / n
    durs = [0.0] * n
    for k, m in enumerate(media):
        durs[k] = min(max(share, PHOTO_MIN), PHOTO_MAX) if m["kind"] == "photo" else min(share, m["dur"])
    for _ in range(5):  # hand leftover time to videos that have more footage
        left = total - sum(durs)
        room = [k for k, m in enumerate(media) if m["kind"] == "video" and m["dur"] - durs[k] > 0.1]
        if left <= 0.1 or not room:
            break
        for k in room:
            durs[k] = min(media[k]["dur"], durs[k] + left / len(room))
    photos = [k for k, m in enumerate(media) if m["kind"] == "photo"]
    left = total - sum(durs)
    if left > 0.1 and photos:  # still short: let photos stay longer (up to 10 s) before the montage loops
        for k in photos:
            durs[k] = min(10.0, durs[k] + left / len(photos))
    return [max(d, 0.5) for d in durs]


def _segment(m: dict, dur: float, out: Path, w: int, h: int):
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-an", str(out)]
    if m["kind"] == "photo":
        frames = int(dur * 30)
        # slow Ken Burns zoom so photos do not look frozen
        vf = (f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,crop={w * 2}:{h * 2},"
              f"zoompan=z='min(zoom+0.0008,1.12)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
              f":d={frames}:s={w}x{h}:fps=30,setsar=1")
        cmd = ["ffmpeg", "-y", "-i", m["path"], "-vf", vf, "-frames:v", str(frames)] + enc
    else:
        start = max(0.0, min(1.0, m["dur"] - dur))  # skip the first second (often a logo/black) when possible
        vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps=30"
        cmd = ["ffmpeg", "-y", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", m["path"], "-vf", vf] + enc
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed on {Path(m['path']).name}:\n" + r.stderr[-800:])


def build_montage(media: list[dict], total: float, work: Path, fmt: str = "9x16") -> Path:
    """media: [{'path', 'kind': 'video'|'photo', 'dur'}] in the order the user sent them."""
    f = FORMATS[fmt]
    work.mkdir(parents=True, exist_ok=True)
    parts = []
    for k, (m, d) in enumerate(zip(media, plan_durations(media, total))):
        seg = work / f"seg_{k:02d}.mp4"
        _segment(m, d, seg, f["w"], f["h"])
        parts.append(seg)
    listing = work / "segments.txt"
    listing.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
    out = work / "montage.mp4"
    r = subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg concat failed:\n" + r.stderr[-800:])
    return out


TELEGRAM_MAX_BYTES = 45_000_000  # bots may upload up to 50 MB; keep a margin


def shrink(path: Path) -> Path:
    """Re-encode in place with a size cap so Telegram accepts the upload."""
    secs = duration(path)
    kbps = int(TELEGRAM_MAX_BYTES * 8 / 1000 / max(secs, 1) * 0.9) - 160  # leave room for 160k audio
    tmp = path.with_name(path.stem + "_small.mp4")
    r = subprocess.run(["ffmpeg", "-y", "-i", str(path), "-c:v", "libx264", "-preset", "veryfast",
                        "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(tmp)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg shrink failed:\n" + r.stderr[-800:])
    tmp.replace(path)
    return path
