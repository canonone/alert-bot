import asyncio
import datetime
import itertools
import random

import pytest

import app.services.daily_sweep_service as sm
from app.services.daily_sweep_service import (
    BUY,
    CONT_BUY,
    CONT_SELL,
    CONTINUATION_KIND,
    NO_CONTINUATION,
    SELL,
    SKIP_GAP,
    SKIP_OPPOSITE,
    DailySweepService,
    SetupState,
    classify_continuation,
    day3_expiry,
    detect_continuation,
    detect_sweep,
    latest_closed_trading_day,
)

# Shared fakes/helpers live with the sweep tests (pytest puts tests/ on sys.path)
from test_daily_sweep import (
    ARM_TIME,
    EXTRA_LINE,
    HOUR,
    D,
    _FakeMarketData,
    _FakeTwelveData,
    _FakeUsers,
    _new_service,
    _User,
    candle,
    day_hours,
    ny,
    utc,
)

PAIR = "AUDJPY"
FRI, MON, TUE = D(2026, 10, 23), D(2026, 10, 26), D(2026, 10, 27)

# (open, high, low, close) — modelled on AUDJPY, FOREX.com daily
FRI_C = (110.068, 110.443, 109.502, 110.349)
MON_C = (110.349, 110.762, 110.089, 110.515)
TUE_C = (110.515, 111.085, 110.362, 110.966)


def _state(day1_ohlc, day2_ohlc, day1_date, day2_date, stage=0, setup_id=1, pair=PAIR) -> SetupState:
    d1, d2 = candle(day1_date, *day1_ohlc), candle(day2_date, *day2_ohlc)
    signal = detect_continuation(d1, d2)
    assert signal is not None
    return SetupState(
        id=setup_id, pair=pair, direction=signal.direction,
        day1=day1_ohlc, day2=day2_ohlc, level=signal.level, day2_date=day2_date,
        day3_expires_at=day3_expiry(day2_date), kind=signal.kind, level2=signal.level2, stage=stage,
    )


def _db(state: SetupState) -> dict:
    return {"status": "ACTIVE", "stage": state.stage, "expires_at": state.day3_expires_at}


def _play(monkeypatch, state, prices):
    """Feed Day 3 ticks through the real check_tick/_trigger. Returns (service, sent, db, counts)
    where counts[i] is how many messages had been sent after tick i."""
    db = _db(state)
    svc, sent = _new_service(monkeypatch, db)
    svc._cache_add(state)
    counts = []
    for i, price in enumerate(prices):
        when = state.day3_expires_at - datetime.timedelta(hours=12) + HOUR * i
        svc.clock = lambda when=when: when
        asyncio.run(svc.check_tick(state.pair, price, None))
        counts.append(len(sent))
    return svc, sent, db, counts


# ── Detection: the pattern itself ───────────────────────────────────────────

def test_buy_continuation_levels_and_stop():
    d1, d2 = candle(MON, *MON_C), candle(TUE, *TUE_C)
    signal = detect_continuation(d1, d2)
    assert (signal.direction, signal.level, signal.level2, signal.sweep_extreme) == (BUY, 110.762, 110.515, 110.362)
    assert signal.level2 < signal.level  # Level 2 always below Level 1 for BUY
    assert detect_sweep(d1, d2) is None


def test_sell_continuation_levels_and_stop():
    d1 = candle(MON, 105, 110, 90, 95)  # bearish
    d2 = candle(TUE, 95, 100, 80, 85)  # bearish, closes below Day 1 low, high held, opened above the low
    signal = detect_continuation(d1, d2)
    assert (signal.direction, signal.level, signal.level2, signal.sweep_extreme) == (SELL, 90, 95, 100)
    assert signal.level2 > signal.level  # Level 2 always above Level 1 for SELL


# ── E. Skipped / non-matching cases ─────────────────────────────────────────

