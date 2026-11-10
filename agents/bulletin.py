"""Daily bulletin: every feed item of the last N hours -> sections -> «منبع/ متن» entries -> Word file.

Two LLM passes: (1) sort each item into a section (or skip it), (2) write each section from its items only.
Facts come only from the feed items; every entry keeps the ids of the items it was written from, for checking.
"""
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from agents.scout import _fetch_all
from services import llm
from services.sources_fa import source_fa

ROOT = Path(__file__).parent.parent
SECTIONS_FILE = ROOT / "prompts" / "bulletin_sections.txt"
STYLE = (ROOT / "prompts" / "bulletin_style.md").read_text(encoding="utf-8")
CLASSIFY_BATCH = 120  # items per sorting request (fewer requests = less rate limiting)
MAX_PER_SECTION = 40       # items handed to the writer per section (newest first)
MAX_ENTRIES = 12           # entries written per section


def load_sections() -> list[dict]:
    out = []
    for line in SECTIONS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.count("|") < 2:
            continue
        key, title, desc = (x.strip() for x in line.split("|", 2))
        out.append({"key": key, "title": title, "desc": desc})
    return out


def _json_array(text: str):
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError("no JSON array in the reply:\n" + text[:300])
    return json.loads(m.group(0))


CLASSIFY = """You sort news items into the sections of a Persian daily news bulletin for an Iranian oil & energy outlet.
Sections (key: what belongs there):
{sections}
- skip: anything that fits none of the sections (sports, celebrities, unrelated world news, ads)

Each item goes into exactly ONE section: the most specific one (e.g. a Trump statement about the deal -> trump;
a strike on a tanker in Hormuz -> hormuz). Read each section's description literally: "domestic" is ONLY about events
inside Iran; "mfa" is ONLY Iran's foreign-ministry spokesman. Reply with ONLY a JSON array, one element per item:
[{{"id": <number>, "section": "<key or skip>"}}]"""


WRITE = """تو دبیر بولتن خبری صبحگاهی «آوای نفت» هستی. بخش «{title}» بولتن را از روی خبرهای ورودی می‌نویسی.
{style}

## قواعد
- هر مدخل: نام منبع + «/ » + متن خبر به فارسی روان و رسمی؛ ۱ تا ۴ جمله؛ مثل نمونه.
- source فقط و فقط نام یک منبع است، همان نامی که کنار خبر آمده (مثلاً «بلومبرگ»، «تسنیم»)؛ هرگز چند منبع با «/» یا «،» کنار هم ننویس. اگر خبر از قول کسی است، مثل نمونه بنویس: «منبع/ گوینده: …» یا «منبع/ … گفت …».
- خبرهای تکراری درباره‌ی یک موضوع را در یک مدخل ادغام کن و فقط منبعِ کامل‌ترین خبر را بیاور (همه‌ی شماره‌ها را در ids بگذار). ادعا و تکذیبِ یک موضوع را پشت سر هم بیاور.
- مهم‌ترین مدخل‌ها اول؛ حداکثر {max_entries} مدخل.
- عنوان بخش فقط تیتر بخش است؛ عبارت‌ها یا صفت‌های عنوان بخش را وارد متن خبرها نکن. افراد را با نام و سمتی که در خبر آمده بنویس (مثلاً «ترامپ»، «رئیس‌جمهور آمریکا»).
- فقط واقعیت‌هایی که در متن خبرهای ورودی هست؛ هیچ عدد، نقل‌قول، نام یا رویدادی اضافه نکن. ادعاها را با «ادعا کرد»، «به گزارش …» نسبت بده. خبرهای انگلیسی/عربی را دقیق ترجمه کن.
{extra}
خروجی فقط یک آرایه‌ی JSON (بدون ```):
[{{"source": "نام منبع", "text": "متن مدخل", "ids": [شماره‌ی خبرهایی که مدخل از آن‌ها نوشته شده]}}]"""

PRICE_RULE = ("- این بخش فقط قیمت‌هاست: هر مدخل باید عدد قیمت (نفت، بنزین، گاز، طلا، سکه، دلار) داشته باشد، عیناً همان که "
              "در خبرها آمده، با منبع. خبرِ بدون عدد قیمت را ننویس. اگر قیمتی در خبرها نیست، آن را از خودت ننویس.")


