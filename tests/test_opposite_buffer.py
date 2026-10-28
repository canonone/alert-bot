"""OPPOSITE_SIDE_BUFFER_PIPS: Day 2 only counts as NOT having taken Day 1's opposite extreme if it stayed at
least this many pips away from it (default 2; pip 0.0001, 0.01 JPY, 0.1 XAUUSD)."""
import asyncio

import pytest

import app.services.daily_sweep_service as sm
from app.services.daily_sweep_service import BUY, SELL, DailySweepService, classify_continuation, detect_continuation, detect_sweep

from test_daily_sweep import D, _FakeMarketData, candle, day_hours, ny

MON, TUE = D(2026, 10, 26), D(2026, 10, 27)
FX = "EURUSD"

# Real bot data, 27 Oct
AUDUSD_D1 = (0.70360, 0.70405, 0.70129, 0.70162)
AUDUSD_D2 = (0.70162, 0.70403, 0.70101, 0.70260)
AUDCAD_D1 = (0.98549, 0.98872, 0.98312, 0.98322)
AUDCAD_D2 = (0.98322, 0.98820, 0.98240, 0.98726)


def c(day, ohlc):
    return candle(day, *ohlc)


# ── AUDUSD: Twelve Data stayed 0.2 pip under Day 1's high (FOREX.com touched it) ─

def test_audusd_no_setup_fails_both_the_sweep_margin_and_the_buffer():
    d1, d2 = c(MON, AUDUSD_D1), c(TUE, AUDUSD_D2)
    assert detect_sweep(d1, d2, "AUDUSD") is None
    assert detect_continuation(d1, d2, "AUDUSD") is None
    heading, *failed = sm.explain_no_setup(d1, d2, "AUDUSD")
    assert heading == "BUY sweep (Day 1 closed bearish):"
    assert failed == [
        "✗ needs Day 2 to sweep Day 1's low by ≥3 pips — but its low went only 2.8 pips below Day 1's low (0.70101 vs 0.70129)",
        "✗ needs Day 2 to stay ≥2 pips below Day 1's high — but it came within 0.2 pips (0.70403 vs 0.70405)",
    ]


def test_audusd_sweepcheck_message():
    bars = day_hours(MON, *AUDUSD_D1) + day_hours(TUE, *AUDUSD_D2)
    svc = DailySweepService("k", _FakeMarketData(bars), None, None)
    svc.clock = lambda: ny(2026, 10, 27, 17, 10)
    text = asyncio.run(svc.get_check_message("AUDUSD"))
    assert "✗ needs Day 2 to stay ≥2 pips below Day 1&#x27;s high — but it came within 0.2 pips (0.70403 vs 0.70405)" in text
    assert "its low went only 2.8 pips below Day 1&#x27;s low (0.70101 vs 0.70129)" in text
    assert "<" not in text.replace("<b>", "").replace("</b>", "") and ">" not in text.replace("<b>", "").replace("</b>", "")


def test_audusd_passes_the_old_strict_rule_which_is_why_it_slipped_through():
    # Strictly, 0.70403 < 0.70405 and the sweep margin was the only thing in the way; with the sweep
    # margin satisfied, the buffer alone must still reject it
    d1 = c(MON, AUDUSD_D1)
    deeper_low = (0.70162, 0.70403, 0.70090, 0.70260)  # 3.9 pips below Day 1's low
    assert detect_sweep(d1, c(TUE, deeper_low), "AUDUSD") is None
    assert "stay ≥2 pips below Day 1's high" in sm.explain_no_setup(d1, c(TUE, deeper_low), "AUDUSD")[1]


# ── AUDCAD: high stayed 5.2 pips below Day 1's high -> still a SWEEP BUY ─────

def test_audcad_is_still_a_sweep_buy():
    d1, d2 = c(MON, AUDCAD_D1), c(TUE, AUDCAD_D2)
    signal = detect_sweep(d1, d2, "AUDCAD")
    assert signal is not None and signal.direction == BUY
    assert signal.level == 0.98322 and signal.sweep_extreme == 0.98240
    assert (d1.high - d2.high) / 0.0001 == pytest.approx(5.2)  # beyond the 2-pip buffer
    assert (d1.low - d2.low) / 0.0001 == pytest.approx(7.2)  # beyond the 3-pip sweep margin
    assert detect_continuation(d1, d2, "AUDCAD") is None  # Day 2 is bullish after a bearish Day 1


# ── The boundary: exactly 2.0 pips is "not taken", 1.9 is "taken" ───────────

def test_sweep_sell_boundary_on_the_low():
    d1 = c(MON, (1.1000, 1.1050, 1.0950, 1.1030))
    d2 = lambda low: c(TUE, (1.1030, 1.1060, low, 1.1000))  # noqa: E731
    assert detect_sweep(d1, d2(1.0952), FX).direction == SELL  # exactly 2.0 pips above Day 1's low
    assert detect_sweep(d1, d2(1.09519), FX) is None  # 1.9 pips
    assert detect_sweep(d1, d2(1.0955), FX).direction == SELL  # 5 pips


