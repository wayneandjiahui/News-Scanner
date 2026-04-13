"""
Investment News Scanner
- 38 RSS feeds across all major sectors
- Two-stage filter: keyword pre-filter + Groq AI scoring
- Duplicate story detection via fuzzy matching
- Ticker extraction + market cap segmentation via yfinance
- Telegram alerts to multiple recipients
"""

import os
import time
import hashlib
import logging
import json
import feedparser
import requests
import yfinance as yf
from rapidfuzz import fuzz
from datetime import datetime
from zoneinfo import ZoneInfo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
GROQ_API_KEY       = os.getenv("GROQ_API_KEY", "YOUR_GROQ_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_IDS  = [207117315, 253163267]  # Wayne, Partner
POLL_INTERVAL      = int(os.getenv("POLL_INTERVAL", "60"))
MIN_SCORE          = int(os.getenv("MIN_SCORE", "60"))
DUPE_THRESHOLD     = 70  # fuzzy similarity % to flag as duplicate

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
# Fallback list — if a model is decommissioned, auto-switches to next
GROQ_MODELS = [
    "llama-3.1-8b-instant",       # fast, high rate limits
    "llama-3.2-3b-preview",       # lightweight fallback
    "llama-3.3-70b-versatile",    # slower but smarter fallback
    "mixtral-8x7b-32768",         # last resort
]
GROQ_MODEL = GROQ_MODELS[0]  # start with first

# ── RSS Feeds (38 total) ──────────────────────────────────────────────────────
RSS_FEEDS = [
    # Benzinga
    {"name": "Benzinga",              "url": "https://www.benzinga.com/feed"},
    {"name": "Benzinga Biotech",      "url": "https://www.benzinga.com/topic/biotech/feed"},
    {"name": "Benzinga M&A",          "url": "https://www.benzinga.com/topic/m-a/feed"},
    {"name": "Benzinga FDA",          "url": "https://www.benzinga.com/topic/fda/feed"},
    {"name": "Benzinga Earnings",     "url": "https://www.benzinga.com/topic/earnings/feed"},
    # Reuters
    {"name": "Reuters Business",      "url": "https://feeds.reuters.com/reuters/businessNews"},
    {"name": "Reuters Tech",          "url": "https://feeds.reuters.com/reuters/technologyNews"},
    {"name": "Reuters Health",        "url": "https://feeds.reuters.com/reuters/healthNews"},
    # Yahoo / Seeking Alpha / MarketWatch
    {"name": "Yahoo Finance",         "url": "https://finance.yahoo.com/news/rssindex"},
    {"name": "Seeking Alpha",         "url": "https://seekingalpha.com/market_currents.xml"},
    {"name": "MarketWatch",           "url": "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines"},
    # PR Newswire / GlobeNewswire
    {"name": "PR Newswire",           "url": "https://www.prnewswire.com/rss/news-releases-list.rss"},
    {"name": "GlobeNewswire Biotech", "url": "https://www.globenewswire.com/RssFeed/subjectcode/15-Biotechnology"},
    {"name": "GlobeNewswire Finance", "url": "https://www.globenewswire.com/RssFeed/subjectcode/6-Financial%20Services"},
    # Biotech / Pharma
    {"name": "BioPharma Dive",        "url": "https://www.biopharmadive.com/feeds/news/"},
    {"name": "STAT News",             "url": "https://www.statnews.com/feed/"},
    {"name": "FiercePharma",          "url": "https://www.fiercepharma.com/rss/xml"},
    {"name": "FierceBiotech",         "url": "https://www.fiercebiotech.com/rss/xml"},
    # Tech / AI / Semiconductors
    {"name": "TechCrunch",            "url": "https://techcrunch.com/feed/"},
    {"name": "VentureBeat",           "url": "https://venturebeat.com/feed/"},
    {"name": "Ars Technica",          "url": "https://feeds.arstechnica.com/arstechnica/index"},
    {"name": "The Verge",             "url": "https://www.theverge.com/rss/index.xml"},
    {"name": "Semiconductor Eng",     "url": "https://semiengineering.com/feed/"},
    # Energy / Oil / Commodities
    {"name": "OilPrice.com",          "url": "https://oilprice.com/rss/main"},
    {"name": "Mining.com",            "url": "https://www.mining.com/feed/"},
    {"name": "Energy Monitor",        "url": "https://www.energymonitor.ai/feed/"},
    # Defence / Geopolitical
    {"name": "Defence News",          "url": "https://www.defensenews.com/arc/outboundfeeds/rss/"},
    {"name": "Breaking Defence",      "url": "https://breakingdefense.com/feed/"},
    # Fintech / Payments / Crypto
    {"name": "Finextra",              "url": "https://www.finextra.com/rss/headlines.aspx"},
    {"name": "Payments Dive",         "url": "https://www.paymentsdive.com/feeds/news/"},
    {"name": "The Block",             "url": "https://www.theblock.co/rss.xml"},
    # Macro / Fed / Rates
    {"name": "Federal Reserve",       "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
    {"name": "Econoday",              "url": "https://rss.econoday.com/byweek.rss"},
    # General Markets
    {"name": "IBD",                   "url": "https://www.investors.com/feed/"},
    {"name": "CNBC Markets",          "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"},
    {"name": "Bloomberg Markets",     "url": "https://feeds.bloomberg.com/markets/news.rss"},
    {"name": "Financial Times",       "url": "https://www.ft.com/rss/home"},
    {"name": "Barron's",              "url": "https://www.barrons.com/xml/rss/3_7531.xml"},
]

# ── Stage 1 keyword filter ────────────────────────────────────────────────────
KEYWORDS = [
    # Biopharma
    "fda approved","fda approval","nda approved","bla approved","marketing authorization",
    "phase 3","phase iii","primary endpoint","pivotal trial","clinical trial",
    "phase 2","phase ii","proof of concept","fast track","breakthrough therapy",
    "priority review","pdufa","complete response letter","clinical hold",
    # Pharma pre-announcements (WATCH alerts)
    "to announce","to present data","to host conference call","to report data",
    "interim analysis","futility analysis","data readout","trial results expected",
    "pdufa date","fda decision expected","fda review","advisory committee",
    "adcom","data to be released","topline data","interim results",
    # M&A
    "to acquire","merger agreement","buyout","takeover","acquisition","definitive agreement",
    "strategic alternatives","exploring sale","received approach",
    # Earnings
    "earnings","beat estimates","missed estimates","raised guidance","lowered guidance",
    "record revenue","blowout","crushed estimates","eps beat","eps miss",
    # Tech / AI / Chips
    "launched","unveiled","world's first","first ever","breakthrough",
    "large language model","llm","artificial intelligence","ai model",
    "chip shortage","export controls","chips act","supply shortage",
    "data centre","data center","hyperscaler","gpu","semiconductor",
    # Energy
    "opec","production cut","supply disruption","oil discovery","reserve upgrade",
    "pipeline attack","force majeure","lng contract",
    # Defence / Geopolitical / War
    "dod contract","pentagon deal","nato","defence contract","defense contract",
    "weapons package","sanctions","embargo","invasion","escalation","ceasefire",
    "war talks","peace talks","peace deal","settlement talks","failed negotiations",
    "nuclear deal","nuclear talks","nuclear threat","military strike","airstrike",
    "missile strike","troops deployed","war declared","conflict escalation",
    "regime change","coup","terror attack","geopolitical risk","geopolitical tension",
    # Iran / Middle East specific
    "iran","tehran","israel","gaza","hezbollah","hamas","houthi",
    "strait of hormuz","red sea","middle east","persian gulf",
    "nuclear program","uranium enrichment","iaea","jcpoa",
    "trump iran","vance iran","iran deal","iran sanctions","iran talks",
    "oil embargo","oil sanctions","iran nuclear",
    # Russia / Ukraine / China / Taiwan
    "russia","ukraine","putin","zelensky","kremlin",
    "taiwan","beijing","xi jinping","south china sea",
    "north korea","kim jong","missile test","icbm",
    # Broader geopolitical
    "trade war","trade dispute","diplomatic crisis","expelled ambassador",
    "military escalation","no-fly zone","blockade","siege",
    "war premium","risk off","flight to safety","safe haven",
    "gold surges","oil surges","market panic","black swan",
    # Macro
    "rate cut","rate hike","federal reserve","fomc","quantitative easing",
    "interest rate","inflation","cpi","pce","jobs report","yield curve",
    "powell","ecb","boj","pboc","bank of england","rba","snb",
    "recession","stagflation","soft landing","hard landing",
    "treasury yield","10-year yield","bond selloff","credit spreads",
    "dollar index","dxy","currency crisis","debt ceiling",
    "gdp","unemployment","nonfarm payroll","consumer confidence",
    # Tax / Policy
    "corporate tax","capital gains","tax reform","tariff","windfall tax",
    "import duty","export ban","trade sanctions","trade restriction",
    "global minimum tax","oecd","pillar two","repatriation",
    # Interest rates / monetary policy
    "rate decision","basis points","bps cut","bps hike",
    "dot plot","terminal rate","forward guidance","pivot",
    "quantitative tightening","balance sheet","repo rate",
    "emergency meeting","inter-meeting","unscheduled meeting",
    # Fintech / Crypto
    "banking licence","stablecoin","crypto approved","etf approved",
    "money laundering","fraud","cease and desist",
    "cbdc","digital dollar","sec crypto","bitcoin etf","spot etf",
    # Short squeeze
    "short squeeze","short interest","days to cover","most shorted",
    "gamma squeeze","options expiry","max pain",
    # Negative catalysts
    "sec investigation","class action","restatement",
    "data breach","cyberattack","ransomware","antitrust","doj investigation",
    "dilution","public offering","share issuance","at-the-money","atm offering",
    "going concern","bankruptcy","chapter 11","default","debt restructuring",
    "profit warning","guidance cut","missed revenue","earnings miss",
]

# ── Noise blacklist — stories containing ANY of these are discarded immediately ──
NOISE_BLACKLIST = [
    # Analyst ratings / price targets — opinions not events
    "initiated coverage","initiates coverage","starts coverage",
    "reiterates buy","reiterates hold","reiterates sell",
    "maintains buy","maintains hold","maintains sell",
    "maintains overweight","maintains underweight","maintains outperform",
    "buy rating","sell rating","hold rating","neutral rating",
    # Opinion / listicle content
    "should you buy","should you sell","is it a buy","is it a sell",
    "reasons to buy","reasons to sell",
    "top 5 stocks","top 10 stocks","best stocks to buy",
    "dividend stocks to buy","passive income stocks",
    # Crypto price predictions only
    "price prediction","2025 prediction","2026 prediction","2030 prediction",
    "altcoin prediction","token prediction","coin prediction",
    # Earnings previews only — actual results still pass
    "earnings preview","earnings calendar","ahead of earnings","before earnings report",
    # Generic Fed commentary — not actual decisions
    "fed considering","fed exploring","fed officials say","fed member says",
    "cross border payments","payment rails","faster payments",
    "treasury warns banks","treasury reminds","occ reminds","fdic reminds",
    # Generic war situation updates — not market-moving escalations
    "troops advance","forces advance","village captured","town captured",
    "shelling continues","frontline report","battlefield update",
    # General roundups / noise
    "weekly roundup","monthly roundup","week in review","month in review",
    "top stories this week","morning note","evening note","daily note",
    # Opinion / analysis framing — not actual events
    "is a riskier","is far riskier","is a risky bet","riskier bet",
    "analysis:","opinion:","commentary:","perspective:","explainer:",
    "why this matters","what this means","here is what","here's what",
    "could signal","might signal","may signal","what to make of",
    "a closer look","deep dive","breaking down","unpacking",
    "is it really","are we heading","is this the end","what happens if",
    "the real reason","the truth about","everything to know",
    "playbook","far riskier","poses more complicated",
]

# ── Groq scoring prompt ───────────────────────────────────────────────────────
SCORING_PROMPT = """You are a financial news scoring engine for a day trader focused on high-impact catalysts.

Analyse this news headline and summary. Return ONLY a valid JSON object, no markdown, no explanation.

News:
HEADLINE: {headline}
SUMMARY: {summary}
SOURCE: {source}
PUBLISHED: {published}

Score this news item:

CATALYST TYPE (0-40 points):
- FDA approved / NDA approved / BLA approved / marketing authorization = 40
- Phase 3 primary endpoint met / pivotal trial success = 38
- Merger agreement / buyout / acquisition announced = 38
- Major tech product launch / world first / revolutionary = 38
- Chip export controls / CHIPS Act / supply shortage = 38
- OPEC surprise cut / geopolitical supply disruption = 38
- Emergency rate cut / surprise rate hike = 38
- QE announced / QT pause / currency intervention = 38
- Major defence contract / Pentagon deal = 38
- War escalation / military strike / invasion = 38
- Nuclear threat / nuclear deal collapse / JCPOA breakdown = 38
- Iran sanctions / oil embargo / Strait of Hormuz threat = 38
- Failed peace talks / war settlement collapsed / negotiations breakdown = 35
- US-China trade war escalation / Taiwan conflict = 35
- Russia-Ukraine major escalation = 35
- Terror attack on major infrastructure = 35
- Coup / regime change in oil-producing nation = 33
- Blowout earnings / record revenue / crushed estimates / raised guidance = 30
- Bond yield spike / yield curve inversion = 30
- Corporate tax cut / capital gains tax change = 30
- Recession signal / GDP contraction / stagflation = 30
- Profit warning / guidance cut / earnings miss = 28
- Short squeeze setup / high short interest = 28
- Bankruptcy / chapter 11 / debt default / going concern = 28
- Diplomatic crisis / ambassador expelled = 25
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
- Ceasefire announced / peace deal signed (bearish defence, bullish markets) = 20

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

SECTOR CASCADE: true if rate cut/hike, QE/QT, tax policy, OPEC, major war escalation. Else false.
DIRECTION: bullish, bearish, or neutral
TICKER: extract ticker if mentioned or implied. Null if none.

SCORE BANDS: 75-100=CRITICAL, 50-74=HIGH, 30-49=MEDIUM, 10-29=LOW, <10=DISCARD

Return ONLY this JSON:
{{
  "catalyst_score": <int>,
  "profile_score": <int>,
  "urgency_score": <int>,
  "total_score": <int>,
  "band": "CRITICAL|HIGH|MEDIUM|LOW|DISCARD",
  "direction": "bullish|bearish|neutral",
  "sector_cascade": <bool>,
  "ticker": "<string or null>",
  "catalyst_type": "<short label>",
  "affected_sectors": ["<sector>"],
  "one_line_reason": "<max 15 words>"
}}"""

# ── State ─────────────────────────────────────────────────────────────────────
seen_ids: set[str] = set()
recent_headlines: list[str] = []
alerted_headlines: list[str] = []      # for cross-cycle fuzzy dedup
alerted_topics: dict[str, float] = {}  # topic -> timestamp, blocks same topic for 2hrs

TOPIC_BLOCK_SECONDS = 7200  # 2 hours — same topic won't alert twice

# Core topic keywords — if two stories share a topic key, they're the same story
TOPIC_KEYS = [
    # Geopolitical
    "iran","israel","russia","ukraine","china taiwan","north korea","houthi",
    "strait of hormuz","red sea","middle east","gaza","hezbollah",
    # Macro
    "powell","fomc","fed rate","rate cut","rate hike","federal reserve decision",
    "ecb rate","boj rate","inflation cpi","jobs report","nonfarm",
    # Specific company events — use ticker if available
    # (handled separately via ticker dedup below)
]

def get_topic_key(title: str, ticker: str | None) -> str | None:
    """Extract a dedup topic key from a headline."""
    t = title.lower()
    # if we have a ticker, use ticker + catalyst type as key
    if ticker:
        for catalyst in ["fda","merger","acquisition","earnings","bankruptcy",
                         "phase 3","phase 2","short squeeze","offering","sec investigation"]:
            if catalyst in t:
                return f"{ticker.upper()}:{catalyst}"
        return f"{ticker.upper()}:general"
    # otherwise match on topic keywords
    for key in TOPIC_KEYS:
        if key in t:
            return key
    return None


def story_id(entry) -> str:
    key = entry.get("link") or entry.get("id") or entry.get("title", "")
    return hashlib.md5(key.encode()).hexdigest()


def is_prepost_market() -> bool:
    now = datetime.now(ZoneInfo("America/New_York"))
    h = now.hour + now.minute / 60
    return (4 <= h < 9.5) or (16 <= h < 20)


def passes_keyword_filter(title: str, summary: str) -> bool:
    text = (title + " " + summary).lower()
    # reject if any blacklisted phrase found
    if any(noise in text for noise in NOISE_BLACKLIST):
        return False
    return any(kw in text for kw in KEYWORDS)


def is_duplicate(headline: str) -> bool:
    for seen in recent_headlines[-200:]:
        if fuzz.token_sort_ratio(headline.lower(), seen.lower()) >= DUPE_THRESHOLD:
            return True
    return False


def get_market_cap_label(ticker: str) -> str:
    if not ticker:
        return ""
    try:
        cap = yf.Ticker(ticker).info.get("marketCap", 0)
        if not cap:
            return ""
        if cap < 300_000_000:
            return "🔬 Micro cap"
        elif cap < 2_000_000_000:
            return "🐣 Small cap"
        elif cap < 10_000_000_000:
            return "🏢 Mid cap"
        elif cap < 200_000_000_000:
            return "🏦 Large cap"
        else:
            return "🐋 Mega cap"
    except Exception:
        return ""


# ── Pharma WATCH alert detection ─────────────────────────────────────────────
# Keywords that signal a scheduled pharma event within ~1 day
WATCH_KEYWORDS = [
    "tomorrow","today","monday","tuesday","wednesday","thursday","friday",
    "this morning","this afternoon","this evening","tonight",
    "april","may","june","july","august","september","october","november","december",
    "pdufa","adcom","advisory committee","fda decision","fda review",
    "interim analysis","futility analysis","topline data","data readout",
    "phase 2","phase 3","phase ii","phase iii","pivotal trial",
    "conference call","webcast","investor call",
]

WATCH_PHARMA_KEYWORDS = [
    "therapeutics","biosciences","biopharma","oncology","pharmaceutical",
    "biotech","biologics","medicines","drug","trial","clinical",
    "fda","nda","bla","inda","pdufa","adcom",
]

TIME_INDICATORS_1DAY = [
    "today","tomorrow","tonight","this morning","this afternoon",
    "monday","tuesday","wednesday","thursday","friday","saturday","sunday",
    "at 8:","at 9:","at 10:","at 11:","at 12:","at 1:","at 2:","at 3:",
    "a.m. et","p.m. et","am et","pm et","eastern time",
]

alerted_watch: set[str] = set()  # prevent duplicate WATCH alerts

def is_pharma_watch_story(title: str, summary: str) -> bool:
    """Detect if this is a pharma pre-announcement with event within ~1 day."""
    text = (title + " " + summary).lower()
    # must be pharma related
    if not any(kw in text for kw in WATCH_PHARMA_KEYWORDS):
        return False
    # must have a time indicator suggesting event is imminent (today/tomorrow)
    if not any(kw in text for kw in TIME_INDICATORS_1DAY):
        return False
    # must mention a catalyst type
    catalyst_signals = [
        "data","results","readout","analysis","decision","approval",
        "conference call","webcast","present","announce","report"
    ]
    if not any(kw in text for kw in catalyst_signals):
        return False
    return True


def format_watch_message(story: dict, ticker: str | None, cap_label: str) -> str:
    """Format a WATCH alert for upcoming pharma catalyst."""
    tv_line = ""
    if ticker:
        tv_url = f"https://www.tradingview.com/symbols/{ticker}/"
        tv_line = (
            f"\n🏷 <b>Ticker:</b> <code>{ticker}</code>"
            + (f"  <b>{cap_label}</b>" if cap_label else "")
            + f'  <a href="{tv_url}">📊 TradingView</a>'
        )
    return (
        f"👀 <b>WATCH ALERT — Pharma Catalyst Incoming</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{story['title']}</b>\n\n"
        f"⏰ <b>Event imminent</b> — within 24 hours"
        f"{tv_line}\n\n"
        f"🔗 <a href=\"{story.get('link', '')}\">[Read full story →</a>\n"
        f"📡 Source: {story.get('source', '')}"
    )


def fetch_feed(feed: dict) -> list[dict]:
    try:
        parsed = feedparser.parse(feed["url"])
        stories = []
        for entry in parsed.entries[:5]:  # max 5 per feed to avoid rate limit burst
            sid     = story_id(entry)
            title   = entry.get("title", "")
            summary = entry.get("summary", entry.get("description", ""))[:500]

            if sid in seen_ids:
                continue
            if not passes_keyword_filter(title, summary):
                continue
            if is_duplicate(title):
                log.info(f"  [DUPE BLOCKED] {title[:70]}")
                seen_ids.add(sid)
                continue

            stories.append({
                "id": sid,
                "source": feed["name"],
                "title": title,
                "summary": summary,
                "link": entry.get("link", ""),
                "published": entry.get("published", "now"),
            })
        return stories
    except Exception as e:
        log.warning(f"Feed error [{feed['name']}]: {e}")
        return []


# track Groq call timestamps for rate limiting
_groq_call_times: list[float] = []

def score_story(story: dict) -> dict | None:
    global _groq_call_times
    prompt = SCORING_PROMPT.format(
        headline=story["title"],
        summary=story["summary"],
        source=story["source"],
        published=story["published"],
    )
    # rate limit: max 25 calls per 60s window
    now = time.time()
    _groq_call_times = [t for t in _groq_call_times if now - t < 60]
    if len(_groq_call_times) >= 25:
        wait = 60 - (now - _groq_call_times[0])
        log.info(f"  Rate limit pause: {wait:.1f}s")
        time.sleep(max(wait, 1))
        _groq_call_times = []

    global GROQ_MODEL
    for attempt in range(3):  # retry up to 3 times
        try:
            r = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                json={"model": GROQ_MODEL, "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 400, "temperature": 0.1},
                timeout=15,
            )
            data = r.json()
            if "choices" not in data:
                err = data.get("error", {}).get("message", str(data))
                # auto-switch if model decommissioned
                if "decommissioned" in err.lower() or "does not exist" in err.lower():
                    current_idx = GROQ_MODELS.index(GROQ_MODEL) if GROQ_MODEL in GROQ_MODELS else 0
                    next_idx = current_idx + 1
                    if next_idx < len(GROQ_MODELS):
                        old = GROQ_MODEL
                        GROQ_MODEL = GROQ_MODELS[next_idx]
                        log.warning(f"  Model '{old}' decommissioned — switching to '{GROQ_MODEL}'")
                        attempt -= 1  # retry with new model
                        continue
                    else:
                        log.error("  All Groq models exhausted — update GROQ_MODELS list")
                        return None
                if "rate" in err.lower() and attempt < 2:
                    log.info(f"  Groq rate limit hit, waiting 10s... (attempt {attempt+1})")
                    time.sleep(10)
                    continue
                log.warning(f"  Groq error: {err[:80]}")
                return None
            _groq_call_times.append(time.time())
            raw = data["choices"][0]["message"]["content"].strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            result = json.loads(raw)
            result.update(story)
            return result
        except Exception as e:
            log.warning(f"Scoring error [{story['title'][:60]}]: {e}")
            if attempt < 2:
                time.sleep(5)
    return None


def format_message(s: dict, cap_label: str) -> str:
    band      = s.get("band", "")
    direction = s.get("direction", "neutral")
    ticker    = s.get("ticker")
    cascade   = s.get("sector_cascade", False)

    band_icon    = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡"}.get(band, "⚪")
    dir_icon     = {"bullish": "📈", "bearish": "📉", "neutral": "➡️"}.get(direction, "➡️")
    cascade_line = "\n⚡ <b>SECTOR CASCADE</b> — reprices entire sector" if cascade else ""
    prepost      = "\n🌙 <b>Pre/Post market</b>" if is_prepost_market() else ""

    # Ticker + market cap + TradingView link
    if ticker:
        tv_url = f"https://www.tradingview.com/symbols/{ticker}/"
        ticker_line = (
            f"\n🏷 <b>Ticker:</b> <code>{ticker}</code>"
            + (f"  <b>{cap_label}</b>" if cap_label else "")
            + f"  <a href=\"{tv_url}\">📊 TradingView</a>"
        )
    else:
        ticker_line = ""

    return (
        f"{band_icon} <b>{band} ALERT</b> {dir_icon}{prepost}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{s['title']}</b>\n\n"
        f"📌 <b>Catalyst:</b> {s.get('catalyst_type', 'N/A')}\n"

        f"{ticker_line}"
        f"{cascade_line}\n\n"
        f"📊 Score: <b>{s.get('total_score', 0)}/100</b> "
        f"(Cat:{s.get('catalyst_score', 0)} + "
        f"Profile:{s.get('profile_score', 0)} + "
        f"Urgency:{s.get('urgency_score', 0)})\n"
        f"🔗 <a href=\"{s.get('link', '')}\">[Read full story →</a>\n"
        f"📡 Source: {s.get('source', '')}"
    )


def get_og_image(url: str) -> str | None:
    """Try to extract Open Graph image from article URL for preview."""
    try:
        r = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
        from html.parser import HTMLParser
        class OGParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.image = None
            def handle_starttag(self, tag, attrs):
                if tag == "meta":
                    d = dict(attrs)
                    if d.get("property") == "og:image" and d.get("content"):
                        self.image = d["content"]
        p = OGParser()
        p.feed(r.text[:10000])
        return p.image
    except Exception:
        return None


def send_telegram(message: str, image_url: str | None = None) -> bool:
    success = False
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            if image_url:
                # send as photo with caption
                r = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                    json={"chat_id": chat_id, "photo": image_url,
                          "caption": message, "parse_mode": "HTML"},
                    timeout=10,
                )
                if r.json().get("ok"):
                    success = True
                    continue
            # fallback to text if no image or photo send failed
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": message,
                      "parse_mode": "HTML", "disable_web_page_preview": False},
                timeout=10,
            )
            if r.json().get("ok"):
                success = True
        except Exception as e:
            log.warning(f"Telegram error to {chat_id}: {e}")
    return success


