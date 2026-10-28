"""Minimum margins (MIN_BEYOND_PIPS / MIN_BODY_PIPS) and merged continuation levels (MERGE_LEVELS_PIPS).
Pip = 0.0001; 0.01 for JPY pairs; 0.1 for XAUUSD."""
import asyncio
import datetime

import pytest

import app.services.daily_sweep_service as sm
from app.services.daily_sweep_service import (
    BUY,
    CONT_BUY,
    CONT_SELL,
    NO_CONTINUATION,
    SELL,
    DailySweepService,
    SetupState,
    classify_continuation,
    detect_continuation,
    detect_sweep,
)

from test_daily_continuation import _db, _play, _state
from test_daily_sweep import EXTRA_LINE, D, _FakeMarketData, _FakeTwelveData, _new_service, candle, day_hours, ny

MON, TUE = D(2026, 10, 26), D(2026, 10, 27)
FX = "EURUSD"


def c(day, o, h, l, cl):
    return candle(day, o, h, l, cl)


def sweep(d1, d2, pair=FX):
    return detect_sweep(d1, d2, pair)


# ── Today's live data ───────────────────────────────────────────────────────

CADJPY_D1 = (112.018, 112.407, 111.775, 112.397)
CADJPY_D2 = (112.397, 112.614, 112.333, 112.408)
# EURCAD Day 1's open wasn't in the log; 1.57300 is assumed (any open that makes Day 1 clearly bearish works)
EURCAD_D1 = (1.57300, 1.57677, 1.56564, 1.56588)
EURCAD_D2 = (1.56588, 1.56668, 1.56072, 1.56349)


def test_cadjpy_no_setup_close_only_a_tenth_of_a_pip_beyond():
    d1, d2 = c(MON, *CADJPY_D1), c(TUE, *CADJPY_D2)
    assert detect_continuation(d1, d2, "CADJPY") is None
    assert classify_continuation(d1, d2, "CADJPY") == NO_CONTINUATION
    assert detect_sweep(d1, d2, "CADJPY") is None
    heading, *failed = sm.explain_no_continuation(d1, d2, "CADJPY")
    assert heading == "BUY continuation (Day 1 closed bullish):"
    assert failed == [
        "✗ Day 2 is a doji (body 1.1 pips < 2)",
        "✗ needs Day 2 to close ≥3 pips above Day 1's high — but it closed 0.1 pips above (112.408 vs 112.407)",
    ]
    # The sweep reading fails on the doji too (the wick did clear Day 1's high by 20.7 pips)
    assert sm.explain_no_setup(d1, d2, "CADJPY")[1:] == ["✗ Day 2 is a doji (body 1.1 pips < 2)"]


def test_cadjpy_sweepcheck_shows_the_margins():
    bars = day_hours(MON, *CADJPY_D1) + day_hours(TUE, *CADJPY_D2)
    svc = DailySweepService("k", _FakeMarketData(bars), None, None)
    svc.clock = lambda: ny(2026, 10, 27, 17, 10)
    text = asyncio.run(svc.get_check_message("CADJPY"))
    assert "✗ Day 2 is a doji (body 1.1 pips &lt; 2)" in text
    assert "✗ needs Day 2 to close ≥3 pips above Day 1&#x27;s high — but it closed 0.1 pips above (112.408 vs 112.407)" in text
    assert "Continuation BUY setup" not in text and "Sweep SELL setup" not in text


def test_eurcad_still_a_continuation_sell_with_merged_levels():
    d1, d2 = c(MON, *EURCAD_D1), c(TUE, *EURCAD_D2)
    signal = detect_continuation(d1, d2, "EURCAD")
    assert signal is not None and signal.direction == SELL
    assert (signal.level, signal.level2) == (1.56564, 1.56588)
    assert classify_continuation(d1, d2, "EURCAD") == CONT_SELL
    state = _state(EURCAD_D1, EURCAD_D2, MON, TUE, pair="EURCAD")
    assert state.levels_apart_pips == pytest.approx(2.4) and state.merged is True


def test_eurcad_merged_levels_send_one_alert_at_level_1(monkeypatch):
    state = _state(EURCAD_D1, EURCAD_D2, MON, TUE, pair="EURCAD")
    # SELL: price rises toward Level 1 (1.56564); Level 2 (1.56588) sits 2.4 pips above it
    svc, sent, db, counts = _play(monkeypatch, state, [1.56300, 1.56560, 1.56565, 1.56600, 1.56700])
    assert counts == [0, 0, 1, 1, 1]  # one message, on the first tick at/through Level 1; nothing afterwards
    assert "LEVELS 1 & 2 HIT — EURCAD (SELL)" in sent[0]
    assert "ℹ️ Levels are only 2.4 pips apart — treated as one zone." in sent[0]
    assert "<b>Current Price:</b> 1.56565" in sent[0]
    assert "Next level" not in sent[0]
    assert (db["stage"], db["status"]) == (2, "TRIGGERED") and db["claims"] == [(0, 2)]
    assert svc.active_symbols() == set()


