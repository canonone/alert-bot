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
import app.services.smt_divergence_service as smt_module
import app.services.users_service as users_module
from app.models import PriceAlert, SmtDivergenceLevel, User
from app.services.daily_candles import trading_day_bounds
from app.services.users_service import UsersService

from test_daily_candles import D, utc

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

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
        for module in (db_module, users_module, smt_module):
            mp.setattr(module, "SessionLocal", Session)
        try:
            await db_module.init_db()  # the real startup path: create_all + the idempotent ALTERs
            yield engine, Session
        finally:
            await engine.dispose()
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


def run(coro_fn):
    asyncio.run(coro_fn())


# ── Upgrading a deployment that still has the removed features' schema ───────

def test_a_database_from_before_the_removal_still_works_and_keeps_its_data():
    async def body():
        async with pg() as (engine, Session):
            async with engine.begin() as conn:  # what an existing deployment still has
                for column in ("daily_sweep_alerts_enabled", "daily_continuation_alerts_enabled"):
                    await conn.execute(text(f"ALTER TABLE users ADD COLUMN {column} BOOLEAN NOT NULL DEFAULT false"))
                await conn.execute(text("CREATE TABLE daily_sweep_setups (id SERIAL PRIMARY KEY, pair VARCHAR NOT NULL)"))
                await conn.execute(text("INSERT INTO daily_sweep_setups (pair) VALUES ('EURUSD')"))
            await db_module.init_db()
            await db_module.init_db()  # idempotent
            async with Session() as session:  # a new sign-up: the old NOT NULL columns fill themselves
                session.add(User(chat_id="new", is_approved=True))
                await session.commit()
                session.add(PriceAlert(chat_id="new", symbol="EURUSD", type="SL", target_price=1.05))
                await session.commit()
            async with engine.begin() as conn:  # nothing was dropped or changed
                assert (await conn.execute(text("SELECT count(*) FROM daily_sweep_setups"))).scalar_one() == 1
                row = (await conn.execute(text(
                    "SELECT daily_sweep_alerts_enabled, daily_continuation_alerts_enabled FROM users"))).one()
                assert tuple(row) == (False, False)

    run(body)


