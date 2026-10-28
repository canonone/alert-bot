import os

# app.config raises on missing env vars at import time; the pure functions under test never use them
for name, value in {
    "TELEGRAM_BOT_TOKEN": "test",
    "ADMIN_CHAT_ID": "1",
    "DB_HOST": "localhost",
    "DB_USERNAME": "test",
    "DB_PASSWORD": "test",
    "DB_NAME": "test",
    "FINNHUB_API_KEY": "test",
}.items():
    os.environ.setdefault(name, value)
