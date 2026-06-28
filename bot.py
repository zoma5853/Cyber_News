"""
Cybersecurity Daily News Telegram Bot
--------------------------------------
- Pulls top cybersecurity stories from free RSS feeds (no API keys / no cost).
- Sends a ranked daily digest at 09:00 Africa/Cairo time.
- Reply with a topic number (or a keyword) to get the full summary of that story.

Built with python-telegram-bot v21 (async) + APScheduler.
"""

import os
import re
import html
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import feedparser

# Optional: load a local .env file if python-dotenv is installed (handy for
# local development). On Render you set env vars in the dashboard instead.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("cyber-news-bot")

# Read from environment variables (set these on Render / locally)
BOT_TOKEN = os.environ["BOT_TOKEN"]                 # from @BotFather
# Comma-separated chat IDs that receive the daily digest automatically.
DIGEST_CHAT_IDS = [
    c.strip() for c in os.environ.get("DIGEST_CHAT_IDS", "").split(",") if c.strip()
]

CAIRO_TZ = ZoneInfo("Africa/Cairo")
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "9"))   # 9 AM Egypt
DIGEST_MINUTE = int(os.environ.get("DIGEST_MINUTE", "0"))
TOP_N = int(os.environ.get("TOP_N", "8"))               # how many topics to show

# Free, high-quality cybersecurity RSS feeds (no API keys needed).
RSS_FEEDS = [
    "https://feeds.feedburner.com/TheHackersNews",
    "https://www.bleepingcomputer.com/feed/",
    "https://krebsonsecurity.com/feed/",
    "https://www.darkreading.com/rss.xml",
    "https://www.securityweek.com/feed/",
    "https://threatpost.com/feed/",
    "https://www.schneier.com/blog/atom.xml",
]

# Keywords that boost a story's "importance" score.
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

# In-memory cache of the latest digest so users can request summaries.
# Maps chat_id -> list of story dicts (so each user gets their own context).
LATEST_DIGEST = {}


# --------------------------------------------------------------------------- #
# Health server (keeps Render Web Service alive — never times out)
# --------------------------------------------------------------------------- #

class _HealthHandler(BaseHTTPRequestHandler):
    """Tiny HTTP handler that always returns 200 OK."""
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass  # silence access logs


def _run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    logger.info("Health server listening on port %d", port)
    server.serve_forever()


# --------------------------------------------------------------------------- #
# News fetching & ranking
# --------------------------------------------------------------------------- #

def _clean_html(raw: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _entry_datetime(entry) -> datetime:
    """Best-effort published datetime for an entry (UTC)."""
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)


def _score_story(title: str, summary: str, published: datetime) -> float:
    """Score a story by importance keywords + recency."""
    text = f"{title} {summary}".lower()
    score = 0.0
    for kw, weight in IMPORTANCE_KEYWORDS.items():
        if kw in text:
            score += weight

    # Recency boost: newer stories rank higher (decay over ~48h).
    age_hours = (datetime.now(timezone.utc) - published).total_seconds() / 3600.0
    if age_hours < 0:
        age_hours = 0
    recency = max(0.0, 5.0 - (age_hours / 12.0))  # up to +5 for very fresh
    score += recency
    return score


def fetch_top_stories(limit: int = TOP_N):
    """Fetch, dedupe, rank, and return the top cybersecurity stories."""
    stories = []
    seen_titles = set()

    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            source = feed.feed.get("title", url)
            for entry in feed.entries[:15]:
                title = _clean_html(entry.get("title", "")).strip()
                if not title:
                    continue
                key = title.lower()[:80]
                if key in seen_titles:
                    continue
                seen_titles.add(key)

                summary = _clean_html(
                    entry.get("summary", "") or entry.get("description", "")
                )
                link = entry.get("link", "")
                published = _entry_datetime(entry)
                score = _score_story(title, summary, published)

                stories.append(
                    {
                        "title": title,
                        "summary": summary,
                        "link": link,
                        "source": source,
                        "published": published,
                        "score": score,
                    }
                )
        except Exception as e:
            logger.warning("Failed to parse feed %s: %s", url, e)

    stories.sort(key=lambda s: s["score"], reverse=True)
    return stories[:limit]


# --------------------------------------------------------------------------- #
# Message formatting
# --------------------------------------------------------------------------- #

