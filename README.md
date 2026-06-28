# 🛡️ Cybersecurity Daily News Telegram Bot

A free Telegram bot that sends you the **most important cybersecurity news every day at 9:00 AM Egypt time**, and lets you reply with a topic number/keyword to get the **full summary** of any story.

- ✅ 100% free news sources (RSS — no API keys, no cost)
- ✅ Smart ranking (zero-days, breaches, RCE, ransomware, recency, etc.)
- ✅ Runs 24/7 on Render's free tier (no need to keep your PC on)
- ✅ Daily digest split into **📌 Important** and **📰 Other topics**
- ✅ Reply with a number or keyword → full summary + link

---

## 1. Create your bot on Telegram (2 minutes)

1. Open Telegram and search for **@BotFather**.
2. Send `/newbot`, pick a name and a username (must end in `bot`).
3. BotFather gives you a **token** like `123456789:ABCdef...`. Copy it — this is your `BOT_TOKEN`.

---

## 2. Run it locally (to test)

```bash
# inside the cyber-news-bot folder
pip install -r requirements.txt

# copy the env template and fill in your token
cp .env.example .env
#   -> edit .env and paste your BOT_TOKEN

python bot.py
```

Then in Telegram:
- Send `/start` to your bot → it replies with your **chat ID**.
- Send `/news` → you get today's ranked digest immediately.
- Reply with a number (e.g. `3`) or keyword → full summary of that story.
- Send `/subscribe` → you'll get the digest automatically every day at 9 AM Egypt.

> Tip: paste your chat ID into `DIGEST_CHAT_IDS` in `.env` so the daily digest
> is sent to you automatically even without `/subscribe`.

---

## 3. Deploy free & 24/7 on Render

This keeps the bot running even when your computer is off.

1. Push this folder to a **GitHub repo** (public or private).
2. Go to **https://render.com** → sign up (free) → **New +** → **Blueprint**.
3. Connect your GitHub repo. Render reads `render.yaml` and creates a
   **Background Worker** automatically.
4. In the service's **Environment** tab, add:
   - `BOT_TOKEN` = your BotFather token
   - `DIGEST_CHAT_IDS` = your chat ID (from `/start`) — optional but recommended
5. Click **Deploy**. Done — your bot now runs 24/7.

> No web port is needed because the bot uses long polling (it connects out to
> Telegram). That's why it's a **Worker**, not a Web Service.

### Alternative free hosts
The same `bot.py` works on **Railway**, **Fly.io**, or any VPS — just set the
same environment variables and run `python bot.py`.

---

## 4. Commands

| Command        | What it does                                  |
|----------------|-----------------------------------------------|
| `/start`       | Welcome + shows your chat ID                  |
| `/news`        | Get today's top stories right now             |
| `/subscribe`   | Receive the daily 9 AM digest automatically   |
| `/unsubscribe` | Stop the daily digest                         |
| `/help`        | Show help                                     |
| *(any number)* | Full summary of that story from the last digest |
| *(any keyword)*| Full summary of the best-matching story       |

---

## 5. Customize

Edit the top of `bot.py`:

- **`RSS_FEEDS`** — add/remove cybersecurity sources.
- **`IMPORTANCE_KEYWORDS`** — tune what counts as "important" and how much.
- **`DIGEST_HOUR` / `DIGEST_MINUTE`** — change the delivery time (Egypt timezone).
- **`TOP_N`** — how many stories appear in the daily digest.

---

## Notes & limits

- Subscribers added via `/subscribe` are stored **in memory**. If the host
  restarts, they're cleared — so it's best to also list your chat ID in the
  `DIGEST_CHAT_IDS` environment variable for a permanent recipient.
- Summaries come from each article's RSS description. The "🔗 Read full article"
  link always points to the original source for the complete story.
- Render free workers may sleep on inactivity on some plans; a Background
  Worker generally stays up. If your digest ever misses, `/news` always works.
