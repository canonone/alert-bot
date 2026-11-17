import datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.daily_sweep_service import (
    BUY,
    SELL,
    DailyCandle,
    aggregate_1h_to_daily,
    day3_expiry,
    detect_sweep,
    format_level_hit,
    format_price,
    is_expired,
    is_market_closed,
    last_two_days,
    latest_closed_trading_day,
    SetupState,
    trading_day_bounds,
    trading_day_label,
    usable_daily_candles,
)

UTC = datetime.timezone.utc
NY = ZoneInfo("America/New_York")
HOUR = datetime.timedelta(hours=1)


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


D = datetime.date


# ── Pattern detection ───────────────────────────────────────────────────────

def test_sell_setup():
    day1 = candle(D(2026, 10, 27), 100, 110, 90, 105)  # bullish
    day2 = candle(D(2026, 10, 28), 105, 112, 95, 100)  # bearish, high swept, low held
    signal = detect_sweep(day1, day2)
    assert signal is not None
    assert signal.direction == SELL
    assert signal.level == 105  # Day 2's actual open
    assert signal.sweep_extreme == 112


def test_buy_setup():
    day1 = candle(D(2026, 10, 27), 105, 110, 90, 95)  # bearish
    day2 = candle(D(2026, 10, 28), 95, 108, 85, 100)  # bullish, low swept, high held
    signal = detect_sweep(day1, day2)
    assert signal is not None
    assert signal.direction == BUY
    assert signal.level == 95
    assert signal.sweep_extreme == 85


def test_level_uses_day2_open_not_day1_close():
    day1 = candle(D(2026, 10, 27), 100, 110, 90, 105)
    day2 = candle(D(2026, 10, 28), 106, 112, 95, 100)  # gapped open
    assert detect_sweep(day1, day2).level == 106


def test_real_xauusd_example_is_a_sell():
    day1 = candle(D(2026, 10, 27), 4017, 4065, 3989, 4060)
    day2 = candle(D(2026, 10, 28), 4060, 4071, 4028, 4045)
    signal = detect_sweep(day1, day2)
    assert signal is not None
    assert signal.direction == SELL
    assert signal.level == 4060


def test_day2_sweeps_both_sides_is_no_setup():
    day1 = candle(D(2026, 10, 27), 100, 110, 90, 105)
    day2 = candle(D(2026, 10, 28), 105, 112, 88, 100)  # high AND low swept
    assert detect_sweep(day1, day2) is None
    day1 = candle(D(2026, 10, 27), 105, 110, 90, 95)
    day2 = candle(D(2026, 10, 28), 95, 112, 88, 100)
    assert detect_sweep(day1, day2) is None


def test_day2_sweeps_neither_side_is_no_setup():
    day1 = candle(D(2026, 10, 27), 100, 110, 90, 105)
    day2 = candle(D(2026, 10, 28), 105, 108, 95, 100)
    assert detect_sweep(day1, day2) is None
    day1 = candle(D(2026, 10, 27), 105, 110, 90, 95)
    day2 = candle(D(2026, 10, 28), 95, 108, 95, 100)
    assert detect_sweep(day1, day2) is None


def test_day1_wrong_colour_is_no_setup():
    # Bearish Day 1 + bearish Day 2 sweeping the high: matches neither pattern
    day1 = candle(D(2026, 10, 27), 105, 110, 90, 100)
    day2 = candle(D(2026, 10, 28), 100, 112, 95, 98)
    assert detect_sweep(day1, day2) is None
    # Flat Day 1 is neither bullish nor bearish
    day1 = candle(D(2026, 10, 27), 100, 110, 90, 100)
    assert detect_sweep(day1, day2) is None


def test_day2_wrong_colour_is_no_setup():
    # Bullish Day 1, but Day 2 closes bullish too (sweeps high, holds low)
    day1 = candle(D(2026, 10, 27), 100, 110, 90, 105)
    day2 = candle(D(2026, 10, 28), 105, 112, 95, 108)
    assert detect_sweep(day1, day2) is None
    # Bearish Day 1, Day 2 closes bearish (sweeps low, holds high)
    day1 = candle(D(2026, 10, 27), 105, 110, 90, 95)
    day2 = candle(D(2026, 10, 28), 95, 108, 85, 92)
    assert detect_sweep(day1, day2) is None


