"""Telegram bot: scout -> pick -> script -> link -> narration approval -> video.

Nothing advances without an explicit button press / message from the user.
"""
import asyncio
import logging
import re
import shutil
import time
from pathlib import Path

from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup, Update
from telegram.request import HTTPXRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, PicklePersistence, filters)

import config
from agents.scout import find_news
from agents.writer import write_package
from services import download, stt, tts, video

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
    return (f"🎬 *هوک*\n{pkg['hook']}\n\n🎙 *نریشن*\n{pkg['narration']}\n\n📝 *کپشن*\n{pkg['caption']}\n"
            f"{' '.join(pkg.get('hashtags', []))}\n\n🎞 *سناریو*\n{scenes}\n\n*موارد نیازمند بررسی شما:*\n{flags}\n\n{tips}")


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
        j.update(stage=None, pkg=None, narration=None)
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
        reply_markup=Markup([[Btn("✅ تایید و ساخت ویدئو", callback_data="nar_ok"),
                              Btn("✏️ ویرایش متن", callback_data="nar_edit")]]))


def _run_build(j, work: Path) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    audio = tts.synthesize(j["narration"], work / "narration.mp3")
    words = stt.align_to_script(j["narration"], stt.transcribe_words(audio))
    ass = video.build_ass(words, work / "subs.ass")
    logo = Path(config.LOGO_PATH)
    return video.build_video([Path(x) for x in j["srcs"]], audio, ass, work / "final.mp4",
                             logo if logo.exists() else None)


async def build_and_send(update, ctx):
    j = job(ctx)
    chat = update.effective_chat.id
    work = config.WORK_DIR / f"{chat}_{int(time.time())}"
    await ctx.bot.send_message(chat, f"⚙️ ساخت نریشن ← Whisper ← میکس {len(j['srcs'])} ویدئو… (چند دقیقه)")
    try:
        out = await asyncio.to_thread(_run_build, j, work)
    except Exception as e:
        log.exception("build failed")
        await ctx.bot.send_message(chat, f"❌ ساخت ویدئو شکست خورد:\n{e}")
        return
    with out.open("rb") as f:
        await ctx.bot.send_video(chat, f, supports_streaming=True, width=1080, height=1920,
                                 caption=(j["pkg"]["caption"] + "\n" + " ".join(j["pkg"].get("hashtags", [])))[:1000])
    with out.open("rb") as f:
        await ctx.bot.send_document(chat, f, filename="reel.mp4", caption="📥 فایل برای دانلود/ذخیره")
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(_clips_dir(chat), ignore_errors=True)
    await next_in_queue(update, ctx)


# ---------- source clips ----------
def _clips_dir(chat) -> Path:
    return config.WORK_DIR / f"dl_{chat}"


def clips_keyboard(n):
    return Markup([[Btn(f"🎬 تمام، میکس کن ({n} ویدئو)", callback_data="clips_done")],
                   [Btn("🗑 پاک کردن ویدئوها", callback_data="clips_clear")]])


async def ask_for_clips(update, ctx):
    j = job(ctx)
    j["stage"], j["srcs"] = "link", []
    shutil.rmtree(_clips_dir(update.effective_chat.id), ignore_errors=True)
    await ctx.bot.send_message(
        update.effective_chat.id,
        "🔗 لینک ویدئوها را بفرستید (چند لینک در یک پیام یا چند پیام جدا) یا خود فایل‌ها را آپلود کنید "
        "(سقف هر فایل ~۲۰MB).\nربات همه را به‌صورت خودکار برش می‌زند و با هم میکس می‌کند.\n"
        "وقتی همه را فرستادید، دکمه‌ی «تمام، میکس کن» را بزنید.")


async def added_clip(update, ctx):
    n = len(job(ctx)["srcs"])
    await update.message.reply_text(f"✅ ویدئوی {n} اضافه شد. باز هم بفرستید یا میکس را شروع کنید.",
                                    reply_markup=clips_keyboard(n))


# ---------- callbacks ----------
STALE = "این دکمه مربوط به مرحله‌ی قبلی است (یا ربات ری‌استارت شده). لطفا از آخرین پیام استفاده کنید یا دوباره /news بزنید."


