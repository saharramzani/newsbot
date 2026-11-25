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


def ass_color(hex_rgb: str, alpha: int = 0) -> str:
    """#RRGGBB (+ transparency 0 = opaque .. 255 = invisible) -> ASS &HAABBGGRR"""
    h = hex_rgb.lstrip("#")
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def build_ass(words: list[dict], path: Path, group: int = 3, fmt: str = "9x16", highlight: str | None = None,
              style: dict | None = None) -> Path:
    """Karaoke-style subtitles. `style` (an outlet's look, all keys optional):
    font, bold, scale (x SUBTITLE_SIZE), color, highlight, outline_color, outline, shadow,
    box (coloured box behind the text), box_color, box_alpha (0 opaque .. 255 invisible), bottom (distance from the
    bottom edge), words (words shown at a time)."""
    st = style or {}
    f = FORMATS[fmt]
    size = round(config.SUBTITLE_SIZE * float(st.get("scale", 1.0)))
    hl = ass_color(st["highlight"]) + "&" if st.get("highlight") else (highlight or YELLOW)
    base = ass_color(st.get("color", "#FFFFFF"))
    group = int(st.get("words") or group)
    bottom = st.get("bottom") or f["sub_bottom"]
    if isinstance(bottom, dict):  # exact position per format (e.g. inside a box of the template)
        bottom = bottom.get(fmt) or f["sub_bottom"]
    elif fmt == "4x5" and st.get("bottom"):  # one number: keep the same proportion on the shorter 4:5 frame
        bottom = bottom * 1350 / 1920
    bottom = int(bottom)
    bold = -1 if st.get("bold", True) else 0
    if st.get("box"):  # opaque/translucent box: BorderStyle 3 paints it in the outline colour
        border, outline_c = 3, ass_color(st.get("box_color", "#000000"), int(st.get("box_alpha", 60)))
        outline, shadow = int(size * 0.16), 0
    else:
        border, outline_c = 1, ass_color(st.get("outline_color", "#000000"))
        outline = int(st.get("outline") or max(6, size // 13))
        shadow = int(st.get("shadow", 2))
    font = st.get("font") or config.SUBTITLE_FONT
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {f["w"]}
PlayResY: {f["h"]}
WrapStyle: 0

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,{font},{size},{base},&H000000FF,{outline_c},&H80000000,{bold},0,0,0,100,100,0,0,{border},{outline},{shadow},2,70,100,{bottom},-1

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
            body = " ".join(f"{{\\c{hl}}}{t}{{\\c{base}&}}" if j == k else t for j, t in enumerate(clean))
            # Encoding -1 (style) lets libass detect the base direction from the text: Persian -> right-to-left.
            # Newer libass (Windows builds) ignores RLE/PDF embedding marks, so we do not rely on them; a leading
            # RLM (a strong RTL character) keeps lines that start with a Latin word or number right-to-left too.
            text = "\u200f" + body
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
                fmt: str = "9x16", template: Path | None = None) -> Path:
    f = FORMATS[fmt]
    w, h = f["w"], f["h"]
    dur = duration(narration) + 0.5
    cmd = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(src), "-i", str(narration)]
    fonts = Path("assets/fonts")
    ass_filter = f"ass={_filter_path(ass)}" + (f":fontsdir={_filter_path(fonts)}" if fonts.exists() else "")
    base = f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps=30[bg]"
    template = Path(template or f["template"])  # the outlet's uniform
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


def _segment(m: dict, dur: float, out: Path, w: int, h: int, start: float | None = None):
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-an", str(out)]
    if m["kind"] == "photo":
        frames = int(dur * 30)
        # slow Ken Burns zoom so photos do not look frozen
        vf = (f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,crop={w * 2}:{h * 2},"
              f"zoompan=z='min(zoom+0.0008,1.12)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
              f":d={frames}:s={w}x{h}:fps=30,setsar=1")
        cmd = ["ffmpeg", "-y", "-i", m["path"], "-vf", vf, "-frames:v", str(frames)] + enc
    else:
        if start is None:
            start = max(0.0, min(1.0, m["dur"] - dur))  # skip the first second (often a logo/black) when possible
        loop = ["-stream_loop", "-1"] if m.get("dur", 0) < dur else []  # clip shorter than its slot: loop it
        vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps=30"
        cmd = ["ffmpeg", "-y"] + loop + ["-ss", f"{start:.2f}", "-i", m["path"], "-t", f"{dur:.2f}", "-vf", vf] + enc
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed on {Path(m['path']).name}:\n" + r.stderr[-800:])


def build_montage(media: list[dict], total: float, work: Path, fmt: str = "9x16", segs: list[dict] | None = None) -> Path:
    """media: [{'path', 'kind': 'video'|'photo', 'dur'}] in the order the user sent them.

    segs (optional, from the AI edit plan): [{'item': index, 'start': s, 'dur': d}] in timeline order. Without it,
    every item is used once, in order, with a fair share of the time.
    """
    f = FORMATS[fmt]
    work.mkdir(parents=True, exist_ok=True)
    if segs is None:
        segs = [{"item": k, "start": None, "dur": d} for k, d in enumerate(plan_durations(media, total))]
    parts = []
    for k, sg in enumerate(segs):
        seg = work / f"seg_{k:02d}.mp4"
        _segment(media[sg["item"]], sg["dur"], seg, f["w"], f["h"], sg.get("start"))
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
