"""Runs the real SQL against a real Postgres. Skipped unless TEST_DATABASE_URL is set, e.g.

    TEST_DATABASE_URL=postgresql+asyncpg://user:pass@127.0.0.1:5432/some_db pytest tests/test_integration_postgres.py

Everything happens in a throwaway schema (created and dropped per test, via search_path), so
existing data in that database is never touched. No Twelve Data / Finnhub / Telegram calls are made:
the market-data client is None and outgoing messages go to an in-memory list.
"""
import asyncio
import contextlib
import datetime
import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.db as db_module
import app.services.daily_sweep_service as sm
import app.services.users_service as users_module
from app.models import DailySweepScanState, DailySweepSetup, User
from app.services.daily_sweep_service import (
    ACTIVE,
    EXPIRED,
    TRIGGERED,
    DailySweepService,
    day3_expiry,
    detect_sweep,
    previous_trading_day,
)
from app.services.users_service import UsersService

from test_daily_sweep import D, candle, utc

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

MID_DAY3 = utc(2026, 10, 28, 8)  # Wed 08:00 UTC


@contextlib.asynccontextmanager
async def pg():
    """Throwaway schema + app.db / service modules pointed at it; init_db has already run."""
    schema = f"itest_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        TEST_DATABASE_URL, poolclass=NullPool, connect_args={"server_settings": {"search_path": schema}}
    )
    Session = async_sessionmaker(engine, expire_on_commit=False)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(db_module, "engine", engine)
        for module in (db_module, sm, users_module):
            mp.setattr(module, "SessionLocal", Session)
        try:
            await db_module.init_db()  # the real startup path: create_all + the idempotent ALTERs
            yield engine, Session
        finally:
            await engine.dispose()
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


def _service():
    users = UsersService()
    sent: list[tuple[str, str]] = []

    async def send(chat_id, message):
        sent.append((chat_id, message))

    svc = DailySweepService("unused-key", None, users, send)  # market_data=None: nothing can hit Twelve Data
    svc.clock = lambda: MID_DAY3
    return svc, users, sent


def _sweep_args(pair="EURUSD", day2=D(2026, 10, 28)):
    """A SELL sweep (Day 1 bullish, Day 2 bearish sweeping the high) whose Day 2 is `day2`."""
    d1 = candle(previous_trading_day(day2), 100, 110, 90, 105)
    d2 = candle(day2, 105, 112, 95, 100)
    return pair, detect_sweep(d1, d2), d1, d2, day3_expiry(day2)


async def _row(Session, setup_id) -> DailySweepSetup:
    async with Session() as session:
        return (await session.execute(select(DailySweepSetup).where(DailySweepSetup.id == setup_id))).scalar_one()


def run(coro_fn):
    asyncio.run(coro_fn())


# ── init_db ─────────────────────────────────────────────────────────────────

def test_init_db_idempotent_on_empty_and_old_schema():
    async def body():
        async with pg() as (engine, Session):
            await db_module.init_db()  # already ran once in pg(); this is run #2 on an empty database
            async with engine.connect() as conn:
                for table, model in (("users", User), ("daily_sweep_setups", DailySweepSetup)):
                    cols = {r[0] for r in await conn.execute(text(
                        "select column_name from information_schema.columns "
                        f"where table_schema = current_schema() and table_name = '{table}'"))}
                    assert cols == {c.name for c in model.__table__.columns}  # exactly the model's columns, none stray

        # OLD schema: users without the new flag, daily_sweep_setups with rows
        async with pg() as (engine, Session):
            async with engine.begin() as conn:
                for table in ("daily_sweep_setups", "users", "daily_sweep_scan_state", "price_alerts", "invite_codes"):
                    await conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
                await conn.execute(text(
                    "CREATE TABLE users (chat_id VARCHAR PRIMARY KEY, first_name VARCHAR, username VARCHAR, "
                    "twelve_data_api_key VARCHAR, is_approved BOOLEAN, is_admin BOOLEAN, is_setup BOOLEAN, "
                    "alert_id_counter INTEGER, zone_alerts_enabled BOOLEAN, "
                    "created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now())"))
                await conn.execute(text(
                    "INSERT INTO users (chat_id, is_approved, is_admin, is_setup, alert_id_counter, zone_alerts_enabled) "
                    "VALUES ('1', true, true, true, 0, true)"))
                await conn.execute(text(
                    "CREATE TABLE daily_sweep_setups (id SERIAL PRIMARY KEY, pair VARCHAR NOT NULL, "
                    "direction VARCHAR(10) NOT NULL, day1_open NUMERIC(18,6) NOT NULL, day1_high NUMERIC(18,6) NOT NULL, "
                    "day1_low NUMERIC(18,6) NOT NULL, day1_close NUMERIC(18,6) NOT NULL, day2_open NUMERIC(18,6) NOT NULL, "
                    "day2_high NUMERIC(18,6) NOT NULL, day2_low NUMERIC(18,6) NOT NULL, day2_close NUMERIC(18,6) NOT NULL, "
                    "level NUMERIC(18,6) NOT NULL, day2_date DATE NOT NULL, day3_expires_at TIMESTAMPTZ NOT NULL, "
                    "status VARCHAR(10) NOT NULL, triggered_at TIMESTAMPTZ, trigger_price NUMERIC(18,6), "
                    "created_at TIMESTAMPTZ DEFAULT now() NOT NULL, CONSTRAINT uq_daily_sweep_pair_day2 UNIQUE (pair, day2_date))"))
                await conn.execute(text(
                    "INSERT INTO daily_sweep_setups (pair,direction,day1_open,day1_high,day1_low,day1_close,day2_open,"
                    "day2_high,day2_low,day2_close,level,day2_date,day3_expires_at,status) VALUES "
                    "('XAUUSD','SELL',1,2,0,1,1,2,0,1,1,'2026-10-27','2026-10-28 21:00+00','ACTIVE')"))
            await db_module.init_db()
            await db_module.init_db()
            async with Session() as session:
                row = (await session.execute(select(DailySweepSetup))).scalar_one()
                user = await session.get(User, "1")
            assert (row.pair, row.status, row.level) == ("XAUUSD", "ACTIVE", 1)
            assert user.daily_sweep_alerts_enabled is False

    run(body)


