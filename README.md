# Price Alert Bot

Multi-user Forex price alert bot powered by Telegram + Twelve Data.

## Features

- 🔔 Price alerts (SL, TP, TARGET) per user
- 🔀 SMT divergence alerts (GBPUSD / EURUSD and EURJPY / GBPJPY)
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
| `/divalerts [on\|off]` | Opt in/out of SMT divergence notifications (both pair groups) |
| `/divergence` (or `/smt`) | Show both groups' PDH/PDL being watched and where each side stands (waiting, leader touched + deadline, confirmed, divergence sent), times in WAT |
| `/divscan` | Admin only: record both groups' levels now (four Twelve Data requests) |
| `/help` | Full command list |

### Alert Types
- `SL` — fires when price drops to level
- `TP` — fires when price rises to level
- `TARGET` — fires when price crosses through the level (real-time tick feed)

### SMT Divergence Alerts (GBPUSD / EURUSD and EURJPY / GBPJPY)
Requires a server-level `TWELVE_DATA_API_KEY` in `.env` (without it the feature is disabled with a startup warning).
Opt in with `/divalerts on` (approved users only); it covers both pair groups.

There are two **separate** pair groups, **GBPUSD / EURUSD** and **EURJPY / GBPJPY**. A pair is only ever compared
with its partner: each group has its own levels, table, state and alerts (the JPY ones are titled
`SMT DIVERGENCE (JPY)`), `/smt` shows one section per group, and a failure in one doesn't stop the other. Both
follow the rules below. The two pairs of a group are positively correlated, so they should take out their
previous-day liquidity at about the same time. At each trading-day close (17:00 America/New_York) the previous-day high (PDH) and low (PDL) of both
pairs are recorded from daily candles the bot builds itself (see **Daily candles** below). They are
watched from that close until the **next** trading day closes (Friday's levels run through Monday's close). The
two sides are independent: a LOW case (both should trade to their PDL, price ≤ PDL) and a HIGH case (both to
their PDH, price ≥ PDH). A day can fire one, both or neither, and each side fires at most once.

```
ACTIVE ──leader touches──▶ LEADER_TOUCHED ──laggard touches in time──▶ CONFIRMED   (silent, logged)
  │  └─ other pair already past its level ─▶ CONFIRMED                └─deadline passes──▶ DIVERGENCE (one alert)
  └─ day closes, nobody touched ─▶ EXPIRED (silent)
```

- The first pair to touch its level is the **leader** (either pair can lead). That starts a 60-minute window; if the
  other pair (the **laggard**) touches its own level before the deadline the pair is in sync and nothing is sent.
  If the deadline passes first it is a **divergence**. The deadline is `min(touch + 60 min, the day's close)`.
- "Touch" is any touch or break, no buffer by default (see the settings below). Weekend quotes are ignored.
- **The alert** (one per divergence, no trade advice) gives the side (PDL/PDH), the leader, its level, when it
  touched (WAT) and the price, the laggard, its level, its current price and distance in pips, **its closest
  approach to the level during the window**, and the window length, plus a line stating what happened ("GBPUSD
  took its PDL, EURUSD held above its PDL after 60 min"). A closest approach within `NEAR_MISS_PIPS` (2) is
  **annotated, never suppressed**: it may be a feed difference between the Twelve Data levels and the Finnhub
  ticks. If the laggard's own tick touched its level *after* the deadline but before the timer ran, the alert says
  it "touched N min after the window closed" instead of "did not touch".
- **Levels** are recorded by a job at 17:10 New York, Monday to Friday: one 1H request per pair (four a day),
  ~8 s apart for Twelve Data's free tier. If a group can't be recorded (quota, an error, a lagging pair) it is
  retried once 15 minutes later. A startup catch-up covers a missed run, and `/divscan` records them by hand.
- **Arming late** (the 17:10 run itself, a catch-up, `/divscan`): the 1H bars since the close are replayed in order.
  A 1H bar only gives the hour a touch happened in, so the window is judged conservatively from the *end* of the
  leader's hour (alerts are never early, only possibly late): both pairs touching in the same or adjacent hour is
  treated as in sync (silent); a laggard two or more hours later, or a leader whose window has already passed, sends
  the divergence immediately, labelled "⚡ Detected late" with the hour (never an exact time that wasn't observed).
- **Deadlines don't depend on ticks:** a job checks pending windows every 60 seconds (so an alert can be up to a
  minute after the exact deadline) and closes untouched rows after the day ends. All state lives in Postgres
  (`smt_divergence_levels` for the USD group, `smt_jpy_divergence_levels` for the JPY group) and is rebuilt on startup, including the laggard's closest approach (saved when the row
  resolves and every minute while a window is open). Every state change is an atomic conditional `UPDATE`, and a
  message is only sent by the caller that won it, so restarts, duplicate ticks and the tick-vs-timer race can't
  produce two messages.

