import asyncio
import datetime
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import SessionLocal
from app.models import DailySweepScanState, DailySweepSetup
from app.services.market_data_service import MarketDataService
from app.services.price_alert_service import ALLOWED_SYMBOLS
from app.services.users_service import UsersService

logger = logging.getLogger("DailySweepService")

SendMessage = Callable[[str, str], Awaitable[None]]
SubscribeSymbol = Callable[[str], Awaitable[None]]

SELL = "SELL"
BUY = "BUY"
ACTIVE = "ACTIVE"
TRIGGERED = "TRIGGERED"
EXPIRED = "EXPIRED"

SWEEP_KIND = "SWEEP"
CONTINUATION_KIND = "CONTINUATION"

# The user's charts are FOREX.com on TradingView, where the forex day rolls over at 17:00
# America/New_York. Twelve Data's own `1day` candles use midnight UTC, which doesn't match, so
# (like the old 4H feature did for 4H bars) we build the daily candle ourselves from 1H bars.
# zoneinfo handles the US daylight-saving change, so no UTC offset is hard-coded anywhere.
NY = ZoneInfo("America/New_York")
UTC = datetime.timezone.utc
DAY_ROLL_HOUR_NY = 17

ONE_H_INTERVAL = "1h"
# Day 1 + Day 2 plus the day before Day 1 (its close is Day 1's open), even across a weekend with
# Twelve Data's Sat/Sun bars (it keeps quoting through the weekend): Fri, 48 weekend bars, Mon, Tue
# ~ 120+, so leave headroom
ONE_H_LOOKBACK = 168

# Twelve Data's free tier allows 8 requests/minute — one request per pair, spaced 8s apart
SCAN_REQUEST_SPACING_SECONDS = 8.0
SCAN_RETRY_DELAY_SECONDS = 15 * 60

# A Finnhub tick older than this isn't trusted for the "level already touched" check
FRESH_PRICE_MAX_AGE = datetime.timedelta(minutes=2)

# A trading day counts as complete when its final bar (16:00-17:00 New York) is present AND it has
# at least this many of its ~24 bars. Not "all 24": gold pauses 17:00-18:00 New York every day (23
# bars), and any pair can occasionally miss a bar. Per-pair override for pairs that need different.
DEFAULT_MIN_DAILY_BARS = 22
PAIR_MIN_DAILY_BARS: dict[str, int] = {}

# Run the scheduled scan at 17:10 New York; a startup catch-up must not race it
SCAN_HOUR_NY = 17
SCAN_MINUTE_NY = 10

# Forex/gold markets close for the weekend: Friday 17:00 through Sunday 17:00 New York time.
# Twelve Data keeps quoting through this window — the 1H bars have real ranges and the price can
# drift tens of pips away from Friday's close — so they are dropped purely by time, not by looking
# at the prices. (The old 4H feature used a fixed UTC window; New York time is used here so the
# window follows the daylight-saving shift.)
# Weekday numbers follow datetime.weekday(): Monday=0 ... Sunday=6.
MARKET_CLOSE_WEEKDAY_NY = 4  # Friday
MARKET_REOPEN_WEEKDAY_NY = 6  # Sunday

# Backstop for cases the calendar window above doesn't anticipate (holidays, unexpected feed
# gaps): if a daily candle's range is suspiciously small the feed is almost certainly repeating
# a frozen last-traded price rather than reporting a real session. Thresholds sit well below a
# normal daily range but above the sub-pip/sub-cent jitter a frozen feed produces.
DEFAULT_DEGENERATE_RANGE_THRESHOLD = 0.0005  # ~5 pips — most FX pairs
PAIR_DEGENERATE_RANGE_THRESHOLD: dict[str, float] = {
    "XAUUSD": 0.05,
}


# ── Pure helpers: time, formatting ──────────────────────────────────────────

def _to_wat(dt: datetime.datetime) -> datetime.datetime:
    # Matches the WAT formatting convention in price_alert_service.build_alert_message
    return dt.astimezone(UTC) + datetime.timedelta(hours=1)


def _wat_str(dt: datetime.datetime) -> str:
    return f"{_to_wat(dt):%Y-%m-%d %H:%M} WAT"


def price_decimals(pair: str) -> int:
    if pair == "XAUUSD":
        return 2
    if pair.endswith("JPY"):
        return 3
    return 5


def format_price(pair: str, value: float) -> str:
    return f"{value:,.{price_decimals(pair)}f}"


def pip_size(pair: str) -> float:
    if pair.endswith("JPY"):
        return 0.01
    if pair == "XAUUSD":
        return 0.1
    return 0.0001


# How far the first 1H bar's open may sit from the previous close before /sweepcheck shows both
OPEN_DIFF_NOTE_PIPS = 2


def min_daily_bars_for(pair: str) -> int:
    return PAIR_MIN_DAILY_BARS.get(pair, DEFAULT_MIN_DAILY_BARS)


def _degenerate_range_threshold_for(pair: str) -> float:
    return PAIR_DEGENERATE_RANGE_THRESHOLD.get(pair, DEFAULT_DEGENERATE_RANGE_THRESHOLD)


def is_market_closed(candle_start_utc: datetime.datetime) -> bool:
    """True if a 1H candle starting at `candle_start_utc` falls within the weekly broker-closed
    window: Friday 17:00 through Sunday 17:00 New York time.

    Keyed on the candle's START so the last real Friday bar (16:00-17:00) stays open and the
    first Sunday-session bar (17:00-18:00) is open. Represented as minutes since the start of the
    week (Monday 00:00 NY) so it's a single range check — the window never wraps the week boundary.
    """
    ny = candle_start_utc.astimezone(NY)
    minute_of_week = ny.weekday() * 24 * 60 + ny.hour * 60 + ny.minute
    close_start = MARKET_CLOSE_WEEKDAY_NY * 24 * 60 + DAY_ROLL_HOUR_NY * 60
    reopen_start = MARKET_REOPEN_WEEKDAY_NY * 24 * 60 + DAY_ROLL_HOUR_NY * 60
    return close_start <= minute_of_week < reopen_start


# ── Pure helpers: trading-day calendar ──────────────────────────────────────

def is_trading_day(day: datetime.date) -> bool:
    return day.weekday() < 5


