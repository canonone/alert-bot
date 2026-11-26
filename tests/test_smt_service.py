"""SmtDivergenceService with an in-memory table that keeps the SAME atomic contract as the SQL claims (the real SQL
is exercised in test_integration_postgres.py). Injectable clock, no network."""
import asyncio
import datetime
from types import SimpleNamespace

import pytest

import app.services.daily_candles as sw
import app.services.smt_divergence_service as smt
from app.services.smt_divergence_service import (
    ACTIVE,
    CONFIRMED,
    DIVERGENCE,
    EXPIRED,
    HIGH,
    LEADER_TOUCHED,
    LOW,
    PAIR_EUR,
    PAIR_GBP,
    RowState,
    SmtDivergenceService,
)

from test_daily_candles import HOUR, D, day_hours, utc
from test_smt_divergence import CLOSE, LEVEL_DAY, LEVELS, PIP, UNTIL, bar, hour

MIN = datetime.timedelta(minutes=1)
NOW = CLOSE + 12 * MIN  # 17:12 New York: when the 17:10 scan has reached GBPUSD


class MemTable:
    """smt_divergence_levels in memory, with the same conditional-UPDATE semantics as the SQL."""

    OPEN = (ACTIVE, LEADER_TOUCHED)

    def __init__(self, group=smt.USD_GROUP):
        self.group = group
        self.rows: dict[int, dict] = {}
        self.claims: list[tuple] = []
        self._next = 1

    async def insert_row(self, trading_day, side, levels, expires_at, state):
        await asyncio.sleep(0)
        if any(r["trading_day"] == trading_day and r["side"] == side for r in self.rows.values()):
            return None
        row_id, self._next = self._next, self._next + 1
        self.rows[row_id] = dict(
            id=row_id, trading_day=trading_day, side=side, **{smt.level_column(p): levels[p] for p in self.group.pairs},
            status=state.status, leader_pair=state.leader_pair, leader_touched_at=state.leader_touched_at,
            leader_price=state.leader_price, leader_touch_basis=state.leader_touch_basis, deadline_at=state.deadline_at,
            resolved_at=state.resolved_at, laggard_touched_at=state.laggard_touched_at, laggard_price=state.laggard_price,
            laggard_best_price=state.laggard_best_price, detected_late=state.detected_late, expires_at=expires_at,
        )
        return row_id

    async def load_open_rows(self, now):
        return [RowState.from_model(SimpleNamespace(**r), self.group) for r in self.rows.values()
                if r["status"] in self.OPEN and r["expires_at"] > now]

    async def claim(self, row_id, expected, new, values, deadline_gt=None, deadline_le=None, expires_le=None):
        await asyncio.sleep(0)  # a real round trip yields, so concurrent claims interleave
        row = self.rows[row_id]
        if row["status"] != expected:
            return False
        if deadline_gt is not None and not (row["deadline_at"] is not None and row["deadline_at"] > deadline_gt):
            return False
        if deadline_le is not None and not (row["deadline_at"] is not None and row["deadline_at"] <= deadline_le):
            return False
        if expires_le is not None and not row["expires_at"] <= expires_le:
            return False
        row.update(values)
        row["status"] = new
        self.claims.append((row_id, expected, new))
        return True

    async def flush_best(self, row_id, best):
        row = self.rows[row_id]
        if row["status"] != LEADER_TOUCHED:
            return False
        row["laggard_best_price"] = best
        return True

    async def expire_in_db(self, now):
        ids = [r["id"] for r in self.rows.values() if r["status"] == ACTIVE and r["expires_at"] <= now]
        for row_id in ids:
            self.rows[row_id].update(status=EXPIRED, resolved_at=now)
        return ids

    async def select_current(self, now):
        rows = [r for r in self.rows.values() if r["expires_at"] > now]
        latest = max((r["trading_day"] for r in rows), default=None)
        return [SimpleNamespace(**r) for r in rows if r["trading_day"] == latest]

    async def count_rows(self, trading_day):
        return sum(1 for r in self.rows.values() if r["trading_day"] == trading_day)

    def attach(self, svc):
        svc._insert_row, svc._load_open_rows, svc._claim = self.insert_row, self.load_open_rows, self.claim
        svc._flush_best, svc._expire_in_db = self.flush_best, self.expire_in_db
        svc._select_current, svc._count_rows = self.select_current, self.count_rows

    def status(self, side):
        return next(r["status"] for r in self.rows.values() if r["side"] == side)

    def row(self, side):
        return next(r for r in self.rows.values() if r["side"] == side)


class Subscribers:
    def __init__(self, on=("on",), off=("off",)):
        self.users = [SimpleNamespace(chat_id=c, divergence_alerts_enabled=True) for c in on]

    async def find_divergence_subscribers(self):
        return self.users


