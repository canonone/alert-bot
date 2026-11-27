"""SMT divergence between GBPUSD and EURUSD: pure logic (levels, touches, deadlines, replay, wording)."""
import datetime

import pytest

import app.services.smt_divergence_service as smt
from app.services.smt_divergence_service import (
    ACTIVE,
    CONFIRMED,
    DIVERGENCE,
    HIGH,
    LEADER_TOUCHED,
    LOW,
    PAIR_EUR,
    PAIR_GBP,
)

from test_daily_candles import HOUR, D, candle, utc

PIP = 0.0001
LEVELS = {LOW: {PAIR_GBP: 1.2650, PAIR_EUR: 1.0720}, HIGH: {PAIR_GBP: 1.2780, PAIR_EUR: 1.0850}}
LEVEL_DAY = D(2026, 10, 27)  # Tuesday: its levels are watched until Wednesday 28 Oct 17:00 NY (EDT) = 21:00 UTC
CLOSE = utc(2026, 10, 27, 21)  # the end of the level day = the start of the monitored period
UNTIL = utc(2026, 10, 28, 21)  # the end of the monitored day

both = pytest.mark.parametrize("side", [LOW, HIGH])


def bar(side, pair, start, beyond):
    """A 1H bar whose extreme toward the level is `beyond` pips PAST it (negative = short of it)."""
    level = LEVELS[side][pair]
    if side == LOW:
        low = level - beyond * PIP
        return {"datetime": start, "low": low, "high": low + 5 * PIP}
    high = level + beyond * PIP
    return {"datetime": start, "high": high, "low": high - 5 * PIP}


def hour(n):
    return CLOSE + n * HOUR  # n hours after the day close


def replay(side, gbp, eur, now, close=UNTIL, basis="hour"):
    """gbp / eur: [(hour_offset, pips_beyond_the_level)]"""
    bars = {PAIR_GBP: [bar(side, PAIR_GBP, hour(h), b) for h, b in gbp],
            PAIR_EUR: [bar(side, PAIR_EUR, hour(h), b) for h, b in eur]}
    return smt.replay_side(side, LEVELS[side], bars, now, close, basis)


# ── Levels, touches, deadlines ──────────────────────────────────────────────

def test_levels_come_from_the_completed_daily_candle():
    day = candle(LEVEL_DAY, 1.2700, 1.2790, 1.2640, 1.2680)
    assert smt.extract_levels(day) == (1.2790, 1.2640)  # (PDH, PDL)


@both
def test_touch_is_any_touch_or_break_with_no_buffer(side):
    level = LEVELS[side][PAIR_GBP]
    beyond = -1 if side == LOW else 1  # price moves down to a PDL, up to a PDH
    assert smt.touched(side, level, level, PAIR_GBP)  # exactly at the level
    assert smt.touched(side, level, level + beyond * 0.0001, PAIR_GBP)  # through it
    assert smt.touched(side, level, level + beyond * 0.0100, PAIR_GBP)
    assert not smt.touched(side, level, level - beyond * 0.00001, PAIR_GBP)  # a tenth of a pip short: not yet
    assert not smt.touched(side, level, level - beyond * 0.0005, PAIR_GBP)


@both
def test_touch_buffer_is_a_per_pair_setting(side, monkeypatch):
    level = LEVELS[side][PAIR_GBP]
    short = -1 if side == LOW else 1
    price = level - short * 1.5 * PIP  # 1.5 pips short of the level
    assert not smt.touched(side, level, price, PAIR_GBP)
    monkeypatch.setitem(smt.PAIR_TOUCH_BUFFER_PIPS, PAIR_GBP, 2.0)
    assert smt.touched(side, level, price, PAIR_GBP)  # within the widened zone
    assert not smt.touched(side, LEVELS[side][PAIR_EUR], LEVELS[side][PAIR_EUR] - short * 1.5 * PIP, PAIR_EUR)  # other pair: default 0
    assert smt.touch_buffer_pips_for(PAIR_EUR) == 0.0


