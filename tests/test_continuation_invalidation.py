"""A continuation setup is voided if, on Day 3, price sweeps Day 2's extreme (BUY: Day 2 high, SELL: Day 2 low)
by more than SWEEP_INVALIDATION_BUFFER_PIPS BEFORE Level 1 has fired. Sweep setups are never affected."""
import asyncio
import datetime

import pytest

import app.services.daily_sweep_service as sm
from app.services.daily_sweep_service import (
    BUY,
    SELL,
    DailySweepService,
    SetupState,
    extremes_since,
    format_invalidation,
    swept_day2_extreme,
)

from test_daily_continuation import K, MON_C, PAIR, TUE_C, _db, _mirror, _play, _sweep_state
from test_daily_continuation import _state as _cont_state_from
from test_daily_sweep import HOUR, D, _FakeMarketData, _FakeUsers, _new_service, _User, _FakeSession, day_hours, ny, utc

MON, TUE = D(2026, 10, 26), D(2026, 10, 27)

# AUDJPY BUY continuation: Day 2 high 111.085, low 110.362; Level 1 110.762, Level 2 110.515; pip 0.01, buffer 2 pips.
SWEEP = 111.110  # 2.5 pips above Day 2's high: a sweep
EXACT = 111.105  # exactly the 2-pip buffer: NOT a sweep
INSIDE = 111.095  # 1 pip above Day 2's high: NOT a sweep
L1, L2 = 110.760, 110.510


def case(direction):
    """(state, f) where f maps a BUY-side price to this direction's price (SELL is the mirror image)."""
    if direction == BUY:
        return _cont_state_from(MON_C, TUE_C, MON, TUE), (lambda p: p)
    return _cont_state_from(_mirror(MON_C), _mirror(TUE_C), MON, TUE), (lambda p: K - p)


both = pytest.mark.parametrize("direction", [BUY, SELL])


def types(sent):
    return ["INVALIDATED" if "SETUP INVALIDATED" in m else "L1" if "LEVEL 1 HIT" in m else "L2" if "LEVEL 2 HIT" in m
            else "BOTH" if "LEVELS 1 & 2" in m else "?" for m in sent]


# ── The rule, tick by tick ──────────────────────────────────────────────────

@both
def test_sweep_before_level_1_invalidates_and_later_retrace_does_not_alert(direction):
    state, f = case(direction)
    svc, sent, db, counts = _play_mp(state, [f(SWEEP), f(L1), f(L2), f(110.400)])
    assert counts == [1, 1, 1, 1] and types(sent) == ["INVALIDATED"]
    assert (db["status"], db["stage"]) == ("INVALIDATED", 0) and db["claims"] == [(0, "INVALIDATED")]
    assert svc.active_symbols() == set()  # out of the cache


def _play_mp(state, prices):
    with pytest.MonkeyPatch.context() as mp:
        return _play(mp, state, prices)


@both
def test_exactly_the_buffer_or_less_is_not_a_sweep_and_later_triggers_normally(direction):
    state, f = case(direction)
    svc, sent, db, counts = _play_mp(state, [f(INSIDE), f(EXACT), f(L1), f(L2)])
    assert types(sent) == ["L1", "L2"] and counts == [0, 0, 1, 2]
    assert db["status"] == "TRIGGERED" and db["claims"] == [(0, 1), (1, 2)]


@both
def test_just_over_the_buffer_is_a_sweep(direction):
    state, f = case(direction)
    _, sent, db, _ = _play_mp(state, [f(111.106)])  # 2.1 pips
    assert types(sent) == ["INVALIDATED"] and db["status"] == "INVALIDATED"


@both
def test_level_1_first_then_sweep_then_level_2_still_fires(direction):
    state, f = case(direction)
    svc, sent, db, counts = _play_mp(state, [f(L1), f(111.500), f(L2)])
    assert types(sent) == ["L1", "L2"] and counts == [1, 1, 2]  # the sweep at stage 1 is ignored
    assert db["status"] == "TRIGGERED" and "INVALIDATED" not in {c[1] for c in db["claims"]}


