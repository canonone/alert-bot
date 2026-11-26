"""The shared daily candles and trading calendar (app/services/daily_candles.py), plus the candle helpers other
test files import."""
import datetime
from zoneinfo import ZoneInfo

import app.services.daily_candles as dc
from app.services.daily_candles import (
    previous_trading_day,
    DailyCandle,
    aggregate_1h_to_daily,
    format_price,
    is_market_closed,
    latest_closed_trading_day,
    trading_day_bounds,
    trading_day_label,
    usable_daily_candles,
)

UTC = datetime.timezone.utc
NY = ZoneInfo("America/New_York")
HOUR = datetime.timedelta(hours=1)
D = datetime.date


def utc(y, mo, d, h=0, mi=0):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=UTC)


def ny(y, mo, d, h=0, mi=0):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=NY).astimezone(UTC)


def candle(day: datetime.date, o, h, l, c) -> DailyCandle:
    start, end = trading_day_bounds(day)
    return DailyCandle(date=day, start_utc=start, end_utc=end, open=o, high=h, low=l, close=c)


def day_hours(label: datetime.date, o, h, l, c, skip=()):
    """24 (or DST-adjusted) 1H bars for a trading day, first open=o, last close=c, one bar
    carrying the high and one the low. `skip` = hour indexes to leave out."""
    start, end = trading_day_bounds(label)
    n = int((end - start) / HOUR)
    mid = (o + c) / 2
    bars = []
    for i in range(n):
        if i in skip:
            continue
        bars.append(
            {
                "datetime": start + HOUR * i,
                "open": o if i == 0 else mid,
                "close": c if i == n - 1 else mid,
                "high": h if i == 1 else mid,
                "low": l if i == 2 else mid,
            }
        )
    return bars


# ── Calendar, aggregation, completeness ─────────────────────────────────────

def test_trading_day_label_summer():
    # EDT (UTC-4): 17:00 NY = 21:00 UTC
    assert trading_day_label(utc(2026, 10, 28, 20)) == D(2026, 10, 28)  # 16:00 NY Wed
    assert trading_day_label(utc(2026, 10, 28, 21)) == D(2026, 10, 29)  # 17:00 NY -> Thursday's candle


def test_trading_day_label_winter():
    # EST (UTC-5): 17:00 NY = 22:00 UTC, after the 1 Nov 2026 change
    assert trading_day_label(utc(2026, 11, 3, 21)) == D(2026, 11, 3)  # 16:00 NY Tue
    assert trading_day_label(utc(2026, 11, 3, 22)) == D(2026, 11, 4)  # 17:00 NY -> Wednesday's candle


def test_bounds_shift_one_hour_across_dst():
    # Fri 30 Oct (EDT): Thu 21:00 UTC -> Fri 21:00 UTC. Mon 2 Nov (EST): Sun 22:00 UTC -> Mon 22:00 UTC.
    assert trading_day_bounds(D(2026, 10, 30)) == (utc(2026, 10, 29, 21), utc(2026, 10, 30, 21))
    assert trading_day_bounds(D(2026, 11, 2)) == (utc(2026, 11, 1, 22), utc(2026, 11, 2, 22))


def test_aggregation_on_both_sides_of_dst_change():
    fri = D(2026, 10, 30)  # EDT
    mon = D(2026, 11, 2)  # EST
    candles = day_hours(fri, 100, 120, 80, 110) + day_hours(mon, 110, 130, 90, 95)
    now = utc(2026, 11, 3, 12)
    daily = aggregate_1h_to_daily(candles, now)
    assert [d.date for d in daily] == [fri, mon]
    assert all(d.is_complete for d in daily)
    assert (daily[0].open, daily[0].high, daily[0].low, daily[0].close) == (100, 120, 80, 110)
    assert (daily[1].open, daily[1].high, daily[1].low, daily[1].close) == (110, 130, 90, 95)
    assert daily[0].end_utc == utc(2026, 10, 30, 21)
    assert daily[1].start_utc == utc(2026, 11, 1, 22)