def test_comparisons_are_strict():
    day1 = candle(D(2026, 10, 27), 100, 110, 90, 105)
    equal_high = candle(D(2026, 10, 28), 105, 110, 95, 100)  # high == Day 1 high, not a sweep
    assert detect_sweep(day1, equal_high) is None
    equal_low = candle(D(2026, 10, 28), 105, 112, 90, 100)  # low == Day 1 low, counts as swept
    assert detect_sweep(day1, equal_low) is None


# ── 17:00 New York bucketing and DST ────────────────────────────────────────

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


# ── Weekend handling ────────────────────────────────────────────────────────

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
    pair = last_two_days(usable)
    assert pair is not None and pair[0].date == fri and pair[1].date == mon  # Fri is the day before Mon


def test_friday_day2_gives_monday_day3():
    fri = D(2026, 10, 23)
    assert day3_expiry(fri) == ny(2026, 10, 26, 17)  # Monday 17:00 NY, not Saturday
    assert day3_expiry(D(2026, 10, 28)) == utc(2026, 10, 29, 21)  # = 22:00 WAT


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
    assert last_two_days(usable) is None  # Tue/Thu aren't adjacent trading days


def test_gold_style_23_bar_day_is_accepted():
    wed = D(2026, 10, 28)
    # Gold pauses 17:00-18:00 NY: the first bar of the trading day (index 0) never exists
    daily = aggregate_1h_to_daily(day_hours(wed, 100, 110, 90, 105, skip={0}), utc(2026, 10, 29, 22))
    assert len(daily) == 1
    assert daily[0].bar_count == 23 and daily[0].has_final_bar
    assert daily[0].is_complete and len(daily[0].missing_hours) == 1  # still reported for /sweepcheck
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
    monkeypatch.setitem(sweep_module.PAIR_MIN_DAILY_BARS, "XAUUSD", 20)
    assert sweep_module.min_daily_bars_for("XAUUSD") == 20
    assert sweep_module.min_daily_bars_for("EURUSD") == 22
    daily = aggregate_1h_to_daily(bars, utc(2026, 10, 29, 22), sweep_module.min_daily_bars_for("XAUUSD"))
    assert daily[0].is_complete


def test_sweepcheck_candle_line_reports_missing_bars():
    wed = D(2026, 10, 28)
    now = utc(2026, 10, 29, 22)
    accepted = aggregate_1h_to_daily(day_hours(wed, 100, 110, 90, 105, skip={0}), now)[0]
    assert "1 bar(s) missing, accepted" in sweep_module.format_candle_line("XAUUSD", "Day 2", accepted)
    rejected = aggregate_1h_to_daily(day_hours(wed, 100, 110, 90, 105, skip={23}), now)[0]
    assert "⚠️ incomplete" in sweep_module.format_candle_line("XAUUSD", "Day 2", rejected)


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


# ── Expiry ──────────────────────────────────────────────────────────────────

def test_expiry_boundary():
    expires = day3_expiry(D(2026, 10, 28))
    assert not is_expired(expires, expires - datetime.timedelta(seconds=1))
    assert is_expired(expires, expires)
    assert is_expired(expires, expires + datetime.timedelta(hours=5))


def test_expiry_follows_dst():
    # Day 2 = Mon 2 Nov (EST) -> Day 3 = Tue 3 Nov closes 17:00 EST = 22:00 UTC
    assert day3_expiry(D(2026, 11, 2)) == utc(2026, 11, 3, 22)
    # Day 2 = Thu 29 Oct (EDT) -> Day 3 = Fri 30 Oct closes 21:00 UTC
    assert day3_expiry(D(2026, 10, 29)) == utc(2026, 10, 30, 21)


# ── Formatting ──────────────────────────────────────────────────────────────

def test_price_precision():
    assert format_price("XAUUSD", 4060.123) == "4,060.12"
    assert format_price("USDJPY", 151.2346) == "151.235"
    assert format_price("EURUSD", 1.0845) == "1.08450"


