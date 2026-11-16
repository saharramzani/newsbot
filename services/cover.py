"""Cover (thumbnail) for each video: a good frame from the source + bold headline banners + brand icon."""
import subprocess
from pathlib import Path

import brands
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


_RGB = {_WHITE: (255, 255, 255), _YELLOW: (251, 184, 22), _RED: (215, 25, 31)}


def _brush(bw: int, bh: int, rgb: tuple, seed: int, out: Path) -> tuple[int, int]:
    """A paint-brush stroke banner (rough edges, dry-brush streaks), rotated like the headline. Returns its size."""
    import random

    from PIL import Image, ImageDraw, ImageFilter
    rnd = random.Random(seed)
    m = int(bh * 0.3)
    img = Image.new("RGBA", (bw + 2 * m, bh + 2 * m), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    step = max(8, bh // 6)
    pts = [(x, m + rnd.uniform(-0.07, 0.07) * bh) for x in range(m, m + bw + 1, step)]
    pts += [(m + bw + rnd.uniform(-0.04, 0.14) * bh, m + t * bh) for t in (0.2, 0.45, 0.7, 0.9)]
    pts += [(x, m + bh + rnd.uniform(-0.07, 0.07) * bh) for x in range(m + bw, m - 1, -step)]
    pts += [(m - rnd.uniform(-0.04, 0.14) * bh, m + t * bh) for t in (0.9, 0.7, 0.45, 0.2)]
    d.polygon(pts, fill=rgb + (255,))
    for _ in range(max(6, bw // 35)):  # dry-brush streaks along the stroke
        y = m + rnd.uniform(0.04, 0.96) * bh
        x0 = m + rnd.uniform(-0.02, 0.6) * bw
        shade = tuple(max(0, min(255, int(c * rnd.uniform(0.82, 1.1)))) for c in rgb)
        d.line([(x0, y), (x0 + rnd.uniform(0.2, 0.5) * bw, y + rnd.uniform(-2, 2))], fill=shade + (255,),
               width=max(1, int(bh * 0.03)))
    for _ in range(16):  # bristle gaps at both ends
        y = m + rnd.uniform(0, 1) * bh
        length = rnd.uniform(0.02, 0.09) * bw
        x0 = m - 6 if rnd.random() < 0.5 else m + bw - length + 6
        d.line([(x0, y), (x0 + length, y)], fill=(0, 0, 0, 0), width=max(1, int(bh * 0.025)))
    img = img.filter(ImageFilter.GaussianBlur(0.8)).rotate(2.5, resample=Image.BICUBIC, expand=True)
    img.save(out)
    return img.size


def compose(bg: Path, title, out: Path, fmt: str = "9x16", icon_path: Path | None = None) -> Path:
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
    styles, events, brushes = [], [], []
    for i, (line, size, look) in enumerate(zip(lines, sizes, pattern)):
        text_c, box_c = LOOKS[look]
        cy = y + heights[i] / 2
        y += heights[i]
        if box_c:  # brush-stroke banner behind the text (drawn as an image), text without outline on top
            width100, line_h100 = _measure(line)
            text_px = width100 * (size * 100 / line_h100) / 100
            bw = int(min(w * 0.97, text_px + size * 0.55))
            bh = int(size * 0.92)
            png = out.with_name(f"{out.stem}_brush{i}.png")
            pw, ph = _brush(bw, bh, _RGB[box_c], sum(map(ord, line)) + i, png)
            brushes.append((png, int(w / 2 - pw / 2), int(cy - ph / 2)))
            styles.append(f"Style: L{i},{FONT_NAME},{size},{text_c},{text_c},{_BLACK},&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,-1")
        else:  # white text with a heavy black outline and hard shadow
            styles.append(f"Style: L{i},{FONT_NAME},{size},{text_c},{text_c},{_BLACK},&H96000000,0,0,0,0,100,100,0,0,1,{int(size * 0.06)},{int(size * 0.04)},5,0,0,0,-1")
        events.append(f"Dialogue: 0,0:00:00.00,0:00:10.00,L{i},,0,0,0,,{{\\pos({w / 2:.0f},{cy:.0f})\\frz2.5}}\u200f{line}")  # Encoding -1 + RLM: right-to-left (see video.build_ass)

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
    fc = f"[0:v]eq=contrast=1.08:saturation=1.15[bg];[bg][1:v]overlay=0:0[b0]"
    for k, (png, bx, by) in enumerate(brushes):  # brush banners, then the text above them
        cmd += ["-i", str(png)]
        fc += f";[b{k}][{k + 2}:v]overlay={bx}:{by}[b{k + 1}]"
    fc += f";[b{len(brushes)}]{ass_filter}[tx]"
    logo_in = len(brushes) + 2
    icon_file = Path(icon_path or LOGO_ICON)
    if icon_file.exists():  # small round brand icon at the bottom centre
        cmd += ["-i", str(icon_file)]
        fc += (f";[{logo_in}:v]scale={icon}:{icon},format=rgba,"
               f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(lte(hypot(X-W/2,Y-H/2),W/2-1),255,0)'[lg];"
               f"[tx][lg]overlay=(W-w)/2:H-h-{int(h * 0.035)}[v]")
    else:
        fc += ";[tx]null[v]"
    _run(cmd + ["-filter_complex", fc, "-map", "[v]", "-frames:v", "1", "-q:v", "2", str(out)])
    for png, _, _ in brushes:
        png.unlink(missing_ok=True)
    return out


def cover_style(brand=None) -> str:
    try:
        return "\n".join(brands.get(getattr(brand, "key", brand)).lines("cover_style"))
    except OSError:
        return ""


AI_PROMPT = """Design the BACKGROUND image of a news cover (vertical {ratio}) for {outlet}.
Story headline: {headline}
Story: {topic}

STYLE:
{style}
{extra}
The attached image is real material for this story (a frame of the news footage or a photo). Build the design around
it: keep any real person in it recognisable and unaltered (same face, expression, gesture; you may cut them out,
enlarge, re-light and place them in the new scene).
RULES: do NOT invent or add any other real, identifiable person. NO text, letters, numbers, logos or watermarks
anywhere in the image (a separate step adds the headline)."""


AI_PROMPT_TEXT = """Design the BACKGROUND image of a news cover (vertical {ratio}) for {outlet}.
Story headline: {headline}
Story: {topic}

STYLE:
{style}
{extra}
Show the story through places and symbols only. Do NOT show any real, identifiable person.
NO text, letters, numbers, logos or watermarks anywhere in the image (a separate step adds the headline)."""


def ai_background(frame: Path, headline: str, topic: str, out: Path, fmt: str = "9x16", extra: str = "",
                  brand=None) -> Path:
    """The image model redesigns real material (video frame or a photo the user sent) into a cover background.

    No text in the image: the Persian headline banners and the logo are added by compose().
    `extra`: the user's own design instructions for this cover (they override the style).
    """
    from services import llm
    f = FORMATS[fmt]
    ratio = "9:16" if fmt == "9x16" else "4:5"
    brand = brands.get(getattr(brand, "key", brand))
    fields = dict(ratio=ratio, headline=headline.replace("\n", " "), topic=topic or "—", style=cover_style(brand),
                  outlet=brand.outlet_en,
                  extra=f"\nTHE EDITOR'S INSTRUCTIONS FOR THIS COVER (follow them; they override the style):\n{extra}\n"
                  if extra else "")
    data = llm.generate_image(AI_PROMPT.format(**fields), frame.read_bytes(), ratio,
                              fallback_prompt=AI_PROMPT_TEXT.format(**fields))
    raw = out.with_name(out.stem + "_raw" + (llm.image_ext(data) or ".png"))  # ffmpeg picks the decoder by extension
    raw.write_bytes(data)
    _run(["ffmpeg", "-y", "-i", str(raw), "-vf",
          f"scale={f['w']}:{f['h']}:force_original_aspect_ratio=increase,crop={f['w']}:{f['h']},setsar=1",
          "-frames:v", "1", str(out)])
    raw.unlink(missing_ok=True)
    return out


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg (cover) failed:\n" + r.stderr[-1000:])
