"""Daily candles and the forex trading calendar, as used by SMT divergence: the 17:00-New-York trading day, synthetic
daily candles built from Twelve Data 1H bars, the weekend window, and pip / price / time formatting. No I/O."""
import datetime
from dataclasses import dataclass, field, replace
from zoneinfo import ZoneInfo

# The user's charts are FOREX.com on TradingView, where the forex day rolls over at 17:00
# America/New_York. Twelve Data's own `1day` candles use midnight UTC, which doesn't match, so
# we build the daily candle ourselves from 1H bars.
# zoneinfo handles the US daylight-saving change, so no UTC offset is hard-coded anywhere.
NY = ZoneInfo("America/New_York")
UTC = datetime.timezone.utc
DAY_ROLL_HOUR_NY = 17

ONE_H_INTERVAL = "1h"
# A week of bars: enough to reach the last complete trading day (and the one before it) across a
# weekend, as Twelve Data keeps quoting through Saturday and Sunday
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

# The daily levels are recorded at 17:10 New York, once the final 1H bar has been published
SCAN_HOUR_NY = 17
SCAN_MINUTE_NY = 10

# Forex/gold markets close for the weekend: Friday 17:00 through Sunday 17:00 New York time.
# Twelve Data keeps quoting through this window — the 1H bars have real ranges and the price can
# drift tens of pips away from Friday's close — so they are dropped purely by time, not by looking
# at the prices. New York time is used so the window follows the daylight-saving shift.
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

_PIP_EPS = 1e-6  # float slack when comparing pip distances to a threshold


# ── Formatting and pips ─────────────────────────────────────────────────────

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


# ── Trading-day calendar ────────────────────────────────────────────────────

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


# ── 1H -> daily aggregation ─────────────────────────────────────────────────

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
    (`open_from_prev_close` False). High, low and close always come from the bars.
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


def bars_since(candles_1h: list[dict], since_utc: datetime.datetime) -> list[dict]:
    """The 1H bars starting at/after `since_utc`, oldest first, including the still-forming bar and skipping the
    weekend window."""
    bars = [b for b in candles_1h if b["datetime"] >= since_utc and not is_market_closed(b["datetime"])]
    return sorted(bars, key=lambda b: b["datetime"])
