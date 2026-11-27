"""The second pair group, EURJPY / GBPJPY: same engine, its own table, and kept apart from GBPUSD / EURUSD.
In-memory tables, injectable clock, no network (the real SQL is in test_integration_postgres.py)."""
import logging

import app.services.daily_candles as sw
import app.services.smt_divergence_service as smt
from app.services.smt_divergence_service import (
    ACTIVE,
    CONFIRMED,
    DIVERGENCE,
    HIGH,
    JPY_GROUP,
    LEADER_TOUCHED,
    LOW,
    USD_GROUP,
    SmtDivergenceHub,
)

from test_daily_candles import HOUR, day_hours
from test_smt_divergence import CLOSE, LEVEL_DAY
from test_smt_service import MIN, NOW, MemTable, Subscribers, day_candles, run

EJ, GJ = "EURJPY", "GBPJPY"
PIP = 0.01
# EURJPY low 161.200 / high 162.500, GBPJPY low 188.400 / high 189.900
LEVELS = {LOW: {EJ: 161.20, GJ: 188.40}, HIGH: {EJ: 162.50, GJ: 189.90}}
T = CLOSE + 2 * HOUR


def jpy_candles():
    return {EJ: day_hours(LEVEL_DAY, 161.80, 162.50, 161.20, 162.00),
            GJ: day_hours(LEVEL_DAY, 189.00, 189.90, 188.40, 189.50)}


def price(pair, side, pips_beyond):
    level = LEVELS[side][pair]
    return level - pips_beyond * PIP if side == LOW else level + pips_beyond * PIP


def make_hub(now=NOW, market_data=None, subscribers=None):
    clock, sent, subscribed = {"now": now}, [], []

    async def send(chat_id, message):
        sent.append((chat_id, message))

    async def subscribe(symbol):
        subscribed.append(symbol)

    hub = SmtDivergenceHub("k", market_data, subscribers or Subscribers(), send)
    tables = {}
    for service in hub.services:
        service.clock = lambda: clock["now"]
        tables[service.group] = MemTable(service.group)
        tables[service.group].attach(service)
    hub.wire(subscribe_symbol=subscribe)
    return hub, tables[USD_GROUP], tables[JPY_GROUP], sent, clock, subscribed


def armed(**kwargs):
    hub, usd, jpy, sent, clock, subscribed = make_hub(**kwargs)
    assert run(hub.services[0].capture(day_candles(), LEVEL_DAY)) is True
    assert run(hub.services[1].capture(jpy_candles(), LEVEL_DAY)) is True
    return hub, usd, jpy, sent, clock


def tick(hub, clock, when, pair, side, pips_beyond):
    clock["now"] = when
    run(hub.check_tick(pair, price(pair, side, pips_beyond), None))


def timer(hub, clock, when):
    clock["now"] = when
    run(hub.check_deadlines())


# ── Levels ──────────────────────────────────────────────────────────────────

def test_each_group_records_its_own_levels_in_its_own_table():
    hub, usd, jpy, sent, _, subscribed = make_hub()
    assert run(hub.services[1].capture(jpy_candles(), LEVEL_DAY)) is True
    assert usd.rows == {} and sorted(r["side"] for r in jpy.rows.values()) == [HIGH, LOW]
    assert (jpy.row(LOW)["eurjpy_level"], jpy.row(LOW)["gbpjpy_level"]) == (161.20, 188.40)
    assert (jpy.row(HIGH)["eurjpy_level"], jpy.row(HIGH)["gbpjpy_level"]) == (162.50, 189.90)
    assert hub.active_symbols() == {EJ, GJ} and sorted(subscribed) == [EJ, GJ] and sent == []
    assert run(hub.services[0].capture(day_candles(), LEVEL_DAY)) is True
    assert len(usd.rows) == 2 and len(jpy.rows) == 2
    assert hub.active_symbols() == {EJ, GJ, "GBPUSD", "EURUSD"}


