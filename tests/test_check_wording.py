"""Failed checks read "needs X — but Y" with the actual numbers (never the requirement's wording as a fact)."""
import asyncio
import logging
import re

import app.services.daily_sweep_service as sm
from app.services.daily_sweep_service import BUY, SELL, DailySweepService

from test_daily_sweep import D, _FakeMarketData, candle, day_hours, ny

MON, TUE = D(2026, 10, 26), D(2026, 10, 27)

# EURNZD Mon -> Tue 27 Oct (open = previous close): Tuesday did NOT sweep Monday's high but DID sweep its low
MON_C = (1.96594, 1.97157, 1.96441, 1.96693)
TUE_C = (1.96693, 1.96841, 1.95395, 1.95849)


def _failed(checks):
    return [c.failure_text() for c in checks if not c.ok]


def test_eurnzd_example_sweep_sell_failures_use_the_actual_numbers():
    d1, d2 = candle(MON, *MON_C), candle(TUE, *TUE_C)
    assert _failed(sm.sweep_checks(d1, d2, SELL, "EURNZD")) == [
        "✗ needs Day 2 to sweep Day 1's high by ≥3 pips — but its high stayed 31.6 pips below Day 1's high (1.96841 vs 1.97157)",
        "✗ needs Day 2 to stay ≥2 pips above Day 1's low — but it went 104.6 pips beyond (1.95395 vs 1.96441)",
    ]  # the two passing checks (Day 1 bullish, Day 2 bearish) are not listed


def test_explain_no_setup_heading_and_lines():
    d1, d2 = candle(MON, *MON_C), candle(TUE, *TUE_C)
    heading, *failed = sm.explain_no_setup(d1, d2, "EURNZD")
    assert heading == "SELL sweep (Day 1 closed bullish):"
    assert failed == _failed(sm.sweep_checks(d1, d2, SELL, "EURNZD"))
    # A bearish Day 1 is judged as a BUY sweep instead
    heading, *failed = sm.explain_no_setup(candle(MON, 105, 110, 90, 95), candle(TUE, 95, 108, 92, 100), "EURUSD")
    assert heading == "BUY sweep (Day 1 closed bearish):"
    assert "✗ needs Day 2 to sweep Day 1's low by ≥3 pips — but its low stayed 20000.0 pips above Day 1's low (92.00000 vs 90.00000)" in failed


def test_equal_values_say_equals_not_less_than():
    d1, d2 = candle(MON, 100, 110, 90, 105), candle(TUE, 105, 110, 95, 100)  # Day 2 high == Day 1 high
    assert "✗ needs Day 2 to sweep Day 1's high by ≥3 pips — but its high only matched Day 1's high (110.00000 vs 110.00000)" in _failed(
        sm.sweep_checks(d1, d2, SELL, "EURUSD")
    )


def test_buy_sweep_wording():
    d1, d2 = candle(MON, 105, 110, 90, 95), candle(TUE, 95, 112, 92, 100)  # bearish, bullish, low held, high swept
    assert _failed(sm.sweep_checks(d1, d2, BUY, "EURUSD")) == [
        "✗ needs Day 2 to sweep Day 1's low by ≥3 pips — but its low stayed 20000.0 pips above Day 1's low (92.00000 vs 90.00000)",
        "✗ needs Day 2 to stay ≥2 pips below Day 1's high — but it went 20000.0 pips beyond (112.00000 vs 110.00000)",
    ]


def test_continuation_failures_use_the_actual_numbers():
    d1, d2 = candle(MON, *MON_C), candle(TUE, *TUE_C)
    heading, *failed = sm.explain_no_continuation(d1, d2, "EURNZD")
    assert heading == "BUY continuation (Day 1 closed bullish):"
    assert failed == [
        "✗ needs Day 2 to close bullish — but Day 2 close 1.95849 < open 1.96693",
        "✗ needs Day 2 to close ≥3 pips above Day 1's high — but it closed 130.8 pips below (1.95849 vs 1.97157)",
        "✗ needs Day 2 to stay ≥2 pips above Day 1's low — but it went 104.6 pips beyond (1.95395 vs 1.96441)",
    ]  # the no-gap check passed (open 1.96693 < Day 1 high 1.97157), so it isn't listed