@pytest.mark.parametrize(
    "d1,d2,expected",
    [
        # BUY side: Day 1 (O,H,L,C) = bullish (100,110,90,105)
        ((100, 110, 90, 105), (111, 120, 100, 118), SKIP_GAP),  # opened above Day 1 high
        ((100, 110, 90, 105), (110, 120, 100, 118), SKIP_GAP),  # opened exactly AT Day 1 high
        ((100, 110, 90, 105), (105, 120, 85, 118), SKIP_OPPOSITE),  # took Day 1 low
        ((100, 110, 90, 105), (105, 120, 90, 118), SKIP_OPPOSITE),  # touched Day 1 low exactly
        ((100, 110, 90, 105), (105, 112, 100, 110), NO_CONTINUATION),  # closed exactly at Day 1 high
        ((100, 110, 90, 105), (105, 112, 100, 109), NO_CONTINUATION),  # closed inside Day 1's range
        ((105, 110, 90, 95), (96, 115, 92, 114), NO_CONTINUATION),  # Day 1 wrong colour (bearish)
        ((100, 110, 90, 100), (101, 115, 92, 114), NO_CONTINUATION),  # Day 1 flat
        ((100, 110, 90, 105), (112, 118, 100, 108), NO_CONTINUATION),  # Day 2 bearish
        ((100, 110, 90, 105), (105, 115, 100, 105), NO_CONTINUATION),  # Day 2 flat
        # SELL side: Day 1 bearish (105,110,90,95)
        ((105, 110, 90, 95), (89, 100, 80, 82), SKIP_GAP),  # opened below Day 1 low
        ((105, 110, 90, 95), (90, 100, 80, 82), SKIP_GAP),  # opened exactly AT Day 1 low
        ((105, 110, 90, 95), (95, 110, 80, 82), SKIP_OPPOSITE),  # took Day 1 high (equal)
        ((105, 110, 90, 95), (95, 115, 80, 82), SKIP_OPPOSITE),
        ((105, 110, 90, 95), (95, 100, 85, 90), NO_CONTINUATION),  # closed exactly at Day 1 low
        ((100, 110, 90, 105), (105, 108, 80, 85), NO_CONTINUATION),  # Day 1 wrong colour (bullish)
        ((105, 110, 90, 95), (85, 100, 80, 92), NO_CONTINUATION),  # Day 2 bullish
    ],
)
def test_skipped_and_non_matching_cases(d1, d2, expected):
    day1, day2 = candle(MON, *d1), candle(TUE, *d2)
    assert classify_continuation(day1, day2) == expected
    assert detect_continuation(day1, day2) is None


def test_classification_labels_for_real_setups():
    assert classify_continuation(candle(MON, *MON_C), candle(TUE, *TUE_C)) == CONT_BUY
    assert classify_continuation(candle(MON, 105, 110, 90, 95), candle(TUE, 95, 100, 80, 85)) == CONT_SELL


# ── G. The four patterns are mutually exclusive ─────────────────────────────

def _matches(d1, d2) -> list[str]:
    hits = []
    if all(ok for _, ok in sm.sweep_checks(d1, d2, SELL)):
        hits.append("sweep SELL")
    if all(ok for _, ok in sm.sweep_checks(d1, d2, BUY)):
        hits.append("sweep BUY")
    result = classify_continuation(d1, d2)
    if result == CONT_BUY:
        hits.append("continuation BUY")
    if result == CONT_SELL:
        hits.append("continuation SELL")
    return hits


def test_four_patterns_are_mutually_exclusive_exhaustive_grid():
    values = (1, 2, 3, 4)
    candles = [
        candle(MON, o, h, l, c)
        for o, c in itertools.product(values, values)
        for h in values if h >= max(o, c)
        for l in values if l <= min(o, c)
    ]
    seen: set[str] = set()
    for d1, d2 in itertools.product(candles, candles):
        hits = _matches(d1, d2)
        assert len(hits) <= 1, (d1, d2, hits)
        seen.update(hits)
    assert seen == {"sweep SELL", "sweep BUY", "continuation BUY", "continuation SELL"}  # grid isn't vacuous


def test_four_patterns_are_mutually_exclusive_random_floats():
    rng = random.Random(20261028)

    def rand_candle():
        o, c = rng.uniform(90, 110), rng.uniform(90, 110)
        return candle(MON, o, max(o, c) + rng.uniform(0, 8), min(o, c) - rng.uniform(0, 8), c)

    for _ in range(20000):
        assert len(_matches(rand_candle(), rand_candle())) <= 1


# ── A. Weekend case: Fri -> Mon BUY continuation, built from 1H bars ────────

def _weekend_frozen_bars():
    start = ny(2026, 10, 23, 17)  # Friday close -> Sunday reopen
    return [
        {"datetime": start + HOUR * i, "open": 110.349, "high": 110.3491, "low": 110.3489, "close": 110.349}
        for i in range(48)
    ]


