r"""Check every feed in prompts/news_sources.txt and show which ones work.

Run from the newsbot folder:  .venv\Scripts\python tools\check_sources.py   (Mac/Linux: .venv/bin/python tools/check_sources.py)
"""
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx  # noqa: E402

from agents.scout import _parse, load_feeds  # noqa: E402


async def check(c, name, url):
    try:
        r = await c.get(url)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        items = _parse(name, r.content)
        if not items:
            return False, "no news items in the feed"
        newest = max(i["published"] for i in items)
        age = (datetime.now(timezone.utc) - newest).total_seconds() / 3600
        return True, f"{len(items)} items, newest {age:.0f}h ago | {items[0]['title'][:60]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"


async def main():
    feeds = load_feeds()
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
        results = await asyncio.gather(*(check(c, n, u) for n, u in feeds))
    for (name, _), (ok, info) in zip(feeds, results):
        print(f"{'OK  ' if ok else 'FAIL'}  {name:34s} {info}")
    print(f"\n{sum(ok for ok, _ in results)} of {len(feeds)} feeds work")


asyncio.run(main())