def test_level_hit_message_matches_spec():
    setup = SetupState(
        id=1, pair="XAUUSD", direction=SELL,
        day1=(4017.30, 4065.10, 3989.20, 4060.12),
        day2=(4060.12, 4071.40, 4028.50, 4045.51),
        level=4060.12, day2_date=D(2026, 10, 28), day3_expires_at=utc(2026, 10, 29, 21),
    )
    text = format_level_hit(setup, 4060.25, utc(2026, 10, 29, 8, 14))
    assert "DAILY SWEEP LEVEL HIT — XAUUSD (SELL)" in text
    assert "<b>Level (Day 2 open):</b> 4,060.12" in text
    assert "<b>Day 2 High (sweep):</b> 4,071.40 — invalidation / SL idea" in text
    assert "<b>Day 1:</b> O 4,017.30 · H 4,065.10 · L 3,989.20 · C 4,060.12" in text
    assert "2026-10-29 09:14 WAT" in text


# ── Live tick handling (in-memory cache; the DB-backed trigger is stubbed) ──

def _service_with_stub_trigger():
    import asyncio
    from app.services.daily_sweep_service import DailySweepService

    svc = DailySweepService(api_key="k", market_data=None, users_service=None, send_message=None)
    fired = []

    async def fake_trigger(setup, price, now):
        fired.append((setup.id, price))

    svc._trigger = fake_trigger
    return svc, fired, asyncio


def _setup(setup_id, pair, direction, level, expires_at):
    return SetupState(
        id=setup_id, pair=pair, direction=direction,
        day1=(1, 2, 0, 1), day2=(1, 2, 0, 1), level=level,
        day2_date=D(2026, 10, 28), day3_expires_at=expires_at,
    )


def test_tick_hits_sell_up_and_buy_down_only():
    svc, fired, asyncio = _service_with_stub_trigger()
    future = datetime.datetime.now(UTC) + datetime.timedelta(hours=5)
    svc._cache_add(_setup(1, "XAUUSD", SELL, 4060, future))
    svc._cache_add(_setup(2, "EURUSD", BUY, 1.08, future))

    asyncio.run(svc.check_tick("XAUUSD", 4059.99, None))  # SELL not yet reached
    asyncio.run(svc.check_tick("EURUSD", 1.0801, None))  # BUY not yet reached
    assert fired == []
    asyncio.run(svc.check_tick("XAUUSD", 4060.0, None))  # >= level
    asyncio.run(svc.check_tick("EURUSD", 1.08, None))  # <= level
    assert fired == [(1, 4060.0), (2, 1.08)]
    assert svc.active_symbols() == {"XAUUSD", "EURUSD"}


def test_tick_after_expiry_is_ignored_even_if_cache_is_stale():
    svc, fired, asyncio = _service_with_stub_trigger()
    past = datetime.datetime.now(UTC) - datetime.timedelta(seconds=1)
    svc._cache_add(_setup(1, "XAUUSD", SELL, 4060, past))
    asyncio.run(svc.check_tick("XAUUSD", 4100, None))
    assert fired == []


def test_cache_remove_drops_symbol():
    svc, _, _ = _service_with_stub_trigger()
    svc._cache_add(_setup(1, "XAUUSD", SELL, 4060, utc(2030, 1, 1)))
    svc._cache_remove(1)
    assert svc.active_symbols() == set()


# ── Real CADCHF SELL example, end to end (FOREX.com daily, 17:00 NY close) ──

import app.services.daily_sweep_service as sweep_module  # noqa: E402
from app.services.daily_sweep_service import DailySweepService  # noqa: E402


class _FakeResult:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class _FakeSession:
    """Stands in for the atomic stage claim. The shared `db` dict is the setup row
    ({"status", optional "stage" (default 0), optional "expires_at"}); the UPDATE is read from the
    statement's bind params, so only a caller whose from-stage matches the row's gets rowcount 1."""

    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        import asyncio

        await asyncio.sleep(0)  # a real round trip yields to the event loop
        p, db = stmt.compile().params, self.db
        not_expired = db.get("expires_at") is None or db["expires_at"] > p["day3_expires_at_1"]
        if db["status"] == "ACTIVE" and db.get("stage", 0) == p["stage_1"] and not_expired:
            db["stage"], db["status"] = p["stage"], p["status"]
            db.setdefault("claims", []).append((p["stage_1"], p["stage"]))
            return _FakeResult(1)
        return _FakeResult(0)

    async def commit(self):
        pass


