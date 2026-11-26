"""How the SMT divergence service is plugged in: the Finnhub tick hook and subscriptions, the scheduler jobs,
build_services and the Telegram commands."""
import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from telegram.ext import Application

import app.main as main
import app.services.finnhub_price_service as finnhub_module
from app.scheduler import register_jobs
from app.services.finnhub_price_service import FinnhubPriceService
from app.services.smt_divergence_service import DEADLINE_CHECK_INTERVAL_SECONDS, PAIR_EUR, PAIR_GBP
from app.telegram_bot import TelegramBotService


# ── Finnhub: ticks and subscriptions ────────────────────────────────────────

class PriceAlerts:
    async def check_tick_against_alerts(self, symbol, price, previous_price):
        return []

    async def get_active_alerts_grouped_by_symbol(self):
        return {}


class Recorder:
    def __init__(self, fail=False, symbols=()):
        self.ticks, self.fail, self.symbols = [], fail, set(symbols)

    async def check_tick(self, symbol, price, previous_price):
        self.ticks.append((symbol, price, previous_price))
        if self.fail:
            raise RuntimeError("boom")

    def active_symbols(self):
        return self.symbols


def trade(symbol, price):
    return json.dumps({"type": "trade", "data": [{"s": symbol, "p": price}]})


def make_finnhub(sweep=None, smt=None):
    async def send(chat_id, message):
        pass

    return FinnhubPriceService(PriceAlerts(), send, daily_sweep_service=sweep, smt_divergence_service=smt)


def test_every_tick_is_forwarded_to_the_smt_service():
    smt = Recorder()
    svc = make_finnhub(smt=smt)
    asyncio.run(svc._handle_message(trade("OANDA:GBP_USD", 1.2649)))
    asyncio.run(svc._handle_message(trade("OANDA:GBP_USD", 1.2641)))
    assert smt.ticks == [("GBPUSD", 1.2649, None), ("GBPUSD", 1.2641, 1.2649)]


def test_an_smt_failure_never_stops_the_other_features_or_the_feed(caplog):
    sweep, smt = Recorder(), Recorder(fail=True)
    svc = make_finnhub(sweep=sweep, smt=smt)
    with caplog.at_level(logging.ERROR):
        asyncio.run(svc._handle_message(trade("OANDA:EUR_USD", 1.0719)))
        asyncio.run(svc._handle_message(trade("OANDA:EUR_USD", 1.0718)))  # and the feed keeps going
    assert len(sweep.ticks) == 2 and len(smt.ticks) == 2
    assert "SMT divergence tick check failed for EURUSD" in caplog.text
    # and the other way round: a failing sweep check doesn't starve SMT
    sweep2, smt2 = Recorder(fail=True), Recorder()
    asyncio.run(make_finnhub(sweep=sweep2, smt=smt2)._handle_message(trade("OANDA:EUR_USD", 1.07)))
    assert len(smt2.ticks) == 1


def test_works_without_the_smt_service():
    svc = make_finnhub()
    asyncio.run(svc._handle_message(trade("OANDA:EUR_USD", 1.07)))
    assert svc.last_price["EURUSD"] == 1.07


def test_pairs_with_open_levels_are_subscribed_even_with_no_price_alerts(monkeypatch):
    real_sleep = asyncio.sleep
    sent = []

    class WS:
        async def send(self, message):
            sent.append(json.loads(message))

    async def no_delay(seconds):
        await real_sleep(0)

    monkeypatch.setattr(finnhub_module.asyncio, "sleep", no_delay)

    async def body(symbols):
        svc = make_finnhub(smt=Recorder(symbols=symbols))
        svc.ws = WS()
        await svc._subscribe_to_active_symbols()
        for _ in range(10):
            await real_sleep(0)
        return svc

    svc = asyncio.run(body({PAIR_GBP, PAIR_EUR}))
    assert sorted(m["symbol"] for m in sent) == ["OANDA:EUR_USD", "OANDA:GBP_USD"]
    assert all(m["type"] == "subscribe" for m in sent) and svc.subscribed_symbols == {PAIR_GBP, PAIR_EUR}
    sent.clear()
    asyncio.run(body(set()))  # nothing open: nothing subscribed
    assert sent == []


