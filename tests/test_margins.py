"""Sweep margins (MIN_BEYOND_PIPS, MIN_BODY_PIPS). Pip = 0.0001; 0.01 for JPY pairs; 0.1 for XAUUSD."""
import asyncio

import pytest

import app.services.daily_sweep_service as sm
from app.services.daily_sweep_service import BUY, SELL, DailySweepService, detect_sweep

from test_daily_sweep import D, _FakeMarketData, candle, day_hours, ny

MON, TUE = D(2026, 10, 26), D(2026, 10, 27)
FX = "EURUSD"


def c(day, o, h, l, cl):
    return candle(day, o, h, l, cl)


def sweep(d1, d2, pair=FX):
    return detect_sweep(d1, d2, pair)


# ── Today's live data ───────────────────────────────────────────────────────

CADJPY_D1 = (112.018, 112.407, 111.775, 112.397)
CADJPY_D2 = (112.397, 112.614, 112.333, 112.408)


def test_cadjpy_no_setup_because_day2_is_a_doji():
    d1, d2 = c(MON, *CADJPY_D1), c(TUE, *CADJPY_D2)
    assert detect_sweep(d1, d2, "CADJPY") is None
    # The wick did clear Day 1's high by 20.7 pips; the only failure is the 1.1-pip body
    assert sm.explain_no_setup(d1, d2, "CADJPY") == [
        "SELL sweep (Day 1 closed bullish):",
        "✗ Day 2 is a doji (body 1.1 pips < 2)",
    ]


def test_cadjpy_sweepcheck_shows_the_doji():
    bars = day_hours(MON, *CADJPY_D1) + day_hours(TUE, *CADJPY_D2)
    svc = DailySweepService("k", _FakeMarketData(bars), None, None)
    svc.clock = lambda: ny(2026, 10, 27, 17, 10)
    text = asyncio.run(svc.get_check_message("CADJPY"))
    assert "✗ Day 2 is a doji (body 1.1 pips &lt; 2)" in text
    assert "Sweep SELL setup" not in text


# ── Sweep: the wick must clear Day 1's extreme by MIN_BEYOND_PIPS ───────────

SELL_D1 = (1.1000, 1.1050, 1.0950, 1.1030)  # bullish, high 1.1050


def sell_d2(high):
    return (1.1030, high, 1.0990, 1.1000)  # bearish body 30 pips, low held


def test_sweep_sell_one_pip_beyond_is_no_setup_five_pips_is():
    d1 = c(MON, *SELL_D1)
    assert sweep(d1, c(TUE, *sell_d2(1.1051))) is None  # 1 pip
    signal = sweep(d1, c(TUE, *sell_d2(1.1055)))  # 5 pips
    assert signal is not None and signal.direction == SELL and signal.level == 1.1030
    assert sweep(d1, c(TUE, *sell_d2(1.1053))) is not None  # exactly 3 pips counts ("at least")
    assert sweep(d1, c(TUE, *sell_d2(1.10529))) is None  # 2.9 pips doesn't
    assert sweep(d1, c(TUE, *sell_d2(1.1050))) is None  # level with the high, not beyond


def test_sweep_buy_one_pip_beyond_is_no_setup_five_pips_is():
    d1 = c(MON, 1.1030, 1.1050, 1.0950, 1.1000)  # bearish, low 1.0950
    assert sweep(d1, c(TUE, 1.1000, 1.1040, 1.0949, 1.1030)) is None  # 1 pip
    signal = sweep(d1, c(TUE, 1.1000, 1.1040, 1.0945, 1.1030))  # 5 pips
    assert signal is not None and signal.direction == BUY and signal.level == 1.1000


def test_sweep_needs_the_opposite_side_untouched_by_the_buffer():
    d1 = c(MON, *SELL_D1)
    assert sweep(d1, c(TUE, 1.1030, 1.1060, 1.0952, 1.1000)) is not None  # 2 pips clear of Day 1's low: not taken
    assert sweep(d1, c(TUE, 1.1030, 1.1060, 1.0951, 1.1000)) is None  # only 1 pip clear: counts as taken
    assert sweep(d1, c(TUE, 1.1030, 1.1060, 1.0950, 1.1000)) is None  # equal to the low
    assert sweep(d1, c(TUE, 1.1030, 1.1060, 1.0949, 1.1000)) is None  # below it