async def answer(q, text=None):
    """Answering a callback can fail (too old / network); that must not abort the handler."""
    try:
        await q.answer(text, show_alert=bool(text))
    except Exception as e:
        log.warning("callback answer failed: %s", e)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in config.ALLOWED_USER_IDS:
        await answer(q)
        return
    j, d = job(ctx), q.data
    log.info("button %s (stage=%s)", d, j.get("stage"))
    if j.get("busy"):
        await answer(q, "⏳ ربات مشغول کار قبلی است؛ چند لحظه صبر کنید.")
        return
    stale = ((d.startswith("sel:") or d == "go") and not j.get("items")
             or d.startswith("sel:") and int(d[4:]) >= len(j.get("items", []))
             or d in ("pkg_ok", "pkg_redo", "pkg_fb", "skip") and not j.get("pkg")
             or d in ("clips_done", "clips_clear") and j.get("stage") != "link"
             or d in ("nar_ok", "nar_edit") and not j.get("narration"))
    if stale:
        await answer(q, STALE)
        return
    if d == "go" and not j.get("selected"):
        await answer(q, "حداقل یک خبر انتخاب کنید")
        return
    if d == "clips_done" and not j.get("srcs"):
        await answer(q, "هنوز هیچ ویدئویی نفرستاده‌اید.")
        return
    await answer(q)
    # remove the buttons of steps that move on, so an old message cannot be pressed twice
    if d not in ("cancel",) and not d.startswith("sel:"):
        try:
            await q.edit_message_reply_markup(None)
        except Exception:
            pass
    j["busy"] = True
    try:
        if d == "cancel":
            ctx.user_data.pop("job", None)
            await q.edit_message_text("لغو شد.")
        elif d.startswith("sel:"):
            j["selected"] ^= {int(d[4:])}
            await q.edit_message_reply_markup(news_keyboard(j["items"], j["selected"]))
        elif d == "go":
            j["queue"] = [j["items"][i] for i in sorted(j["selected"])]
            j["items"] = []
            await q.message.reply_text(f"{len(j['queue'])} خبر انتخاب شد. یکی‌یکی جلو می‌رویم.")
            await next_in_queue(update, ctx)
        elif d == "pkg_redo":
            await write_and_show(update, ctx, "همان خبر را با زاویه و جمله‌بندی متفاوت بازنویسی کن.")
        elif d == "pkg_fb":
            j["stage"] = "feedback"
            await q.message.reply_text("توضیحتان را بنویسید:")
        elif d == "skip":
            await next_in_queue(update, ctx)
        elif d == "pkg_ok":
            await ask_for_clips(update, ctx)
        elif d == "clips_clear":
            await ask_for_clips(update, ctx)
        elif d == "clips_done":
            j["stage"] = None
            await ask_narration_approval(update, ctx)
        elif d == "nar_edit":
            j["stage"] = "narration_edit"
            await q.message.reply_text("متن جدید نریشن را بفرستید:")
        elif d == "nar_ok":
            await build_and_send(update, ctx)
    finally:
        j["busy"] = False


# ---------- messages ----------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    j, text = job(ctx), update.message.text
    stage = j.get("stage")
    if j.get("busy"):
        await update.message.reply_text("⏳ ربات مشغول کار قبلی است؛ چند لحظه صبر کنید.")
        return
    if stage == "feedback":
        j["stage"] = None
        j["busy"] = True
        try:
            await write_and_show(update, ctx, text)
        finally:
            j["busy"] = False
    elif stage == "link":
        urls = URL_RE.findall(text)
        if not urls:
            await update.message.reply_text("لینک معتبری پیدا نشد. لینک ویدئو یا خود فایل را بفرستید.")
            return
        j["busy"] = True
        try:
            for url in urls:
                await update.message.reply_text(f"⬇️ در حال دانلود…\n{url}")
                k = len(j["srcs"]) + 1
                try:
                    path = await asyncio.to_thread(download.download_video, url,
                                                   _clips_dir(update.effective_chat.id) / str(k))
                except Exception as e:
                    await update.message.reply_text(f"❌ این لینک دانلود نشد:\n{url}\n{str(e)[-400:]}\n\n"
                                                    "می‌توانید خود فایل را آپلود کنید.")
                    continue
                j["srcs"].append(str(path))
                await added_clip(update, ctx)
        finally:
            j["busy"] = False
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
    k = len(j["srcs"]) + 1
    d = _clips_dir(update.effective_chat.id) / str(k)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "source.mp4"
    try:
        f = await att.get_file()
        await f.download_to_drive(path)
    except Exception as e:
        await update.message.reply_text(f"❌ فایل دریافت نشد (شاید بزرگ‌تر از ۲۰MB است):\n{e}")
        return
    j["srcs"].append(str(path))
    await added_clip(update, ctx)


async def _reset_busy(app):
    # a crash/restart mid-job must not leave a user locked out
    for data in app.user_data.values():
        data.get("job", {}).pop("busy", None)


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("handler error", exc_info=ctx.error)


def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    if not config.ALLOWED_USER_IDS:
        raise SystemExit("ALLOWED_USER_IDS is empty: refusing to run an open bot")
    # persist user_data so the news list / pipeline state survive a restart
    persistence = PicklePersistence(config.WORK_DIR / "state.pickle", update_interval=5)
    app = (Application.builder().token(config.TELEGRAM_BOT_TOKEN).persistence(persistence)
           # long jobs (writing, mixing) must not block button presses from being answered
           .concurrent_updates(True)
           .request(HTTPXRequest(connect_timeout=20, read_timeout=60, write_timeout=120, media_write_timeout=300))
           .get_updates_request(HTTPXRequest(connect_timeout=20, read_timeout=60))
           .post_init(_reset_busy)
           .build())
    app.add_error_handler(on_error)
    allowed = filters.User(user_id=list(config.ALLOWED_USER_IDS))
    app.add_handler(CommandHandler("start", cmd_start, filters=allowed))
    app.add_handler(CommandHandler("news", cmd_news, filters=allowed))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=allowed))
    app.add_handler(MessageHandler(allowed & (filters.VIDEO | filters.Document.VIDEO), on_video))
    app.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
