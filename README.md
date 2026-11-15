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

## دانلود از اینستاگرام (کوکی)
اینستاگرام (و گاهی یوتیوب روی سرور) بدون لاگین اجازه‌ی دانلود نمی‌دهد. ربات می‌تواند از کوکی‌های یک حساب لاگین‌شده استفاده کند:
1. ترجیحاً با یک **حساب دوم** (نه حساب اصلی) در Chrome یا Edge وارد instagram.com شوید.
2. افزونه‌ی **Get cookies.txt LOCALLY** را نصب کنید، در صفحه‌ی instagram.com رویش بزنید و **Export** کنید (فایلی مثل `instagram.com_cookies.txt` ذخیره می‌شود).
3. فایل را به سرور بفرستید (روی لپ‌تاپ، در PowerShell):
   `scp "$HOME\Downloads\instagram.com_cookies.txt" root@IP:/opt/newsbot/cookies.txt`
4. روی سرور: `chown newsbot:newsbot /opt/newsbot/cookies.txt && chmod 600 /opt/newsbot/cookies.txt`
- ری‌استارت لازم نیست. در آن مرورگر از اینستاگرام **Log out نکنید** (کوکی‌ها باطل می‌شوند).
- اگر بعد از مدتی دوباره خطای لاگین آمد، کوکی‌ها منقضی شده‌اند: مراحل ۲ تا ۴ را تکرار کنید.
- فایل کوکی مثل رمز عبور است؛ به کسی ندهید. (`cookies.txt` در git ثبت نمی‌شود.)