# ── Scheduler ───────────────────────────────────────────────────────────────

class Smt:
    def __init__(self, catch_up_result="ok", fail=False):
        self.calls, self.result, self.fail = [], catch_up_result, fail

    async def check_deadlines(self):
        self.calls.append("deadlines")
        if self.fail:
            raise RuntimeError("boom")

    async def catch_up(self):
        self.calls.append("catch_up")
        if self.fail:
            raise RuntimeError("boom")
        return self.result


def jobs_for(smt):
    application = Application.builder().token("1:x").build()
    register_jobs(application, price_alert_service=None, telegram_bot=None, users_service=None, smt_divergence_service=smt)
    return application


def test_the_deadline_check_runs_every_minute_and_the_catch_up_once():
    async def body():
        smt = Smt()
        application = jobs_for(smt)
        application.job_queue.scheduler.start()
        try:
            by_name = {job.name: job for job in application.job_queue.jobs()}
            assert {"check_smt_deadlines", "catch_up_smt_levels", "run_daily_cleanup", "reset_alert_id_counters"} <= set(by_name)
            deadline_job = by_name["check_smt_deadlines"]
            assert deadline_job.job.trigger.interval.total_seconds() == DEADLINE_CHECK_INTERVAL_SECONDS == 60
            await deadline_job.callback(SimpleNamespace())
            await by_name["catch_up_smt_levels"].callback(SimpleNamespace())
            assert smt.calls == ["deadlines", "catch_up"]
        finally:
            application.job_queue.scheduler.shutdown(wait=False)

    asyncio.run(body())


def test_job_failures_are_logged_not_raised(caplog):
    async def body():
        smt = Smt(fail=True)
        application = jobs_for(smt)
        application.job_queue.scheduler.start()
        try:
            by_name = {job.name: job for job in application.job_queue.jobs()}
            with caplog.at_level(logging.ERROR):
                await by_name["check_smt_deadlines"].callback(SimpleNamespace())
                await by_name["catch_up_smt_levels"].callback(SimpleNamespace())
        finally:
            application.job_queue.scheduler.shutdown(wait=False)

    asyncio.run(body())
    assert "Deadline check failed" in caplog.text and "Catch-up failed" in caplog.text


def test_no_smt_jobs_without_the_service():
    async def body():
        application = jobs_for(None)
        application.job_queue.scheduler.start()
        try:
            return {job.name for job in application.job_queue.jobs()}
        finally:
            application.job_queue.scheduler.shutdown(wait=False)

    names = asyncio.run(body())
    assert "check_smt_deadlines" not in names and "catch_up_smt_levels" not in names


# ── build_services ──────────────────────────────────────────────────────────

def test_build_services_wires_the_smt_service_to_the_sweep_scan_and_the_feed(monkeypatch):
    monkeypatch.setattr(main.config, "TWELVE_DATA_API_KEY", "k")
    telegram, finnhub = main.build_services()
    smt, sweep = telegram.smt_divergence_service, telegram.daily_sweep_service
    assert smt is not None and finnhub.smt_divergence_service is smt
    assert sweep.candle_listeners == [smt.on_candles]
    assert [s.pairs for s in smt.services] == [(PAIR_GBP, PAIR_EUR), ("EURJPY", "GBPJPY")]
    for service in smt.services:  # every group is wired the same way
        assert service.sweep_service is sweep and service.api_key == "k"
        assert service.subscribe_symbol == finnhub.subscribe_to_symbol and service.get_last_price == finnhub.get_last_tick
    assert smt.services[0].pacer is smt.services[1].pacer  # one Twelve Data spacing between them