@both
def test_without_a_sweep_behaviour_is_unchanged(direction):
    state, f = case(direction)
    _, sent, db, counts = _play_mp(state, [f(110.900), f(L1), f(110.600), f(L2), f(110.400)])
    assert types(sent) == ["L1", "L2"] and counts == [0, 1, 1, 2, 2]
    state, f = case(direction)
    _, sent, db, _ = _play_mp(state, [f(110.500)])  # a price past both levels: one combined message
    assert types(sent) == ["BOTH"] and (db["stage"], db["status"]) == (2, "TRIGGERED")


# ── Merged levels (EURCAD SELL: Day 2 low 1.56072, levels 2.4 pips apart) ────

EURCAD_D1 = (1.57300, 1.57677, 1.56564, 1.56588)
EURCAD_D2 = (1.56588, 1.56668, 1.56072, 1.56349)


def test_merged_levels_are_unaffected_without_a_sweep_and_invalidated_with_one():
    state = _cont_state_from(EURCAD_D1, EURCAD_D2, MON, TUE, pair="EURCAD")
    assert state.merged and state.direction == SELL
    _, sent, db, _ = _play_mp(state, [1.56300, 1.56565])  # no sweep: the one merged alert
    assert len(sent) == 1 and "treated as one zone" in sent[0] and db["status"] == "TRIGGERED"
    state = _cont_state_from(EURCAD_D1, EURCAD_D2, MON, TUE, pair="EURCAD")
    # SELL: sweeping Day 2's LOW — more than 2 pips below 1.56072 — before the merged zone
    _, sent, db, _ = _play_mp(state, [1.56040, 1.56565])
    assert types(sent) == ["INVALIDATED"] and db["status"] == "INVALIDATED"


# ── Sweep setups are never invalidated ──────────────────────────────────────

def test_sweep_setups_are_never_invalidated():
    sweep = _sweep_state()  # XAUUSD SELL sweep: Day 2 high 4071.40, low 4028.50, level 4060.12
    for price in (3000.0, 4028.0, 4071.50, 4090.0, 9000.0):
        assert sweep.invalidates(price) is False
    with pytest.MonkeyPatch.context() as mp:
        db = {"status": "ACTIVE", "expires_at": sweep.day3_expires_at}
        svc, sent = _new_service(mp, db)
        svc._cache_add(sweep)
        svc.clock = lambda: sweep.day3_expires_at - 5 * HOUR
        asyncio.run(svc.check_tick("XAUUSD", 4100.0, None))  # far above Day 2's high AND through the level
        assert len(sent) == 1 and "DAILY SWEEP LEVEL HIT" in sent[0] and "INVALIDATED" not in sent[0]
        assert db["status"] == "TRIGGERED"


def test_a_continuation_past_stage_0_is_not_invalidated():
    state, _ = case(BUY)
    assert state.invalidates(SWEEP) is True
    state.stage = 1
    assert state.invalidates(SWEEP) is False


# ── Atomicity ───────────────────────────────────────────────────────────────

def _service(mp, state, db=None):
    db = db if db is not None else _db(state)
    svc, sent = _new_service(mp, db)
    svc._cache_add(state)
    svc.clock = lambda: state.day3_expires_at - 5 * HOUR
    return svc, sent, db


@pytest.mark.parametrize("sweep_first", [True, False])
def test_concurrent_trigger_and_invalidation_in_one_process_yield_one_outcome(sweep_first):
    state, _ = case(BUY)
    with pytest.MonkeyPatch.context() as mp:
        svc, sent, db = _service(mp, state)

        async def go():
            sweep, trigger = svc.check_tick(PAIR, SWEEP, None), svc.check_tick(PAIR, L1, None)
            await asyncio.gather(*((sweep, trigger) if sweep_first else (trigger, sweep)))

        asyncio.run(go())
    assert len(sent) == 1
    if sweep_first:
        assert types(sent) == ["INVALIDATED"] and db["status"] == "INVALIDATED" and db["claims"] == [(0, "INVALIDATED")]
    else:
        assert types(sent) == ["L1"] and db["status"] == "ACTIVE" and db["stage"] == 1 and db["claims"] == [(0, 1)]


