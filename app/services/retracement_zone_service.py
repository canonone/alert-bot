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

# Twelve Data's native `interval=4h` endpoint closes bars at 01/05/09/13/17/21:00 UTC — one hour
# off the user's real broker feed (FOREX.com, viewed on TradingView), confirmed by comparing
# real chart timestamps against a manually-drawn line on a live trade. FOREX.com's 4H bars close
# at 03/07/11/15/19/23:00 WAT (UTC+1) = 02/06/10/14/18/22:00 UTC. Twelve Data can't be parameterized
# to that boundary directly, so instead we build synthetic 4H bars by aggregating four consecutive
# 1H candles — 1H bars are always hour-aligned, so this reproduces the correct boundary exactly.
#
# Per-pair override lives in PAIR_FOUR_H_BOUNDARY_HOURS_UTC — if a future pair trades through a
# broker/feed with a different alignment, add it there; nothing else needs to change.
DEFAULT_FOUR_H_BOUNDARY_HOURS_UTC = [2, 6, 10, 14, 18, 22]
PAIR_FOUR_H_BOUNDARY_HOURS_UTC: dict[str, list[int]] = {}

ONE_H_INTERVAL = "1h"
ONE_H_BOUNDARY_SAMPLE_SIZE = 24  # ~1 day of 1H candles, logged once on bootstrap for verification
ONE_H_POLL_LOOKBACK = 10  # comfortably covers the last 2 full synthetic 4H buckets on every poll

# Forex/gold markets close for the weekend and reopen on a fixed weekly schedule. Roughly
# Friday 21:00 UTC through Sunday 21:00 UTC. During this window Twelve Data keeps returning
# "complete" 1H candles for a closed market — same last-traded price repeated with tiny float
# jitter — which would otherwise look like a genuine new 4H bar every boundary. Weekday numbers
# follow datetime.weekday(): Monday=0 ... Sunday=6.
MARKET_CLOSE_WEEKDAY_UTC = 4  # Friday
MARKET_CLOSE_HOUR_UTC = 21
MARKET_REOPEN_WEEKDAY_UTC = 6  # Sunday
MARKET_REOPEN_HOUR_UTC = 21

# Backstop for cases the calendar window above doesn't anticipate (holidays, unexpected feed
# gaps, a broker closing/reopening slightly off our assumed schedule): if a synthetic 4H
# candle's range is suspiciously small, the feed is almost certainly repeating a frozen
# last-traded price rather than reporting a real session. Thresholds sit well below a normal
# quiet-hour range for the asset, but comfortably above the sub-pip/sub-cent jitter a frozen
# closed-market feed produces. Per-pair since typical range varies wildly by asset class.
DEFAULT_DEGENERATE_RANGE_THRESHOLD = 0.0005  # ~5 pips — most FX pairs
PAIR_DEGENERATE_RANGE_THRESHOLD: dict[str, float] = {
    "XAUUSD": 0.05,  # gold: real 4H ranges typically span several dollars; a nickel is far
    # below even the quietest genuine session
}


def _to_wat(dt: datetime.datetime) -> datetime.datetime:
    # Matches the WAT formatting convention in price_alert_service.build_alert_message
    return dt.astimezone(datetime.timezone.utc) + datetime.timedelta(hours=1)


def _boundary_hours_for(pair: str) -> list[int]:
    return PAIR_FOUR_H_BOUNDARY_HOURS_UTC.get(pair, DEFAULT_FOUR_H_BOUNDARY_HOURS_UTC)


def _degenerate_range_threshold_for(pair: str) -> float:
    return PAIR_DEGENERATE_RANGE_THRESHOLD.get(pair, DEFAULT_DEGENERATE_RANGE_THRESHOLD)


def _is_market_closed(close_time_utc: datetime.datetime) -> bool:
    """True if `close_time_utc` (a candle's close timestamp, UTC) falls within the weekly
    broker-closed window: Friday MARKET_CLOSE_HOUR_UTC through Sunday MARKET_REOPEN_HOUR_UTC.

    Represented as minutes since the start of the week (Monday 00:00 UTC) so the comparison
    is a single range check — the window doesn't wrap past the week boundary since Friday
    comes before Sunday.
    """
    minute_of_week = close_time_utc.weekday() * 24 * 60 + close_time_utc.hour * 60 + close_time_utc.minute
    close_start = MARKET_CLOSE_WEEKDAY_UTC * 24 * 60 + MARKET_CLOSE_HOUR_UTC * 60
    reopen_start = MARKET_REOPEN_WEEKDAY_UTC * 24 * 60 + MARKET_REOPEN_HOUR_UTC * 60
    return close_start <= minute_of_week < reopen_start


def _bucket_start(dt: datetime.datetime, boundary_hours: list[int]) -> datetime.datetime:
    # Boundary hours are spaced exactly 4 apart, so they share one value mod 4 — that value is
    # also the hour every synthetic bucket starts on (start = close - 4h, and -4 ≡ 0 mod 4).
    offset = boundary_hours[0] % 4
    hour_start = dt.replace(minute=0, second=0, microsecond=0)
    shift = (hour_start.hour - offset) % 4
    return hour_start - datetime.timedelta(hours=shift)


