"""
Cybersecurity Daily News Telegram Bot  — Interactive Edition
-------------------------------------------------------------
All story selection is done via inline keyboard buttons;
no typing required after the digest appears.

Story buttons update the same message in-place (edit_message_text).

Callback-data format (all well under Telegram's 64-byte limit):
  s|<idx>   — open full summary for story at 0-based index
  b         — back to the digest story list
  rf        — re-fetch feeds and refresh the digest

Commands:
  /news          — pull & display today's top stories now
  /subscribe     — add this chat to the daily digest
  /unsubscribe   — remove this chat from the daily digest
  /help          — usage

Dependencies: python-telegram-bot[job-queue]>=21, feedparser
"""

import os
import re
import html
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import feedparser

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("cyber-news-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
DIGEST_CHAT_IDS = [
    c.strip() for c in os.environ.get("DIGEST_CHAT_IDS", "").split(",") if c.strip()
]

CAIRO_TZ    = ZoneInfo("Africa/Cairo")
DIGEST_HOUR   = int(os.environ.get("DIGEST_HOUR",   "9"))
DIGEST_MINUTE = int(os.environ.get("DIGEST_MINUTE", "0"))
TOP_N         = int(os.environ.get("TOP_N", "8"))

RSS_FEEDS = [
    "https://feeds.feedburner.com/TheHackersNews",
    "https://www.bleepingcomputer.com/feed/",
    "https://krebsonsecurity.com/feed/",
    "https://www.darkreading.com/rss.xml",
    "https://www.securityweek.com/feed/",
    "https://threatpost.com/feed/",
    "https://www.schneier.com/blog/atom.xml",
]

IMPORTANCE_KEYWORDS = {
    "zero-day": 6, "zero day": 6, "0-day": 6,
    "critical": 5, "actively exploited": 6, "exploited in the wild": 6,
    "ransomware": 4, "breach": 4, "data breach": 5, "leak": 3,
    "vulnerability": 3, "rce": 5, "remote code execution": 5,
    "cve-": 3, "patch": 2, "malware": 3, "backdoor": 4,
    "supply chain": 4, "apt": 4, "nation-state": 4, "espionage": 3,
    "phishing": 2, "ddos": 2, "botnet": 3, "spyware": 3,
    "privilege escalation": 4, "authentication bypass": 4,
    "warns": 2, "urgent": 4, "emergency": 4,
}

# In-memory session: chat_id -> list[story_dict]
# Each user gets their own snapshot so /news refreshes don't stomp each other.
LATEST_DIGEST: dict[int, list] = {}


# ---------------------------------------------------------------------------
# News fetching & ranking
# ---------------------------------------------------------------------------

def _clean_html(raw: str) -> str:
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _entry_datetime(entry) -> datetime:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)


def _score_story(title: str, summary: str, published: datetime) -> float:
    text = f"{title} {summary}".lower()
    score = sum(w for kw, w in IMPORTANCE_KEYWORDS.items() if kw in text)
    age_h = max(0.0, (datetime.now(timezone.utc) - published).total_seconds() / 3600)
    return score + max(0.0, 5.0 - age_h / 12.0)


def fetch_top_stories(limit: int = TOP_N) -> list[dict]:
    stories, seen = [], set()
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            source = feed.feed.get("title", url)
            for entry in feed.entries[:15]:
                title = _clean_html(entry.get("title", "")).strip()
                if not title:
                    continue
                key = title.lower()[:80]
                if key in seen:
                    continue
                seen.add(key)
                summary   = _clean_html(entry.get("summary", "") or entry.get("description", ""))
                link      = entry.get("link", "")
                published = _entry_datetime(entry)
                stories.append({
                    "title": title, "summary": summary, "link": link,
                    "source": source, "published": published,
                    "score": _score_story(title, summary, published),
                })
        except Exception as e:
            logger.warning("Failed to parse feed %s: %s", url, e)
    stories.sort(key=lambda s: s["score"], reverse=True)
    return stories[:limit]


