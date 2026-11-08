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

## اجرا روی سرور (همیشه روشن)
روی یک سرور Ubuntu به‌عنوان root:
```bash
git clone -b claude/trusting-bohr-rgtyr3 https://github.com/saharramzani/newsbot.git
cd newsbot && bash deploy/install.sh
nano /opt/newsbot/.env          # کلیدها را پر کنید
systemctl restart newsbot
```
- وضعیت: `systemctl status newsbot` — لاگ زنده: `journalctl -u newsbot -f`
- به‌روزرسانی: `cd ~/newsbot && git pull && bash deploy/install.sh && systemctl restart newsbot`
- ربات بعد از خطا یا ری‌استارت سرور خودکار دوباره روشن می‌شود.
- هم‌زمان فقط یک نسخه با یک توکن اجرا کنید (وگرنه خطای Conflict).
