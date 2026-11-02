"""Subtitles (ASS, burned in) + 9:16 Instagram video assembly with ffmpeg."""
import subprocess
from pathlib import Path

import config

W, H = 1080, 1920


def _ts(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def build_ass(words: list[dict], path: Path, group: int = 3) -> Path:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,{config.SUBTITLE_FONT},78,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,2,60,60,420,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    lines = []
    for i in range(0, len(words), group):
        chunk = words[i:i + group]
        # U+202B (RLE) + U+200F (RLM) force a right-to-left base direction so word order is correct
        text = "‫‏" + " ".join(w["word"] for w in chunk).replace("{", "(").replace("}", ")") + "‏‬"
        end = chunk[-1]["end"]
        if i + group < len(words):  # no gaps between subtitle groups
            end = max(end, words[i + group]["start"])
        lines.append(f"Dialogue: 0,{_ts(chunk[0]['start'])},{_ts(end)},Default,,0,0,0,,{text}")
    path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return path


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def build_video(src: Path, narration: Path, ass: Path, out: Path, logo: Path | None = None) -> Path:
    dur = duration(narration) + 0.5
    cmd = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(src), "-i", str(narration)]
    fonts = Path("assets/fonts")
    ass_filter = f"ass={ass}" + (f":fontsdir={fonts}" if fonts.exists() else "")
    base = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps=30[bg]"
    if logo and logo.exists():
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