# ---------------------------------------------------------------------------
# Message & keyboard builders
# ---------------------------------------------------------------------------

def _digest_text(stories: list[dict]) -> str:
    today = datetime.now(CAIRO_TZ).strftime("%A, %d %B %Y")
    imp   = max(1, len(stories) // 2)
    return (
        f"🛡️ <b>Cybersecurity Daily — {today}</b>\n\n"
        f"🔴 = Important  ·  🔵 = Other\n\n"
        f"Tap any story to read the full summary. 👇\n\n"
        + "\n".join(
            f"{'🔴' if i < imp else '🔵'} <b>{i+1}.</b> {html.escape(s['title'])}"
            f"\n   <i>{html.escape(s['source'])}</i>"
            for i, s in enumerate(stories)
        )
    )


def _digest_keyboard(stories: list[dict]) -> InlineKeyboardMarkup:
    imp  = max(1, len(stories) // 2)
    rows = []
    for i, s in enumerate(stories):
        icon  = "🔴" if i < imp else "🔵"
        label = f"{icon} {i+1}. {s['title'][:52]}{'…' if len(s['title']) > 52 else ''}"
        rows.append([InlineKeyboardButton(label, callback_data=f"s|{i}")])
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="rf")])
    return InlineKeyboardMarkup(rows)


def _summary_text(story: dict) -> str:
    summary = story["summary"] or "No summary available."
    if len(summary) > 3000:
        summary = summary[:3000].rsplit(" ", 1)[0] + "…"
    parts = [
        f"🛡️ <b>{html.escape(story['title'])}</b>",
        f"<i>Source: {html.escape(story['source'])}</i>",
        "",
        html.escape(summary),
    ]
    if story.get("link"):
        parts += ["", f'🔗 <a href="{html.escape(story["link"])}">Read full article</a>']
    return "\n".join(parts)


def _summary_keyboard(idx: int, total: int) -> InlineKeyboardMarkup:
    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"s|{idx - 1}"))
    nav.append(InlineKeyboardButton("🔃 All", callback_data="b"))
    if idx < total - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"s|{idx + 1}"))
    return InlineKeyboardMarkup([nav])


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 <b>Welcome to your Cybersecurity News Bot!</b>\n\n"
        f"I send the most important cybersecurity stories every day at "
        f"<b>{DIGEST_HOUR:02d}:{DIGEST_MINUTE:02d}</b> Egypt time.\n\n"
        "Commands:\n"
        "• /news — get today's top stories right now\n"
        "• /subscribe — receive the daily digest automatically\n"
        "• /unsubscribe — stop the daily digest\n"
        "• /help — show this message\n\n"
        f"💡 Your chat ID is <code>{update.effective_chat.id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    wait_msg = await update.message.reply_text("⏳ Fetching the latest cybersecurity news…")
    stories  = fetch_top_stories()
    await wait_msg.delete()
    if not stories:
        await update.message.reply_text("⚠️ Couldn't fetch news right now. Try again later.")
        return
    LATEST_DIGEST[chat_id] = stories
    await update.message.reply_text(
        _digest_text(stories),
        parse_mode=ParseMode.HTML,
        reply_markup=_digest_keyboard(stories),
        disable_web_page_preview=True,
    )


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subs = context.application.bot_data.setdefault("subscribers", set())
    subs.add(str(update.effective_chat.id))
    await update.message.reply_text(
        f"✅ Subscribed! You'll get the daily digest at "
        f"{DIGEST_HOUR:02d}:{DIGEST_MINUTE:02d} Egypt time.\n"
        "Use /unsubscribe to stop."
    )


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subs = context.application.bot_data.setdefault("subscribers", set())
    subs.discard(str(update.effective_chat.id))
    await update.message.reply_text("🛑 Unsubscribed from the daily digest.")