**Daily candles.** A trading day runs 17:00 → 17:00 `America/New_York` (matching FOREX.com on TradingView),
built by aggregating Twelve Data 1H bars — Twelve Data's own `1day` candles use midnight UTC and don't match. US
daylight saving is handled by `zoneinfo` (hence the `tzdata` requirement). Sunday evening's session belongs to
Monday, and the bars between Friday 17:00 and Sunday 17:00 New York time are dropped by time alone (Twelve Data
keeps quoting through the weekend). A day is only used if its final bar (16:00–17:00 NY) is present, it has at
least 22 bars (`PAIR_MIN_DAILY_BARS` overrides per pair) and its range isn't degenerate.

**Known limitation.** Levels come from Twelve Data, live ticks from Finnhub (OANDA), and your chart from
FOREX.com. These are three different feeds, so levels can differ by a few pips from what you see on TradingView.

### SMT settings
Constants in `app/services/smt_divergence_service.py`, some with a per-pair override dict. A pip is 0.0001
(0.01 for JPY pairs).

| Setting | Default | Meaning |
|---|---|---|
| `DIVERGENCE_WAIT_MINUTES` | 60 | How long the laggard has, after the leader touches its level, to touch its own (capped by the day's close). |
| `TOUCH_BUFFER_PIPS` (`PAIR_TOUCH_BUFFER_PIPS`) | 0 | Tolerance that widens the touch zone toward price (price within this many pips of the level counts as a touch). 0 = a plain touch or break. |
| `NEAR_MISS_PIPS` (`PAIR_NEAR_MISS_PIPS`) | 2 | A laggard whose closest approach stayed within this many pips of its level gets a "may be a feed difference" note on the alert (it is never suppressed). |
| `DEADLINE_CHECK_INTERVAL_SECONDS` | 60 | How often the scheduler resolves windows whose deadline has passed. |

---

## Upgrading

The app creates missing tables on startup and adds the `users.divergence_alerts_enabled` column if it's missing.
All of this is idempotent; nothing is ever dropped at runtime, and no manual SQL is needed to upgrade.

The Daily Sweep and Daily Continuation features have been removed. Their data is left in the database, unused. If
you're sure you won't bring them back, this optional one-off cleanup deletes it permanently:

```sql
DROP TABLE IF EXISTS daily_sweep_setups;
DROP TABLE IF EXISTS daily_sweep_scan_state;
ALTER TABLE users DROP COLUMN IF EXISTS daily_sweep_alerts_enabled;
ALTER TABLE users DROP COLUMN IF EXISTS daily_continuation_alerts_enabled;
```

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

`tests/test_integration_postgres.py` runs the real SQL (schema upgrade, unique constraints, atomic claims,
expiry, opt-in flag) and is skipped unless `TEST_DATABASE_URL` is set. It works inside a
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
│   ├── market_data_service.py     # Twelve Data price + candle fetching (per-user key, or server key for SMT)
│   ├── price_alert_service.py     # Alert CRUD + trigger logic
│   ├── invite_service.py          # Invite-code generation/redemption
│   ├── daily_candles.py           # 17:00-NY daily candles, trading calendar, pip/price formatting
│   ├── smt_divergence_service.py  # SMT divergence (USD and JPY pair groups): PDH/PDL levels, wait-window state machine, alerts
│   └── finnhub_price_service.py   # Live Finnhub WebSocket feed
├── telegram_bot.py                # All bot commands + message routing
├── webhook.py                     # FastAPI app exposing POST /webhook/<token> (production only)
├── scheduler.py                   # Friday cleanup + alert-ID reset jobs, SMT level capture (17:10 NY) and deadline check (60 s)
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