@both
def test_distance_to_the_level(side):
    level = LEVELS[side][PAIR_EUR]
    away = 1 if side == LOW else -1
    assert smt.pips_from_level(side, level, level + away * 8 * PIP, PAIR_EUR) == pytest.approx(8.0)
    assert smt.pips_from_level(side, level, level, PAIR_EUR) == pytest.approx(0.0)
    assert smt.pips_from_level(side, level, level - away * 3 * PIP, PAIR_EUR) == pytest.approx(-3.0)  # touched


def test_deadline_is_the_earlier_of_t_plus_30_and_the_day_close():
    t = utc(2026, 10, 28, 13, 0)
    assert smt.window_deadline(t, UNTIL) == utc(2026, 10, 28, 13, 30)
    assert smt.window_deadline(utc(2026, 10, 28, 20, 45), UNTIL) == UNTIL  # cut short by the close at 21:00
    assert smt.window_deadline(utc(2026, 10, 28, 20, 30), UNTIL) == UNTIL  # exactly 30 min before: same
    assert smt.window_deadline(t, UNTIL, wait_minutes=15) == utc(2026, 10, 28, 13, 15)


def test_monitored_period_runs_to_the_next_trading_day_close_incl_weekends_and_dst():
    assert smt.monitored_until(D(2026, 10, 27)) == utc(2026, 10, 28, 21)  # Tue -> Wed 17:00 EDT
    assert smt.monitored_until(D(2026, 10, 29)) == utc(2026, 10, 30, 21)  # Thu -> Fri 17:00 EDT
    # Friday's levels run through MONDAY's close, and the US clocks change in between (1 Nov 2026: EDT -> EST)
    assert smt.monitored_until(D(2026, 10, 30)) == utc(2026, 11, 2, 22)  # Mon 17:00 EST = 22:00 UTC
    assert smt.monitored_until(D(2026, 11, 6)) == utc(2026, 11, 9, 22)  # Fri -> Mon, both EST
    assert smt.monitored_until(D(2026, 11, 2)) == utc(2026, 11, 3, 22)  # Mon -> Tue, after the change


def test_the_window_is_cut_by_the_close_on_both_sides_of_dst():
    assert smt.window_deadline(utc(2026, 10, 30, 20, 45), smt.monitored_until(D(2026, 10, 29))) == utc(2026, 10, 30, 21)
    assert smt.window_deadline(utc(2026, 11, 3, 21, 45), smt.monitored_until(D(2026, 11, 2))) == utc(2026, 11, 3, 22)


# ── Live outcomes ───────────────────────────────────────────────────────────

@both
def test_first_touch_either_pair_can_lead(side):
    levels = LEVELS[side]
    beyond = lambda pair, pips: levels[pair] - pips * PIP if side == LOW else levels[pair] + pips * PIP  # noqa: E731
    for leader in (PAIR_GBP, PAIR_EUR):
        assert smt.first_touch_outcome(side, levels, leader, beyond(leader, 1), None) == LEADER_TOUCHED
        assert smt.first_touch_outcome(side, levels, leader, beyond(leader, -1), None) is None  # 1 pip short


@both
def test_first_touch_with_the_other_pair_already_past_its_level_is_confirmed(side):
    levels = LEVELS[side]
    past = lambda pair: levels[pair] - 2 * PIP if side == LOW else levels[pair] + 2 * PIP  # noqa: E731
    short = lambda pair: levels[pair] + 2 * PIP if side == LOW else levels[pair] - 2 * PIP  # noqa: E731
    assert smt.first_touch_outcome(side, levels, PAIR_GBP, past(PAIR_GBP), past(PAIR_EUR)) == CONFIRMED
    assert smt.first_touch_outcome(side, levels, PAIR_EUR, past(PAIR_EUR), past(PAIR_GBP)) == CONFIRMED
    assert smt.first_touch_outcome(side, levels, PAIR_GBP, past(PAIR_GBP), short(PAIR_EUR)) == LEADER_TOUCHED


