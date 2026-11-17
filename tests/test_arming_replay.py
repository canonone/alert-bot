"""A continuation armed LATE (a manual /sweepscan, a catch-up scan) replays the 1H bars since Day 2's close in order
with the live rule, so it ends up where a live bot would have left it: voided, already at Level 1/2 (and announced
as "reached earlier"), or untouched."""
import asyncio
import datetime

import pytest

import app.services.daily_sweep_service as sm
from app.services.daily_sweep_service import (
    BUY,
    SELL,
    DailySweepService,
    SetupState,
    ReplayEvent,
    bars_since,
    levels_merged,
    replay_since_day2_close,
)

from test_daily_sweep import HOUR, D, _FakeMarketData, _FakeSession, _FakeUsers, _User, day_hours, ny, utc

UTC = datetime.timezone.utc

# ── The real USDJPY SELL setup of 17 Nov 2026 ───────────────────────────────
# Level 1 = Day 1 low 159.537, Level 2 = Day 2 open 159.702; Day 2 low 159.344, so the sweep threshold is 159.324.
L1, L2, D2_HIGH, D2_LOW = 159.537, 159.702, 159.80, 159.344
PAIR = "USDJPY"
DAY3_START = utc(2026, 11, 16, 22)  # Monday 17:00 New York (EST)
K = 320.0


def bar(hours_after_close, high, low):
    return {"datetime": DAY3_START + HOUR * hours_after_close, "high": high, "low": low}


def replay(bars, direction=SELL, level=L1, level2=L2, high=D2_HIGH, low=D2_LOW, pair=PAIR, mirror=False):
    """SELL-side numbers; with mirror=True the same story is told as a BUY (prices reflected around K)."""
    if mirror:
        direction, level, level2 = BUY, K - level, K - level2
        high, low = K - low, K - high
        bars = [{**b, "high": K - b["low"], "low": K - b["high"]} for b in bars]
    return replay_since_day2_close(direction, level, level2, high, low, pair, bars)


both = pytest.mark.parametrize("mirror", [False, True])

CALM = [bar(0, 159.46, 159.40), bar(1, 159.47, 159.41), bar(2, 159.50, 159.44)]


@both
def test_calm_bars_leave_the_setup_untouched(mirror):
    r = replay(CALM, mirror=mirror)
    assert (r.voided, r.stage, r.events) == (False, 0, ())


@both
def test_usdjpy_level_1_reached_then_day2_low_swept_stays_alive_at_stage_1(mirror):
    # 02:00 WAT bar tags 159.613 (above Level 1 159.537); only LATER does the day's low (158.869) sweep Day 2's low
    bars = CALM + [bar(3, 159.613, 159.48), bar(4, 159.58, 159.50), bar(11, 159.10, 158.869), bar(15, 159.475, 159.36)]
    r = replay(bars, mirror=mirror)
    assert r.voided is False and r.stage == 1 and r.ambiguous is False
    assert len(r.events) == 1 and r.events[0].stage == 1 and r.events[0].bar_start == DAY3_START + 3 * HOUR
    assert r.events[0].price == pytest.approx(159.613 if not mirror else K - 159.613 + 0)  # the bar's extreme toward L1


@both
def test_sweep_before_level_1_voids(mirror):
    bars = CALM + [bar(3, 159.20, 158.90), bar(8, 159.613, 159.40)]  # swept first; Level 1 only afterwards
    r = replay(bars, mirror=mirror)
    assert r.voided is True and r.stage == 0 and r.events == () and r.ambiguous is False
    assert r.swept_at == DAY3_START + 3 * HOUR


@both
def test_sweep_exactly_the_buffer_does_not_void(mirror):
    r = replay(CALM + [bar(3, 159.30, 159.324)], mirror=mirror)  # low exactly 2.0 pips under Day 2's low: not a sweep
    assert r.voided is False
    r = replay(CALM + [bar(3, 159.30, 159.323)], mirror=mirror)  # 2.1 pips
    assert r.voided is True


@both
def test_one_bar_with_both_a_sweep_and_level_1_is_ambiguous_and_voids(mirror):
    r = replay(CALM + [bar(3, 159.60, 159.20)], mirror=mirror)
    assert r.voided is True and r.ambiguous is True and r.events == ()


@both
def test_level_1_then_level_2_in_later_bars(mirror):
    bars = CALM + [bar(3, 159.613, 159.48), bar(6, 159.60, 159.50), bar(8, 159.72, 159.55)]
    r = replay(bars, mirror=mirror)
    assert r.voided is False and r.stage == 2
    assert [(e.stage, e.bar_start) for e in r.events] == [(1, DAY3_START + 3 * HOUR), (2, DAY3_START + 8 * HOUR)]


@both
def test_level_1_and_level_2_in_the_same_bar(mirror):
    r = replay(CALM + [bar(3, 159.75, 159.48)], mirror=mirror)
    assert r.stage == 2 and [e.stage for e in r.events] == [1, 2]
    assert r.events[0].bar_start == r.events[1].bar_start