def test_continuation_sell_and_gap_wording():
    d1, d2 = candle(MON, 105, 110, 90, 95), candle(TUE, 89, 100, 80, 82)  # opened below Day 1's low
    assert _failed(sm.continuation_checks(d1, d2, SELL, "EURUSD")) == [
        "✗ needs Day 2 to open above Day 1's low (no gap) — but Day 2 open 89.00000 < Day 1 low 90.00000"
    ]


def test_prices_use_the_pairs_precision():
    d1, d2 = candle(MON, 110.0, 110.5, 109.5, 110.4), candle(TUE, 110.4, 110.45, 110.0, 110.1)
    jpy = _failed(sm.sweep_checks(d1, d2, SELL, "AUDJPY"))
    assert "✗ needs Day 2 to sweep Day 1's high by ≥3 pips — but its high stayed 5.0 pips below Day 1's high (110.450 vs 110.500)" in jpy  # 3 dp
    gold = _failed(sm.sweep_checks(candle(MON, 4017, 4065, 3989, 4060), candle(TUE, 4060, 4060, 4028, 4045), SELL, "XAUUSD"))
    assert "✗ needs Day 2 to sweep Day 1's high by ≥3 pips — but its high stayed 50.0 pips below Day 1's high (4,060.00 vs 4,065.00)" in gold  # 2 dp


def test_flat_day1_has_no_direction():
    heading, *rest = sm.explain_no_setup(candle(MON, 100, 110, 90, 100), candle(TUE, 100, 112, 95, 98), "EURUSD")
    assert heading == "✗ Day 1 is a doji (body 0.0 pips < 2) — neither direction applies" and rest == []


def test_every_failed_check_lists_what_the_pattern_needs():
    # None of the check texts may state a requirement as a fact: every failure line starts "✗ needs" and has "— but"
    d1, d2 = candle(MON, *MON_C), candle(TUE, *TUE_C)
    for checks in (sm.sweep_checks(d1, d2, SELL), sm.sweep_checks(d1, d2, BUY),
                   sm.continuation_checks(d1, d2, BUY), sm.continuation_checks(d1, d2, SELL)):
        for line in _failed(checks):
            assert re.fullmatch(r"✗ needs .+ — but .+", line)


def _check_message(day1, day2, now, pair="EURNZD"):
    svc = DailySweepService("k", _FakeMarketData(day_hours(MON, *day1) + day_hours(TUE, *day2)), None, None)
    svc.clock = lambda: now
    return asyncio.run(svc.get_check_message(pair))


def test_sweepcheck_message_wording_and_is_valid_telegram_html():
    text = _check_message(MON_C, TUE_C, ny(2026, 10, 27, 17, 10))
    assert "🔁 Sweep: no setup — SELL sweep (Day 1 closed bullish):" in text
    assert "✗ needs Day 2 to sweep Day 1&#x27;s high by ≥3 pips — but its high stayed 31.6 pips below Day 1&#x27;s high (1.96841 vs 1.97157)" in text
    assert "✗ needs Day 2 to stay ≥2 pips above Day 1&#x27;s low — but it went 104.6 pips beyond (1.95395 vs 1.96441)" in text
    assert "📈 Continuation: none — BUY continuation (Day 1 closed bullish):" in text
    # Telegram HTML: a raw < or > anywhere but in the <b> tags we use makes the whole message fail to send
    assert "<" not in re.sub(r"</?b>", "", text) and ">" not in re.sub(r"</?b>", "", text)


def test_scan_log_uses_the_same_wording(caplog):
    bars = day_hours(MON, *MON_C) + day_hours(TUE, *TUE_C)
    svc = DailySweepService("k", _FakeMarketData(bars), None, None)
    svc.clock = lambda: ny(2026, 10, 27, 17, 10)
    with caplog.at_level(logging.INFO):
        asyncio.run(svc._scan_pair("EURNZD", TUE))
    assert "no setup — SELL sweep (Day 1 closed bullish): ✗ needs Day 2 to sweep Day 1's high by ≥3 pips — but its high stayed 31.6 pips below Day 1's high (1.96841 vs 1.97157)" in caplog.text
    assert "continuation: none BUY continuation (Day 1 closed bullish): ✗ needs Day 2 to close bullish" in caplog.text