@pytest.mark.parametrize("sweep_first", [True, False])
def test_two_instances_racing_on_the_database_yield_one_outcome(sweep_first):
    """Both instances loaded the setup at stage 0; one sees the sweep tick, the other the Level 1 tick."""
    a_state, _ = case(BUY)
    b_state, _ = case(BUY)
    db = _db(a_state)
    with pytest.MonkeyPatch.context() as mp:
        a, sent_a, _ = _service(mp, a_state, db)
        b, sent_b, _ = _service(mp, b_state, db)

        async def go():
            sweep, trigger = a.check_tick(PAIR, SWEEP, None), b.check_tick(PAIR, L1, None)
            await asyncio.gather(*((sweep, trigger) if sweep_first else (trigger, sweep)))

        asyncio.run(go())
    assert len(sent_a) + len(sent_b) == 1 and len(db["claims"]) == 1
    assert (db["status"] == "INVALIDATED") == bool(sent_a and "SETUP INVALIDATED" in sent_a[0])


def test_duplicate_and_concurrent_sweep_ticks_notify_once():
    state, _ = case(BUY)
    with pytest.MonkeyPatch.context() as mp:
        svc, sent, db = _service(mp, state)

        async def storm():
            await asyncio.gather(*(svc.check_tick(PAIR, SWEEP, None) for _ in range(5)))
            await svc.check_tick(PAIR, SWEEP + 0.5, None)

        asyncio.run(storm())
    assert types(sent) == ["INVALIDATED"] and db["claims"] == [(0, "INVALIDATED")]


def test_invalidation_is_a_noop_after_expiry():
    state, _ = case(BUY)
    with pytest.MonkeyPatch.context() as mp:
        svc, sent, db = _service(mp, state)
        late = state.day3_expires_at + datetime.timedelta(seconds=1)
        asyncio.run(svc._invalidate(state, SWEEP, state.day3_expires_at))  # exactly at expiry: not "> now"
        asyncio.run(svc._invalidate(state, SWEEP, late))
        svc.clock = lambda: late
        asyncio.run(svc.check_tick(PAIR, SWEEP, None))  # and the tick path skips expired setups altogether
    assert sent == [] and db["status"] == "ACTIVE" and "claims" not in db


def test_invalidation_after_level_1_is_refused_by_the_database():
    state, _ = case(BUY)
    db = _db(state)
    db["stage"] = 1  # the row is already past Level 1, even though this instance still thinks stage 0
    with pytest.MonkeyPatch.context() as mp:
        svc, sent, _ = _service(mp, state, db)
        asyncio.run(svc.check_tick(PAIR, SWEEP, None))
    assert sent == [] and db["status"] == "ACTIVE" and db["stage"] == 1
    assert svc.active_symbols() == set()  # the stale cache entry is dropped (the DB says another path advanced it)