def _aggregate_1h_to_4h(candles_1h: list[dict], boundary_hours: list[int]) -> list[dict]:
    """Groups closed, hour-aligned 1H candles (oldest-first) into synthetic 4H bars.

    A bucket is only emitted once all 4 of its constituent hourly candles are present and
    contiguous — a bucket straddling a weekend close/open (or any data gap) is simply skipped,
    which is what "closed" and "market closed" mean here in the absence of a native 4H bar.
    """
    buckets: dict[datetime.datetime, list[dict]] = {}
    for candle in candles_1h:
        start = _bucket_start(candle["datetime"], boundary_hours)
        buckets.setdefault(start, []).append(candle)

    aggregated: list[dict] = []
    for start in sorted(buckets):
        members = sorted(buckets[start], key=lambda c: c["datetime"])
        expected_hours = [start + datetime.timedelta(hours=i) for i in range(4)]
        if [m["datetime"] for m in members] != expected_hours:
            continue
        aggregated.append(
            {
                "datetime": start,
                "open": members[0]["open"],
                "close": members[-1]["close"],
                "high": max(m["high"] for m in members),
                "low": min(m["low"] for m in members),
            }
        )
    return aggregated


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

        # Pairs for which we've already logged "entering market-closed window" — cleared on
        # reopen so the message logs once per closed period, not on every 5-minute poll.
        self._market_closed_logged: set[str] = set()

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
        outputsize = ONE_H_BOUNDARY_SAMPLE_SIZE if log_boundary else ONE_H_POLL_LOOKBACK
        candles_1h, quota_exceeded = await self.market_data.get_candles(
            pair, ONE_H_INTERVAL, self.api_key, outputsize=outputsize
        )
        if quota_exceeded:
            logger.warning(f"[RetracementZone] Twelve Data quota exceeded while polling {pair}")
            return
        if not candles_1h:
            logger.warning(f"[RetracementZone] No candle data returned for {pair} — market may be closed")
            return

        # Twelve Data may include the still-forming bar as the latest entry — only aggregate
        # 1H candles whose hour has fully elapsed.
        now = datetime.datetime.now(datetime.timezone.utc)
        closed_1h = [c for c in candles_1h if c["datetime"] + datetime.timedelta(hours=1) <= now]

        boundary_hours = _boundary_hours_for(pair)
        synthetic = _aggregate_1h_to_4h(closed_1h, boundary_hours)
        if not synthetic:
            logger.warning(f"[RetracementZone] No complete synthetic 4H bar yet for {pair}")
            return

        if log_boundary:
            self._log_boundary_alignment(pair, synthetic)

        latest = synthetic[-1]  # oldest-first, so the last entry is the most recently closed bar
        candle_end = latest["datetime"] + datetime.timedelta(hours=4)

        if _is_market_closed(candle_end):
            if pair not in self._market_closed_logged:
                logger.info(
                    f"[RetracementZone] {pair} entering market-closed window (candle closing "
                    f"{_to_wat(candle_end):%Y-%m-%d %H:%M} WAT) — suppressing zone creation "
                    "until the market reopens"
                )
                self._market_closed_logged.add(pair)
            return  # leave whatever zone is already tracked untouched — no notification
        self._market_closed_logged.discard(pair)

        current_state = self._active.get(pair)
        if current_state is not None and current_state.candle_start_utc == latest["datetime"]:
            return  # already tracking this candle — no new close yet

        await self._create_zone_for_candle(pair, latest, candle_end)

    def _log_boundary_alignment(self, pair: str, synthetic_candles: list[dict]) -> None:
        logger.info(
            f"[RetracementZone] {pair} synthetic 4H bars aggregated from 1H data — confirmed "
            f"FOREX.com boundary, closes at {_boundary_hours_for(pair)} UTC:"
        )
        for candle in synthetic_candles:
            start_utc = candle["datetime"]
            end_utc = start_utc + datetime.timedelta(hours=4)
            logger.info(
                f"[RetracementZone] {pair}  {start_utc:%Y-%m-%d %H:%M} -> {end_utc:%H:%M} UTC "
                f"({_to_wat(end_utc):%H:%M} WAT close)"
            )

    # ── Classification + zone calculation ───────────────────────────────────

    async def _create_zone_for_candle(
        self, pair: str, candle: dict, candle_end: datetime.datetime
    ) -> None:
        o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
        candle_range = h - l

        threshold = _degenerate_range_threshold_for(pair)
        if candle_range < threshold:
            wat_start = _to_wat(candle["datetime"])
            logger.info(
                f"[RetracementZone] {pair} candle at {wat_start:%Y-%m-%d %H:%M} WAT has a "
                f"degenerate range ({candle_range:.5f} < {threshold} threshold) — treating as "
                "stale/frozen data (market closed?) and skipping zone creation"
            )
            return  # leave whatever zone is already tracked untouched — no notification

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