def test_levels_ten_pips_apart_still_send_two_alerts(monkeypatch):
    d1, d2 = (1.1000, 1.1050, 1.0950, 1.1040), (1.1040, 1.1090, 1.1020, 1.1070)  # L1 1.1050, L2 1.1040
    state = _state(d1, d2, MON, TUE, pair=FX)
    assert state.direction == BUY and state.levels_apart_pips == pytest.approx(10.0) and not state.merged
    _, sent, db, counts = _play(monkeypatch, state, [1.1060, 1.1049, 1.1045, 1.1040, 1.1030])
    assert counts == [0, 1, 1, 2, 2]
    assert "LEVEL 1 HIT" in sent[0] and "LEVEL 2 HIT" in sent[1] and "treated as one zone" not in "".join(sent)
    assert db["claims"] == [(0, 1), (1, 2)]


def test_merge_threshold_is_inclusive_and_per_pair(monkeypatch):
    def at(pair, level, level2):
        return SetupState(id=1, pair=pair, direction=BUY, day1=(1, 2, 0, 1), day2=(1, 2, 0, 1), level=level,
                          day2_date=TUE, day3_expires_at=ny(2026, 10, 28, 17), kind="CONTINUATION", level2=level2)

    assert at(FX, 1.1050, 1.1047).merged  # exactly 3.0 pips
    assert not at(FX, 1.1050, 1.10468).merged  # 3.2 pips
    assert at("AUDJPY", 110.50, 110.47).merged and not at("AUDJPY", 110.50, 110.46).merged  # JPY pip = 0.01
    assert at("XAUUSD", 4060.0, 4059.7).merged and not at("XAUUSD", 4060.0, 4059.6).merged  # gold pip = 0.1
    monkeypatch.setitem(sm.PAIR_MERGE_LEVELS_PIPS, FX, 12.0)
    assert at(FX, 1.1050, 1.1040).merged  # 10 pips, now within this pair's 12
    sweep_state = SetupState(id=2, pair=FX, direction=SELL, day1=(1, 2, 0, 1), day2=(1, 2, 0, 1), level=1.1,
                             day2_date=TUE, day3_expires_at=ny(2026, 10, 28, 17))
    assert sweep_state.merged is False and sweep_state.levels_apart_pips is None


def test_merged_setup_armed_with_price_past_level_1_sends_one_message_with_both_notes(monkeypatch):
    monkeypatch.setattr(sm, "SCAN_REQUEST_SPACING_SECONDS", 0)
    state = _state(EURCAD_D1, EURCAD_D2, MON, TUE, pair="EURCAD")
    db = _db(state)
    svc, sent = _new_service(monkeypatch, db)
    svc.market_data = _FakeTwelveData()
    arm = ny(2026, 10, 27, 17, 10)
    svc.clock = lambda: arm
    svc.get_last_price = lambda symbol: (1.56570, arm)  # past Level 1 (1.56564), short of Level 2 (1.56588)
    svc._cache_add(state)
    asyncio.run(svc._trigger_if_already_reached(state))
    assert len(sent) == 1 and EXTRA_LINE in sent[0] and "treated as one zone" in sent[0]
    assert (db["stage"], db["status"]) == (2, "TRIGGERED")


def test_single_alert_shown_in_digest_sweepcheck_and_sweeps():
    merged = _state(EURCAD_D1, EURCAD_D2, MON, TUE, pair="EURCAD")
    apart = _state((1.1000, 1.1050, 1.0950, 1.1040), (1.1040, 1.1090, 1.1020, 1.1070), MON, TUE, pair=FX, setup_id=2)
    digest = sm.format_new_setups_digest([merged, apart])
    assert "   single alert (levels 2.4 pips apart)" in digest
    assert digest.count("single alert") == 1  # only the merged one

    bars = day_hours(MON, *EURCAD_D1) + day_hours(TUE, *EURCAD_D2)
    svc = DailySweepService("k", _FakeMarketData(bars), None, None)
    svc.clock = lambda: ny(2026, 10, 27, 17, 10)
    check = asyncio.run(svc.get_check_message("EURCAD"))
    assert "Continuation SELL setup" in check and "single alert (levels 2.4 pips apart)" in check

    async def active():
        return [merged, apart]

    svc.list_active = active
    listing = asyncio.run(svc.get_sweeps_message())
    assert listing.count("single alert") == 1 and "levels 2.4 pips apart" in listing


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


def test_sweep_still_needs_the_opposite_side_untouched_with_no_margin():
    d1 = c(MON, *SELL_D1)
    # Low 1 pip ABOVE Day 1's low is fine — "didn't take the low" is strict, with no margin
    assert sweep(d1, c(TUE, 1.1030, 1.1060, 1.0951, 1.1000)) is not None
    assert sweep(d1, c(TUE, 1.1030, 1.1060, 1.0950, 1.1000)) is None  # equal to the low: taken
    assert sweep(d1, c(TUE, 1.1030, 1.1060, 1.0949, 1.1000)) is None  # below it


# ── Continuation: the CLOSE must clear Day 1's extreme by MIN_BEYOND_PIPS ───