def test_database_error_keeps_the_setup_armed_and_sends_nothing():
    state, _ = case(BUY)
    with pytest.MonkeyPatch.context() as mp:
        svc, sent, db = _service(mp, state)
        mp.setattr(sm, "SessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
        asyncio.run(svc.check_tick(PAIR, SWEEP, None))
        assert sent == [] and svc.active_symbols() == {PAIR}
        mp.setattr(sm, "SessionLocal", lambda: _FakeSession(db))
        asyncio.run(svc.check_tick(PAIR, SWEEP, None))  # retried on the next tick
    assert types(sent) == ["INVALIDATED"]


# ── Notification: wording, WAT time, who receives it ────────────────────────

@both
def test_invalidation_message(direction):
    state, f = case(direction)
    text = format_invalidation(state, f(SWEEP), utc(2026, 10, 28, 8, 14))
    side, label = ("high", "High") if direction == BUY else ("low", "Low")
    assert f"SETUP INVALIDATED — AUDJPY ({direction})" in text
    assert f"Setup invalidated: price swept Day 2's {side} before reaching the points of interest" in text
    assert f"Day 2 {label} (swept):</b> {state.invalidation_extreme:.3f}" in text
    assert f"<b>Price:</b> {f(SWEEP):.3f}" in text and "2026-10-28 09:14 WAT" in text
    assert "<" not in text.replace("<b>", "").replace("</b>", "")  # valid Telegram HTML


def test_invalidation_goes_only_to_continuation_subscribers():
    state, _ = case(BUY)
    sent: list[str] = []

    async def send(chat_id, message):
        sent.append(chat_id)

    users = _FakeUsers([_User("cont", continuations=True), _User("sweep", sweeps=True), _User("both", True, True)])
    with pytest.MonkeyPatch.context() as mp:
        svc = DailySweepService("k", None, users, send)
        db = _db(state)
        mp.setattr(sm, "SessionLocal", lambda: _FakeSession(db))
        svc._cache_add(state)
        asyncio.run(svc._invalidate(state, SWEEP, state.day3_expires_at - 5 * HOUR))
    assert sent == ["cont", "both"]
    assert db["status"] == "INVALIDATED"


# ── Scan time: don't arm a setup whose Day 2 extreme was already swept ──────

class _MD(_FakeMarketData):
    price = None

    async def get_current_price(self, symbol, api_key):
        return self.price, False


NOW = ny(2026, 10, 27, 17, 10)  # the 17:10 scan after Tuesday (Day 2) closes
CLOSE = ny(2026, 10, 27, 17)


def _bars(direction):
    mon, tue = (MON_C, TUE_C) if direction == BUY else (_mirror(MON_C), _mirror(TUE_C))
    return day_hours(MON, *mon) + day_hours(TUE, *tue)


def _bar(direction, beyond):
    """The in-progress 17:00 bar, whose extreme is `beyond` (a BUY-side high, mirrored for SELL)."""
    high = beyond if direction == BUY else K - beyond
    mid = 110.9 if direction == BUY else K - 110.9
    return {"datetime": CLOSE, "open": mid, "high": max(mid, high), "low": min(mid, high), "close": mid}


def _scan(extra_bars=(), direction=BUY, price=None, tick=None, spy_price=False):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sm, "SCAN_REQUEST_SPACING_SECONDS", 0)
        md = _MD(list(_bars(direction)) + list(extra_bars))
        md.price = price
        svc = DailySweepService("k", md, None, None)
        svc.clock = lambda: NOW
        svc.get_last_price = lambda symbol: tick
        stored = []

        async def fake_store(pair, signal, d1, d2, expires_at):
            stored.append(signal)
            return SetupState(id=9, pair=pair, direction=signal.direction, day1=(1, 2, 0, 1), day2=(1, 2, 0, 1),
                              level=signal.level, day2_date=d2.date, day3_expires_at=expires_at,
                              kind=signal.kind, level2=signal.level2)

        svc._store_setup = fake_store
        result = asyncio.run(svc._scan_pair(PAIR, TUE))
        return result, stored


@both
def test_scan_skips_a_setup_whose_day2_extreme_was_swept_since_the_close(direction, caplog):
    import logging

    with caplog.at_level(logging.INFO):
        result, stored = _scan([_bar(direction, SWEEP)], direction)
    assert result.status == "invalidated" and stored == [] and result.setup is None
    assert "NOT created" in caplog.text and "already swept since Day 2 closed" in caplog.text
    assert "1H bars since Day 2 close" in caplog.text and "buffer 2 pips" in caplog.text
    assert "invalidated" not in sm.ScanReport([result]).retry_pairs  # not retried: the sweep is a final answer


@both
def test_scan_arms_the_setup_when_the_bars_since_the_close_did_not_sweep(direction):
    for beyond in (INSIDE, EXACT, 110.95):  # inside the buffer, exactly the buffer, nowhere near
        result, stored = _scan([_bar(direction, beyond)], direction)
        assert result.status == "new" and len(stored) == 1


@both
def test_scan_without_bars_since_the_close_falls_back_to_a_fresh_tick(direction):
    swept = (111.2 if direction == BUY else K - 111.2, NOW - datetime.timedelta(seconds=30))
    result, stored = _scan([], direction, tick=swept)
    assert result.status == "invalidated" and stored == []
    calm = (110.9 if direction == BUY else K - 110.9, NOW - datetime.timedelta(seconds=30))
    result, stored = _scan([], direction, tick=calm)
    assert result.status == "new" and len(stored) == 1


