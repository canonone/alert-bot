import logging

from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application

logger = logging.getLogger("TelegramWebhook")


def create_webhook_app(application: Application, bot_token: str) -> FastAPI:
    app = FastAPI()

    # Telegram sends POST requests to /webhook/<bot-token>
    # The bot token in the URL acts as a secret — only Telegram knows it
    @app.post("/webhook/{token}")
    async def handle_webhook(token: str, request: Request) -> dict:
        # Reject requests with wrong token
        if token != bot_token:
            return {"ok": False}

        data = await request.json()
        update = Update.de_json(data, application.bot)

        # Hand off to PTB's own update-processing task and return 200 immediately —
        # Telegram will retry if it doesn't get 200 within 60s
        await application.update_queue.put(update)

        return {"ok": True}

    return app
