"""Writer: turns one approved news item into narration + caption + scenario."""
import asyncio
import json
import re
from pathlib import Path

from services import llm

STYLE = (Path(__file__).parent.parent / "prompts" / "writer_style.md").read_text(encoding="utf-8")

OUTPUT_SPEC = """
خروجی: فقط یک آبجکت JSON (بدون ```) با این کلیدها:
- hook: دو خط هوک
- narration: متن کامل نریشن (محاوره‌ای، با «/» برای مکث)
- caption: کپشن اینستاگرام (کوتاه)؛ رفرنس خبر را طبیعی در متن بیاور (مثلاً «به گزارش بلومبرگ»)؛ خط «منبع:» و هشتگ‌ها را ننویس، خودکار اضافه می‌شوند
- cover_lines: تیتر کاور ویدئو به‌صورت لیست ۲ یا ۳ خط کوتاه (هر خط ۱ تا ۳ کلمه، جمعاً ۳ تا ۷ کلمه)؛ کوبنده و کنجکاوی‌برانگیز، فقط بر پایه‌ی خبر؛ خط آخر ضربه‌ی اصلی است و با «!» یا «؟» تمام می‌شود؛ بدون هشتگ و ایموجی. مثال: ["نتانیاهو", "ایران را", "تهدید کرد!"]
- hashtags: لیست هشتگ
- scenario: لیست صحنه‌ها؛ هر صحنه {time, visual, text_on_screen}
- flags: لیست جمله‌های تفسیری یا تأییدنشده‌ی متن که کاربر باید بررسی کند
- tips: حداکثر ۳ پیشنهاد برای جذابیت بیشتر
"""


async def write_package(news: dict, feedback: str | None = None, previous: dict | None = None) -> dict:
    user = "خبر ورودی:\n" + json.dumps(news, ensure_ascii=False, indent=2)
    if previous and feedback:
        user += "\n\nنسخه‌ی قبلی:\n" + json.dumps(previous, ensure_ascii=False)
        user += f"\n\nبازخورد کاربر (اعمال کن):\n{feedback}"
    text, _ = await asyncio.to_thread(llm.generate, STYLE + OUTPUT_SPEC, user, False, 3000)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("Writer returned no JSON:\n" + text[:500])
    return json.loads(m.group(0))
