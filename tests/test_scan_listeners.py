"""DailySweepService.candle_listeners: other features may reuse the candles a scan already fetched, and a listener
that fails must never change or break the scan."""
import asyncio
import logging

import app.services.daily_sweep_service as sm
from app.services.daily_sweep_service import DailySweepService, SetupState

from test_daily_sweep import D, _FakeMarketData, day_hours, ny

MON, TUE = D(2026, 10, 26), D(2026, 10, 27)
NOW = ny(2026, 10, 27, 17, 10)


class _MD(_FakeMarketData):
    async def get_current_price(self, symbol, api_key):
        return None, False


def _service(bars=None, quota=False):
    bars = bars if bars is not None else day_hours(MON, 100, 110, 90, 105) + day_hours(TUE, 105, 112, 95, 100)  # a SELL sweep
    md = _MD(bars)
    if quota:
        async def quota_candles(*args, **kwargs):
            return [], True

        md.get_candles = quota_candles
    svc = DailySweepService("k", md, None, None)
    svc.clock = lambda: NOW
    stored = []

    async def fake_store(pair, signal, d1, d2, expires_at, stage=0, reached=None):
        stored.append(signal.direction)
        return SetupState(id=1, pair=pair, direction=signal.direction, day1=(1, 2, 0, 1), day2=(1, 2, 0, 1),
                          level=signal.level, day2_date=d2.date, day3_expires_at=expires_at, kind=signal.kind)

    svc._store_setup = fake_store
    return svc, stored


def _scan(svc):
    return asyncio.run(svc._scan_pair("EURUSD", TUE))


def test_listener_gets_the_fetched_candles_once_per_pair(monkeypatch):
    svc, _ = _service()
    calls = []
    svc.candle_listeners.append(lambda pair, candles, day, now: calls.append((pair, len(candles), day, now)))
    _scan(svc)
    assert calls == [("EURUSD", len(svc.market_data.bars), TUE, NOW)]


def test_listener_is_not_called_when_nothing_was_fetched():
    for kwargs in ({"bars": []}, {"quota": True}):
        svc, _ = _service(**kwargs)
        calls = []
        svc.candle_listeners.append(lambda *args: calls.append(args))
        assert _scan(svc).status in ("nodata", "quota")
        assert calls == []


def test_a_listener_that_raises_leaves_the_scan_result_unchanged(caplog):
    clean, clean_stored = _service()
    expected = _scan(clean)

    broken, broken_stored = _service()
    ran_after = []

    def boom(pair, candles, day, now):
        raise RuntimeError("smt exploded")

    broken.candle_listeners.extend([boom, lambda *args: ran_after.append(args[0])])
    with caplog.at_level(logging.ERROR):
        result = _scan(broken)

    assert (result.status, result.pair, result.setup) == (expected.status, expected.pair, expected.setup)
    assert result.status == "new" and broken_stored == clean_stored == ["SELL"]
    assert ran_after == ["EURUSD"]  # the next listener still runs
    assert "Candle listener" in caplog.text and "smt exploded" in caplog.text  # and the failure is logged


def test_a_raising_listener_does_not_change_a_whole_run_scan(monkeypatch):
    monkeypatch.setattr(sm, "SCAN_REQUEST_SPACING_SECONDS", 0)

    async def noop(*a, **k):
        return 0

    def build(with_boom):
        svc, stored = _service()
        svc.pairs = ["EURUSD"]
        svc.expire_setups, svc._record_scan = noop, noop
        digests = []

        async def digest(setups):
            digests.append([s.pair for s in setups])

        svc._broadcast_digest = digest
        if with_boom:
            svc.candle_listeners.append(lambda *a: (_ for _ in ()).throw(ValueError("nope")))
        report = asyncio.run(svc.run_scan())
        return [r.status for r in report.results], report.summary(), digests, stored

    assert build(True) == build(False)
