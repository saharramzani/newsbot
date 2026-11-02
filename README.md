# newsbot — ربات تلگرام تولید ویدئوی خبری

جریان: `/news` ← تیترها ← انتخاب ← سناریو/نریشن/کپشن ← تایید ← لینک ویدئو ← تایید متن نریشن ← ElevenLabs ← Whisper ← ffmpeg ← ویدئوی ۹:۱۶ با زیرنویس و لوگو.

## اجرا
```bash
pip install -r requirements.txt     # ffmpeg هم باید نصب باشد
cp .env.example .env                # مقادیر را پر کنید (ALLOWED_USER_IDS اجباری است)
cp /path/logo.png assets/logo.png
# فونت فارسی (مثلا Vazirmatn-Bold.ttf) را در assets/fonts/ بگذارید
python bot.py
```

## وضعیت تست
- تست‌شده: ساخت زیرنویس ASS، هم‌ترازسازی متن با زمان‌بندی، ساخت ویدئو (9:16، لوگو، صدا) با ffmpeg.
- تست‌نشده (به شبکه/کلید نیاز دارند): Telegram، Claude web search، ElevenLabs، faster-whisper، yt-dlp.

## نکته‌ها
- Whisper صدا نمی‌سازد؛ فقط زمان‌بندی کلمات را از صدای ElevenLabs در می‌آورد.
- فقط ALLOWED_USER_IDS می‌توانند از ربات استفاده کنند.
- ربات‌ها فقط تا ~۲۰MB دانلود فایل از تلگرام دارند؛ ویدئوی بزرگ‌تر را با لینک بدهید.