# ── Dojis (MIN_BODY_PIPS) ───────────────────────────────────────────────────

def test_doji_day1_matches_no_pattern():
    d1 = c(MON, 1.1000, 1.1050, 1.0950, 1.10015)  # body 1.5 pips
    d2 = c(TUE, 1.10015, 1.1060, 1.1000, 1.1000)  # would be a clean SELL sweep after a bullish Day 1
    assert sweep(d1, d2) is None
    assert sm.explain_no_setup(d1, d2, FX) == ["✗ Day 1 is a doji (body 1.5 pips < 2) — neither direction applies"]


def test_doji_day2_matches_no_pattern_and_is_reported():
    d1 = c(MON, 1.1000, 1.1050, 1.0950, 1.1040)
    d2 = c(TUE, 1.1040, 1.1060, 1.1000, 1.10385)  # swept the high by 10 pips, but body only 1.5 pips
    assert sweep(d1, d2) is None
    assert sm.explain_no_setup(d1, d2, FX)[1:] == ["✗ Day 2 is a doji (body 1.5 pips < 2)"]


def test_body_threshold_is_inclusive_and_per_pair(monkeypatch):
    d1 = c(MON, 1.1000, 1.1050, 1.0950, 1.1040)
    assert sweep(d1, c(TUE, 1.1040, 1.1060, 1.1000, 1.1038)) is not None  # body exactly 2.0 pips
    assert sweep(d1, c(TUE, 1.1040, 1.1060, 1.1000, 1.10381)) is None  # 1.9 pips
    monkeypatch.setitem(sm.PAIR_MIN_BODY_PIPS, FX, 1.0)
    assert sweep(d1, c(TUE, 1.1040, 1.1060, 1.1000, 1.10385)) is not None  # 1.5 pips passes this pair's 1.0
    assert sm.is_bullish(c(MON, 1.1000, 1.1005, 1.0995, 1.1002), FX) is True


def test_wrong_colour_is_still_reported_as_a_colour_not_a_doji():
    d1 = c(MON, 1.1000, 1.1050, 1.0950, 1.0960)  # clearly bearish -> judged as a BUY sweep
    _, *failed = sm.explain_no_setup(d1, c(TUE, 1.0960, 1.0990, 1.0900, 1.0935), FX)  # Day 2 bearish, 25 pip body
    assert failed == ["✗ needs Day 2 to close bullish — but Day 2 close 1.09350 < open 1.09600"]


# ── Pips per instrument, per-pair overrides ─────────────────────────────────

def test_margin_uses_the_right_pip_for_jpy_and_gold():
    jpy_d1 = c(MON, 110.00, 110.50, 109.50, 110.40)
    assert detect_sweep(jpy_d1, c(TUE, 110.40, 110.52, 110.00, 110.20), "AUDJPY") is None  # 2 pips beyond
    assert detect_sweep(jpy_d1, c(TUE, 110.40, 110.53, 110.00, 110.20), "AUDJPY") is not None  # 3 pips
    gold_d1 = c(MON, 4000.0, 4050.0, 3980.0, 4040.0)
    assert detect_sweep(gold_d1, c(TUE, 4040.0, 4050.2, 4020.0, 4000.0), "XAUUSD") is None  # 2 pips = 0.2
    assert detect_sweep(gold_d1, c(TUE, 4040.0, 4050.3, 4020.0, 4000.0), "XAUUSD") is not None  # 3 pips = 0.3
    assert (sm.pip_size("EURUSD"), sm.pip_size("USDJPY"), sm.pip_size("XAUUSD")) == (0.0001, 0.01, 0.1)


def test_min_beyond_is_a_per_pair_setting(monkeypatch):
    d1 = c(MON, *SELL_D1)
    five = c(TUE, *sell_d2(1.1055))
    assert sweep(d1, five) is not None
    monkeypatch.setitem(sm.PAIR_MIN_BEYOND_PIPS, FX, 10.0)
    assert sweep(d1, five) is None  # needs 10 now
    assert sweep(d1, five, "GBPUSD") is not None  # other pairs keep the default 3
    assert "by ≥10 pips" in sm.explain_no_setup(d1, five, FX)[1]