def _new_service(monkeypatch=None, db=None):
    svc = DailySweepService(api_key="k", market_data=None, users_service=None, send_message=None)
    sent = []

    async def fake_broadcast(message, kind="SWEEP"):
        sent.append(message)

    svc._broadcast = fake_broadcast
    if monkeypatch is not None:
        monkeypatch.setattr(sweep_module, "SessionLocal", lambda: _FakeSession(db))
    return svc, sent


def test_cadchf_real_sell_example_setup_and_tick_simulation(monkeypatch):
    import asyncio

    mon, tue = D(2026, 10, 26), D(2026, 10, 27)
    day1 = candle(mon, 0.58910, 0.59090, 0.58720, 0.59040)
    day2 = candle(tue, 0.59040, 0.59130, 0.58960, 0.58980)

    signal = detect_sweep(day1, day2)
    assert signal is not None
    assert signal.direction == SELL
    assert signal.level == 0.59040
    assert signal.sweep_extreme == 0.59130
    expires = day3_expiry(tue)
    assert expires == ny(2026, 10, 28, 17)
    assert expires == utc(2026, 10, 28, 21)  # 22:00 WAT
    assert f"{sweep_module._to_wat(expires):%Y-%m-%d %H:%M}" == "2026-10-28 22:00"

    db = {"status": "ACTIVE"}
    svc, sent = _new_service(monkeypatch, db)
    svc._cache_add(
        SetupState(
            id=1, pair="CADCHF", direction=SELL,
            day1=(day1.open, day1.high, day1.low, day1.close),
            day2=(day2.open, day2.high, day2.low, day2.close),
            level=signal.level, day2_date=tue, day3_expires_at=expires,
        )
    )

    async def tick(price, when):
        svc.clock = lambda: when
        await svc.check_tick("CADCHF", price, None)

    day3 = ny(2026, 10, 28, 3)
    for price in (0.58990, 0.59020):
        asyncio.run(tick(price, day3))
    assert sent == []  # below the level, nothing yet

    asyncio.run(tick(0.59045, day3 + HOUR))  # first tick >= 0.59040
    assert len(sent) == 1
    assert "DAILY SWEEP LEVEL HIT — CADCHF (SELL)" in sent[0]
    assert "<b>Current Price:</b> 0.59045" in sent[0]
    assert "<b>Day 2 High (sweep):</b> 0.59130" in sent[0]
    assert db["status"] == "TRIGGERED"
    assert svc.active_symbols() == set()

    asyncio.run(tick(0.59094, day3 + 2 * HOUR))  # still above the level — must not fire again
    assert len(sent) == 1


def test_tick_after_expiry_ignored_for_cadchf_setup(monkeypatch):
    import asyncio

    db = {"status": "ACTIVE"}
    svc, sent = _new_service(monkeypatch, db)
    expires = ny(2026, 10, 28, 17)
    svc._cache_add(
        SetupState(
            id=1, pair="CADCHF", direction=SELL, day1=(1, 2, 0, 1), day2=(1, 2, 0, 1),
            level=0.59040, day2_date=D(2026, 10, 27), day3_expires_at=expires,
        )
    )
    svc.clock = lambda: expires + datetime.timedelta(seconds=1)
    asyncio.run(svc.check_tick("CADCHF", 0.59500, None))
    assert sent == [] and db["status"] == "ACTIVE"
    svc.clock = lambda: expires - datetime.timedelta(seconds=1)  # one second before: still live
    asyncio.run(svc.check_tick("CADCHF", 0.59500, None))
    assert len(sent) == 1