def previous_trading_day(day: datetime.date) -> datetime.date:
    day -= datetime.timedelta(days=1)
    while not is_trading_day(day):
        day -= datetime.timedelta(days=1)
    return day


def next_trading_day(day: datetime.date) -> datetime.date:
    day += datetime.timedelta(days=1)
    while not is_trading_day(day):
        day += datetime.timedelta(days=1)
    return day


def trading_day_label(candle_start_utc: datetime.datetime) -> datetime.date:
    """The trading day a 1H candle belongs to. A trading day runs 17:00 -> 17:00 New York time and
    is labelled by the date it CLOSES on, so Sunday evening's session belongs to Monday and the
    Friday candle is the one that opened Thursday 17:00."""
    ny = candle_start_utc.astimezone(NY)
    if ny.hour >= DAY_ROLL_HOUR_NY:
        return ny.date() + datetime.timedelta(days=1)
    return ny.date()


def trading_day_bounds(label: datetime.date) -> tuple[datetime.datetime, datetime.datetime]:
    """(start, end) of a trading day in UTC. Built from New York wall-clock times, so the UTC
    offset follows daylight saving automatically."""
    close_time = datetime.time(DAY_ROLL_HOUR_NY)
    end = datetime.datetime.combine(label, close_time, tzinfo=NY)
    start = datetime.datetime.combine(label - datetime.timedelta(days=1), close_time, tzinfo=NY)
    return start.astimezone(UTC), end.astimezone(UTC)


def latest_closed_trading_day(now_utc: datetime.datetime) -> datetime.date:
    day = now_utc.astimezone(NY).date()
    while True:
        if is_trading_day(day) and trading_day_bounds(day)[1] <= now_utc:
            return day
        day -= datetime.timedelta(days=1)


def day3_expiry(day2_date: datetime.date) -> datetime.datetime:
    """Day 3 is the next trading day after Day 2 (Monday if Day 2 is Friday); the setup lives
    until Day 3 closes."""
    return trading_day_bounds(next_trading_day(day2_date))[1]


def is_expired(expires_at: datetime.datetime, now_utc: datetime.datetime) -> bool:
    return now_utc >= expires_at


# ── Pure: 1H -> daily aggregation ───────────────────────────────────────────

@dataclass(frozen=True)
class DailyCandle:
    date: datetime.date  # trading day, labelled by the NY date it closes on
    start_utc: datetime.datetime
    end_utc: datetime.datetime
    open: float
    high: float
    low: float
    close: float
    missing_hours: tuple[datetime.datetime, ...] = field(default=())
    bar_count: int = 24
    has_final_bar: bool = True
    min_bars: int = DEFAULT_MIN_DAILY_BARS
    # The first 1H bar's open (None = same as `open`). `open` itself is the previous trading day's
    # close whenever that day is available — see aggregate_1h_to_daily.
    first_bar_open: float | None = None
    open_from_prev_close: bool = False

    @property
    def is_complete(self) -> bool:
        return self.has_final_bar and self.bar_count >= self.min_bars

    @property
    def incomplete_reason(self) -> str | None:
        if not self.has_final_bar:
            return "final bar (16:00–17:00 New York) is missing"
        if self.bar_count < self.min_bars:
            return f"only {self.bar_count} bars, need at least {self.min_bars}"
        return None

    @property
    def range(self) -> float:
        return self.high - self.low


def aggregate_1h_to_daily(
    candles_1h: list[dict], now_utc: datetime.datetime, min_bars: int = DEFAULT_MIN_DAILY_BARS
) -> list[DailyCandle]:
    """Groups hour-aligned 1H candles (oldest-first) into synthetic 17:00-NY trading days.

    Skipped entirely: the still-forming hour, market-closed (weekend) bars, and trading days that
    haven't closed yet. Days short on bars are still returned (`is_complete` False, `missing_hours`
    listing every absent hour) so callers can diagnose them — use `usable_daily_candles` to keep
    only complete days.

    OPEN: FOREX.com's daily open is exactly the previous day's close (Monday's too: the weekend
    reopen print is not used), but a 1H feed's first bar of a day can open away from it (Monday
    most of all). So each day's open is set to the previous trading day's close when that day is
    present and complete; otherwise it falls back to the first 1H bar's open
    (`open_from_prev_close` False — callers log that). High, low and close always come from the bars.
    """
    one_hour = datetime.timedelta(hours=1)
    buckets: dict[datetime.date, list[dict]] = {}
    for candle in candles_1h:
        start = candle["datetime"]
        if start + one_hour > now_utc:
            continue
        if is_market_closed(start):
            continue
        label = trading_day_label(start)
        if not is_trading_day(label):
            continue
        buckets.setdefault(label, []).append(candle)

    daily: list[DailyCandle] = []
    for label in sorted(buckets):
        day_start, day_end = trading_day_bounds(label)
        if day_end > now_utc:
            continue
        members = sorted(
            (m for m in buckets[label] if day_start <= m["datetime"] < day_end),
            key=lambda c: c["datetime"],
        )
        if not members:
            continue
        present = {m["datetime"] for m in members}
        hours_in_day = int((day_end - day_start) / one_hour)
        missing = tuple(
            day_start + one_hour * i for i in range(hours_in_day) if day_start + one_hour * i not in present
        )
        daily.append(
            DailyCandle(
                date=label,
                start_utc=day_start,
                end_utc=day_end,
                open=members[0]["open"],
                high=max(m["high"] for m in members),
                low=min(m["low"] for m in members),
                close=members[-1]["close"],
                missing_hours=missing,
                bar_count=len(present),
                has_final_bar=(day_end - one_hour) in present,
                min_bars=min_bars,
                first_bar_open=members[0]["open"],
            )
        )

    by_date = {d.date: d for d in daily}
    result: list[DailyCandle] = []
    for day in daily:
        previous = by_date.get(previous_trading_day(day.date))
        if previous is not None and previous.is_complete:
            day = replace(day, open=previous.close, open_from_prev_close=True)
        result.append(day)
    return result


def usable_daily_candles(daily: list[DailyCandle], pair: str) -> list[DailyCandle]:
    """Keeps only complete days whose range isn't suspiciously small (frozen feed)."""
    threshold = _degenerate_range_threshold_for(pair)
    return [d for d in daily if d.is_complete and d.range >= threshold]


