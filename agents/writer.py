"""Writer: turns one approved news item into a full media package (cover options, angle, B-roll, narration, caption)."""
import asyncio
import json
import re
from pathlib import Path

import brands
from services import llm


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
- hashtags: لیست هشتگ‌ها (اولی {hashtag})
- flags: لیست جمله‌های تفسیری یا تأییدنشده که کاربر باید بررسی کند
- web_article: متن خبر برای سایت {outlet}، به سبک «هرم وارونه»:
  {"rootitr": "روتیتر کوتاه (زمینه یا گوینده)", "title": "تیتر خبری دقیق، حداکثر ۱۲ کلمه",
   "lead": "لید: ۱ یا ۲ جمله که مهم‌ترین واقعیت‌ها را می‌گوید (چه کسی، چه گفت/کرد، کجا، کی، چرا)",
   "body": ["پاراگراف‌ها به ترتیب اهمیتِ نزولی: اول جزئیات و نقل‌قول‌های اصلی، بعد واکنش‌ها و جزئیات تکمیلی، آخر زمینه و پیشینه"],
   "tags": ["۵ تا ۸ کلیدواژه برای سایت"]}
  قواعد متن سایت: فارسی رسمی و نوشتاری خبری (نه محاوره‌ای، نه شعاری)، بی‌طرف و دقیق؛ روتیتر هرگز «اختصاصی» یا «گزارش {outlet}» نباشد (خبر از منبع دیگری است)؛ هیچ تحلیل، پیش‌بینی، جمع‌بندی یا «تحلیلگران معتقدند» از خودت ننویس (فقط اگر عیناً در خبر ورودی آمده)؛ نام منبع فقط همان منبع خبر ورودی و کسانی که در خبر نقل شده‌اند؛ جمله‌ی اول لید با «به گزارش {outlet} به نقل از [منبع خبر]،» شروع شود؛ ۱۵۰ تا ۳۰۰ کلمه در ۳ تا ۵ پاراگراف، ولی اگر خبر ورودی کوتاه است متن هم کوتاه‌تر (برای طولانی کردن چیزی اضافه نکن)؛ بدون ایموجی و هشتگ؛ فقط واقعیت‌های «خبر ورودی»، نقل‌قول‌ها عیناً و با نام گوینده؛ ادعاها با «مدعی شد/اعلام کرد» نسبت داده شوند.
"""


MULTI_SOURCE = """
اگر «خبر ورودی» فیلد sources دارد، یعنی چند رسانه همین خبر را منتشر کرده‌اند و به ترتیب زمان انتشار مرتب شده‌اند (اولی، اول منتشر کرده). از متن همه‌ی آن‌ها استفاده کن و هر جا منبع را می‌آوری (references، لید و متن سایت، کپشن) آن‌ها را به همان ترتیب بیاور؛ لید سایت: «به گزارش {outlet} به نقل از [منبع اول]، …» و منابع بعدی در متن («… [منبع دوم] نیز گزارش داد …»).
"""


async def write_package(news: dict, feedback: str | None = None, previous: dict | None = None, brand=None) -> dict:
    brand = brands.get(getattr(brand, "key", brand))
    tone = (f"\n## لحن این رسانه (مهم‌تر از لحن نمونه)\nنریشن و هوک را با این لحن بنویس و narration_tone را هم همین بگذار: "
            f"{brand.tone}\n") if brand.tone else ""
    system = brand.text("writer_style") + tone + (OUTPUT_SPEC + MULTI_SOURCE).replace("{hashtag}", brand.hashtag).replace("{outlet}", brand.name)
    user = "خبر ورودی:\n" + json.dumps(news, ensure_ascii=False, indent=2)
    if previous and feedback:
        user += "\n\nنسخه‌ی قبلی:\n" + json.dumps(previous, ensure_ascii=False)
        user += f"\n\nبازخورد کاربر (اعمال کن):\n{feedback}"
    text, _ = await asyncio.to_thread(llm.generate, system, user, False, 7000)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("Writer returned no JSON:\n" + text[:500])
    return json.loads(m.group(0))