def test_A_weekend_buy_continuation_ticks_and_silent_expiry(monkeypatch):
    bars = day_hours(FRI, *FRI_C) + _weekend_frozen_bars() + day_hours(MON, *MON_C)
    now = ny(2026, 10, 26, 17, 10)
    db = {"status": "ACTIVE", "stage": 0}
    svc, sent = _new_service(monkeypatch, db)
    svc.market_data = _FakeMarketData(bars)
    svc.clock = lambda: now
    stored = []

    async def fake_store(pair, signal, d1, d2, expires_at):
        stored.append((signal, d1.date, d2.date, expires_at))
        return SetupState(
            id=1, pair=pair, direction=signal.direction, day1=(d1.open, d1.high, d1.low, d1.close),
            day2=(d2.open, d2.high, d2.low, d2.close), level=signal.level, day2_date=d2.date,
            day3_expires_at=expires_at, kind=signal.kind, level2=signal.level2,
        )

    svc._store_setup = fake_store
    result = asyncio.run(svc._scan_pair(PAIR, latest_closed_trading_day(now)))
    assert result.status == "new"
    signal, d1_date, d2_date, expires_at = stored[0]
    assert (signal.kind, signal.direction) == (CONTINUATION_KIND, BUY)
    assert (signal.level, signal.level2) == (110.443, 110.349)
    assert (d1_date, d2_date) == (FRI, MON)  # Friday is the day before Monday
    assert expires_at == ny(2026, 10, 27, 17) == utc(2026, 10, 27, 21)

    state = result.setup
    db["expires_at"] = expires_at
    svc._cache_add(state)
    prices = [110.600, 110.450, 110.440, 110.362, 110.966]
    counts = []
    for i, price in enumerate(prices):
        svc.clock = lambda i=i: ny(2026, 10, 27, 1) + HOUR * i
        asyncio.run(svc.check_tick(PAIR, price, None))
        counts.append(len(sent))
    assert counts == [0, 0, 1, 1, 1]  # alert 1 only, on the 110.440 tick
    assert "CONTINUATION · LEVEL 1 HIT — AUDJPY (BUY)" in sent[0]
    assert "<b>Day 1 High (broken):</b> 110.443" in sent[0]
    assert "<b>Current Price:</b> 110.440" in sent[0]
    assert "<b>Next level — Day 2 Open:</b> 110.349" in sent[0]
    assert "<b>Day 2 Low:</b> 110.089 — invalidation / SL idea" in sent[0]
    assert (db["stage"], db["status"]) == (1, "ACTIVE")
    assert svc.active_symbols() == {PAIR}  # still armed for Level 2

    # After Day 3's close the tick that WOULD have hit Level 2 is ignored; nothing is sent
    svc.clock = lambda: expires_at + datetime.timedelta(seconds=1)
    asyncio.run(svc.check_tick(PAIR, 110.300, None))
    assert len(sent) == 1 and (db["stage"], db["status"]) == (1, "ACTIVE")


def test_expiry_at_stage_1_is_silent(monkeypatch):
    state = _state(FRI_C, MON_C, FRI, MON, stage=1)
    db = _db(state)
    svc, sent = _new_service(monkeypatch, db)
    svc._cache_add(state)

    class _Rows:
        def all(self):
            return [(state.id, PAIR, "BUY", CONTINUATION_KIND, 1)]

    class _ExpireSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, stmt):
            return _Rows()

        async def commit(self):
            pass

    monkeypatch.setattr(sm, "SessionLocal", lambda: _ExpireSession())
    assert asyncio.run(svc.expire_setups()) == 1
    assert sent == [] and svc.active_symbols() == set()  # logged, never messaged, gone from the cache


# ── B. Chained setup: Mon -> Tue BUY continuation ───────────────────────────

def test_B_chained_buy_continuation_two_stages(monkeypatch):
    state = _state(MON_C, TUE_C, MON, TUE)
    assert (state.level, state.level2) == (110.762, 110.515)
    assert state.day3_expires_at == ny(2026, 10, 28, 17)
    svc, sent, db, counts = _play(monkeypatch, state, [110.760, 110.600, 110.510, 110.400])
    assert counts == [1, 1, 2, 2]  # alert 1, nothing, alert 2, nothing
    assert "LEVEL 1 HIT" in sent[0] and "LEVEL 2 HIT" in sent[1]
    assert "<b>Day 2 Open:</b> 110.515" in sent[1]
    assert "didn't hold" in sent[1]
    assert (db["stage"], db["status"]) == (2, "TRIGGERED")
    assert db["claims"] == [(0, 1), (1, 2)]
    assert svc.active_symbols() == set()  # complete -> out of the cache