def test_market_closed_window_follows_new_york_time():
    # Summer: Fri 16:00 NY (20:00 UTC) is open, Fri 17:00 NY (21:00 UTC) is closed
    assert not is_market_closed(utc(2026, 10, 23, 20))
    assert is_market_closed(utc(2026, 10, 23, 21))
    # Sunday 17:00 NY (21:00 UTC in summer) is the first open bar
    assert is_market_closed(utc(2026, 10, 25, 20))
    assert not is_market_closed(utc(2026, 10, 25, 21))
    # Winter: shifts to 22:00 UTC
    assert not is_market_closed(utc(2026, 11, 6, 21))
    assert is_market_closed(utc(2026, 11, 6, 22))
    assert not is_market_closed(utc(2026, 11, 8, 22))


def test_friday_to_monday_with_sunday_session_folded_into_monday():
    fri = D(2026, 10, 23)
    mon = D(2026, 10, 26)
    friday = day_hours(fri, 100, 110, 90, 105)
    monday = day_hours(mon, 105, 112, 95, 100)  # starts Sun 17:00 NY
    # Weekend bars, Fri 17:00 NY -> Sun 17:00 NY: Twelve Data keeps quoting, price drifting away from 105
    weekend_start = ny(2026, 10, 23, 17)
    frozen = [
        {"datetime": weekend_start + HOUR * i, "open": 105.0 + 0.05 * i, "high": 105.0 + 0.05 * (i + 2),
         "low": 105.0 + 0.05 * (i - 1), "close": 105.0 + 0.05 * (i + 1)}
        for i in range(48)
    ]
    now = utc(2026, 10, 26, 22)
    daily = aggregate_1h_to_daily(friday + frozen + monday, now)

    assert [d.date for d in daily] == [fri, mon]  # no Saturday/Sunday candles
    assert daily[1].start_utc == ny(2026, 10, 25, 17)  # Monday's candle opens Sunday evening
    assert daily[1].open == 105  # first Sunday-session bar's open
    assert daily[0].close == 105  # Friday untouched by the (live, drifting) weekend bars

    usable = usable_daily_candles(daily, "EURUSD")
    assert [d.date for d in usable] == [fri, mon] and previous_trading_day(mon) == fri  # Fri is the day before Mon


def test_degenerate_candle_is_dropped_so_days_are_not_consecutive():
    tue, wed, thu = D(2026, 10, 27), D(2026, 10, 28), D(2026, 10, 29)
    candles = (
        day_hours(tue, 100, 110, 90, 105)
        + day_hours(wed, 100, 100.0001, 99.9999, 100)  # frozen feed (holiday)
        + day_hours(thu, 105, 112, 95, 100)
    )
    daily = aggregate_1h_to_daily(candles, utc(2026, 10, 30, 22))
    usable = usable_daily_candles(daily, "EURUSD")
    assert [d.date for d in usable] == [tue, thu]


def test_gold_style_23_bar_day_is_accepted():
    wed = D(2026, 10, 28)
    # Gold pauses 17:00-18:00 NY: the first bar of the trading day (index 0) never exists
    daily = aggregate_1h_to_daily(day_hours(wed, 100, 110, 90, 105, skip={0}), utc(2026, 10, 29, 22))
    assert len(daily) == 1
    assert daily[0].bar_count == 23 and daily[0].has_final_bar
    assert daily[0].is_complete and len(daily[0].missing_hours) == 1  # still reported
    assert usable_daily_candles(daily, "XAUUSD") == daily


def test_day_without_final_bar_is_rejected():
    wed = D(2026, 10, 28)
    bars = day_hours(wed, 100, 110, 90, 105, skip={23})  # 23 bars, but the 16:00-17:00 NY bar is gone
    daily = aggregate_1h_to_daily(bars, utc(2026, 10, 29, 22))
    assert daily[0].bar_count == 23 and not daily[0].has_final_bar
    assert not daily[0].is_complete
    assert "final bar" in daily[0].incomplete_reason
    assert usable_daily_candles(daily, "EURUSD") == []


