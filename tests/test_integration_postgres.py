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
    detect_continuation,
    detect_sweep,
)
from app.services.users_service import UsersService

from test_daily_sweep import D, candle, utc

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

MON_C = (110.349, 110.762, 110.089, 110.515)
TUE_C = (110.515, 111.085, 110.362, 110.966)
MID_DAY3 = utc(2026, 10, 28, 8)  # Wed 08:00 UTC: inside the continuation's Day 3 (Day 2 = Tue 27 Oct)


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


def _sweep_args():
    d1, d2 = candle(D(2026, 10, 27), 100, 110, 90, 105), candle(D(2026, 10, 28), 105, 112, 95, 100)
    signal = detect_sweep(d1, d2)
    return "EURUSD", signal, d1, d2, day3_expiry(d2.date)


def _cont_args(pair="AUDJPY"):
    d1, d2 = candle(D(2026, 10, 26), *MON_C), candle(D(2026, 10, 27), *TUE_C)
    signal = detect_continuation(d1, d2)
    return pair, signal, d1, d2, day3_expiry(d2.date)


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
                cols = {r[0] for r in await conn.execute(text(
                    "select column_name from information_schema.columns where table_schema = current_schema() "
                    "and table_name in ('users','daily_sweep_setups')"))}
            assert {"daily_sweep_alerts_enabled", "daily_continuation_alerts_enabled", "kind", "level2", "stage"} <= cols

        # OLD schema: users without the new flags, daily_sweep_setups without kind/level2/stage, with rows
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
            assert (row.kind, row.stage, row.level2) == ("SWEEP", 0, None)
            assert user.daily_sweep_alerts_enabled is False and user.daily_continuation_alerts_enabled is False

    run(body)


# ── setups: insert, unique constraint, staged claims, expiry ────────────────