def test_without_the_twelve_data_key_the_feature_is_disabled_with_a_warning(monkeypatch, caplog):
    monkeypatch.setattr(main.config, "TWELVE_DATA_API_KEY", "")
    with caplog.at_level(logging.WARNING):
        telegram, finnhub = main.build_services()
    assert telegram.smt_divergence_service is None and finnhub.smt_divergence_service is None
    assert "SMT divergence feature disabled" in caplog.text


# ── Telegram commands ───────────────────────────────────────────────────────

class Users:
    def __init__(self, approved=True):
        self.approved, self.flags = approved, []

    def is_admin_chat_id(self, chat_id):
        return chat_id == "admin"

    async def find_by_chat_id(self, chat_id):
        return SimpleNamespace(is_approved=self.approved, is_setup=True)

    async def set_divergence_alerts_enabled(self, chat_id, enabled):
        self.flags.append((chat_id, enabled))


class SmtStub:
    def __init__(self):
        self.captures = 0

    async def get_status_message(self):
        return "STATUS TEXT"

    async def capture_now(self):
        self.captures += 1
        return "Levels recorded."


def bot(approved=True, smt=True):
    users = Users(approved)
    service = TelegramBotService(users, None, None, None)
    service.smt_divergence_service = SmtStub() if smt else None
    sent = []

    async def send(chat_id, text):
        sent.append((chat_id, text))

    service.send_message_to_user = send
    return service, users, sent


def command(service, chat_id, text):
    async def body():
        await service._handle_command(chat_id, text)
        if service._background_tasks:
            await asyncio.gather(*service._background_tasks)

    asyncio.run(body())


def test_divalerts_toggles_the_users_flag():
    service, users, sent = bot()
    command(service, "42", "/divalerts on")
    command(service, "42", "/divalerts OFF")
    assert users.flags == [("42", True), ("42", False)]
    assert [m for _, m in sent] == ["SMT divergence alerts enabled ✅.", "SMT divergence alerts disabled ❌."]


def test_divalerts_usage_and_approval_gating():
    service, users, sent = bot()
    command(service, "42", "/divalerts")
    command(service, "42", "/divalerts maybe")
    assert users.flags == [] and all("Usage: <code>/divalerts on</code>" in m for _, m in sent)
    service, users, sent = bot(approved=False)
    command(service, "42", "/divalerts on")
    assert users.flags == [] and "Access Required" in sent[0][1]  # approved users only, like /sweepalerts


def test_divergence_and_smt_show_the_status():
    service, _, sent = bot()
    command(service, "42", "/divergence")
    command(service, "42", "/smt")
    assert [m for _, m in sent] == ["STATUS TEXT", "STATUS TEXT"]


def test_commands_say_so_when_the_feature_is_not_configured():
    service, users, sent = bot(smt=False)
    for text in ("/divalerts on", "/divergence", "/smt"):
        command(service, "42", text)
    assert users.flags == [] and all("not configured" in m for _, m in sent) and len(sent) == 3


def test_divscan_is_admin_only_and_runs_in_the_background():
    service, _, sent = bot()
    command(service, "admin", "/divscan")
    assert service.smt_divergence_service.captures == 1
    assert "Recording the GBPUSD / EURUSD and EURJPY / GBPJPY levels" in sent[0][1] and sent[-1][1] == "✅ Levels recorded."
    service2, _, sent2 = bot()
    command(service2, "42", "/divscan")  # a normal user: ignored
    assert service2.smt_divergence_service.captures == 0 and sent2 == []


def test_divscan_reports_a_failure(caplog):
    service, _, sent = bot()

    async def broken():
        raise RuntimeError("no data")

    service.smt_divergence_service.capture_now = broken
    command(service, "admin", "/divscan")
    assert "Level capture failed: no data" in sent[-1][1]


def test_help_lists_the_new_commands():
    service, _, sent = bot()
    command(service, "42", "/help")
    text = sent[0][1]
    assert "SMT Divergence" in text and "/divalerts [on|off]" in text and "/divergence</code> (or <code>/smt" in text
    assert "/divscan" not in text  # admin only
    service, _, sent = bot()
    command(service, "admin", "/help")
    assert "/divscan" in sent[0][1]