def test_laggard_touch_is_in_time_strictly_before_the_deadline():
    deadline = utc(2026, 10, 28, 14, 0)
    assert smt.laggard_touch_outcome(deadline, deadline - datetime.timedelta(seconds=1)) == CONFIRMED
    assert smt.laggard_touch_outcome(deadline, deadline) == DIVERGENCE
    assert smt.laggard_touch_outcome(deadline, deadline + datetime.timedelta(minutes=7)) == DIVERGENCE


@both
def test_closest_approach_tracking(side):
    level = LEVELS[side][PAIR_EUR]
    toward = -1 if side == LOW else 1
    best = None
    for pips_short in (9, 6, 7, 3.5, 5):  # price wanders; the best is the closest it got
        price = level - toward * pips_short * PIP
        if smt.better_approach(side, price, best):
            best = price
    assert smt.pips_from_level(side, level, best, PAIR_EUR) == pytest.approx(3.5)
    assert smt.better_approach(side, level, None)


# ── Replay: arming late ─────────────────────────────────────────────────────

@both
def test_replay_neither_pair_touched_stays_active(side):
    r = replay(side, [(0, -8), (1, -5)], [(0, -9), (1, -4)], now=hour(2))
    assert r.status == ACTIVE and r.leader_pair is None and r.deadline_at is None


@both
def test_replay_leader_only_inside_its_window_resumes_waiting(side):
    now = hour(0) + datetime.timedelta(minutes=20)  # the leader's hour is still running
    r = replay(side, [(0, 1.5)], [(0, -6)], now=now)
    assert (r.status, r.leader_pair, r.leader_touch_basis) == (LEADER_TOUCHED, PAIR_GBP, "hour")
    assert r.leader_touched_at == hour(0)  # only the hour is known
    assert r.deadline_at == now + datetime.timedelta(minutes=30)  # judged from NOW: never earlier than it should be
    gap = smt.pips_from_level(side, LEVELS[side][PAIR_EUR], r.laggard_best_price, PAIR_EUR)
    assert gap == pytest.approx(6.0)  # the laggard's closest approach so far: 6 pips short of its level
    assert r.detected_late is False


@both
def test_replay_leader_only_deadline_boundary(side):
    # leader touched in the 21:00 hour; judged from the END of that hour (22:00): deadline 22:30 UTC
    deadline = hour(1) + datetime.timedelta(minutes=30)
    gbp, eur = [(0, 2)], [(0, -5), (1, -4)]
    assert replay(side, gbp, eur, now=deadline - datetime.timedelta(seconds=1)).status == LEADER_TOUCHED
    r = replay(side, gbp, eur, now=deadline)
    assert r.status == DIVERGENCE and r.detected_late is True and r.resolved_at == deadline
    assert r.deadline_at == deadline and r.laggard_touched_at is None


@both
def test_replay_either_pair_can_lead(side):
    now = hour(1) + datetime.timedelta(minutes=10)
    assert replay(side, [(0, 2)], [(0, -3)], now=now).leader_pair == PAIR_GBP
    assert replay(side, [(0, -3)], [(0, 2)], now=now).leader_pair == PAIR_EUR


@both
def test_replay_same_or_adjacent_hour_is_confirmed_and_silent(side):
    same = replay(side, [(1, 2)], [(1, 1)], now=hour(5))
    assert same.status == CONFIRMED and same.leader_pair is None  # can't tell who was first
    adjacent = replay(side, [(1, 2)], [(2, 1)], now=hour(5))
    assert adjacent.status == CONFIRMED and adjacent.leader_pair == PAIR_GBP
    assert replay(side, [(2, 2)], [(1, 1)], now=hour(5)).leader_pair == PAIR_EUR


@both
def test_replay_laggard_two_or_more_hours_later_is_a_divergence(side):
    r = replay(side, [(0, 2)], [(2, 1)], now=hour(6))
    assert r.status == DIVERGENCE and r.detected_late is True
    assert (r.leader_pair, r.laggard_touched_at) == (PAIR_GBP, hour(2))
    assert r.deadline_at == hour(1) + datetime.timedelta(minutes=30)  # leader's hour ended, plus 30 min
    r = replay(side, [(3, 2)], [(0, 1)], now=hour(6))  # the other way round
    assert r.status == DIVERGENCE and r.leader_pair == PAIR_EUR and r.laggard_touched_at == hour(3)


