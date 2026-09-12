import asyncio
import logging

import uvicorn
from telegram.ext import Application, MessageHandler, filters

from app import config, scheduler
from app.db import init_db
from app.services.finnhub_price_service import FinnhubPriceService
from app.services.invite_service import InviteService
from app.services.market_data_service import MarketDataService
from app.services.price_alert_service import PriceAlertService
from app.services.retracement_zone_service import RetracementZoneService
from app.services.users_service import UsersService
from app.telegram_bot import TelegramBotService
from app.webhook import create_webhook_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
)
logger = logging.getLogger("Bootstrap")


def build_services() -> tuple[TelegramBotService, FinnhubPriceService]:
    market_data = MarketDataService()
    users_service = UsersService()
    price_alert_service = PriceAlertService(market_data, users_service)
    invite_service = InviteService()

    telegram_bot_service = TelegramBotService(
        users_service=users_service,
        price_alert_service=price_alert_service,
        market_data=market_data,
        invite_service=invite_service,
    )

    retracement_zone_service: RetracementZoneService | None = None
    if config.TWELVE_DATA_API_KEY:
        retracement_zone_service = RetracementZoneService(
            api_key=config.TWELVE_DATA_API_KEY,
            pairs=config.RETRACEMENT_ZONE_PAIRS,
            market_data=market_data,
            users_service=users_service,
            send_message=telegram_bot_service.send_message_to_user,
        )
    else:
        logger.warning("TWELVE_DATA_API_KEY not set — 4H retracement zone feature disabled")
    telegram_bot_service.retracement_zone_service = retracement_zone_service

    finnhub_price_service = FinnhubPriceService(
        price_alert_service=price_alert_service,
        send_message=telegram_bot_service.send_message_to_user,
        retracement_zone_service=retracement_zone_service,
        extra_symbols=set(config.RETRACEMENT_ZONE_PAIRS) if retracement_zone_service else set(),
    )
    telegram_bot_service.finnhub_price_service = finnhub_price_service

    return telegram_bot_service, finnhub_price_service


def build_application(telegram_bot_service: TelegramBotService) -> Application:
    application = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    telegram_bot_service.bot = application.bot
    application.add_handler(
        MessageHandler(filters.TEXT | filters.COMMAND, telegram_bot_service.handle_update)
    )
    return application


async def _start_common(
    application: Application,
    telegram_bot_service: TelegramBotService,
    finnhub_price_service: FinnhubPriceService,
) -> None:
    await init_db()
    if telegram_bot_service.retracement_zone_service is not None:
        await telegram_bot_service.retracement_zone_service.bootstrap()
    finnhub_price_service.start()
    scheduler.register_jobs(
        application,
        price_alert_service=telegram_bot_service.price_alert_service,
        telegram_bot=telegram_bot_service,
        users_service=telegram_bot_service.users_service,
        retracement_zone_service=telegram_bot_service.retracement_zone_service,
    )


# ── Production: FastAPI + uvicorn own the HTTP server, PTB just processes updates ──

async def run_production(
    application: Application,
    telegram_bot_service: TelegramBotService,
    finnhub_price_service: FinnhubPriceService,
) -> None:
    await application.initialize()
    await application.start()
    await _start_common(application, telegram_bot_service, finnhub_price_service)

    webhook_path = f"webhook/{config.TELEGRAM_BOT_TOKEN}"
    full_url = f"{config.WEBHOOK_URL}/{webhook_path}"

    await application.bot.delete_webhook()
    result = await application.bot.set_webhook(
        url=full_url, allowed_updates=["message"], drop_pending_updates=True
    )
    if result:
        logger.info(f"✅ Webhook registered: {full_url}")
    else:
        logger.error("❌ Webhook registration failed")

    logger.info(f"Price Alert Bot running [{config.NODE_ENV}]")

    fastapi_app = create_webhook_app(application, config.TELEGRAM_BOT_TOKEN)
    server = uvicorn.Server(uvicorn.Config(fastapi_app, host="0.0.0.0", port=config.PORT, log_level="warning"))

    try:
        await server.serve()
    finally:
        await finnhub_price_service.stop()
        await application.stop()
        await application.shutdown()


# ── Development: PTB owns polling directly, no HTTP server involved ──

def run_development(
    application: Application,
    telegram_bot_service: TelegramBotService,
    finnhub_price_service: FinnhubPriceService,
) -> None:
    async def post_init(app: Application) -> None:
        await _start_common(app, telegram_bot_service, finnhub_price_service)
        logger.info(f"Price Alert Bot running [{config.NODE_ENV}]")

    async def post_shutdown(app: Application) -> None:
        await finnhub_price_service.stop()

    application.post_init = post_init
    application.post_shutdown = post_shutdown

    logger.info("Development mode — using polling")
    application.run_polling(allowed_updates=["message"])


def main() -> None:
    telegram_bot_service, finnhub_price_service = build_services()
    application = build_application(telegram_bot_service)

    if config.IS_PRODUCTION and config.WEBHOOK_URL:
        asyncio.run(run_production(application, telegram_bot_service, finnhub_price_service))
    else:
        run_development(application, telegram_bot_service, finnhub_price_service)


if __name__ == "__main__":
    main()