def make(table=None, now=NOW, subscribers=None):
    table = table or MemTable()
    clock = {"now": now}
    sent: list[tuple[str, str]] = []

    async def send(chat_id, message):
        sent.append((chat_id, message))

    svc = SmtDivergenceService("k", None, subscribers or Subscribers(), send)
    svc.clock = lambda: clock["now"]
    table.attach(svc)
    subscribed: list[str] = []

    async def subscribe(symbol):
        subscribed.append(symbol)

    svc.subscribe_symbol = subscribe
    svc.subscribed = subscribed
    return svc, table, sent, clock


# the level day's candles: GBPUSD low 1.2650 / high 1.2780, EURUSD low 1.0720 / high 1.0850
def day_candles(label=LEVEL_DAY):
    return {PAIR_GBP: day_hours(label, 1.2700, 1.2780, 1.2650, 1.2720),
            PAIR_EUR: day_hours(label, 1.0780, 1.0850, 1.0720, 1.0800)}


def with_bars(candles, side, gbp=(), eur=()):
    out = {p: list(c) for p, c in candles.items()}
    out[PAIR_GBP] += [bar(side, PAIR_GBP, hour(h), b) for h, b in gbp]
    out[PAIR_EUR] += [bar(side, PAIR_EUR, hour(h), b) for h, b in eur]
    return out


def run(coro):
    return asyncio.run(coro)


def seeded(now=NOW, **kwargs):
    svc, table, sent, clock = make(now=now, **kwargs)
    assert run(svc.capture(day_candles(), LEVEL_DAY)) is True
    return svc, table, sent, clock


def tick(svc, clock, when, pair, side, pips_beyond):
    """A tick `pips_beyond` pips PAST `pair`'s level (negative = short of it), at `when`."""
    clock["now"] = when
    level = LEVELS[side][pair]
    price = level - pips_beyond * PIP if side == LOW else level + pips_beyond * PIP
    run(svc.check_tick(pair, price, None))
    return price


T = CLOSE + 2 * HOUR  # a touch two hours into the monitored period


def timer(svc, clock, when):
    clock["now"] = when
    run(svc.check_deadlines())


# ── Capturing the levels ────────────────────────────────────────────────────

def test_capture_records_both_sides_and_subscribes_the_feed():
    svc, table, sent, _ = make()
    assert run(svc.capture(day_candles(), LEVEL_DAY)) is True
    assert sorted(r["side"] for r in table.rows.values()) == [HIGH, LOW]
    low, high = table.row(LOW), table.row(HIGH)
    assert (low["gbpusd_level"], low["eurusd_level"]) == (1.2650, 1.0720)  # the previous-day LOWS
    assert (high["gbpusd_level"], high["eurusd_level"]) == (1.2780, 1.0850)  # the previous-day HIGHS
    assert low["trading_day"] == LEVEL_DAY and low["expires_at"] == UNTIL and low["status"] == ACTIVE
    assert svc.active_symbols() == {PAIR_GBP, PAIR_EUR} and sorted(svc.subscribed) == [PAIR_EUR, PAIR_GBP]
    assert sent == []


def test_capturing_again_creates_and_announces_nothing():
    svc, table, sent, _ = seeded()
    assert run(svc.capture(day_candles(), LEVEL_DAY)) is True
    assert len(table.rows) == 2 and sent == []


def test_capture_waits_until_both_pairs_have_the_expected_complete_day():
    svc, table, sent, _ = make()
    stale = day_candles(D(2026, 10, 26))  # the day before: the latest complete day isn't the expected one
    assert run(svc.capture({PAIR_GBP: day_candles()[PAIR_GBP], PAIR_EUR: stale[PAIR_EUR]}, LEVEL_DAY)) is False
    assert run(svc.capture({PAIR_GBP: day_candles()[PAIR_GBP], PAIR_EUR: []}, LEVEL_DAY)) is False
    assert table.rows == {}
    assert run(svc.capture(day_candles(), LEVEL_DAY)) is True  # the retry, with both pairs, works
    assert len(table.rows) == 2


def test_capture_skips_a_monitored_period_that_is_already_over():
    svc, table, _, _ = make(now=UNTIL + MIN)
    assert run(svc.capture(day_candles(), LEVEL_DAY)) is True and table.rows == {}


def test_incomplete_day_is_not_used():
    svc, table, _, _ = make()
    candles = {PAIR_GBP: day_hours(LEVEL_DAY, 1.27, 1.278, 1.265, 1.272, skip={23}),  # no final bar
               PAIR_EUR: day_candles()[PAIR_EUR]}
    assert run(svc.capture(candles, LEVEL_DAY)) is False and table.rows == {}


# ── Arming late: replay the bars since the close ────────────────────────────