def check_watch_alerts(new_stories: list[dict]):
    """Check for pharma pre-announcements with event within ~1 day."""
    import re
    watch_sent = 0
    for story in new_stories:
        title   = story.get("title", "")
        summary = story.get("summary", "")
        if not is_pharma_watch_story(title, summary):
            continue
        watch_key = hashlib.md5(title.encode()).hexdigest()[:12]
        if watch_key in alerted_watch:
            continue
        alerted_watch.add(watch_key)
        ticker = None
        m = re.search(r'\((?:NASDAQ|NYSE|AMEX):\s*([A-Z]{1,5})\)', title + " " + summary)
        if m:
            ticker = m.group(1)
        cap_label = get_market_cap_label(ticker) if ticker else ""
        msg = format_watch_message(story, ticker, cap_label)
        image_url = get_og_image(story.get("link", ""))
        ok = send_telegram(msg, image_url)
        if ok:
            watch_sent += 1
            log.info(f"  👀 WATCH sent — {ticker or 'no ticker'} — {title[:60]}")
    if watch_sent:
        log.info(f"  {watch_sent} WATCH alerts sent.")


def run_scan():
    log.info("── Scan cycle starting ──")
    new_stories = []

    for feed in RSS_FEEDS:
        stories = fetch_feed(feed)
        if stories:
            log.info(f"  {feed['name']}: {len(stories)} passed filter")
        new_stories.extend(stories)

    if not new_stories:
        log.info("  Nothing new passed filter this cycle.")
        return

    # run WATCH check before scoring — no Groq needed
    check_watch_alerts(new_stories)

    log.info(f"  Scoring {len(new_stories)} stories with Groq...")
    alerted = 0

    for story in new_stories:
        seen_ids.add(story["id"])
        recent_headlines.append(story["title"])

        scored = score_story(story)
        if not scored:
            continue

        band   = scored.get("band", "DISCARD")
        total  = scored.get("total_score", 0)
        ticker = scored.get("ticker")

        log.info(
            f"  [{band:8s}] {total:3d}/100  {story['title'][:70]}"
            + (f"  [{ticker}]" if ticker else "")
        )

        # hard gates — must pass all before alerting
        if total < 60:
            log.info(f"  [BELOW-60] {total}/100 — skipped")
            continue
        if band in ("LOW", "DISCARD"):
            log.info(f"  [BAND-SKIP] {band} — skipped")
            continue
        # ticker required for MEDIUM — HIGH/CRITICAL can be sector-wide signals
        if not ticker and band == "MEDIUM":
            log.info(f"  [NO-TICKER] MEDIUM with no ticker — skipped")
            continue

        if True:  # gates passed
            # 1. fuzzy headline dedup — catch near-identical wording
            if any(fuzz.token_sort_ratio(story["title"].lower(), h.lower()) >= DUPE_THRESHOLD
                   for h in alerted_headlines[-300:]):
                log.info(f"    [FUZZY-DUPE] Skipping near-identical headline")
                continue

            # 2. topic dedup — block same topic for 2 hours
            topic_key = get_topic_key(story["title"], ticker)
            if topic_key:
                now = time.time()
                expired = [k for k, t in alerted_topics.items() if now - t > TOPIC_BLOCK_SECONDS]
                for k in expired:
                    del alerted_topics[k]
                if topic_key in alerted_topics:
                    age_mins = int((now - alerted_topics[topic_key]) / 60)
                    log.info(f"    [TOPIC-DUPE] '{topic_key}' already alerted {age_mins}min ago, skipping")
                    continue
                alerted_topics[topic_key] = now

            alerted_headlines.append(story["title"])
            cap_label = get_market_cap_label(ticker) if ticker else ""
            msg = format_message(scored, cap_label)
            image_url = get_og_image(story.get("link", ""))
            ok  = send_telegram(msg, image_url)
            if ok:
                alerted += 1
                log.info(f"    ✅ Sent — {ticker or 'no ticker'} {cap_label}")
            else:
                log.warning("    ❌ Telegram failed")

    log.info(f"── Done. {alerted} alerts sent. ──\n")


def main():
    log.info("=" * 50)
    log.info("Investment News Scanner — starting up")
    log.info(f"Feeds          : {len(RSS_FEEDS)}")
    log.info(f"Poll interval  : {POLL_INTERVAL}s")
    log.info(f"Alert threshold: {MIN_SCORE}/100")
    log.info(f"Dupe threshold : {DUPE_THRESHOLD}% similarity")
    log.info("=" * 50)

    ok = send_telegram(
        f"🚀 <b>Investment News Scanner online</b>\n"
        f"📡 {len(RSS_FEEDS)} feeds · "
        f"⚡ {POLL_INTERVAL}s interval · "
        f"🎯 Threshold: {MIN_SCORE}/100"
    )
    log.info("Telegram ✅" if ok else "Telegram ❌ — check token/chat ID")

    while True:
        try:
            run_scan()
        except KeyboardInterrupt:
            log.info("Stopped.")
            break
        except Exception as e:
            log.error(f"Scan error: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