# ---------------------------------------------------------------------------
# Button (callback) handler — this replaces the old handle_text
# ---------------------------------------------------------------------------

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data    = query.data

    # ── Refresh ─────────────────────────────────────────────────────────────
    if data == "rf":
        await query.edit_message_text("⏳ Refreshing…", parse_mode=ParseMode.HTML)
        stories = fetch_top_stories()
        if not stories:
            await query.edit_message_text("⚠️ Couldn't fetch news. Try /news later.")
            return
        LATEST_DIGEST[chat_id] = stories
        await query.edit_message_text(
            _digest_text(stories),
            parse_mode=ParseMode.HTML,
            reply_markup=_digest_keyboard(stories),
            disable_web_page_preview=True,
        )
        return

    # ── Back to digest list ──────────────────────────────────────────────────
    if data == "b":
        stories = LATEST_DIGEST.get(chat_id)
        if not stories:
            await query.edit_message_text(
                "⏳ Session expired — fetching fresh stories…",
                parse_mode=ParseMode.HTML,
            )
            stories = fetch_top_stories()
            if not stories:
                await query.edit_message_text("⚠️ Couldn't fetch news. Try /news.")
                return
            LATEST_DIGEST[chat_id] = stories
        await query.edit_message_text(
            _digest_text(stories),
            parse_mode=ParseMode.HTML,
            reply_markup=_digest_keyboard(stories),
            disable_web_page_preview=True,
        )
        return

    # ── Open story summary ───────────────────────────────────────────────────
    if data.startswith("s|"):
        stories = LATEST_DIGEST.get(chat_id)
        if not stories:
            await query.edit_message_text("Session expired. Please send /news again.")
            return
        try:
            idx = int(data.split("|", 1)[1])
        except (ValueError, IndexError):
            return
        if not 0 <= idx < len(stories):
            await query.answer("Story not found.", show_alert=True)
            return
        await query.edit_message_text(
            _summary_text(stories[idx]),
            parse_mode=ParseMode.HTML,
            reply_markup=_summary_keyboard(idx, len(stories)),
            disable_web_page_preview=False,
        )


# ---------------------------------------------------------------------------
# Daily digest job
# ---------------------------------------------------------------------------

async def send_daily_digest(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Running daily digest job…")
    stories = fetch_top_stories()
    if not stories:
        logger.warning("No stories fetched for daily digest.")
        return

    subs = set(DIGEST_CHAT_IDS) | context.application.bot_data.get("subscribers", set())
    for chat_id in subs:
        try:
            cid = int(chat_id)
            LATEST_DIGEST[cid] = stories
            await context.bot.send_message(
                chat_id=cid,
                text=_digest_text(stories),
                parse_mode=ParseMode.HTML,
                reply_markup=_digest_keyboard(stories),
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning("Failed to send digest to %s: %s", chat_id, e)


# ---------------------------------------------------------------------------
# App bootstrap
# ---------------------------------------------------------------------------

async def _post_init(application: Application) -> None:
    """Delete any stale webhook so polling doesn't conflict with a previous deploy."""
    await application.bot.delete_webhook(drop_pending_updates=True)
    logger.info("Webhook cleared — polling mode active.")


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("news",        cmd_news))
    app.add_handler(CommandHandler("subscribe",   cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CallbackQueryHandler(on_button))  # replaces MessageHandler / handle_text

    app.job_queue.run_daily(
        send_daily_digest,
        time=datetime.now(CAIRO_TZ)
        .replace(hour=DIGEST_HOUR, minute=DIGEST_MINUTE, second=0, microsecond=0)
        .timetz(),
        name="daily_digest",
    )

    logger.info("Bot started. Daily digest at %02d:%02d Africa/Cairo.",
                DIGEST_HOUR, DIGEST_MINUTE)
    # drop_pending_updates discards messages that piled up while the bot was offline
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
