"""Cover (thumbnail) for each video: a good frame from the source + bold headline banners + brand icon."""
import subprocess
from pathlib import Path

import config
from services.video import FORMATS, _filter_path, duration



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


FONT_FILE = Path("assets/fonts/Vazirmatn-Black.ttf")
FONT_NAME = "Vazirmatn Black"
LOGO_ICON = Path("assets/brand/logo_icon_circle.jpg")

# ASS colours are &HAABBGGRR
_WHITE, _BLACK, _YELLOW, _RED = "&H00FFFFFF", "&H00000000", "&H0016B8FB", "&H001F19D7"
# (text colour, box colour or None = outlined text without a box)
LOOKS = {
    "plain": (_WHITE, None),
    "white": (_BLACK, _WHITE),
    "yellow": (_BLACK, _YELLOW),
    "red": (_WHITE, _RED),
}
# one pattern per line count; the last line is always the red punchline, like the reference covers
PATTERNS = {1: [["red"]], 2: [["plain", "red"], ["white", "red"], ["yellow", "red"]],
            3: [["white", "yellow", "red"], ["plain", "yellow", "red"], ["yellow", "white", "red"]]}
# vertical centre of the headline block, as a fraction of the height
BLOCK_CENTER = {"9x16": 0.70, "4x5": 0.66}


def split_lines(title: str) -> list[str]:
    """Explicit line breaks win; otherwise split into 1-3 balanced lines of 1-3 words."""
    if "\n" in title.strip():
        return [l.strip() for l in title.strip().splitlines() if l.strip()][:3]
    words = title.split()
    n = 1 if len(words) <= 2 else 2 if len(words) <= 5 else 3
    target = len(" ".join(words)) / n
    lines, cur = [], []
    for w in words:
        if cur and len(lines) < n - 1 and len(" ".join(cur + [w])) > target * 1.15:
            lines.append(" ".join(cur))
            cur = []
        cur.append(w)
    lines.append(" ".join(cur))
    return lines


def _measure(text: str) -> tuple[float, float]:
    """(width of text at em size 100, font line height at em size 100) in pixels."""
    try:
        from PIL import ImageFont, features
        raqm = features.check("raqm")
        font = ImageFont.truetype(str(FONT_FILE), 100,
                                  layout_engine=ImageFont.Layout.RAQM if raqm else ImageFont.Layout.BASIC)
        width = font.getlength(text, direction="rtl") if raqm else font.getlength(text) * 0.85
        asc, desc = font.getmetrics()
        return width, asc + desc
    except Exception:
        return len(text) * 50, 160


def _fit_size(text: str, max_w: float, max_size: int) -> int:
    """ASS font size so the line fills max_w. libass sizes fonts by line height (ascent+descent), not by em."""
    width, line_h = _measure(text)
    em = 100 * max_w / max(width, 1)
    return int(max(70, min(max_size, em * line_h / 100)))


def compose(bg: Path, title, out: Path, fmt: str = "9x16") -> Path:
    """title: a string (auto-split) or a list of 1-3 lines."""
    f = FORMATS[fmt]
    w, h = f["w"], f["h"]
    lines = title if isinstance(title, list) else split_lines(title)
    lines = [" ".join(l.replace("{", "(").replace("}", ")").split()) for l in lines if l.strip()][:3] or ["—"]
    pattern = PATTERNS[len(lines)][sum(map(ord, "".join(lines))) % len(PATTERNS[len(lines)])]

    max_size = 330 if fmt == "9x16" else 270
    sizes = [_fit_size(l, w * 0.84, max_size) for l in lines]
    # the block must stay between ~38% of the height and just above the logo; shrink it if it does not fit
    icon = 120 if fmt == "9x16" else 100
    bottom = h - int(h * 0.035) - icon - 40
    top = h * 0.38
    total = sum(s * 1.12 for s in sizes)
    if total > bottom - top:
        sizes = [int(s * (bottom - top) / total) for s in sizes]
    heights = [s * 1.12 for s in sizes]  # banners nearly touch, like the references
    center = min(h * BLOCK_CENTER[fmt], bottom - sum(heights) / 2)
    y = center - sum(heights) / 2
    styles, events = [], []
    for i, (line, size, look) in enumerate(zip(lines, sizes, pattern)):
        text_c, box_c = LOOKS[look]
        if box_c:  # opaque banner: BorderStyle 3 draws the box in the outline colour
            styles.append(f"Style: L{i},{FONT_NAME},{size},{text_c},{text_c},{box_c},&H00000000,0,0,0,0,100,100,0,0,3,{int(size * 0.06)},0,5,0,0,0,1")
        else:  # outlined text with a hard shadow
            styles.append(f"Style: L{i},{FONT_NAME},{size},{text_c},{text_c},{_BLACK},&H64000000,0,0,0,0,100,100,0,0,1,{int(size * 0.035)},{int(size * 0.03)},5,0,0,0,1")
        cy = y + heights[i] / 2
        y += heights[i]
        events.append(f"Dialogue: 0,0:00:00.00,0:00:10.00,L{i},,0,0,0,,{{\\pos({w / 2:.0f},{cy:.0f})\\frz2.5}}\u202b{line}\u202c")

    ass = out.with_suffix(".ass")
    ass.write_text(f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
""" + "\n".join(styles) + """

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
""" + "\n".join(events) + "\n", encoding="utf-8")

    fonts = Path("assets/fonts")
    ass_filter = f"ass={_filter_path(ass)}" + (f":fontsdir={_filter_path(fonts)}" if fonts.exists() else "")
    # darken towards the bottom so the headline pops, like the reference covers
    shade = f"color=black:s={w}x{h},format=rgba,geq=r=0:g=0:b=0:a='clip(255*(Y/H-0.35)/0.65*0.85\\,0\\,220)'"
    cmd = ["ffmpeg", "-y", "-i", str(bg), "-f", "lavfi", "-i", shade]
    fc = f"[0:v]eq=contrast=1.08:saturation=1.15[bg];[bg][1:v]overlay=0:0[sh];[sh]{ass_filter}[tx]"
    if LOGO_ICON.exists():  # small round brand icon at the bottom centre
        cmd += ["-i", str(LOGO_ICON)]
        fc += (f";[2:v]scale={icon}:{icon},format=rgba,"
               f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(lte(hypot(X-W/2,Y-H/2),W/2-1),255,0)'[lg];"
               f"[tx][lg]overlay=(W-w)/2:H-h-{int(h * 0.035)}[v]")
    else:
        fc += ";[tx]null[v]"
    _run(cmd + ["-filter_complex", fc, "-map", "[v]", "-frames:v", "1", "-q:v", "2", str(out)])
    return out


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg (cover) failed:\n" + r.stderr[-1000:])