def test_21_bar_day_is_rejected_and_22_accepted():
    wed = D(2026, 10, 28)
    now = utc(2026, 10, 29, 22)
    short = aggregate_1h_to_daily(day_hours(wed, 100, 110, 90, 105, skip={5, 6, 7}), now)  # 21 bars, final present
    assert short[0].bar_count == 21 and short[0].has_final_bar and not short[0].is_complete
    assert "need at least 22" in short[0].incomplete_reason
    assert usable_daily_candles(short, "EURUSD") == []
    ok = aggregate_1h_to_daily(day_hours(wed, 100, 110, 90, 105, skip={5, 6}), now)  # 22 bars
    assert ok[0].is_complete


def test_min_bars_is_a_per_pair_setting(monkeypatch):
    wed = D(2026, 10, 28)
    bars = day_hours(wed, 100, 110, 90, 105, skip={5, 6, 7})  # 21 bars
    monkeypatch.setitem(dc.PAIR_MIN_DAILY_BARS, "XAUUSD", 20)
    assert dc.min_daily_bars_for("XAUUSD") == 20
    assert dc.min_daily_bars_for("EURUSD") == 22
    daily = aggregate_1h_to_daily(bars, utc(2026, 10, 29, 22), dc.min_daily_bars_for("XAUUSD"))
    assert daily[0].is_complete


def test_forming_day_and_forming_hour_are_excluded():
    wed = D(2026, 10, 28)
    bars = day_hours(wed, 100, 110, 90, 105)
    assert aggregate_1h_to_daily(bars, trading_day_bounds(wed)[1] - HOUR)  == []  # last hour still forming
    assert len(aggregate_1h_to_daily(bars, trading_day_bounds(wed)[1])) == 1


def test_latest_closed_trading_day():
    assert latest_closed_trading_day(utc(2026, 10, 28, 21, 10)) == D(2026, 10, 28)  # 17:10 NY Wed
    assert latest_closed_trading_day(utc(2026, 10, 28, 20, 59)) == D(2026, 10, 27)  # 16:59 NY Wed
    assert latest_closed_trading_day(utc(2026, 10, 24, 12)) == D(2026, 10, 23)  # Saturday -> Friday
    assert latest_closed_trading_day(utc(2026, 10, 25, 23)) == D(2026, 10, 23)  # Sunday evening -> Friday


def test_price_precision():
    assert format_price("XAUUSD", 4060.123) == "4,060.12"
    assert format_price("USDJPY", 151.2346) == "151.235"
    assert format_price("EURUSD", 1.0845) == "1.08450"


def test_every_day_not_just_monday_opens_at_the_previous_close():
    bars = day_hours(D(2026, 10, 26), 100, 110, 90, 105) + day_hours(D(2026, 10, 27), 107, 112, 95, 100) \
        + day_hours(D(2026, 10, 28), 99, 104, 96, 101)
    mon, tue, wed = aggregate_1h_to_daily(bars, utc(2026, 10, 29, 22))
    assert (tue.open, wed.open) == (mon.close, tue.close) == (105, 100)  # first bars (107, 99) are ignored
    assert (tue.first_bar_open, wed.first_bar_open) == (107, 99)
    assert [d.open_from_prev_close for d in (mon, tue, wed)] == [False, True, True]


def test_falls_back_to_the_first_bar_when_the_previous_day_is_unavailable():
    # Previous day incomplete (final bar missing) -> Tuesday keeps its first bar's open
    bars = day_hours(D(2026, 10, 26), 100, 110, 90, 105, skip={23}) + day_hours(D(2026, 10, 27), 107, 112, 95, 100)
    mon, tue = aggregate_1h_to_daily(bars, utc(2026, 10, 28, 22))
    assert not mon.is_complete
    assert tue.open == 107 and tue.open_from_prev_close is False
    # Holiday gap: Wednesday's previous trading day (Tuesday) is simply absent from the data
    bars = day_hours(D(2026, 10, 26), 100, 110, 90, 105) + day_hours(D(2026, 10, 28), 99, 104, 96, 101)
    mon, wed = aggregate_1h_to_daily(bars, utc(2026, 10, 29, 22))
    assert wed.open == 99 and wed.open_from_prev_close is False