# ── C. One tick past both levels -> one combined message ────────────────────

def test_C_single_tick_past_both_levels_is_one_combined_message(monkeypatch):
    state = _state(MON_C, TUE_C, MON, TUE)
    svc, sent, db, counts = _play(monkeypatch, state, [110.500, 110.400, 110.800, 110.510])
    assert counts == [1, 1, 1, 1]
    assert "CONTINUATION · LEVELS 1 & 2 HIT — AUDJPY (BUY)" in sent[0]
    assert "<b>Day 1 High (broken):</b> 110.762" in sent[0] and "<b>Day 2 Open:</b> 110.515" in sent[0]
    assert EXTRA_LINE not in sent[0]
    assert (db["stage"], db["status"]) == (2, "TRIGGERED") and db["claims"] == [(0, 2)]
    assert svc.active_symbols() == set()


# ── F. SELL mirrors of A and B ──────────────────────────────────────────────

K = 220.0


def _mirror(ohlc):
    o, h, l, c = ohlc
    return (K - o, K - l, K - h, K - c)


def test_F_sell_mirror_of_A(monkeypatch):
    state = _state(_mirror(FRI_C), _mirror(MON_C), FRI, MON)
    assert state.direction == SELL
    assert state.level == pytest.approx(K - 110.443) and state.level2 == pytest.approx(K - 110.349)
    assert state.level2 > state.level
    assert state.day3_expires_at == utc(2026, 10, 27, 21)
    svc, sent, db, counts = _play(monkeypatch, state, [K - p for p in (110.600, 110.450, 110.440, 110.362, 110.966)])
    assert counts == [0, 0, 1, 1, 1]
    assert "CONTINUATION · LEVEL 1 HIT — AUDJPY (SELL)" in sent[0]
    assert "Day 1 Low (broken)" in sent[0] and "<b>Day 2 High:</b>" in sent[0]
    assert "bearish close below it" in sent[0]
    assert (db["stage"], db["status"]) == (1, "ACTIVE")


def test_F_sell_mirror_of_B(monkeypatch):
    state = _state(_mirror(MON_C), _mirror(TUE_C), MON, TUE)
    svc, sent, db, counts = _play(monkeypatch, state, [K - p for p in (110.760, 110.600, 110.510, 110.400)])
    assert counts == [1, 1, 2, 2]
    assert "LEVEL 1 HIT" in sent[0] and "LEVEL 2 HIT" in sent[1] and "(SELL)" in sent[1]
    assert (db["stage"], db["status"]) == (2, "TRIGGERED")


def test_F_sell_combined(monkeypatch):
    state = _state(_mirror(MON_C), _mirror(TUE_C), MON, TUE)
    _, sent, db, counts = _play(monkeypatch, state, [K - 110.500, K - 110.900])
    assert counts == [1, 1] and "LEVELS 1 & 2 HIT" in sent[0] and db["stage"] == 2


# ── D. Armed-time check applies to both levels ──────────────────────────────

def _armed(monkeypatch, last_price, tick_age=5, td=None):
    monkeypatch.setattr(sm, "SCAN_REQUEST_SPACING_SECONDS", 0)
    state = _state(MON_C, TUE_C, MON, TUE)  # BUY: L1 110.762, L2 110.515
    db = _db(state)
    svc, sent = _new_service(monkeypatch, db)
    svc.market_data = td or _FakeTwelveData()
    arm = ny(2026, 10, 27, 17, 10)
    svc.clock = lambda: arm
    svc.get_last_price = (
        (lambda symbol: None) if last_price is None
        else (lambda symbol: (last_price, arm - datetime.timedelta(seconds=tick_age)))
    )
    svc._cache_add(state)
    asyncio.run(svc._trigger_if_already_reached(state))
    return sent, db


def test_D_fresh_price_past_level1_only_sends_alert_1_with_extra_line(monkeypatch):
    sent, db = _armed(monkeypatch, 110.700)
    assert len(sent) == 1 and "LEVEL 1 HIT" in sent[0] and EXTRA_LINE in sent[0]
    assert (db["stage"], db["status"]) == (1, "ACTIVE")


