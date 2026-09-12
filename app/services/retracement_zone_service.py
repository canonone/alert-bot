import datetime
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from sqlalchemy import select

from app.db import SessionLocal
from app.models import RetracementZone
from app.services.market_data_service import MarketDataService
from app.services.users_service import UsersService

logger = logging.getLogger("RetracementZoneService")

SendMessage = Callable[[str, str], Awaitable[None]]

CANDLE_INTERVAL = "4h"
BOUNDARY_SAMPLE_SIZE = 6  # how many candles to pull when logging the observed boundary alignment
POLL_LOOKBACK = 2  # candles fetched on every routine poll — just enough to see the latest closed one


def _to_wat(dt: datetime.datetime) -> datetime.datetime:
    # Matches the WAT formatting convention in price_alert_service.build_alert_message
    return dt.astimezone(datetime.timezone.utc) + datetime.timedelta(hours=1)


@dataclass
class ZoneState:
    id: int
    pair: str
    candle_start_utc: datetime.datetime
    bias: str
    high: float
    low: float
    level_50: float
    zone_entry_triggered: bool
    invalidated: bool

    @classmethod
    def from_model(cls, zone: RetracementZone) -> "ZoneState":
        return cls(
            id=zone.id,
            pair=zone.pair,
            candle_start_utc=zone.candle_start_utc,
            bias=zone.bias,
            high=zone.high,
            low=zone.low,
            level_50=zone.level_50,
            zone_entry_triggered=zone.zone_entry_triggered,
            invalidated=zone.invalidated,
        )


