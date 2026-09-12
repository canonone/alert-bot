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
| `/xauzone [PAIR]` | Current 4H retracement-zone status (default XAUUSD) |
| `/zonealerts [on\|off]` | Opt in/out of 4H zone notifications |
| `/help` | Full command list |

### Alert Types
- `SL` — fires when price drops to level
- `TP` — fires when price rises to level
- `TARGET` — fires when price crosses through the level (real-time tick feed)

### 4H Retracement Zone ("OTE") Alerts
Requires `TWELVE_DATA_API_KEY` (server-level) in `.env` — disabled otherwise. Tracks each
configured pair's (default `XAUUSD`) completed 4H candles, classifies bias, and computes the
midpoint (50%) of that candle's range as the trigger price (direction-independent — same value
either way). While the next 4H candle forms, live Finnhub ticks are checked against that trigger
and fire one-shot `ZONE_ENTRY` and `INVALIDATED` alerts (invalidated if price breaks the prior
candle's opposite extreme first) to opted-in users (`/zonealerts on`). See
`scripts/inspect_4h_candles.py` to inspect Twelve Data's actual 4H candle boundary alignment.

---

## Architecture

```
app/
├── services/
│   ├── users_service.py           # User registration + API key storage
│   ├── market_data_service.py     # Twelve Data price + candle fetching (per-user key, or server key for zones)
│   ├── price_alert_service.py     # Alert CRUD + trigger logic
│   ├── invite_service.py          # Invite-code generation/redemption
│   ├── retracement_zone_service.py # 4H candle retracement ("OTE") zone tracking + alerts
│   └── finnhub_price_service.py   # Live Finnhub WebSocket feed
├── telegram_bot.py                # All bot commands + message routing
├── webhook.py                     # FastAPI app exposing POST /webhook/<token> (production only)
├── scheduler.py                   # Daily cleanup + alert-ID reset jobs
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
