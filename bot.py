"""Telegram bot: scout -> pick -> script -> link -> narration approval -> video.

Nothing advances without an explicit button press / message from the user.
"""
import asyncio
import logging
import re
import shutil
import time
from pathlib import Path

from telegram.error import BadRequest
from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, PicklePersistence, filters)

import config
from agents.scout import find_news
from agents.writer import write_package
from services import cover, download, stt, tts, video

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
# httpx logs full URLs at INFO level, and Telegram URLs contain the bot token
for noisy in ("httpx", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("newsbot")
URL_RE = re.compile(r"https?://\S+")


def job(ctx) -> dict:
    return ctx.user_data.setdefault("job", {})


# ---------- helpers ----------
def news_keyboard(items, selected):
    rows = [[Btn(("✅ " if i in selected else "⬜ ") + f"{i + 1}. {it['title_fa'][:48]}", callback_data=f"sel:{i}")]
            for i, it in enumerate(items)]
    rows.append([Btn(f"▶️ تایید انتخاب ({len(selected)})", callback_data="go"), Btn("❌ لغو", callback_data="cancel")])
    return Markup(rows)


def format_news(items) -> str:
    out = ["📰 *تیترهای ۱۲ ساعت اخیر* (برای انتخاب روی دکمه بزنید)\n"]
    for i, it in enumerate(items, 1):
        out.append(f"{i}. {it['title_fa']}\n   منبع: {it['source']} | زمان: {it.get('hours_ago_estimate', '?')} "
                   f"(اطمینان زمانی: {it.get('time_confidence', '?')}) | ویدئو: {it.get('has_video', '?')}\n"
                   f"   {it['summary_fa']}\n   {it['url']}\n")
    return "\n".join(out)


def format_package(pkg: dict) -> str:
    scenes = "\n".join(f"• {s.get('time', '')} | {s.get('visual', '')} | «{s.get('text_on_screen', '')}»"
                       for s in pkg.get("scenario", []))
    flags = "\n".join(f"⚠️ {f}" for f in pkg.get("flags", [])) or "—"
    tips = "\n".join(f"💡 {t}" for t in pkg.get("tips", [])) or "—"
    return (f"🎬 *هوک*\n{pkg['hook']}\n\n🖼 *تیتر کاور*\n{cover_title(pkg)}\n\n🎙 *نریشن*\n{pkg['narration']}\n\n📝 *کپشن*\n{pkg['caption']}\n"
            f"{' '.join(pkg.get('hashtags', []))}\n\n🎞 *سناریو*\n{scenes}\n\n*موارد نیازمند بررسی شما:*\n{flags}\n\n{tips}")


def cover_title(pkg: dict) -> str:
    return (pkg.get("cover_title") or pkg.get("hook", "").split("\n")[0]).strip()


async def say(update_or_q, ctx, text, markup=None):
    chat_id = update_or_q.effective_chat.id
    # Telegram limit is 4096 chars; split plain
    for i in range(0, len(text), 4000):
        last = i + 4000 >= len(text)
        await ctx.bot.send_message(chat_id, text[i:i + 4000], reply_markup=markup if last else None)


# ---------- commands ----------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("سلام 👋\n/news ← جست‌وجوی تیترهای ۱۲ ساعت اخیر\n/cancel ← لغو کار جاری")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("job", None)
    await update.message.reply_text("کار جاری لغو شد.")


async def cmd_news(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["job"] = {}
    await update.message.reply_text("🔎 در حال جست‌وجوی اخبار… (۱ تا ۳ دقیقه)")
    try:
        items, sources = await find_news()
    except Exception as e:
        log.exception("scout failed")
        await update.message.reply_text(f"❌ جست‌وجو شکست خورد:\n{e}")
        return
    j = job(ctx)
    j.update(items=items, selected=set())
    await say(update, ctx, format_news(items))
    if sources:
        await say(update, ctx, "🔍 منابعی که جست‌وجو واقعاً به آن‌ها رسید (برای راستی‌آزمایی):\n"
                  + "\n".join(f"• {s['title']} {s['uri']}" for s in sources[:12]))
    await ctx.bot.send_message(update.effective_chat.id, "کدام خبرها؟", reply_markup=news_keyboard(items, j["selected"]))


# ---------- pipeline steps ----------
async def next_in_queue(update, ctx):
    j = job(ctx)
    if not j.get("queue"):
        await ctx.bot.send_message(update.effective_chat.id, "✅ همه‌ی خبرهای انتخاب‌شده تمام شد. برای دور جدید /news")
        return
    j["current"] = j["queue"].pop(0)
    j.update(stage=None, pkg=None, src=None, narration=None)
    await write_and_show(update, ctx)


async def write_and_show(update, ctx, feedback=None):
    j = job(ctx)
    await ctx.bot.send_message(update.effective_chat.id, f"✍️ در حال نوشتن سناریو برای:\n{j['current']['title_fa']}")
    try:
        j["pkg"] = await write_package(j["current"], feedback, j.get("pkg"))
    except Exception as e:
        log.exception("writer failed")
        await ctx.bot.send_message(update.effective_chat.id, f"❌ نوشتن شکست خورد:\n{e}")
        return
    await say(update, ctx, format_package(j["pkg"]), Markup([[
        Btn("✅ تایید", callback_data="pkg_ok"), Btn("🔁 بازنویسی", callback_data="pkg_redo"),
        Btn("✏️ با توضیح من", callback_data="pkg_fb"), Btn("⏭ رد کردن", callback_data="skip")]]))


async def ask_narration_approval(update, ctx):
    j = job(ctx)
    j["narration"] = j["pkg"]["narration"]
    await ctx.bot.send_message(
        update.effective_chat.id,
        "🎙 متن نهایی نریشن (این متن به ElevenLabs داده می‌شود):\n\n" + j["narration"],
        reply_markup=Markup([[Btn("✅ ساخت " + f["label"], callback_data=f"nar_ok:{k}") for k, f in video.FORMATS.items()],
                             [Btn("✏️ ویرایش متن", callback_data="nar_edit")]]))


def _run_build(j, work: Path) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    audio = tts.synthesize(j["narration"], work / "narration.mp3")
    words = stt.align_to_script(j["narration"], stt.transcribe_words(audio))
    fmt = j.get("fmt", "9x16")
    ass = video.build_ass(words, work / "subs.ass", fmt=fmt)
    logo = Path(config.LOGO_PATH)
    return video.build_video(Path(j["src"]), audio, ass, work / "final.mp4", logo if logo.exists() else None, fmt=fmt)


async def build_and_send(update, ctx):
    j = job(ctx)
    chat = update.effective_chat.id
    work = config.WORK_DIR / f"{chat}_{int(time.time())}"
    await ctx.bot.send_message(chat, "⚙️ ساخت نریشن ← Whisper ← تدوین… (چند دقیقه)")
    try:
        out = await asyncio.to_thread(_run_build, j, work)
    except Exception as e:
        log.exception("build failed")
        await ctx.bot.send_message(chat, f"❌ ساخت ویدئو شکست خورد:\n{e}")
        return
    with out.open("rb") as f:
        size = video.FORMATS[j.get("fmt", "9x16")]
        await ctx.bot.send_video(chat, f, supports_streaming=True, width=size["w"], height=size["h"],
                                 caption=(j["pkg"]["caption"] + "\n" + " ".join(j["pkg"].get("hashtags", [])))[:1000])
    await ctx.bot.send_document(chat, out.open("rb"), filename="reel.mp4", caption="📥 فایل برای دانلود/ذخیره")
    shutil.rmtree(work, ignore_errors=True)
    await make_cover(update, ctx)
    await next_in_queue(update, ctx)


# ---------- cover ----------
COVER_DIR = config.WORK_DIR / "covers"


async def make_cover(update, ctx):
    """Build the cover for the video just sent. Failure here never blocks the next item."""
    j, chat = job(ctx), update.effective_chat.id
    fmt = j.get("fmt", "9x16")
    key = str(int(time.time() * 1000))
    COVER_DIR.mkdir(parents=True, exist_ok=True)
    bg = COVER_DIR / f"{chat}_{key}_bg.png"
    try:
        await asyncio.to_thread(cover.extract_frame, Path(j["src"]), bg, fmt)
    except Exception as e:
        log.exception("cover frame failed")
        await ctx.bot.send_message(chat, f"⚠️ ساخت کاور شکست خورد (ویدئو سالم است):\n{e}")
        return
    covers = j.setdefault("covers", {})
    covers[key] = {"bg": str(bg), "fmt": fmt, "title": cover_title(j["pkg"])}
    for old in sorted(covers)[:-10]:  # keep the last 10 covers editable
        Path(covers.pop(old)["bg"]).unlink(missing_ok=True)
    await send_cover(update, ctx, key)


async def send_cover(update, ctx, key):
    j, chat = job(ctx), update.effective_chat.id
    c = j["covers"][key]
    out = COVER_DIR / f"{chat}_{key}.jpg"
    try:
        await asyncio.to_thread(cover.compose, Path(c["bg"]), c["title"], out, c["fmt"])
    except Exception as e:
        log.exception("cover compose failed")
        await ctx.bot.send_message(chat, f"⚠️ ساخت کاور شکست خورد (ویدئو سالم است):\n{e}")
        return
    with out.open("rb") as f:
        await ctx.bot.send_photo(chat, f, caption=f"🖼 کاور: {c['title']}",
                                 reply_markup=Markup([[Btn("✏️ تغییر متن کاور", callback_data=f"cov:{key}")]]))
    with out.open("rb") as f:
        await ctx.bot.send_document(chat, f, filename="cover.jpg", caption="📥 کاور با کیفیت کامل")
    out.unlink(missing_ok=True)
    out.with_suffix(".ass").unlink(missing_ok=True)


# ---------- callbacks ----------
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        await q.answer()
    except BadRequest:  # button pressed while the bot was offline: Telegram no longer accepts the answer
        pass
    if q.from_user.id not in config.ALLOWED_USER_IDS:
        return
    j, d = job(ctx), q.data
    # state is lost if the bot restarted, or the button belongs to an older round
    needs = {"go": "items", "pkg_redo": "current", "pkg_fb": "current", "pkg_ok": "pkg", "skip": "queue",
             "nar_edit": "pkg", "nar_ok": "src"}
    key = d.split(":")[0] if d.startswith("nar_ok") else d
    if (d.startswith("sel:") and not j.get("items")) or (key in needs and needs[key] not in j):
        await q.message.reply_text("⏳ این دکمه منقضی شده (ربات ری‌استارت شده یا دور جدید شروع شده). دوباره /news بزنید.")
        return
    if d.startswith("cov:"):
        key = d[4:]
        if key not in j.get("covers", {}):
            await q.message.reply_text("⏳ این کاور دیگر قابل ویرایش نیست.")
            return
        j["cover_edit"], j["stage_before_cover"] = key, j.get("stage")
        j["stage"] = "cover_text"
        await q.message.reply_text(f"متن جدید کاور را بفرستید:\n(متن فعلی: {j['covers'][key]['title']})")
        return
    if d == "cancel":
        ctx.user_data.pop("job", None)
        await q.edit_message_text("لغو شد.")
    elif d.startswith("sel:") and j.get("items"):
        i = int(d[4:])
        j["selected"] ^= {i}
        await q.edit_message_reply_markup(news_keyboard(j["items"], j["selected"]))
    elif d == "go":
        if not j.get("selected"):
            await q.answer("حداقل یک خبر انتخاب کنید", show_alert=True)
            return
        j["queue"] = [j["items"][i] for i in sorted(j["selected"])]
        await q.edit_message_text(f"{len(j['queue'])} خبر انتخاب شد. یکی‌یکی جلو می‌رویم.")
        await next_in_queue(update, ctx)
    elif d == "pkg_redo":
        await write_and_show(update, ctx, "همان خبر را با زاویه و جمله‌بندی متفاوت بازنویسی کن.")
    elif d == "pkg_fb":
        j["stage"] = "feedback"
        await q.message.reply_text("توضیحتان را بنویسید:")
    elif d == "skip":
        await next_in_queue(update, ctx)
    elif d == "pkg_ok":
        j["stage"] = "link"
        await q.message.reply_text("🔗 لینک ویدئوی منبع را بفرستید (یا خود فایل ویدئو را آپلود کنید؛ سقف دانلود ربات ~۲۰MB).")
    elif d == "nar_edit":
        j["stage"] = "narration_edit"
        await q.message.reply_text("متن جدید نریشن را بفرستید:")
    elif d.startswith("nar_ok"):
        fmt = d.split(":", 1)[1] if ":" in d else "9x16"  # old buttons had no format
        j["fmt"] = fmt if fmt in video.FORMATS else "9x16"
        await q.edit_message_reply_markup(None)  # no double build from a second press
        await build_and_send(update, ctx)


# ---------- messages ----------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    j, text = job(ctx), update.message.text
    stage = j.get("stage")
    if stage == "downloading":
        await update.message.reply_text("⏳ هنوز در حال دانلود ویدئوی قبلی هستم؛ صبر کنید.")
        return
    if stage == "cover_text":
        key = j.pop("cover_edit", None)
        j["stage"] = j.pop("stage_before_cover", None)  # go back to whatever step the user was on
        if key in j.get("covers", {}):
            j["covers"][key]["title"] = text.strip()
            await update.message.reply_text("🖼 در حال ساخت کاور جدید…")
            await send_cover(update, ctx, key)
        return
    if stage == "feedback":
        j["stage"] = None
        await write_and_show(update, ctx, text)
    elif stage == "link":
        m = URL_RE.search(text)
        if not m:
            await update.message.reply_text("لینک معتبر نیست.")
            return
        j["stage"] = "downloading"
        await update.message.reply_text("⬇️ در حال دانلود… (تا تمام نشود مرحله‌ی بعد شروع نمی‌شود)")
        try:
            src = await asyncio.to_thread(download.download_video, m.group(0), config.WORK_DIR / f"dl_{update.effective_chat.id}")
        except Exception as e:
            log.exception("download failed")
            j["stage"] = "link"
            await update.message.reply_text(f"❌ دانلود نشد:\n{e}\n\nلینک دیگری بفرستید یا خود فایل را آپلود کنید.")
            return
        j["src"], j["stage"] = str(src), None
        await report_source(update, src)
        await ask_narration_approval(update, ctx)
    elif stage == "narration_edit":
        j["pkg"]["narration"] = text
        j["stage"] = None
        await ask_narration_approval(update, ctx)
    else:
        await update.message.reply_text("برای شروع /news")


async def on_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    j = job(ctx)
    if j.get("stage") != "link":
        return
    att = update.message.video or update.message.document
    j["stage"] = "downloading"
    await update.message.reply_text("⬇️ در حال دریافت فایل…")
    try:
        path = download.fresh_dir(config.WORK_DIR / f"dl_{update.effective_chat.id}") / "source.mp4"
        f = await att.get_file()
        await f.download_to_drive(path)
        await asyncio.to_thread(download.check_video, path)
    except Exception as e:
        log.exception("upload failed")
        j["stage"] = "link"
        await update.message.reply_text(f"❌ فایل دریافت نشد:\n{e}\n\n(سقف فایل برای ربات ~۲۰MB است؛ فایل بزرگ‌تر را با لینک بفرستید.)")
        return
    j["src"], j["stage"] = str(path), None
    await report_source(update, path)
    await ask_narration_approval(update, ctx)


async def report_source(update, path: Path):
    secs = await asyncio.to_thread(video.duration, path)
    mb = path.stat().st_size / 1_000_000
    await update.message.reply_text(f"✅ ویدئو کامل دریافت شد: {mb:.1f}MB، {secs:.0f} ثانیه")


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("update failed", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await ctx.bot.send_message(update.effective_chat.id, f"❌ خطای غیرمنتظره:\n{ctx.error}\n\nدوباره امتحان کنید یا /news")
        except Exception:
            pass


def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    if not config.ALLOWED_USER_IDS:
        raise SystemExit("ALLOWED_USER_IDS is empty: refusing to run an open bot")
    # concurrent_updates: one user's long step (search / video build) must not block other users' buttons.
    # persistence: per-user progress survives a restart.
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        log.warning("ffmpeg/ffprobe NOT found: downloading and building videos will fail. "
                    "Install it (winget install Gyan.FFmpeg --source winget) or set FFMPEG_DIR in .env")
    else:
        log.info("ffmpeg found: %s", shutil.which("ffmpeg"))
    app = (Application.builder().token(config.TELEGRAM_BOT_TOKEN).concurrent_updates(True)
           .persistence(PicklePersistence(filepath=config.WORK_DIR / "state.pickle")).build())
    allowed = filters.User(user_id=list(config.ALLOWED_USER_IDS))
    app.add_handler(CommandHandler("start", cmd_start, filters=allowed))
    app.add_handler(CommandHandler("news", cmd_news, filters=allowed))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=allowed))
    app.add_handler(MessageHandler(allowed & (filters.VIDEO | filters.Document.VIDEO), on_video))
    app.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)
    app.run_polling()


if __name__ == "__main__":
    main()
