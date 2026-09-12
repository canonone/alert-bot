import datetime
import logging

from telegram.ext import Application, ContextTypes

from app.services.price_alert_service import PriceAlertService
from app.services.users_service import UsersService
from app.telegram_bot import TelegramBotService

logger = logging.getLogger("DailyCleanupCron")


def register_jobs(
    application: Application,
    price_alert_service: PriceAlertService,
    telegram_bot: TelegramBotService,
    users_service: UsersService,
) -> None:
    async def run_daily_cleanup(context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.info("[DailyCleanup] Running daily alert cleanup at 10 PM WAT")

        price_counts = await price_alert_service.cancel_all_alerts_for_all_users()
        total_cancelled = 0

        for chat_id, price_count in price_counts.items():
            total_cancelled += price_count

            if price_count > 0:
                message = (
                    "🌙 <b>Daily Alert Cleanup</b>\n\n"
                    "All your active alerts have been automatically cancelled for end of day.\n\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"🔔 Price alerts cancelled: {price_count}\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "<i>Set new alerts tomorrow with /setalert</i>"
                )
                await telegram_bot.send_message_to_user(chat_id, message)

        logger.info(
            f"[DailyCleanup] Total alerts cancelled: {total_cancelled} across {len(price_counts)} user(s)"
        )

    async def reset_alert_id_counters(context: ContextTypes.DEFAULT_TYPE) -> None:
        await users_service.reset_all_alert_id_counters()
        logger.info("[DailyCleanup] Alert ID counters reset for all users")

    application.job_queue.run_daily(
        run_daily_cleanup, time=datetime.time(hour=21, tzinfo=datetime.timezone.utc)
    )
    application.job_queue.run_daily(
        reset_alert_id_counters, time=datetime.time(hour=23, tzinfo=datetime.timezone.utc)
    )
