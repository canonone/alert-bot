"""A trading day's OPEN is the previous trading day's CLOSE (as on FOREX.com); H/L/C come from the 1H bars.
Real FOREX.com Fri/Mon values from TradingView; the 'bot' Monday values are what Twelve Data's 1H bars gave."""
import asyncio
import dataclasses
import datetime

import pytest

import app.services.daily_sweep_service as sm
from app.services.daily_sweep_service import (
    CONT_SELL,
    DailySweepService,
    aggregate_1h_to_daily,
    classify_continuation,
    detect_continuation,
    detect_sweep,
    open_note,
    pip_size,
)

from test_daily_sweep import HOUR, D, _FakeMarketData, day_hours, ny, utc

FRI, MON, TUE = D(2026, 10, 23), D(2026, 10, 26), D(2026, 10, 27)

# pair: (FOREX.com Friday O,H,L,C, FOREX.com Monday O,H,L,C, bot's Monday O,H,L,C from Twelve Data)
REAL = {
    "EURNZD": ((1.97353, 1.97632, 1.96310, 1.96594), (1.96594, 1.97144, 1.96457, 1.96669), (1.97031, 1.97157, 1.96441, 1.96693)),
    "AUDJPY": ((110.066, 110.448, 109.494, 110.348), (110.348, 110.761, 110.084, 110.516), (110.47388, 110.7646, 110.09461, 110.50879)),
    "CADCHF": ((0.58570, 0.59012, 0.58517, 0.58913), (0.58913, 0.59085, 0.58716, 0.59045), (0.58738, 0.59083, 0.58686, 0.59068)),
}


def _frozen_weekend(price):
    start = ny(2026, 10, 23, 17)
    return [{"datetime": start + HOUR * i, "open": price, "high": price, "low": price, "close": price} for i in range(48)]


def _fri_mon_bars(pair):
    fri, _, bot_mon = REAL[pair]
    return day_hours(FRI, *fri) + _frozen_weekend(fri[3]) + day_hours(MON, *bot_mon)


@pytest.mark.parametrize("pair", list(REAL))
def test_monday_open_equals_friday_close_on_real_pairs(pair):
    fri, forex_mon, bot_mon = REAL[pair]
    daily = aggregate_1h_to_daily(_fri_mon_bars(pair), utc(2026, 10, 27, 12))
    friday, monday = daily
    assert (friday.date, monday.date) == (FRI, MON)
    # The bug: Monday's first 1H bar opens away from Friday's close...
    assert monday.first_bar_open == bot_mon[0]
    assert abs(monday.first_bar_open - fri[3]) / pip_size(pair) > 5
    # ...the fix: Monday's open is Friday's close (= FOREX.com's Monday open, exactly)
    assert monday.open == friday.close == fri[3] == forex_mon[0]
    assert monday.open_from_prev_close is True
    # High, low and close still come straight from the 1H bars
    assert (monday.high, monday.low, monday.close) == bot_mon[1:]
    # Friday is the oldest day in the window: no previous day, so it falls back to its first bar
    assert friday.open == fri[0] and friday.open_from_prev_close is False


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


# ── EURNZD Fri -> Mon -> Tue: no false CONTINUATION SELL ────────────────────

def _eurnzd_three_days():
    fri, _, bot_mon = REAL["EURNZD"]
    tue = (1.96700, 1.96841, 1.95395, 1.95849)  # H/L/C from the bot's logs; first-bar open is irrelevant now
    bars = day_hours(FRI, *fri) + _frozen_weekend(fri[3]) + day_hours(MON, *bot_mon) + day_hours(TUE, *tue)
    return aggregate_1h_to_daily(bars, utc(2026, 10, 28, 12))