def last_two_days(usable: list[DailyCandle]) -> tuple[DailyCandle, DailyCandle] | None:
    """The two most recent usable days, only if they're consecutive trading days (Fri -> Mon
    counts as consecutive). A skipped day in between means Day 1 isn't really Day 2's neighbour."""
    if len(usable) < 2:
        return None
    day1, day2 = usable[-2], usable[-1]
    if previous_trading_day(day2.date) != day1.date:
        return None
    return day1, day2


# ── Pure: pattern detection ─────────────────────────────────────────────────

@dataclass(frozen=True)
class SweepSignal:
    direction: str  # SELL | BUY
    level: float  # Day 2's open
    sweep_extreme: float  # Day 2 high (SELL) / Day 2 low (BUY) — invalidation idea
    kind: str = SWEEP_KIND
    level2: float | None = None


@dataclass(frozen=True)
class ContinuationSignal:
    direction: str  # BUY | SELL
    level: float  # Level 1: Day 1's high (BUY) / low (SELL) — the broken extreme
    level2: float  # Level 2: Day 2's open (always beyond Level 1)
    sweep_extreme: float  # Day 2 low (BUY) / high (SELL) — invalidation idea
    kind: str = CONTINUATION_KIND


def sweep_checks(day1: DailyCandle, day2: DailyCandle, direction: str) -> list[tuple[str, bool]]:
    """Every strict condition for `direction`, labelled — used by detect_sweep and /sweepcheck."""
    if direction == SELL:
        return [
            ("Day 1 closed bullish (close > open)", day1.close > day1.open),
            ("Day 2 closed bearish (close < open)", day2.close < day2.open),
            ("Day 2 swept Day 1's high (high > Day 1 high)", day2.high > day1.high),
            ("Day 2 did not sweep Day 1's low (low > Day 1 low)", day2.low > day1.low),
        ]
    return [
        ("Day 1 closed bearish (close < open)", day1.close < day1.open),
        ("Day 2 closed bullish (close > open)", day2.close > day2.open),
        ("Day 2 swept Day 1's low (low < Day 1 low)", day2.low < day1.low),
        ("Day 2 did not sweep Day 1's high (high < Day 1 high)", day2.high < day1.high),
    ]


def detect_sweep(day1: DailyCandle, day2: DailyCandle) -> SweepSignal | None:
    """Both candles must already be fully closed — the caller guarantees it."""
    if all(ok for _, ok in sweep_checks(day1, day2, SELL)):
        return SweepSignal(SELL, day2.open, day2.high)
    if all(ok for _, ok in sweep_checks(day1, day2, BUY)):
        return SweepSignal(BUY, day2.open, day2.low)
    return None


# Results of classify_continuation
CONT_BUY = "CONTINUATION BUY"
CONT_SELL = "CONTINUATION SELL"
SKIP_GAP = "skipped (gap)"
SKIP_OPPOSITE = "skipped (took opposite side)"
NO_CONTINUATION = "none"


def continuation_checks(day1: DailyCandle, day2: DailyCandle, direction: str) -> list[tuple[str, bool]]:
    """Every strict condition for a continuation, labelled. The first three are the pattern itself;
    the last two are the exclusions (took the opposite side / gapped past Day 1's extreme).

    The gap exclusion can effectively never fire on candles built by aggregate_1h_to_daily: Day 2's
    open is Day 1's close, which can't be at or beyond Day 1's high/low unless Day 1 closed exactly
    on it. It stays as a harmless guard (and for candles whose open fell back to the first bar)."""
    if direction == BUY:
        return [
            ("Day 1 closed bullish (close > open)", day1.close > day1.open),
            ("Day 2 closed bullish (close > open)", day2.close > day2.open),
            ("Day 2 closed above Day 1's high (close > Day 1 high)", day2.close > day1.high),
            ("Day 2 did not take Day 1's low (low > Day 1 low)", day2.low > day1.low),
            ("Day 2 did not gap above Day 1's high (open < Day 1 high)", day2.open < day1.high),
        ]
    return [
        ("Day 1 closed bearish (close < open)", day1.close < day1.open),
        ("Day 2 closed bearish (close < open)", day2.close < day2.open),
        ("Day 2 closed below Day 1's low (close < Day 1 low)", day2.close < day1.low),
        ("Day 2 did not take Day 1's high (high < Day 1 high)", day2.high < day1.high),
        ("Day 2 did not gap below Day 1's low (open > Day 1 low)", day2.open > day1.low),
    ]


def classify_continuation(day1: DailyCandle, day2: DailyCandle) -> str:
    for direction, label in ((BUY, CONT_BUY), (SELL, CONT_SELL)):
        checks = continuation_checks(day1, day2, direction)
        if all(ok for _, ok in checks[:3]):
            if not checks[3][1]:
                return SKIP_OPPOSITE
            if not checks[4][1]:
                return SKIP_GAP
            return label
    return NO_CONTINUATION


def detect_continuation(day1: DailyCandle, day2: DailyCandle) -> ContinuationSignal | None:
    """Both candles must already be fully closed — the caller guarantees it."""
    result = classify_continuation(day1, day2)
    if result == CONT_BUY:
        return ContinuationSignal(BUY, day1.high, day2.open, day2.low)
    if result == CONT_SELL:
        return ContinuationSignal(SELL, day1.low, day2.open, day2.high)
    return None


def level_reached(direction: str, level: float, price: float) -> bool:
    return price >= level if direction == SELL else price <= level


def should_catch_up(last_scanned_day: datetime.date | None, now_utc: datetime.datetime) -> bool:
    """Startup decision: has the most recent closed trading day gone unscanned while its Day 3 is
    still running? Skipped while the scheduled 17:10 scan for that day is still to come."""
    expected_day = latest_closed_trading_day(now_utc)
    if last_scanned_day is not None and last_scanned_day >= expected_day:
        return False
    scheduled = datetime.datetime.combine(
        expected_day, datetime.time(SCAN_HOUR_NY, SCAN_MINUTE_NY), tzinfo=NY
    ).astimezone(UTC)
    if now_utc < scheduled:
        return False
    return not is_expired(day3_expiry(expected_day), now_utc)


def explain_no_setup(day1: DailyCandle, day2: DailyCandle) -> str:
    """Why detect_sweep returned None, judged against the direction Day 1's colour points to."""
    if day1.close > day1.open:
        direction = SELL
    elif day1.close < day1.open:
        direction = BUY
    else:
        return "Day 1 closed flat (close == open)"
    failed = [label for label, ok in sweep_checks(day1, day2, direction) if not ok]
    return f"{direction} checks failed: " + "; ".join(failed)