def test_D_fresh_price_past_both_levels_sends_combined_with_extra_line(monkeypatch):
    sent, db = _armed(monkeypatch, 110.500)
    assert len(sent) == 1 and "LEVELS 1 & 2 HIT" in sent[0] and EXTRA_LINE in sent[0]
    assert (db["stage"], db["status"]) == (2, "TRIGGERED")


def test_D_price_short_of_level1_does_nothing(monkeypatch):
    sent, db = _armed(monkeypatch, 110.900)
    assert sent == [] and db["stage"] == 0


def test_D_stale_or_missing_price_does_nothing(monkeypatch):
    # Stale tick that WOULD hit both levels is ignored; Twelve Data has nothing either
    sent, db = _armed(monkeypatch, 110.500, tick_age=300, td=_FakeTwelveData(price=None))
    assert sent == [] and db["stage"] == 0
    sent, db = _armed(monkeypatch, None, td=_FakeTwelveData(price=None))
    assert sent == [] and db["stage"] == 0
    # ...but a stale tick falls back to Twelve Data, whose price is used
    sent, db = _armed(monkeypatch, 110.500, tick_age=300, td=_FakeTwelveData(price=110.700))
    assert len(sent) == 1 and "LEVEL 1 HIT" in sent[0] and db["stage"] == 1


def test_D_armed_at_level1_then_live_tick_sends_alert_2_only(monkeypatch):
    monkeypatch.setattr(sm, "SCAN_REQUEST_SPACING_SECONDS", 0)
    sent, db = _armed(monkeypatch, 110.700)  # stage 1 now
    assert len(sent) == 1
    state = _state(MON_C, TUE_C, MON, TUE, stage=1)
    svc, sent2, db2, counts = _play(monkeypatch, state, [110.700, 110.514])
    assert counts == [0, 1] and "LEVEL 2 HIT" in sent2[0] and EXTRA_LINE not in sent2[0]


# ── H. Atomic stages ────────────────────────────────────────────────────────

def test_H_duplicate_ticks_send_each_stage_once(monkeypatch):
    state = _state(MON_C, TUE_C, MON, TUE)
    _, sent, db, counts = _play(monkeypatch, state, [110.760, 110.760, 110.700, 110.510, 110.510, 110.400])
    assert counts == [1, 1, 1, 2, 2, 2]
    assert db["claims"] == [(0, 1), (1, 2)]


def test_H_concurrent_ticks_cannot_send_a_stage_twice(monkeypatch):
    state = _state(MON_C, TUE_C, MON, TUE)
    db = _db(state)
    svc, sent = _new_service(monkeypatch, db)
    svc._cache_add(state)
    svc.clock = lambda: state.day3_expires_at - datetime.timedelta(hours=5)

    async def storm():
        # Same price on five "simultaneous" ticks; the fake DB round trip yields in between
        await asyncio.gather(*(svc.check_tick(PAIR, 110.700, None) for _ in range(5)))
        await asyncio.gather(*(svc.check_tick(PAIR, 110.500, None) for _ in range(5)))

    asyncio.run(storm())
    assert len(sent) == 2 and "LEVEL 1 HIT" in sent[0] and "LEVEL 2 HIT" in sent[1]
    assert db["claims"] == [(0, 1), (1, 2)]


def test_H_two_processes_with_the_same_cache_send_a_stage_once(monkeypatch):
    # e.g. an overlapping deploy: both instances loaded the setup at stage 0 and see the same tick.
    # Only the database claim stands between them and a double send.
    state_a = _state(MON_C, TUE_C, MON, TUE)
    state_b = _state(MON_C, TUE_C, MON, TUE)
    db = _db(state_a)
    svc_a, sent_a = _new_service(monkeypatch, db)
    svc_b, sent_b = _new_service(monkeypatch, db)
    for svc, st in ((svc_a, state_a), (svc_b, state_b)):
        svc._cache_add(st)
        svc.clock = lambda: state_a.day3_expires_at - datetime.timedelta(hours=5)

    async def both():
        await asyncio.gather(svc_a.check_tick(PAIR, 110.700, None), svc_b.check_tick(PAIR, 110.700, None))

    asyncio.run(both())
    assert len(sent_a) + len(sent_b) == 1
    assert db["claims"] == [(0, 1)]


