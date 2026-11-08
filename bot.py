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
PAGE = 10  # news buttons per keyboard page


def news_keyboard(items, selected, page=0):
    pages = max(1, -(-len(items) // PAGE))
    page = min(max(page, 0), pages - 1)
    start = page * PAGE
    rows = [[Btn(("✅ " if i in selected else "⬜ ") + ("⭐" if items[i].get("relevant") else "")
                 + f"{i + 1}. {items[i]['title_fa'][:44]}", callback_data=f"sel:{i}")]
            for i in range(start, min(start + PAGE, len(items)))]
    if pages > 1:
        rows.append([Btn("◀️ قبلی", callback_data=f"pg:{page - 1}") if page > 0 else Btn(" ", callback_data="noop"),
                     Btn(f"صفحه {page + 1} از {pages}", callback_data="noop"),
                     Btn("بعدی ▶️", callback_data=f"pg:{page + 1}") if page < pages - 1 else Btn(" ", callback_data="noop")])
    rows.append([Btn(f"▶️ تایید انتخاب ({len(selected)})", callback_data="go"), Btn("❌ لغو", callback_data="cancel")])
    return Markup(rows)


def format_news(items) -> str:
    out = [f"📰 همه‌ی {len(items)} خبر (جدیدترین اول؛ ⭐ = مرتبط با موضوعات اصلی)\n"]
    for i, it in enumerate(items, 1):
        star = "⭐ " if it.get("relevant") else ""
        out.append(f"{i}. {star}{it['title_fa']}\n   {it['source']} | {it.get('hours_ago_estimate', '?')}\n   {it['url']}\n")
    return "\n".join(out)


# Persian names of the outlets, for the source line in captions (prefix match on the feed name)
SOURCE_FA = [("Bloomberg", "بلومبرگ"), ("Reuters", "رویترز"), ("Financial Times", "فایننشال تایمز"),
             ("Wall Street Journal", "وال‌استریت ژورنال"), ("Asharq Al-Awsat", "الشرق الاوسط"), ("Asharq", "الشرق"),
             ("Foreign Policy", "فارن پالیسی"), ("Al Jazeera", "الجزیره"), ("OilPrice", "اویل‌پرایس"),
             ("The Guardian", "گاردین"), ("BBC Persian", "بی‌بی‌سی فارسی"), ("BBC", "بی‌بی‌سی"),
             ("New York Times", "نیویورک تایمز")]


def source_fa(item: dict) -> str:
    name = item.get("source", "")
    for prefix, fa in SOURCE_FA:
        if name.startswith(prefix):
            return fa
    return re.sub(r"\s*\(.*?\)\s*$", "", name).strip()  # e.g. "دنیای اقتصاد (Google News)" -> "دنیای اقتصاد"


def final_caption(pkg: dict, item: dict, limit: int = 2200) -> str:
    """Caption + mandatory source line + hashtags. The caption body is shortened if needed, never the source."""
    src = source_fa(item)
    source_line = f"📌 منبع: {src}" if src else ""
    tags = [t if t.startswith("#") else "#" + t for t in (str(x).strip().replace(" ", "_") for x in pkg.get("hashtags", [])) if t]
    tags = " ".join(["#آوای_نفت"] + [t for t in tags if t != "#آوای_نفت"])
    tail = "\n\n".join(x for x in (source_line, tags) if x)
    body = pkg.get("caption", "").strip()
    room = limit - len(tail) - 2
    if len(body) > room:
        body = body[:max(0, room - 1)].rstrip() + "…"
    return f"{body}\n\n{tail}" if body else tail


def format_package(pkg: dict, item: dict | None = None) -> str:
    if "cover_options" not in pkg and "scenario" in pkg:  # package written before the new format
        return _format_package_old(pkg, item)
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
            f"📝 کپشن کامل اینستاگرام\n{final_caption(pkg, item or {})}\n\n"
            f"موارد نیازمند بررسی شما:\n{flags}")


def _format_package_old(pkg: dict, item: dict | None = None) -> str:
    scenes = "\n".join(f"• {s.get('time', '')} | {s.get('visual', '')} | «{s.get('text_on_screen', '')}»"
                       for s in pkg.get("scenario", []))
    flags = "\n".join(f"⚠️ {f}" for f in pkg.get("flags", [])) or "—"
    return (f"🎬 هوک\n{pkg.get('hook', '')}\n\n🖼 تیتر کاور\n{cover_title(pkg)}\n\n🎙 نریشن\n{pkg.get('narration', '')}"
            f"\n\n📝 کپشن\n{final_caption(pkg, item or {})}\n\n🎞 سناریو\n{scenes}\n\nموارد نیازمند بررسی شما:\n{flags}")


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
    await update.message.reply_text("سلام 👋\n/news ← جست‌وجوی تیترهای ۱۲ ساعت اخیر\n/cancel ← لغو کار جاری")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("job", None)
    await update.message.reply_text("کار جاری لغو شد.")


async def cmd_news(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["job"] = {}
    await update.message.reply_text("🔎 در حال جمع‌آوری همه‌ی اخبار و ترجمه‌ی تیترها… (۱ تا ۳ دقیقه)")
    try:
        items, sources = await find_news()
    except Exception as e:
        log.exception("scout failed")
        await update.message.reply_text(f"❌ جست‌وجو شکست خورد:\n{e}")
        return
    j = job(ctx)
    j.update(items=items, selected=set(), page=0)
    await say(update, ctx, format_news(items))
    if sources:
        await say(update, ctx, "\n".join(f"ℹ️ {s['title']} {s['uri']}".strip() for s in sources[:12]))
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
    await say(update, ctx, format_package(j["pkg"], j["current"]), Markup([[
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
    media = [m for m in j.get("media", []) if m.get("path")]
    if len(media) > 1 or (media and media[0]["kind"] == "photo"):
        src = video.build_montage(media, video.duration(audio) + 0.5, work / "montage", fmt)
    else:
        src = Path(j["src"])
    logo = Path(config.LOGO_PATH)
    return video.build_video(src, audio, ass, work / "final.mp4", logo if logo.exists() else None, fmt=fmt)


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
                                 caption=("🎬 " + cover_title(j["pkg"]).replace("\n", " "))[:1000])
    await ctx.bot.send_document(chat, out.open("rb"), filename="reel.mp4", caption="📥 فایل برای دانلود/ذخیره")
    await ctx.bot.send_message(chat, final_caption(j["pkg"], j["current"]))  # full Instagram caption, ready to copy
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
    covers[key] = {"bg": str(bg), "fmt": fmt, "title": cover_title(j["pkg"]), "options": cover_options(j["pkg"])}
    for old in sorted(covers)[:-10]:  # keep the last 10 covers editable
        Path(covers.pop(old)["bg"]).unlink(missing_ok=True)
    await send_cover(update, ctx, key)


def cover_keyboard(c: dict, key: str) -> Markup:
    opts = [Btn(f"گزینه {k + 1}", callback_data=f"covopt:{key}:{k}")
            for k, o in enumerate(c.get("options", [])) if o != c["title"]]
    return Markup(([opts] if opts else []) + [[Btn("✏️ نوشتن متن دلخواه", callback_data=f"cov:{key}")]])


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
             "nar_edit": "pkg", "nar_ok": "src"}
    key = d.split(":")[0] if d.startswith("nar_ok") else d
    if (d.startswith(("sel:", "pg:")) and not j.get("items")) or (key in needs and needs[key] not in j):
        await q.message.reply_text("⏳ این دکمه منقضی شده (ربات ری‌استارت شده یا دور جدید شروع شده). دوباره /news بزنید.")
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
        j["stage"], j["media"], j["pending"] = "link", [], 0
        download.fresh_dir(media_root(update))
        await q.message.reply_text(
            "🎞 ویدئوها و عکس‌های این خبر را بفرستید؛ هر تعداد که خواستید، به همان ترتیبی که باید پخش شوند:\n"
            "• لینک ویدئو (چند لینک در یک پیام هم می‌شود)\n• فایل ویدئو یا عکس (سقف هر فایل ~۲۰MB)\n\n"
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
        await q.message.reply_text(f"↩️ آخرین مورد حذف شد. الان: {media_summary(j.get('media', []))}",
                                   reply_markup=media_keyboard())
    elif d == "media_reset":
        j["media"] = [m for m in j.get("media", []) if not m.get("path")]  # keep only in-flight downloads
        await q.message.reply_text("🗑 همه حذف شد؛ از اول بفرستید.", reply_markup=media_keyboard())
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
        urls = URL_RE.findall(text)
        if not urls:
            await update.message.reply_text("لینک معتبر نیست. لینک یا فایل بفرستید، یا «✅ تمام شد» را بزنید.",
                                            reply_markup=media_keyboard())
            return
        await asyncio.gather(*(add_link(update, ctx, u) for u in urls))
    elif stage == "narration_edit":
        j["pkg"]["narration"] = text
        j["stage"] = None
        await ask_narration_approval(update, ctx)
    else:
        await update.message.reply_text("برای شروع /news")


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


async def _collect(update, ctx, label: str, fetch):
    """Reserve a slot (keeps the order the user sent things), fetch the file, then report."""
    j = job(ctx)
    slot = {"path": None, "id": time.time_ns()}  # unique id: placeholders must never compare equal
    j.setdefault("media", []).append(slot)
    j["pending"] = j.get("pending", 0) + 1
    try:
        d = download.fresh_dir(media_root(update) / str(time.time_ns()))
        path, kind = await fetch(d)
        dur = await asyncio.to_thread(download.check_video, path) if kind == "video" else 0.0
        slot.update(path=str(path), kind=kind, dur=dur)
    except Exception as e:
        log.exception("media fetch failed")
        if slot in j.get("media", []):
            j["media"].remove(slot)
        await update.message.reply_text(f"❌ {label} دریافت نشد:\n{e}\n\nمی‌توانید دوباره بفرستید.")
        return
    finally:
        j["pending"] = max(0, j.get("pending", 1) - 1)
    if slot not in j.get("media", []):  # user pressed «از اول» meanwhile
        return
    n = sum(1 for m in j["media"] if m.get("path"))
    what = f"ویدئو، {dur:.0f} ثانیه" if kind == "video" else "عکس"
    await update.message.reply_text(f"✅ مورد {n} اضافه شد ({what}). جمع: {media_summary(j['media'])}\n"
                                    "بعدی را بفرستید یا «✅ تمام شد» را بزنید.", reply_markup=media_keyboard())


async def add_link(update, ctx, url: str):
    async def fetch(d):
        return await asyncio.to_thread(download.download_video, url, d), "video"
    await update.message.reply_text(f"⬇️ در حال دانلود…\n{url[:80]}")
    await _collect(update, ctx, "ویدئوی این لینک", fetch)


async def on_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    j = job(ctx)
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
    await _collect(update, ctx, "فایل (سقف ~۲۰MB؛ فایل بزرگ‌تر را با لینک بفرستید)", fetch)


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
    app.add_handler(MessageHandler(allowed & (filters.VIDEO | filters.ANIMATION | filters.PHOTO | filters.Document.VIDEO
                                              | filters.Document.IMAGE), on_media))
    app.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)
    app.run_polling()


if __name__ == "__main__":
    main()
