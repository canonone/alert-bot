import datetime
import logging
from dataclasses import dataclass

from sqlalchemy import select, update

from app.db import SessionLocal
from app.models import PriceAlert
from app.services.market_data_service import MarketDataService
from app.services.users_service import UsersService

logger = logging.getLogger("PriceAlertService")

ALLOWED_SYMBOLS = {
    "AUDCAD", "AUDCHF", "AUDJPY", "AUDNZD", "AUDUSD",
    "CADCHF", "CADJPY", "CHFJPY",
    "EURAUD", "EURCAD", "EURCHF", "EURGBP", "EURJPY",
    "EURNZD", "EURUSD",
    "GBPAUD", "GBPCAD", "GBPCHF", "GBPJPY", "GBPNZD", "GBPUSD",
    "NZDCAD", "NZDCHF", "NZDJPY", "NZDUSD",
    "USDCAD", "USDCHF", "USDJPY",
    "XAUUSD",
}


@dataclass
class TriggeredAlert:
    alert: PriceAlert
    current_price: float


class PriceAlertService:
    def __init__(self, market_data: MarketDataService, users_service: UsersService) -> None:
        self.market_data = market_data
        self.users_service = users_service

    # ── Add alert ─────────────────────────────────────────────────

    async def add_alert(self, chat_id: str, symbol: str, type_: str, target_price: float) -> tuple[bool, str]:
        upper_symbol = symbol.upper()

        if upper_symbol not in ALLOWED_SYMBOLS:
            return False, (
                f"❌ <b>{upper_symbol}</b> is not supported.\n\n"
                f"Use /pairs to see all supported symbols."
            )

        if target_price != target_price or target_price <= 0:  # NaN check via self-inequality
            return False, f"❌ Invalid price <b>{target_price}</b>. Enter a valid positive number."

        if type_ in ("SL", "TP"):
            user = await self.users_service.find_by_chat_id(chat_id)
            if user and user.twelve_data_api_key:
                current_price, _ = await self.market_data.get_current_price(upper_symbol, user.twelve_data_api_key)
                if current_price is not None:
                    if type_ == "SL" and current_price <= target_price:
                        return False, (
                            f"❌ <b>Invalid SL Level</b>\n\n"
                            f"Your SL ({target_price}) is at or above the current price ({current_price}).\n\n"
                            f"SL must be set BELOW current price.\n\n"
                            f"Current price: <b>{current_price}</b>"
                        )
                    if type_ == "TP" and current_price >= target_price:
                        return False, (
                            f"❌ <b>Invalid TP Level</b>\n\n"
                            f"Your TP ({target_price}) is at or below the current price ({current_price}).\n\n"
                            f"TP must be set ABOVE current price.\n\n"
                            f"Current price: <b>{current_price}</b>"
                        )

        user_alert_id = await self.users_service.get_next_alert_id(chat_id)

        async with SessionLocal() as session:
            alert = PriceAlert(
                chat_id=chat_id,
                symbol=upper_symbol,
                type=type_,
                target_price=target_price,
                user_alert_id=user_alert_id,
                active=True,
            )
            session.add(alert)
            await session.commit()

        emoji = self.get_emoji(type_)

        return True, (
            f"{emoji} <b>Alert Set!</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Symbol: <b>{upper_symbol}</b>\n"
            f"📌 Type: <b>{type_}</b>\n"
            f"💰 Target Price: <b>{target_price}</b>\n"
            f"🔢 Alert ID: <b>#{user_alert_id}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"You'll be notified when price reaches this level.\n"
            f"Real-time alerts via live price feed ⚡"
        )

    # ── Cancel alert by ID (scoped to user) ───────────────────────

    async def cancel_alert(self, chat_id: str, alert_id: int) -> tuple[bool, str]:
        async with SessionLocal() as session:
            result = await session.execute(
                select(PriceAlert).where(
                    PriceAlert.user_alert_id == alert_id,
                    PriceAlert.chat_id == chat_id,
                    PriceAlert.active.is_(True),
                )
            )
            alert = result.scalar_one_or_none()

            if alert is None:
                return False, (
                    f"❌ No active alert found with ID <b>#{alert_id}</b>\n\nUse /listalerts to see your alerts."
                )

            alert.active = False
            await session.commit()

            return True, (
                f"✅ <b>Alert Cancelled</b>\n\n"
                f"ID: #{alert_id}\n"
                f"Symbol: {alert.symbol}\n"
                f"Type: {alert.type}\n"
                f"Price: {alert.target_price}"
            )

    # ── Cancel all alerts for a symbol (scoped to user) ──────────

    async def cancel_alerts_by_symbol(self, chat_id: str, symbol: str) -> tuple[bool, str]:
        upper_symbol = symbol.upper()

        async with SessionLocal() as session:
            result = await session.execute(
                update(PriceAlert)
                .where(
                    PriceAlert.chat_id == chat_id,
                    PriceAlert.symbol == upper_symbol,
                    PriceAlert.active.is_(True),
                )
                .values(active=False)
            )
            await session.commit()
            count = result.rowcount or 0

        if count == 0:
            return False, f"❌ No active alerts found for <b>{upper_symbol}</b>"

        return True, f"✅ Cancelled <b>{count}</b> alert(s) for <b>{upper_symbol}</b>"

    # ── Cancel all user alerts ────────────────────────────────────

    async def cancel_all_alerts(self, chat_id: str) -> tuple[bool, str]:
        async with SessionLocal() as session:
            result = await session.execute(
                update(PriceAlert)
                .where(PriceAlert.chat_id == chat_id, PriceAlert.active.is_(True))
                .values(active=False)
            )
            await session.commit()
            count = result.rowcount or 0

        if count == 0:
            return False, "❌ You have no active alerts to cancel."

        return True, f"✅ Cancelled all <b>{count}</b> active alert(s)."

    # ── List all active alerts for a user ─────────────────────────

    async def list_alerts(self, chat_id: str) -> str:
        async with SessionLocal() as session:
            result = await session.execute(
                select(PriceAlert)
                .where(PriceAlert.chat_id == chat_id, PriceAlert.active.is_(True))
                .order_by(PriceAlert.symbol.asc(), PriceAlert.created_at.asc())
            )
            active = list(result.scalars().all())

        if not active:
            return (
                "📭 <b>No Active Alerts</b>\n\n"
                "Use /setalert to create one.\n\n"
                "Example:\n"
                "<code>/setalert GBPUSD SL 1.3200</code>"
            )

        # Group by symbol, preserving first-seen order (matches JS reduce-into-object semantics)
        grouped: dict[str, list[PriceAlert]] = {}
        for alert in active:
            grouped.setdefault(alert.symbol, []).append(alert)

        message = f"📋 <b>Your Active Alerts ({len(active)})</b>\n\n"

        for symbol, symbol_alerts in grouped.items():
            message += "━━━━━━━━━━━━━━━━━━━━\n"
            message += f"📊 <b>{symbol}</b>\n"
            for a in symbol_alerts:
                message += f"  {self.get_emoji(a.type)} {a.type} @ <b>{a.target_price}</b> — #<b>{a.user_alert_id}</b>\n"

        message += "━━━━━━━━━━━━━━━━━━━━\n"
        message += "/cancelalert [id] — cancel by ID\n"
        message += "/cancelalerts [symbol] — cancel all for pair\n"
        message += "/cancelalerts all — cancel everything"

        return message

    # ── WebSocket: get all active alerts grouped by symbol ────────

    async def get_active_alerts_grouped_by_symbol(self) -> dict[str, list[PriceAlert]]:
        async with SessionLocal() as session:
            result = await session.execute(select(PriceAlert).where(PriceAlert.active.is_(True)))
            active = list(result.scalars().all())

        grouped: dict[str, list[PriceAlert]] = {}
        for alert in active:
            grouped.setdefault(alert.symbol, []).append(alert)
        return grouped

    # ── WebSocket: check a single tick against all alerts for a symbol

    async def check_tick_against_alerts(
        self, symbol: str, price: float, previous_price: float | None
    ) -> list[TriggeredAlert]:
        async with SessionLocal() as session:
            result = await session.execute(
                select(PriceAlert).where(PriceAlert.symbol == symbol, PriceAlert.active.is_(True))
            )
            active = list(result.scalars().all())
            if not active:
                return []

            triggered: list[TriggeredAlert] = []

            for alert in active:
                if self.is_triggered(alert, price, previous_price):
                    alert.active = False
                    triggered.append(TriggeredAlert(alert=alert, current_price=price))

            await session.commit()
            return triggered

    # ── Daily cleanup: cancel all active alerts system-wide ───────

    async def cancel_all_alerts_for_all_users(self) -> dict[str, int]:
        async with SessionLocal() as session:
            result = await session.execute(select(PriceAlert).where(PriceAlert.active.is_(True)))
            active_alerts = list(result.scalars().all())

            count_map: dict[str, int] = {}
            for alert in active_alerts:
                count_map[alert.chat_id] = count_map.get(alert.chat_id, 0) + 1

            if active_alerts:
                await session.execute(update(PriceAlert).where(PriceAlert.active.is_(True)).values(active=False))
                await session.commit()

            return count_map

    # ── Cron: check all alerts for a specific user ────────────────
    # NOTE: not wired to any scheduler — this mirrors the original TS
    # PriceAlertService.checkAlertsForUser, which likewise exists but is
    # never invoked (only the Finnhub WebSocket path is live).

    async def check_alerts_for_user(self, chat_id: str, api_key: str) -> tuple[list[TriggeredAlert], bool]:
        async with SessionLocal() as session:
            result = await session.execute(
                select(PriceAlert).where(PriceAlert.chat_id == chat_id, PriceAlert.active.is_(True))
            )
            active = list(result.scalars().all())
            if not active:
                return [], False

            symbols = list({a.symbol for a in active})
            logger.info(f"[{chat_id}] Checking {len(symbols)} symbol(s)...")

            prices, quota_exceeded = await self.market_data.get_batch_prices(symbols, api_key)

            if quota_exceeded:
                logger.warning(f"[{chat_id}] Quota exceeded")
                return [], True

            triggered: list[TriggeredAlert] = []

            for alert in active:
                current_price = prices.get(alert.symbol)
                if current_price is None:
                    logger.warning(f"[{chat_id}] No price for {alert.symbol}")
                    continue

                if self.is_triggered(alert, current_price, None):
                    alert.active = False
                    triggered.append(TriggeredAlert(alert=alert, current_price=current_price))
                    logger.info(
                        f"[{chat_id}] 🔔 #{alert.id} triggered — {alert.symbol} {alert.type} "
                        f"@ {alert.target_price} (current: {current_price})"
                    )

            await session.commit()
            return triggered, False

    # ── Alert trigger logic ───────────────────────────────────────

    def is_triggered(self, alert: PriceAlert, current_price: float, previous_price: float | None) -> bool:
        target = float(alert.target_price)

        if alert.type == "SL":
            return current_price <= target
        if alert.type == "TP":
            return current_price >= target
        if alert.type == "TARGET":
            if previous_price is not None:
                crossed_up = previous_price < target <= current_price
                crossed_down = previous_price > target >= current_price
                return crossed_up or crossed_down
            return current_price == target
        return False

    # ── Build alert notification message ─────────────────────────

    def build_alert_message(self, alert: PriceAlert, current_price: float) -> str:
        wat_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
        formatted_time = wat_time.strftime("%Y-%m-%d %H:%M") + " WAT"

        if alert.type == "SL":
            return (
                f"🔴 <b>STOP LOSS HIT — {alert.symbol}</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💀 <b>SL Level:</b> {alert.target_price}\n"
                f"📉 <b>Current Price:</b> {current_price}\n"
                f"🕐 <b>Time:</b> {formatted_time}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⚠️ Cut your losses. Protect your capital."
            )
        if alert.type == "TP":
            return (
                f"🟢 <b>TAKE PROFIT HIT — {alert.symbol}</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 <b>TP Level:</b> {alert.target_price}\n"
                f"📈 <b>Current Price:</b> {current_price}\n"
                f"🕐 <b>Time:</b> {formatted_time}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 Well done. Lock in those gains."
            )
        # TARGET
        return (
            f"🎯 <b>TARGET REACHED — {alert.symbol}</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 <b>Target:</b> {alert.target_price}\n"
            f"💰 <b>Current Price:</b> {current_price}\n"
            f"🕐 <b>Time:</b> {formatted_time}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Your target level has been reached."
        )

    def get_emoji(self, type_: str) -> str:
        return {"SL": "🔴", "TP": "🟢", "TARGET": "🎯"}[type_]