def test_H_claim_from_a_stale_stage_is_refused(monkeypatch):
    state = _state(MON_C, TUE_C, MON, TUE)
    db = _db(state)
    svc, _ = _new_service(monkeypatch, db)
    now = state.day3_expires_at - datetime.timedelta(hours=5)
    assert asyncio.run(svc._claim_stage(state, 0, 1, 110.7, now)) is True
    assert asyncio.run(svc._claim_stage(state, 0, 1, 110.7, now)) is False  # 0 -> 1 only ever once
    assert asyncio.run(svc._claim_stage(state, 1, 2, 110.5, now)) is True
    assert asyncio.run(svc._claim_stage(state, 1, 2, 110.5, now)) is False  # 1 -> 2 only ever once
    assert asyncio.run(svc._claim_stage(state, 0, 2, 110.5, now)) is False  # status is TRIGGERED now


def test_H_claim_refused_after_expiry(monkeypatch):
    state = _state(MON_C, TUE_C, MON, TUE)
    db = _db(state)
    svc, sent = _new_service(monkeypatch, db)
    svc._cache_add(state)
    asyncio.run(svc._trigger(state, 110.700, state.day3_expires_at + datetime.timedelta(seconds=1)))
    assert sent == [] and db["stage"] == 0 and db["status"] == "ACTIVE"


def test_H_restart_at_stage_1_does_not_resend_alert_1(monkeypatch):
    state = _state(MON_C, TUE_C, MON, TUE, stage=1)  # as bootstrap would reload it from the DB row
    _, sent, db, counts = _play(monkeypatch, state, [110.760, 110.700, 110.514])
    assert counts == [0, 0, 1]
    assert "LEVEL 2 HIT" in sent[0] and "LEVEL 1" not in sent[0]
    assert db["claims"] == [(1, 2)]