# ── Pure: message formatting ────────────────────────────────────────────────

@dataclass
class SetupState:
    id: int
    pair: str
    direction: str
    day1: tuple[float, float, float, float]  # O, H, L, C
    day2: tuple[float, float, float, float]
    level: float
    day2_date: datetime.date
    day3_expires_at: datetime.datetime
    kind: str = SWEEP_KIND
    level2: float | None = None  # CONTINUATION only
    stage: int = 0  # alerts already sent

    @property
    def sweep_extreme(self) -> float:
        """Day 2 high for SELL / Day 2 low for BUY — the invalidation / SL idea for both patterns."""
        return self.day2[1] if self.direction == SELL else self.day2[2]

    @property
    def final_stage(self) -> int:
        return 2 if self.kind == CONTINUATION_KIND else 1

    def next_stage(self, price: float) -> int | None:
        """The stage this price would move the setup to, or None if it fires nothing. A price past
        both continuation levels at stage 0 jumps straight to 2 (one combined message)."""
        if self.stage >= self.final_stage:
            return None
        if self.stage == 0:
            if not level_reached(self.direction, self.level, price):
                return None
            if self.kind == CONTINUATION_KIND and level_reached(self.direction, self.level2, price):
                return 2
            return 1
        return 2 if level_reached(self.direction, self.level2, price) else None

    @classmethod
    def from_model(cls, row: DailySweepSetup) -> "SetupState":
        return cls(
            id=row.id,
            pair=row.pair,
            direction=row.direction,
            day1=(row.day1_open, row.day1_high, row.day1_low, row.day1_close),
            day2=(row.day2_open, row.day2_high, row.day2_low, row.day2_close),
            level=row.level,
            day2_date=row.day2_date,
            day3_expires_at=row.day3_expires_at,
            kind=row.kind,
            level2=row.level2,
            stage=row.stage,
        )


def _ohlc_str(pair: str, ohlc: tuple[float, float, float, float]) -> str:
    o, h, l, c = (format_price(pair, v) for v in ohlc)
    return f"O {o} · H {h} · L {l} · C {c}"


def format_new_setups_digest(
    setups: list[SetupState], include_sweeps: bool = True, include_continuations: bool = True
) -> str | None:
    """One message per user, with only the sections they opted into. None if there's nothing to show."""
    sweeps = [x for x in setups if x.kind == SWEEP_KIND] if include_sweeps else []
    continuations = [x for x in setups if x.kind == CONTINUATION_KIND] if include_continuations else []
    shown = sweeps + continuations
    if not shown:
        return None

    expiries = {x.day3_expires_at for x in shown}
    suffix = lambda x: f" · until {_wat_str(x.day3_expires_at)}" if len(expiries) > 1 else ""  # noqa: E731

    lines = ["🗓 <b>Daily Setups</b>", "━━━━━━━━━━━━━━━━━━━━"]
    if sweeps:
        lines.append("<b>🔁 Sweeps</b>")
        for x in sweeps:
            emoji = "🔻" if x.direction == SELL else "🔺"
            side = "high" if x.direction == SELL else "low"
            lines.append(
                f"{emoji} <b>{x.pair}</b> {x.direction} @ {format_price(x.pair, x.level)}"
                f" · sweep {side} {format_price(x.pair, x.sweep_extreme)}{suffix(x)}"
            )
    if sweeps and continuations:
        lines.append("")
    if continuations:
        lines.append("<b>📈 Continuations</b>")
        for x in continuations:
            emoji = "🔻" if x.direction == SELL else "🔺"
            side = "high" if x.direction == BUY else "low"
            lines.append(f"{emoji} <b>{x.pair}</b> {x.direction}")
            lines.append(f"   ① Day 1 {side}: {format_price(x.pair, x.level)}")
            lines.append(f"   ② Day 2 open: {format_price(x.pair, x.level2)}{suffix(x)}")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    if len(expiries) == 1:
        lines.append(f"⏳ Valid until Day 3 close: {_wat_str(next(iter(expiries)))}")
    else:
        lines.append("⏳ Each setup is valid until its Day 3 close")
    return "\n".join(lines)


def format_level_hit(
    setup: SetupState, price: float, now_utc: datetime.datetime, pre_armed: bool = False
) -> str:
    pair = setup.pair
    is_sell = setup.direction == SELL
    extreme_line = (
        f"🔝 <b>Day 2 High (sweep):</b> {format_price(pair, setup.sweep_extreme)} — invalidation / SL idea"
        if is_sell
        else f"🔻 <b>Day 2 Low (sweep):</b> {format_price(pair, setup.sweep_extreme)} — invalidation / SL idea"
    )
    explanation = (
        "⚠️ Price retraced to the Day 2 open after a bearish sweep of Day 1's high."
        if is_sell
        else "⚠️ Price retraced to the Day 2 open after a bullish sweep of Day 1's low."
    )
    return (
        f"🎯 <b>DAILY SWEEP LEVEL HIT — {pair} ({setup.direction})</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>Level (Day 2 open):</b> {format_price(pair, setup.level)}\n"
        f"💰 <b>Current Price:</b> {format_price(pair, price)}\n"
        f"{extreme_line}\n"
        f"📅 <b>Day 1:</b> {_ohlc_str(pair, setup.day1)}\n"
        f"📅 <b>Day 2:</b> {_ohlc_str(pair, setup.day2)}\n"
        f"🕐 <b>Time:</b> {_wat_str(now_utc)}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{explanation}"
        + (
            "\n⚡ Level was reached before the setup was armed (between Day 2 close and the scan)."
            if pre_armed
            else ""
        )
    )