def test_synthetic_buy_example_with_ticks(monkeypatch):
    import asyncio

    day1 = candle(D(2026, 10, 27), 1.2000, 1.2100, 1.1900, 1.1950)  # bearish
    day2 = candle(D(2026, 10, 28), 1.1950, 1.2050, 1.1850, 1.2000)  # bullish, swept low, high held
    signal = detect_sweep(day1, day2)
    assert (signal.direction, signal.level, signal.sweep_extreme) == (BUY, 1.1950, 1.1850)
    expires = day3_expiry(D(2026, 10, 28))
    assert expires == utc(2026, 10, 29, 21)

    db = {"status": "ACTIVE"}
    svc, sent = _new_service(monkeypatch, db)
    svc._cache_add(
        SetupState(
            id=1, pair="EURUSD", direction=BUY,
            day1=(day1.open, day1.high, day1.low, day1.close),
            day2=(day2.open, day2.high, day2.low, day2.close),
            level=signal.level, day2_date=D(2026, 10, 28), day3_expires_at=expires,
        )
    )
    svc.clock = lambda: ny(2026, 10, 29, 9)
    for price in (1.2000, 1.1970):
        asyncio.run(svc.check_tick("EURUSD", price, None))
    assert sent == []  # BUY waits for price to trade DOWN to the level
    asyncio.run(svc.check_tick("EURUSD", 1.1949, None))
    asyncio.run(svc.check_tick("EURUSD", 1.1900, None))
    assert len(sent) == 1
    assert "(BUY)" in sent[0] and "Day 2 Low (sweep):</b> 1.18500" in sent[0]
    assert "after a bullish sweep of Day 1's low" in sent[0]


# ── Unclosed Day 2 ──────────────────────────────────────────────────────────

class _FakeMarketData:
    def __init__(self, bars):
        self.bars = bars

    async def get_candles(self, pair, interval, api_key, outputsize=2):
        return self.bars, False


def _forming_wed_scenario():
    """Mon + Tue closed; Wed 28 Oct is a SELL-forming candle against Tue (bullish Tue, bearish Wed
    that sweeps Tue's high). If Wed were wrongly treated as Day 2 this would create a setup."""
    mon, tue, wed = D(2026, 10, 26), D(2026, 10, 27), D(2026, 10, 28)
    return (
        day_hours(mon, 100, 104, 96, 98)
        + day_hours(tue, 98, 110, 90, 105)  # bullish
        + day_hours(wed, 105, 112, 95, 100),  # bearish, high > Tue high, low > Tue low
        wed,
    )


def test_forming_day_is_not_used_as_day2_and_creates_no_setup():
    import asyncio

    bars, wed = _forming_wed_scenario()
    now = ny(2026, 10, 28, 12)  # Wed 12:00 NY — Wed's 17:00 close hasn't passed
    forming_view = [b for b in bars if b["datetime"] + HOUR <= now]  # what the feed could have by now

    daily = aggregate_1h_to_daily(forming_view, now)
    assert wed not in [d.date for d in daily]

    svc = DailySweepService("k", _FakeMarketData(forming_view), None, None)
    svc.clock = lambda: now
    stored = []

    async def fake_store(*args):
        stored.append(args)

    svc._store_setup = fake_store
    expected_day = latest_closed_trading_day(now)
    assert expected_day == D(2026, 10, 27)
    result = asyncio.run(svc._scan_pair("EURUSD", expected_day))
    assert stored == [] and result.setup is None
    assert result.status == "none"  # Mon/Tue (bearish->bullish, no sweep) is evaluated instead


def test_same_data_creates_setup_once_wednesday_has_closed():
    import asyncio

    bars, wed = _forming_wed_scenario()
    now = ny(2026, 10, 28, 17, 10)
    svc = DailySweepService("k", _FakeMarketData(bars), None, None)
    svc.clock = lambda: now
    stored = []

    async def fake_store(pair, signal, d1, d2, expires_at):
        stored.append((signal, d1.date, d2.date, expires_at))
        return SetupState(
            id=7, pair=pair, direction=signal.direction, day1=(1, 2, 0, 1), day2=(1, 2, 0, 1),
            level=signal.level, day2_date=d2.date, day3_expires_at=expires_at,
        )

    svc._store_setup = fake_store
    result = asyncio.run(svc._scan_pair("EURUSD", latest_closed_trading_day(now)))
    assert result.status == "new"
    signal, d1, d2, expires_at = stored[0]
    assert (signal.direction, signal.level, d1, d2) == (SELL, 105, D(2026, 10, 27), wed)
    assert expires_at == ny(2026, 10, 29, 17)


