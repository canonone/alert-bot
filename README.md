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
| `/sweeps` (or `/setups`) | List all active setups — sweeps and continuations (pair, direction, level(s), which continuation level is hit, expiry in WAT) — only active setups; an invalidated one simply disappears |
| `/sweepcheck [PAIR]` | Show the last two completed daily candles as the bot built them, and whether they form a sweep or continuation setup |
| `/sweepscan` | Admin only: run the daily scan (both features) now |
| `/divalerts [on\|off]` | Opt in/out of SMT divergence notifications (both pair groups) |
| `/divergence` (or `/smt`) | Show both groups' PDH/PDL being watched and where each side stands (waiting, leader touched + deadline, confirmed, divergence sent), times in WAT |
| `/divscan` | Admin only: record both groups' levels now (four Twelve Data requests) |
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

**Invalidation (continuations only).** The pullback to the points of interest is only valid if price has
not first swept Day 2's extreme. While a continuation is still at stage 0 (Level 1 not yet reached), if
price trades beyond Day 2's **high** (BUY) or **low** (SELL) by *more than* `SWEEP_INVALIDATION_BUFFER_PIPS`
(2 pips by default), the setup is marked `INVALIDATED`: a later retrace to Level 1 or Level 2 won't alert,
and `/contalerts` subscribers get one short message with the swept level, the price seen and *when it was
detected* in WAT (after a restart, or when a setup is armed late, the sweep may have happened earlier, so the
message never claims when it happened). The row keeps `invalidated_at` / `invalidated_price` for audit, but
`/sweeps` no longer lists it. The threshold shown in `/sweeps` is Day 2's high **+** the buffer for a BUY and
Day 2's low **−** the buffer for a SELL. The check
runs before any stage advancement, and the status change is an atomic database claim
(`ACTIVE → INVALIDATED` only at stage 0 and before Day 3 closes), so restarts, duplicate ticks and a
simultaneous Level 1 tick can't produce two outcomes. Once Level 1 has fired the rule no longer applies:
Level 2 stays live. **Arming late.** At the normal 17:10 scan almost nothing has happened since Day 2 closed. A manual `/sweepscan`
or a catch-up scan hours later is different, so a continuation is armed by **replaying the 1H bars since Day 2's
close in order** (including the in-progress bar) with the live rule: Day 2's extreme swept while still at stage 0
voids it (the setup is not created and the scan logs why); reaching Level 1 first moves it to stage 1, after
which a later sweep no longer matters, then Level 2 to stage 2 (a merged zone completes at Level 1). A setup
that comes out already at a later stage is stored at that stage and announced once as "reached earlier" with
the 1H hour it happened in (`⚡ Level 1 was reached earlier — in the 02:00–03:00 WAT hour (high 159.613) —
before the setup was armed.`), then keeps watching for what's left. Because the insert is the once-only claim, a
rescan can't announce it again. A single 1H bar that both swept Day 2's extreme and reached Level 1 can't be
ordered, so it voids (no alert is better than a wrong one). If the fetch has no bars since the close, the latest
price is used for the sweep check instead, as before. Sweep setups are not replayed.

Because a day's open is the previous day's close, Day 2 always opens at Day 1's close, so a gap
between Day 1 and Day 2 can no longer occur and the "no gap" skip effectively never fires (only if
Day 1 closed exactly on its own high/low, or when an open fell back to the first bar). The code is
kept as a harmless guard. `/sweepcheck` shows `open = previous close` per candle, and both values
when the first 1H bar's open differs by more than 2 pips.

### SMT Divergence Alerts (GBPUSD / EURUSD and EURJPY / GBPJPY)
Requires `TWELVE_DATA_API_KEY` (like Daily Sweep; without it the feature is disabled with a startup warning).
Opt in with `/divalerts on` (approved users only); it covers both pair groups.