def test_the_daily_capture_records_both_groups_and_asks_for_a_retry_when_one_cannot(monkeypatch, caplog):
    monkeypatch.setattr(sw, "SCAN_REQUEST_SPACING_SECONDS", 0)
    market = MarketData()
    hub, usd, jpy, *_ = make_hub(market_data=market)
    real = market.get_candles

    async def jpy_quota(pair, interval, api_key, outputsize):
        if pair == GJ:
            return [], True
        return await real(pair, interval, api_key, outputsize)

    market.get_candles = jpy_quota
    with caplog.at_level(logging.INFO):
        assert run(hub.record_levels()) is False
    assert len(usd.rows) == 2 and jpy.rows == {}
    assert "[SmtDivergence JPY] Twelve Data quota exceeded" in caplog.text
    market.get_candles = real
    market.requests.clear()
    assert run(hub.record_levels()) is True  # the retry
    assert market.requests == [EJ, GJ] and len(jpy.rows) == 2  # USD is not fetched again


def test_an_error_in_one_groups_capture_asks_for_a_retry_without_stopping_the_other(monkeypatch):
    monkeypatch.setattr(sw, "SCAN_REQUEST_SPACING_SECONDS", 0)
    hub, _, jpy, *_ = make_hub(market_data=MarketData())

    async def broken():
        raise RuntimeError("boom")

    hub.services[0].record_levels = broken
    assert run(hub.record_levels()) is False and len(jpy.rows) == 2


# ── Live behaviour, on 0.01 pips ────────────────────────────────────────────

def test_jpy_divergence_sends_one_jpy_alert_and_leaves_usd_alone():
    hub, usd, jpy, sent, clock = armed()
    tick(hub, clock, T, EJ, LOW, 1)  # EURJPY takes its PDL
    assert jpy.status(LOW) == LEADER_TOUCHED and jpy.row(LOW)["leader_pair"] == EJ
    tick(hub, clock, T + 5 * MIN, GJ, LOW, -1.5)  # GBPJPY gets within 1.5 pips (0.015)
    timer(hub, clock, T + 30 * MIN)
    timer(hub, clock, T + 31 * MIN)
    assert jpy.status(LOW) == DIVERGENCE and len(sent) == 1
    message = sent[0][1]
    assert "SMT DIVERGENCE (JPY) — PDL" in message
    assert "<b>EURJPY</b> took its PDL 161.200" in message and "<b>GBPJPY</b> PDL 188.400" in message
    assert "closest approach 1.5 pips" in message and "within 2 pips" in message
    assert [r["status"] for r in usd.rows.values()] == [ACTIVE, ACTIVE]


def test_jpy_pdh_in_sync_is_silent():
    hub, _, jpy, sent, clock = armed()
    tick(hub, clock, T, GJ, HIGH, 0)
    tick(hub, clock, T + 29 * MIN, EJ, HIGH, 0.5)
    timer(hub, clock, T + 2 * HOUR)
    assert jpy.status(HIGH) == CONFIRMED and sent == []


def test_a_pair_of_one_group_never_confirms_the_other_group():
    hub, usd, jpy, sent, clock = armed()
    clock["now"] = T
    run(hub.check_tick("GBPUSD", 1.2649, None))  # a USD leader
    tick(hub, clock, T + MIN, EJ, LOW, 1)  # a JPY leader
    assert (usd.status(LOW), jpy.status(LOW)) == (LEADER_TOUCHED, LEADER_TOUCHED)
    assert (usd.row(LOW)["leader_pair"], jpy.row(LOW)["leader_pair"]) == ("GBPUSD", EJ)
    timer(hub, clock, T + 2 * HOUR)
    titles = sorted(m.splitlines()[0] for _, m in sent)
    assert titles == ["🔀 <b>SMT DIVERGENCE (JPY) — PDL</b>", "🔀 <b>SMT DIVERGENCE — PDL</b>"]


def test_jpy_armed_after_the_move_replays_the_hourly_bars():
    hub, _, jpy, sent, clock, _ = make_hub(now=CLOSE + 5 * HOUR)
    candles = jpy_candles()
    candles[EJ] += [{"datetime": CLOSE + HOUR, "high": 161.50, "low": 161.15}]
    candles[GJ] += [{"datetime": CLOSE + HOUR, "high": 188.90, "low": 188.55}]
    assert run(hub.services[1].capture(candles, LEVEL_DAY)) is True
    assert jpy.status(LOW) == DIVERGENCE and jpy.row(LOW)["detected_late"] is True
    assert len(sent) == 1 and "(JPY)" in sent[0][1] and "Detected late" in sent[0][1]