# ── Level already touched before the setup was armed ────────────────────────

def _armed_setup(direction, level):
    return SetupState(
        id=1, pair="EURUSD", direction=direction, day1=(1, 2, 0, 1), day2=(1, 2, 0, 1),
        level=level, day2_date=D(2026, 10, 28), day3_expires_at=utc(2026, 10, 29, 21),
    )


class _FakeTwelveData:
    def __init__(self, price=None, quota=False, boom=False):
        self.price, self.quota, self.boom, self.calls = price, quota, boom, 0

    async def get_current_price(self, symbol, api_key):
        self.calls += 1
        if self.boom:
            raise RuntimeError("network down")
        return self.price, self.quota


ARM_TIME = utc(2026, 10, 28, 21, 10)


def _pre_armed_case(monkeypatch, direction, level, last_price, tick_age_seconds=5, twelve_data=None):
    """last_price = Finnhub tick price (None = no tick yet), received `tick_age_seconds` before arming."""
    import asyncio

    monkeypatch.setattr(sweep_module, "SCAN_REQUEST_SPACING_SECONDS", 0)
    db = {"status": "ACTIVE"}
    svc, sent = _new_service(monkeypatch, db)
    svc.market_data = twelve_data or _FakeTwelveData()
    svc.clock = lambda: ARM_TIME
    if last_price is None:
        svc.get_last_price = lambda symbol: None
    else:
        received = ARM_TIME - datetime.timedelta(seconds=tick_age_seconds)
        svc.get_last_price = lambda symbol: (last_price, received)
    setup = _armed_setup(direction, level)
    svc._cache_add(setup)
    asyncio.run(svc._trigger_if_already_reached(setup))
    return svc, sent, db


EXTRA_LINE = "⚡ Level was reached before the setup was armed (between Day 2 close and the scan)."


def test_sell_level_already_reached_fires_immediately_with_extra_line(monkeypatch):
    svc, sent, db = _pre_armed_case(monkeypatch, SELL, 1.0850, 1.0851)  # beyond (>=) the level
    assert len(sent) == 1 and EXTRA_LINE in sent[0]
    assert db["status"] == "TRIGGERED" and svc.active_symbols() == set()
    svc, sent, _ = _pre_armed_case(monkeypatch, SELL, 1.0850, 1.0850)  # exactly at the level
    assert len(sent) == 1


def test_buy_level_already_reached_fires_immediately(monkeypatch):
    _, sent, db = _pre_armed_case(monkeypatch, BUY, 1.0850, 1.0849)  # <= level
    assert len(sent) == 1 and EXTRA_LINE in sent[0]
    assert db["status"] == "TRIGGERED"


def test_level_not_reached_leaves_setup_armed(monkeypatch):
    svc, sent, db = _pre_armed_case(monkeypatch, SELL, 1.0850, 1.0849)
    assert sent == [] and db["status"] == "ACTIVE" and svc.active_symbols() == {"EURUSD"}
    svc, sent, db = _pre_armed_case(monkeypatch, BUY, 1.0850, 1.0851)
    assert sent == [] and db["status"] == "ACTIVE"


def test_no_finnhub_price_yet_creates_setup_as_normal(monkeypatch):
    svc, sent, db = _pre_armed_case(monkeypatch, SELL, 1.0850, None)
    assert sent == [] and db["status"] == "ACTIVE" and svc.active_symbols() == {"EURUSD"}


def test_normal_tick_trigger_has_no_extra_line(monkeypatch):
    import asyncio

    db = {"status": "ACTIVE"}
    svc, sent = _new_service(monkeypatch, db)
    svc.clock = lambda: utc(2026, 10, 29, 9)
    svc._cache_add(_armed_setup(SELL, 1.0850))
    asyncio.run(svc.check_tick("EURUSD", 1.0851, None))
    assert len(sent) == 1 and EXTRA_LINE not in sent[0]