There are two **separate** pair groups, **GBPUSD / EURUSD** and **EURJPY / GBPJPY**. A pair is only ever compared
with its partner: each group has its own levels, table, state and alerts (the JPY ones are titled
`SMT DIVERGENCE (JPY)`), `/smt` shows one section per group, and a failure in one doesn't stop the other. Both
follow the rules below. The two pairs of a group are positively correlated, so they should take out their
previous-day liquidity at about the same time. At each trading-day close (17:00 America/New_York) the previous-day high (PDH) and low (PDL) of both
pairs are recorded from the same synthetic daily candles Daily Sweep builds (not Twelve Data's `1day`). They are
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
- **Levels** are recorded by the 17:10 New York Daily Sweep scan, reusing the candles of the four pairs it already
  fetched (no extra Twelve Data requests); the scan's own 15-minute retry covers a lagging pair, and a startup
  catch-up (or `/divscan`, which fetches just those pairs with the same request spacing and never while a sweep
  scan is running) covers a missed scan. Because the scan is alphabetical, a group's second pair is reached a couple
  of minutes in, so the first minutes of the period are covered by replaying the 1H bars since the close.
- **Arming late** (the 17:10 scan itself, a catch-up, `/divscan`): the 1H bars since the close are replayed in order.
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

### Margins and settings
Small feed differences between Twelve Data and FOREX.com shouldn't create or break a setup, so the
patterns carry minimum margins (and the SMT divergence settings are listed here too). A pip is 0.0001 (0.01 for JPY pairs, 0.1 for XAUUSD). Each setting is
a default constant in `app/services/daily_sweep_service.py` with a per-pair override dict:

| Setting | Default | Meaning |
|---|---|---|
| `MIN_BEYOND_PIPS` (`PAIR_MIN_BEYOND_PIPS`) | 3 | A continuation's Day 2 **close** must be at least this far beyond Day 1's high (BUY) / low (SELL); a sweep's Day 2 **wick** must go at least this far beyond Day 1's high (SELL) / low (BUY). |
| `OPPOSITE_SIDE_BUFFER_PIPS` (`PAIR_OPPOSITE_SIDE_BUFFER_PIPS`) | 2 | Day 2 only counts as *not having taken* Day 1's opposite extreme if it stayed at least this many pips away from it (sweep SELL: low ≥ Day 1 low + buffer; sweep BUY: high ≤ Day 1 high − buffer; continuation BUY: low ≥ Day 1 low + buffer; continuation SELL: high ≤ Day 1 high − buffer). Twelve Data's extremes can sit a fraction of a pip inside FOREX.com's, so a near-touch counts as a touch. Exactly the buffer counts as clear; anything closer is taken. |
| `SWEEP_INVALIDATION_BUFFER_PIPS` (`PAIR_SWEEP_INVALIDATION_BUFFER_PIPS`) | 2 | Continuations only: price must exceed Day 2's high (BUY) / low (SELL) by MORE than this many pips, before Level 1, to void the setup. Exactly the buffer does not count. |
| `DIVERGENCE_WAIT_MINUTES` | 60 | SMT divergence: how long the laggard has, after the leader touches its level, to touch its own (capped by the day's close). |
| `TOUCH_BUFFER_PIPS` (`PAIR_TOUCH_BUFFER_PIPS`) | 0 | SMT divergence: tolerance that widens the touch zone toward price (price within this many pips of the level counts as a touch). 0 = a plain touch or break. |
| `NEAR_MISS_PIPS` (`PAIR_NEAR_MISS_PIPS`) | 2 | SMT divergence: a laggard whose closest approach stayed within this many pips of its level gets a "may be a feed difference" note on the alert (it is never suppressed). |
| `DEADLINE_CHECK_INTERVAL_SECONDS` | 60 | SMT divergence: how often the scheduler resolves windows whose deadline has passed. |
| `MIN_BODY_PIPS` (`PAIR_MIN_BODY_PIPS`) | 2 | A day is only bullish/bearish if `|close − open|` is at least this. Anything smaller is a doji and matches neither pattern. |
| `MERGE_LEVELS_PIPS` (`PAIR_MERGE_LEVELS_PIPS`) | 3 | When a continuation's Level 1 and Level 2 are within this many pips, they are one zone: ONE combined alert when Level 1 is reached (with a note that the levels are only X pips apart), and the setup is complete. `/sweepcheck`, `/sweeps` and the digest show "single alert (levels X pips apart)". |

`/sweepcheck` lists every failed check with the numbers, e.g.
`✗ needs Day 2 to close ≥3 pips above Day 1's high — but it closed 0.1 pips above (112.408 vs 112.407)`
`✗ needs Day 2 to stay ≥2 pips below Day 1's high — but it came within 0.2 pips (0.70403 vs 0.70405)`
and `✗ Day 2 is a doji (body 1.1 pips < 2)`. Whether levels are merged is decided from the stored
levels and the current setting, so changing `MERGE_LEVELS_PIPS` also applies to setups already armed.

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
the `users.daily_sweep_alerts_enabled` and `users.divergence_alerts_enabled` columns if they're missing, and creates
the `smt_divergence_levels` and `smt_jpy_divergence_levels` tables (SMT divergence) automatically. It also adds the continuation columns
(including `invalidated_at` / `invalidated_price`) and widens `daily_sweep_setups.status` from `VARCHAR(10)` to
`VARCHAR(16)` so the `INVALIDATED` status fits (a no-rewrite change). All of this is idempotent and happens
on startup; nothing is ever dropped at runtime, and there is no manual SQL for this upgrade.
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
│   ├── smt_divergence_service.py  # SMT divergence (USD and JPY pair groups): PDH/PDL levels, wait-window state machine, alerts
│   └── finnhub_price_service.py   # Live Finnhub WebSocket feed
├── telegram_bot.py                # All bot commands + message routing
├── webhook.py                     # FastAPI app exposing POST /webhook/<token> (production only)
├── scheduler.py                   # Friday cleanup + alert-ID reset jobs, Daily Sweep scan (17:10 NY), SMT deadline check (60 s)
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