def test_insert_unique_constraint_and_staged_claims():
    async def body():
        async with pg() as (engine, Session):
            svc, _, _ = _service()

            sweep = await svc._store_setup(*_sweep_args())
            cont = await svc._store_setup(*_cont_args())
            assert sweep is not None and cont is not None
            sweep_row, cont_row = await _row(Session, sweep.id), await _row(Session, cont.id)
            assert (sweep_row.kind, sweep_row.stage, sweep_row.level2, sweep_row.status) == ("SWEEP", 0, None, ACTIVE)
            assert (cont_row.kind, cont_row.stage, cont_row.status) == ("CONTINUATION", 0, ACTIVE)
            assert (cont_row.level, cont_row.level2) == (110.762, 110.515)
            assert cont_row.day2_date == D(2026, 10, 27) and cont_row.day3_expires_at == utc(2026, 10, 28, 21)
            assert sweep.level2 is None and cont.kind == "CONTINUATION" and cont.level2 == 110.515

            # Unique (pair, day2_date): the service's ON CONFLICT DO NOTHING stores nothing...
            assert await svc._store_setup(*_sweep_args()) is None
            assert await svc._store_setup(*_cont_args()) is None
            # ...even for the other pattern kind on the same Day 2 (a Day 2 can only match one)
            pair, signal, d1, d2, expires = _cont_args()
            assert await svc._store_setup(pair, detect_sweep(*_sweep_args()[2:4]), d1, d2, expires) is None
            # ...and a plain INSERT is rejected by the constraint itself
            async with Session() as session:
                session.add(DailySweepSetup(
                    pair="AUDJPY", kind="CONTINUATION", direction="BUY", level=1, level2=2, stage=0, status=ACTIVE,
                    day1_open=1, day1_high=1, day1_low=1, day1_close=1, day2_open=1, day2_high=1, day2_low=1, day2_close=1,
                    day2_date=D(2026, 10, 27), day3_expires_at=utc(2026, 10, 28, 21)))
                with pytest.raises(IntegrityError) as excinfo:
                    await session.commit()
            assert "uq_daily_sweep_pair_day2" in str(excinfo.value.orig)
            async with Session() as session:
                assert len((await session.execute(select(DailySweepSetup))).scalars().all()) == 2

            # Continuation stage 0 -> 1: still ACTIVE, price/time recorded
            assert await svc._claim_stage(cont, 0, 1, 110.760, MID_DAY3) is True
            row = await _row(Session, cont.id)
            assert (row.stage, row.status, row.trigger_price, row.triggered_at) == (1, ACTIVE, 110.760, MID_DAY3)
            # A repeat claim of the same stage matches nothing
            assert await svc._claim_stage(cont, 0, 1, 110.760, MID_DAY3) is False
            # Stage 1 -> 2 completes it; repeats and skipping back are refused
            assert await svc._claim_stage(cont, 1, 2, 110.510, MID_DAY3) is True
            row = await _row(Session, cont.id)
            assert (row.stage, row.status, row.trigger_price) == (2, TRIGGERED, 110.510)
            assert await svc._claim_stage(cont, 1, 2, 110.510, MID_DAY3) is False
            assert await svc._claim_stage(cont, 0, 2, 110.510, MID_DAY3) is False
            assert (await _row(Session, cont.id)).stage == 2

            # A sweep has one stage: 0 -> 1 completes it
            assert await svc._claim_stage(sweep, 0, 1, 105.0, MID_DAY3) is True
            row = await _row(Session, sweep.id)
            assert (row.stage, row.status) == (1, TRIGGERED)
            assert await svc._claim_stage(sweep, 0, 1, 105.0, MID_DAY3) is False

            # Combined 0 -> 2 straight from stage 0
            combined = await svc._store_setup(*_cont_args("GBPJPY"))
            assert await svc._claim_stage(combined, 0, 2, 110.0, MID_DAY3) is True
            assert (await _row(Session, combined.id)).status == TRIGGERED

            # A claim at/after day3_expires_at returns nothing and changes nothing
            late = await svc._store_setup(*_cont_args("NZDJPY"))
            expiry = late.day3_expires_at
            assert await svc._claim_stage(late, 0, 1, 110.7, expiry) is False  # exactly at expiry: not "> now"
            assert await svc._claim_stage(late, 0, 1, 110.7, expiry + datetime.timedelta(seconds=1)) is False
            row = await _row(Session, late.id)
            assert (row.stage, row.status, row.trigger_price) == (0, ACTIVE, None)
            assert await svc._claim_stage(late, 0, 1, 110.7, expiry - datetime.timedelta(seconds=1)) is True

    run(body)


def test_expire_bootstrap_and_list_active():
    async def body():
        async with pg() as (engine, Session):
            svc, _, sent = _service()
            cont = await svc._store_setup(*_cont_args())  # Day 3 ends Wed 28 Oct 21:00 UTC
            sweep = await svc._store_setup(*_sweep_args())  # Day 3 ends Thu 29 Oct 21:00 UTC
            done = await svc._store_setup(*_cont_args("GBPJPY"))
            await svc._claim_stage(cont, 0, 1, 110.76, MID_DAY3)  # cont is at stage 1 (Level 1 hit)
            await svc._claim_stage(done, 0, 2, 110.40, MID_DAY3)  # complete -> TRIGGERED

            # Restart mid-way: bootstrap reloads ACTIVE rows with kind/level2/stage intact
            await svc.bootstrap()
            cached = {s.pair: s for pair_setups in svc._active.values() for s in pair_setups}
            assert set(cached) == {"AUDJPY", "EURUSD"}
            assert (cached["AUDJPY"].kind, cached["AUDJPY"].stage, cached["AUDJPY"].level2) == ("CONTINUATION", 1, 110.515)
            assert (cached["EURUSD"].kind, cached["EURUSD"].stage, cached["EURUSD"].level2) == ("SWEEP", 0, None)
            assert svc.active_symbols() == {"AUDJPY", "EURUSD"}
            assert {s.pair for s in await svc.list_active()} == {"AUDJPY", "EURUSD"}

            # After the continuation's Day 3 close: expire_setups flips only that one, silently
            svc.clock = lambda: utc(2026, 10, 28, 21, 0, ) + datetime.timedelta(seconds=1)
            assert await svc.expire_setups() == 1
            row = await _row(Session, cont.id)
            assert (row.status, row.stage) == (EXPIRED, 1)  # stage preserved: it expired after Level 1
            assert (await _row(Session, sweep.id)).status == ACTIVE
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