@both
def test_replay_window_is_cut_short_by_the_day_close(side):
    close = hour(24)  # the monitored period ends 24 h after the level day closed
    now = close - datetime.timedelta(minutes=15)  # the leader's hour (23:00-24:00) is still running
    r = replay(side, [(23, 2)], [(23, -6)], now=now, close=close)
    assert r.status == LEADER_TOUCHED and r.deadline_at == close  # not now + 30 min: the close comes first
    r = replay(side, [(23, 2)], [(23, -6)], now=close, close=close)
    assert r.status == DIVERGENCE and r.resolved_at == close


@both
def test_replay_ignores_bars_at_or_after_the_close(side):
    r = replay(side, [(24, 5)], [(25, 5)], now=hour(26), close=hour(24))
    assert r.status == ACTIVE


@both
def test_replay_without_bars_uses_the_price_readings_taken_now(side):
    now = hour(3)
    leader_only = replay(side, [(3, 2)], [(3, -4)], now=now, basis="detected")
    assert leader_only.status == LEADER_TOUCHED and leader_only.leader_touch_basis == "detected"
    assert leader_only.leader_touched_at == now and leader_only.deadline_at == now + datetime.timedelta(minutes=30)
    both_past = replay(side, [(3, 2)], [(3, 1)], now=now, basis="detected")
    assert both_past.status == CONFIRMED  # both already past their level: in sync


@both
def test_replay_tracks_the_laggards_closest_approach_from_the_leaders_hour(side):
    # EUR: 9, 5 then 2.5 pips short at its best in hour 2 (it touches nothing); GBP leads in hour 1
    r = replay(side, [(1, 1)], [(0, -1), (1, -5), (2, -2.5), (3, -4)], now=hour(2) + datetime.timedelta(minutes=15))
    assert r.status == LEADER_TOUCHED
    gap = smt.pips_from_level(side, LEVELS[side][PAIR_EUR], r.laggard_best_price, PAIR_EUR)
    assert gap == pytest.approx(2.5)  # hour 0 (before the leader's hour) is not counted


def test_replay_buffer_widens_what_counts_as_a_touch(monkeypatch):
    short = replay(LOW, [(0, -1.5)], [], now=hour(1))
    assert short.status == ACTIVE
    monkeypatch.setitem(smt.PAIR_TOUCH_BUFFER_PIPS, PAIR_GBP, 2.0)
    assert replay(LOW, [(0, -1.5)], [], now=hour(0) + datetime.timedelta(minutes=10)).status == LEADER_TOUCHED


# ── The alert text ──────────────────────────────────────────────────────────

LEAD_AT = utc(2026, 10, 28, 13, 32)  # 14:32 WAT
DEADLINE = utc(2026, 10, 28, 14, 2)  # 15:02 WAT


def alert(side=LOW, leader=PAIR_GBP, now=None, **kwargs):
    defaults = dict(
        leader_touched_at=LEAD_AT, leader_price=LEVELS[side][leader] - 0.0003 if side == LOW else LEVELS[side][leader] + 0.0003,
        basis="tick", deadline_at=DEADLINE, now=now or DEADLINE + datetime.timedelta(seconds=20),
        laggard_price=None, laggard_best=None,
    )
    defaults.update(kwargs)
    return smt.format_divergence(side, leader, LEVELS[side], **defaults)


def test_alert_for_a_low_divergence_with_closest_approach():
    text = alert(laggard_price=1.0728, laggard_best=1.0723)
    assert "SMT DIVERGENCE — PDL" in text
    assert "🥇 <b>GBPUSD</b> took its PDL 1.26500 — touched 2026-10-28 14:32 WAT at 1.26470" in text
    assert "🥈 <b>EURUSD</b> PDL 1.07200 — price 1.07280 (8.0 pips above it), closest approach 3.0 pips from the level" in text
    assert "⏱ Window: 30 min, closed 2026-10-28 15:02 WAT" in text
    assert "ℹ️ GBPUSD took its PDL; EURUSD held above its PDL after 30 min." in text
    assert "feed difference" not in text and "Detected" not in text  # 3.0 pips is not a near miss; and not late
    assert "<" not in text.replace("<b>", "").replace("</b>", "") and ">" not in text.replace("<b>", "").replace("</b>", "")


