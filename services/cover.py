"""Cover (thumbnail) for each video: a good frame from the source + brand template + big headline."""
import subprocess
from pathlib import Path

import config
from services.video import FORMATS, _filter_path, duration

# where the bottom of the headline block sits, measured from the bottom edge (text grows upward)
COVER_BOTTOM = {"9x16": 380, "4x5": 250}


def extract_frame(src: Path, out: Path, fmt: str = "9x16") -> Path:
    """Pick a representative (non-black, sharp) frame about a third into the video, scaled/cropped to the format."""
    f = FORMATS[fmt]
    try:
        t = max(0.0, min(duration(src) * 0.3, duration(src) - 1.5))
    except Exception:
        t = 0.0
    vf = f"thumbnail=40,scale={f['w']}:{f['h']}:force_original_aspect_ratio=increase,crop={f['w']}:{f['h']},setsar=1"
    _run(["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(src), "-vf", vf, "-frames:v", "1", str(out)])
    if not out.exists():  # very short clip: thumbnail filter had too few frames, take the first one
        vf = vf.replace("thumbnail=40,", "")
        _run(["ffmpeg", "-y", "-i", str(src), "-vf", vf, "-frames:v", "1", str(out)])
    return out


def _font_size(title: str) -> int:
    n = len(title)
    return 124 if n <= 18 else 108 if n <= 30 else 92 if n <= 45 else 78


def compose(bg: Path, title: str, out: Path, fmt: str = "9x16") -> Path:
    f = FORMATS[fmt]
    w, h = f["w"], f["h"]
    title = " ".join(title.replace("{", "(").replace("}", ")").split())
    ass = out.with_suffix(".ass")
    ass.write_text(f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Cover,{config.SUBTITLE_FONT},{_font_size(title)},&H00FFFFFF,&H00FFFFFF,&H00241A0D,&H96000000,-1,0,0,0,100,100,0,0,1,7,4,2,90,130,{COVER_BOTTOM[fmt]},1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
Dialogue: 0,0:00:00.00,0:00:10.00,Cover,,0,0,0,,‫‏{title}‏‬
""", encoding="utf-8")
    fonts = Path("assets/fonts")
    ass_filter = f"ass={_filter_path(ass)}" + (f":fontsdir={_filter_path(fonts)}" if fonts.exists() else "")
    template = Path(f["template"])
    cmd = ["ffmpeg", "-y", "-i", str(bg)]
    if template.exists():
        cmd += ["-i", str(template)]
        fc = (f"[0:v]eq=brightness=-0.05:saturation=1.05[bg];[1:v]scale={w}:{h},format=rgba[tp];"
              f"[bg][tp]overlay=0:0[ov];[ov]{ass_filter}[v]")
    else:
        fc = f"[0:v]{ass_filter}[v]"
    _run(cmd + ["-filter_complex", fc, "-map", "[v]", "-frames:v", "1", "-q:v", "2", str(out)])
    return out


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg (cover) failed:\n" + r.stderr[-1000:])