BUY_D1 = (1.1000, 1.1050, 1.0950, 1.1040)  # bullish, high 1.1050


def buy_d2(close, low=1.1020):
    return (1.1040, 1.1090, low, close)


def test_continuation_buy_close_margin():
    d1 = c(MON, *BUY_D1)
    assert detect_continuation(d1, c(TUE, *buy_d2(1.1051)), FX) is None  # 1 pip beyond
    assert detect_continuation(d1, c(TUE, *buy_d2(1.1050)), FX) is None  # level with the high
    assert detect_continuation(d1, c(TUE, *buy_d2(1.10529)), FX) is None  # 2.9 pips
    assert detect_continuation(d1, c(TUE, *buy_d2(1.1053)), FX).direction == BUY  # exactly 3 pips
    assert detect_continuation(d1, c(TUE, *buy_d2(1.1055)), FX).direction == BUY  # 5 pips
    # "Did not take the low" stays strict: 1 pip above Day 1's low is enough, equal is not
    assert detect_continuation(d1, c(TUE, *buy_d2(1.1060, low=1.0951)), FX) is not None
    assert classify_continuation(d1, c(TUE, *buy_d2(1.1060, low=1.0950)), FX) == sm.SKIP_OPPOSITE


def test_continuation_sell_close_margin():
    d1 = c(MON, 1.1040, 1.1050, 1.0950, 1.1000)  # bearish, low 1.0950
    d2 = lambda close, high=1.1030: c(TUE, 1.1000, high, 1.0900, close)  # noqa: E731
    assert detect_continuation(d1, d2(1.0949), FX) is None  # 1 pip below
    assert detect_continuation(d1, d2(1.0950), FX) is None
    assert detect_continuation(d1, d2(1.0947), FX).direction == SELL  # exactly 3 pips
    assert detect_continuation(d1, d2(1.0945), FX).direction == SELL
    assert classify_continuation(d1, d2(1.0945, high=1.1050), FX) == sm.SKIP_OPPOSITE  # took Day 1's high


# ── Dojis (MIN_BODY_PIPS) ───────────────────────────────────────────────────

def test_doji_day1_matches_no_pattern():
    d1 = c(MON, 1.1000, 1.1050, 1.0950, 1.10015)  # body 1.5 pips
    d2 = c(TUE, 1.10015, 1.1090, 1.1020, 1.1070)  # would be a clean BUY continuation after a bullish Day 1
    assert detect_continuation(d1, d2, FX) is None and detect_sweep(d1, d2, FX) is None
    assert sm.explain_no_continuation(d1, d2, FX) == ["✗ Day 1 is a doji (body 1.5 pips < 2) — neither direction applies"]
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
    d1 = c(MON, 1.1000, 1.1050, 1.0950, 1.0960)  # clearly bearish -> judged as a SELL continuation
    _, *failed = sm.explain_no_continuation(d1, c(TUE, 1.0960, 1.0990, 1.0900, 1.0985), FX)  # Day 2 bullish, 25 pip body
    assert failed == [
        "✗ needs Day 2 to close bearish — but Day 2 close 1.09850 > open 1.09600",
        "✗ needs Day 2 to close ≥3 pips below Day 1's low — but it closed 35.0 pips above (1.09850 vs 1.09500)",
    ]


# ── Pips per instrument, per-pair overrides ─────────────────────────────────

def test_margin_uses_the_right_pip_for_jpy_and_gold():
    jpy_d1 = c(MON, 110.00, 110.50, 109.50, 110.40)
    assert detect_continuation(jpy_d1, c(TUE, 110.40, 111.0, 110.20, 110.52), "AUDJPY") is None  # 2 pips beyond
    assert detect_continuation(jpy_d1, c(TUE, 110.40, 111.0, 110.20, 110.53), "AUDJPY") is not None  # 3 pips
    gold_d1 = c(MON, 4000.0, 4050.0, 3980.0, 4040.0)
    assert detect_continuation(gold_d1, c(TUE, 4040.0, 4100.0, 4020.0, 4050.2), "XAUUSD") is None  # 2 pips = 0.2
    assert detect_continuation(gold_d1, c(TUE, 4040.0, 4100.0, 4020.0, 4050.3), "XAUUSD") is not None  # 3 pips = 0.3
    assert (sm.pip_size("EURUSD"), sm.pip_size("USDJPY"), sm.pip_size("XAUUSD")) == (0.0001, 0.01, 0.1)


def test_min_beyond_is_a_per_pair_setting(monkeypatch):
    d1 = c(MON, *SELL_D1)
    five = c(TUE, *sell_d2(1.1055))
    assert sweep(d1, five) is not None
    monkeypatch.setitem(sm.PAIR_MIN_BEYOND_PIPS, FX, 10.0)
    assert sweep(d1, five) is None  # needs 10 now
    assert sweep(d1, five, "GBPUSD") is not None  # other pairs keep the default 3
    assert "by ≥10 pips" in sm.explain_no_setup(d1, five, FX)[1]
