# Investment News Scanner

Live news scanner for day trading catalysts. Polls 8 RSS feeds every 60 seconds,
scores each story using Claude AI, and sends Telegram alerts for HIGH/CRITICAL events.

## What it monitors
- Biopharma: FDA approvals, clinical trial results
- Tech: product launches, earnings, AI breakthroughs
- Semiconductors: export controls, supply news, chip launches
- Data Centres / AI Infrastructure: hyperscaler deals
- Real Estate / REITs: rate decisions, acquisitions
- Energy / Oil & Gas: OPEC, supply disruptions
- Defence: contracts, conflict escalation, sanctions
- Payments / Fintech: regulatory approvals, fraud
- Macro: FOMC, interest rates, monetary policy, tax policy

## Score bands
| Score | Band | Action |
|-------|------|--------|
| 75-100 | 🔴 CRITICAL | Instant Telegram alert |
| 50-74 | 🟠 HIGH | Telegram alert |
| 30-49 | 🟡 MEDIUM | Logged only (no alert) |
| <30 | ⚪ LOW/DISCARD | Ignored |

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Set environment variables
```bash
cp .env.example .env
# Edit .env with your keys
```

Or export directly:
```bash
export ANTHROPIC_API_KEY=your_key
export TELEGRAM_BOT_TOKEN=your_token
export TELEGRAM_CHAT_ID=your_chat_id
```

### 3. Get your keys

**Anthropic API key:**
- Sign up at console.anthropic.com
- Create an API key

**Telegram bot token:**
- Open Telegram, search @BotFather
- Send /newbot and follow prompts
- Copy the token

**Telegram chat ID:**
- Search @userinfobot on Telegram
- Send any message — it replies with your ID

### 4. Run locally
```bash
python main.py
```

### 5. Deploy to Railway (recommended — free tier available)
1. Push this folder to a GitHub repo
2. Go to railway.app → New Project → Deploy from GitHub
3. Add environment variables in Railway dashboard
4. Done — it runs 24/7

## Tuning

**Change alert threshold** (default: 50 = HIGH and above):
```bash
export MIN_SCORE=75   # CRITICAL only
export MIN_SCORE=30   # MEDIUM and above
```

**Change poll interval** (default: 60 seconds):
```bash
export POLL_INTERVAL=30   # every 30 seconds
```

## Cost estimate
- Claude Haiku: ~$0.001 per story scored
- ~15 stories per feed × 8 feeds = ~120 stories/scan
- At 60s intervals: ~7,200 stories/hour max (most will be cached/seen)
- In practice: ~50-100 new stories/hour = ~$0.05-0.10/hour
- Monthly: ~$36-72/month at full throttle (less in off-hours)