def test_alert_for_a_high_divergence_with_eurusd_leading():
    text = alert(HIGH, PAIR_EUR, laggard_price=1.2771, laggard_best=1.2774)
    assert "SMT DIVERGENCE — PDH" in text and "🥇 <b>EURUSD</b> took its PDH 1.08500" in text
    assert "🥈 <b>GBPUSD</b> PDH 1.27800 — price 1.27710 (9.0 pips below it), closest approach 6.0 pips from the level" in text
    assert "EURUSD took its PDH; GBPUSD held below its PDH after 30 min." in text


def test_a_near_miss_is_annotated_but_never_suppressed():
    text = alert(laggard_price=1.0726, laggard_best=1.0721)  # closest approach 1.0 pip
    assert "closest approach 1.0 pips from the level" in text
    assert "⚠️ The closest approach was within 2 pips of the level — this may be a feed difference between Twelve Data levels and Finnhub ticks." in text
    assert "SMT DIVERGENCE" in text  # the alert is still sent
    edge = alert(laggard_price=1.0726, laggard_best=1.0722)  # exactly 2.0 pips: still annotated
    assert "feed difference" in edge
    assert "feed difference" not in alert(laggard_price=1.0726, laggard_best=1.07221)  # 2.1 pips


def test_near_miss_distance_is_a_per_pair_setting(monkeypatch):
    assert "feed difference" not in alert(laggard_price=1.0730, laggard_best=1.0725)  # 5 pips
    monkeypatch.setitem(smt.PAIR_NEAR_MISS_PIPS, PAIR_EUR, 6.0)  # the laggard here is EURUSD
    assert "within 6 pips of the level" in alert(laggard_price=1.0730, laggard_best=1.0725)


def test_alert_when_the_laggard_touched_after_the_window_closed():
    text = alert(laggard_price=1.0719, laggard_best=1.0719, laggard_touched_at=DEADLINE + datetime.timedelta(minutes=7))
    assert "🥈 <b>EURUSD</b> PDL 1.07200 — touched 7 min after the window closed" in text
    assert "did not touch" not in text and "held above" not in text and "closest approach" not in text
    assert "EURUSD touched its PDL only after the window closed" in text
    soon = alert(laggard_touched_at=DEADLINE + datetime.timedelta(seconds=20))
    assert "touched under 1 min after the window closed" in soon


def test_alert_with_no_laggard_price_says_so():
    assert "price n/a" in alert()


def test_alert_for_a_window_cut_short_by_the_day_close():
    text = alert(deadline_at=LEAD_AT + datetime.timedelta(minutes=25), laggard_price=1.0730, laggard_best=1.0727)
    assert "⏱ Window: 25 min" in text and "after 25 min" in text


def test_late_detection_wording_never_claims_an_observed_time():
    hour_based = alert(basis="hour", leader_touched_at=utc(2026, 10, 28, 1), detected_late=True,
                       laggard_price=1.0730, laggard_best=1.0727)
    assert "touched in the 2026-10-28 02:00–03:00 WAT hour (extreme" in hour_based
    assert "⚡ Detected late" in hour_based and "only known to the hour" in hour_based and "touched 2026" not in hour_based
    late_touch = alert(basis="hour", leader_touched_at=utc(2026, 10, 28, 1), detected_late=True,
                       laggard_touched_at=utc(2026, 10, 28, 4), deadline_at=utc(2026, 10, 28, 3))
    assert "touched in the 2026-10-28 05:00–06:00 WAT hour, after the window had closed" in late_touch
    detected = alert(basis="detected", detected_late=True, laggard_price=1.0730)
    assert "already beyond it when detected" in detected and "the touch time wasn't observed" in detected
    overdue = alert(now=DEADLINE + datetime.timedelta(minutes=12), detected_late=True, laggard_price=1.0730)
    assert "⚡ Detected 12 min after the window closed" in overdue
