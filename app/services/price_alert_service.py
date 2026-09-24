import datetime
import html
import logging
from dataclasses import dataclass
from typing import Literal

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
    outcome: Literal["SL", "TP", "TARGET", "INVALIDATED"]


class PriceAlertService:
    def __init__(self, market_data: MarketDataService, users_service: UsersService) -> None:
        self.market_data = market_data
        self.users_service = users_service

    # ── Add alert ─────────────────────────────────────────────────

    async def add_alert(
        self,
        chat_id: str,
        symbol: str,
        type_: str,
        target_price: float,
        invalidation_price: float | None = None,
        note: str | None = None,
        direction: str = "LONG",
    ) -> tuple[bool, str]:
        upper_symbol = symbol.upper()
        note = note.strip()[:200] if note and note.strip() else None
        direction = direction.upper()
        # Only mention direction in rejections for SHORT, so LONG messages stay exactly as before
        direction_suffix = " for a <b>SHORT</b> position" if direction == "SHORT" else ""

        if upper_symbol not in ALLOWED_SYMBOLS:
            return False, (
                f"❌ <b>{upper_symbol}</b> is not supported.\n\n"
                f"Use /pairs to see all supported symbols."
            )

        if target_price != target_price or target_price <= 0:  # NaN check via self-inequality
            return False, f"❌ Invalid price <b>{target_price}</b>. Enter a valid positive number."

        if invalidation_price is not None and type_ != "TARGET":
            return False, "❌ An invalidation price is only valid for <b>TARGET</b> alerts."

        if invalidation_price is not None and (invalidation_price != invalidation_price or invalidation_price <= 0):
            return False, f"❌ Invalid invalidation price <b>{invalidation_price}</b>. Enter a valid positive number."

        if direction not in ("LONG", "SHORT"):
            return False, f"❌ Invalid direction <b>{html.escape(direction)}</b>. Use <b>LONG</b> or <b>SHORT</b>."

        if direction == "SHORT" and type_ not in ("SL", "TP"):
            return False, "❌ A direction is only valid for <b>SL</b> and <b>TP</b> alerts."

        if type_ in ("SL", "TP"):
            user = await self.users_service.find_by_chat_id(chat_id)
            if user and user.twelve_data_api_key:
                current_price, _ = await self.market_data.get_current_price(upper_symbol, user.twelve_data_api_key)
                if current_price is not None:
                    # LONG SL / SHORT TP sit below price; LONG TP / SHORT SL sit above it.
                    must_be_below = (type_ == "SL") == (direction == "LONG")
                    if must_be_below and current_price <= target_price:
                        return False, (
                            f"❌ <b>Invalid {type_} Level</b>\n\n"
                            f"Your {type_} ({target_price}) is at or above the current price ({current_price}).\n\n"
                            f"{type_} must be set BELOW current price{direction_suffix}.\n\n"
                            f"Current price: <b>{current_price}</b>"
                        )
                    if not must_be_below and current_price >= target_price:
                        return False, (
                            f"❌ <b>Invalid {type_} Level</b>\n\n"
                            f"Your {type_} ({target_price}) is at or below the current price ({current_price}).\n\n"
                            f"{type_} must be set ABOVE current price{direction_suffix}.\n\n"
                            f"Current price: <b>{current_price}</b>"
                        )

        if type_ == "TARGET" and invalidation_price is not None:
            user = await self.users_service.find_by_chat_id(chat_id)
            if user and user.twelve_data_api_key:
                current_price, _ = await self.market_data.get_current_price(upper_symbol, user.twelve_data_api_key)
                if current_price is not None:
                    target_above = target_price > current_price
                    invalidation_above = invalidation_price > current_price
                    if target_above == invalidation_above:
                        return False, (
                            f"❌ <b>Invalid Invalidation Level</b>\n\n"
                            f"Target ({target_price}) and invalidation ({invalidation_price}) are on the same "
                            f"side of the current price ({current_price}).\n\n"
                            f"They must be on opposite sides of the current price.\n\n"
                            f"Current price: <b>{current_price}</b>"
                        )

        user_alert_id = await self.users_service.get_next_alert_id(chat_id)

        async with SessionLocal() as session:
            alert = PriceAlert(
                chat_id=chat_id,
                symbol=upper_symbol,
                type=type_,
                target_price=target_price,
                invalidation_price=invalidation_price,
                note=note,
                direction=direction,
                user_alert_id=user_alert_id,
                active=True,
            )
            session.add(alert)
            await session.commit()

        emoji = self.get_emoji(type_)

        invalidation_line = (
            f"🛑 Invalidation Price: <b>{invalidation_price}</b>\n" if invalidation_price is not None else ""
        )
        note_line = f"📝 Note: <b>{html.escape(note)}</b>\n" if note is not None else ""
        direction_line = f"🧭 Direction: <b>{direction}</b>\n" if type_ in ("SL", "TP") else ""

        return True, (
            f"{emoji} <b>Alert Set!</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Symbol: <b>{upper_symbol}</b>\n"
            f"📌 Type: <b>{type_}</b>\n"
            f"{direction_line}"
            f"💰 Target Price: <b>{target_price}</b>\n"
            f"{invalidation_line}"
            f"{note_line}"
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
                invalidation_suffix = (
                    f" (invalidation: <b>{a.invalidation_price}</b>)" if a.invalidation_price is not None else ""
                )
                note_suffix = f" 📝 <i>{html.escape(a.note)}</i>" if a.note is not None else ""
                direction_label = f" ({a.direction})" if a.type in ("SL", "TP") else ""
                message += (
                    f"  {self.get_emoji(a.type)} {a.type}{direction_label} @ <b>{a.target_price}</b>{invalidation_suffix} "
                    f"— #<b>{a.user_alert_id}</b>{note_suffix}\n"
                )

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
                outcome = self.is_triggered(alert, price, previous_price)
                if outcome is not None:
                    alert.active = False
                    triggered.append(TriggeredAlert(alert=alert, current_price=price, outcome=outcome))

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

                outcome = self.is_triggered(alert, current_price, None)
                if outcome is not None:
                    alert.active = False
                    triggered.append(TriggeredAlert(alert=alert, current_price=current_price, outcome=outcome))
                    logger.info(
                        f"[{chat_id}] 🔔 #{alert.id} {outcome} — {alert.symbol} {alert.type} "
                        f"@ {alert.target_price} (current: {current_price})"
                    )

            await session.commit()
            return triggered, False

    # ── Alert trigger logic ───────────────────────────────────────

    def is_triggered(
        self, alert: PriceAlert, current_price: float, previous_price: float | None
    ) -> Literal["SL", "TP", "TARGET", "INVALIDATED"] | None:
        target = float(alert.target_price)

        # LONG: SL below / TP above. SHORT mirrors it: SL above / TP below.
        is_short = alert.direction == "SHORT"
        if alert.type == "SL":
            hit = current_price >= target if is_short else current_price <= target
            return "SL" if hit else None
        if alert.type == "TP":
            hit = current_price <= target if is_short else current_price >= target
            return "TP" if hit else None
        if alert.type == "TARGET":
            invalidation = alert.invalidation_price
            if invalidation is not None:
                # Tie-break: INVALIDATED wins on same-tick ambiguity (matches
                # RetracementZoneService.check_tick's convention).
                if self._level_crossed(float(invalidation), current_price, previous_price):
                    return "INVALIDATED"
                if self._level_crossed(target, current_price, previous_price):
                    return "TARGET"
                return None
            if self._level_crossed(target, current_price, previous_price):
                return "TARGET"
            return None
        return None

    @staticmethod
    def _level_crossed(level: float, current_price: float, previous_price: float | None) -> bool:
        if previous_price is not None:
            crossed_up = previous_price < level <= current_price
            crossed_down = previous_price > level >= current_price
            return crossed_up or crossed_down
        return current_price == level

    # ── Build alert notification message ─────────────────────────

    def build_alert_message(
        self,
        alert: PriceAlert,
        current_price: float,
        outcome: Literal["SL", "TP", "TARGET", "INVALIDATED"] | None = None,
    ) -> str:
        wat_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
        formatted_time = wat_time.strftime("%Y-%m-%d %H:%M") + " WAT"
        note_line = f"📝 <b>Note:</b> {html.escape(alert.note)}\n" if alert.note is not None else ""

        if outcome == "INVALIDATED":
            return (
                f"🚫 <b>TARGET INVALIDATED — {alert.symbol}</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🛑 <b>Invalidation Level:</b> {alert.invalidation_price}\n"
                f"📍 <b>Target Level:</b> {alert.target_price}\n"
                f"💰 <b>Current Price:</b> {current_price}\n"
                f"🕐 <b>Time:</b> {formatted_time}\n"
                f"{note_line}"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⚠️ Price hit the invalidation level before the target."
            )

        is_short = alert.direction == "SHORT"

        if alert.type == "SL":
            return (
                f"🔴 <b>STOP LOSS HIT ({alert.direction} position) — {alert.symbol}</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💀 <b>SL Level:</b> {alert.target_price}\n"
                f"{'📈' if is_short else '📉'} <b>Current Price:</b> {current_price}\n"
                f"🕐 <b>Time:</b> {formatted_time}\n"
                f"{note_line}"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"⚠️ Cut your losses. Protect your capital."
            )
        if alert.type == "TP":
            return (
                f"🟢 <b>TAKE PROFIT HIT ({alert.direction} position) — {alert.symbol}</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 <b>TP Level:</b> {alert.target_price}\n"
                f"{'📉' if is_short else '📈'} <b>Current Price:</b> {current_price}\n"
                f"🕐 <b>Time:</b> {formatted_time}\n"
                f"{note_line}"
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
            f"{note_line}"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Your target level has been reached."
        )

    def get_emoji(self, type_: str) -> str:
        return {"SL": "🔴", "TP": "🟢", "TARGET": "🎯"}[type_]

    # ── Supported pairs list message ──────────────────────────────

    def get_supported_pairs_message(self) -> str:
        return f"📋 <b>Supported Pairs</b>\n\n{' • '.join(sorted(ALLOWED_SYMBOLS))}"