def build_digest_message(stories) -> str:
    """Build the daily digest message (HTML formatted)."""
    today = datetime.now(CAIRO_TZ).strftime("%A, %d %B %Y")
    lines = [
        f"🛡️ <b>Cybersecurity Daily — {today}</b>",
        "",
        "<b>📌 Important</b>",
    ]

    # Split into "important" (top half) and "other".
    important_count = max(1, len(stories) // 2)
    for i, s in enumerate(stories[:important_count], start=1):
        lines.append(f"<b>{i}.</b> {html.escape(s['title'])}")
        lines.append(f"   <i>{html.escape(s['source'])}</i>")

    if len(stories) > important_count:
        lines.append("")
        lines.append("<b>📰 Other topics</b>")
        for i, s in enumerate(stories[important_count:], start=important_count + 1):
            lines.append(f"<b>{i}.</b> {html.escape(s['title'])}")
            lines.append(f"   <i>{html.escape(s['source'])}</i>")

    lines.append("")
    lines.append(
        "💬 Reply with a <b>number</b> (e.g. <code>3</code>) or a keyword "
        "to get the full summary of that story."
    )
    return "\n".join(lines)


def build_summary_message(story) -> str:
    """Build a full-summary message for a single story (HTML formatted)."""
    summary = story["summary"] or "No summary available for this story."
    # Telegram message limit is 4096 chars; keep summaries reasonable.
    if len(summary) > 3000:
        summary = summary[:3000].rsplit(" ", 1)[0] + "…"

    parts = [
        f"🛡️ <b>{html.escape(story['title'])}</b>",
        f"<i>Source: {html.escape(story['source'])}</i>",
        "",
        html.escape(summary),
    ]
    if story.get("link"):
        parts.append("")
        parts.append(f"🔗 <a href=\"{html.escape(story['link'])}\">Read full article</a>")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Telegram handlers
# --------------------------------------------------------------------------- #

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = (
        "👋 <b>Welcome to your Cybersecurity News Bot!</b>\n\n"
        "I'll send you the most important cybersecurity news every day at "
        f"<b>{DIGEST_HOUR:02d}:{DIGEST_MINUTE:02d}</b> Egypt time.\n\n"
        "Commands:\n"
        "• /news — get today's top stories now\n"
        "• /subscribe — receive the daily digest automatically\n"
        "• /unsubscribe — stop the daily digest\n"
        "• /help — show this message\n\n"
        f"💡 Your chat ID is <code>{chat_id}</code>\n"
        "After a digest, reply with a number or keyword to get the full summary."
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("⏳ Fetching the latest cybersecurity news…")
    stories = fetch_top_stories()
    if not stories:
        await update.message.reply_text("⚠️ Couldn't fetch news right now. Try again later.")
        return
    LATEST_DIGEST[chat_id] = stories
    await update.message.reply_text(
        build_digest_message(stories),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    subs = context.application.bot_data.setdefault("subscribers", set())
    subs.add(chat_id)
    await update.message.reply_text(
        "✅ Subscribed! You'll get the daily digest at "
        f"{DIGEST_HOUR:02d}:{DIGEST_MINUTE:02d} Egypt time."
    )


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    subs = context.application.bot_data.setdefault("subscribers", set())
    subs.discard(chat_id)
    await update.message.reply_text("🛑 Unsubscribed from the daily digest.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User replied with a number or keyword -> send the full summary."""
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    stories = LATEST_DIGEST.get(chat_id)

    if not stories:
        await update.message.reply_text(
            "ℹ️ I don't have a recent digest for you yet. Send /news first."
        )
        return

    # Match by number.
    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(stories):
            await update.message.reply_text(
                build_summary_message(stories[idx]),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
        else:
            await update.message.reply_text(
                f"⚠️ Please pick a number between 1 and {len(stories)}."
            )
        return

    # Match by keyword (best match in titles).
    q = text.lower()
    matches = [s for s in stories if q in s["title"].lower()]
    if matches:
        await update.message.reply_text(
            build_summary_message(matches[0]),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
    else:
        await update.message.reply_text(
            "🔍 No matching topic in today's digest. Reply with a number "
            "from the list, or send /news for a fresh digest."
        )


# --------------------------------------------------------------------------- #
# Daily digest job
# --------------------------------------------------------------------------- #

async def send_daily_digest(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Running daily digest job…")
    stories = fetch_top_stories()
    if not stories:
        logger.warning("No stories fetched for daily digest.")
        return

    # Recipients = env-configured IDs + runtime subscribers.
    subs = set(DIGEST_CHAT_IDS)
    subs |= context.application.bot_data.get("subscribers", set())

    message = build_digest_message(stories)
    for chat_id in subs:
        try:
            LATEST_DIGEST[int(chat_id)] = stories
            await context.bot.send_message(
                chat_id=int(chat_id),
                text=message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning("Failed to send digest to %s: %s", chat_id, e)


# --------------------------------------------------------------------------- #
# App bootstrap
# --------------------------------------------------------------------------- #

def main():
    # Start the health server in a background thread so Render
    # sees an open port and never marks the service as timed out.
    threading.Thread(target=_run_health_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Schedule the daily digest at 09:00 Africa/Cairo.
    app.job_queue.run_daily(
        send_daily_digest,
        time=datetime.now(CAIRO_TZ)
        .replace(hour=DIGEST_HOUR, minute=DIGEST_MINUTE, second=0, microsecond=0)
        .timetz(),
        name="daily_digest",
    )

    logger.info("Bot started. Daily digest at %02d:%02d Africa/Cairo.",
                DIGEST_HOUR, DIGEST_MINUTE)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
