# Price Alert Bot

Multi-user Forex price alert bot powered by Telegram + Twelve Data.

## Features

- 🔔 Price alerts (SL, TP, TARGET) per user
- 👤 Per-user Twelve Data API keys — isolated quotas
- 💾 PostgreSQL persistence — alerts survive restarts
- 🤖 100% Telegram-native — no web frontend needed

## Setup

### 1. Clone & install
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Create your `.env` file
```bash
cp .env.example .env
```

Fill in:
```env
TELEGRAM_BOT_TOKEN=your-bot-token-here   # from @BotFather
ADMIN_CHAT_ID=your-telegram-chat-id
DB_HOST=localhost
DB_PORT=5432
DB_USERNAME=postgres
DB_PASSWORD=yourpassword
DB_NAME=price_alert_bot
PORT=3000
NODE_ENV=development
FINNHUB_API_KEY=your-finnhub-api-key-here
```

### 3. Create a Telegram bot
1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the token into `TELEGRAM_BOT_TOKEN`

### 4. Start PostgreSQL and create the database
```sql
CREATE DATABASE price_alert_bot;
```

### 5. Run the bot
```bash
python -m app.main
```

Tables are created automatically on first run.

---

## Onboarding New Team Members

1. Share your bot link: `t.me/YourBotName`
2. They message `/start`
3. They sign up for a free Twelve Data key at [twelvedata.com](https://twelvedata.com) (800 req/day free)
4. They send `/setup their_api_key`
5. Done — fully isolated quota from everyone else

---

## Bot Commands

| Command | Description |
|---|---|
| `/start` | Welcome message + setup prompt |
| `/setup [API_KEY]` | Save Twelve Data API key |
| `/resetkey` | Remove saved API key |
| `/setalert [PAIR] [TYPE] [PRICE]` | Set a price alert |
| `/listalerts` | View active alerts |
| `/cancelalert [ID]` | Cancel by ID |
| `/cancelalerts [SYMBOL or all]` | Cancel by symbol or all |
| `/price [PAIR]` | Get live price |
| `/pairs` | List supported pairs |
| `/sweepalerts [on\|off]` | Opt in/out of Daily Sweep notifications |
| `/contalerts [on\|off]` | Opt in/out of Daily Continuation notifications (separate from sweeps) |
| `/sweeps` (or `/setups`) | List all active setups — sweeps and continuations (pair, direction, level(s), which continuation level is hit, expiry in WAT) |
| `/sweepcheck [PAIR]` | Show the last two completed daily candles as the bot built them, and whether they form a sweep or continuation setup |
| `/sweepscan` | Admin only: run the daily scan (both features) now |
| `/help` | Full command list |

### Alert Types
- `SL` — fires when price drops to level
- `TP` — fires when price rises to level
- `TARGET` — fires when price crosses through the level (real-time tick feed)

### Daily Sweep Alerts
Requires `TWELVE_DATA_API_KEY` (server-level) in `.env` — disabled otherwise. Opt in with
`/sweepalerts on` (approved users only).

**Pattern** (daily timeframe, all comparisons strict, evaluated only after Day 2 has fully closed):

- **SELL** — Day 1 closes bullish, Day 2 closes bearish, Day 2's high > Day 1's high (sweep) and
  Day 2's low > Day 1's low (no low sweep). Level = Day 2's open. During Day 3, alert when price
  trades up to the level.
- **BUY** — the mirror: Day 1 bearish, Day 2 bullish, Day 2's low < Day 1's low and Day 2's high <
  Day 1's high. Alert when Day 3 price trades down to Day 2's open.

If the level isn't reached by the time Day 3 closes, the setup expires silently. Each setup fires
at most once.

### Daily Continuation Alerts
Runs on the same candles, scan, tick feed and storage as Daily Sweep — each pair is fetched once
per scan and checked for both patterns. Opt in with `/contalerts on` (independent of
`/sweepalerts`). Day 2 must be fully closed; all comparisons are strict.

- **BUY** — Day 1 and Day 2 both close bullish, Day 2 closes above Day 1's high, Day 2's low >
  Day 1's low (it didn't take the low), and Day 2 opened below Day 1's high (no gap — see the note
  below: with open = previous close this can effectively never trigger, but the rule is kept).
  Level 1 = Day 1's high, Level 2 = Day 2's open (always below Level 1).
- **SELL** — the mirror: both bearish, Day 2 closes below Day 1's low, Day 2's high < Day 1's
  high, Day 2 opened above Day 1's low. Level 1 = Day 1's low, Level 2 = Day 2's open.

During Day 3 the levels fire in order: Level 1 first (price pulls back to the broken extreme),
then Level 2. A price already past both fires one combined message. Each stage is claimed with an
atomic database update, so it can never be sent twice. At Day 3's close the setup expires silently,
whether nothing or only Level 1 was hit. The suggested stop is Day 2's low (BUY) / high (SELL).
A Day 2 can match only one of the four patterns (the Day 1/Day 2 colour combination decides it).