def test_arming_with_the_leader_still_inside_its_window_resumes_waiting():
    svc, table, sent, _ = make()
    candles = with_bars(day_candles(), LOW, gbp=[(0, 2)], eur=[(0, -6)])  # GBPUSD touched its PDL in the first hour
    run(svc.capture(candles, LEVEL_DAY))
    row = table.row(LOW)
    assert (row["status"], row["leader_pair"], row["leader_touch_basis"]) == (LEADER_TOUCHED, PAIR_GBP, "hour")
    assert row["deadline_at"] == NOW + 60 * MIN  # judged from NOW (the hour is still running): never early
    assert row["laggard_best_price"] == pytest.approx(1.0720 + 6 * PIP)
    assert table.status(HIGH) == ACTIVE and sent == []


def test_arming_with_the_leader_more_than_60_minutes_ago_alerts_immediately_labelled_late():
    svc, table, sent, _ = make(now=CLOSE + 3 * HOUR)
    candles = with_bars(day_candles(), LOW, gbp=[(0, 2)], eur=[(0, -6), (1, -5), (2, -4)])
    run(svc.capture(candles, LEVEL_DAY))
    assert table.status(LOW) == DIVERGENCE and table.row(LOW)["detected_late"] is True
    assert len(sent) == 1
    text = sent[0][1]
    assert "SMT DIVERGENCE — PDL" in text and "⚡ Detected late" in text and "only known to the hour" in text
    assert "touched in the 2026-10-27 22:00–23:00 WAT hour" in text  # an hour, never an exact time we didn't observe
    assert "closest approach 4.0 pips from the level" in text
    run(svc.capture(candles, LEVEL_DAY))  # rescan / catch-up
    assert len(sent) == 1 and len(table.rows) == 2


def test_arming_with_both_pairs_in_the_same_hour_is_silent():
    svc, table, sent, _ = make(now=CLOSE + 5 * HOUR)
    run(svc.capture(with_bars(day_candles(), LOW, gbp=[(1, 2)], eur=[(1, 1)]), LEVEL_DAY))
    assert table.status(LOW) == CONFIRMED and sent == [] and table.status(HIGH) == ACTIVE
    assert [r.side for r in svc._rows.values()] == [HIGH]  # only the open side stays in the cache


def test_arming_with_the_laggard_touching_two_hours_later_alerts_that_it_touched_late():
    svc, table, sent, _ = make(now=CLOSE + 6 * HOUR)
    run(svc.capture(with_bars(day_candles(), HIGH, gbp=[(0, 2)], eur=[(3, 1)]), LEVEL_DAY))
    assert table.status(HIGH) == DIVERGENCE and len(sent) == 1
    assert "touched in the 2026-10-28 01:00–02:00 WAT hour, after the window had closed" in sent[0][1]
    assert "did not touch" not in sent[0][1]


def test_arming_with_no_bars_uses_the_latest_prices():
    svc, table, sent, clock = make()
    svc._ticks[PAIR_GBP] = (1.2648, NOW - 5 * MIN * 0)  # a fresh tick, 2 pips under GBPUSD's PDL; EURUSD has none
    run(svc.capture(day_candles(), LEVEL_DAY))
    row = table.row(LOW)
    assert (row["status"], row["leader_pair"], row["leader_touch_basis"]) == (LEADER_TOUCHED, PAIR_GBP, "detected")
    assert row["leader_touched_at"] == NOW  # the moment it was DETECTED: no earlier time is claimed
    svc2, table2, sent2, _ = make()
    svc2._ticks[PAIR_GBP] = (1.2648, NOW)
    svc2._ticks[PAIR_EUR] = (1.0718, NOW)  # both already beyond their PDL when armed
    run(svc2.capture(day_candles(), LEVEL_DAY))
    assert table2.status(LOW) == CONFIRMED and sent2 == []


# ── Live monitoring ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("side", [LOW, HIGH])
@pytest.mark.parametrize("leader,laggard", [(PAIR_GBP, PAIR_EUR), (PAIR_EUR, PAIR_GBP)])
def test_both_touch_inside_the_window_is_confirmed_and_silent(side, leader, laggard):
    svc, table, sent, clock = seeded()
    tick(svc, clock, T, leader, side, 1)
    row = table.row(side)
    assert (row["status"], row["leader_pair"], row["leader_touch_basis"]) == (LEADER_TOUCHED, leader, "tick")
    assert row["leader_touched_at"] == T and row["deadline_at"] == T + 60 * MIN
    tick(svc, clock, T + 59 * MIN, laggard, side, 0)  # exactly at its level counts as a touch
    assert table.status(side) == CONFIRMED and sent == []
    assert svc._rows and all(r.side != side for r in svc._rows.values())  # no longer watched
    other = HIGH if side == LOW else LOW
    assert table.status(other) == ACTIVE  # the other side is independent