# ── users: opt-in flags ─────────────────────────────────────────────────────

def test_user_opt_in_flags_toggle_independently():
    async def body():
        async with pg() as (engine, Session):
            users = UsersService()
            async with Session() as session:
                session.add_all([
                    User(chat_id="a", is_approved=True), User(chat_id="b", is_approved=True),
                    User(chat_id="c", is_approved=False),  # not approved: never a subscriber
                ])
                await session.commit()

            async def ids(finder):
                return sorted(u.chat_id for u in await finder())

            assert await ids(users.find_daily_sweep_subscribers) == []
            assert await ids(users.find_daily_continuation_subscribers) == []

            await users.set_daily_sweep_alerts_enabled("a", True)
            await users.set_daily_continuation_alerts_enabled("b", True)
            await users.set_daily_sweep_alerts_enabled("c", True)
            await users.set_daily_continuation_alerts_enabled("c", True)
            assert await ids(users.find_daily_sweep_subscribers) == ["a"]
            assert await ids(users.find_daily_continuation_subscribers) == ["b"]
            assert await ids(users.find_digest_subscribers) == ["a", "b"]  # either flag; c is unapproved

            await users.set_daily_continuation_alerts_enabled("a", True)  # a: both
            assert await ids(users.find_daily_continuation_subscribers) == ["a", "b"]
            await users.set_daily_sweep_alerts_enabled("a", False)  # turning one off leaves the other on
            assert await ids(users.find_daily_sweep_subscribers) == []
            assert await ids(users.find_daily_continuation_subscribers) == ["a", "b"]
            await users.set_daily_continuation_alerts_enabled("a", False)
            await users.set_daily_continuation_alerts_enabled("b", False)
            assert await ids(users.find_digest_subscribers) == []

    run(body)


# ── end to end on real rows: ticks -> claims -> messages ────────────────────

def test_ticks_end_to_end_with_real_claims_and_two_instances():
    async def body():
        async with pg() as (engine, Session):
            svc, users, sent = _service()
            async with Session() as session:
                session.add_all([User(chat_id="cont", is_approved=True), User(chat_id="sweep", is_approved=True)])
                await session.commit()
            await users.set_daily_continuation_alerts_enabled("cont", True)
            await users.set_daily_sweep_alerts_enabled("sweep", True)

            setup = await svc._store_setup(*_cont_args())
            await svc.bootstrap()

            for price in (110.900, 110.760, 110.760, 110.700, 110.510, 110.510, 110.400):
                await svc.check_tick("AUDJPY", price, None)
            assert [chat for chat, _ in sent] == ["cont", "cont"]  # only continuation subscribers, two stages
            assert "LEVEL 1 HIT" in sent[0][1] and "LEVEL 2 HIT" in sent[1][1]
            row = await _row(Session, setup.id)
            assert (row.stage, row.status) == (2, TRIGGERED)

            # Two instances that both loaded the setup at stage 0 race on the same tick: one winner
            sent.clear()
            other = await svc._store_setup(*_cont_args("GBPJPY"))
            a, _, sent_a = _service()
            b, _, sent_b = _service()
            await a.bootstrap()
            await b.bootstrap()
            await asyncio.gather(a.check_tick("GBPJPY", 110.700, None), b.check_tick("GBPJPY", 110.700, None))
            assert len(sent_a) + len(sent_b) == 1
            assert (await _row(Session, other.id)).stage == 1

    run(body)
