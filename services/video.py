"""Subtitles (ASS, burned in) + 9:16 Instagram video assembly with ffmpeg."""
import subprocess
from pathlib import Path

import config

W, H = 1080, 1920


def _ts(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


# Caption box that hides the source clips' own subtitles. Narration is written on it.
BOX_Y, BOX_H = 1380, 420            # covers the lower part of the frame where burned-in subs usually sit
STRIPE_H = 16                       # yellow accent stripe on top of the box
NAVY, YELLOW = "0x0B1F4B", "0xFFC400"
TEXT_SIZE = 96
HIGHLIGHT = "&H0000C4FF&"          # ASS colour (BGR) of the yellow above, for the word being spoken
LOGO_W = 420


def box_filter() -> str:
    return (f"drawbox=x=0:y={BOX_Y}:w=iw:h={BOX_H}:color={NAVY}:t=fill,"
            f"drawbox=x=0:y={BOX_Y}:w=iw:h={STRIPE_H}:color={YELLOW}:t=fill")


def build_ass(words: list[dict], path: Path, group: int = 4) -> Path:
    """Narration subtitles inside the caption box; the word being read is coloured yellow."""
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,{config.SUBTITLE_FONT},{TEXT_SIZE},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,2,0,5,70,70,0,-1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    pos = f"{{\\an5\\pos({W // 2},{BOX_Y + STRIPE_H + (BOX_H - STRIPE_H) // 2})}}"
    clean = [w["word"].replace("{", "(").replace("}", ")") for w in words]
    lines = []
    for i in range(0, len(words), group):
        chunk = words[i:i + group]
        chunk_end = chunk[-1]["end"]
        if i + group < len(words):  # no gaps between subtitle groups
            chunk_end = max(chunk_end, words[i + group]["start"])
        for k, w in enumerate(chunk):
            start = chunk[0]["start"] if k == 0 else w["start"]
            end = chunk[k + 1]["start"] if k + 1 < len(chunk) else chunk_end
            if end <= start:
                continue
            parts = [f"{{\\c{HIGHLIGHT}}}{clean[i + n]}{{\\c&H00FFFFFF&}}" if n == k else clean[i + n]
                     for n in range(len(chunk))]
            # Encoding -1 (in the style) makes libass run bidi over the whole line, so the colour
            # tags around the highlighted word do not break right-to-left word order
            text = " ".join(parts)
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Default,,0,0,0,,{pos}{text}")
    path.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return path


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def _plan_segments(srcs: list[Path], total: float) -> list[tuple[int, float, float]]:
    """Cut the clips into short alternating shots that fill `total` seconds.

    Returns [(source_index, start, length)]. Each source is read forward from where its
    previous shot ended (wrapping around), so the montage does not repeat the same frames.
    """
    durs = []
    for s in srcs:
        try:
            durs.append(max(duration(s), 0.5))
        except Exception:
            durs.append(10.0)
    shot = min(5.0, max(2.5, total / max(len(srcs), 1))) if len(srcs) > 1 else total
    pos = [min(1.0, d * 0.1) for d in durs]  # skip the first second (often a title card)
    plan, t, k = [], 0.0, 0
    while t < total - 0.01:
        i = k % len(srcs)
        length = min(shot, total - t, durs[i])
        if pos[i] + length > durs[i]:
            pos[i] = 0.0
        plan.append((i, pos[i], length))
        pos[i] += length
        t += length
        k += 1
    return plan


def build_video(srcs: list[Path] | Path, narration: Path, ass: Path, out: Path, logo: Path | None = None) -> Path:
    """Montage the source clips (muted) under the narration, then add logo + subtitles. 9:16 output."""
    if isinstance(srcs, Path):
        srcs = [srcs]
    dur = duration(narration) + 0.5
    plan = _plan_segments(srcs, dur)
    cmd = ["ffmpeg", "-y"]
    parts = []
    for n, (i, start, length) in enumerate(plan):
        # loop each input so a clip shorter than the shot still fills it
        cmd += ["-stream_loop", "-1", "-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", str(srcs[i])]
        parts.append(f"[{n}:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                     f"setsar=1,fps=30,format=yuv420p,trim=duration={length:.2f},setpts=PTS-STARTPTS[s{n}]")
    a_idx = len(plan)
    cmd += ["-i", str(narration)]
    fonts = Path("assets/fonts")
    ass_filter = f"ass={ass}" + (f":fontsdir={fonts}" if fonts.exists() else "")
    fc = ";".join(parts) + ";" + "".join(f"[s{n}]" for n in range(len(plan))) + f"concat=n={len(plan)}:v=1:a=0[bg]"
    if logo and logo.exists():
        cmd += ["-i", str(logo)]
        fc += (f";[bg]{box_filter()}[bx];[{a_idx + 1}:v]scale={LOGO_W}:-1[lg];"
               f"[bx][lg]overlay=W-w-50:80[ov];[ov]{ass_filter}[vout]")
    else:
        fc += f";[bg]{box_filter()},{ass_filter}[vout]"
    cmd += ["-filter_complex", fc, "-map", "[vout]", "-map", f"{a_idx}:a",
            "-t", f"{dur:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + r.stderr[-1200:])
    return out