@pytest.mark.parametrize("side", [LOW, HIGH])
@pytest.mark.parametrize("leader,laggard", [(PAIR_GBP, PAIR_EUR), (PAIR_EUR, PAIR_GBP)])
def test_laggard_never_touches_is_a_divergence_sent_once_by_the_timer(side, leader, laggard):
    svc, table, sent, clock = seeded()
    tick(svc, clock, T, leader, side, 1.4)
    tick(svc, clock, T + 20 * MIN, laggard, side, -9)
    tick(svc, clock, T + 30 * MIN, laggard, side, -3)  # its closest approach: 3 pips short
    tick(svc, clock, T + 40 * MIN, laggard, side, -6)
    timer(svc, clock, T + 60 * MIN - datetime.timedelta(seconds=1))
    assert sent == [] and table.status(side) == LEADER_TOUCHED
    timer(svc, clock, T + 60 * MIN)
    assert table.status(side) == DIVERGENCE and len(sent) == 1
    text = sent[0][1]
    name = "PDL" if side == LOW else "PDH"
    assert f"SMT DIVERGENCE — {name}" in text and f"🥇 <b>{leader}</b> took its {name}" in text
    assert f"🥈 <b>{laggard}</b> {name}" in text and "closest approach 3.0 pips from the level" in text
    assert "held " + ("above" if side == LOW else "below") + f" its {name} after 60 min" in text
    assert "⚡ Detected" not in text  # on time
    # the closest approach is saved on the row when it resolves (it needn't have been flushed earlier)
    level = LEVELS[side][laggard]
    assert table.row(side)["laggard_best_price"] == pytest.approx(level + 3 * PIP if side == LOW else level - 3 * PIP)
    timer(svc, clock, T + 61 * MIN)
    tick(svc, clock, T + 62 * MIN, laggard, side, 5)  # a touch after it was already reported: nothing more
    assert len(sent) == 1 and table.claims.count((table.row(side)["id"], LEADER_TOUCHED, DIVERGENCE)) == 1


def test_low_and_high_are_independent_on_the_same_day():
    svc, table, sent, clock = seeded()
    tick(svc, clock, T, PAIR_GBP, LOW, 1)  # LOW: GBPUSD leads and EURUSD never follows
    tick(svc, clock, T + 10 * MIN, PAIR_EUR, LOW, -4)
    tick(svc, clock, T + 30 * MIN, PAIR_EUR, HIGH, 1)  # HIGH: EURUSD leads, GBPUSD follows in time
    tick(svc, clock, T + 50 * MIN, PAIR_GBP, HIGH, 2)
    timer(svc, clock, T + 61 * MIN)
    assert (table.status(LOW), table.status(HIGH)) == (DIVERGENCE, CONFIRMED)
    assert len(sent) == 1 and "PDL" in sent[0][1]
    # and both can diverge: two separate alerts
    svc2, table2, sent2, clock2 = seeded()
    tick(svc2, clock2, T, PAIR_GBP, LOW, 1)
    tick(svc2, clock2, T + 5 * MIN, PAIR_EUR, HIGH, 1)
    timer(svc2, clock2, T + 66 * MIN)
    assert (table2.status(LOW), table2.status(HIGH)) == (DIVERGENCE, DIVERGENCE) and len(sent2) == 2
    assert sorted("PDL" if "— PDL" in m else "PDH" for _, m in sent2) == ["PDH", "PDL"]


def test_the_other_pair_already_past_its_level_means_in_sync_not_a_divergence():
    svc, table, sent, clock = seeded()
    clock["now"] = T
    svc._ticks[PAIR_EUR] = (1.0719, T)  # EURUSD's latest tick is already beyond its PDL (1.0720)...
    run(svc.check_tick(PAIR_GBP, 1.2649, None))  # ...when GBPUSD touches its PDL
    assert table.status(LOW) == CONFIRMED and sent == []


def test_a_stale_tick_of_the_other_pair_does_not_count_as_already_past():
    svc, table, sent, clock = seeded()
    svc._ticks[PAIR_EUR] = (1.0719, T - 10 * MIN)  # ten minutes old
    clock["now"] = T
    run(svc.check_tick(PAIR_GBP, 1.2649, None))
    assert table.status(LOW) == LEADER_TOUCHED


def test_window_cut_short_by_the_day_close():
    svc, table, sent, clock = seeded()
    tick(svc, clock, UNTIL - 30 * MIN, PAIR_GBP, LOW, 1)
    assert table.row(LOW)["deadline_at"] == UNTIL  # not touch + 60 min
    tick(svc, clock, UNTIL - 5 * MIN, PAIR_EUR, LOW, -3)
    timer(svc, clock, UNTIL - datetime.timedelta(seconds=1))
    assert sent == []
    timer(svc, clock, UNTIL)
    assert table.status(LOW) == DIVERGENCE and len(sent) == 1
    assert "⏱ Window: 30 min" in sent[0][1] and "after 30 min" in sent[0][1]