def test_a_fresh_database_gets_only_the_remaining_tables():
    async def body():
        async with pg() as (engine, Session):
            async with engine.connect() as conn:
                result = await conn.execute(text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"))
                assert {r[0] for r in result} == {
                    "users", "price_alerts", "invite_codes", "smt_divergence_levels", "smt_jpy_divergence_levels"}

    run(body)


# ── SMT divergence: the table, its unique constraint and the users column ───

def _smt_row(day=D(2026, 10, 27), side="LOW", **overrides):
    values = dict(trading_day=day, side=side, gbpusd_level=1.25, eurusd_level=1.05, status="ACTIVE",
                  expires_at=utc(2026, 10, 28, 21))
    values.update(overrides)
    return SmtDivergenceLevel(**values)


def test_smt_table_unique_constraint_and_defaults():
    async def body():
        async with pg() as (engine, Session):
            async with Session() as session:
                session.add_all([_smt_row(side="LOW"), _smt_row(side="HIGH"), _smt_row(day=D(2026, 10, 28), side="LOW")])
                await session.commit()
            async with Session() as session:
                session.add(_smt_row(side="LOW"))  # same (trading_day, side)
                with pytest.raises(IntegrityError) as excinfo:
                    await session.commit()
            assert "uq_smt_day_side" in str(excinfo.value.orig)
            async with Session() as session:
                rows = (await session.execute(select(SmtDivergenceLevel).order_by(SmtDivergenceLevel.id))).scalars().all()
            assert len(rows) == 3
            first = rows[0]
            assert (first.status, first.detected_late, first.leader_pair, first.deadline_at) == ("ACTIVE", False, None, None)
            assert first.created_at is not None
            # the longest status fits the column
            async with engine.begin() as conn:
                await conn.execute(text("UPDATE smt_divergence_levels SET status = 'LEADER_TOUCHED' WHERE id = :i"), {"i": first.id})

    run(body)


def test_divergence_flag_is_added_to_an_existing_users_table_and_toggles():
    async def body():
        async with pg() as (engine, Session):
            async with engine.begin() as conn:  # an existing deployment's users table, before the flag existed
                await conn.execute(text("ALTER TABLE users DROP COLUMN divergence_alerts_enabled"))
            await db_module.init_db()
            await db_module.init_db()  # idempotent
            users = UsersService()
            async with Session() as session:
                session.add_all([User(chat_id="a", is_approved=True), User(chat_id="b", is_approved=True),
                                 User(chat_id="c", is_approved=False)])
                await session.commit()

            async def ids():
                return sorted(u.chat_id for u in await users.find_divergence_subscribers())

            assert await ids() == []  # default false for the existing rows
            await users.set_divergence_alerts_enabled("a", True)
            await users.set_divergence_alerts_enabled("c", True)  # not approved: never a subscriber
            assert await ids() == ["a"]
            await users.set_divergence_alerts_enabled("b", True)
            await users.set_divergence_alerts_enabled("a", False)
            assert await ids() == ["b"]

    run(body)


# ── SMT divergence: the real SQL claims, restarts and races ─────────────────

from test_daily_candles import day_hours  # noqa: E402
from test_smt_divergence import CLOSE as SMT_CLOSE, LEVEL_DAY as SMT_DAY, LEVELS as SMT_LEVELS, PIP as SMT_PIP, UNTIL as SMT_UNTIL  # noqa: E402
from app.services.smt_divergence_service import SmtDivergenceService  # noqa: E402

SMT_NOW = SMT_CLOSE + datetime.timedelta(minutes=12)
SMT_T = SMT_CLOSE + datetime.timedelta(hours=2)  # the leader's touch
MINUTE = datetime.timedelta(minutes=1)


def _smt_candles():
    return {"GBPUSD": day_hours(SMT_DAY, 1.2700, 1.2780, 1.2650, 1.2720),
            "EURUSD": day_hours(SMT_DAY, 1.0780, 1.0850, 1.0720, 1.0800)}


def _smt_service(now=SMT_NOW):
    users = UsersService()
    sent: list[tuple[str, str]] = []

    async def send(chat_id, message):
        sent.append((chat_id, message))

    svc = SmtDivergenceService("unused-key", None, users, send)  # market_data=None: nothing can hit Twelve Data
    clock = {"now": now}
    svc.clock = lambda: clock["now"]
    return svc, users, sent, clock


async def _subscriber(Session, users):
    async with Session() as session:
        session.add(User(chat_id="s", is_approved=True))
        await session.commit()
    await users.set_divergence_alerts_enabled("s", True)


async def _smt_rows(Session):
    async with Session() as session:
        rows = (await session.execute(select(SmtDivergenceLevel).order_by(SmtDivergenceLevel.side))).scalars().all()
    return {row.side: row for row in rows}


def _tick_price(pair, side, pips_beyond):
    level = SMT_LEVELS[side][pair]
    return level - pips_beyond * SMT_PIP if side == "LOW" else level + pips_beyond * SMT_PIP


def test_smt_claims_are_atomic_and_their_time_conditions_hold_at_the_boundary():
    async def body():
        async with pg() as (engine, Session):
            svc, users, sent, clock = _smt_service()
            assert await svc.capture(_smt_candles(), SMT_DAY) is True
            assert await svc.capture(_smt_candles(), SMT_DAY) is True  # a rescan: nothing new
            rows = await _smt_rows(Session)
            assert sorted(rows) == ["HIGH", "LOW"] and (rows["LOW"].status, rows["HIGH"].status) == ("ACTIVE", "ACTIVE")
            assert (rows["LOW"].gbpusd_level, rows["LOW"].eurusd_level) == (1.2650, 1.0720)
            assert (rows["HIGH"].gbpusd_level, rows["HIGH"].eurusd_level) == (1.2780, 1.0850)
            assert rows["LOW"].trading_day == SMT_DAY and rows["LOW"].expires_at == SMT_UNTIL
            assert await svc._count_rows(SMT_DAY) == 2 and await svc._count_rows(D(2026, 1, 1)) == 0
            low = rows["LOW"].id

            # a wrong expected status claims nothing
            assert await svc._claim(low, "LEADER_TOUCHED", "CONFIRMED", {}) is False
            deadline = SMT_T + 60 * MINUTE
            leader = dict(leader_pair="GBPUSD", leader_touched_at=SMT_T, leader_price=1.2649, leader_touch_basis="tick", deadline_at=deadline)
            assert await svc._claim(low, "ACTIVE", "LEADER_TOUCHED", leader) is True
            assert await svc._claim(low, "ACTIVE", "LEADER_TOUCHED", leader) is False  # once
            row = (await _smt_rows(Session))["LOW"]
            assert (row.status, row.leader_pair, row.leader_touch_basis, row.deadline_at) == ("LEADER_TOUCHED", "GBPUSD", "tick", deadline)

            # deadline_gt is strict (a laggard touch AT the deadline is too late), deadline_le inclusive
            second = datetime.timedelta(seconds=1)
            assert await svc._claim(low, "LEADER_TOUCHED", "CONFIRMED", {}, deadline_gt=deadline) is False
            assert await svc._claim(low, "LEADER_TOUCHED", "DIVERGENCE", {}, deadline_le=deadline - second) is False
            assert await svc._claim(low, "LEADER_TOUCHED", "CONFIRMED", {}, deadline_gt=deadline - second) is True
            assert (await _smt_rows(Session))["LOW"].status == "CONFIRMED"
            assert await svc._claim(low, "LEADER_TOUCHED", "DIVERGENCE", {}, deadline_le=deadline) is False  # already resolved

            # inclusive at the deadline for the divergence claim; expires_le only once the day is over
            high = rows["HIGH"].id
            assert await svc._claim(high, "ACTIVE", "LEADER_TOUCHED", leader) is True
            assert await svc._claim(high, "LEADER_TOUCHED", "DIVERGENCE", dict(resolved_at=deadline, laggard_best_price=1.0725), deadline_le=deadline) is True
            row = (await _smt_rows(Session))["HIGH"]
            assert (row.status, row.resolved_at, row.laggard_best_price) == ("DIVERGENCE", deadline, 1.0725)

    run(body)


def test_smt_flush_expire_select_and_load_helpers():
    async def body():
        async with pg() as (engine, Session):
            svc, users, sent, clock = _smt_service()
            await svc.capture(_smt_candles(), SMT_DAY)
            rows = await _smt_rows(Session)
            # the closest approach is only written while the window is open
            assert await svc._flush_best(rows["LOW"].id, 1.0724) is False  # still ACTIVE
            await svc._claim(rows["LOW"].id, "ACTIVE", "LEADER_TOUCHED", dict(leader_pair="GBPUSD", leader_touched_at=SMT_T, deadline_at=SMT_T + 60 * MINUTE))
            assert await svc._flush_best(rows["LOW"].id, 1.0724) is True
            assert (await _smt_rows(Session))["LOW"].laggard_best_price == 1.0724

            open_rows = await svc._load_open_rows(SMT_NOW)
            assert sorted((r.side, r.status) for r in open_rows) == [("HIGH", "ACTIVE"), ("LOW", "LEADER_TOUCHED")]
            assert [r for r in open_rows if r.side == "LOW"][0].laggard_best == 1.0724  # restored after a restart
            assert await svc._load_open_rows(SMT_UNTIL) == []  # nothing is open once the period is over

            current = await svc._select_current(SMT_NOW)
            assert sorted(r.side for r in current) == ["HIGH", "LOW"]
            assert await svc._select_current(SMT_UNTIL) == []

            # only ACTIVE rows expire (silently); the one with a leader is the timer's to resolve
            assert await svc._expire_in_db(SMT_UNTIL - MINUTE) == []
            assert await svc._expire_in_db(SMT_UNTIL) == [rows["HIGH"].id]
            assert (await _smt_rows(Session))["HIGH"].status == "EXPIRED" and (await _smt_rows(Session))["LOW"].status == "LEADER_TOUCHED"
            assert await svc._expire_in_db(SMT_UNTIL) == []  # idempotent
            assert sent == []

    run(body)


def test_smt_full_lifecycle_on_real_rows_including_a_restart():
    async def body():
        async with pg() as (engine, Session):
            svc, users, sent, clock = _smt_service()
            await _subscriber(Session, users)
            await svc.capture(_smt_candles(), SMT_DAY)

            clock["now"] = SMT_T
            await svc.check_tick("GBPUSD", _tick_price("GBPUSD", "LOW", 1), None)  # the leader touches its PDL
            for minutes, pips_short in ((10, 6), (20, 2.5), (30, 5)):  # EURUSD wanders, closest 2.5 pips short
                clock["now"] = SMT_T + minutes * MINUTE
                await svc.check_tick("EURUSD", _tick_price("EURUSD", "LOW", -pips_short), None)
            clock["now"] = SMT_T + 40 * MINUTE
            await svc.check_deadlines()  # persists the closest approach
            row = (await _smt_rows(Session))["LOW"]
            assert (row.status, row.leader_pair, row.leader_touch_basis, row.deadline_at) == ("LEADER_TOUCHED", "GBPUSD", "tick", SMT_T + 60 * MINUTE)
            assert row.laggard_best_price == pytest.approx(1.0720 + 2.5 * SMT_PIP)

            # restart: a new instance resumes from the rows
            revived, _, sent2, clock2 = _smt_service(now=SMT_T + 50 * MINUTE)
            await revived.bootstrap()
            assert revived.active_symbols() == {"GBPUSD", "EURUSD"}
            clock2["now"] = SMT_T + 61 * MINUTE
            await revived.check_deadlines()
            assert len(sent2) == 1 and "SMT DIVERGENCE — PDL" in sent2[0][1]
            assert "closest approach 2.5 pips from the level" in sent2[0][1]
            row = (await _smt_rows(Session))["LOW"]
            assert (row.status, row.laggard_best_price, row.resolved_at) == ("DIVERGENCE", pytest.approx(1.0720 + 2.5 * SMT_PIP), SMT_T + 61 * MINUTE)
            await revived.check_deadlines()
            await svc.check_deadlines()  # the OLD instance still has the row cached: its claim loses, it sends nothing
            assert len(sent2) == 1 and sent == []
            assert (await _smt_rows(Session))["HIGH"].status == "ACTIVE"  # the other side was never touched

    run(body)


def test_smt_late_touch_on_a_tick_before_the_timer_is_worded_as_such():
    async def body():
        async with pg() as (engine, Session):
            svc, users, sent, clock = _smt_service()
            await _subscriber(Session, users)
            await svc.capture(_smt_candles(), SMT_DAY)
            clock["now"] = SMT_T
            await svc.check_tick("EURUSD", _tick_price("EURUSD", "HIGH", 1), None)  # EURUSD leads the HIGH case
            clock["now"] = SMT_T + 67 * MINUTE  # 7 minutes after its window closed; the timer hasn't run
            await svc.check_tick("GBPUSD", _tick_price("GBPUSD", "HIGH", 1), None)
            row = (await _smt_rows(Session))["HIGH"]
            assert row.status == "DIVERGENCE" and row.laggard_touched_at == SMT_T + 67 * MINUTE
            assert len(sent) == 1 and "touched 7 min after the window closed" in sent[0][1] and "did not touch" not in sent[0][1]
            await svc.check_deadlines()
            assert len(sent) == 1

    run(body)


def test_smt_arming_late_stores_the_replayed_state_and_never_announces_twice():
    async def body():
        async with pg() as (engine, Session):
            svc, users, sent, clock = _smt_service(now=SMT_CLOSE + datetime.timedelta(hours=3))
            await _subscriber(Session, users)
            from test_smt_divergence import bar, hour

            candles = _smt_candles()
            candles["GBPUSD"] += [bar("LOW", "GBPUSD", hour(0), 2)]  # GBPUSD took its PDL in the first hour
            candles["EURUSD"] += [bar("LOW", "EURUSD", hour(0), -6), bar("LOW", "EURUSD", hour(1), -4)]
            await svc.capture(candles, SMT_DAY)
            row = (await _smt_rows(Session))["LOW"]
            assert (row.status, row.detected_late, row.leader_touch_basis) == ("DIVERGENCE", True, "hour")
            assert len(sent) == 1 and "⚡ Detected late" in sent[0][1]
            await svc.capture(candles, SMT_DAY)  # the catch-up / a rescan hits the unique constraint
            assert len(sent) == 1 and len(await _smt_rows(Session)) == 2
            fresh, _, _, _ = _smt_service(now=SMT_CLOSE + datetime.timedelta(hours=3))
            await fresh.bootstrap()
            assert {r.side for r in fresh._rows.values()} == {"HIGH"}  # the resolved side isn't watched again

    run(body)


def test_smt_two_instances_cannot_both_announce_one_deadline():
    async def body():
        async with pg() as (engine, Session):
            a, users, sent_a, clock_a = _smt_service()
            await _subscriber(Session, users)
            await a.capture(_smt_candles(), SMT_DAY)
            clock_a["now"] = SMT_T
            await a.check_tick("GBPUSD", _tick_price("GBPUSD", "LOW", 1), None)
            b, _, sent_b, clock_b = _smt_service()
            await b.bootstrap()
            clock_a["now"] = clock_b["now"] = SMT_T + 61 * MINUTE
            await asyncio.gather(a.check_deadlines(), b.check_deadlines())
            assert len(sent_a) + len(sent_b) == 1
            assert (await _smt_rows(Session))["LOW"].status == "DIVERGENCE"

    run(body)


def test_smt_tick_versus_timer_race_has_one_outcome_on_real_rows():
    async def body():
        async with pg() as (engine, Session):
            # a fresh level day each round, so each round is a new row to fight over
            for round_no in range(4):
                day = SMT_DAY + datetime.timedelta(days=7 * round_no)  # Tuesdays
                close = trading_day_bounds(day)[1]  # (rounds 1+ are after the US clocks change: 22:00 UTC, not 21:00)
                svc, users, sent_tick, clock_tick = _smt_service(now=close + 12 * MINUTE)
                if round_no == 0:
                    await _subscriber(Session, users)
                candles = {"GBPUSD": day_hours(day, 1.2700, 1.2780, 1.2650, 1.2720), "EURUSD": day_hours(day, 1.0780, 1.0850, 1.0720, 1.0800)}
                await svc.capture(candles, day)
                touch = close + datetime.timedelta(hours=2)
                clock_tick["now"] = touch
                await svc.check_tick("GBPUSD", _tick_price("GBPUSD", "LOW", 1), None)
                timer_svc, _, sent_timer, clock_timer = _smt_service(now=touch)
                await timer_svc.bootstrap()
                deadline = touch + 60 * MINUTE
                clock_tick["now"] = deadline - datetime.timedelta(seconds=1)  # the laggard touches just BEFORE the deadline
                clock_timer["now"] = deadline + datetime.timedelta(seconds=1)  # the timer fires just AFTER it
                first, second = (
                    (svc.check_tick("EURUSD", _tick_price("EURUSD", "LOW", 1), None), timer_svc.check_deadlines())
                    if round_no % 2 == 0
                    else (timer_svc.check_deadlines(), svc.check_tick("EURUSD", _tick_price("EURUSD", "LOW", 1), None))
                )
                await asyncio.gather(first, second)
                async with Session() as session:
                    row = (await session.execute(select(SmtDivergenceLevel).where(SmtDivergenceLevel.trading_day == day, SmtDivergenceLevel.side == "LOW"))).scalar_one()
                assert row.status in ("CONFIRMED", "DIVERGENCE")
                messages = len(sent_tick) + len(sent_timer)
                assert messages == (1 if row.status == "DIVERGENCE" else 0)  # exactly one outcome, and only a divergence speaks

    run(body)


# ── SMT divergence: the second table (EURJPY / GBPJPY) ──────────────────────

from app.models import SmtJpyDivergenceLevel  # noqa: E402
from app.services.smt_divergence_service import SmtDivergenceHub  # noqa: E402


def _jpy_candles():
    return {"EURJPY": day_hours(SMT_DAY, 161.80, 162.50, 161.20, 162.00),
            "GBPJPY": day_hours(SMT_DAY, 189.00, 189.90, 188.40, 189.50)}


def test_jpy_table_is_added_to_a_running_deployment_without_touching_the_usd_one():
    async def body():
        async with pg() as (engine, Session):
            async with engine.begin() as conn:  # a deployment from before the JPY group existed
                await conn.execute(text("DROP TABLE smt_jpy_divergence_levels"))
            async with Session() as session:
                session.add(_smt_row())
                await session.commit()
            await db_module.init_db()
            await db_module.init_db()  # idempotent
            async with Session() as session:
                usd = (await session.execute(select(SmtDivergenceLevel))).scalars().all()
                assert [(r.side, r.gbpusd_level, r.eurusd_level) for r in usd] == [("LOW", 1.25, 1.05)]
                assert (await session.execute(select(SmtJpyDivergenceLevel))).scalars().all() == []

    run(body)


def test_both_groups_claim_their_own_rows_and_resume_after_a_restart():
    async def body():
        async with pg() as (engine, Session):
            users, sent, clock = UsersService(), [], {"now": SMT_NOW}

            async def send(chat_id, message):
                sent.append((chat_id, message))

            def hub():
                h = SmtDivergenceHub("unused-key", None, users, send)
                for service in h.services:
                    service.clock = lambda: clock["now"]
                return h

            first = hub()
            await _subscriber(Session, users)
            usd, jpy = first.services
            assert await usd.capture(_smt_candles(), SMT_DAY) is True
            for _ in range(2):  # the JPY unique constraint is the once-only claim too
                assert await jpy.capture(_jpy_candles(), SMT_DAY) is True
            assert await jpy._count_rows(SMT_DAY) == 2 and await usd._count_rows(SMT_DAY) == 2

            clock["now"] = SMT_T
            await first.check_tick("EURJPY", 161.19, None)  # the JPY leader takes its PDL
            clock["now"] = SMT_T + 10 * MINUTE
            await first.check_tick("GBPJPY", 188.43, None)
            await first.check_deadlines()  # saves the closest approach

            restarted = hub()
            await restarted.bootstrap()
            assert restarted.active_symbols() == {"GBPUSD", "EURUSD", "EURJPY", "GBPJPY"}
            clock["now"] = SMT_T + 61 * MINUTE
            await restarted.check_deadlines()
            await first.check_deadlines()  # the old instance loses the claim
            assert len(sent) == 1 and "SMT DIVERGENCE (JPY) — PDL" in sent[0][1]
            assert "closest approach 3.0 pips" in sent[0][1]
            async with Session() as session:
                rows = (await session.execute(select(SmtJpyDivergenceLevel))).scalars().all()
                states = {r.side: (r.status, r.leader_pair) for r in rows}
                assert states == {"LOW": ("DIVERGENCE", "EURJPY"), "HIGH": ("ACTIVE", None)}
                usd_rows = (await session.execute(select(SmtDivergenceLevel))).scalars().all()
                assert {r.status for r in usd_rows} == {"ACTIVE"}
            assert "EURJPY / GBPJPY" in await restarted.get_status_message()

    run(body)