def test_sweep_buy_boundary_on_the_high():
    d1 = c(MON, (1.1030, 1.1050, 1.0950, 1.1000))
    d2 = lambda high: c(TUE, (1.1000, high, 1.0945, 1.1030))  # noqa: E731
    assert detect_sweep(d1, d2(1.1048), FX).direction == BUY  # exactly 2.0 pips below Day 1's high
    assert detect_sweep(d1, d2(1.10481), FX) is None  # 1.9 pips
    assert detect_sweep(d1, d2(1.1045), FX).direction == BUY


def test_continuation_buy_boundary_on_the_low():
    d1 = c(MON, (1.1000, 1.1050, 1.0950, 1.1040))
    d2 = lambda low: c(TUE, (1.1040, 1.1090, low, 1.1060))  # noqa: E731
    assert detect_continuation(d1, d2(1.0952), FX).direction == BUY  # exactly 2.0
    assert classify_continuation(d1, d2(1.09519), FX) == sm.SKIP_OPPOSITE  # 1.9
    assert classify_continuation(d1, d2(1.0950), FX) == sm.SKIP_OPPOSITE  # equal


def test_continuation_sell_boundary_on_the_high():
    d1 = c(MON, (1.1040, 1.1050, 1.0950, 1.1000))
    d2 = lambda high: c(TUE, (1.1000, high, 1.0900, 1.0940))  # noqa: E731
    assert detect_continuation(d1, d2(1.1048), FX).direction == SELL  # exactly 2.0 below Day 1's high
    assert classify_continuation(d1, d2(1.10481), FX) == sm.SKIP_OPPOSITE  # 1.9
    assert classify_continuation(d1, d2(1.1050), FX) == sm.SKIP_OPPOSITE


# ── Wording ─────────────────────────────────────────────────────────────────

def test_wording_for_within_touched_and_beyond():
    d1 = c(MON, (1.1000, 1.1050, 1.0950, 1.1030))

    def line(low):
        checks = sm.sweep_checks(d1, c(TUE, (1.1030, 1.1060, low, 1.1000)), SELL, FX)
        return [x.failure_text() for x in checks if not x.ok][0]

    assert line(1.09515) == "✗ needs Day 2 to stay ≥2 pips above Day 1's low — but it came within 1.5 pips (1.09515 vs 1.09500)"
    assert line(1.0950) == "✗ needs Day 2 to stay ≥2 pips above Day 1's low — but it touched it (1.09500 vs 1.09500)"
    assert line(1.0940) == "✗ needs Day 2 to stay ≥2 pips above Day 1's low — but it went 10.0 pips beyond (1.09400 vs 1.09500)"


# ── Pip sizes and per-pair override ─────────────────────────────────────────

def test_buffer_uses_the_pairs_pip_size():
    jpy = c(MON, (110.00, 110.50, 109.50, 110.40))
    d2 = lambda low: c(TUE, (110.40, 111.00, low, 110.60))  # noqa: E731
    assert detect_continuation(jpy, d2(109.52), "AUDJPY") is not None  # 2 pips (0.02) clear
    assert classify_continuation(jpy, d2(109.51), "AUDJPY") == sm.SKIP_OPPOSITE  # 1 pip
    gold = c(MON, (4000.0, 4050.0, 3980.0, 4040.0))
    g2 = lambda low: c(TUE, (4040.0, 4100.0, low, 4060.0))  # noqa: E731
    assert detect_continuation(gold, g2(3980.2), "XAUUSD") is not None  # 2 pips = 0.2
    assert classify_continuation(gold, g2(3980.1), "XAUUSD") == sm.SKIP_OPPOSITE


def test_buffer_is_a_per_pair_setting(monkeypatch):
    d1 = c(MON, (1.1000, 1.1050, 1.0950, 1.1040))
    d2 = c(TUE, (1.1040, 1.1090, 1.0953, 1.1060))  # 3 pips clear
    assert detect_continuation(d1, d2, FX) is not None
    monkeypatch.setitem(sm.PAIR_OPPOSITE_SIDE_BUFFER_PIPS, FX, 5.0)
    assert classify_continuation(d1, d2, FX) == sm.SKIP_OPPOSITE
    assert detect_continuation(d1, d2, "GBPUSD") is not None  # other pairs keep the default 2
    monkeypatch.setitem(sm.PAIR_OPPOSITE_SIDE_BUFFER_PIPS, FX, 0.0)
    assert detect_continuation(d1, c(TUE, (1.1040, 1.1090, 1.09501, 1.1060)), FX) is not None  # buffer 0: any clearance
