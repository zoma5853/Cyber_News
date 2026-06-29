"""
Cybersecurity Daily News Telegram Bot (Refactored Interactive UI)
-------------------------------------------------------------------
- Pulls top cybersecurity stories from free RSS feeds.
- Sends a ranked daily digest at 09:00 Africa/Cairo time.
- Implements an inline keyboard UI mirroring SPA navigation paradigms.
- Replaces command-line text replies with tap-based interaction.
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

# --------------------------------------------------------------------------- #
# System Configuration and Logging
# --------------------------------------------------------------------------- #

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("cyber-news-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
DIGEST_CHAT_IDS = [
    c.strip() for c in os.environ.get("DIGEST_CHAT_IDS", "").split(",") if c.strip()
]

CAIRO_TZ = ZoneInfo("Africa/Cairo")
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "9"))
DIGEST_MINUTE = int(os.environ.get("DIGEST_MINUTE", "0"))
TOP_N = int(os.environ.get("TOP_N", "8"))

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
    "warns": 2, "urgent": 4, "emergency": 4,
}

# Application Memory: chat_id -> list of story dictionaries
LATEST_DIGEST = {}

# --------------------------------------------------------------------------- #
# Cloud Deployment Health Server
# --------------------------------------------------------------------------- #
class _HealthHandler(BaseHTTPRequestHandler):
    """Provides a 200 OK endpoint to prevent cloud provider timeouts."""
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args): pass

def _run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    logger.info("Health server listening on port %d", port)
    server.serve_forever()

# --------------------------------------------------------------------------- #
# Data Ingestion and Heuristic Ranking
# --------------------------------------------------------------------------- #
def _clean_html(raw: str) -> str:
    """Strips unauthorized HTML tags and normalizes whitespace."""
    if not raw: return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()

def _entry_datetime(entry) -> datetime:
    """Best-effort extraction of UTC publication times."""
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try: return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception: pass
    return datetime.now(timezone.utc)

def _score_story(title: str, summary: str, published: datetime) -> float:
    """Assigns an importance score based on threat keywords and recency."""
    text = f"{title} {summary}".lower()
    score = sum(weight for kw, weight in IMPORTANCE_KEYWORDS.items() if kw in text)
    age_hours = max(0, (datetime.now(timezone.utc) - published).total_seconds() / 3600.0)
    score += max(0.0, 5.0 - (age_hours / 12.0))
    return score

def fetch_top_stories(limit: int = TOP_N):
    """Fetches, deduplicates, and ranks cybersecurity news."""
    stories, seen_titles = [], set()
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            source = feed.feed.get("title", url)
            for entry in feed.entries[:15]:
                title = _clean_html(entry.get("title", "")).strip()
                if not title or title.lower()[:80] in seen_titles: continue
                seen_titles.add(title.lower()[:80])
                
                summary = _clean_html(entry.get("summary", "") or entry.get("description", ""))
                stories.append({
                    "title": title, "summary": summary, "link": entry.get("link", ""),
                    "source": source, "score": _score_story(title, summary, _entry_datetime(entry))
                })
        except Exception as e:
            logger.warning("Failed to parse feed %s: %s", url, e)
    
    stories.sort(key=lambda s: s["score"], reverse=True)
    return stories[:limit]

# --------------------------------------------------------------------------- #
# UI Layout Rendering Engines
# --------------------------------------------------------------------------- #
def build_digest_ui(stories):
    """Constructs the root menu interface with numbered navigational buttons."""
    today = datetime.now(CAIRO_TZ).strftime("%A, %d %B %Y")
    lines = [f"🛡️ <b>Cybersecurity Daily — {today}</b>", ""]
    buttons = []
    
    # Escape user-generated strings to prevent BadRequest parsing errors
    for i, s in enumerate(stories, start=1):
        lines.append(f"<b>{i}.</b> {html.escape(s['title'])}")
        lines.append(f"   <i>{html.escape(s['source'])}</i>\n")
        
        # Construct grid layout: 4 buttons per row max
        if len(buttons) == 0 or len(buttons[-1]) == 4:
            buttons.append([])
        
        # Callback payload is tightly packed: identifier | action | index
        buttons[-1].append(InlineKeyboardButton(str(i), callback_data=f"c|read|{i-1}"))

    buttons.append([InlineKeyboardButton("🔄 Refresh News", callback_data="c|refresh|0")])
    lines.append("Tap a number below to read the full summary.")
    
    return "\n".join(lines), InlineKeyboardMarkup(buttons)

def build_summary_ui(story):
    """Constructs the detail view pane for a specific article."""
    summary = story["summary"] or "No summary available for this story."
    if len(summary) > 3000:
        summary = summary[:3000].rsplit(" ", 1)[0] + "…"

    text = (
        f"🛡️ <b>{html.escape(story['title'])}</b>\n"
        f"<i>Source: {html.escape(story['source'])}</i>\n\n"
        f"{html.escape(summary)}"
    )
    
    kb = [[InlineKeyboardButton("⬅️ Back", callback_data="c|digest|0")]]
    if story.get("link"):
        kb.insert(0, [InlineKeyboardButton("🔗 Read full article", url=story["link"])])
        
    return text, InlineKeyboardMarkup(kb)

# --------------------------------------------------------------------------- #
# Telegram Bot API Handlers
# --------------------------------------------------------------------------- #
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 <b>Welcome to your Cybersecurity News Bot!</b>\n\n"
        f"Daily digests arrive at <b>{DIGEST_HOUR:02d}:{DIGEST_MINUTE:02d}</b> (Cairo Time).\n"
        "Use /news to fetch headlines immediately."
    )
    await update.message.reply_html(msg)

async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text("⏳ Fetching the latest cybersecurity news…")
    stories = fetch_top_stories()
    
    if not stories:
        await msg.edit_text("⚠️ Couldn't fetch news right now. Try again later.")
        return
        
    LATEST_DIGEST[chat_id] = stories
    text, markup = build_digest_ui(stories)
    await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Centralized routing logic for inline button presses."""
    query = update.callback_query
    await query.answer() # Immediately halt the client-side loading animation
    
    parts = query.data.split("|")
    if len(parts) != 3 or parts[0] != "c": return
    
    action, payload = parts[1], int(parts[2])
    chat_id = query.message.chat.id
    stories = LATEST_DIGEST.get(chat_id)

    if not stories and action != "refresh":
        await query.edit_message_text("ℹ️ Session expired. Please run /news again.")
        return

    try:
        if action == "digest":
            text, markup = build_digest_ui(stories)
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True)
            
        elif action == "read":
            if 0 <= payload < len(stories):
                text, markup = build_summary_ui(stories[payload])
                await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True)
                
        elif action == "refresh":
            await query.edit_message_text("⏳ Refreshing news...")
            new_stories = fetch_top_stories()
            LATEST_DIGEST[chat_id] = new_stories
            text, markup = build_digest_ui(new_stories)
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True)
            
    except Exception as e:
        logger.error(f"Error handling callback: {e}")

# --------------------------------------------------------------------------- #
# Application Bootstrap
# --------------------------------------------------------------------------- #
def main():
    threading.Thread(target=_run_health_server, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler(["start", "help", "subscribe", "unsubscribe"], cmd_start))
    app.add_handler(CommandHandler("news", cmd_news))
    
    # Isolate callbacks intended only for the cybersecurity module
    app.add_handler(CallbackQueryHandler(handle_callback, pattern="^c\\|"))
    
    logger.info("Cybersecurity Daily News Bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