# ── setups: insert, unique constraint, atomic claim, expiry ─────────────────

def test_insert_unique_constraint_and_atomic_claim():
    async def body():
        async with pg() as (engine, Session):
            svc, _, _ = _service()

            sweep = await svc._store_setup(*_sweep_args())
            assert sweep is not None
            row = await _row(Session, sweep.id)
            assert (row.pair, row.direction, row.status, row.level) == ("EURUSD", "SELL", ACTIVE, 105)
            assert row.day2_date == D(2026, 10, 28) and row.day3_expires_at == utc(2026, 10, 29, 21)
            assert (row.triggered_at, row.trigger_price) == (None, None)

            # Unique (pair, day2_date): the service's ON CONFLICT DO NOTHING stores nothing for a rescan...
            assert await svc._store_setup(*_sweep_args()) is None
            # ...and a plain INSERT is rejected by the constraint itself
            async with Session() as session:
                session.add(DailySweepSetup(
                    pair="EURUSD", direction="SELL", level=1, status=ACTIVE,
                    day1_open=1, day1_high=1, day1_low=1, day1_close=1, day2_open=1, day2_high=1, day2_low=1, day2_close=1,
                    day2_date=D(2026, 10, 28), day3_expires_at=utc(2026, 10, 29, 21)))
                with pytest.raises(IntegrityError) as excinfo:
                    await session.commit()
            assert "uq_daily_sweep_pair_day2" in str(excinfo.value.orig)
            # A different pair, or the same pair on another Day 2, is fine
            assert await svc._store_setup(*_sweep_args("GBPUSD")) is not None
            assert await svc._store_setup(*_sweep_args("EURUSD", D(2026, 10, 27))) is not None

            # ACTIVE -> TRIGGERED exactly once, recording price and time
            assert await svc._claim(sweep, 105.5, MID_DAY3) is True
            row = await _row(Session, sweep.id)
            assert (row.status, row.trigger_price, row.triggered_at) == (TRIGGERED, 105.5, MID_DAY3)
            assert await svc._claim(sweep, 105.5, MID_DAY3) is False  # a repeat claim matches nothing
            assert (await _row(Session, sweep.id)).status == TRIGGERED

            # A claim at/after day3_expires_at returns nothing and changes nothing
            late = await svc._store_setup(*_sweep_args("NZDUSD"))
            expiry = late.day3_expires_at
            assert await svc._claim(late, 105.5, expiry) is False  # exactly at expiry: not "> now"
            assert await svc._claim(late, 105.5, expiry + datetime.timedelta(seconds=1)) is False
            row = await _row(Session, late.id)
            assert (row.status, row.trigger_price, row.triggered_at) == (ACTIVE, None, None)
            assert await svc._claim(late, 105.5, expiry - datetime.timedelta(seconds=1)) is True

    run(body)


