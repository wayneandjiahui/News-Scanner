"""
Investment News Scanner — Prototype
Polls RSS feeds, scores stories with Groq (free), sends Telegram alerts.
"""

import os
import time
import hashlib
import logging
import json
import feedparser
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config (set via env vars or edit directly) ──────────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "YOUR_GROQ_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_IDS = [207117315, 253163267]  # Wayne, Partner
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL", "60"))
MIN_SCORE_TO_ALERT = int(os.getenv("MIN_SCORE", "50"))  # 50=HIGH, 75=CRITICAL only

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama3-8b-8192"  # fast, free, more than capable for scoring

# ── RSS Feeds ────────────────────────────────────────────────────────────────
RSS_FEEDS = [
    # Benzinga (free RSS)
    {"name": "Benzinga", "url": "https://www.benzinga.com/feed"},
    {"name": "Benzinga Biotech", "url": "https://www.benzinga.com/topic/biotech/feed"},
    {"name": "Benzinga M&A", "url": "https://www.benzinga.com/topic/m-a/feed"},
    # Reuters
    {"name": "Reuters Business", "url": "https://feeds.reuters.com/reuters/businessNews"},
    {"name": "Reuters Tech", "url": "https://feeds.reuters.com/reuters/technologyNews"},
    # Yahoo Finance
    {"name": "Yahoo Finance", "url": "https://finance.yahoo.com/news/rssindex"},
    # Seeking Alpha
    {"name": "Seeking Alpha", "url": "https://seekingalpha.com/market_currents.xml"},
    # MarketWatch
    {"name": "MarketWatch", "url": "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines"},
]

# ── Scoring keywords ─────────────────────────────────────────────────────────
SCORING_PROMPT = """You are a financial news scoring engine for a day trader focused on high-impact catalysts.

Analyse this news headline and summary. Return ONLY a valid JSON object, no markdown, no explanation.

News:
HEADLINE: {headline}
SUMMARY: {summary}
SOURCE: {source}
PUBLISHED: {published}

Score this news item using these rules:

CATALYST TYPE (0-40 points):
- FDA approved / NDA approved / BLA approved / marketing authorization = 40
- Phase 3 primary endpoint met / pivotal trial success = 38
- Merger agreement / buyout / acquisition announced = 38
- Major tech product launch / world's first / revolutionary = 38
- Chip export controls / CHIPS Act / supply shortage = 38
- OPEC surprise cut / geopolitical supply disruption = 38
- Emergency rate cut / surprise rate hike = 38
- QE announced / QT pause / currency intervention = 38
- Major defence contract / Pentagon deal = 38
- Blowout earnings / record revenue / crushed estimates / raised guidance = 30
- Bond yield spike / yield curve inversion = 30
- Corporate tax cut / capital gains tax change = 30
- Short squeeze setup / high short interest = 28
- Phase 2 success / positive clinical data = 22
- AI breakthrough / LLM announcement / benchmark = 20
- Data centre mega deal / hyperscaler contract = 20
- Analyst upgrade / price target raise = 10
- General positive partnership / contract win = 5
- FDA rejection / CRL issued / trial failure = -15
- Dilution / public offering / share issuance = -20
- Data breach / cyberattack / ransomware = -15
- Antitrust / DOJ investigation / EU fine = -20
- Fraud / SEC investigation / class action = -25
- Ceasefire / peace deal (bearish for defence) = -15

COMPANY PROFILE (0-35 points):
- Clinical-stage biopharma / single catalyst company = 35
- Small-cap biotech / therapeutics / oncology = 28
- Small/mid-cap in hot sector (AI, chips, EV, defence) = 20
- Mid-cap general = 10
- Large-cap (AAPL, MSFT scale) = 5

NEWS URGENCY (0-25 points):
- Breaking / just announced / halted-news = 25
- Pre-market (4am-9:30am ET) or after-hours (4pm-8pm ET) = 20
- Primary source (PR Newswire, Globe Newswire, BusinessWire) = 15
- General news wire = 10
- Rumoured / reportedly / unconfirmed = -10

SECTOR CASCADE — does this news reprice an entire sector? (true/false)
- Rate cut/hike, QE/QT, tax policy, OPEC, major war escalation = true

DIRECTION:
- bullish, bearish, or neutral

TICKER EXTRACTION:
- Extract stock ticker symbol if mentioned or strongly implied. If none, return null.
- For well-known companies not mentioned by ticker, infer it (e.g. "Apple" = "AAPL")

SCORE BANDS:
- 75-100 = CRITICAL
- 50-74 = HIGH  
- 30-49 = MEDIUM
- 10-29 = LOW
- <10 = DISCARD

Return this exact JSON structure:
{{
  "catalyst_score": <int 0-40>,
  "profile_score": <int 0-35>,
  "urgency_score": <int 0-25>,
  "total_score": <int>,
  "band": "CRITICAL" | "HIGH" | "MEDIUM" | "LOW" | "DISCARD",
  "direction": "bullish" | "bearish" | "neutral",
  "sector_cascade": <bool>,
  "ticker": "<string or null>",
  "catalyst_type": "<short label e.g. FDA Approval, Earnings Beat, M&A>",
  "affected_sectors": ["<sector1>", "<sector2>"],
  "one_line_reason": "<why this scored this way in max 15 words>"
}}"""


# ── State ────────────────────────────────────────────────────────────────────
seen_stories: set[str] = set()


def story_id(entry) -> str:
    """Stable unique ID for a feed entry."""
    key = (entry.get("link") or entry.get("id") or entry.get("title", ""))
    return hashlib.md5(key.encode()).hexdigest()


def is_prepost_market() -> bool:
    """True if current ET time is pre-market (4–9:30) or after-hours (16–20)."""
    now = datetime.now(ZoneInfo("America/New_York"))
    h = now.hour + now.minute / 60
    return (4 <= h < 9.5) or (16 <= h < 20)