class RetracementZoneService:
    def __init__(
        self,
        api_key: str,
        pairs: list[str],
        market_data: MarketDataService,
        users_service: UsersService,
        send_message: SendMessage,
    ) -> None:
        self.api_key = api_key
        self.pairs = pairs
        self.market_data = market_data
        self.users_service = users_service
        self.send_message = send_message

        # In-memory mirror of each pair's current zone row, refreshed on every DB write —
        # avoids a DB round trip on every single price tick.
        self._active: dict[str, ZoneState] = {}

    # ── Startup: resume from DB, or seed from the latest closed candle ─────

    async def bootstrap(self) -> None:
        for pair in self.pairs:
            zone = await self._load_latest_zone_from_db(pair)
            if zone is not None:
                self._active[pair] = ZoneState.from_model(zone)
                logger.info(
                    f"[RetracementZone] Resumed {pair} zone from candle "
                    f"{_to_wat(zone.candle_start_utc):%Y-%m-%d %H:%M} WAT (bias={zone.bias})"
                )
            else:
                await self._refresh_pair(pair, log_boundary=True)

    async def _load_latest_zone_from_db(self, pair: str) -> RetracementZone | None:
        async with SessionLocal() as session:
            result = await session.execute(
                select(RetracementZone)
                .where(RetracementZone.pair == pair)
                .order_by(RetracementZone.candle_start_utc.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    # ── Routine poll: detect a newly closed 4H candle ───────────────────────

    async def poll_all_pairs(self) -> None:
        for pair in self.pairs:
            try:
                await self._refresh_pair(pair)
            except Exception as error:
                logger.error(f"[RetracementZone] Poll failed for {pair}: {error}")

    async def _refresh_pair(self, pair: str, log_boundary: bool = False) -> None:
        outputsize = BOUNDARY_SAMPLE_SIZE if log_boundary else POLL_LOOKBACK
        candles, quota_exceeded = await self.market_data.get_candles(
            pair, CANDLE_INTERVAL, self.api_key, outputsize=outputsize
        )
        if quota_exceeded:
            logger.warning(f"[RetracementZone] Twelve Data quota exceeded while polling {pair}")
            return
        if not candles:
            logger.warning(f"[RetracementZone] No candle data returned for {pair} — market may be closed")
            return

        if log_boundary:
            self._log_boundary_alignment(pair, candles)

        latest = candles[-1]  # oldest-first, so the last entry is the most recent

        # Twelve Data may include the still-forming bar as the latest entry. Only treat a
        # candle as "closed" once its 4H window has fully elapsed.
        now = datetime.datetime.now(datetime.timezone.utc)
        if latest["datetime"] + datetime.timedelta(hours=4) > now:
            if len(candles) < 2:
                return
            latest = candles[-2]

        current_state = self._active.get(pair)
        if current_state is not None and current_state.candle_start_utc == latest["datetime"]:
            return  # already tracking this candle — no new close yet (e.g. weekend market closure)

        candle_end = latest["datetime"] + datetime.timedelta(hours=4)
        await self._create_zone_for_candle(pair, latest, candle_end)

    def _log_boundary_alignment(self, pair: str, candles: list[dict]) -> None:
        timestamps = [c["datetime"] for c in candles]
        offsets = sorted(
            {
                int((dt - dt.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() // 60) % 240
                for dt in timestamps
            }
        )
        logger.info(
            f"[RetracementZone] {pair} 4H boundary sample (UTC): "
            + ", ".join(dt.strftime("%Y-%m-%d %H:%M") for dt in timestamps)
        )
        logger.info(
            f"[RetracementZone] {pair} observed minute-of-4h-cycle offset(s): {offsets} "
            "(a single consistent value confirms the true boundary alignment)"
        )

    # ── Classification + zone calculation ───────────────────────────────────

    async def _create_zone_for_candle(
        self, pair: str, candle: dict, candle_end: datetime.datetime
    ) -> None:
        o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
        candle_range = h - l

        bias = "bullish" if c > o else ("bearish" if c < o else "neutral")
        is_neutral = bias == "neutral"

        # Direction-independent: L + 0.5*range == H - 0.5*range. Bias is kept for messaging
        # and for the (still directional) invalidation rule below — not for this trigger price.
        level_50 = l + 0.5 * candle_range

        async with SessionLocal() as session:
            zone = RetracementZone(
                pair=pair,
                candle_start_utc=candle["datetime"],
                candle_end_utc=candle_end,
                open=o,
                high=h,
                low=l,
                close=c,
                bias=bias,
                range=candle_range,
                level_50=level_50,
                invalidated=is_neutral,  # neutral candles have no valid directional zone to monitor
            )
            session.add(zone)
            await session.commit()
            await session.refresh(zone)

        wat_start = _to_wat(candle["datetime"])
        self._active[pair] = ZoneState.from_model(zone)

        if is_neutral:
            logger.info(
                f"[RetracementZone] {pair} candle at {wat_start:%Y-%m-%d %H:%M} WAT was neutral "
                "(O==C) — skipping zone monitoring"
            )
            return

        logger.info(
            f"[RetracementZone] {pair} new {bias} zone from candle {wat_start:%Y-%m-%d %H:%M} WAT — "
            f"O:{o} H:{h} L:{l} C:{c} range:{candle_range:.5f} level_50:{level_50:.5f}"
        )
        await self._notify_new_zone(pair, zone)

    # ── Real-time tick monitoring ────────────────────────────────────────────

    async def check_tick(self, symbol: str, price: float, previous_price: float | None) -> None:
        state = self._active.get(symbol)
        if state is None or state.invalidated or state.zone_entry_triggered:
            return
        if state.bias not in ("bullish", "bearish"):
            return

        is_bullish = state.bias == "bullish"
        opposite_extreme = state.low if is_bullish else state.high
        beyond_opposite = price <= opposite_extreme if is_bullish else price >= opposite_extreme
        reached_50 = price <= state.level_50 if is_bullish else price >= state.level_50

        if beyond_opposite:
            await self._apply_event(state, "INVALIDATED", price)
        elif reached_50:
            await self._apply_event(state, "ZONE_ENTRY", price)

    async def _apply_event(self, state: ZoneState, event: str, price: float) -> None:
        async with SessionLocal() as session:
            db_zone = await session.get(RetracementZone, state.id)
            if db_zone is None:
                return

            if event == "ZONE_ENTRY":
                if db_zone.zone_entry_triggered:
                    return
                db_zone.zone_entry_triggered = True
                state.zone_entry_triggered = True
            elif event == "INVALIDATED":
                if db_zone.invalidated:
                    return
                db_zone.invalidated = True
                state.invalidated = True

            await session.commit()

        logger.info(f"[RetracementZone] {state.pair} {event} @ {price} (candle {state.candle_start_utc})")
        await self._notify_event(state, event, price)

    # ── Notifications ────────────────────────────────────────────────────────

    async def _notify_new_zone(self, pair: str, zone: RetracementZone) -> None:
        message = self._format_message(pair, zone.bias, zone.level_50, "Waiting for price to enter zone")
        await self._broadcast(message)

    async def _notify_event(self, state: ZoneState, event: str, price: float) -> None:
        if event == "ZONE_ENTRY":
            status = f"Price entered zone at {price:.2f}"
        else:
            status = f"Zone invalidated — price broke beyond {price:.2f}"

        message = self._format_message(state.pair, state.bias, state.level_50, status)
        await self._broadcast(message)

    def _format_message(self, pair: str, bias: str, level_50: float, status: str) -> str:
        emoji = "🟡" if pair == "XAUUSD" else "📊"
        bias_label = "Bullish" if bias == "bullish" else "Bearish"
        return (
            f"{emoji} <b>{pair} 4H Zone Alert</b>\n\n"
            f"Bias: <b>{bias_label}</b> (prior candle)\n"
            f"Trigger: <b>{level_50:.2f}</b> (50% level)\n"
            f"Status: {status}"
        )

    async def _broadcast(self, message: str) -> None:
        subscribers = await self.users_service.find_zone_alert_subscribers()
        for user in subscribers:
            await self.send_message(user.chat_id, message)

    # ── /xauzone status command ──────────────────────────────────────────────

    def get_status_message(self, pair: str) -> str:
        pair = pair.upper()
        if pair not in self.pairs:
            return f"❌ <b>{pair}</b> is not a configured retracement-zone pair.\n\nConfigured: {', '.join(self.pairs)}"

        state = self._active.get(pair)
        if state is None:
            return f"⏳ No 4H zone computed yet for <b>{pair}</b>. Check back after the next candle close."

        emoji = "🟡" if pair == "XAUUSD" else "📊"
        wat_start = _to_wat(state.candle_start_utc)

        if state.bias == "neutral":
            return (
                f"{emoji} <b>{pair} 4H Zone</b>\n\n"
                f"Candle: <b>{wat_start:%Y-%m-%d %H:%M} WAT</b>\n"
                "Prior candle was neutral (open == close) — no directional zone to monitor."
            )

        bias_label = "Bullish" if state.bias == "bullish" else "Bearish"

        if state.invalidated:
            status = "❌ Invalidated"
        elif state.zone_entry_triggered:
            status = f"✅ Zone entered — level {state.level_50:.2f}"
        else:
            status = "⏳ Waiting for price to enter zone"

        return (
            f"{emoji} <b>{pair} 4H Zone</b>\n\n"
            f"Candle: <b>{wat_start:%Y-%m-%d %H:%M} WAT</b>\n"
            f"Bias: <b>{bias_label}</b> (prior candle)\n"
            f"Trigger: <b>{state.level_50:.2f}</b> (50% level)\n"
            f"Status: {status}"
        )
