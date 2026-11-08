"""Writer: turns one approved news item into a full media package (cover options, angle, B-roll, narration, caption)."""
import asyncio
import json
import re
from pathlib import Path

from services import llm

STYLE = (Path(__file__).parent.parent / "prompts" / "writer_style.md").read_text(encoding="utf-8")

OUTPUT_SPEC = """
خروجی: فقط یک آبجکت JSON (بدون ```) با این کلیدها:
- cover_options: لیست دقیقاً ۳ گزینه‌ی تیتر کاور؛ هر گزینه {"title": "تیتر کامل", "lines": ["خط۱", "خط۲", "خط۳"]}
  (lines همان تیتر است که برای چاپ روی کاور به ۲ یا ۳ خط کوتاه شکسته شده؛ هر خط ۱ تا ۳ کلمه؛ خط آخر ضربه‌ی اصلی)
- topic: موضوع
- references: منبع و رفرنس
- angle: زاویه دید رسانه‌ای
- hook: قلاب جذابیت
- broll: {"raw": [۳ کلیدواژه], "location": [۳ کلیدواژه], "graphics": [۳ کلیدواژه]}
- narration_tone: لحن نریشن (چند صفت)
- narration: متن نریتور
- caption: کپشن کامل اینستاگرام (بدون هشتگ و بدون خط «منبع:»)
- hashtags: لیست هشتگ‌ها (اولی #آوای_نفت)
- flags: لیست جمله‌های تفسیری یا تأییدنشده که کاربر باید بررسی کند
"""


async def write_package(news: dict, feedback: str | None = None, previous: dict | None = None) -> dict:
    user = "خبر ورودی:\n" + json.dumps(news, ensure_ascii=False, indent=2)
    if previous and feedback:
        user += "\n\nنسخه‌ی قبلی:\n" + json.dumps(previous, ensure_ascii=False)
        user += f"\n\nبازخورد کاربر (اعمال کن):\n{feedback}"
    text, _ = await asyncio.to_thread(llm.generate, STYLE + OUTPUT_SPEC, user, False, 7000)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("Writer returned no JSON:\n" + text[:500])
    return json.loads(m.group(0))