def test_scan_without_bars_or_fresh_tick_uses_the_twelve_data_price_or_arms_as_normal(caplog):
    stale = (111.2, NOW - datetime.timedelta(minutes=10))  # too old to trust
    result, stored = _scan([], BUY, price=111.2, tick=stale)  # Twelve Data says swept
    assert result.status == "invalidated" and stored == []
    result, stored = _scan([], BUY, price=110.9, tick=stale)
    assert result.status == "new"
    result, stored = _scan([], BUY, price=None, tick=None)  # nothing to judge by: arm it as normal
    assert result.status == "new" and len(stored) == 1


def test_scan_ignores_weekend_window_quotes_when_judging_the_sweep():
    friday_close = ny(2026, 10, 23, 17)
    weekend = {"datetime": friday_close + 5 * HOUR, "open": 120, "high": 130, "low": 110, "close": 125}  # Sat: drifting
    friday_last = {"datetime": friday_close - HOUR, "open": 1, "high": 999, "low": 1, "close": 1}  # before the close
    sunday_session = {"datetime": ny(2026, 10, 25, 17), "open": 111, "high": 111.5, "low": 110.9, "close": 111.2}
    assert extremes_since([weekend, friday_last], friday_close) is None
    assert extremes_since([weekend, friday_last, sunday_session], friday_close) == (111.5, 110.9)


def test_scan_does_not_check_sweep_setups_for_invalidation():
    # A sweep Day 2 followed by a bar far beyond Day 2's high: still armed
    bars = day_hours(MON, 100, 110, 90, 105) + day_hours(TUE, 105, 112, 95, 100)
    far = {"datetime": CLOSE, "open": 100, "high": 500, "low": 99, "close": 101}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sm, "SCAN_REQUEST_SPACING_SECONDS", 0)
        svc = DailySweepService("k", _MD(bars + [far]), None, None)
        svc.clock = lambda: NOW
        kinds = []

        async def fake_store(pair, signal, d1, d2, expires_at):
            kinds.append(signal.kind)
            return None

        svc._store_setup = fake_store
        asyncio.run(svc._scan_pair("EURUSD", TUE))
    assert kinds == ["SWEEP"]


# ── Late arming ─────────────────────────────────────────────────────────────

@both
def test_late_arming_invalidates_instead_of_triggering_when_price_has_swept(direction):
    state, f = case(direction)
    db = _db(state)
    arm = ny(2026, 10, 27, 17, 10)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sm, "SCAN_REQUEST_SPACING_SECONDS", 0)
        svc, sent, _ = _service(mp, state, db)
        svc.clock = lambda: arm
        svc.get_last_price = lambda symbol: (f(SWEEP), arm)
        asyncio.run(svc._trigger_if_already_reached(state))
    assert types(sent) == ["INVALIDATED"] and db["status"] == "INVALIDATED"


def test_late_arming_reached_before_armed_is_unchanged_without_a_sweep():
    state, _ = case(BUY)
    db = _db(state)
    arm = ny(2026, 10, 27, 17, 10)
    with pytest.MonkeyPatch.context() as mp:
        svc, sent, _ = _service(mp, state, db)
        svc.clock = lambda: arm
        svc.get_last_price = lambda symbol: (110.700, arm)  # past Level 1 only
        asyncio.run(svc._trigger_if_already_reached(state))
    assert types(sent) == ["L1"] and "Level was reached before the setup was armed" in sent[0]
    assert db["stage"] == 1 and db["status"] == "ACTIVE"


# ── /sweeps display ─────────────────────────────────────────────────────────

def test_sweeps_shows_invalidated_setups_apart_from_active_ones():
    active, _ = case(BUY)
    started, _ = case(BUY)
    started.stage, started.id = 1, 2
    sell, _ = case(SELL)
    sell.id = 3
    svc = DailySweepService("k", None, None, None)

    async def active_list():
        return [active, started]

    async def invalidated_list():
        return [(sell, K - SWEEP, utc(2026, 10, 28, 8, 14))]

    svc.list_active, svc.list_invalidated = active_list, invalidated_list
    text = asyncio.run(svc.get_sweeps_message())
    active_part, invalidated_part = text.split("🚫 <b>Invalidated</b>")
    assert "⚠️ Voided if price trades beyond 111.105 (Day 2 high + 2 pips) before ①" in active_part
    assert active_part.count("Voided if price") == 1  # not shown once Level 1 has fired (stage 1)
    assert "AUDJPY" not in active_part.split("Voided")[0].split("Daily Setups — Active")[0]  # sanity: header only
    assert "📈 CONTINUATION 🔻 <b>AUDJPY</b> SELL — swept Day 2 low 108.915 @ 108.890 · 2026-10-28 09:14 WAT" in invalidated_part
    assert "Invalidated</b> (no longer alerting)" in text