@both
def test_a_sweep_after_level_1_does_not_stop_level_2(mirror):
    bars = CALM + [bar(3, 159.613, 159.48), bar(5, 159.30, 158.90), bar(9, 159.75, 159.30)]
    r = replay(bars, mirror=mirror)
    assert r.voided is False and r.stage == 2


@both
def test_merged_levels_complete_at_level_1_with_a_single_event(mirror):
    assert levels_merged("EURCAD", 1.56564, 1.56588)  # 2.4 pips apart
    r = replay_since_day2_close(
        SELL, 1.56564, 1.56588, 1.56668, 1.56072, "EURCAD", [bar(0, 1.56300, 1.56200), bar(1, 1.56570, 1.56300)]
    ) if not mirror else replay_since_day2_close(
        BUY, 3.0 - 1.56564, 3.0 - 1.56588, 3.0 - 1.56072, 3.0 - 1.56668, "EURCAD",
        [bar(0, 3.0 - 1.56200, 3.0 - 1.56300), bar(1, 3.0 - 1.56300, 3.0 - 1.56570)])
    assert r.stage == 2 and len(r.events) == 1 and r.events[0].stage == 2


def test_bars_since_filters_sorts_and_skips_the_weekend():
    friday_close = ny(2026, 10, 23, 17)
    before = {"datetime": friday_close - HOUR, "high": 999, "low": 1}
    weekend = {"datetime": friday_close + 5 * HOUR, "high": 130, "low": 110}
    sunday = {"datetime": ny(2026, 10, 25, 17), "high": 111.5, "low": 110.9}
    saturday_later = {"datetime": friday_close + 30 * HOUR, "high": 140, "low": 100}
    later = {"datetime": ny(2026, 10, 25, 20), "high": 111.0, "low": 110.8}
    out = bars_since([later, before, weekend, sunday, saturday_later], friday_close)
    assert out == [sunday, later]  # oldest first; the pre-close bar and the weekend-window bars are gone


# ── End to end through run_scan: today's USDJPY, armed by a manual scan in the afternoon ────

NOW = utc(2026, 11, 17, 13, 45)  # Tue 14:45 WAT: the manual /sweepscan
FRI, MON = D(2026, 11, 13), D(2026, 11, 16)
DAY1 = (160.00, 160.10, 159.537, 159.702)  # bearish; low = Level 1
DAY2 = (159.702, 159.80, 159.344, 159.40)  # bearish, closed 13.7 pips under Day 1's low; open = Level 2


def day3_bars(profile):
    """profile: [(high, low)] one per hour from Monday's close; returns 1H bars with those extremes."""
    return [{"datetime": DAY3_START + HOUR * i, "open": (h + lo) / 2, "high": h, "low": lo, "close": (h + lo) / 2}
            for i, (h, lo) in enumerate(profile)]


# 22:00 UTC Mon ... 13:00 UTC Tue (the last one is still forming at 13:45)
REAL_DAY = [
    (159.46, 159.40), (159.47, 159.41), (159.50, 159.44),
    (159.613, 159.48),  # 01:00 UTC = 02:00 WAT: tags Level 1 (159.537)
    (159.58, 159.50), (159.55, 159.42), (159.45, 159.30), (159.40, 159.25), (159.30, 159.10),
    (159.20, 158.95), (159.10, 158.869),  # ~09:00-10:00 WAT: the day's low, far under Day 2's low
    (159.30, 159.05), (159.40, 159.25), (159.475, 159.36), (159.42, 159.36),
]


class _MD(_FakeMarketData):
    async def get_current_price(self, symbol, api_key):
        return None, False


def _run_scan(monkeypatch, profile, subscribed=True, db=None):
    monkeypatch.setattr(sm, "SCAN_REQUEST_SPACING_SECONDS", 0)
    bars = day_hours(FRI, *DAY1) + day_hours(MON, *DAY2) + day3_bars(profile)
    sent: list[tuple[str, str]] = []

    async def send(chat_id, message):
        sent.append((chat_id, message))

    users = _FakeUsers([_User("cont", continuations=True)]) if subscribed else _FakeUsers([])
    svc = DailySweepService("k", _MD(bars), users, send)
    svc.clock = lambda: NOW
    svc.pairs = [PAIR]
    stored, subscriptions = [], []

    async def fake_store(pair, signal, d1, d2, expires_at, stage=0, reached=None):
        stored.append((stage, reached))
        return SetupState(id=5, pair=pair, direction=signal.direction, day1=DAY1, day2=DAY2, level=signal.level,
                          day2_date=d2.date, day3_expires_at=expires_at, kind=signal.kind, level2=signal.level2, stage=stage)

    async def noop(*a, **k):
        return 0

    async def subscribe(symbol):
        subscriptions.append(symbol)

    svc._store_setup, svc.expire_setups, svc._record_scan, svc.subscribe_symbol = fake_store, noop, noop, subscribe
    report = asyncio.run(svc.run_scan())
    return svc, report, sent, stored, subscriptions