Because a day's open is the previous day's close, Day 2 always opens at Day 1's close, so a gap
between Day 1 and Day 2 can no longer occur and the "no gap" skip effectively never fires (only if
Day 1 closed exactly on its own high/low, or when an open fell back to the first bar). The code is
kept as a harmless guard. `/sweepcheck` shows `open = previous close` per candle, and both values
when the first 1H bar's open differs by more than 2 pips.

**Daily candles.** A trading day runs 17:00 → 17:00 `America/New_York` (matching FOREX.com on
TradingView), built by aggregating Twelve Data 1H bars — Twelve Data's own `1day` candles use
midnight UTC and don't match. US daylight saving is handled by `zoneinfo` (hence the `tzdata`
requirement). A day's **open is the previous trading day's close** (as on FOREX.com, where Monday opens
exactly at Friday's close); a 1H feed's first bar can open well away from that, Monday most of all.
High, low and close come from the 1H bars. If the previous day is missing or incomplete (or is the
oldest day in the fetched data) the open falls back to the first 1H bar's open, and the scan logs it.
Sunday evening's session belongs to Monday; weekend/frozen bars are discarded, so
the day before Monday is Friday. A day is only used if its final bar (16:00–17:00 NY) is present and it has at least 22 bars
(`PAIR_MIN_DAILY_BARS` overrides per pair — gold has a daily 17:00–18:00 break) and its range isn't
degenerate; Day 1 and Day 2 must be consecutive trading days.

**Scan.** Every Mon–Fri at 17:10 New York time, each pair in `ALLOWED_SYMBOLS` is fetched (one
request per pair, ~8 s apart for Twelve Data's 8 req/min free tier — about 29 requests/day).
Pairs whose latest day hasn't appeared yet (or that hit a quota error) are retried once 15
minutes later. Each pair's computed Day 1 / Day 2 OHLC is logged on every scan. New setups are
sent as one digest; live Finnhub ticks then trigger level-hit alerts. Setups are stored in
`daily_sweep_setups`, so they survive restarts. If the latest closed trading day hasn't been scanned when the bot starts
(e.g. it was down at 17:10) and its Day 3 is still running, a catch-up scan runs in the background. Each scan fetches 168 1H bars per pair (Day 1's open needs
the close of the day before it).
If the current price (latest Finnhub tick if under 2 minutes old, else Twelve Data) is already at/through a new setup's level, the alert fires immediately
with a note that the level was reached before the setup was armed.

**Known limitation.** Candles come from Twelve Data, live ticks from Finnhub (OANDA), and your
chart from FOREX.com. These are three different feeds, so levels can differ by a few pips or
cents from what you see on TradingView. Use `/sweepcheck PAIR` (or the scan logs) to compare the
bot's candles against your chart.

---

## Upgrading

The app creates missing tables on startup (so `daily_sweep_setups` appears automatically) and adds
the `users.daily_sweep_alerts_enabled` column if it's missing. Nothing is ever dropped at runtime.
After upgrading from a version with the 4H zone feature, run this once to clean up the old schema:

```sql
DROP TABLE IF EXISTS retracement_zones;
ALTER TABLE users DROP COLUMN IF EXISTS zone_alerts_enabled;
```

## Tests

```bash
pip install pytest
pytest
```

`tests/test_integration_postgres.py` runs the real SQL (schema upgrade, unique constraint, staged claims,
expiry, scan state, opt-in flags) and is skipped unless `TEST_DATABASE_URL` is set. It works inside a
throwaway schema that it drops afterwards, so it is safe to point at any database:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://user:pass@127.0.0.1:5432/dbname pytest tests/test_integration_postgres.py
```

---

## Architecture

```
app/
├── services/
│   ├── users_service.py           # User registration + API key storage
│   ├── market_data_service.py     # Twelve Data price + candle fetching (per-user key, or server key for Daily Sweep)
│   ├── price_alert_service.py     # Alert CRUD + trigger logic
│   ├── invite_service.py          # Invite-code generation/redemption
│   ├── daily_sweep_service.py     # Daily Sweep: 17:00-NY daily candles, pattern scan, live level alerts
│   └── finnhub_price_service.py   # Live Finnhub WebSocket feed
├── telegram_bot.py                # All bot commands + message routing
├── webhook.py                     # FastAPI app exposing POST /webhook/<token> (production only)
├── scheduler.py                   # Friday cleanup + alert-ID reset jobs, Daily Sweep scan (17:10 NY)
├── db.py / models.py              # SQLAlchemy async models + engine
├── config.py                      # Environment variable loading
└── main.py                        # Entrypoint — polling (dev) / FastAPI+uvicorn webhook (prod)
```

Built on `python-telegram-bot` (async; owns update handling and, in dev, polling),
FastAPI + uvicorn (owns the HTTP server for the production webhook endpoint — PTB
just consumes updates handed to it, it doesn't run its own web server),
SQLAlchemy 2.0 async ORM + `asyncpg` for Postgres, `httpx` for Twelve Data REST calls,
and `websockets` for the live Finnhub feed.

---

## Deployment (Railway)

1. Push to GitHub
2. Create new Railway project → Deploy from GitHub
3. Add PostgreSQL plugin
4. Set environment variables in Railway dashboard
5. Set the start command to `python -m app.main`
6. Deploy