def test_sweeps_with_only_invalidated_setups_says_nothing_is_active():
    sell, _ = case(SELL)
    svc = DailySweepService("k", None, None, None)

    async def none_active():
        return []

    async def one():
        return [(sell, None, None)]

    svc.list_active, svc.list_invalidated = none_active, one
    text = asyncio.run(svc.get_sweeps_message())
    assert "No active setups right now." in text and "AUDJPY</b> SELL — swept Day 2 low 108.915" in text


def test_sweeps_with_nothing_at_all():
    svc = DailySweepService("k", None, None, None)

    async def empty():
        return []

    svc.list_active, svc.list_invalidated = empty, empty
    assert "No active setups right now." in asyncio.run(svc.get_sweeps_message())


# ── The buffer: per pair, pip sizes ─────────────────────────────────────────

def test_buffer_boundaries_use_the_pairs_pip_size():
    assert swept_day2_extreme(BUY, 1.2000, 1.1900, 1.20021, "EURUSD") is True  # 2.1 pips
    assert swept_day2_extreme(BUY, 1.2000, 1.1900, 1.2002, "EURUSD") is False  # exactly 2
    assert swept_day2_extreme(SELL, 1.2000, 1.1900, 1.18979, "EURUSD") is True
    assert swept_day2_extreme(SELL, 1.2000, 1.1900, 1.1898, "EURUSD") is False
    assert swept_day2_extreme(BUY, 111.085, 110.0, 111.106, "AUDJPY") is True  # JPY pip 0.01
    assert swept_day2_extreme(BUY, 111.085, 110.0, 111.105, "AUDJPY") is False
    assert swept_day2_extreme(BUY, 4071.40, 4028.5, 4071.61, "XAUUSD") is True  # gold pip 0.1: 2.1 pips
    assert swept_day2_extreme(BUY, 4071.40, 4028.5, 4071.60, "XAUUSD") is False


def test_buffer_is_a_per_pair_setting(monkeypatch):
    assert sm.sweep_invalidation_buffer_pips_for("EURUSD") == 2.0
    monkeypatch.setitem(sm.PAIR_SWEEP_INVALIDATION_BUFFER_PIPS, "EURUSD", 5.0)
    assert swept_day2_extreme(BUY, 1.2000, 1.1900, 1.2004, "EURUSD") is False  # 4 pips < this pair's 5
    assert swept_day2_extreme(BUY, 1.2000, 1.1900, 1.20051, "EURUSD") is True
    assert swept_day2_extreme(BUY, 1.2000, 1.1900, 1.2004, "GBPUSD") is True  # other pairs keep 2


def test_invalidation_price_is_the_extreme_plus_the_buffer():
    buy, _ = case(BUY)
    sell, _ = case(SELL)
    assert buy.invalidation_extreme == 111.085 and buy.invalidation_price == pytest.approx(111.105)
    assert sell.invalidation_extreme == pytest.approx(K - 111.085) and sell.invalidation_price == pytest.approx(K - 111.105)


def test_invalidation_is_evaluated_before_stage_advancement():
    # Real setups can't have one tick that both sweeps Day 2's extreme and reaches Level 1 (Level 1 is on the other
    # side of Day 2's open). Force it with a pathological Level 1 ABOVE Day 2's high, to pin the order.
    state, _ = case(BUY)
    state.level = 120.0
    assert state.next_stage(SWEEP) == 1 and state.invalidates(SWEEP)  # the one price does both
    _, sent, db, _ = _play_mp(state, [SWEEP])
    assert types(sent) == ["INVALIDATED"] and db["status"] == "INVALIDATED" and db["claims"] == [(0, "INVALIDATED")]
