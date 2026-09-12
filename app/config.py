import os

from dotenv import load_dotenv

load_dotenv()


def _get_or_throw(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


TELEGRAM_BOT_TOKEN = _get_or_throw("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID = _get_or_throw("ADMIN_CHAT_ID")

DB_HOST = _get_or_throw("DB_HOST")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_USERNAME = _get_or_throw("DB_USERNAME")
DB_PASSWORD = _get_or_throw("DB_PASSWORD")
DB_NAME = _get_or_throw("DB_NAME")

PORT = int(os.environ.get("PORT", "3000"))
NODE_ENV = os.environ.get("NODE_ENV", "development")
IS_PRODUCTION = NODE_ENV == "production"

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

FINNHUB_API_KEY = _get_or_throw("FINNHUB_API_KEY")
