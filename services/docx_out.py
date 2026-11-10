"""Bulletin -> Word (.docx), right-to-left, in the layout of the approved bulletin."""
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

import config

MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
WEEKDAYS = ["دوشنبه", "سه شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]  # datetime.weekday() order


def to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + g_d_m[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        return jy, 1 + days // 31, 1 + days % 31
    return jy, 7 + (days - 186) // 30, 1 + (days - 186) % 30


def persian_date(d: datetime) -> str:
    jy, jm, jd = to_jalali(d.year, d.month, d.day)
    return f"{WEEKDAYS[d.weekday()]} {jd} {MONTHS[jm - 1]} {jy}"


def _rtl(paragraph, align=WD_ALIGN_PARAGRAPH.JUSTIFY):
    paragraph.alignment = align
    paragraph.paragraph_format.space_after = Pt(6)
    ppr = paragraph._p.get_or_add_pPr()
    ppr.insert(0, OxmlElement("w:bidi"))


def _run(paragraph, text, size=12, bold=False, color=None):
    r = paragraph.add_run(text)
    r.bold = bold
    r.font.size = Pt(size)
    r.font.name = config.BULLETIN_FONT
    if color:
        r.font.color.rgb = RGBColor(*color)
    rpr = r._r.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.insert(0, fonts)
    for attr in ("w:ascii", "w:hAnsi", "w:cs"):
        fonts.set(qn(attr), config.BULLETIN_FONT)
    for tag in ("w:rtl", "w:szCs") + (("w:bCs",) if bold else ()):
        el = OxmlElement(tag)
        if tag == "w:szCs":
            el.set(qn("w:val"), str(size * 2))
        rpr.append(el)
    return r


def write(bulletin: dict, out: Path, when: datetime, with_links: bool = False) -> Path:
    doc = Document()
    sec = doc.sections[0]
    sec._sectPr.append(OxmlElement("w:bidi"))
    title = doc.add_paragraph()
    _rtl(title, WD_ALIGN_PARAGRAPH.RIGHT)
    part = "صبح" if when.hour < 12 else "بعدازظهر" if when.hour < 18 else "شب"
    hour = when.hour if when.hour <= 12 else when.hour - 12
    _run(title, f"بولتن ساعت {hour} {part} {persian_date(when)}", 14, bold=True)
    if with_links:
        p = doc.add_paragraph()
        _rtl(p, WD_ALIGN_PARAGRAPH.RIGHT)
        _run(p, "نسخه‌ی بازبینی: زیر هر خبر لینک منبع آمده است. برای انتشار از فایل بدون لینک استفاده کنید.",
             10, color=(150, 0, 0))
    for s in bulletin["sections"]:
        h = doc.add_paragraph()
        _rtl(h, WD_ALIGN_PARAGRAPH.RIGHT)
        h.paragraph_format.space_before = Pt(12)
        _run(h, s["title"], 13, bold=True)
        for e in s["entries"]:
            p = doc.add_paragraph()
            _rtl(p)
            _run(p, (f"{e['source']}/ " if e["source"] else "") + e["text"], 12)
            if with_links and e.get("links"):
                lp = doc.add_paragraph()
                _rtl(lp, WD_ALIGN_PARAGRAPH.LEFT)
                for name, url in e["links"]:
                    _run(lp, f"{name}: {url}\n", 8, color=(90, 90, 90))
    doc.save(out)
    return out