# ── The hub ─────────────────────────────────────────────────────────────────

def test_status_shows_both_groups_separately():
    hub, _, _, _, clock = armed()
    tick(hub, clock, T, EJ, LOW, 1)
    tick(hub, clock, T + 5 * MIN, GJ, LOW, -3)
    timer(hub, clock, T + 6 * MIN)  # persists the closest approach
    text = run(hub.get_status_message())
    usd_part, jpy_part = text.split("SMT Divergence — EURJPY / GBPJPY")
    assert "SMT Divergence — GBPUSD / EURUSD" in usd_part and "EURJPY" not in usd_part and "GBPJPY" not in usd_part
    assert "EURJPY 161.200 · GBPJPY 188.400" in jpy_part
    assert "EURJPY touched its PDL" in jpy_part and "closest so far: 3.0 pips away" in jpy_part


def test_one_group_failing_never_stops_the_other(caplog):
    hub, usd, jpy, sent, clock = armed()
    tick(hub, clock, T, EJ, LOW, 1)

    async def broken(*args, **kwargs):
        raise RuntimeError("usd table is down")

    hub.services[0].check_tick = hub.services[0].check_deadlines = broken
    with caplog.at_level(logging.ERROR):
        tick(hub, clock, T + MIN, GJ, LOW, -5)
        timer(hub, clock, T + 31 * MIN)
    assert jpy.status(LOW) == DIVERGENCE and len(sent) == 1
    assert "Tick check failed: usd table is down" in caplog.text and "Deadline check failed" in caplog.text


class MarketData:
    def __init__(self):
        self.requests = []

    async def get_candles(self, pair, interval, api_key, outputsize):
        self.requests.append(pair)
        return {**day_candles(), **jpy_candles()}[pair], False


def test_catch_up_after_deploying_fetches_only_the_group_that_has_no_levels(monkeypatch):
    monkeypatch.setattr(sw, "SCAN_REQUEST_SPACING_SECONDS", 0)
    market = MarketData()
    hub, usd, jpy, sent, _, _ = make_hub(market_data=market)
    assert run(hub.services[0].capture(day_candles(), LEVEL_DAY)) is True  # USD is already live
    before = {k: dict(v) for k, v in usd.rows.items()}
    result = run(hub.catch_up())
    assert market.requests == [EJ, GJ] and result == "EURJPY / GBPJPY: Levels recorded."
    assert len(jpy.rows) == 2 and usd.rows == before and sent == []
    assert run(hub.catch_up()) is None and len(market.requests) == 2


def test_the_two_groups_share_one_request_spacing(monkeypatch):
    monkeypatch.setattr(sw, "SCAN_REQUEST_SPACING_SECONDS", 8.0)
    now, waits = {"t": 1000.0}, []

    async def sleep(seconds):
        if seconds:  # (the in-memory table yields with sleep(0))
            waits.append(seconds)
        now["t"] += seconds

    monkeypatch.setattr(smt.time, "monotonic", lambda: now["t"])
    monkeypatch.setattr(smt.asyncio, "sleep", sleep)
    market = MarketData()
    hub, *_ = make_hub(market_data=market)
    result = run(hub.capture_now())
    assert market.requests == ["GBPUSD", "EURUSD", EJ, GJ]
    assert waits == [8.0, 8.0, 8.0]  # also between the second USD request and the first JPY one
    assert result == "GBPUSD / EURUSD: Levels recorded.\nEURJPY / GBPJPY: Levels recorded."


def test_capture_now_reports_each_group_even_when_one_fails(monkeypatch):
    monkeypatch.setattr(sw, "SCAN_REQUEST_SPACING_SECONDS", 0)
    hub, _, jpy, *_ = make_hub(market_data=MarketData())

    async def broken():
        raise RuntimeError("boom")

    hub.services[0].capture_now = broken
    assert run(hub.capture_now()) == "GBPUSD / EURUSD: failed — boom\nEURJPY / GBPJPY: Levels recorded."
    assert len(jpy.rows) == 2