def test_usdjpy_armed_in_the_afternoon_announces_the_earlier_level_1_hit(monkeypatch):
    svc, report, sent, stored, subs = _run_scan(monkeypatch, REAL_DAY)
    assert [r.status for r in report.results] == ["new"]
    # stored at stage 1 (Level 1 reached at 01:00 UTC; the later 158.869 low didn't void it)
    (stage, reached), = stored
    assert stage == 1 and reached.stage == 1 and reached.bar_start == DAY3_START + 3 * HOUR and reached.price == 159.613
    # digest first, then ONE "reached earlier" alert
    assert len(sent) == 2 and "Daily Setups" in sent[0][1]
    alert = sent[1][1]
    assert "CONTINUATION · LEVEL 1 HIT — USDJPY (SELL)" in alert
    assert "<b>Price reached:</b> 159.613" in alert and "Current Price" not in alert
    assert "Level 1 was reached earlier — in the 2026-11-17 02:00–03:00 WAT hour (high 159.613) — before the setup was armed." in alert
    assert "② <b>Next level — Day 2 Open:</b> 159.702" in alert
    # still live for Level 2: cached at stage 1 and subscribed to the feed
    assert svc.active_symbols() == {PAIR} and subs == [PAIR]
    assert svc._active[PAIR][0].stage == 1


def test_that_setup_then_fires_level_2_live_and_only_level_2(monkeypatch):
    svc, _, sent, _, _ = _run_scan(monkeypatch, REAL_DAY)
    sent.clear()
    db = {"status": "ACTIVE", "stage": 1, "expires_at": utc(2026, 11, 17, 22)}
    monkeypatch.setattr(sm, "SessionLocal", lambda: _FakeSession(db))
    svc.clock = lambda: NOW + datetime.timedelta(hours=2)

    async def ticks():
        await svc.check_tick(PAIR, 159.60, None)  # still short of Level 2 (159.702)
        await svc.check_tick(PAIR, 159.30, None)  # a later sweep of Day 2's low: ignored at stage 1
        await svc.check_tick(PAIR, 159.71, None)  # Level 2

    asyncio.run(ticks())
    assert len(sent) == 1 and "LEVEL 2 HIT" in sent[0][1] and db["status"] == "TRIGGERED"


def test_the_same_day_with_the_order_reversed_is_voided(monkeypatch):
    # the sweep of Day 2's low comes FIRST (at 01:00 UTC), the spike to Level 1 only at 09:00 UTC
    profile = list(REAL_DAY)
    profile[3], profile[10] = (159.30, 158.869), (159.613, 159.48)
    svc, report, sent, stored, subs = _run_scan(monkeypatch, profile)
    assert [r.status for r in report.results] == ["invalidated"]
    assert stored == [] and sent == [] and svc.active_symbols() == set() and subs == []


def test_no_levels_reached_arms_normally_with_no_extra_alert(monkeypatch):
    svc, report, sent, stored, subs = _run_scan(monkeypatch, [(159.46, 159.40), (159.47, 159.41), (159.50, 159.44)])
    assert stored == [(0, None)] and len(sent) == 1 and "Daily Setups" in sent[0][1]
    assert svc.active_symbols() == {PAIR} and subs == [PAIR]


def test_both_levels_already_reached_is_stored_complete_and_announced_once(monkeypatch):
    profile = REAL_DAY + [(159.75, 159.50)]  # the 13:00 UTC (14:00 WAT) bar tags Level 2 (159.702) too
    svc, report, sent, stored, subs = _run_scan(monkeypatch, profile)
    (stage, reached), = stored
    assert stage == 2 and reached.stage == 2 and reached.price == 159.75
    assert len(sent) == 2 and "LEVELS 1 & 2 HIT" in sent[1][1]
    assert "Level 1 was reached earlier in the 2026-11-17 02:00–03:00 WAT hour (high 159.613)" in sent[1][1]
    assert "and Level 2 in the 2026-11-17 14:00–15:00 WAT hour (high 159.750)" in sent[1][1]
    assert svc.active_symbols() == set() and subs == []  # complete: nothing left to watch


def test_sweep_setups_are_not_replayed(monkeypatch):
    # A sweep Day 2 followed by bars that reach its level: the old behaviour (price check only) is unchanged
    monkeypatch.setattr(sm, "SCAN_REQUEST_SPACING_SECONDS", 0)
    mon, tue = D(2026, 10, 26), D(2026, 10, 27)
    bars = day_hours(mon, 100, 110, 90, 105) + day_hours(tue, 105, 112, 95, 100)
    close = ny(2026, 10, 27, 17)
    bars.append({"datetime": close, "open": 100, "high": 106, "low": 99, "close": 101})  # tagged the 105 level already
    svc = DailySweepService("k", _MD(bars), None, None)
    svc.clock = lambda: ny(2026, 10, 27, 17, 10)
    calls = []

    async def fake_store(pair, signal, d1, d2, expires_at, **kwargs):
        calls.append((signal.kind, kwargs))
        return None

    svc._store_setup = fake_store
    asyncio.run(svc._scan_pair("EURUSD", tue))
    assert calls == [("SWEEP", {})]  # no stage / reached passed: a sweep is armed at stage 0 as before