def test_H_database_error_keeps_setup_armed_and_sends_nothing(monkeypatch):
    state = _state(MON_C, TUE_C, MON, TUE)
    db = _db(state)
    svc, sent = _new_service(monkeypatch, db)
    svc._cache_add(state)
    real_session = sm.SessionLocal
    monkeypatch.setattr(sm, "SessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    now = state.day3_expires_at - datetime.timedelta(hours=5)
    svc.clock = lambda: now
    asyncio.run(svc.check_tick(PAIR, 110.500, None))  # would be the combined message
    assert sent == [] and state.stage == 0 and svc.active_symbols() == {PAIR}
    monkeypatch.setattr(sm, "SessionLocal", real_session)
    asyncio.run(svc.check_tick(PAIR, 110.500, None))  # DB is back -> retried on the next tick
    assert len(sent) == 1 and "LEVELS 1 & 2 HIT" in sent[0]


# ── I. Digest: each user only sees the sections they opted into ─────────────

def _sweep_state():
    return SetupState(
        id=1, pair="XAUUSD", direction=SELL,
        day1=(4017.30, 4065.10, 3989.20, 4060.12), day2=(4060.12, 4071.40, 4028.50, 4045.51),
        level=4060.12, day2_date=D(2026, 10, 27), day3_expires_at=utc(2026, 10, 28, 21),
    )


def _cont_state():
    return SetupState(
        id=2, pair=PAIR, direction=BUY,
        day1=MON_C, day2=TUE_C, level=110.762, level2=110.515,
        day2_date=D(2026, 10, 27), day3_expires_at=utc(2026, 10, 28, 21), kind=CONTINUATION_KIND,
    )


SPEC_DIGEST = """🗓 <b>Daily Setups</b>
━━━━━━━━━━━━━━━━━━━━
<b>🔁 Sweeps</b>
🔻 <b>XAUUSD</b> SELL @ 4,060.12 · sweep high 4,071.40

<b>📈 Continuations</b>
🔺 <b>AUDJPY</b> BUY
   ① Day 1 high: 110.762
   ② Day 2 open: 110.515
━━━━━━━━━━━━━━━━━━━━
⏳ Valid until Day 3 close: 2026-10-28 22:00 WAT"""


def test_I_digest_matches_the_spec_layout():
    assert sm.format_new_setups_digest([_sweep_state(), _cont_state()]) == SPEC_DIGEST


def test_I_digest_sections_follow_the_opt_ins():
    both = [_sweep_state(), _cont_state()]
    sweeps_only = sm.format_new_setups_digest(both, include_sweeps=True, include_continuations=False)
    assert "Sweeps" in sweeps_only and "Continuations" not in sweeps_only and "AUDJPY" not in sweeps_only
    cont_only = sm.format_new_setups_digest(both, include_sweeps=False, include_continuations=True)
    assert "Continuations" in cont_only and "Sweeps" not in cont_only and "XAUUSD" not in cont_only
    assert sm.format_new_setups_digest(both, False, False) is None
    assert sm.format_new_setups_digest([], True, True) is None
    # Nothing of the user's kind in this scan -> no message at all
    assert sm.format_new_setups_digest([_sweep_state()], include_sweeps=False, include_continuations=True) is None
    assert sm.format_new_setups_digest([_cont_state()], include_sweeps=True, include_continuations=False) is None


def test_I_broadcast_digest_sends_each_user_only_their_sections():
    svc = DailySweepService("k", None, None, None)
    sent: list[tuple[str, str]] = []

    async def send(chat_id, message):
        sent.append((chat_id, message))

    svc.send_message = send
    svc.users_service = _FakeUsers(
        [_User("sweeps", sweeps=True), _User("cont", continuations=True), _User("both", True, True), _User("none")]
    )
    asyncio.run(svc._broadcast_digest([_sweep_state(), _cont_state()]))
    by_user = dict(sent)
    assert set(by_user) == {"sweeps", "cont", "both"}  # opted-out users get nothing
    assert "XAUUSD" in by_user["sweeps"] and "AUDJPY" not in by_user["sweeps"]
    assert "AUDJPY" in by_user["cont"] and "XAUUSD" not in by_user["cont"]
    assert by_user["both"] == SPEC_DIGEST

    sent.clear()
    asyncio.run(svc._broadcast_digest([_sweep_state()]))  # sweeps only in this scan
    assert [chat for chat, _ in sent] == ["sweeps", "both"]  # the continuation-only user is skipped


def test_hits_go_to_the_matching_feeds_subscribers():
    svc = DailySweepService("k", None, None, None)
    sent = []

    async def send(chat_id, message):
        sent.append(chat_id)

    svc.send_message = send
    svc.users_service = _FakeUsers([_User("s", sweeps=True), _User("c", continuations=True)])
    asyncio.run(svc._broadcast("m", "SWEEP"))
    asyncio.run(svc._broadcast("m", CONTINUATION_KIND))
    assert sent == ["s", "c"]


# ── Messages and commands ───────────────────────────────────────────────────

def test_continuation_hit_wording_buy_and_sell():
    buy = _state(MON_C, TUE_C, MON, TUE)
    now = utc(2026, 10, 28, 8, 14)
    one = sm.format_continuation_hit(buy, 110.758, now, "1")
    assert "<b>CONTINUATION · LEVEL 1 HIT — AUDJPY (BUY)</b>" in one
    assert "① <b>Day 1 High (broken):</b> 110.762" in one and "<b>Current Price:</b> 110.758" in one
    assert "② <b>Next level — Day 2 Open:</b> 110.515" in one
    assert "🛡 <b>Day 2 Low:</b> 110.362 — invalidation / SL idea" in one
    assert "<b>Day 1:</b> O 110.349 · H 110.762 · L 110.089 · C 110.515" in one
    assert "2026-10-28 09:14 WAT" in one
    assert "✅ Price pulled back to the broken Day 1 high after a bullish close above it." in one
    two = sm.format_continuation_hit(buy, 110.510, now, "2")
    assert "LEVEL 2 HIT" in two and "② <b>Day 2 Open:</b> 110.515" in two
    assert "⚠️ Day 1's high didn't hold — price is now at Day 2's open, the deepest level for this setup." in two
    sell = _state(_mirror(MON_C), _mirror(TUE_C), MON, TUE)
    text = sm.format_continuation_hit(sell, K - 110.760, now, "1")
    assert "(SELL)" in text and "Day 1 Low (broken)" in text and "🛡 <b>Day 2 High:</b>" in text
    assert "✅ Price pulled back to the broken Day 1 low after a bearish close below it." in text


def test_sweeps_command_lists_both_kinds_with_stage():
    svc = DailySweepService("k", None, None, None)
    states = [_sweep_state(), _state(MON_C, TUE_C, MON, TUE, stage=1, setup_id=3)]

    async def fake_active():
        return states

    svc.list_active = fake_active
    text = asyncio.run(svc.get_sweeps_message())
    assert "🔁 SWEEP" in text and "XAUUSD" in text
    assert "📈 CONTINUATION" in text
    assert "① Day 1 high: 110.762 — ✅ hit" in text
    assert "② Day 2 open: 110.515 — ⏳ waiting" in text
    states[1].stage = 0
    assert "② Day 2 open: 110.515 — ⏳ after ①" in asyncio.run(svc.get_sweeps_message())


def _check_message(day1_ohlc, day2_ohlc, d1_date, d2_date, now):
    svc = DailySweepService("k", _FakeMarketData(day_hours(d1_date, *day1_ohlc) + day_hours(d2_date, *day2_ohlc)), None, None)
    svc.clock = lambda: now
    return asyncio.run(svc.get_check_message("EURUSD"))


def test_sweepcheck_reports_continuation_classification():
    now = ny(2026, 10, 27, 17, 10)
    text = _check_message((100, 110, 90, 105), (105, 112, 100, 118), MON, TUE, now)
    assert "Continuation BUY setup" in text and "① Day 1 high 110.00000" in text and "② Day 2 open 105.00000" in text
    assert "Sweep: no setup" in text
    # A first 1H bar that opens above Day 1's high used to look like a gap; Day 2's open is now Day 1's
    # close, so the same bars give an ordinary continuation (the gap rule is covered on direct candles above)
    gapped = _check_message((100, 110, 90, 105), (111, 120, 100, 118), MON, TUE, now)
    assert "Continuation BUY setup" in gapped and "skipped (gap)" not in gapped
    assert "Continuation: skipped (took opposite side)" in _check_message(
        (100, 110, 90, 105), (105, 120, 85, 118), MON, TUE, now
    )
    assert "Continuation: none" in _check_message((100, 110, 90, 105), (105, 112, 100, 108), MON, TUE, now)
    sweep = _check_message((100, 110, 90, 105), (105, 112, 95, 100), MON, TUE, now)
    assert "Sweep SELL setup" in sweep and "Continuation: none" in sweep


# ── Shared machinery: one fetch per pair serves both features ───────────────

def test_one_candle_fetch_per_pair_serves_both_features(monkeypatch):
    class _Counting(_FakeMarketData):
        calls = 0

        async def get_candles(self, *args, **kwargs):
            _Counting.calls += 1
            return await super().get_candles(*args, **kwargs)

    now = ny(2026, 10, 27, 17, 10)
    bars = day_hours(MON, *MON_C) + day_hours(TUE, *TUE_C)
    svc = DailySweepService("k", _Counting(bars), None, None)
    svc.clock = lambda: now
    kinds = []

    async def fake_store(pair, signal, d1, d2, expires_at):
        kinds.append(signal.kind)
        return None

    svc._store_setup = fake_store
    asyncio.run(svc._scan_pair(PAIR, latest_closed_trading_day(now)))
    assert _Counting.calls == 1 and kinds == [CONTINUATION_KIND]

    # A sweep Day 2 through the same path is stored as a SWEEP
    kinds.clear()
    sweep_bars = day_hours(MON, 100, 110, 90, 105) + day_hours(TUE, 105, 112, 95, 100)
    svc = DailySweepService("k", _Counting(sweep_bars), None, None)
    svc.clock = lambda: now
    svc._store_setup = fake_store
    asyncio.run(svc._scan_pair("EURUSD", latest_closed_trading_day(now)))
    assert kinds == ["SWEEP"]


def test_setup_state_stage_progression():
    state = _state(MON_C, TUE_C, MON, TUE)
    assert state.final_stage == 2
    assert state.next_stage(110.900) is None  # not reached
    assert state.next_stage(110.762) == 1  # exactly at Level 1
    assert state.next_stage(110.515) == 2  # exactly at Level 2 from stage 0 -> combined
    state.stage = 1
    assert state.next_stage(110.700) is None and state.next_stage(110.515) == 2
    state.stage = 2
    assert state.next_stage(100.0) is None
    sweep = _sweep_state()
    assert sweep.final_stage == 1 and sweep.next_stage(4060.12) == 1
    sweep.stage = 1
    assert sweep.next_stage(5000) is None
