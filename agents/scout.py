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

import brands
import config
from services import llm
from services.sources_fa import source_fa

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

def load_topics(brand=None) -> str:
    return "\n".join(f"- {l}" for l in brands.get(getattr(brand, "key", brand)).lines("topics"))


def load_filter(brand=None) -> str:
    return "\n".join(brands.get(getattr(brand, "key", brand)).lines("filter"))


SYSTEM = """You are the news editor of {outlet}.
Its core topics are:
{topics}

You get a numbered list of news items (source, headline, snippet). For EVERY item (do not skip any):
- title_fa: a short, faithful Persian headline. Use ONLY the given text; never add facts. Persian items: keep or tidy the title.
- importance: an integer 1-5 for how important the item is for THIS outlet, using exactly this scale:
{scale}
Be strict: most items are 3 or lower. Routine price quotes, local incidents and administrative notices are never 4-5,
even when they mention the dollar, oil or energy. Analysis of WHY something moves, and decisions that change markets or
the war/geopolitical situation, are what earn 4-5.

- event: a short English slug naming the underlying event (e.g. "us-jets-leave-uk-base"); items that report the SAME
  event must get the SAME slug, so duplicates can be merged.

Reply with ONLY a JSON array (no markdown fence), one element per item, same ids:
[{{"id": <number>, "title_fa": "...", "importance": 3, "event": "..."}}]"""


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


async def _translate(batch: list[dict], offset: int, topics: str, brand) -> dict[int, dict]:
    listing = "\n".join(f"{offset + k}. [{i['source']}] {i['title']} — {i['snippet'][:160]}" for k, i in enumerate(batch))
    text, _ = await asyncio.to_thread(llm.generate, SYSTEM.format(topics=topics, scale=load_filter(brand), outlet=brand.outlet_en), listing,
                                      False, 6000)
    return {p["id"]: p for p in _extract_json(text) if isinstance(p, dict) and isinstance(p.get("id"), int)}


CLUSTER = """You find duplicate news. Below are numbered headlines from different outlets (Persian translation |
original). Group together the items that report the SAME specific event, statement or data release (same actors,
same action, same moment), not merely the same topic. Reply with ONLY a JSON array of groups, each group an array
of ids; list only groups with 2 or more items. Example: [[3, 17, 42], [5, 9]]"""
CLUSTER_MAX = 160  # items compared in one call (the important ones first)


