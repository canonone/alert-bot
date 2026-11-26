import datetime
import logging
from zoneinfo import ZoneInfo

from telegram.ext import Application, ContextTypes

from app.services.daily_candles import SCAN_HOUR_NY, SCAN_MINUTE_NY, SCAN_RETRY_DELAY_SECONDS
from app.services.smt_divergence_service import DEADLINE_CHECK_INTERVAL_SECONDS, SmtDivergenceHub
from app.services.price_alert_service import PriceAlertService
from app.services.users_service import UsersService
from app.telegram_bot import TelegramBotService

logger = logging.getLogger("DailyCleanupCron")


def register_jobs(
    application: Application,
    price_alert_service: PriceAlertService,
    telegram_bot: TelegramBotService,
    users_service: UsersService,
    smt_divergence_service: SmtDivergenceHub | None = None,
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

    if smt_divergence_service is not None:
        async def check_smt_deadlines(context: ContextTypes.DEFAULT_TYPE) -> None:
            # Windows must close even when no tick arrives, so this runs on its own clock, not on price ticks
            try:
                await smt_divergence_service.check_deadlines()
            except Exception as error:
                logger.error(f"[SmtDivergence] Deadline check failed: {error}")

        async def catch_up_smt_levels(context: ContextTypes.DEFAULT_TYPE) -> None:
            try:
                result = await smt_divergence_service.catch_up()
            except Exception as error:
                logger.error(f"[SmtDivergence] Catch-up failed: {error}")
                return
            if result:
                logger.info(f"[SmtDivergence] Catch-up: {result}")

        async def record_levels(context: ContextTypes.DEFAULT_TYPE) -> bool:
            try:
                return await smt_divergence_service.record_levels()
            except Exception as error:
                logger.error(f"[SmtDivergence] Level capture failed: {error}")
                return False

        async def retry_smt_levels(context: ContextTypes.DEFAULT_TYPE) -> None:
            if not await record_levels(context):
                logger.warning("[SmtDivergence] Still could not record every group's levels after the retry")

        async def record_smt_levels(context: ContextTypes.DEFAULT_TYPE) -> None:
            if not await record_levels(context):
                logger.info("[SmtDivergence] Retrying the level capture in 15 minutes")
                context.job_queue.run_once(retry_smt_levels, when=SCAN_RETRY_DELAY_SECONDS)

        # 17:10 New York time — ten minutes after each forex trading day closes at 17:00 NY, so the final 1H bar has
        # had time to be published. The tzinfo makes APScheduler follow US daylight saving. Monday-Friday only:
        # days use PTB's 0=Sunday convention (see above).
        application.job_queue.run_daily(
            record_smt_levels,
            time=datetime.time(hour=SCAN_HOUR_NY, minute=SCAN_MINUTE_NY, tzinfo=ZoneInfo("America/New_York")),
            days=(1, 2, 3, 4, 5),
        )
        logger.info("[Scheduler] SMT divergence level capture registered (17:10 America/New_York, Mon-Fri)")
        application.job_queue.run_repeating(
            check_smt_deadlines, interval=DEADLINE_CHECK_INTERVAL_SECONDS, first=30
        )
        # Startup catch-up (bot was down at 17:10): a one-off job so it runs in the background
        application.job_queue.run_once(catch_up_smt_levels, when=15)
        logger.info(f"[Scheduler] SMT divergence deadline check registered (every {DEADLINE_CHECK_INTERVAL_SECONDS}s)")