def test_expire_bootstrap_and_list_active():
    async def body():
        async with pg() as (engine, Session):
            svc, _, sent = _service()
            first = await svc._store_setup(*_sweep_args("AUDUSD", D(2026, 10, 27)))  # Day 3 ends Wed 28 Oct 21:00 UTC
            second = await svc._store_setup(*_sweep_args("EURUSD", D(2026, 10, 28)))  # Day 3 ends Thu 29 Oct 21:00 UTC
            done = await svc._store_setup(*_sweep_args("GBPUSD", D(2026, 10, 28)))
            await svc._claim(done, 105.5, MID_DAY3)  # already TRIGGERED

            # Restart: bootstrap reloads only the ACTIVE rows
            await svc.bootstrap()
            assert svc.active_symbols() == {"AUDUSD", "EURUSD"}
            assert {s.pair for s in await svc.list_active()} == {"AUDUSD", "EURUSD"}

            # After the first setup's Day 3 close: expire_setups flips only that one, silently
            svc.clock = lambda: utc(2026, 10, 28, 21, 0) + datetime.timedelta(seconds=1)
            assert await svc.expire_setups() == 1
            assert (await _row(Session, first.id)).status == EXPIRED
            assert (await _row(Session, second.id)).status == ACTIVE
            assert (await _row(Session, done.id)).status == TRIGGERED  # completed rows are never rewritten
            assert svc.active_symbols() == {"EURUSD"}
            assert [s.pair for s in await svc.list_active()] == ["EURUSD"]
            assert sent == []  # expiry never messages anyone
            assert await svc.expire_setups() == 0  # idempotent

    run(body)


# ── scan state ──────────────────────────────────────────────────────────────

def test_scan_state_row_read_and_update():
    async def body():
        async with pg() as (engine, Session):
            svc, _, _ = _service()
            assert await svc.get_last_scanned_day() is None
            assert await svc.needs_catch_up() is True  # never scanned, Day 3 still running at MID_DAY3

            await svc._record_scan(D(2026, 10, 26))
            assert await svc.get_last_scanned_day() == D(2026, 10, 26)
            await svc._record_scan(D(2026, 10, 27))  # upsert moves forward
            assert await svc.get_last_scanned_day() == D(2026, 10, 27)
            await svc._record_scan(D(2026, 10, 26))  # never moves backwards
            assert await svc.get_last_scanned_day() == D(2026, 10, 27)
            await svc._record_scan(D(2026, 10, 27))  # same day again: a no-op, not an error
            assert await svc.get_last_scanned_day() == D(2026, 10, 27)
            async with Session() as session:
                rows = (await session.execute(select(DailySweepScanState))).scalars().all()
            assert len(rows) == 1 and rows[0].id == 1  # singleton row
            assert await svc.needs_catch_up() is False  # Tue 27 is the latest closed day at MID_DAY3 and it's scanned

    run(body)


# ── users: opt-in flag ──────────────────────────────────────────────────────

def test_user_opt_in_flag_toggles():
    async def body():
        async with pg() as (engine, Session):
            users = UsersService()
            async with Session() as session:
                session.add_all([
                    User(chat_id="a", is_approved=True), User(chat_id="b", is_approved=True),
                    User(chat_id="c", is_approved=False),  # not approved: never a subscriber
                ])
                await session.commit()

            async def ids():
                return sorted(u.chat_id for u in await users.find_daily_sweep_subscribers())

            assert await ids() == []
            await users.set_daily_sweep_alerts_enabled("a", True)
            await users.set_daily_sweep_alerts_enabled("c", True)
            assert await ids() == ["a"]  # c is unapproved
            await users.set_daily_sweep_alerts_enabled("b", True)
            assert await ids() == ["a", "b"]
            await users.set_daily_sweep_alerts_enabled("a", False)
            assert await ids() == ["b"]

    run(body)


# ── end to end on real rows: ticks -> claim -> message ──────────────────────

def test_ticks_end_to_end_with_real_claims_and_two_instances():
    async def body():
        async with pg() as (engine, Session):
            svc, users, sent = _service()
            async with Session() as session:
                session.add_all([User(chat_id="on", is_approved=True), User(chat_id="off", is_approved=True)])
                await session.commit()
            await users.set_daily_sweep_alerts_enabled("on", True)

            setup = await svc._store_setup(*_sweep_args())
            await svc.bootstrap()

            for price in (104.0, 105.0, 105.0, 106.0):  # SELL: fires when price trades up to 105, once
                await svc.check_tick("EURUSD", price, None)
            assert [chat for chat, _ in sent] == ["on"]  # only /sweepalerts subscribers
            assert "DAILY SWEEP LEVEL HIT — EURUSD (SELL)" in sent[0][1]
            assert (await _row(Session, setup.id)).status == TRIGGERED

            # Two instances that both loaded the setup race on the same tick: one winner
            other = await svc._store_setup(*_sweep_args("GBPUSD"))
            a, _, sent_a = _service()
            b, _, sent_b = _service()
            await a.bootstrap()
            await b.bootstrap()
            await asyncio.gather(a.check_tick("GBPUSD", 105.5, None), b.check_tick("GBPUSD", 105.5, None))
            assert len(sent_a) + len(sent_b) == 1  # one winner sends (to the one subscriber); the loser sends nothing
            assert (await _row(Session, other.id)).status == TRIGGERED

    run(body)