async def merge_duplicates(items: list[dict]) -> list[dict]:
    """One item per story, with every outlet that reported it, in order of publication (first publisher first).

    Two signals are combined: the per-batch event slug from the translation step, and one grouping pass over all
    candidate headlines together (batches alone miss duplicates that landed in different batches).
    """
    parent = list(range(len(items)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    by_slug = {}
    for k, i in enumerate(items):
        if i.get("event"):
            if i["event"] in by_slug:
                union(k, by_slug[i["event"]])
            by_slug.setdefault(i["event"], k)
    cand = sorted((k for k, i in enumerate(items) if i["importance"] >= 3), key=lambda k: -items[k]["importance"])
    cand = cand[:CLUSTER_MAX]
    if len(cand) > 1:
        listing = "\n".join(f"{k}. [{items[k]['source']}] {items[k]['title_fa']} | {items[k]['title_en']}" for k in cand)
        try:
            text, _ = await asyncio.to_thread(llm.generate, CLUSTER, listing, False, 3000)
            for group in _extract_json(text):
                ids = [x for x in (group if isinstance(group, list) else []) if isinstance(x, int) and 0 <= x < len(items)]
                for x in ids[1:]:
                    union(ids[0], x)
        except Exception:
            pass  # grouping is a nicety; without it the list just shows the duplicates
    groups = {}
    for k in range(len(items)):
        groups.setdefault(find(k), []).append(items[k])
    merged = []
    for members in groups.values():
        members.sort(key=lambda i: i["published_iso"])  # first publisher first
        best = max(members, key=lambda i: (i["importance"], len(i["snippet"])))
        item = dict(best)
        item["importance"] = max(i["importance"] for i in members)
        item["sources"] = [{"source": i["source"], "url": i["url"], "published": i["published"],
                            "published_iso": i["published_iso"], "title_en": i["title_en"], "snippet": i["snippet"]}
                           for i in members]
        first = members[0]
        item.update(source=first["source"], url=first["url"], published=first["published"],
                    published_iso=first["published_iso"], hours_ago_estimate=first["hours_ago_estimate"])
        merged.append(item)
    merged.sort(key=lambda i: i["published_iso"], reverse=True)  # newest first; sorted by importance afterwards
    return merged


def feeds_summary(recent: list[dict], stories: list[dict], errors: list[str]) -> str:
    """Which outlets the search read: items in the window per outlet (feeds of one outlet added up), how many of
    them are in important stories, and outlets that had nothing in the window."""
    names = {}
    for name, _ in load_feeds():
        if name not in errors:
            names.setdefault(source_fa({"source": name}), 0)
    for i in recent:
        o = source_fa(i)
        names[o] = names.get(o, 0) + 1
    important = {}
    for st in stories:
        if st.get("relevant"):
            for o in {source_fa(x) for x in st.get("sources", [st])}:
                important[o] = important.get(o, 0) + 1
    active = sorted(((o, n) for o, n in names.items() if n), key=lambda x: -x[1])
    empty = [o for o, n in names.items() if not n]
    line = "📡 منابعی که خوانده شد (تعداد خبر در این بازه، و چندتایش در خبرهای مهم): " + "، ".join(
        f"{o} {n}" + (f" ({important[o]} مهم)" if important.get(o) else "") for o, n in active)
    if empty:
        line += "\n📭 بدون خبر در این بازه: " + "، ".join(empty)
    return line


async def find_news(hours: int | None = None, brand=None) -> tuple[list[dict], list[dict]]:
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

    brand = brands.get(getattr(brand, "key", brand))
    topics = load_topics(brand)
    sem = asyncio.Semaphore(3)  # stay under the free-tier requests-per-minute limit

    batch_errors = []

    async def run(start):
        async with sem:
            for attempt in range(2):  # a failed batch is retried once; failures are reported, never hidden
                try:
                    return await _translate(recent[start:start + BATCH], start, topics, brand)
                except Exception as e:
                    err = str(e)
                    if attempt == 0:
                        await asyncio.sleep(5)
            batch_errors.append(err[:300])
            return {}
    translated = {}
    for part in await asyncio.gather(*(run(s) for s in range(0, len(recent), BATCH))):
        translated.update(part)
    if not translated:  # nothing scored: say why instead of "no important news"
        raise RuntimeError("هوش مصنوعی به ترجمه و نمره‌دهی خبرها جواب نداد، پس لیست ساخته نشد. دلیل: "
                           + (batch_errors[0] if batch_errors else "پاسخ خالی") + "\nچند دقیقه بعد دوباره /news بزنید.")

    out = []
    for k, src in enumerate(recent):
        t = translated.get(k, {})
        age = (now - src["published"]).total_seconds() / 3600
        out.append({
            "title_fa": t.get("title_fa") or src["title"], "title_en": src["title"], "source": src["source"],
            "published": src["published"].strftime("%Y-%m-%d %H:%M UTC"),
            "hours_ago_estimate": f"{age:.0f} ساعت پیش", "time_confidence": "high",
            "url": src["url"], "summary_fa": "", "snippet": src["snippet"],
            "importance": int(t["importance"]) if isinstance(t.get("importance"), (int, float)) else 0,
            "event": str(t.get("event", "")).strip().lower(),
            "published_iso": src["published"].isoformat(),
        })
    out = await merge_duplicates(out)
    # most important first, newest first within the same score; untranslated items (score 0) stay visible in "show all"
    out.sort(key=lambda i: -i["importance"])
    for i in out:
        i["relevant"] = i["importance"] >= config.NEWS_MIN_IMPORTANCE
    n_imp = sum(1 for i in out if i["relevant"])
    notes = [{"title": f"بازه‌ی جست‌وجو: {window} ساعت اخیر؛ از {len(out)} خبر ({n_feeds - len(errors)} فید)، "
                       f"{n_imp} خبر مهم انتخاب شد (نمره‌ی اهمیت {config.NEWS_MIN_IMPORTANCE} از ۵ یا بیشتر)", "uri": ""}]
    notes.append({"title": feeds_summary(recent, out, errors), "uri": ""})
    if len(translated) < len(recent):
        notes.append({"title": f"⚠️ {len(recent) - len(translated)} خبر به خاطر خطای هوش مصنوعی ترجمه و نمره‌دهی نشد "
                               f"(در «نمایش همه» به زبان اصلی آمده‌اند): {(batch_errors or ['؟'])[0][:150]}", "uri": ""})
    if errors:
        failed = list(dict.fromkeys(source_fa({"source": e}) for e in errors))  # one name per outlet, in order
        notes.append({"title": f"⚠️ منابعی که باز نشدند ({len(errors)} فید): " + "، ".join(failed), "uri": ""})
    return out, notes