def format_continuation_hit(
    setup: SetupState,
    price: float,
    now_utc: datetime.datetime,
    hit: str,  # "1" | "2" | "both"
    pre_armed: bool = False,
) -> str:
    pair = setup.pair
    is_buy = setup.direction == BUY
    broken = "High" if is_buy else "Low"
    stop_side = "Low" if is_buy else "High"
    l1 = format_price(pair, setup.level)
    l2 = format_price(pair, setup.level2)
    current = f"💰 <b>Current Price:</b> {format_price(pair, price)}"
    level1_line = f"① <b>Day 1 {broken} (broken):</b> {l1}"
    stop_line = f"🛡 <b>Day 2 {stop_side}:</b> {format_price(pair, setup.sweep_extreme)} — invalidation / SL idea"

    if hit == "1":
        title = "CONTINUATION · LEVEL 1 HIT"
        body = [level1_line, current, f"② <b>Next level — Day 2 Open:</b> {l2}", stop_line]
        close_word = "bullish close above" if is_buy else "bearish close below"
        explanation = f"✅ Price pulled back to the broken Day 1 {broken.lower()} after a {close_word} it."
    elif hit == "2":
        title = "CONTINUATION · LEVEL 2 HIT"
        body = [f"② <b>Day 2 Open:</b> {l2}", current, level1_line, stop_line]
        explanation = (
            f"⚠️ Day 1's {broken.lower()} didn't hold — price is now at Day 2's open, "
            "the deepest level for this setup."
        )
    else:
        title = "CONTINUATION · LEVELS 1 & 2 HIT"
        body = [level1_line, f"② <b>Day 2 Open:</b> {l2}", current, stop_line]
        explanation = (
            f"⚠️ Price went through the broken Day 1 {broken.lower()} and on to Day 2's open — "
            "the deepest level for this setup."
        )
    return (
        f"🎯 <b>{title} — {pair} ({setup.direction})</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        + "\n".join(body)
        + "\n"
        f"📅 <b>Day 1:</b> {_ohlc_str(pair, setup.day1)}\n"
        f"📅 <b>Day 2:</b> {_ohlc_str(pair, setup.day2)}\n"
        f"🕐 <b>Time:</b> {_wat_str(now_utc)}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{explanation}"
        + ("\n⚡ Level was reached before the setup was armed (between Day 2 close and the scan)." if pre_armed else "")
    )


def open_note(pair: str, candle: DailyCandle) -> str:
    """How the candle's open was decided; shows the first 1H bar's open too when it's far off."""
    if not candle.open_from_prev_close:
        return "⚠️ open = first 1H bar (previous trading day not available)"
    first = candle.first_bar_open
    if first is not None:
        pips = abs(first - candle.open) / pip_size(pair)
        if pips > OPEN_DIFF_NOTE_PIPS:
            return (
                f"open = previous close ({format_price(pair, candle.open)}); "
                f"first 1H bar opened {format_price(pair, first)} ({pips:.1f} pips apart)"
            )
    return "open = previous close"


def format_candle_line(pair: str, label: str, candle: DailyCandle) -> str:
    if not candle.is_complete:
        warning = f" ⚠️ incomplete — {candle.incomplete_reason}"
    elif candle.missing_hours:
        warning = f" ℹ️ {len(candle.missing_hours)} bar(s) missing, accepted"
    else:
        warning = ""
    return (
        f"<b>{label} — {candle.date:%a %Y-%m-%d}</b>{warning}\n"
        f"   {_wat_str(candle.start_utc)} → {_wat_str(candle.end_utc)}\n"
        f"   {_ohlc_str(pair, (candle.open, candle.high, candle.low, candle.close))}\n"
        f"   {open_note(pair, candle)}"
    )


# ── Service ─────────────────────────────────────────────────────────────────

@dataclass
class PairScan:
    pair: str
    status: str  # new | none | duplicate | lag | quota | nodata | gap | stale
    setup: SetupState | None = None


@dataclass
class ScanReport:
    results: list[PairScan]

    @property
    def new_setups(self) -> list[SetupState]:
        return [r.setup for r in self.results if r.status == "new" and r.setup is not None]

    @property
    def retry_pairs(self) -> list[str]:
        # Data lag, quota errors and empty responses are all worth one more try later
        return [r.pair for r in self.results if r.status in ("lag", "quota", "nodata")]

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for r in self.results:
            counts[r.status] = counts.get(r.status, 0) + 1
        labels = {
            "new": "new setups",
            "none": "no pattern",
            "duplicate": "already stored",
            "lag": "data not caught up",
            "quota": "quota errors",
            "nodata": "no data",
            "gap": "non-consecutive/unusable days",
            "stale": "already past Day 3",
        }
        parts = [f"{counts[k]} {labels[k]}" for k in labels if k in counts]
        return f"Scanned {len(self.results)} pair(s): " + ", ".join(parts)