class _User:
    def __init__(self, chat_id, sweeps=False, continuations=False):
        self.chat_id = chat_id
        self.daily_sweep_alerts_enabled = sweeps
        self.daily_continuation_alerts_enabled = continuations


class _FakeUsers:
    def __init__(self, users):
        self.users = users

    async def find_digest_subscribers(self):
        return [u for u in self.users if u.daily_sweep_alerts_enabled or u.daily_continuation_alerts_enabled]

    async def find_daily_sweep_subscribers(self):
        return [u for u in self.users if u.daily_sweep_alerts_enabled]

    async def find_daily_continuation_subscribers(self):
        return [u for u in self.users if u.daily_continuation_alerts_enabled]


def test_run_scan_sends_digest_then_pre_armed_hit(monkeypatch):
    import asyncio

    db = {"status": "ACTIVE"}
    svc, sent = _new_service(monkeypatch, db)
    svc.clock = lambda: utc(2026, 10, 28, 21, 10)
    svc.pairs = ["EURUSD"]
    svc.get_last_price = lambda symbol: (1.0860, svc.clock())
    monkeypatch.setattr(sweep_module, "SCAN_REQUEST_SPACING_SECONDS", 0)
    svc.users_service = _FakeUsers([_User("1", sweeps=True)])

    async def send(chat_id, message):  # the digest goes out per user, straight to the chat
        sent.append(message)

    svc.send_message = send

    setup = _armed_setup(SELL, 1.0850)

    async def noop(*a, **k):
        return 0

    async def fake_scan_pair(pair, expected_day):
        svc._cache_add(setup)
        return sweep_module.PairScan(pair, "new", setup)

    svc.expire_setups = noop
    svc._scan_pair = fake_scan_pair
    svc._record_scan = noop
    asyncio.run(svc.run_scan())
    assert len(sent) == 2
    assert "Daily Setups" in sent[0] and "LEVEL HIT" in sent[1] and EXTRA_LINE in sent[1]


# ── Startup catch-up decision ───────────────────────────────────────────────

def test_catch_up_when_latest_day_unscanned_and_day3_running():
    now = ny(2026, 10, 28, 20)  # Wed evening; Wed (Day 2) closed 17:00, scan was missed
    assert sweep_module.should_catch_up(D(2026, 10, 27), now)  # last scan was Tuesday's
    assert sweep_module.should_catch_up(None, now)  # never scanned


def test_no_catch_up_when_already_scanned():
    assert not sweep_module.should_catch_up(D(2026, 10, 28), ny(2026, 10, 28, 20))
    assert not sweep_module.should_catch_up(D(2026, 10, 29), ny(2026, 10, 28, 20))  # defensive: ahead


def test_no_catch_up_before_the_scheduled_scan_runs():
    assert not sweep_module.should_catch_up(D(2026, 10, 27), ny(2026, 10, 28, 17, 5))  # 17:10 job still to come
    assert sweep_module.should_catch_up(D(2026, 10, 27), ny(2026, 10, 28, 17, 10))


def test_catch_up_across_weekend_while_monday_day3_is_pending():
    # Sunday: Friday (Day 2) is the latest closed day, Day 3 = Monday and hasn't ended
    assert sweep_module.should_catch_up(D(2026, 10, 22), ny(2026, 10, 25, 12))
    assert not sweep_module.should_catch_up(D(2026, 10, 23), ny(2026, 10, 25, 12))


def test_no_catch_up_if_day3_already_ended(monkeypatch):
    # With the real calendar Day 3 always outlives the latest closed day, so force the guard
    monkeypatch.setattr(sweep_module, "day3_expiry", lambda day: ny(2026, 10, 28, 18))
    assert not sweep_module.should_catch_up(D(2026, 10, 27), ny(2026, 10, 28, 20))


def test_catch_up_over_weekend_uses_friday():
    assert sweep_module.should_catch_up(D(2026, 10, 22), ny(2026, 10, 24, 10))  # Saturday morning


def test_needs_catch_up_reads_stored_day(monkeypatch):
    import asyncio

    svc, _ = _new_service()
    svc.clock = lambda: ny(2026, 10, 28, 20)
    stored = {"day": D(2026, 10, 27)}

    async def fake_get():
        return stored["day"]

    svc.get_last_scanned_day = fake_get
    assert asyncio.run(svc.needs_catch_up()) is True
    stored["day"] = D(2026, 10, 28)
    assert asyncio.run(svc.needs_catch_up()) is False


