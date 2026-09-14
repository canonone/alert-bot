import datetime
import logging
from typing import Any

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
    retracement_zone_service: Any = None,
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

    # Weekly, not daily: runs only on Friday, the last trading day before the weekend.
    # NOTE: python-telegram-bot's `days` uses 0=Sunday...6=Saturday (NOT Python's usual
    # Monday=0 convention — this changed in PTB v20.0), so Friday is index 5.
    # Both jobs MUST share the same cadence — alert IDs are only safe to reset when the
    # alerts themselves are cleared at the same time, otherwise a still-active alert from
    # earlier in the week could collide with a new alert reusing the same reset ID.
    FRIDAY = (5,)
    application.job_queue.run_daily(
        run_daily_cleanup, time=datetime.time(hour=21, tzinfo=datetime.timezone.utc), days=FRIDAY
    )
    application.job_queue.run_daily(
        reset_alert_id_counters, time=datetime.time(hour=23, tzinfo=datetime.timezone.utc), days=FRIDAY
    )

    if retracement_zone_service is not None:
        async def poll_retracement_zones(context: ContextTypes.DEFAULT_TYPE) -> None:
            await retracement_zone_service.poll_all_pairs()

        # We deliberately poll rather than schedule at a hardcoded 4H boundary: the true
        # boundary alignment can only be confirmed by inspecting live Twelve Data output
        # (see RetracementZoneService._log_boundary_alignment / scripts/inspect_4h_candles.py),
        # and polling detects a new closed candle regardless of that alignment.
        application.job_queue.run_repeating(poll_retracement_zones, interval=300, first=60)
        logger.info("[Scheduler] 4H retracement-zone polling job registered (every 5 min)")
