"""Telegram bot: scout -> pick -> script -> link -> narration approval -> video.

Nothing advances without an explicit button press / message from the user.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

from telegram.error import BadRequest
from telegram import BotCommand, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup, Update
from telegram.ext import (AIORateLimiter, Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, PicklePersistence, filters)

import config
from agents import bulletin
from agents.scout import find_news
from agents.writer import write_package
from services import cover, docx_out, download, editor, stt, tts, video
from services.sources_fa import source_fa
import brands

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
# httpx logs full URLs at INFO level, and Telegram URLs contain the bot token
for noisy in ("httpx", "httpcore", "httpx2", "httpcore2"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("newsbot")
URL_RE = re.compile(r"https?://\S+")


TEHRAN = timezone(timedelta(hours=3, minutes=30))


def job(ctx) -> dict:
    return ctx.user_data.setdefault("job", {})


def brand_of(ctx) -> brands.Brand:
    """The outlet of the current job (falls back to the user's last choice, then the default outlet)."""
    return brands.get(ctx.user_data.get("job", {}).get("brand") or ctx.user_data.get("brand"))


def brand_keyboard(action: str) -> Markup:
    return Markup([[Btn(b.label, callback_data=f"{action}:{b.key}")] for b in brands.available()])


# ---------- helpers ----------
PAGE = 10  # news buttons per keyboard page


def news_keyboard(items, selected, page=0):
    pages = max(1, -(-len(items) // PAGE))
    page = min(max(page, 0), pages - 1)
    start = page * PAGE
    rows = [[Btn(("✅ " if i in selected else "⬜ ") + mark(items[i])
                 + f"{i + 1}. {items[i]['title_fa'][:44]}", callback_data=f"sel:{i}")]
            for i in range(start, min(start + PAGE, len(items)))]
    if pages > 1:
        rows.append([Btn("◀️ قبلی", callback_data=f"pg:{page - 1}") if page > 0 else Btn(" ", callback_data="noop"),
                     Btn(f"صفحه {page + 1} از {pages}", callback_data="noop"),
                     Btn("بعدی ▶️", callback_data=f"pg:{page + 1}") if page < pages - 1 else Btn(" ", callback_data="noop")])
    rows.append([Btn(f"▶️ تایید انتخاب ({len(selected)})", callback_data="go"), Btn("❌ لغو", callback_data="cancel")])
    return Markup(rows)


def mark(item) -> str:
    imp = item.get("importance", 0)
    return "🔥" if imp >= 5 else "⭐" if imp == 4 else ""


def _tehran_hm(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(TEHRAN).strftime("%H:%M")
    except Exception:
        return "?"


def item_sources(it: dict) -> list[dict]:
    """The outlets that reported this story, first publisher first (old items: just their own source)."""
    return it.get("sources") or [{"source": it.get("source", ""), "url": it.get("url", ""),
                                  "published_iso": it.get("published_iso", "")}]


def sources_block(it: dict) -> str:
    srcs, seen = [], set()
    for x in item_sources(it):  # each outlet once, at its first publication
        if source_fa(x) not in seen:
            seen.add(source_fa(x))
            srcs.append(x)
    if len(srcs) == 1:
        s0 = srcs[0]
        return f"   {source_fa(s0)} | {it.get('hours_ago_estimate', '?')}\n   {s0['url']}"
    chain = " ← ".join(f"{source_fa(x)} {_tehran_hm(x.get('published_iso', ''))}" for x in srcs)
    links = "\n".join(f"   🔗 {source_fa(x)}: {x['url']}" for x in srcs)
    return f"   منابع به ترتیب انتشار ({len(srcs)}): {chain}\n{links}"


def format_news(items, all_view: bool = False, brand=None) -> str:
    head = (f"📋 همه‌ی {len(items)} خبر (🔥 = خیلی مهم، ⭐ = مهم)" if all_view
            else f"📰 {len(items)} خبر مهم (مهم‌ترین اول؛ 🔥 = خیلی مهم)")
    if brand and len(brands.available()) > 1:
        head = f"{brand.label} | " + head
    out = [head + "\n"]
    for i, it in enumerate(items, 1):
        m = mark(it)
        out.append(f"{i}. {m + ' ' if m else ''}{it['title_fa']}\n{sources_block(it)}\n")
    return "\n".join(out)


async def show_news(update, ctx, all_view: bool = False):
    j = job(ctx)
    items = j["all_items"] if all_view else [i for i in j["all_items"] if i.get("relevant")]
    j.update(items=items, selected=set(), page=0)
    chat = update.effective_chat.id
    if not items:
        await ctx.bot.send_message(chat, "در این بازه خبر مهمی پیدا نشد.", reply_markup=Markup(
            [[Btn(f"📋 نمایش همه ({len(j['all_items'])} خبر)", callback_data="news_all")]]))
        return
    await say(update, ctx, format_news(items, all_view, brand_of(ctx)))
    kb = news_keyboard(items, j["selected"])
    if not all_view and len(j["all_items"]) > len(items):
        kb = Markup(list(kb.inline_keyboard) + [[Btn(f"📋 نمایش همه ({len(j['all_items'])} خبر)", callback_data="news_all")]])
    await ctx.bot.send_message(chat, "کدام خبرها؟", reply_markup=kb)


def final_caption(pkg: dict, item: dict, limit: int = 2200, brand=None) -> str:
    """Caption + mandatory source line + hashtags. The caption body is shortened if needed, never the source."""
    names = []
    for x in item_sources(item):
        n = source_fa(x)
        if n and n not in names:
            names.append(n)
    source_line = (f"📌 منبع: {names[0]}" if len(names) == 1 else f"📌 منابع: {'، '.join(names)}") if names else ""
    tags = [t if t.startswith("#") else "#" + t for t in (str(x).strip().replace(" ", "_") for x in pkg.get("hashtags", [])) if t]
    brand_tag = brands.get(getattr(brand, "key", brand)).hashtag
    tags = " ".join(([brand_tag] if brand_tag else []) + [t for t in tags if t != brand_tag])
    tail = "\n\n".join(x for x in (source_line, tags) if x)
    body = pkg.get("caption", "").strip()
    room = limit - len(tail) - 2
    if len(body) > room:
        body = body[:max(0, room - 1)].rstrip() + "…"
    return f"{body}\n\n{tail}" if body else tail


def format_package(pkg: dict, item: dict | None = None, brand=None) -> str:
    if "cover_options" not in pkg and "scenario" in pkg:  # package written before the new format
        return _format_package_old(pkg, item, brand)
    opts = "\n".join(f"• گزینه {k}: {o.get('title', '')}" for k, o in enumerate(pkg.get("cover_options", []), 1))
    broll = pkg.get("broll", {})
    def kw(key):
        return "\n".join(f"• {x}" for x in broll.get(key, [])) or "—"
    words = len(pkg.get("narration", "").split())
    flags = "\n".join(f"⚠️ {f}" for f in pkg.get("flags", [])) or "—"
    return (f"🖼 تیترهای جذاب و ضربتی کاور\n{opts}\n\n"
            f"📌 موضوع: {pkg.get('topic', '')}\n\n"
            f"🔗 منبع و رفرنس: {pkg.get('references', '')}\n\n"
            f"🎯 زاویه دید رسانه‌ای: {pkg.get('angle', '')}\n\n"
            f"🪝 قلاب جذابیت (Hook): {pkg.get('hook', '')}\n\n"
            f"🎞 کلیدواژه‌های تصویر (B-Roll)\n"
            f"۱. فیلم‌های خبری اصل رویداد (Raw Footage)\n{kw('raw')}\n"
            f"۲. فوتیج فضاساز میدانی (Location / Context)\n{kw('location')}\n"
            f"۳. گرافیک و انیمیشن تحلیلی (3D / Maps / Explainer)\n{kw('graphics')}\n\n"
            f"🎙 متن نریتور (Voice-Over)\nلحن: {pkg.get('narration_tone', '')}\n"
            f"تعداد کلمات: {words} (حدود {words / 2.6:.0f} ثانیه)\n\n{pkg.get('narration', '')}\n\n"
            f"📝 کپشن کامل اینستاگرام\n{final_caption(pkg, item or {}, brand=brand)}\n\n"
            f"موارد نیازمند بررسی شما:\n{flags}")


def format_web(pkg: dict) -> str:
    """The website news text (inverted pyramid), ready to copy into the site."""
    w = pkg.get("web_article")
    if not isinstance(w, dict) or not (w.get("title") or w.get("lead")):
        return ""
    body = w.get("body") or []
    body = body if isinstance(body, list) else [str(body)]
    tags = "، ".join(str(t).lstrip("#") for t in (w.get("tags") or []))
    parts = [f"روتیتر: {w.get('rootitr', '')}".strip(), f"تیتر: {w.get('title', '')}", "",
             f"لید: {w.get('lead', '')}", ""] + [x for b in body if str(b).strip() for x in (str(b).strip(), "")]
    if tags:
        parts += ["", f"کلیدواژه‌ها: {tags}"]
    return "\n".join(parts).replace("\n\n\n", "\n\n").strip()


def _format_package_old(pkg: dict, item: dict | None = None, brand=None) -> str:
    scenes = "\n".join(f"• {s.get('time', '')} | {s.get('visual', '')} | «{s.get('text_on_screen', '')}»"
                       for s in pkg.get("scenario", []))
    flags = "\n".join(f"⚠️ {f}" for f in pkg.get("flags", [])) or "—"
    return (f"🎬 هوک\n{pkg.get('hook', '')}\n\n🖼 تیتر کاور\n{cover_title(pkg)}\n\n🎙 نریشن\n{pkg.get('narration', '')}"
            f"\n\n📝 کپشن\n{final_caption(pkg, item or {}, brand=brand)}\n\n🎞 سناریو\n{scenes}\n\nموارد نیازمند بررسی شما:\n{flags}")


def cover_options(pkg: dict) -> list[str]:
    """Cover headline options; each one newline-separated, one line per banner."""
    out = []
    for o in pkg.get("cover_options", []):
        if isinstance(o, dict):
            lines = [str(l).strip() for l in o.get("lines", []) if str(l).strip()]
            out.append("\n".join(lines) if lines else str(o.get("title", "")).strip())
        elif str(o).strip():
            out.append(str(o).strip())
    return [o for o in out if o]


def cover_title(pkg: dict) -> str:
    """Cover headline; one line per banner (newline-separated)."""
    if cover_options(pkg):
        return cover_options(pkg)[0]
    lines = pkg.get("cover_lines")
    if isinstance(lines, list) and lines:
        return "\n".join(str(l).strip() for l in lines if str(l).strip())
    return (pkg.get("cover_title") or pkg.get("hook", "").split("\n")[0]).strip()


async def say(update_or_q, ctx, text, markup=None):
    chat_id = update_or_q.effective_chat.id
    # Telegram limit is 4096 chars: split on line breaks so an item is never cut in half
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > 4000:
            parts.append(line[:4000])
            line = line[4000:]
        if len(cur) + len(line) + 1 > 4000:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur)
    for k, part in enumerate(parts):
        await ctx.bot.send_message(chat_id, part, reply_markup=markup if k == len(parts) - 1 else None,
                                   disable_web_page_preview=True)


# ---------- commands ----------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    own = brands.available()
    hello = f"سلام 👋 این‌جا ربات {own[0].label} است." if len(own) == 1 else "سلام 👋"
    await update.message.reply_text(hello + "\n/news ← جست‌وجوی خبرهای مهم\n/list ← برگشت به همان لیست اخبار (بدون جست‌وجوی دوباره)\n"
                                    "/bulletin ← ساخت بولتن خبری (فایل Word)\n/cancel ← لغو کار جاری")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("job", None)
    await update.message.reply_text("کار جاری لغو شد. برای دیدن دوباره‌ی همان لیست اخبار /list")


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Back to the last news list: drops the current step, no new search."""
    last = ctx.user_data.get("last_news")
    if not last or not last.get("items"):
        await update.message.reply_text("هنوز لیستی نگرفته‌اید؛ اول /news بزنید.")
        return
    ctx.user_data["job"] = {"all_items": last["items"], "brand": last.get("brand") or brands.DEFAULT}
    mins = int((time.time() - last["at"]) // 60)
    age = f"{mins} دقیقه پیش" if mins < 120 else f"{mins // 60} ساعت پیش"
    await update.message.reply_text(f"🔙 برگشت به لیست اخبار (جست‌وجوی {age}؛ کار نیمه‌تمام کنار گذاشته شد). "
                                    "برای خبرهای تازه /news")
    await show_news(update, ctx)


async def cmd_news(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(brands.available()) > 1:
        await update.message.reply_text("برای کدام رسانه؟", reply_markup=brand_keyboard("news_brand"))
        return
    await run_news(update, ctx, brands.DEFAULT)


async def run_news(update, ctx, brand_key: str):
    chat = update.effective_chat.id
    brand = brands.get(brand_key)
    ctx.user_data["brand"] = brand.key  # remembered as this user's last outlet
    ctx.user_data["job"] = {"brand": brand.key}
    await ctx.bot.send_message(chat, f"🔎 {brand.label}: در حال جمع‌آوری اخبار و انتخاب خبرهای مهم… (۱ تا ۳ دقیقه)")
    try:
        items, sources = await find_news(brand=brand)
    except Exception as e:
        log.exception("scout failed")
        await ctx.bot.send_message(chat, f"❌ جست‌وجو شکست خورد:\n{e}")
        return
    j = job(ctx)
    j["all_items"] = items
    # kept outside the job, so /cancel or a new step does not lose it; /list shows it again without searching
    ctx.user_data["last_news"] = {"items": items, "at": time.time(), "brand": brand.key}
    if sources:
        await say(update, ctx, "\n".join(f"ℹ️ {s['title']} {s['uri']}".strip() for s in sources[:12]))
    await show_news(update, ctx)


# ---------- daily bulletin ----------


async def cmd_bulletin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(brands.available()) > 1:
        await update.message.reply_text("بولتن کدام رسانه؟", reply_markup=brand_keyboard("bul_brand"))
        return
    await run_bulletin(update, ctx, brands.DEFAULT)


def bulletin_window():
    """(start, end) of the bulletin: 08:00 yesterday to 08:00 today, Tehran time (before 08:00: the day before);
    or (None, None) for the last BULLETIN_HOURS hours."""
    if config.BULLETIN_WINDOW != "8to8":
        return None, None
    now = datetime.now(TEHRAN)
    end = now.replace(hour=config.BULLETIN_HOUR, minute=0, second=0, microsecond=0)
    if now < end:
        end -= timedelta(days=1)
    return end - timedelta(days=1), end


async def bulletin_home(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """The bulletin bot's only screen."""
    b = brands.get(None)
    start, end = bulletin_window()
    span = (f"خبرهای ساعت {config.BULLETIN_HOUR} صبح {docx_out.persian_date(start)} تا {config.BULLETIN_HOUR} صبح "
            f"{docx_out.persian_date(end)}" if start else f"خبرهای {config.BULLETIN_HOURS} ساعت اخیر")
    await update.effective_message.reply_text(
        f"🗞 ربات بولتن {b.name}\n{span}، با بخش‌های بولتن"
        + (" و تحلیل زیر هر خبر" if config.BULLETIN_ANALYSIS else "") + "، به‌صورت فایل Word.",
        reply_markup=Markup([[Btn("🗞 ساخت بولتن", callback_data="bul_go")]]))


async def bulletin_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await run_bulletin(update, ctx, brands.DEFAULT)


async def run_bulletin(update, ctx, brand_key: str):
    chat = update.effective_chat.id
    brand = brands.get(brand_key)

    async def reply(text):
        await ctx.bot.send_message(chat, text)
    if ctx.chat_data.get("bulletin_running"):
        await reply("⏳ یک بولتن در حال ساخت است؛ صبر کنید.")
        return
    ctx.chat_data["bulletin_running"] = True
    try:
        await reply(f"🗞 {brand.label}: در حال جمع‌آوری خبرهای {config.BULLETIN_HOURS} ساعت اخیر و نوشتن بولتن… "
                    "(چند دقیقه)")
        try:
            start, end = bulletin_window()
            b = await bulletin.build(brand=brand, start=start, end=end, analysis=config.BULLETIN_ANALYSIS)
        except Exception as e:
            log.exception("bulletin failed")
            await reply(f"❌ ساخت بولتن شکست خورد:\n{e}")
            return
        if not b["sections"]:
            await reply("هیچ خبری برای بخش‌های بولتن پیدا نشد.")
            return
        when = end if config.BULLETIN_WINDOW == "8to8" else datetime.now(TEHRAN)
        period = (f"اخبار از ساعت {config.BULLETIN_HOUR} صبح {docx_out.persian_date(start)} تا ساعت "
                  f"{config.BULLETIN_HOUR} صبح {docx_out.persian_date(end)}" if config.BULLETIN_WINDOW == "8to8" else "")
        folder = config.WORK_DIR / "bulletins"
        folder.mkdir(parents=True, exist_ok=True)
        jy, jm, jd = docx_out.to_jalali(when.year, when.month, when.day)
        stem = f"bulletin_{brand.key}_{jy}-{jm:02d}-{jd:02d}_{when:%H%M}"
        name = "" if brand.key == brands.FIRST else brand.name  # the first outlet keeps the approved title
        clean = docx_out.write(b, folder / f"{stem}.docx", when, outlet=name, period=period)
        review = docx_out.write(b, folder / f"{stem}_links.docx", when, with_links=True, outlet=name, period=period)
        counts = "\n".join(f"• {s['title']}: {len(s['entries'])}" for s in b["sections"])
        st = b["stats"]
        note = f"\n⚠️ فیدهای ناموفق: {', '.join(b['errors'])}" if b["errors"] else ""
        note += f"\n⚠️ این بخش‌ها نوشته نشدند: {', '.join(b['failed'])}" if b["failed"] else ""
        if st["sorted"] < st["items"]:
            note += (f"\n⚠️ {st['items'] - st['sorted']} خبر دسته‌بندی نشد و در بولتن نیامد"
                     + (f" ({b['class_errors'][0][:150]})" if b["class_errors"] else ""))
        await reply(
            f"✅ بولتن {brand.label} آماده شد ({'۸ صبح تا ۸ صبح؛ ' if config.BULLETIN_WINDOW == '8to8' else ''}{st['items']} خبر در {st['hours']} ساعت اخیر، {st['used']} خبر مرتبط در بخش‌ها):\n{counts}"
            f"{note}\n\nقبل از انتشار حتماً بازبینی کنید؛ فایل دوم زیر هر خبر لینک منبع را دارد.")
        with clean.open("rb") as f:
            await ctx.bot.send_document(chat, f, filename=clean.name, caption="🗞 بولتن (برای انتشار)")
        with review.open("rb") as f:
            await ctx.bot.send_document(chat, f, filename=review.name, caption="🔗 نسخه‌ی بازبینی با لینک منابع")
    finally:
        ctx.chat_data["bulletin_running"] = False


# ---------- pipeline steps ----------
async def next_in_queue(update, ctx):
    j = job(ctx)
    if "queue" not in j:  # the user went back with /list or /cancel while this item was still being built
        return
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
        j["pkg"] = await write_package(j["current"], feedback, j.get("pkg"), brand=brand_of(ctx))
    except Exception as e:
        log.exception("writer failed")
        await ctx.bot.send_message(update.effective_chat.id, f"❌ نوشتن شکست خورد:\n{e}")
        return
    buttons = Markup([[
        Btn("✅ تایید", callback_data="pkg_ok"), Btn("🔁 بازنویسی", callback_data="pkg_redo"),
        Btn("✏️ با توضیح من", callback_data="pkg_fb"), Btn("⏭ رد کردن", callback_data="skip")]])
    web = format_web(j["pkg"])
    await say(update, ctx, format_package(j["pkg"], j["current"], brand_of(ctx)), None if web else buttons)
    if web:  # the website text (inverted pyramid) as its own message, ready to copy; the buttons go under it
        await say(update, ctx, "📰 متن خبر برای سایت (هرم وارونه)\n\n" + web, buttons)


async def ask_narration_approval(update, ctx):
    j = job(ctx)
    j["narration"] = j["pkg"]["narration"]
    await ctx.bot.send_message(
        update.effective_chat.id,
        "🎙 متن نهایی نریشن (این متن به ElevenLabs داده می‌شود):\n\n" + j["narration"]
        + f"\n\n({len(j['narration'])} کاراکتر ≈ همین‌قدر اعتبار ElevenLabs)\n"
        "🧪 «پیش‌نمایش رایگان» ویدئو را با صدای رایگان می‌سازد تا بدون خرج اعتبار ببینید؛ بعد ساخت نهایی را بزنید.",
        reply_markup=Markup([[Btn("✅ ساخت " + f["label"], callback_data=f"nar_ok:{k}") for k, f in video.FORMATS.items()],
                             [Btn("🧪 پیش‌نمایش رایگان " + f["label"], callback_data=f"nar_test:{k}")
                              for k, f in video.FORMATS.items()],
                             [Btn("🖼 فقط کاور ۹:۱۶", callback_data="cover_only:9x16"),
                              Btn("🖼 فقط کاور ۴:۵", callback_data="cover_only:4x5")],
                             [Btn("✏️ ویرایش متن", callback_data="nar_edit")]]))


class BuildError(Exception):
    def __init__(self, step, err):
        super().__init__(f"مرحله‌ی «{step}»: {err}")


def _step(name, fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:
        log.exception("build step failed: %s", name)
        raise BuildError(name, e) from e


def done_media(j) -> list[dict]:
    return [m for m in j.get("media", []) if m.get("path")]


def _prepare(j):
    """Voice, word timings, subtitles; replaces the estimated sentence times with real ones (same sentences)."""
    prep = j["prep"]
    work = Path(prep["work"])
    work.mkdir(parents=True, exist_ok=True)
    brand = brands.get(j.get("brand"))
    audio, engine = _step("ساخت صدا", tts.synthesize_with_info, j["narration"], work / "narration.mp3",
                          "edge" if j.get("preview") else None, brand.voice_id or None)
    prep["voice_engine"] = engine
    spoken = _step("زمان‌بندی زیرنویس با Whisper", stt.transcribe_words, audio)
    words = stt.align_to_script(j["narration"], spoken)
    ass = video.build_ass(words, work / "subs.ass", group=config.SUBTITLE_WORDS, fmt=prep["fmt"],
                          highlight=brand.ass_highlight())
    total = video.duration(audio) + 0.5
    prep.update(audio=str(audio), ass=str(ass), total=total,
                sentences=editor.sentences(j["narration"], words, total))


def _plan(j, feedback: str | None = None) -> list[dict]:
    prep, media = j["prep"], done_media(j)
    shots = editor.frames(media, Path(prep["work"]) / "frames")
    return editor.plan(prep["sentences"], media, shots, feedback, prep.get("plan"))


def _render(j) -> Path:
    prep, media = j["prep"], done_media(j)
    work, fmt = Path(prep["work"]), prep["fmt"]
    if prep.get("plan"):
        segs = editor.segments(prep["plan"], prep["sentences"], media)
        src = _step("ترکیب ویدئوها و عکس‌ها", video.build_montage, media, prep["total"], work / "montage", fmt, segs)
    elif len(media) > 1 or (media and media[0]["kind"] == "photo"):
        src = _step("ترکیب ویدئوها و عکس‌ها", video.build_montage, media, prep["total"], work / "montage", fmt)
    else:
        src = Path(j["src"])
    logo = Path(config.LOGO_PATH)
    tpl = brands.get(j.get("brand")).path(f"template_{fmt}")
    return _step("تدوین نهایی با ffmpeg", video.build_video, src, Path(prep["audio"]), Path(prep["ass"]),
                 work / "final.mp4", logo if logo.exists() else None, fmt=fmt, template=tpl)


def retry_markup(j) -> Markup:
    fmt = j.get("fmt", "9x16")
    return Markup([[Btn(f"🔁 دوباره بساز ({video.FORMATS[fmt]['label']})", callback_data=f"nar_ok:{fmt}")]])


async def build_and_send(update, ctx):
    """After narration approval. 2+ items: AI edit plan FIRST (no ElevenLabs credits spent until it is approved);
    otherwise straight to voice + render."""
    j = job(ctx)
    chat = update.effective_chat.id
    if j.get("prep"):  # an earlier attempt: drop its files
        shutil.rmtree(j["prep"]["work"], ignore_errors=True)
    j["prep"] = {"work": str(config.WORK_DIR / f"{chat}_{int(time.time())}"), "fmt": j.get("fmt", "9x16"),
                 "sentences": editor.estimate(j["narration"]), "plan": None, "audio": None}
    j["prep"]["total"] = j["prep"]["sentences"][-1]["end"]
    if len(done_media(j)) < 2:
        await produce(update, ctx)
        return
    await ctx.bot.send_message(chat, "🧠 هوش مصنوعی در حال دیدن تصویرها و چیدنشان روی جمله‌های نریشن است…")
    await make_plan(update, ctx)


async def produce(update, ctx):
    """Voice (cached: never paid twice for the same text) + timings, then the final render."""
    j = job(ctx)
    chat = update.effective_chat.id
    prep = j["prep"]
    wrong_voice = not j.get("preview") and prep.get("voice_engine") == "edge" and config.TTS_ENGINE != "edge" \
        and not prep.get("edge_fallback_ok")
    if wrong_voice or not (prep.get("audio") and Path(prep["audio"]).exists()):
        await ctx.bot.send_message(chat, "⚙️ ساخت نریشن و زمان‌بندی زیرنویس…")
        try:
            await asyncio.to_thread(_prepare, j)
        except Exception as e:
            log.exception("prepare failed")
            await ctx.bot.send_message(chat, f"❌ ساخت ویدئو شکست خورد:\n{e}\n\nبرای تلاش دوباره، دکمه‌ی زیر را بزنید.",
                                       reply_markup=Markup([[Btn("🔁 دوباره امتحان کن", callback_data="plan_ok")]]))
            return
        if j["prep"].get("voice_engine") == "edge" and not j.get("preview") and config.TTS_ENGINE != "edge":
            j["prep"]["edge_fallback_ok"] = True  # ElevenLabs refused (credits/key): do not retry it on every press
            await ctx.bot.send_message(chat, "ℹ️ اعتبار ElevenLabs تمام شده (یا کلیدش کار نمی‌کند)؛ این بار از صدای "
                                             "رایگان مایکروسافت استفاده شد. بعد از شارژ ElevenLabs خودکار به صدای اصلی برمی‌گردد.")
    await render_and_send(update, ctx)


async def make_plan(update, ctx, feedback: str | None = None):
    j, chat = job(ctx), update.effective_chat.id
    try:
        j["prep"]["plan"] = await asyncio.to_thread(_plan, j, feedback)
    except Exception as e:
        log.exception("edit plan failed")
        if not j["prep"].get("plan"):
            j["prep"]["plan"] = editor.simple_plan(j["prep"]["sentences"], done_media(j))
        await ctx.bot.send_message(chat, f"⚠️ هوش مصنوعی نقشه‌ی تدوین را نساخت ({str(e)[:200]}).\n"
                                         "فعلاً نقشه‌ی قبلی یا ترتیب ساده نشان داده می‌شود.")
    await show_plan(update, ctx)


def _mmss(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


async def show_plan(update, ctx):
    j = job(ctx)
    prep, media = j["prep"], done_media(j)
    approx = "" if prep.get("audio") else "≈"
    lines = [f"🎬 نقشه‌ی تدوین (جمع {approx}{prep['total']:.0f} ثانیه"
             + ("؛ زمان‌ها تقریبی است و بعد از ساخت صدا دقیق می‌شود" if approx else "") + ")\n"]
    for p, sn in zip(prep["plan"], prep["sentences"]):
        m = media[p["item"] - 1]
        if m["kind"] == "video":
            what = f"ویدئو {p['item']}" + (f" از ثانیه {p['at']:.0f}" if p.get("at") is not None else "")
        else:
            what = f"عکس {p['item']}"
        note = f" «{m['note'][:30]}»" if m.get("note") else ""
        why = f" — {p['why']}" if p.get("why") else ""
        lines.append(f"{p['sentence']}. {_mmss(sn['start'])}–{_mmss(sn['end'])}  «{sn['text'][:70]}»\n"
                     f"   ← {what}{note}{why}\n")
    await say(update, ctx, "\n".join(lines), Markup([
        [Btn("✅ تایید و ساخت ویدئو", callback_data="plan_ok")],
        [Btn("✏️ تغییر با توضیح من", callback_data="plan_edit"), Btn("🔀 ترتیب ساده", callback_data="plan_simple")]]))


async def render_and_send(update, ctx):
    """Step 2: montage + subtitles -> final video, then send it with caption and cover."""
    j = job(ctx)
    chat = update.effective_chat.id
    await ctx.bot.send_message(chat, "🎬 تدوین نهایی… (چند دقیقه)")
    try:
        out = await asyncio.to_thread(_render, j)
    except Exception as e:
        log.exception("render failed")
        await ctx.bot.send_message(chat, f"❌ ساخت ویدئو شکست خورد:\n{e}\n\nبرای تلاش دوباره، دکمه‌ی زیر را بزنید.",
                                   reply_markup=Markup([[Btn("🔁 دوباره تدوین کن", callback_data="plan_ok")]]))
        return
    if j.get("preview"):  # free preview: show it, keep everything, offer the paid final build
        if out.stat().st_size > video.TELEGRAM_MAX_BYTES:
            await asyncio.to_thread(video.shrink, out)
        try:
            with out.open("rb") as f:
                size = video.FORMATS[j["prep"]["fmt"]]
                await ctx.bot.send_video(chat, f, supports_streaming=True, width=size["w"], height=size["h"],
                                         caption="🧪 پیش‌نمایش با صدای رایگان (بدون خرج اعتبار ElevenLabs)")
        except Exception as e:
            log.exception("preview upload failed")
            await ctx.bot.send_message(chat, f"⚠️ ارسال پیش‌نمایش شکست خورد: {e}")
        rows = [[Btn(f"✅ ساخت نهایی با ElevenLabs ({len(j['narration'])} کاراکتر)", callback_data="final_ok")],
                [Btn("✏️ ویرایش متن نریشن", callback_data="nar_edit")]]
        if j["prep"].get("plan"):
            rows.insert(1, [Btn("✏️ تغییر نقشه‌ی تدوین", callback_data="plan_edit")])
        await ctx.bot.send_message(chat, "اگر راضی هستید ساخت نهایی را بزنید؛ نقشه‌ی تدوین همین می‌ماند و فقط صدا عوض "
                                         "می‌شود.", reply_markup=Markup(rows))
        return
    work = Path(j["prep"]["work"])
    j["prep"] = None
    # keep the result outside the work dir so a failed upload can be retried
    OUTBOX.mkdir(parents=True, exist_ok=True)
    key = f"{chat}_{int(time.time())}"
    final = OUTBOX / f"{key}.mp4"
    shutil.move(str(out), final)
    shutil.rmtree(work, ignore_errors=True)
    if final.stat().st_size > video.TELEGRAM_MAX_BYTES:
        await ctx.bot.send_message(chat, "🗜 ویدئو برای تلگرام بزرگ است؛ کمی فشرده می‌شود…")
        await asyncio.to_thread(video.shrink, final)
    j.setdefault("outbox", {})[key] = {"fmt": j.get("fmt", "9x16"),
                                       "caption": ("🎬 " + cover_title(j["pkg"]).replace("\n", " "))[:1000]}
    for old in sorted(j["outbox"])[:-5]:  # keep the last 5 videos for re-sending
        j["outbox"].pop(old)
        (OUTBOX / f"{old}.mp4").unlink(missing_ok=True)
    await send_result(chat, ctx, key)
    await ctx.bot.send_message(chat, final_caption(j["pkg"], j["current"], brand=brand_of(ctx)))  # ready to copy
    if format_web(j["pkg"]):
        await say(update, ctx, "📰 متن خبر برای سایت\n\n" + format_web(j["pkg"]))
    await make_cover(update, ctx)
    await next_in_queue(update, ctx)


OUTBOX = config.WORK_DIR / "outbox"


async def send_result(chat, ctx, key) -> bool:
    """Upload the finished video once. On failure keep it and offer a resend button."""
    item = job(ctx).get("outbox", {}).get(key)
    path = OUTBOX / f"{key}.mp4"
    if not item or not path.exists():
        await ctx.bot.send_message(chat, "⏳ این ویدئو دیگر موجود نیست.")
        return False
    size = video.FORMATS[item["fmt"]]
    mb = path.stat().st_size / 1_000_000
    try:
        with path.open("rb") as f:
            await ctx.bot.send_video(chat, f, supports_streaming=True, width=size["w"], height=size["h"],
                                     caption=f"{item['caption']}\n\n📥 فایل اصلی با کیفیت کامل ({mb:.1f}MB)",
                                     filename="reel.mp4")
        return True
    except Exception as e:
        log.exception("video upload failed")
        await ctx.bot.send_message(
            chat, f"⚠️ ویدئو ساخته شد ({mb:.1f}MB) ولی ارسالش به تلگرام شکست خورد:\n{e}\n\n"
                  "ویدئو نگه داشته شده؛ دکمه‌ی زیر را بزنید تا دوباره فرستاده شود.",
            reply_markup=Markup([[Btn("🔁 ارسال دوباره‌ی ویدئو", callback_data=f"resend:{key}")]]))
        return False


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
    covers[key] = {"bg": str(bg), "bg_frame": str(bg), "bg_ai": None, "fmt": fmt, "title": cover_title(j["pkg"]),
                   "options": cover_options(j["pkg"]),
                   "topic": j["pkg"].get("topic") or j.get("current", {}).get("title_fa", ""),
                   "brand": brand_of(ctx).key}
    for old in sorted(covers)[:-10]:  # keep the last 10 covers editable
        c_old = covers.pop(old)
        for f in (c_old.get("bg_frame") or c_old["bg"], c_old.get("bg_ai")):
            if f:
                Path(f).unlink(missing_ok=True)
    if config.COVER_AI:
        await ai_cover_bg(update, ctx, key, quiet=True)
    await send_cover(update, ctx, key)


_ai_cover_warned = [False]


def AI_NAME() -> str:
    return "OpenAI" if config.IMAGE_PROVIDER == "openai" else "Gemini"


async def ai_cover_bg(update, ctx, key, quiet=False, extra: str | None = None, reference: str | None = None) -> bool:
    """Ask the image model for a designed background; keep the plain frame if it is not available.

    extra / reference: the user's own design instructions and a real photo to build on; both are remembered, so
    «طراحی دوباره» keeps using them.
    """
    j, chat = job(ctx), update.effective_chat.id
    c = j["covers"][key]
    if extra is not None:
        c["design_note"] = extra
    if reference is not None:
        c["ref"] = reference
    out = COVER_DIR / f"{chat}_{key}_ai_{int(time.time())}.png"
    if not quiet:
        await ctx.bot.send_message(chat, f"🎨 {AI_NAME()} در حال طراحی پس‌زمینه‌ی کاور…")
    try:
        await asyncio.to_thread(cover.ai_background, Path(c.get("ref") or c["bg_frame"]), c["title"], c.get("topic", ""),
                                out, c["fmt"], c.get("design_note", ""), brands.get(c.get("brand")))
    except Exception as e:
        log.warning("AI cover background failed: %s", e)
        if not quiet or not _ai_cover_warned[0]:  # say it once, not on every cover
            _ai_cover_warned[0] = True
            await ctx.bot.send_message(chat, f"ℹ️ طراحی کاور با {AI_NAME()} انجام نشد؛ کاور با فریم ویدئو ساخته شد.\n{e}")
        return False
    if c.get("bg_ai"):
        Path(c["bg_ai"]).unlink(missing_ok=True)
    c["bg_ai"] = c["bg"] = str(out)
    return True


def cover_keyboard(c: dict, key: str) -> Markup:
    opts = [Btn(f"گزینه {k + 1}", callback_data=f"covopt:{key}:{k}")
            for k, o in enumerate(c.get("options", [])) if o != c["title"]]
    design = [Btn(f"🎨 طراحی با {AI_NAME()}" if not c.get("bg_ai") else f"🎨 طراحی دوباره با {AI_NAME()}",
                  callback_data=f"covai:{key}")]
    if c.get("bg_ai") and c["bg"] == c["bg_ai"]:
        design.append(Btn("🖼 فقط فریم ویدئو", callback_data=f"covbg:{key}:frame"))
    elif c.get("bg_ai"):
        design.append(Btn(f"🎨 نسخه‌ی {AI_NAME()}", callback_data=f"covbg:{key}:ai"))
    return Markup(([opts] if opts else []) + [design,
                                              [Btn("✏️ طراحی با توضیح من", callback_data=f"covdesign:{key}")],
                                              [Btn("✏️ نوشتن متن دلخواه", callback_data=f"cov:{key}")]])


async def send_cover(update, ctx, key):
    j, chat = job(ctx), update.effective_chat.id
    c = j["covers"][key]
    out = COVER_DIR / f"{chat}_{key}.jpg"
    try:
        await asyncio.to_thread(cover.compose, Path(c["bg"]), c["title"], out, c["fmt"],
                                brands.get(c.get("brand")).path("cover_icon"))
    except Exception as e:
        log.exception("cover compose failed")
        await ctx.bot.send_message(chat, f"⚠️ ساخت کاور شکست خورد (ویدئو سالم است):\n{e}")
        return
    with out.open("rb") as f:
        await ctx.bot.send_photo(chat, f, caption="🖼 کاور: " + c["title"].replace("\n", " / "),
                                 reply_markup=cover_keyboard(c, key))
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
             "nar_edit": "pkg", "nar_ok": "src", "nar_test": "src"}
    key = d.split(":")[0] if d.startswith(("nar_ok", "nar_test")) else d
    if (d.startswith(("sel:", "pg:")) and not j.get("items")) or (key in needs and needs[key] not in j):
        await q.message.reply_text("⏳ این دکمه منقضی شده (ربات ری‌استارت شده یا دور جدید شروع شده). دوباره /news بزنید.")
        return
    if d.startswith("resend:"):
        await q.edit_message_reply_markup(None)
        await q.message.reply_text("📤 در حال ارسال دوباره…")
        await send_result(update.effective_chat.id, ctx, d[7:])
        return
    if d.startswith(("news_brand:", "bul_brand:")):
        action, key = d.split(":", 1)
        await q.edit_message_reply_markup(None)
        if action == "news_brand":
            await run_news(update, ctx, key)
        else:
            await run_bulletin(update, ctx, key)
        return
    if d.startswith("covdesign:"):
        key = d.split(":", 1)[1]
        if key not in j.get("covers", {}):
            await q.message.reply_text("⏳ این کاور دیگر قابل ویرایش نیست.")
            return
        j["cover_edit"], j["stage_before_cover"] = key, j.get("stage")
        j["stage"] = "cover_design"
        await q.message.reply_text(
            "🎨 بنویسید چه طرحی می‌خواهید، مثلاً:\n"
            "• «پرچم عراق موج‌دار و نقشه‌ی عراق طلایی با فلش قرمز، کاخ دولت پشتش»\n"
            "• «نفتکش در حال سوختن در تنگه هرمز، آسمان نارنجی»\n\n"
            "📷 یا یک **عکس واقعی** بفرستید (مثلاً عکس خبری شخص یا محل) و توضیح را در کپشنش بنویسید؛ طرح دور همان عکس "
            "ساخته می‌شود.")
        return
    if d.startswith("cover_only:"):
        # cover without voice or video: no ElevenLabs credits; the narration buttons stay usable afterwards
        if not j.get("src") or not j.get("pkg"):
            await q.message.reply_text("⏳ این دکمه منقضی شده؛ دوباره از /list یا /news شروع کنید.")
            return
        fmt = d.split(":", 1)[1]
        prev = j.get("fmt")
        j["fmt"] = fmt if fmt in video.FORMATS else "9x16"
        await q.message.reply_text("🖼 در حال ساخت کاور (بدون صدا و بدون خرج اعتبار ElevenLabs)…")
        await make_cover(update, ctx)
        if prev:
            j["fmt"] = prev
        return
    if d.startswith(("covai:", "covbg:")):
        parts = d.split(":")
        c = j.get("covers", {}).get(parts[1])
        if not c:
            await q.message.reply_text("⏳ این کاور دیگر قابل ویرایش نیست.")
            return
        if parts[0] == "covai":
            if not await ai_cover_bg(update, ctx, parts[1]):
                return
        else:
            c["bg"] = c["bg_ai"] if parts[2] == "ai" and c.get("bg_ai") else c.get("bg_frame", c["bg"])
        await send_cover(update, ctx, parts[1])
        return
    if d.startswith("covopt:"):
        _, key, k = d.split(":")
        c = j.get("covers", {}).get(key)
        if not c or int(k) >= len(c.get("options", [])):
            await q.message.reply_text("⏳ این کاور دیگر قابل ویرایش نیست.")
            return
        c["title"] = c["options"][int(k)]
        await q.message.reply_text(f"🖼 در حال ساخت کاور با گزینه {int(k) + 1}…")
        await send_cover(update, ctx, key)
        return
    if d.startswith("cov:"):
        key = d[4:]
        if key not in j.get("covers", {}):
            await q.message.reply_text("⏳ این کاور دیگر قابل ویرایش نیست.")
            return
        j["cover_edit"], j["stage_before_cover"] = key, j.get("stage")
        j["stage"] = "cover_text"
        await q.message.reply_text("متن جدید کاور را بفرستید؛ هر خط یک نوار جدا می‌شود (حداکثر ۳ خط). متن فعلی:\n\n"
                                   + j["covers"][key]["title"])
        return
    if d == "cancel":
        ctx.user_data.pop("job", None)
        await q.edit_message_text("لغو شد.")
    elif d.startswith("sel:") and j.get("items"):
        i = int(d[4:])
        j["selected"] ^= {i}
        await q.edit_message_reply_markup(news_keyboard(j["items"], j["selected"], j.get("page", 0)))
    elif d.startswith("pg:") and j.get("items"):
        j["page"] = int(d[3:])
        await q.edit_message_reply_markup(news_keyboard(j["items"], j["selected"], j["page"]))
    elif d == "noop":
        pass
    elif d == "news_all" and j.get("all_items"):
        await show_news(update, ctx, all_view=True)
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
        j["stage"], j["media"], j["pending"], j["media_msg"] = "link", [], 0, None
        download.fresh_dir(media_root(update))
        await q.message.reply_text(
            "🎞 ویدئوها و عکس‌های این خبر را بفرستید؛ هر تعداد که خواستید، به همان ترتیبی که باید پخش شوند:\n"
            "• لینک ویدئو یا عکس، پست عکس اینستاگرام (چندعکسی هم)، یا لینک صفحه‌ی خبر (چند لینک در یک پیام هم می‌شود)\n"
            "• فایل ویدئو یا عکس (سقف هر فایل ~۲۰MB)\n\n"
            "هر وقت تمام شد، دکمه‌ی «✅ تمام شد» را بزنید.")
    elif d == "media_done":
        if j.get("stage") != "link":
            return
        if j.get("pending"):
            await q.answer(f"صبر کنید؛ {j['pending']} مورد هنوز در حال دانلود است.", show_alert=True)
            return
        media = [m for m in j.get("media", []) if m.get("path")]
        if not media:
            await q.answer("هنوز هیچ ویدئو یا عکسی نفرستاده‌اید.", show_alert=True)
            return
        j["media"], j["stage"] = media, None
        cover_src = next((m for m in media if m["kind"] == "video"), media[0])  # cover prefers real footage
        j["src"] = cover_src["path"]
        await q.edit_message_reply_markup(None)
        await q.message.reply_text(f"✅ {len(media)} مورد دریافت شد: {media_summary(media)}")
        await ask_narration_approval(update, ctx)
    elif d == "media_undo":
        done = [m for m in j.get("media", []) if m.get("path")]
        if done:
            j["media"].remove(done[-1])
        await media_status(update, ctx)
    elif d == "media_reset":
        j["media"] = [m for m in j.get("media", []) if not m.get("path")]  # keep only in-flight downloads
        await media_status(update, ctx)
    elif d == "nar_edit":
        j["stage"] = "narration_edit"
        await q.message.reply_text("متن جدید نریشن را بفرستید:")
    elif d in ("plan_ok", "plan_edit", "plan_simple"):
        if not j.get("prep"):
            await q.message.reply_text("⏳ این نقشه منقضی شده؛ دوباره «ساخت» را بزنید.", reply_markup=retry_markup(j))
            return
        if d == "plan_ok":
            await q.edit_message_reply_markup(None)  # no double render from a second press
            await produce(update, ctx)
        elif d == "plan_edit":
            j["stage"] = "plan_feedback"
            await q.message.reply_text(
                "توضیح بدهید چه تغییری می‌خواهید؛ مثلاً:\n• «جمله‌ی ۳ رو با عکس ۲ بذار»\n"
                "• «ویدئوی ۱ رو از ثانیه ۴۰ نشون بده»\n• «ناو هواپیمابر بیشتر دیده بشه»")
        else:
            j["prep"]["plan"] = editor.simple_plan(j["prep"]["sentences"], done_media(j))
            await show_plan(update, ctx)
    elif d == "final_ok":
        if not j.get("prep"):
            await q.message.reply_text("⏳ این پیش‌نمایش منقضی شده؛ دوباره «ساخت» را بزنید.", reply_markup=retry_markup(j))
            return
        await q.edit_message_reply_markup(None)
        j["preview"] = False
        await produce(update, ctx)
    elif d.startswith(("nar_ok", "nar_test")):
        fmt = d.split(":", 1)[1] if ":" in d else "9x16"  # old buttons had no format
        j["fmt"] = fmt if fmt in video.FORMATS else "9x16"
        j["preview"] = d.startswith("nar_test")
        await q.edit_message_reply_markup(None)  # no double build from a second press
        await build_and_send(update, ctx)


# ---------- messages ----------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    j, text = job(ctx), update.message.text
    stage = j.get("stage")
    if stage == "downloading":
        await update.message.reply_text("⏳ هنوز در حال دانلود ویدئوی قبلی هستم؛ صبر کنید.")
        return
    if stage == "cover_design":
        key = j.pop("cover_edit", None)
        j["stage"] = j.pop("stage_before_cover", None)
        if key in j.get("covers", {}):
            if await ai_cover_bg(update, ctx, key, extra=text.strip()):
                await send_cover(update, ctx, key)
        return
    if stage == "cover_text":
        key = j.pop("cover_edit", None)
        j["stage"] = j.pop("stage_before_cover", None)  # go back to whatever step the user was on
        if key in j.get("covers", {}):
            j["covers"][key]["title"] = text.strip()
            await update.message.reply_text("🖼 در حال ساخت کاور جدید…")
            await send_cover(update, ctx, key)
        return
    if stage == "plan_feedback":
        j["stage"] = None
        if not j.get("prep"):
            await update.message.reply_text("⏳ این نقشه منقضی شده؛ دوباره «ساخت» را بزنید.")
            return
        await update.message.reply_text("🧠 در حال اعمال توضیح شما روی نقشه‌ی تدوین…")
        await make_plan(update, ctx, text)
        return
    if stage == "feedback":
        j["stage"] = None
        await write_and_show(update, ctx, text)
    elif stage == "link":
        urls = URL_RE.findall(text)
        rest = URL_RE.sub(" ", text).strip()
        if not urls:  # plain text: a description of the last item sent
            if not j.get("media"):
                await update.message.reply_text("اول لینک یا فایل بفرستید؛ بعد می‌توانید زیرش توضیح بنویسید.")
                return
            j["media"][-1]["note"] = rest
            await media_status(update, ctx, force_new=True)
            return
        note = rest if len(urls) == 1 and rest else None  # "link + description" in one message
        await asyncio.gather(*(add_link(update, ctx, u, note) for u in urls))
    elif stage == "narration_edit":
        j["pkg"]["narration"] = text
        j["stage"] = None
        await ask_narration_approval(update, ctx)
    else:
        await update.message.reply_text("برای شروع /news")


async def cover_photo(update, ctx):
    """A real photo sent for the cover design: the background is built around it (caption = instructions)."""
    j, msg = job(ctx), update.message
    att = msg.photo[-1] if msg.photo else msg.document
    if not att or (msg.document and not (msg.document.mime_type or "").startswith("image/")):
        await msg.reply_text("برای طراحی کاور، عکس بفرستید (نه ویدئو).")
        return
    key = j.pop("cover_edit", None)
    j["stage"] = j.pop("stage_before_cover", None)
    if key not in j.get("covers", {}):
        return
    COVER_DIR.mkdir(parents=True, exist_ok=True)
    raw = COVER_DIR / f"{update.effective_chat.id}_{key}_ref_raw"
    f = await att.get_file()
    await f.download_to_drive(raw)
    ref = COVER_DIR / f"{update.effective_chat.id}_{key}_ref.png"  # normalise any image format to PNG
    await asyncio.to_thread(subprocess.run, ["ffmpeg", "-y", "-i", str(raw), "-frames:v", "1", str(ref)], capture_output=True)
    raw.unlink(missing_ok=True)
    if not ref.exists():
        await msg.reply_text("❌ این عکس خوانده نشد؛ عکس دیگری بفرستید.")
        return
    if await ai_cover_bg(update, ctx, key, extra=(msg.caption or "").strip(), reference=str(ref)):
        await send_cover(update, ctx, key)


def media_root(update) -> Path:
    return config.WORK_DIR / f"dl_{update.effective_chat.id}"


def media_keyboard() -> Markup:
    return Markup([[Btn("✅ تمام شد", callback_data="media_done")],
                   [Btn("↩️ حذف آخری", callback_data="media_undo"), Btn("🗑 از اول", callback_data="media_reset")]])


def media_summary(media: list[dict]) -> str:
    done = [m for m in media if m.get("path")]
    vids = [m for m in done if m["kind"] == "video"]
    photos = len(done) - len(vids)
    parts = []
    if vids:
        parts.append(f"{len(vids)} ویدئو ({sum(m['dur'] for m in vids):.0f} ثانیه)")
    if photos:
        parts.append(f"{photos} عکس")
    return "، ".join(parts) or "هیچ"


def media_lines(media: list[dict]) -> str:
    out = []
    for k, m in enumerate(media, 1):
        if not m.get("path"):
            what = "⏳ در حال دانلود"
        elif m["kind"] == "video":
            what = f"ویدئو، {m['dur']:.0f} ثانیه"
        else:
            what = "عکس"
        out.append(f"{k}. {what}" + (f" — 📝 {m['note'][:60]}" if m.get("note") else ""))
    return "\n".join(out[-20:])


async def media_status(update, ctx, force_new: bool = False):
    """Keep ONE status message up to date (edit it) instead of replying to every file: avoids flood limits."""
    j, chat = job(ctx), update.effective_chat.id
    pending = j.get("pending", 0)
    text = (f"🎞 دریافت‌شده: {media_summary(j.get('media', []))}"
            + (f"\n⏳ در حال دانلود: {pending} مورد" if pending else "")
            + f"\n\n{media_lines(j.get('media', []))}\n\n"
            "بعدی را بفرستید یا «✅ تمام شد» را بزنید.\n"
            "💡 بعد از هر لینک یا فایل می‌توانید یک توضیح کوتاه بنویسید (مثلاً «سخنرانی ترامپ») "
            "تا هوش مصنوعی بهتر بچیند.")
    msg_id = None if force_new else j.get("media_msg")
    if msg_id:
        try:
            await ctx.bot.edit_message_text(text, chat_id=chat, message_id=msg_id, reply_markup=media_keyboard())
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
    m = await ctx.bot.send_message(chat, text, reply_markup=media_keyboard())
    j["media_msg"] = m.message_id


async def _collect(update, ctx, label: str, fetch, note: str | None = None):
    """Reserve a slot (keeps the order the user sent things), fetch the file, then report."""
    j = job(ctx)
    slot = {"path": None, "id": time.time_ns(), "note": note}  # unique id: placeholders never compare equal
    j.setdefault("media", []).append(slot)
    j["pending"] = j.get("pending", 0) + 1
    await media_status(update, ctx)
    try:
        d = download.fresh_dir(media_root(update) / str(time.time_ns()))
        got = await fetch(d)
        got = got if isinstance(got, list) else [got]  # a photo post / carousel gives several files
        at = j["media"].index(slot) if slot in j.get("media", []) else len(j.get("media", []))
        for k, (path, kind) in enumerate(got):
            dur = await asyncio.to_thread(download.check_video, path) if kind == "video" else 0.0
            if k == 0:  # a description typed while downloading stays on the first slot
                slot.update(path=str(path), kind=kind, dur=dur)
            elif slot in j.get("media", []):  # the rest go right after it, in post order
                j["media"].insert(at + k, {"path": str(path), "kind": kind, "dur": dur, "id": time.time_ns(),
                                           "note": None})
    except Exception as e:
        log.exception("media fetch failed")
        if slot in j.get("media", []):
            j["media"].remove(slot)
        await update.message.reply_text(f"❌ {label} دریافت نشد:\n{e}\n\nمی‌توانید دوباره بفرستید.")
        return
    finally:
        j["pending"] = max(0, j.get("pending", 1) - 1)
        await media_status(update, ctx)


async def add_link(update, ctx, url: str, note: str | None = None):
    async def fetch(d):  # video, photo link, photo post/carousel or a news page's main picture
        return await asyncio.to_thread(download.fetch_link, url, d)
    await _collect(update, ctx, f"محتوای این لینک ({url[:60]})", fetch, note)


async def on_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    j = job(ctx)
    if j.get("stage") == "cover_design":
        await cover_photo(update, ctx)
        return
    if j.get("stage") != "link":
        return
    msg = update.message
    if msg.photo:
        att, kind, name = msg.photo[-1], "photo", "photo.jpg"  # largest size
    elif msg.video or msg.animation:
        att, kind, name = msg.video or msg.animation, "video", "source.mp4"
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        att, kind, name = msg.document, "photo", "photo" + (Path(msg.document.file_name or "").suffix or ".jpg")
    else:
        att, kind, name = msg.document, "video", "source" + (Path(msg.document.file_name or "").suffix or ".mp4")

    async def fetch(d):
        path = d / name
        f = await att.get_file()
        await f.download_to_drive(path)
        return path, kind
    await _collect(update, ctx, "فایل (سقف ~۲۰MB؛ فایل بزرگ‌تر را با لینک بفرستید)", fetch,
                   (msg.caption or "").strip() or None)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("update failed", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await ctx.bot.send_message(update.effective_chat.id, f"❌ خطای غیرمنتظره:\n{ctx.error}\n\nدوباره امتحان کنید یا /news")
        except Exception:
            pass


# ---------- hub ("mother") bot ----------
async def hub_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """News Room: a menu that opens the bot of each outlet this user may use."""
    uid = update.effective_user.id
    rows = []
    for b in brands.BRANDS.values():
        name = brands.bot_username(b)
        if name and uid in brands.allowed_users(b):
            rows.append([Btn(b.label, url=f"https://t.me/{name}?start=hub")])
        bul = brands.bulletin_username(b)
        if bul and uid in brands.allowed_users(b):
            rows.append([Btn(f"🗞 بولتن {b.name}", url=f"https://t.me/{bul}?start=hub")])
    if not rows:
        await update.effective_message.reply_text("هنوز به ربات هیچ رسانه‌ای دسترسی ندارید.")
        return
    await update.effective_message.reply_text(
        "🗞 برای کدام رسانه کار می‌کنید؟\n(دکمه، ربات همان رسانه را باز می‌کند؛ بار اول یک‌بار «Start» را بزنید.)",
        reply_markup=Markup(rows))


async def set_menu(app):
    """The command menu next to the message box (so it never has to be set by hand in BotFather)."""
    if config.BOT_NAME:
        try:
            await app.bot.set_my_name(config.BOT_NAME)
        except Exception:
            log.exception("could not set the bot name")
    if config.BOT_MODE == "bulletin":
        try:
            await app.bot.set_my_commands([BotCommand("bulletin", "ساخت بولتن (Word)")])
        except Exception:
            log.exception("could not set the command menu")
        return
    if config.BOT_MODE == "hub":
        try:
            await app.bot.set_my_commands([BotCommand("start", "انتخاب رسانه")])
        except Exception:
            log.exception("could not set the command menu")
        return
    try:
        await app.bot.set_my_commands([
            BotCommand("news", "جست‌وجوی خبرهای مهم"),
            BotCommand("list", "برگرد به لیست اخبار"),
            BotCommand("bulletin", "ساخت بولتن خبری (Word)"),
            BotCommand("cancel", "لغو کار جاری"),
        ])
    except Exception:
        log.exception("could not set the command menu")


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
    # rate_limiter: when Telegram says "flood control, retry in N s", wait and resend instead of failing
    # generous timeouts: uploading a finished video from a home connection can take minutes
    app = (Application.builder().token(config.TELEGRAM_BOT_TOKEN).concurrent_updates(True)
           .connect_timeout(30).read_timeout(60).write_timeout(60).pool_timeout(30).media_write_timeout(600)
           .rate_limiter(AIORateLimiter(max_retries=5))
           .persistence(PicklePersistence(filepath=config.WORK_DIR / "state.pickle")).post_init(set_menu).build())
    if config.BOT_MODE == "hub":  # the mother bot only shows the outlet menu
        everyone = set(config.ALLOWED_USER_IDS).union(*(brands.allowed_users(b) for b in brands.BRANDS.values()))
        hub_users = filters.User(user_id=list(everyone))
        app.add_handler(MessageHandler(hub_users, hub_start))
        app.add_error_handler(on_error)
        log.info("hub bot: %s", ", ".join(f"{b.key}=@{brands.bot_username(b) or '?'}" for b in brands.BRANDS.values()))
        app.run_polling()
        return
    if config.BOT_MODE == "bulletin":  # a bot that only makes the bulletin
        bul_users = filters.User(user_id=list(config.ALLOWED_USER_IDS))
        app.add_handler(CommandHandler("bulletin", lambda u, c: run_bulletin(u, c, brands.DEFAULT), filters=bul_users))
        app.add_handler(CallbackQueryHandler(bulletin_button, pattern="^bul_go$"))
        app.add_handler(MessageHandler(bul_users, bulletin_home))
        app.add_error_handler(on_error)
        log.info("bulletin bot for %s (window=%s, analysis=%s)", brands.get(None).name, config.BULLETIN_WINDOW,
                 config.BULLETIN_ANALYSIS)
        app.run_polling()
        return
    if config.BOT_MODE == "brand" and config.BOT_BRAND not in brands.BRANDS:
        raise SystemExit(f"BOT_BRAND={config.BOT_BRAND!r} is not a folder in brands/ ({', '.join(brands.BRANDS)})")
    log.info("bot for: %s", ", ".join(b.name for b in brands.available()))
    allowed = filters.User(user_id=list(config.ALLOWED_USER_IDS))
    app.add_handler(CommandHandler("start", cmd_start, filters=allowed))
    app.add_handler(CommandHandler("news", cmd_news, filters=allowed))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=allowed))
    app.add_handler(CommandHandler("bulletin", cmd_bulletin, filters=allowed))
    app.add_handler(CommandHandler("list", cmd_list, filters=allowed))
    app.add_handler(MessageHandler(allowed & (filters.VIDEO | filters.ANIMATION | filters.PHOTO | filters.Document.VIDEO
                                              | filters.Document.IMAGE), on_media))
    app.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)
    # load Whisper in the background now, so a missing download shows up at startup, not in the first video
    import threading

    def _preload():
        try:
            stt.preload()
            log.info("Whisper model ready")
        except Exception:
            log.exception("Whisper model could not be loaded (will retry when a video is built)")
    threading.Thread(target=_preload, daemon=True).start()
    app.run_polling()


if __name__ == "__main__":
    main()
