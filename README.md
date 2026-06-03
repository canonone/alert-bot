# Price Alert Bot

Multi-user Forex price alert bot with lot size calculator, powered by Telegram + Twelve Data.

## Features

- 🔔 Price alerts (SL, TP, TARGET) per user
- 📦 Lot size calculator with auto live rate fetching
- 👤 Per-user Twelve Data API keys — isolated quotas
- 💾 PostgreSQL persistence — alerts survive restarts
- 🤖 100% Telegram-native — no web frontend needed

## Setup

### 1. Clone & install
```bash
npm install
```

### 2. Create your `.env` file
```bash
cp .env.example .env
```

Fill in:
```env
TELEGRAM_BOT_TOKEN=your-bot-token-here   # from @BotFather
DB_HOST=localhost
DB_PORT=5432
DB_USERNAME=postgres
DB_PASSWORD=yourpassword
DB_NAME=price_alert_bot
PORT=3000
NODE_ENV=development
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
# Development
npm run start:dev

# Production
npm run build
npm run start:prod
```

Tables are created automatically on first run (`synchronize: true`).

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
| `/lotsize [PAIR] [RISK$] [SL PIPS]` | Calculate lot size |
| `/price [PAIR]` | Get live price |
| `/pairs` | List supported pairs |
| `/help` | Full command list |

### Alert Types
- `SL` — fires when price drops to level
- `TP` — fires when price rises to level
- `TARGET` — fires when price touches level (0.05% tolerance)

### Lot Size Examples
```
/lotsize GBPUSD 50 20       → pip value $10 flat (USD quoted)
/lotsize EURJPY 100 30      → fetches USDJPY live, converts pip value
/lotsize USDCHF 75 25       → fetches USDCHF live, converts pip value
/lotsize EURCAD 50 15       → fetches USDCAD live, converts pip value
```

---

## Architecture

```
src/
├── users/              # User registration + API key storage
├── market-data/        # Twelve Data price fetching (per-user key)
├── lot-size/           # Lot size calculation engine
├── price-alert/        # Alert CRUD + 15-min cron checker
└── telegram-bot/       # All bot commands + message routing
```

---

## Deployment (Railway)

1. Push to GitHub
2. Create new Railway project → Deploy from GitHub
3. Add PostgreSQL plugin
4. Set environment variables in Railway dashboard
5. Deploy

Railway auto-detects NestJS and runs `npm run start:prod`.
