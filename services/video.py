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
