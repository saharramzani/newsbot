"""News Scout: reads RSS feeds of major outlets, filters by time window, lets the LLM rank/translate.

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


SYSTEM = """You are a news editor for Avaye Naft, a Persian-language news outlet specialised in oil and energy.
The channel covers ONLY these topics:
{topics}

You get a numbered list of recent items (title + snippet). Pick up to {n} of the most important ones that clearly fit
the topics above; ignore items outside them (sports, celebrity, local crime, etc.), even if they are big news.
Prefer stories that matter to an Iranian audience; skip duplicates of the same story (keep the best one).
Use ONLY the given text. Never add facts that are not in the item.

Reply with ONLY a JSON array (no markdown fence), most important first. Each element:
{{"id": <number from the list>, "title_fa": "short punchy Persian headline", "summary_fa": "1-2 Persian sentences, only facts from the item", "has_video": "yes|no|unknown"}}"""


def _text(el, *names):
    for n in names:
        found = el.find(n)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return ""


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _parse(name: str, xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    out = []
    for it in root.iter("item"):
        try:
            published = parsedate_to_datetime(_text(it, "pubDate"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        out.append({"source": name, "title": _clean(_text(it, "title")), "url": _text(it, "link"),
                    "snippet": _clean(_text(it, "description"))[:400], "published": published})
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


async def find_news(hours: int | None = None, n: int = 8) -> tuple[list[dict], list[dict]]:
    """Returns (items, notes). `notes` is a list of {'title','uri'} shown to the user as extra info."""
    hours = hours or config.NEWS_WINDOW_HOURS
    items, errors, n_feeds = await _fetch_all()
    now = datetime.now(timezone.utc)
    window = hours
    recent = [i for i in items if now - i["published"] <= timedelta(hours=window)]
    if len(recent) < 8:  # quiet period: widen the window and say so
        window = 24
        recent = [i for i in items if now - i["published"] <= timedelta(hours=window)]
    recent.sort(key=lambda i: i["published"], reverse=True)
    recent = recent[:80]
    if not recent:
        raise RuntimeError("هیچ خبری از فیدها دریافت نشد. فیدهای ناموفق: " + ", ".join(errors or ["—"]))

    listing = "\n".join(f"{k}. [{i['source']}] {i['title']} — {i['snippet']}" for k, i in enumerate(recent))
    text, _ = await asyncio.to_thread(llm.generate, SYSTEM.format(n=n, topics=load_topics()), listing, False, 4000)
    picks = _extract_json(text)

    out = []
    for p in picks:
        k = p.get("id")
        if not isinstance(k, int) or not 0 <= k < len(recent):
            continue
        src = recent[k]
        age = (now - src["published"]).total_seconds() / 3600
        out.append({
            "title_fa": p.get("title_fa", src["title"]), "title_en": src["title"], "source": src["source"],
            "published": src["published"].strftime("%Y-%m-%d %H:%M UTC"),
            "hours_ago_estimate": f"{age:.0f} ساعت پیش", "time_confidence": "high",
            "url": src["url"], "summary_fa": p.get("summary_fa", ""), "has_video": p.get("has_video", "unknown"),
        })
    notes = [{"title": f"بازه‌ی جست‌وجو: {window} ساعت اخیر؛ {len(recent)} خبر از {n_feeds - len(errors)} فید", "uri": ""}]
    if errors:
        notes.append({"title": "فیدهای ناموفق: " + ", ".join(errors), "uri": ""})
    return out, notes