def test_untouched_rows_expire_silently_at_the_close():
    svc, table, sent, clock = seeded()
    timer(svc, clock, UNTIL - MIN)
    assert (table.status(LOW), table.status(HIGH)) == (ACTIVE, ACTIVE)
    timer(svc, clock, UNTIL)
    assert (table.status(LOW), table.status(HIGH)) == (EXPIRED, EXPIRED) and sent == []
    assert svc.active_symbols() == set()


def test_ignores_other_symbols_and_does_nothing_without_open_rows():
    svc, table, sent, clock = seeded()
    clock["now"] = T
    run(svc.check_tick("USDJPY", 100.0, None))
    assert table.claims == [] and "USDJPY" not in svc._ticks


# ── Weekends and DST ────────────────────────────────────────────────────────

def test_friday_levels_run_through_monday_and_weekend_quotes_are_ignored():
    friday = D(2026, 10, 30)
    now = utc(2026, 10, 30, 21, 12)  # Friday 17:12 EDT
    svc, table, sent, clock = make(now=now)
    assert run(svc.capture(day_candles(friday), friday)) is True
    assert table.row(LOW)["expires_at"] == utc(2026, 11, 2, 22)  # Monday's close, after the clocks changed (EST)
    tick(svc, clock, utc(2026, 10, 31, 12), PAIR_GBP, LOW, 5)  # Saturday: market closed, ignored
    assert table.status(LOW) == ACTIVE
    sunday_open = utc(2026, 11, 1, 22, 30)  # Sunday 17:30 EST: Monday's session has started
    tick(svc, clock, sunday_open, PAIR_GBP, LOW, 1)
    assert table.status(LOW) == LEADER_TOUCHED and table.row(LOW)["deadline_at"] == sunday_open + 60 * MIN
    tick(svc, clock, sunday_open + 20 * MIN, PAIR_EUR, LOW, 1)
    assert table.status(LOW) == CONFIRMED


def test_the_window_is_cut_by_monday_close_in_winter_time():
    day = D(2026, 11, 6)  # Friday; Monday 9 Nov closes 22:00 UTC (EST)
    svc, table, sent, clock = make(now=utc(2026, 11, 6, 22, 12))
    run(svc.capture(day_candles(day), day))
    leader_at = utc(2026, 11, 9, 21, 40)
    tick(svc, clock, leader_at, PAIR_EUR, HIGH, 1)
    assert table.row(HIGH)["deadline_at"] == utc(2026, 11, 9, 22)


# ── Duplicates, races and restarts ──────────────────────────────────────────

def test_duplicate_ticks_fire_each_transition_once():
    svc, table, sent, clock = seeded()
    for _ in range(5):
        tick(svc, clock, T, PAIR_GBP, LOW, 1)
    assert table.claims == [(table.row(LOW)["id"], ACTIVE, LEADER_TOUCHED)]
    timer(svc, clock, T + 61 * MIN)
    for _ in range(3):
        timer(svc, clock, T + 62 * MIN)
    assert len(sent) == 1


def test_concurrent_ticks_and_timers_cannot_double_send():
    svc, table, sent, clock = seeded()
    tick(svc, clock, T, PAIR_GBP, LOW, 1)

    async def storm():
        clock["now"] = T + 61 * MIN
        await asyncio.gather(
            svc.check_deadlines(), svc.check_deadlines(),
            svc.check_tick(PAIR_EUR, 1.0750, None), svc.check_tick(PAIR_GBP, 1.2640, None),
        )

    run(storm())
    assert len(sent) == 1 and table.status(LOW) == DIVERGENCE


@pytest.mark.parametrize("tick_first", [True, False])
def test_tick_versus_timer_race_at_the_deadline_has_one_outcome(tick_first):
    svc, table, sent, clock = seeded()
    tick(svc, clock, T, PAIR_GBP, LOW, 1)
    deadline = T + 60 * MIN

    async def race():
        clock["now"] = deadline - datetime.timedelta(seconds=1)
        touch = svc.check_tick(PAIR_EUR, 1.0719, None)  # the laggard touches 1 s BEFORE the deadline...
        clock["now"] = deadline + datetime.timedelta(seconds=1)
        expire = svc.check_deadlines()  # ...while the timer runs 1 s AFTER it
        await asyncio.gather(*((touch, expire) if tick_first else (expire, touch)))

    run(race())
    assert len(sent) <= 1
    status = table.status(LOW)
    assert status in (CONFIRMED, DIVERGENCE) and (len(sent) == 1) == (status == DIVERGENCE)
    assert len([c for c in table.claims if c[1] == LEADER_TOUCHED]) == 1  # exactly one resolution