class DailySweepService:
    def __init__(
        self,
        api_key: str,
        market_data: MarketDataService,
        users_service: UsersService,
        send_message: SendMessage,
    ) -> None:
        self.api_key = api_key
        self.market_data = market_data
        self.users_service = users_service
        self.send_message = send_message
        self.pairs = sorted(ALLOWED_SYMBOLS)

        # Wired up in main.py once the Finnhub service exists — subscribes a pair to the live
        # feed when a setup for it is created (pairs may have no normal price alerts).
        self.subscribe_symbol: SubscribeSymbol | None = None

        # In-memory mirror of ACTIVE setups by pair, updated on every create/trigger/expire —
        # ticks never touch the database.
        self._active: dict[str, list[SetupState]] = {}
        self._scan_lock = asyncio.Lock()
        # Injectable so tests can pin "now"
        self.clock: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(UTC)
        # Wired up in main.py to FinnhubPriceService.get_last_tick: (price, UTC receive time) or None
        self.get_last_price: Callable[[str], tuple[float, datetime.datetime] | None] = lambda symbol: None
        # Monotonic time of the latest Twelve Data request, so every request respects the spacing
        self._last_td_request_at = float("-inf")

    @property
    def scan_running(self) -> bool:
        return self._scan_lock.locked()

    # ── Startup: resume from DB ──────────────────────────────────────────────

    async def bootstrap(self) -> None:
        await self.expire_setups()
        async with SessionLocal() as session:
            result = await session.execute(select(DailySweepSetup).where(DailySweepSetup.status == ACTIVE))
            rows = list(result.scalars().all())
        self._active.clear()
        for row in rows:
            self._cache_add(SetupState.from_model(row))
        logger.info(f"[DailySweep] Resumed {len(rows)} active setup(s) from the database")

    def active_symbols(self) -> set[str]:
        return {pair for pair, setups in self._active.items() if setups}

    def _cache_add(self, setup: SetupState) -> None:
        self._active.setdefault(setup.pair, []).append(setup)

    def _cache_remove(self, setup_id: int) -> None:
        for pair in list(self._active):
            self._active[pair] = [s for s in self._active[pair] if s.id != setup_id]
            if not self._active[pair]:
                del self._active[pair]

    # ── Expiry ───────────────────────────────────────────────────────────────

    async def expire_setups(self) -> int:
        now = self.clock()
        async with SessionLocal() as session:
            result = await session.execute(
                update(DailySweepSetup)
                .where(DailySweepSetup.status == ACTIVE, DailySweepSetup.day3_expires_at <= now)
                .values(status=EXPIRED)
                .returning(
                    DailySweepSetup.id,
                    DailySweepSetup.pair,
                    DailySweepSetup.direction,
                    DailySweepSetup.kind,
                    DailySweepSetup.stage,
                )
            )
            expired = result.all()
            await session.commit()
        # Silent by design: expiry is logged, never messaged (also when a continuation got only Level 1)
        for setup_id, pair, direction, kind, stage in expired:
            self._cache_remove(setup_id)
            logger.info(
                f"[DailySweep] {pair} {kind} {direction} setup #{setup_id} expired at stage {stage} (Day 3 ended)"
            )
        return len(expired)

    # ── Scan ─────────────────────────────────────────────────────────────────

    async def run_scan(self, pairs: list[str] | None = None) -> ScanReport:
        """Fetch candles for each pair, create setups, then send ONE digest if any are new."""
        async with self._scan_lock:
            await self.expire_setups()
            scan_pairs = pairs if pairs is not None else self.pairs
            expected_day = latest_closed_trading_day(self.clock())
            logger.info(
                f"[DailySweep] Scan starting for {len(scan_pairs)} pair(s); "
                f"latest closed trading day = {expected_day}"
            )

            results: list[PairScan] = []
            for index, pair in enumerate(scan_pairs):
                if index > 0:
                    await asyncio.sleep(SCAN_REQUEST_SPACING_SECONDS)
                try:
                    results.append(await self._scan_pair(pair, expected_day))
                except Exception as error:
                    logger.error(f"[DailySweep] Scan failed for {pair}: {error}")
                    results.append(PairScan(pair, "nodata"))

            report = ScanReport(results)
            logger.info(f"[DailySweep] {report.summary()}")

            new_setups = report.new_setups
            if new_setups:
                await self._broadcast_digest(new_setups)
                # After the digest, so the "level hit" message reads as a follow-up to it
                for setup in new_setups:
                    await self._trigger_if_already_reached(setup)

            if not report.retry_pairs:
                await self._record_scan(expected_day)
            return report

    async def _trigger_if_already_reached(self, setup: SetupState) -> None:
        """Price may already be at/through the level(s) between Day 2's close and the scan."""
        price = await self._price_for_arming(setup.pair)
        if price is None or setup.next_stage(price) is None:
            return
        logger.info(f"[DailySweep] {setup.pair} level {setup.level} already reached (last price {price}) when armed")
        await self._trigger(setup, price, self.clock(), pre_armed=True)

    async def _price_for_arming(self, pair: str) -> float | None:
        """Latest Finnhub tick if it's fresh, else the current Twelve Data price, else None."""
        tick = self.get_last_price(pair)
        if tick is not None:
            price, received_at = tick
            age = self.clock() - received_at
            if age <= FRESH_PRICE_MAX_AGE:
                return price
            logger.info(f"[DailySweep] {pair} last Finnhub tick is {age.total_seconds():.0f}s old — fetching from Twelve Data")
        else:
            logger.info(f"[DailySweep] No Finnhub tick yet for {pair} — fetching from Twelve Data")

        await self._throttle_twelve_data()
        try:
            price, quota_exceeded = await self.market_data.get_current_price(pair, self.api_key)
        except Exception as error:
            logger.error(f"[DailySweep] Current price fetch failed for {pair}: {error}")
            price, quota_exceeded = None, False
        finally:
            self._last_td_request_at = time.monotonic()
        if price is None:
            reason = "quota exceeded" if quota_exceeded else "no price returned"
            logger.warning(f"[DailySweep] No current price for {pair} ({reason}) — setup armed without a touched-level check")
        return price

    async def _throttle_twelve_data(self) -> None:
        wait = self._last_td_request_at + SCAN_REQUEST_SPACING_SECONDS - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

    # ── Catch-up bookkeeping ─────────────────────────────────────────────────

    async def get_last_scanned_day(self) -> datetime.date | None:
        async with SessionLocal() as session:
            state = await session.get(DailySweepScanState, 1)
            return state.last_scanned_day if state is not None else None

    async def _record_scan(self, day: datetime.date) -> None:
        """Remember the trading day whose scan fully completed (no pair left to retry)."""
        try:
            stmt = pg_insert(DailySweepScanState).values(id=1, last_scanned_day=day)
            stmt = stmt.on_conflict_do_update(
                index_elements=[DailySweepScanState.id],
                set_={"last_scanned_day": day, "updated_at": self.clock()},
                where=DailySweepScanState.last_scanned_day < day,
            )
            async with SessionLocal() as session:
                await session.execute(stmt)
                await session.commit()
        except Exception as error:
            logger.error(f"[DailySweep] Could not record completed scan for {day}: {error}")

    async def needs_catch_up(self) -> bool:
        return should_catch_up(await self.get_last_scanned_day(), self.clock())

    async def _scan_pair(self, pair: str, expected_day: datetime.date) -> PairScan:
        candles_1h, quota_exceeded = await self.market_data.get_candles(
            pair, ONE_H_INTERVAL, self.api_key, outputsize=ONE_H_LOOKBACK
        )
        self._last_td_request_at = time.monotonic()
        if quota_exceeded:
            logger.warning(f"[DailySweep] Twelve Data quota exceeded while scanning {pair}")
            return PairScan(pair, "quota")
        if not candles_1h:
            logger.warning(f"[DailySweep] No candle data returned for {pair}")
            return PairScan(pair, "nodata")

        now = self.clock()
        usable = usable_daily_candles(aggregate_1h_to_daily(candles_1h, now, min_daily_bars_for(pair)), pair)
        for label, candle in zip(("Day 1", "Day 2"), usable[-2:] if len(usable) >= 2 else usable):
            logger.info(
                f"[DailySweep] {pair} {label} {candle.date} "
                f"({_wat_str(candle.start_utc)} → {_wat_str(candle.end_utc)}) "
                f"O:{candle.open} H:{candle.high} L:{candle.low} C:{candle.close} "
                f"[{open_note(pair, candle)}]"
            )
            if not candle.open_from_prev_close:
                logger.warning(
                    f"[DailySweep] {pair} {label} {candle.date}: previous trading day unavailable — "
                    f"using the first 1H bar's open {candle.open}"
                )

        if not usable or usable[-1].date != expected_day:
            latest = usable[-1].date if usable else None
            logger.warning(
                f"[DailySweep] {pair} latest complete trading day is {latest}, expected {expected_day} — "
                "data lag or missing hours"
            )
            return PairScan(pair, "lag")

        days = last_two_days(usable)
        if days is None:
            logger.warning(f"[DailySweep] {pair} has no consecutive Day 1/Day 2 — skipping")
            return PairScan(pair, "gap")
        day1, day2 = days

        # The same candles feed both features; a Day 2 can match at most one of the four patterns
        signal: SweepSignal | ContinuationSignal | None = detect_sweep(day1, day2) or detect_continuation(day1, day2)
        if signal is None:
            logger.info(
                f"[DailySweep] {pair} no setup — sweep: {explain_no_setup(day1, day2)}; "
                f"continuation: {classify_continuation(day1, day2)}"
            )
            return PairScan(pair, "none")

        expires_at = day3_expiry(day2.date)
        if is_expired(expires_at, now):
            return PairScan(pair, "stale")

        setup = await self._store_setup(pair, signal, day1, day2, expires_at)
        if setup is None:
            logger.info(f"[DailySweep] {pair} setup for Day 2 {day2.date} already stored")
            return PairScan(pair, "duplicate")

        self._cache_add(setup)
        logger.info(
            f"[DailySweep] {pair} NEW {signal.kind} {signal.direction} setup #{setup.id} level "
            f"{signal.level}" + (f" / {signal.level2}" if signal.level2 is not None else "")
            + f" (Day 2 {day2.date}) valid until {_wat_str(expires_at)}"
        )
        if self.subscribe_symbol is not None:
            try:
                await self.subscribe_symbol(pair)
            except Exception as error:
                logger.error(f"[DailySweep] Could not subscribe {pair} to the live feed: {error}")
        return PairScan(pair, "new", setup)

    async def _store_setup(
        self,
        pair: str,
        signal: SweepSignal | ContinuationSignal,
        day1: DailyCandle,
        day2: DailyCandle,
        expires_at: datetime.datetime,
    ) -> SetupState | None:
        values = dict(
            pair=pair,
            kind=signal.kind,
            direction=signal.direction,
            day1_open=day1.open, day1_high=day1.high, day1_low=day1.low, day1_close=day1.close,
            day2_open=day2.open, day2_high=day2.high, day2_low=day2.low, day2_close=day2.close,
            level=signal.level,
            level2=signal.level2,
            stage=0,
            day2_date=day2.date,
            day3_expires_at=expires_at,
            status=ACTIVE,
        )
        async with SessionLocal() as session:
            # (pair, day2_date) is unique — a rescan or retry hits the conflict and inserts nothing
            result = await session.execute(
                pg_insert(DailySweepSetup)
                .values(**values)
                .on_conflict_do_nothing(constraint="uq_daily_sweep_pair_day2")
                .returning(DailySweepSetup.id)
            )
            setup_id = result.scalar_one_or_none()
            await session.commit()
        if setup_id is None:
            return None
        return SetupState(
            id=setup_id,
            pair=pair,
            direction=signal.direction,
            day1=(day1.open, day1.high, day1.low, day1.close),
            day2=(day2.open, day2.high, day2.low, day2.close),
            level=signal.level,
            day2_date=day2.date,
            day3_expires_at=expires_at,
            kind=signal.kind,
            level2=signal.level2,
        )

    # ── Real-time tick monitoring ────────────────────────────────────────────

    async def check_tick(self, symbol: str, price: float, previous_price: float | None) -> None:
        setups = self._active.get(symbol)
        if not setups:
            return
        now = self.clock()
        for setup in list(setups):
            if is_expired(setup.day3_expires_at, now):
                continue  # expiry job hasn't run yet — never fire after Day 3 has closed
            if setup.next_stage(price) is not None:
                await self._trigger(setup, price, now)

    async def _trigger(
        self, setup: SetupState, price: float, now: datetime.datetime, pre_armed: bool = False
    ) -> None:
        """Move the setup to the stage this price reaches and send that stage's message — once."""
        to_stage = setup.next_stage(price)
        if to_stage is None:
            return
        from_stage = setup.stage
        complete = to_stage >= setup.final_stage

        # Advance in memory BEFORE awaiting the database, so a duplicate or concurrent tick can't
        # re-enter this stage; a finished setup also leaves the cache.
        setup.stage = to_stage
        if complete:
            self._cache_remove(setup.id)
        try:
            claimed = await self._claim_stage(setup, from_stage, to_stage, price, now)
        except Exception as error:
            logger.error(f"[DailySweep] Could not record setup #{setup.id} stage {to_stage}, will retry on next tick: {error}")
            setup.stage = from_stage
            if complete:
                self._cache_add(setup)
            return
        if not claimed:
            # The database says another path already advanced or expired it — never send
            self._cache_remove(setup.id)
            return

        logger.info(
            f"[DailySweep] {setup.pair} {setup.kind} {setup.direction} stage {from_stage}->{to_stage} "
            f"@ {price} (setup #{setup.id})"
        )
        if setup.kind == CONTINUATION_KIND:
            hit = "both" if (from_stage == 0 and to_stage == 2) else str(to_stage)
            message = format_continuation_hit(setup, price, now, hit, pre_armed)
        else:
            message = format_level_hit(setup, price, now, pre_armed)
        await self._broadcast(message, setup.kind)

    async def _claim_stage(
        self, setup: SetupState, from_stage: int, to_stage: int, price: float, now: datetime.datetime
    ) -> bool:
        """Atomic: only the caller that moves the row from `from_stage` (while still ACTIVE and not
        past Day 3) gets rowcount 1, so a restart or duplicate tick can never re-send a stage."""
        status = TRIGGERED if to_stage >= setup.final_stage else ACTIVE
        async with SessionLocal() as session:
            result = await session.execute(
                update(DailySweepSetup)
                .where(
                    DailySweepSetup.id == setup.id,
                    DailySweepSetup.status == ACTIVE,
                    DailySweepSetup.stage == from_stage,
                    DailySweepSetup.day3_expires_at > now,
                )
                .values(stage=to_stage, status=status, triggered_at=now, trigger_price=price)
            )
            claimed = result.rowcount == 1
            await session.commit()
        return claimed

    # ── Notifications ────────────────────────────────────────────────────────

    async def _broadcast(self, message: str, kind: str = SWEEP_KIND) -> None:
        if kind == CONTINUATION_KIND:
            subscribers = await self.users_service.find_daily_continuation_subscribers()
        else:
            subscribers = await self.users_service.find_daily_sweep_subscribers()
        for user in subscribers:
            await self.send_message(user.chat_id, message)

    async def _broadcast_digest(self, setups: list[SetupState]) -> None:
        """ONE digest per scan; each user only sees the sections they opted into."""
        for user in await self.users_service.find_digest_subscribers():
            message = format_new_setups_digest(
                setups,
                include_sweeps=user.daily_sweep_alerts_enabled,
                include_continuations=user.daily_continuation_alerts_enabled,
            )
            if message is not None:
                await self.send_message(user.chat_id, message)

    # ── Commands ─────────────────────────────────────────────────────────────

    async def list_active(self) -> list[SetupState]:
        now = self.clock()
        async with SessionLocal() as session:
            result = await session.execute(
                select(DailySweepSetup)
                .where(DailySweepSetup.status == ACTIVE, DailySweepSetup.day3_expires_at > now)
                .order_by(DailySweepSetup.day3_expires_at, DailySweepSetup.pair)
            )
            return [SetupState.from_model(row) for row in result.scalars().all()]

    async def get_sweeps_message(self) -> str:
        setups = await self.list_active()
        if not setups:
            return "🗓 <b>Daily Setups</b>\n\nNo active setups right now."
        lines = ["🗓 <b>Daily Setups — Active</b>", "━━━━━━━━━━━━━━━━━━━━"]
        for s in setups:
            emoji = "🔻" if s.direction == SELL else "🔺"
            if s.kind == CONTINUATION_KIND:
                side = "high" if s.direction == BUY else "low"
                hit1 = "✅ hit" if s.stage >= 1 else "⏳ waiting"
                hit2 = "✅ hit" if s.stage >= 2 else "⏳ waiting" if s.stage == 1 else "⏳ after ①"
                lines.append(
                    f"📈 CONTINUATION {emoji} <b>{s.pair}</b> {s.direction}\n"
                    f"   ① Day 1 {side}: {format_price(s.pair, s.level)} — {hit1}\n"
                    f"   ② Day 2 open: {format_price(s.pair, s.level2)} — {hit2}\n"
                    f"   Expires: {_wat_str(s.day3_expires_at)}"
                )
            else:
                lines.append(
                    f"🔁 SWEEP {emoji} <b>{s.pair}</b> {s.direction} @ {format_price(s.pair, s.level)}\n"
                    f"   Expires: {_wat_str(s.day3_expires_at)}"
                )
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        return "\n".join(lines)

    async def get_check_message(self, pair: str) -> str:
        pair = pair.upper()
        if pair not in ALLOWED_SYMBOLS:
            return f"❌ <b>{pair}</b> is not a supported pair. See /pairs."

        candles_1h, quota_exceeded = await self.market_data.get_candles(
            pair, ONE_H_INTERVAL, self.api_key, outputsize=ONE_H_LOOKBACK
        )
        if quota_exceeded:
            return "❌ Twelve Data quota exceeded — try again in a minute."
        if not candles_1h:
            return f"❌ No candle data returned for <b>{pair}</b>."

        daily = aggregate_1h_to_daily(candles_1h, self.clock(), min_daily_bars_for(pair))
        if len(daily) < 2:
            return f"⏳ Fewer than two completed daily candles available for <b>{pair}</b>."

        day1, day2 = daily[-2], daily[-1]
        lines = [
            f"🗓 <b>{pair} — last two daily candles</b>",
            "(built from 1H data, 17:00 New York → 17:00 New York)",
            "━━━━━━━━━━━━━━━━━━━━",
            format_candle_line(pair, "Day 1", day1),
            format_candle_line(pair, "Day 2", day2),
            "━━━━━━━━━━━━━━━━━━━━",
        ]
        if not (day1.is_complete and day2.is_complete):
            bad = next(d for d in (day1, day2) if not d.is_complete)
            gap = f", first gap {_wat_str(bad.missing_hours[0])}" if bad.missing_hours else ""
            lines.append(f"⚠️ Not evaluated: {bad.date:%a %Y-%m-%d} is incomplete — {bad.incomplete_reason}{gap}.")
        elif previous_trading_day(day2.date) != day1.date:
            lines.append("⚠️ Not evaluated: the two candles are not consecutive trading days.")
        else:
            threshold = _degenerate_range_threshold_for(pair)
            if day1.range < threshold or day2.range < threshold:
                lines.append("⚠️ Not evaluated: a candle has a degenerate range (frozen/stale data?).")
            else:
                signal = detect_sweep(day1, day2)
                if signal is None:
                    lines.append(f"🔁 Sweep: no setup — {explain_no_setup(day1, day2)}")
                else:
                    emoji = "🔻" if signal.direction == SELL else "🔺"
                    lines.append(
                        f"{emoji} <b>Sweep {signal.direction} setup</b> — level (Day 2 open) "
                        f"{format_price(pair, signal.level)}, valid until "
                        f"{_wat_str(day3_expiry(day2.date))}"
                    )
                continuation = detect_continuation(day1, day2)
                if continuation is None:
                    lines.append(f"📈 Continuation: {classify_continuation(day1, day2)}")
                else:
                    emoji = "🔻" if continuation.direction == SELL else "🔺"
                    side = "high" if continuation.direction == BUY else "low"
                    lines.append(
                        f"{emoji} <b>Continuation {continuation.direction} setup</b> — "
                        f"① Day 1 {side} {format_price(pair, continuation.level)}, "
                        f"② Day 2 open {format_price(pair, continuation.level2)}, valid until "
                        f"{_wat_str(day3_expiry(day2.date))}"
                    )
        return "\n".join(lines)
