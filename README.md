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
| `/sweeps` (or `/setups`) | List active Daily Sweep setups (pair, direction, level, expiry in WAT) |
| `/sweepcheck [PAIR]` | Show the last two completed daily candles as the bot built them, and whether they form a sweep setup |
| `/sweepscan` | Admin only: run the Daily Sweep scan now |
| `/help` | Full command list |

### Alert Types
- `SL` — fires when price drops to level
- `TP` — fires when price rises to level
- `TARGET` — fires when price crosses through the level (real-time tick feed)

### Daily Sweep Alerts
Requires `TWELVE_DATA_API_KEY` (server-level) in `.env` — disabled otherwise. Opt in with
`/sweepalerts on` (approved users only).

**Pattern** (daily timeframe, evaluated only after Day 2 has fully closed; the margins below apply on top of
these comparisons — see *Margins and settings*):

- **SELL** — Day 1 closes bullish, Day 2 closes bearish, Day 2's high > Day 1's high (sweep) and
  Day 2's low > Day 1's low (no low sweep). Level = Day 2's open. During Day 3, alert when price
  trades up to the level.
- **BUY** — the mirror: Day 1 bearish, Day 2 bullish, Day 2's low < Day 1's low and Day 2's high <
  Day 1's high. Alert when Day 3 price trades down to Day 2's open.

If the level isn't reached by the time Day 3 closes, the setup expires silently. Each setup fires
at most once.

### Margins and settings
Small feed differences between Twelve Data and FOREX.com shouldn't create or break a setup, so the
patterns carry minimum margins. A pip is 0.0001 (0.01 for JPY pairs, 0.1 for XAUUSD). Each setting is
a default constant in `app/services/daily_sweep_service.py` with a per-pair override dict:

| Setting | Default | Meaning |
|---|---|---|
| `MIN_BEYOND_PIPS` (`PAIR_MIN_BEYOND_PIPS`) | 3 | A sweep's Day 2 **wick** must go at least this far beyond Day 1's high (SELL) / low (BUY). |
| `OPPOSITE_SIDE_BUFFER_PIPS` (`PAIR_OPPOSITE_SIDE_BUFFER_PIPS`) | 2 | Day 2 only counts as *not having taken* Day 1's opposite extreme if it stayed at least this many pips away from it (SELL: Day 2 low ≥ Day 1 low + buffer; BUY: Day 2 high ≤ Day 1 high − buffer). Twelve Data's extremes can sit a fraction of a pip inside FOREX.com's, so a near-touch counts as a touch. Exactly the buffer counts as clear; anything closer is taken. |
| `MIN_BODY_PIPS` (`PAIR_MIN_BODY_PIPS`) | 2 | A day is only bullish/bearish if `|close − open|` is at least this. Anything smaller is a doji and matches neither direction. |

`/sweepcheck` lists every failed check with the numbers, e.g.
`✗ needs Day 2 to sweep Day 1's low by ≥3 pips — but its low went only 2.8 pips below Day 1's low (0.70101 vs 0.70129)`,
`✗ needs Day 2 to stay ≥2 pips below Day 1's high — but it came within 0.2 pips (0.70403 vs 0.70405)`
and `✗ Day 2 is a doji (body 1.1 pips < 2)`. It also shows `open = previous close` per candle (both
values when the first 1H bar's open differs by more than 2 pips).

**Daily candles.** A trading day runs 17:00 → 17:00 `America/New_York` (matching FOREX.com on
TradingView), built by aggregating Twelve Data 1H bars — Twelve Data's own `1day` candles use
midnight UTC and don't match. US daylight saving is handled by `zoneinfo` (hence the `tzdata`
requirement). A day's **open is the previous trading day's close** (as on FOREX.com, where Monday opens
exactly at Friday's close); a 1H feed's first bar can open well away from that, Monday most of all.
High, low and close come from the 1H bars. If the previous day is missing or incomplete (or is the
oldest day in the fetched data) the open falls back to the first 1H bar's open, and the scan logs it.
Sunday evening's session belongs to Monday. Twelve Data keeps quoting through the weekend (those
bars have real ranges and the price can drift well away from Friday's close), so the bars between
Friday 17:00 and Sunday 17:00 New York time are dropped by time alone; the day before Monday is Friday. A day is only used if its final bar (16:00–17:00 NY) is present and it has at least 22 bars
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

In particular, Friday's close can differ from FOREX.com's by a few pips on some pairs (for example
EURNZD on 23 Oct 2026: 8.7 pips), and because a day's open is the previous close, Monday's open
inherits that difference. High, low and close can also be a few pips off (typically 0.2–3 pips).

---

## Upgrading

The app creates missing tables on startup (so `daily_sweep_setups` appears automatically) and adds
the `users.daily_sweep_alerts_enabled` column if it's missing. Nothing is ever dropped at runtime.
After upgrading from a version with the 4H zone feature, run this once to clean up the old schema:

```sql
DROP TABLE IF EXISTS retracement_zones;
ALTER TABLE users DROP COLUMN IF EXISTS zone_alerts_enabled;
```

**After deploying the version that retired the two-level alert feature**, run this once on production,
in this order (the app no longer reads these rows or columns, and never drops anything itself). Run it
right after the deploy: until step 1 has run, that feature's leftover active rows would be read as
ordinary sweep setups. Re-running it fails at step 1 because the column is already gone, which is harmless.

```sql
-- 1. Delete the retired feature's setups
DELETE FROM daily_sweep_setups WHERE kind = 'CONTINUATION';

-- 2. Drop the retired columns
ALTER TABLE daily_sweep_setups DROP COLUMN IF EXISTS kind;
ALTER TABLE daily_sweep_setups DROP COLUMN IF EXISTS level2;
ALTER TABLE daily_sweep_setups DROP COLUMN IF EXISTS stage;
ALTER TABLE users DROP COLUMN IF EXISTS daily_continuation_alerts_enabled;
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