def fetch_feed(feed: dict) -> list[dict]:
    """Parse one RSS feed, return list of story dicts."""
    try:
        parsed = feedparser.parse(feed["url"])
        stories = []
        for entry in parsed.entries[:15]:  # latest 15 per feed
            sid = story_id(entry)
            if sid in seen_stories:
                continue
            stories.append({
                "id": sid,
                "source": feed["name"],
                "title": entry.get("title", ""),
                "summary": entry.get("summary", entry.get("description", ""))[:500],
                "link": entry.get("link", ""),
                "published": entry.get("published", "now"),
            })
        return stories
    except Exception as e:
        log.warning(f"Feed error [{feed['name']}]: {e}")
        return []


def score_story(story: dict) -> dict | None:
    """Ask Groq to score a story. Returns scored dict or None on failure."""
    prompt = SCORING_PROMPT.format(
        headline=story["title"],
        summary=story["summary"],
        source=story["source"],
        published=story["published"],
    )
    try:
        r = requests.post(
            GROQ_API_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 400,
                "temperature": 0.1,
            },
            timeout=15,
        )
        raw = r.json()["choices"][0]["message"]["content"].strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)
        result.update(story)
        return result
    except Exception as e:
        log.warning(f"Scoring error for [{story['title'][:60]}]: {e}")
        return None


def format_telegram_message(s: dict) -> str:
    """Format a scored story into a Telegram message."""
    band = s.get("band", "")
    direction = s.get("direction", "neutral")
    ticker = s.get("ticker")
    cascade = s.get("sector_cascade", False)

    # emoji indicators
    band_icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡"}.get(band, "⚪")
    dir_icon = {"bullish": "📈", "bearish": "📉", "neutral": "➡️"}.get(direction, "➡️")
    cascade_line = "\n⚡ <b>SECTOR CASCADE</b> — reprices entire sector" if cascade else ""
    ticker_line = f"\n🏷 <b>Ticker:</b> <code>{ticker}</code>" if ticker else ""
    prepost = "\n🌙 <b>Pre/Post market</b>" if is_prepost_market() else ""

    sectors = ", ".join(s.get("affected_sectors", []))
    sector_line = f"\n📂 <b>Sectors:</b> {sectors}" if sectors else ""

    msg = (
        f"{band_icon} <b>{band} ALERT</b> {dir_icon}{prepost}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{s['title']}</b>\n\n"
        f"📌 <b>Catalyst:</b> {s.get('catalyst_type', 'N/A')}\n"
        f"💡 <b>Why:</b> {s.get('one_line_reason', '')}"
        f"{ticker_line}"
        f"{sector_line}"
        f"{cascade_line}\n\n"
        f"📊 Score: <b>{s.get('total_score', 0)}/100</b> "
        f"(Cat:{s.get('catalyst_score',0)} + "
        f"Profile:{s.get('profile_score',0)} + "
        f"Urgency:{s.get('urgency_score',0)})\n"
        f"🔗 <a href=\"{s.get('link', '')}\">Read full story</a>\n"
        f"📡 Source: {s.get('source', '')}"
    )
    return msg


def send_telegram(message: str) -> bool:
    """Send a message to all recipients via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    success = False
    for chat_id in TELEGRAM_CHAT_IDS:
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.json().get("ok"):
                success = True
        except Exception as e:
            log.warning(f"Telegram send error to {chat_id}: {e}")
    return success


def run_scan():
    """One full scan cycle across all feeds."""
    log.info("── Scan cycle starting ──")
    new_stories = []

    for feed in RSS_FEEDS:
        stories = fetch_feed(feed)
        if stories:
            log.info(f"  {feed['name']}: {len(stories)} new stories")
        new_stories.extend(stories)

    if not new_stories:
        log.info("  No new stories this cycle.")
        return

    log.info(f"  Scoring {len(new_stories)} stories...")
    alerted = 0

    for story in new_stories:
        seen_stories.add(story["id"])  # mark seen before scoring to avoid dupes
        scored = score_story(story)
        if not scored:
            continue

        band = scored.get("band", "DISCARD")
        total = scored.get("total_score", 0)

        log.info(
            f"  [{band:8s}] {total:3d}/100  {story['title'][:70]}"
            + (f"  [{scored.get('ticker')}]" if scored.get("ticker") else "")
        )

        if total >= MIN_SCORE_TO_ALERT and band not in ("LOW", "DISCARD"):
            msg = format_telegram_message(scored)
            ok = send_telegram(msg)
            if ok:
                alerted += 1
                log.info(f"    ✅ Telegram sent")
            else:
                log.warning(f"    ❌ Telegram failed")

        time.sleep(0.5)  # gentle rate limiting between Claude calls

    log.info(f"── Scan complete. {alerted} alerts sent. ──\n")


def main():
    log.info("=" * 50)
    log.info("Investment News Scanner — starting up")
    log.info(f"Poll interval : {POLL_INTERVAL_SECONDS}s")
    log.info(f"Alert threshold: score >= {MIN_SCORE_TO_ALERT}")
    log.info(f"Feeds          : {len(RSS_FEEDS)}")
    log.info("=" * 50)

    # startup test
    ok = send_telegram(
        "🚀 <b>Investment News Scanner online</b>\n"
        f"Monitoring {len(RSS_FEEDS)} feeds · Alert threshold: {MIN_SCORE_TO_ALERT}/100"
    )
    if ok:
        log.info("Telegram test message sent ✅")
    else:
        log.warning("Telegram test failed — check your token and chat ID")

    while True:
        try:
            run_scan()
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break
        except Exception as e:
            log.error(f"Scan error: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
