import datetime
import logging
from zoneinfo import ZoneInfo

from telegram.ext import Application, ContextTypes

from app.services.daily_sweep_service import SCAN_RETRY_DELAY_SECONDS, DailySweepService
from app.services.price_alert_service import PriceAlertService
from app.services.users_service import UsersService
from app.telegram_bot import TelegramBotService

logger = logging.getLogger("DailyCleanupCron")


def register_jobs(
    application: Application,
    price_alert_service: PriceAlertService,
    telegram_bot: TelegramBotService,
    users_service: UsersService,
    daily_sweep_service: DailySweepService | None = None,
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

    if daily_sweep_service is not None:
        async def retry_daily_sweep_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
            pairs: list[str] = context.job.data
            try:
                report = await daily_sweep_service.run_scan(pairs)
            except Exception as error:
                logger.error(f"[DailySweep] Retry scan failed: {error}")
                return
            if report.retry_pairs:
                logger.warning(f"[DailySweep] Still no usable data after retry for: {', '.join(report.retry_pairs)}")

        async def run_daily_sweep_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
            try:
                report = await daily_sweep_service.run_scan()
            except Exception as error:
                logger.error(f"[DailySweep] Scan failed: {error}")
                return
            if report.retry_pairs:
                logger.info(f"[DailySweep] Retrying {len(report.retry_pairs)} pair(s) in 15 minutes")
                context.job_queue.run_once(
                    retry_daily_sweep_scan, when=SCAN_RETRY_DELAY_SECONDS, data=report.retry_pairs
                )

        async def catch_up_daily_sweep_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
            try:
                if not await daily_sweep_service.needs_catch_up():
                    return
            except Exception as error:
                logger.error(f"[DailySweep] Catch-up check failed: {error}")
                return
            logger.info("[DailySweep] Latest closed trading day was never scanned — running catch-up scan")
            await run_daily_sweep_scan(context)

        # Startup catch-up (bot was down at 17:10): a one-off job so it runs in the background
        application.job_queue.run_once(catch_up_daily_sweep_scan, when=15)

        # 17:10 New York time — ten minutes after each forex trading day closes at 17:00 NY, so the
        # final 1H bar has had time to be published. The tzinfo makes APScheduler follow US
        # daylight saving. Monday-Friday only: days use PTB's 0=Sunday convention (see above).
        application.job_queue.run_daily(
            run_daily_sweep_scan,
            time=datetime.time(hour=17, minute=10, tzinfo=ZoneInfo("America/New_York")),
            days=(1, 2, 3, 4, 5),
        )
        logger.info("[Scheduler] Daily Sweep scan registered (17:10 America/New_York, Mon-Fri)")