def test_laggard_touching_after_the_deadline_but_before_the_timer_says_so():
    svc, table, sent, clock = seeded()
    tick(svc, clock, T, PAIR_GBP, LOW, 1)
    tick(svc, clock, T + 30 * MIN, PAIR_EUR, LOW, -4)
    tick(svc, clock, T + 67 * MIN, PAIR_EUR, LOW, 1)  # 7 minutes after the window closed; the timer hasn't run
    assert table.status(LOW) == DIVERGENCE and len(sent) == 1
    text = sent[0][1]
    assert "touched 7 min after the window closed" in text
    assert "did not touch" not in text and "held above" not in text and "closest approach" not in text
    assert table.row(LOW)["laggard_touched_at"] == T + 67 * MIN
    timer(svc, clock, T + 68 * MIN)
    assert len(sent) == 1  # the timer can't send it again


def test_a_tick_after_the_deadline_that_does_not_touch_still_resolves_it():
    svc, table, sent, clock = seeded()
    tick(svc, clock, T, PAIR_GBP, LOW, 1)
    tick(svc, clock, T + 61 * MIN, PAIR_EUR, LOW, -5)  # the laggard's tick, short of its level, after the deadline
    assert table.status(LOW) == DIVERGENCE and len(sent) == 1 and "held above" in sent[0][1]


def test_two_instances_resolving_the_same_deadline_send_one_alert():
    table = MemTable()
    a, _, sent_a, clock_a = seeded()  # (its own table, replaced below)
    a, table, sent_a, clock_a = make(table=table)
    run(a.capture(day_candles(), LEVEL_DAY))
    tick(a, clock_a, T, PAIR_GBP, LOW, 1)
    b, _, sent_b, clock_b = make(table=table)
    run(b.bootstrap())
    clock_a["now"] = clock_b["now"] = T + 61 * MIN

    async def both():
        await asyncio.gather(a.check_deadlines(), b.check_deadlines())

    run(both())
    assert len(sent_a) + len(sent_b) == 1 and table.status(LOW) == DIVERGENCE


def test_restart_mid_window_keeps_the_leader_deadline_and_closest_approach():
    svc, table, sent, clock = seeded()
    tick(svc, clock, T, PAIR_GBP, LOW, 1)
    tick(svc, clock, T + 10 * MIN, PAIR_EUR, LOW, -6)
    tick(svc, clock, T + 20 * MIN, PAIR_EUR, LOW, -2.5)
    tick(svc, clock, T + 25 * MIN, PAIR_EUR, LOW, -4)
    timer(svc, clock, T + 30 * MIN)  # the timer persists the closest approach
    assert table.row(LOW)["laggard_best_price"] == pytest.approx(1.0720 + 2.5 * PIP)

    revived, _, sent2, clock2 = make(table=table, now=T + 35 * MIN)  # the bot restarts
    run(revived.bootstrap())
    (row,) = [r for r in revived._rows.values() if r.side == LOW]
    assert (row.status, row.leader_pair, row.deadline_at) == (LEADER_TOUCHED, PAIR_GBP, T + 60 * MIN)
    assert row.laggard_best == pytest.approx(1.0720 + 2.5 * PIP)
    timer(revived, clock2, T + 61 * MIN)
    assert len(sent2) == 1 and "closest approach 2.5 pips from the level" in sent2[0][1]
    assert "feed difference" not in sent2[0][1]  # 2.5 pips is outside the 2-pip near-miss range


def test_restart_mid_window_then_the_laggard_touches_in_time():
    svc, table, sent, clock = seeded()
    tick(svc, clock, T, PAIR_EUR, HIGH, 1)
    revived, _, sent2, clock2 = make(table=table, now=T + 20 * MIN)
    run(revived.bootstrap())
    tick(revived, clock2, T + 40 * MIN, PAIR_GBP, HIGH, 1)
    assert table.status(HIGH) == CONFIRMED and sent == [] and sent2 == []


def test_a_window_that_closed_while_the_bot_was_down_is_reported_as_detected_late():
    svc, table, sent, clock = seeded()
    tick(svc, clock, T, PAIR_GBP, LOW, 1)
    revived, _, sent2, clock2 = make(table=table, now=T + 75 * MIN)
    run(revived.bootstrap())
    timer(revived, clock2, T + 75 * MIN)
    assert len(sent2) == 1 and "⚡ Detected 15 min after the window closed" in sent2[0][1]
    assert table.row(LOW)["detected_late"] is True


# ── The closest approach and the near-miss note ─────────────────────────────

def test_a_near_miss_is_annotated_and_still_sent():
    svc, table, sent, clock = seeded()
    tick(svc, clock, T, PAIR_GBP, LOW, 1)
    tick(svc, clock, T + 30 * MIN, PAIR_EUR, LOW, -1.5)  # 1.5 pips short of EURUSD's PDL at its closest
    tick(svc, clock, T + 50 * MIN, PAIR_EUR, LOW, -7)
    timer(svc, clock, T + 61 * MIN)
    assert len(sent) == 1
    text = sent[0][1]
    assert "closest approach 1.5 pips from the level" in text
    assert "⚠️ The closest approach was within 2 pips of the level" in text and "feed difference" in text


