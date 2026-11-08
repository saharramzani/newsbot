"""News Scout: reads RSS feeds, keeps EVERY item in the time window, the LLM only translates headlines.

Links, sources and publish times come from the feeds themselves (never from the model), so nothing is invented.
"""
import asyncio
import html
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

import config
from services import llm

PER_SOURCE, MAX_ITEMS = 40, 400  # safety caps only: newest items per source, total
BATCH = 50  # headlines translated per LLM call
SOURCES_FILE = Path(__file__).parent.parent / "prompts" / "news_sources.txt"


def load_feeds() -> list[tuple[str, str]]:
    """Lines 'name | url' from prompts/news_sources.txt; '#' lines are ignored."""
    feeds = []
    for line in SOURCES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        name, url = (x.strip() for x in line.split("|", 1))
        if url.startswith("http"):
            feeds.append((name, url))
    return feeds

TOPICS_FILE = Path(__file__).parent.parent / "prompts" / "scout_topics.md"


def load_topics() -> str:
    lines = [l.strip() for l in TOPICS_FILE.read_text(encoding="utf-8").splitlines()]
    return "\n".join(f"- {l}" for l in lines if l and not l.startswith("#"))


SYSTEM = """You translate news headlines for Avaye Naft, a Persian-language news outlet specialised in oil and energy.
Its core topics are:
{topics}

You get a numbered list of news items (source, headline, snippet). For EVERY item (do not skip any):
- title_fa: a short, faithful Persian headline. Use ONLY the given text; never add facts. Persian items: keep or tidy the title.
- relevant: true if the item clearly fits the core topics above, else false.

Reply with ONLY a JSON array (no markdown fence), one element per item, same ids:
[{{"id": <number>, "title_fa": "...", "relevant": true}}]"""


def _text(el, *names):
    for n in names:
        found = el.find(n)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return ""


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el, *names) -> str:
    """Text of the first non-empty child whose local tag name is in names (works with XML namespaces)."""
    for n in names:
        for ch in el:
            if _local(ch.tag) == n and (ch.text or "").strip():
                return ch.text.strip()
    return ""


def _date(s: str):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _parse(name: str, xml_bytes: bytes) -> list[dict]:
    """RSS <item> and Atom <entry> feeds."""
    root = ET.fromstring(xml_bytes)
    google_news = False
    out = []
    for it in root.iter():
        kind = _local(it.tag)
        if kind not in ("item", "entry"):
            continue
        published = _date(_child(it, "pubDate", "published", "updated", "date"))
        if not published:
            continue
        link = _child(it, "link")
        if not link:  # Atom: <link href="..."/>
            for ch in it:
                if _local(ch.tag) == "link" and ch.get("href"):
                    link = ch.get("href")
                    break
        title = _clean(_child(it, "title"))
        google_news = google_news or "news.google.com" in link
        if google_news:  # Google News titles end with " - Publisher"
            title = re.sub(r"\s+-\s+[^-]{2,40}$", "", title)
        out.append({"source": name, "title": title, "url": link,
                    "snippet": _clean(_child(it, "description", "summary", "content"))[:400], "published": published})
    return out


async def _fetch_all() -> tuple[list[dict], list[str], int]:
    FEEDS = load_feeds()
    items, errors = [], []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
        results = await asyncio.gather(*(c.get(u) for _, u in FEEDS), return_exceptions=True)
    for (name, _), r in zip(FEEDS, results):
        if isinstance(r, Exception) or r.status_code != 200:
            errors.append(name)
            continue
        try:
            items += _parse(name, r.content)
        except Exception:
            errors.append(name)
    return items, errors, len(FEEDS)


def _extract_json(text: str):
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError("Scout returned no JSON array:\n" + text[:500])
    return json.loads(m.group(0))


async def _translate(batch: list[dict], offset: int, topics: str) -> dict[int, dict]:
    listing = "\n".join(f"{offset + k}. [{i['source']}] {i['title']} — {i['snippet'][:160]}" for k, i in enumerate(batch))
    text, _ = await asyncio.to_thread(llm.generate, SYSTEM.format(topics=topics), listing, False, 6000)
    return {p["id"]: p for p in _extract_json(text) if isinstance(p, dict) and isinstance(p.get("id"), int)}


async def find_news(hours: int | None = None) -> tuple[list[dict], list[dict]]:
    """ALL items from the feeds in the time window (newest first), headlines translated to Persian.

    Returns (items, notes). `notes` is a list of {'title','uri'} shown to the user as extra info.
    """
    hours = hours or config.NEWS_WINDOW_HOURS
    items, errors, n_feeds = await _fetch_all()
    now = datetime.now(timezone.utc)
    window = hours
    recent = [i for i in items if now - i["published"] <= timedelta(hours=window)]
    if len(recent) < 8:  # quiet period: widen the window and say so
        window = 24
        recent = [i for i in items if now - i["published"] <= timedelta(hours=window)]
    recent.sort(key=lambda i: i["published"], reverse=True)
    seen, per_source, picked = set(), {}, []
    for i in recent:
        key = re.sub(r"\W+", "", i["title"].lower())
        if key in seen or per_source.get(i["source"], 0) >= PER_SOURCE:  # same headline in two feeds of one site
            continue
        seen.add(key)
        per_source[i["source"]] = per_source.get(i["source"], 0) + 1
        picked.append(i)
    recent = picked[:MAX_ITEMS]
    if not recent:
        raise RuntimeError("هیچ خبری از فیدها دریافت نشد. فیدهای ناموفق: " + ", ".join(errors or ["—"]))

    topics = load_topics()
    sem = asyncio.Semaphore(3)  # stay under the free-tier requests-per-minute limit

    async def run(start):
        async with sem:
            try:
                return await _translate(recent[start:start + BATCH], start, topics)
            except Exception:
                return {}
    translated = {}
    for part in await asyncio.gather(*(run(s) for s in range(0, len(recent), BATCH))):
        translated.update(part)

    out = []
    for k, src in enumerate(recent):
        t = translated.get(k, {})
        age = (now - src["published"]).total_seconds() / 3600
        out.append({
            "title_fa": t.get("title_fa") or src["title"], "title_en": src["title"], "source": src["source"],
            "published": src["published"].strftime("%Y-%m-%d %H:%M UTC"),
            "hours_ago_estimate": f"{age:.0f} ساعت پیش", "time_confidence": "high",
            "url": src["url"], "summary_fa": "", "snippet": src["snippet"],
            "relevant": bool(t.get("relevant", False)),
        })
    notes = [{"title": f"بازه‌ی جست‌وجو: {window} ساعت اخیر؛ همه‌ی {len(out)} خبر از {n_feeds - len(errors)} فید"
                       f" (⭐ = مرتبط با موضوعات اصلی)", "uri": ""}]
    if len(translated) < len(recent):
        notes.append({"title": f"⚠️ {len(recent) - len(translated)} تیتر ترجمه نشد و به زبان اصلی آمده", "uri": ""})
    if errors:
        notes.append({"title": "فیدهای ناموفق: " + ", ".join(errors), "uri": ""})
    return out, notes
