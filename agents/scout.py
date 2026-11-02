"""News Scout: finds important headlines of the last N hours using an LLM with web search."""
import asyncio
import json
import re

import config
from services import llm

SYSTEM = """You are a news scout. Find the most important news of the LAST {hours} HOURS on: Iran politics,
Iran war/military news, oil prices, USD/Iran rial rate, gold price, and anything tied to politics/oil/dollar/gold.
Prefer major international outlets (Reuters, AP, BBC, Bloomberg, Al Jazeera, FT, WSJ).
Never invent anything. Only include items you actually found via search, with their real URL. If you cannot confirm that
an item is inside the time window, say so in time_confidence ("high"|"medium"|"low").

Reply with ONLY a JSON array (no markdown fence) of up to {n} objects with keys:
title_fa, title_en, source, published (string, as precise as you know), hours_ago_estimate, time_confidence,
url, summary_fa (1-2 sentences, verified facts only), has_video ("yes"|"no"|"unknown")."""


def _extract_json(text: str):
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError("Scout returned no JSON array:\n" + text[:500])
    return json.loads(m.group(0))


async def find_news(hours: int | None = None, n: int = 8) -> tuple[list[dict], list[dict]]:
    """Returns (items, grounding_sources). Sources let the user verify the model's claims."""
    hours = hours or config.NEWS_WINDOW_HOURS
    text, sources = await asyncio.to_thread(
        llm.generate, SYSTEM.format(hours=hours, n=n), f"Find the top {n} news items now.", True, 6000)
    return _extract_json(text), sources
