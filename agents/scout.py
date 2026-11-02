"""News Scout: finds important headlines of the last N hours using Claude + web search."""
import json
import re

import anthropic

import config

SYSTEM = """You are a news scout. Find the most important news of the LAST {hours} HOURS on: Iran politics,
Iran war/military news, oil prices, USD/Iran rial rate, gold price, and anything tied to politics/oil/dollar/gold.
Prefer major international outlets (Reuters, AP, BBC, Bloomberg, Al Jazeera, FT, WSJ).
Never invent anything. Every item needs a real URL. If you cannot confirm that the item is inside the time window,
say so in time_confidence ("high"|"medium"|"low").

Reply with ONLY a JSON array (no markdown fence) of up to {n} objects with keys:
title_fa, title_en, source, published (string, as precise as you know), hours_ago_estimate, time_confidence,
url, summary_fa (1-2 sentences, verified facts only), has_video ("yes"|"no"|"unknown")."""


def _extract_json(text: str):
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError("Scout returned no JSON array:\n" + text[:500])
    return json.loads(m.group(0))


async def find_news(hours: int | None = None, n: int = 8) -> list[dict]:
    hours = hours or config.NEWS_WINDOW_HOURS
    client = anthropic.AsyncAnthropic()
    messages = [{"role": "user", "content": f"Find the top {n} news items now."}]
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 10}]
    for _ in range(5):  # continue if the server-side search loop pauses
        resp = await client.messages.create(
            model=config.SCOUT_MODEL,
            max_tokens=6000,
            system=SYSTEM.format(hours=hours, n=n),
            tools=tools,
            messages=messages,
        )
        if resp.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": resp.content})
    text = "".join(b.text for b in resp.content if b.type == "text")
    return _extract_json(text)