def test_closest_approach_is_the_laggards_alone_and_starts_when_the_window_opens():
    svc, table, sent, clock = seeded()
    tick(svc, clock, T - 5 * MIN, PAIR_EUR, LOW, -0.5)  # before anyone led: not part of the window
    tick(svc, clock, T, PAIR_GBP, LOW, 1)
    tick(svc, clock, T + 5 * MIN, PAIR_GBP, LOW, 4)  # the LEADER moving further is irrelevant
    tick(svc, clock, T + 10 * MIN, PAIR_EUR, LOW, -6)
    timer(svc, clock, T + 61 * MIN)
    assert "closest approach 6.0 pips from the level" in sent[0][1]


# ── Failures ────────────────────────────────────────────────────────────────

def test_a_database_error_changes_nothing_and_is_retried():
    svc, table, sent, clock = seeded()
    real_claim = table.claim

    async def broken(*args, **kwargs):
        raise RuntimeError("db down")

    svc._claim = broken
    tick(svc, clock, T, PAIR_GBP, LOW, 1)
    assert table.status(LOW) == ACTIVE and sent == []
    svc._claim = real_claim
    tick(svc, clock, T + MIN, PAIR_GBP, LOW, 1)  # the next tick retries
    assert table.status(LOW) == LEADER_TOUCHED


# ── Notification routing ────────────────────────────────────────────────────

def test_only_divergence_subscribers_are_messaged():
    users = Subscribers(on=("alice", "bob"))
    svc, table, sent, clock = seeded(subscribers=users)
    tick(svc, clock, T, PAIR_GBP, LOW, 1)
    timer(svc, clock, T + 61 * MIN)
    assert sorted(chat for chat, _ in sent) == ["alice", "bob"]
    quiet, table2, sent2, clock2 = seeded(subscribers=Subscribers(on=()))
    tick(quiet, clock2, T, PAIR_GBP, LOW, 1)
    timer(quiet, clock2, T + 61 * MIN)
    assert sent2 == [] and table2.status(LOW) == DIVERGENCE  # still resolved; just nobody to tell


# ── The daily capture (17:10 New York) ──────────────────────────────────────

class MarketData:
    def __init__(self, candles=None, quota=False):
        self.candles, self.quota, self.requests = candles or day_candles(), quota, []

    async def get_candles(self, pair, interval, api_key, outputsize=2):
        self.requests.append((pair, interval, outputsize))
        return ([], True) if self.quota else (self.candles[pair], False)


def test_record_levels_fetches_once_and_not_again_once_recorded(monkeypatch):
    monkeypatch.setattr(sw, "SCAN_REQUEST_SPACING_SECONDS", 0)
    svc, table, _, _ = make()
    svc.market_data = MarketData()
    assert run(svc.record_levels()) == (True, "Levels recorded.")
    assert [r[0] for r in svc.market_data.requests] == [PAIR_GBP, PAIR_EUR] and len(table.rows) == 2
    assert run(svc.record_levels()) == (True, None)  # nothing to do: no further requests
    assert len(svc.market_data.requests) == 2


def test_record_levels_reports_not_done_while_a_pair_is_lagging_then_succeeds(monkeypatch):
    monkeypatch.setattr(sw, "SCAN_REQUEST_SPACING_SECONDS", 0)
    svc, table, _, _ = make()
    lagging = {**day_candles(), PAIR_EUR: day_candles(D(2026, 10, 26))[PAIR_EUR]}
    svc.market_data = MarketData(lagging)
    done, message = run(svc.record_levels())
    assert done is False and "isn't available" in message and table.rows == {}
    svc.market_data = MarketData()  # the 15-minute retry
    assert run(svc.record_levels())[0] is True and len(table.rows) == 2


# ── Catch-up and the own fetch ──────────────────────────────────────────────

def test_needs_catch_up_only_when_levels_are_missing_inside_the_period():
    svc, table, _, clock = make(now=CLOSE + 5 * MIN)  # before the 17:10 scan time
    assert run(svc.needs_catch_up()) is False
    clock["now"] = CLOSE + 11 * MIN
    assert run(svc.needs_catch_up()) is True
    run(svc.capture(day_candles(), LEVEL_DAY))
    assert run(svc.needs_catch_up()) is False  # recorded
    clock["now"] = UNTIL + MIN
    assert run(svc.needs_catch_up()) is False  # the next day's levels aren't due until its own 17:10... and this one is over
    other, _, _, clock2 = make(now=UNTIL - MIN)
    assert run(other.needs_catch_up()) is True  # missing, and the period is still running


