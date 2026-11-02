"""Writer: turns one approved news item into narration + caption + scenario."""
import json
import re
from pathlib import Path

import anthropic

import config

STYLE = (Path(__file__).parent.parent / "prompts" / "writer_style.md").read_text(encoding="utf-8")

OUTPUT_SPEC = """
خروجی: فقط یک آبجکت JSON (بدون ```) با این کلیدها:
- hook: دو خط هوک
- narration: متن کامل نریشن (محاوره‌ای، با «/» برای مکث)
- caption: کپشن اینستاگرام (کوتاه)
- hashtags: لیست هشتگ
- scenario: لیست صحنه‌ها؛ هر صحنه {time, visual, text_on_screen}
- flags: لیست جمله‌های تفسیری یا تأییدنشده‌ی متن که کاربر باید بررسی کند
- tips: حداکثر ۳ پیشنهاد برای جذابیت بیشتر
"""


async def write_package(news: dict, feedback: str | None = None, previous: dict | None = None) -> dict:
    client = anthropic.AsyncAnthropic()
    user = "خبر ورودی:\n" + json.dumps(news, ensure_ascii=False, indent=2)
    if previous and feedback:
        user += "\n\nنسخه‌ی قبلی:\n" + json.dumps(previous, ensure_ascii=False)
        user += f"\n\nبازخورد کاربر (اعمال کن):\n{feedback}"
    resp = await client.messages.create(
        model=config.WRITER_MODEL,
        max_tokens=3000,
        system=STYLE + OUTPUT_SPEC,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("Writer returned no JSON:\n" + text[:500])
    return json.loads(m.group(0))