async def collect(hours: int) -> tuple[list[dict], list[str]]:
    items, errors, _ = await _fetch_all()
    now = datetime.now(timezone.utc)
    recent = sorted((i for i in items if now - i["published"] <= timedelta(hours=hours)),
                    key=lambda i: i["published"], reverse=True)
    seen, out = set(), []
    for i in recent:
        key = re.sub(r"\W+", "", i["title"].lower())
        if key and key not in seen:
            seen.add(key)
            out.append(i)
    return out, errors


async def classify(items: list[dict], sections: list[dict]) -> tuple[dict[int, str], list[str]]:
    sec_text = "\n".join(f"- {s['key']}: {s['desc']}" for s in sections)
    keys = {s["key"] for s in sections}
    sem = asyncio.Semaphore(3)

    async def batch(start):
        part = items[start:start + CLASSIFY_BATCH]
        listing = "\n".join(f"{start + k}. [{source_fa(i)}] {i['title']} — {i['snippet'][:200]}" for k, i in enumerate(part))
        async with sem:
            for attempt in range(2):  # a failed batch is retried once; it is never silently dropped
                try:
                    text, _ = await asyncio.to_thread(llm.generate, CLASSIFY.format(sections=sec_text), listing, False,
                                                      6000)
                    got = {p["id"]: p["section"] for p in _json_array(text)
                           if isinstance(p, dict) and isinstance(p.get("id"), int) and p.get("section") in keys | {"skip"}}
                    return got, None
                except Exception as e:
                    err = str(e)[:200]
            return {}, err
    result, errors = {}, []
    for part, err in await asyncio.gather(*(batch(s) for s in range(0, len(items), CLASSIFY_BATCH))):
        result.update(part)
        if err:
            errors.append(err)
    return result, errors


async def write_section(section: dict, items: list[tuple[int, dict]]) -> list[dict]:
    listing = "\n\n".join(f"{k}. منبع: {source_fa(i)} | زمان: {i['published']:%H:%M UTC}\nتیتر: {i['title']}\nمتن: {i['snippet']}"
                          for k, i in items)
    system = WRITE.format(title=section["title"], style=STYLE, max_entries=MAX_ENTRIES,
                          extra=PRICE_RULE if section["key"] == "prices" else "")
    text, _ = await asyncio.to_thread(llm.generate, system, listing, False, 6000)
    valid = {k for k, _ in items}
    out = []
    for e in _json_array(text):
        if isinstance(e, dict) and str(e.get("text", "")).strip():
            ids = [x for x in e.get("ids", []) if isinstance(x, int) and x in valid]
            source = re.split(r"\s*[/،,]\s*", str(e.get("source", "")).strip())[0]  # one source per entry
            out.append({"source": source, "text": str(e["text"]).strip(), "ids": ids})
    return out[:MAX_ENTRIES]


async def build(hours: int | None = None) -> dict:
    """Returns {'sections': [{'title', 'entries': [{'source','text','links'}]}], 'stats': {...}, 'errors': [...]}."""
    hours = hours or config.BULLETIN_HOURS
    sections = load_sections()
    items, errors = await collect(hours)
    if not items:
        raise RuntimeError("هیچ خبری از فیدها دریافت نشد. فیدهای ناموفق: " + ", ".join(errors or ["—"]))
    where, class_errors = await classify(items, sections)
    if not where:
        raise RuntimeError("دسته‌بندی خبرها با هوش مصنوعی انجام نشد: " + "؛ ".join(class_errors or ["پاسخ خالی"]))
    sem = asyncio.Semaphore(3)

    async def one(sec):
        mine = [(k, i) for k, i in enumerate(items) if where.get(k) == sec["key"]][:MAX_PER_SECTION]
        if not mine:
            return sec, [], None
        async with sem:
            try:
                return sec, await write_section(sec, mine), None
            except Exception:
                try:  # one retry (usually a rate-limit hiccup)
                    return sec, await write_section(sec, mine), None
                except Exception as e:
                    return sec, [], str(e)[:200]
    out_sections, failed = [], []
    for sec, entries, err in await asyncio.gather(*(one(s) for s in sections)):
        if err:
            failed.append(sec["title"])
        if entries:
            for e in entries:
                e["links"] = [(source_fa(items[k]), items[k]["url"]) for k in e.pop("ids")]
            out_sections.append({"title": sec["title"], "entries": entries})
    sorted_n = len(where)
    used = sum(1 for v in where.values() if v != "skip")
    return {"sections": out_sections, "errors": errors, "failed": failed, "class_errors": class_errors,
            "stats": {"items": len(items), "sorted": sorted_n, "used": used, "hours": hours}}