def test_catch_up_fetches_only_when_the_levels_are_missing():
    svc, table, _, _ = make(now=CLOSE + 30 * MIN)
    calls = []

    async def fake_capture_now():
        calls.append("own")
        return "ok"

    svc.capture_now = fake_capture_now
    assert run(svc.catch_up()) == "ok" and calls == ["own"]
    table.rows[1] = dict(trading_day=LEVEL_DAY)  # levels exist: nothing to do
    assert run(svc.catch_up()) is None and calls == ["own"]


def test_own_fetch_gets_just_the_two_pairs_with_spacing(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(smt.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(sw, "SCAN_REQUEST_SPACING_SECONDS", 8.0)
    svc, table, sent, _ = make(now=CLOSE + 30 * MIN)
    svc.market_data = MarketData()
    assert run(svc.capture_now()) == "Levels recorded."
    fetched = svc.market_data.requests
    assert [f[0] for f in fetched] == [PAIR_GBP, PAIR_EUR] and len(table.rows) == 2
    assert fetched[0][1:] == (sw.ONE_H_INTERVAL, sw.ONE_H_LOOKBACK)
    assert any(7 < s <= 8 for s in sleeps)  # the second request waited out the spacing


def test_own_fetch_reports_quota_and_missing_data(monkeypatch):
    monkeypatch.setattr(sw, "SCAN_REQUEST_SPACING_SECONDS", 0)
    svc, table, _, _ = make(now=CLOSE + 30 * MIN)

    class Quota:
        async def get_candles(self, *a, **k):
            return [], True

    class Empty:
        async def get_candles(self, *a, **k):
            return [], False

    svc.market_data = Quota()
    assert "quota" in run(svc.capture_now())
    svc.market_data = Empty()
    assert "No candle data" in run(svc.capture_now()) and table.rows == {}


# ── /divergence ─────────────────────────────────────────────────────────────

def test_status_message_shows_levels_and_each_sides_state():
    svc, table, sent, clock = seeded()
    text = run(svc.get_status_message())
    assert "Levels from Tue 2026-10-27 · monitored until 2026-10-28 22:00 WAT" in text
    assert "<b>PDH</b>  GBPUSD 1.27800 · EURUSD 1.08500" in text and "<b>PDL</b>  GBPUSD 1.26500 · EURUSD 1.07200" in text
    assert text.count("⏳ waiting") == 2
    tick(svc, clock, T, PAIR_GBP, LOW, 1)
    tick(svc, clock, T + 10 * MIN, PAIR_EUR, LOW, -3)
    timer(svc, clock, T + 20 * MIN)  # (persists the closest approach)
    text = run(svc.get_status_message())
    assert "🟡 GBPUSD touched its PDL 2026-10-28 00:00 WAT — EURUSD must touch by 2026-10-28 01:00 WAT" in text
    assert "(closest so far: 3.0 pips away)" in text
    tick(svc, clock, T + 30 * MIN, PAIR_EUR, LOW, 1)
    assert "✅ confirmed" in run(svc.get_status_message())
    timer(svc, clock, UNTIL - MIN)
    assert "⌛ expired" not in run(svc.get_status_message())  # still inside the period
    timer(svc, clock, UNTIL)
    assert "No levels are being monitored yet" in run(svc.get_status_message())  # the period is over


def test_status_message_for_a_divergence_and_when_nothing_is_monitored():
    svc, table, sent, clock = seeded()
    tick(svc, clock, T, PAIR_GBP, HIGH, 1)
    timer(svc, clock, T + 61 * MIN)
    assert "🔀 divergence — alert sent" in run(svc.get_status_message())
    empty, _, _, _ = make()
    assert "No levels are being monitored yet" in run(empty.get_status_message())


def test_confirmed_rows_keep_the_laggards_closest_approach_too():
    svc, table, sent, clock = seeded()
    tick(svc, clock, T, PAIR_GBP, LOW, 1)
    tick(svc, clock, T + 10 * MIN, PAIR_EUR, LOW, -4)
    tick(svc, clock, T + 20 * MIN, PAIR_EUR, LOW, 0.5)  # touches
    row = table.row(LOW)
    assert row["status"] == CONFIRMED and row["laggard_touched_at"] == T + 20 * MIN
    assert row["laggard_best_price"] == pytest.approx(1.0720 - 0.5 * PIP)  # its best IS the touching tick (0.5 pip beyond)


def test_concurrent_duplicate_leader_ticks_leave_the_row_watched():
    svc, table, sent, clock = seeded()

    async def storm():
        clock["now"] = T
        await asyncio.gather(*(svc.check_tick(PAIR_GBP, 1.2649, None) for _ in range(5)))

    run(storm())
    assert table.claims == [(table.row(LOW)["id"], ACTIVE, LEADER_TOUCHED)]
    assert any(r.side == LOW and r.status == LEADER_TOUCHED for r in svc._rows.values())  # still in the cache
    tick(svc, clock, T + 10 * MIN, PAIR_EUR, LOW, 1)  # so the laggard's touch is still seen
    assert table.status(LOW) == CONFIRMED