def test_eurnzd_mon_tue_is_no_longer_a_continuation_sell():
    friday, monday, tuesday = _eurnzd_three_days()
    assert monday.open == 1.96594 and monday.close == 1.96693 and monday.close > monday.open  # bullish
    assert tuesday.open == monday.close  # Day 2 opens at Day 1's close
    assert (tuesday.high, tuesday.low, tuesday.close) == (1.96841, 1.95395, 1.95849)  # bearish Day 2
    assert classify_continuation(monday, tuesday) == "none"
    assert detect_continuation(monday, tuesday) is None
    assert detect_sweep(monday, tuesday) is None  # Day 2 didn't sweep Monday's high either


def test_eurnzd_old_first_bar_open_reproduces_the_false_sell():
    _, monday, tuesday = _eurnzd_three_days()
    old_monday = dataclasses.replace(monday, open=monday.first_bar_open)  # the pre-fix Monday open 1.97031
    assert old_monday.open > old_monday.close  # bearish by the wrong open
    assert classify_continuation(old_monday, tuesday) == CONT_SELL


# ── Gap rule: harmless, but can no longer fire from 1H data ─────────────────

def test_gap_between_days_cannot_arise_from_one_hour_data():
    # Tuesday's first bar opens ABOVE Monday's high (111 > 110); with open = previous close it's 105
    bars = day_hours(D(2026, 10, 26), 100, 110, 90, 105) + day_hours(D(2026, 10, 27), 111, 120, 100, 118)
    mon, tue = aggregate_1h_to_daily(bars, utc(2026, 10, 28, 22))
    assert tue.open == 105 < mon.high
    assert classify_continuation(mon, tue) != "skipped (gap)"
    # The rule itself is intact on candles that do gap
    gapped = dataclasses.replace(tue, open=111)
    assert classify_continuation(mon, gapped) == "skipped (gap)"


# ── /sweepcheck notes ───────────────────────────────────────────────────────

def test_open_note_wording_and_two_pip_threshold():
    _, monday = aggregate_1h_to_daily(_fri_mon_bars("EURNZD"), utc(2026, 10, 27, 12))
    note = open_note("EURNZD", monday)
    assert "open = previous close (1.96594)" in note and "first 1H bar opened 1.97031" in note and "43.7 pips apart" in note

    # Within 2 pips (JPY pip = 0.01): just the plain note
    near = dataclasses.replace(monday, first_bar_open=monday.open + 0.019)
    assert open_note("AUDJPY", near) == "open = previous close"
    far = dataclasses.replace(monday, first_bar_open=monday.open + 0.021)
    assert "2.1 pips apart" in open_note("AUDJPY", far)

    friday = aggregate_1h_to_daily(_fri_mon_bars("EURNZD"), utc(2026, 10, 27, 12))[0]
    assert "first 1H bar (previous trading day not available)" in open_note("EURNZD", friday)


def test_sweepcheck_message_shows_the_adjusted_open():
    svc = DailySweepService("k", _FakeMarketData(_fri_mon_bars("CADCHF")), None, None)
    svc.clock = lambda: utc(2026, 10, 27, 12)
    text = asyncio.run(svc.get_check_message("CADCHF"))
    assert "Day 2 — Mon 2026-10-26" in text
    assert "O 0.58913 · H 0.59083 · L 0.58686 · C 0.59068" in text  # Monday open = Friday close
    assert "open = previous close (0.58913); first 1H bar opened 0.58738 (17.5 pips apart)" in text
    assert "open = first 1H bar (previous trading day not available)" in text  # Friday, oldest in the window


def test_scan_logs_the_fallback(caplog):
    import logging

    bars = day_hours(D(2026, 10, 26), 100, 110, 90, 105) + day_hours(D(2026, 10, 27), 105, 112, 95, 100)
    svc = DailySweepService("k", _FakeMarketData(bars), None, None)
    svc.clock = lambda: ny(2026, 10, 27, 17, 10)

    async def no_store(*a):
        return None

    svc._store_setup = no_store
    with caplog.at_level(logging.INFO):
        asyncio.run(svc._scan_pair("EURUSD", D(2026, 10, 27)))
    assert "previous trading day unavailable" in caplog.text  # Monday (Day 1) is the oldest day in the data
    assert "[open = previous close]" in caplog.text  # Tuesday