def _run_scan_recording(retry_status):
    import asyncio

    svc, _ = _new_service()
    svc.clock = lambda: ny(2026, 10, 28, 17, 10)
    svc.pairs = ["EURUSD"]
    recorded = []

    async def noop(*a, **k):
        return 0

    async def rec(day):
        recorded.append(day)

    async def fake_scan_pair(pair, expected_day):
        return sweep_module.PairScan(pair, retry_status)

    svc.expire_setups = noop
    svc._scan_pair = fake_scan_pair
    svc._record_scan = rec
    asyncio.run(svc.run_scan())
    return recorded


def test_completed_scan_is_recorded_but_one_with_pairs_to_retry_is_not():
    assert _run_scan_recording("none") == [D(2026, 10, 28)]
    assert _run_scan_recording("lag") == []


# ── Price source for the armed-time check: fresh Finnhub tick, else Twelve Data ──

def test_fresh_finnhub_price_is_used_without_calling_twelve_data(monkeypatch):
    td = _FakeTwelveData(price=1.0000)  # would NOT trigger a SELL at 1.0850 if it were used
    _, sent, db = _pre_armed_case(monkeypatch, SELL, 1.0850, 1.0860, tick_age_seconds=119, twelve_data=td)
    assert td.calls == 0
    assert len(sent) == 1 and EXTRA_LINE in sent[0] and db["status"] == "TRIGGERED"


def test_stale_finnhub_price_is_ignored_and_twelve_data_used(monkeypatch):
    # Stale tick says 1.0000 (no hit); Twelve Data says 1.0860 (hit) -> only the latter can trigger
    td = _FakeTwelveData(price=1.0860)
    _, sent, db = _pre_armed_case(monkeypatch, SELL, 1.0850, 1.0000, tick_age_seconds=121, twelve_data=td)
    assert td.calls == 1
    assert len(sent) == 1 and "<b>Current Price:</b> 1.08600" in sent[0]
    # And the reverse: a stale tick that WOULD hit must not trigger when Twelve Data says it hasn't
    td = _FakeTwelveData(price=1.0000)
    _, sent, db = _pre_armed_case(monkeypatch, SELL, 1.0850, 1.0900, tick_age_seconds=600, twelve_data=td)
    assert td.calls == 1 and sent == [] and db["status"] == "ACTIVE"


def test_no_finnhub_tick_falls_back_to_twelve_data(monkeypatch):
    td = _FakeTwelveData(price=1.0840)
    _, sent, _ = _pre_armed_case(monkeypatch, BUY, 1.0850, None, twelve_data=td)
    assert td.calls == 1 and len(sent) == 1


def test_no_price_from_either_source_creates_setup_without_trigger(monkeypatch, caplog):
    import logging

    for td in (_FakeTwelveData(price=None), _FakeTwelveData(quota=True), _FakeTwelveData(boom=True)):
        with caplog.at_level(logging.WARNING):
            svc, sent, db = _pre_armed_case(monkeypatch, SELL, 1.0850, None, twelve_data=td)
        assert td.calls == 1
        assert sent == [] and db["status"] == "ACTIVE" and svc.active_symbols() == {"EURUSD"}
    assert "armed without a touched-level check" in caplog.text


def test_twelve_data_requests_are_spaced(monkeypatch):
    import asyncio

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(sweep_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(sweep_module, "SCAN_REQUEST_SPACING_SECONDS", 8.0)
    svc, _ = _new_service()
    svc.market_data = _FakeTwelveData(price=1.0)
    svc.get_last_price = lambda symbol: None
    svc.clock = lambda: ARM_TIME
    asyncio.run(svc._price_for_arming("EURUSD"))
    assert sleeps == []  # nothing requested recently -> no wait
    asyncio.run(svc._price_for_arming("GBPUSD"))  # immediately after -> must wait ~8s
    assert len(sleeps) == 1 and 7 < sleeps[0] <= 8
