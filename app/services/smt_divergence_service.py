"""SMT (smart money) divergence between GBPUSD and EURUSD on the daily timeframe.

The two pairs are positively correlated, so they should take out their previous-day liquidity at about the same
time. At each trading-day close (17:00 America/New_York) the previous-day high (PDH) and low (PDL) of both pairs are
recorded from the synthetic daily candles Daily Sweep builds. For the next trading day each SIDE is watched on its
own:

  LOW  case: both pairs should trade to their PDL (price <= PDL)
  HIGH case: both pairs should trade to their PDH (price >= PDH)

The first pair to touch its level is the LEADER. If the other pair (the LAGGARD) hasn't touched its own level within
DIVERGENCE_WAIT_MINUTES (or by the day close, whichever is first) that is a DIVERGENCE: one alert. If it touches in
time the pair is in sync (CONFIRMED) and nothing is sent. Each side fires at most once per trading day.

    ACTIVE ──leader touches──────────────▶ LEADER_TOUCHED ──laggard touches in time──▶ CONFIRMED   (silent)
      │  └─ other pair already past ─▶ CONFIRMED            └─deadline passes──────────▶ DIVERGENCE  (one alert)
      └─ day closes, nobody touched ─▶ EXPIRED (silent)

Every transition is an atomic conditional UPDATE (`... WHERE status = <expected>`), and a message is only sent by
the caller whose claim succeeded, so restarts, duplicate ticks and the tick-vs-timer race can't send twice.

This module has three layers: pure functions (this top part, no I/O), the in-memory row state, and the service.
"""
import asyncio
import datetime
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

import app.services.daily_sweep_service as sweep
from app.db import SessionLocal
from app.models import SmtDivergenceLevel
from app.services.market_data_service import MarketDataService
from app.services.users_service import UsersService

logger = logging.getLogger("SmtDivergenceService")

SendMessage = Callable[[str, str], Awaitable[None]]
SubscribeSymbol = Callable[[str], Awaitable[None]]

UTC = datetime.timezone.utc
HOUR = datetime.timedelta(hours=1)
ZERO = datetime.timedelta(0)

PAIR_GBP, PAIR_EUR = "GBPUSD", "EURUSD"
PAIRS = (PAIR_GBP, PAIR_EUR)

LOW, HIGH = "LOW", "HIGH"
SIDES = (LOW, HIGH)

ACTIVE = "ACTIVE"
LEADER_TOUCHED = "LEADER_TOUCHED"
CONFIRMED = "CONFIRMED"
DIVERGENCE = "DIVERGENCE"
EXPIRED = "EXPIRED"

# How long the laggard has, after the leader touches its level, to touch its own.
DIVERGENCE_WAIT_MINUTES = 60
# "Trades to the level" is ANY touch or break. This is the tolerance (in pips; 0.0001 per pip for these pairs) that
# widens the touch zone toward price: with a positive value, price within that many pips of the level counts as a
# touch. Default 0 = a plain touch. Per-pair override like the margins in daily_sweep_service.
TOUCH_BUFFER_PIPS = 0.0
PAIR_TOUCH_BUFFER_PIPS: dict[str, float] = {}
# A laggard whose closest approach stayed within this many pips of its level is annotated (never suppressed): it may
# be a feed difference between the Twelve Data levels and the Finnhub (OANDA) ticks.
NEAR_MISS_PIPS = 2.0
PAIR_NEAR_MISS_PIPS: dict[str, float] = {}
# The scheduler evaluates pending windows this often, so an alert can be up to this late after the exact deadline.
DEADLINE_CHECK_INTERVAL_SECONDS = 60

# How the leader's touch time is known: "tick" (observed live), "hour" (from a 1H bar: only the hour is known),
# "detected" (price was already beyond the level when we looked; the time wasn't observed)
BASIS_TICK, BASIS_HOUR, BASIS_DETECTED = "tick", "hour", "detected"


def touch_buffer_pips_for(pair: str) -> float:
    return PAIR_TOUCH_BUFFER_PIPS.get(pair, TOUCH_BUFFER_PIPS)


def near_miss_pips_for(pair: str) -> float:
    return PAIR_NEAR_MISS_PIPS.get(pair, NEAR_MISS_PIPS)


def other_pair(pair: str) -> str:
    return PAIR_EUR if pair == PAIR_GBP else PAIR_GBP


def level_name(side: str) -> str:
    return "PDL" if side == LOW else "PDH"


# ── Pure: levels, touches, deadlines ────────────────────────────────────────

def extract_levels(day: sweep.DailyCandle) -> tuple[float, float]:
    """(PDH, PDL) of a completed synthetic daily candle."""
    return day.high, day.low


def monitored_until(level_day: datetime.date) -> datetime.datetime:
    """The levels of `level_day` are watched until the NEXT trading day closes (Friday's levels run through
    Monday's close). DST-safe: built from New York wall-clock times."""
    return sweep.trading_day_bounds(sweep.next_trading_day(level_day))[1]


def touched(side: str, level: float, price: float, pair: str) -> bool:
    """LOW: price <= PDL; HIGH: price >= PDH — any touch or break, widened by the (default 0) buffer."""
    slack = (touch_buffer_pips_for(pair) + sweep._PIP_EPS) * sweep.pip_size(pair)
    return price <= level + slack if side == LOW else price >= level - slack


def pips_from_level(side: str, level: float, price: float, pair: str) -> float:
    """How far `price` still is from touching `level`, in pips (positive = not there yet, <= 0 = touched)."""
    pip = sweep.pip_size(pair)
    return (price - level) / pip if side == LOW else (level - price) / pip


def better_approach(side: str, price: float, best: float | None) -> bool:
    """Is `price` closer to the level than `best` (lower for LOW, higher for HIGH)?"""
    return best is None or (price < best if side == LOW else price > best)


def bar_extreme(side: str, bar: dict) -> float:
    """The bar's extreme toward the level: its low for LOW, its high for HIGH."""
    return bar["low"] if side == LOW else bar["high"]


def window_deadline(
    touched_at: datetime.datetime, day_close: datetime.datetime, wait_minutes: int | None = None
) -> datetime.datetime:
    """min(T + wait, day close): a window never outlives the monitored period."""
    wait = DIVERGENCE_WAIT_MINUTES if wait_minutes is None else wait_minutes
    return min(touched_at + datetime.timedelta(minutes=wait), day_close)


def first_touch_outcome(
    side: str, levels: dict[str, float], symbol: str, price: float, other_price: float | None
) -> str | None:
    """A tick for `symbol` while the row is ACTIVE. None = no touch; LEADER_TOUCHED = it leads; CONFIRMED = the other
    pair is already beyond its own level (they are in sync, there is no divergence to wait for)."""
    if not touched(side, levels[symbol], price, symbol):
        return None
    other = other_pair(symbol)
    if other_price is not None and touched(side, levels[other], other_price, other):
        return CONFIRMED
    return LEADER_TOUCHED


def laggard_touch_outcome(deadline: datetime.datetime, now: datetime.datetime) -> str:
    """The laggard touches its level at `now`: in time (strictly before the deadline) confirms, otherwise the
    window had already closed, so it is still a divergence (the alert says it touched late)."""
    return CONFIRMED if now < deadline else DIVERGENCE


def minutes_between(earlier: datetime.datetime, later: datetime.datetime) -> int:
    return max(0, round((later - earlier).total_seconds() / 60))


# ── Pure: replay the 1H bars since the close (arming late) ──────────────────

@dataclass(frozen=True)
class SideState:
    """Where one side stands after replaying the bars since the day close."""

    status: str
    leader_pair: str | None = None
    leader_touched_at: datetime.datetime | None = None
    leader_price: float | None = None
    leader_touch_basis: str | None = None
    deadline_at: datetime.datetime | None = None
    laggard_touched_at: datetime.datetime | None = None
    laggard_price: float | None = None
    laggard_best_price: float | None = None
    resolved_at: datetime.datetime | None = None
    detected_late: bool = False


def replay_side(
    side: str,
    levels: dict[str, float],
    bars: dict[str, list[dict]],
    now: datetime.datetime,
    day_close: datetime.datetime,
    basis: str = BASIS_HOUR,
) -> SideState:
    """Replays the bars since the day close (oldest first, per pair) so a side armed late ends up where a live
    bot would have left it. A 1H bar only tells us the HOUR a touch happened in, so the window is judged
    conservatively: the leader's touch is taken as late as possible (the end of its hour, or `now` if that hour is
    still running). An alert is therefore never early; it can only be late, and is labelled so.

      neither pair touched                      -> ACTIVE
      both touched in the same / adjacent hour  -> CONFIRMED (can't be ordered with certainty: stay silent)
      laggard touched >= 2 hours after leader   -> DIVERGENCE (certainly outside the window)
      only the leader touched                   -> LEADER_TOUCHED, or DIVERGENCE if its deadline has already passed

    basis "detected" means the "bars" are single price readings taken now (no bars were available): times are `now`.
    """
    bar_len = HOUR if basis == BASIS_HOUR else ZERO
    first: dict[str, tuple[datetime.datetime, float]] = {}
    for pair in PAIRS:
        for bar in bars.get(pair, []):
            if bar["datetime"] >= day_close:
                continue
            extreme = bar_extreme(side, bar)
            if pair not in first and touched(side, levels[pair], extreme, pair):
                first[pair] = (bar["datetime"], extreme)
                break
    if not first:
        return SideState(ACTIVE)

    def best_from(pair: str, start: datetime.datetime) -> float | None:
        best = None
        for bar in bars.get(pair, []):
            if start <= bar["datetime"] < day_close and better_approach(side, bar_extreme(side, bar), best):
                best = bar_extreme(side, bar)
        return best

    if len(first) == 2:
        leader, laggard = sorted(PAIRS, key=lambda p: first[p][0])
        lead_at, lead_price = first[leader]
        lag_at, lag_price = first[laggard]
        if lag_at - lead_at <= bar_len:  # the same hour, or the very next one: ambiguous, so in sync
            return SideState(
                CONFIRMED, leader if lag_at != lead_at else None, lead_at, lead_price, basis,
                laggard_touched_at=lag_at, laggard_price=lag_price, resolved_at=now,
            )
        deadline = window_deadline(lead_at + bar_len, day_close)
        return SideState(
            DIVERGENCE, leader, lead_at, lead_price, basis, deadline,
            laggard_touched_at=lag_at, laggard_price=lag_price, resolved_at=now, detected_late=True,
        )

    (leader,) = first
    laggard = other_pair(leader)
    lead_at, lead_price = first[leader]
    deadline = window_deadline(min(lead_at + bar_len, now), day_close)
    best = best_from(laggard, lead_at)
    if now >= deadline:
        return SideState(
            DIVERGENCE, leader, lead_at, lead_price, basis, deadline,
            laggard_best_price=best, resolved_at=now, detected_late=True,
        )
    return SideState(LEADER_TOUCHED, leader, lead_at, lead_price, basis, deadline, laggard_best_price=best)


# ── In-memory row state ─────────────────────────────────────────────────────

@dataclass
class RowState:
    """The cached mirror of a smt_divergence_levels row that is still open (ACTIVE / LEADER_TOUCHED). Ticks work on
    this, never on the database."""

    id: int
    trading_day: datetime.date
    side: str
    levels: dict[str, float]  # pair -> level
    expires_at: datetime.datetime  # the close of the monitored trading day
    status: str = ACTIVE
    leader_pair: str | None = None
    leader_touched_at: datetime.datetime | None = None
    leader_price: float | None = None
    leader_touch_basis: str | None = None
    deadline_at: datetime.datetime | None = None
    laggard_best: float | None = None  # closest approach so far (lowest for LOW, highest for HIGH)
    best_dirty: bool = False  # changed since it was last written to the database
    detected_late: bool = False

    @property
    def laggard(self) -> str | None:
        return other_pair(self.leader_pair) if self.leader_pair else None

    @classmethod
    def from_model(cls, row: SmtDivergenceLevel) -> "RowState":
        return cls(
            id=row.id, trading_day=row.trading_day, side=row.side,
            levels={PAIR_GBP: row.gbpusd_level, PAIR_EUR: row.eurusd_level}, expires_at=row.expires_at,
            status=row.status, leader_pair=row.leader_pair, leader_touched_at=row.leader_touched_at,
            leader_price=row.leader_price, leader_touch_basis=row.leader_touch_basis, deadline_at=row.deadline_at,
            laggard_best=row.laggard_best_price, detected_late=row.detected_late,
        )


# ── Pure: the alert text ────────────────────────────────────────────────────

def _hour_range(start: datetime.datetime) -> str:
    wat = sweep._to_wat(start)
    return f"{wat:%Y-%m-%d %H:%M}–{wat + HOUR:%H:%M} WAT"


def format_divergence(
    side: str,
    leader: str,
    levels: dict[str, float],
    leader_touched_at: datetime.datetime,
    leader_price: float,
    basis: str,
    deadline_at: datetime.datetime,
    now: datetime.datetime,
    laggard_price: float | None,
    laggard_best: float | None,
    laggard_touched_at: datetime.datetime | None = None,
    detected_late: bool = False,
) -> str:
    """One divergence alert (HTML). Short and factual: states what happened, no trade advice.

    `laggard_touched_at` is set when the laggard DID touch its level, but only after the window had closed (the tick
    path got there before the timer): the alert then says how long after, instead of "did not touch"."""
    laggard = other_pair(leader)
    name = level_name(side)
    position = "above" if side == LOW else "below"
    fmt_leader, fmt_laggard = (lambda v: sweep.format_price(leader, v)), (lambda v: sweep.format_price(laggard, v))
    lead_level, lag_level = levels[leader], levels[laggard]

    if basis == BASIS_TICK:
        when = f"touched {sweep._wat_str(leader_touched_at)} at {fmt_leader(leader_price)}"
    elif basis == BASIS_HOUR:
        when = f"touched in the {_hour_range(leader_touched_at)} hour (extreme {fmt_leader(leader_price)})"
    else:
        when = f"already beyond it when detected, {sweep._wat_str(leader_touched_at)} (price {fmt_leader(leader_price)})"
    window = minutes_between(leader_touched_at, deadline_at) if basis == BASIS_TICK else DIVERGENCE_WAIT_MINUTES

    lines = [
        f"🔀 <b>SMT DIVERGENCE — {name}</b>",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🥇 <b>{leader}</b> took its {name} {fmt_leader(lead_level)} — {when}",
    ]

    note = None
    if laggard_touched_at is not None:
        late = minutes_between(deadline_at, laggard_touched_at)
        if basis == BASIS_HOUR and detected_late:
            after = f"touched in the {_hour_range(laggard_touched_at)} hour, after the window had closed"
        else:
            after = f"touched {'under 1' if late < 1 else late} min after the window closed"
        lines.append(f"🥈 <b>{laggard}</b> {name} {fmt_laggard(lag_level)} — {after}")
        interpretation = f"{leader} took its {name}; {laggard} touched its {name} only after the window closed."
    else:
        price_text = "price n/a"
        if laggard_price is not None:
            gap = abs(pips_from_level(side, lag_level, laggard_price, laggard))
            price_text = f"price {fmt_laggard(laggard_price)} ({gap:.1f} pips {position} it)"
        lines.append(f"🥈 <b>{laggard}</b> {name} {fmt_laggard(lag_level)} — {price_text}")
        if laggard_best is not None:
            approach = pips_from_level(side, lag_level, laggard_best, laggard)
            lines[-1] += f", closest approach {approach:.1f} pips from the level"
            if 0 < approach <= near_miss_pips_for(laggard):
                note = (
                    f"⚠️ The closest approach was within {near_miss_pips_for(laggard):g} pips of the level — "
                    "this may be a feed difference between Twelve Data levels and Finnhub ticks."
                )
        interpretation = f"{leader} took its {name}; {laggard} held {position} its {name} after {window} min."

    lines.append(f"⏱ Window: {window} min, closed {sweep._wat_str(deadline_at)}")
    lines += ["━━━━━━━━━━━━━━━━━━━━", f"ℹ️ {interpretation}"]
    if note:
        lines.append(note)
    if detected_late:
        if basis == BASIS_HOUR:
            lines.append(
                f"⚡ Detected late: the levels were armed after the move, so {leader}'s touch is only known to the hour; "
                "the window is counted from the end of that hour."
            )
        elif basis == BASIS_DETECTED:
            lines.append("⚡ Detected late: price was already beyond the level when this was armed; the touch time wasn't observed.")
        else:
            overdue = minutes_between(deadline_at, now)
            lines.append(f"⚡ Detected {overdue} min after the window closed (the check was not running).")
    return "\n".join(lines)


# ── Pure: the /divergence status text ───────────────────────────────────────

def format_status_message(rows: list, now: datetime.datetime) -> str:
    """`rows`: the smt_divergence_levels rows of the period being monitored (any objects with the model's fields)."""
    if not rows:
        return (
            "🔀 <b>SMT Divergence — GBPUSD / EURUSD</b>\n\nNo levels are being monitored yet. They are recorded at the "
            "17:10 New York scan after each trading day closes."
        )
    first = rows[0]
    lines = [
        "🔀 <b>SMT Divergence — GBPUSD / EURUSD</b>",
        f"Levels from {first.trading_day:%a %Y-%m-%d} · monitored until {sweep._wat_str(first.expires_at)}",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    for row in sorted(rows, key=lambda r: (r.side != HIGH,)):  # PDH first, then PDL
        name = level_name(row.side)
        lines.append(
            f"<b>{name}</b>  GBPUSD {sweep.format_price(PAIR_GBP, row.gbpusd_level)} · "
            f"EURUSD {sweep.format_price(PAIR_EUR, row.eurusd_level)}"
        )
        lines.append("   " + _status_line(row, now))
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def _status_line(row, now: datetime.datetime) -> str:
    name = level_name(row.side)
    if row.status == ACTIVE:
        return "⏳ waiting — neither pair has touched its level"
    if row.status == LEADER_TOUCHED:
        laggard = other_pair(row.leader_pair)
        if row.leader_touch_basis == BASIS_HOUR:
            when = f"in the {_hour_range(row.leader_touched_at)} hour"
        else:
            when = sweep._wat_str(row.leader_touched_at)
        line = f"🟡 {row.leader_pair} touched its {name} {when} — {laggard} must touch by {sweep._wat_str(row.deadline_at)}"
        if row.laggard_best_price is not None:
            level = row.gbpusd_level if laggard == PAIR_GBP else row.eurusd_level
            line += f" (closest so far: {pips_from_level(row.side, level, row.laggard_best_price, laggard):.1f} pips away)"
        return line
    if row.status == CONFIRMED:
        return "✅ confirmed — both pairs touched their level in time"
    if row.status == DIVERGENCE:
        return "🔀 divergence — alert sent"
    return "⌛ expired — neither pair touched its level"


# ── The service ─────────────────────────────────────────────────────────────

class SmtDivergenceService:
    def __init__(
        self,
        api_key: str,
        market_data: MarketDataService,
        users_service: UsersService,
        send_message: SendMessage,
    ) -> None:
        self.api_key = api_key
        self.market_data = market_data
        self.users_service = users_service
        self.send_message = send_message

        # Wired up in main.py: the sweep service (its scan feeds this one and shares the Twelve Data rate limit), the
        # Finnhub subscribe call, and the latest tick per symbol.
        self.sweep_service: sweep.DailySweepService | None = None
        self.subscribe_symbol: SubscribeSymbol | None = None
        self.get_last_price: Callable[[str], tuple[float, datetime.datetime] | None] = lambda symbol: None
        # Injectable so tests can pin "now"
        self.clock: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(UTC)

        # In-memory mirror of the open rows (ACTIVE / LEADER_TOUCHED): ticks never touch the database.
        self._rows: dict[int, RowState] = {}
        self._inflight: set[int] = set()  # rows with a claim being awaited: a concurrent tick/timer skips them
        self._ticks: dict[str, tuple[float, datetime.datetime]] = {}  # the latest tick of each pair, seen here
        self._candles: dict[str, tuple[datetime.date, list[dict]]] = {}  # from the sweep scan's listener
        self._captured_day: datetime.date | None = None
        self._capture_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._last_td_request_at = float("-inf")

    # ── Database (every access lives here; tests swap these for an in-memory table) ──

    async def _insert_row(
        self, trading_day: datetime.date, side: str, levels: dict[str, float], expires_at: datetime.datetime, state: SideState
    ) -> int | None:
        """Inserts a side already in the state the replay found. None = it exists already (the unique constraint is
        the once-only claim, so a rescan creates and announces nothing twice)."""
        values = dict(
            trading_day=trading_day, side=side, gbpusd_level=levels[PAIR_GBP], eurusd_level=levels[PAIR_EUR],
            status=state.status, leader_pair=state.leader_pair, leader_touched_at=state.leader_touched_at,
            leader_price=state.leader_price, leader_touch_basis=state.leader_touch_basis, deadline_at=state.deadline_at,
            resolved_at=state.resolved_at, laggard_touched_at=state.laggard_touched_at, laggard_price=state.laggard_price,
            laggard_best_price=state.laggard_best_price, detected_late=state.detected_late, expires_at=expires_at,
        )
        async with SessionLocal() as session:
            result = await session.execute(
                pg_insert(SmtDivergenceLevel).values(**values)
                .on_conflict_do_nothing(constraint="uq_smt_day_side").returning(SmtDivergenceLevel.id)
            )
            row_id = result.scalar_one_or_none()
            await session.commit()
        return row_id

    async def _load_open_rows(self, now: datetime.datetime) -> list[RowState]:
        async with SessionLocal() as session:
            result = await session.execute(
                select(SmtDivergenceLevel).where(
                    SmtDivergenceLevel.status.in_((ACTIVE, LEADER_TOUCHED)), SmtDivergenceLevel.expires_at > now
                )
            )
            return [RowState.from_model(row) for row in result.scalars().all()]

    async def _claim(
        self,
        row_id: int,
        expected: str,
        new: str,
        values: dict,
        deadline_gt: datetime.datetime | None = None,
        deadline_le: datetime.datetime | None = None,
        expires_le: datetime.datetime | None = None,
    ) -> bool:
        """Atomic conditional UPDATE: only the caller that finds the row still in `expected` (and the time conditions
        holding) gets rowcount 1 and may announce the new state."""
        conditions = [SmtDivergenceLevel.id == row_id, SmtDivergenceLevel.status == expected]
        if deadline_gt is not None:
            conditions.append(SmtDivergenceLevel.deadline_at > deadline_gt)
        if deadline_le is not None:
            conditions.append(SmtDivergenceLevel.deadline_at <= deadline_le)
        if expires_le is not None:
            conditions.append(SmtDivergenceLevel.expires_at <= expires_le)
        async with SessionLocal() as session:
            result = await session.execute(
                update(SmtDivergenceLevel).where(*conditions).values(status=new, **values)
            )
            claimed = result.rowcount == 1
            await session.commit()
        return claimed

    async def _flush_best(self, row_id: int, best: float | None) -> bool:
        """Best-effort write of the laggard's closest approach while its window is open."""
        async with SessionLocal() as session:
            result = await session.execute(
                update(SmtDivergenceLevel)
                .where(SmtDivergenceLevel.id == row_id, SmtDivergenceLevel.status == LEADER_TOUCHED)
                .values(laggard_best_price=best)
            )
            await session.commit()
            return result.rowcount == 1

    async def _expire_in_db(self, now: datetime.datetime) -> list[int]:
        """Rows nobody touched by the end of their monitored day. Silent."""
        async with SessionLocal() as session:
            result = await session.execute(
                update(SmtDivergenceLevel)
                .where(SmtDivergenceLevel.status == ACTIVE, SmtDivergenceLevel.expires_at <= now)
                .values(status=EXPIRED, resolved_at=now)
                .returning(SmtDivergenceLevel.id)
            )
            ids = [row[0] for row in result.all()]
            await session.commit()
        return ids

    async def _select_current(self, now: datetime.datetime) -> list[SmtDivergenceLevel]:
        async with SessionLocal() as session:
            result = await session.execute(
                select(SmtDivergenceLevel)
                .where(SmtDivergenceLevel.expires_at > now)
                .order_by(SmtDivergenceLevel.trading_day.desc(), SmtDivergenceLevel.side)
            )
            rows = list(result.scalars().all())
        latest = rows[0].trading_day if rows else None
        return [row for row in rows if row.trading_day == latest]

    async def _count_rows(self, trading_day: datetime.date) -> int:
        async with SessionLocal() as session:
            result = await session.execute(
                select(func.count()).select_from(SmtDivergenceLevel).where(SmtDivergenceLevel.trading_day == trading_day)
            )
            return result.scalar_one()

    # ── Startup ──────────────────────────────────────────────────────────────

    async def bootstrap(self) -> None:
        """Rebuilds the open rows from the database. A window that closed while the bot was down is resolved by the
        first timer run (and labelled as detected late)."""
        now = self.clock()
        await self._expire_in_db(now)
        self._rows = {row.id: row for row in await self._load_open_rows(now)}
        logger.info(f"[SmtDivergence] Resumed {len(self._rows)} open row(s) from the database")

    def active_symbols(self) -> set[str]:
        return set(PAIRS) if self._rows else set()

    # ── Prices ───────────────────────────────────────────────────────────────

    def _fresh_price(self, pair: str, now: datetime.datetime) -> float | None:
        """The pair's latest tick if it's recent enough to say it is beyond a level right now."""
        tick = self._ticks.get(pair) or self.get_last_price(pair)
        if tick is None:
            return None
        price, at = tick
        return price if now - at <= sweep.FRESH_PRICE_MAX_AGE else None

    def _latest_price(self, pair: str) -> float | None:
        """The latest Finnhub tick, falling back to the last known price (however old)."""
        tick = self._ticks.get(pair) or self.get_last_price(pair)
        return tick[0] if tick else None

    # ── Level capture ────────────────────────────────────────────────────────

    def on_candles(
        self, pair: str, candles_1h: list[dict], expected_day: datetime.date, now: datetime.datetime
    ) -> None:
        """DailySweepService.candle_listeners hook. Cheap and synchronous (it runs inside the sweep scan): it only
        buffers the candles and, once both pairs are in, spawns the capture as its own task."""
        if pair not in PAIRS:
            return
        self._candles[pair] = (expected_day, candles_1h)
        if self._captured_day == expected_day:
            return
        if all(self._candles.get(p, (None, []))[0] == expected_day for p in PAIRS):
            task = asyncio.create_task(self._capture_from_buffer(expected_day))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _capture_from_buffer(self, expected_day: datetime.date) -> None:
        try:
            if await self.capture({p: self._candles[p][1] for p in PAIRS}, expected_day):
                self._captured_day = expected_day
        except Exception as error:
            logger.error(f"[SmtDivergence] Level capture failed: {error}")

    async def capture(self, candles_by_pair: dict[str, list[dict]], expected_day: datetime.date) -> bool:
        """Records both pairs' PDH/PDL from `expected_day`'s synthetic daily candle (only if BOTH pairs' latest
        complete day is that day) and arms each side at the state the bars since the close show. True = done (or
        nothing left to do); False = not possible yet (a pair is missing/lagging): the sweep's retry will call again."""
        async with self._capture_lock:
            now = self.clock()
            days: dict[str, sweep.DailyCandle] = {}
            for pair in PAIRS:
                candles = candles_by_pair.get(pair)
                if not candles:
                    logger.warning(f"[SmtDivergence] No candles for {pair} yet — levels not recorded")
                    return False
                usable = sweep.usable_daily_candles(
                    sweep.aggregate_1h_to_daily(candles, now, sweep.min_daily_bars_for(pair)), pair
                )
                if not usable or usable[-1].date != expected_day:
                    latest = usable[-1].date if usable else None
                    logger.warning(
                        f"[SmtDivergence] {pair} latest complete trading day is {latest}, expected {expected_day} — "
                        "levels not recorded (retry)"
                    )
                    return False
                days[pair] = usable[-1]

            expires_at = monitored_until(expected_day)
            if now >= expires_at:
                logger.info(f"[SmtDivergence] The monitored period of {expected_day} is already over — nothing to arm")
                return True
            day_close = days[PAIR_GBP].end_utc
            bars = {
                pair: [b for b in sweep.bars_since(candles_by_pair[pair], day_close) if b["datetime"] < expires_at]
                for pair in PAIRS
            }
            basis = BASIS_HOUR
            if not any(bars.values()):  # no bars since the close: use the latest prices instead
                basis = BASIS_DETECTED
                for pair in PAIRS:
                    price = self._fresh_price(pair, now)
                    if price is not None:
                        bars[pair] = [{"datetime": now, "high": price, "low": price}]

            created = 0
            for side in SIDES:
                levels = {pair: (days[pair].low if side == LOW else days[pair].high) for pair in PAIRS}
                logger.info(
                    f"[SmtDivergence] {expected_day} {level_name(side)}: "
                    + " · ".join(f"{pair} {levels[pair]}" for pair in PAIRS)
                )
                state = replay_side(side, levels, bars, now, expires_at, basis)
                row_id = await self._insert_row(expected_day, side, levels, expires_at, state)
                if row_id is None:
                    logger.info(f"[SmtDivergence] {expected_day} {level_name(side)} already recorded")
                    continue
                created += 1
                row = RowState(
                    id=row_id, trading_day=expected_day, side=side, levels=levels, expires_at=expires_at,
                    status=state.status, leader_pair=state.leader_pair, leader_touched_at=state.leader_touched_at,
                    leader_price=state.leader_price, leader_touch_basis=state.leader_touch_basis,
                    deadline_at=state.deadline_at, laggard_best=state.laggard_best_price, detected_late=state.detected_late,
                )
                if state.status in (ACTIVE, LEADER_TOUCHED):
                    self._rows[row_id] = row
                    if state.status == LEADER_TOUCHED:
                        logger.info(
                            f"[SmtDivergence] {level_name(side)}: {state.leader_pair} already touched its level when armed "
                            f"({state.leader_touch_basis}); {other_pair(state.leader_pair)} has until {state.deadline_at}"
                        )
                elif state.status == CONFIRMED:
                    logger.info(f"[SmtDivergence] {level_name(side)}: both pairs already touched in sync when armed — silent")
                else:
                    await self._send_divergence(
                        row, now, state.laggard_price if state.laggard_touched_at else self._latest_price(row.laggard),
                        state.laggard_touched_at, state.detected_late,
                    )
            logger.info(f"[SmtDivergence] Levels for {expected_day}: {created} new row(s)")
            if self._rows and self.subscribe_symbol is not None:
                for pair in PAIRS:
                    try:
                        await self.subscribe_symbol(pair)
                    except Exception as error:
                        logger.error(f"[SmtDivergence] Could not subscribe {pair} to the live feed: {error}")
            return True

    async def capture_now(self) -> str:
        """Own fetch of just the two pairs (the sweep scan normally feeds the levels): for /divscan and the startup
        catch-up. Shares the Twelve Data spacing and never runs while a sweep scan is in progress."""
        if self.sweep_service is not None and self.sweep_service.scan_running:
            return "A Daily Sweep scan is running — it feeds the levels itself when it reaches GBPUSD and EURUSD."
        now = self.clock()
        expected_day = sweep.latest_closed_trading_day(now)
        candles: dict[str, list[dict]] = {}
        for pair in PAIRS:
            wait = self._last_td_request_at + sweep.SCAN_REQUEST_SPACING_SECONDS - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            data, quota_exceeded = await self.market_data.get_candles(
                pair, sweep.ONE_H_INTERVAL, self.api_key, outputsize=sweep.ONE_H_LOOKBACK
            )
            self._last_td_request_at = time.monotonic()
            if quota_exceeded:
                return "Twelve Data quota exceeded — try again in a minute."
            if not data:
                return f"No candle data returned for {pair}."
            candles[pair] = data
        done = await self.capture(candles, expected_day)
        if done:
            self._captured_day = expected_day
        return "Levels recorded." if done else "The latest complete trading day isn't available for both pairs yet."

    async def needs_catch_up(self) -> bool:
        """The latest closed trading day's levels are missing although its 17:10 scan time has passed and its
        monitored period isn't over (the bot was down, or the feature was just deployed)."""
        now = self.clock()
        expected_day = sweep.latest_closed_trading_day(now)
        scheduled = datetime.datetime.combine(
            expected_day, datetime.time(sweep.SCAN_HOUR_NY, sweep.SCAN_MINUTE_NY), tzinfo=sweep.NY
        ).astimezone(UTC)
        if now < scheduled or now >= monitored_until(expected_day):
            return False
        return await self._count_rows(expected_day) == 0

    async def catch_up(self) -> str | None:
        """Startup: capture the missing levels ourselves, unless the sweep's own catch-up scan is about to feed them."""
        if not await self.needs_catch_up():
            return None
        if self.sweep_service is not None and await self.sweep_service.needs_catch_up():
            return "the Daily Sweep catch-up scan will feed the levels"
        logger.info("[SmtDivergence] Levels for the latest closed trading day are missing — capturing them")
        return await self.capture_now()

    # ── State changes: one atomic claim each ─────────────────────────────────

    async def _transition(
        self, row: RowState, expected: str, new: str, values: dict, **conditions: datetime.datetime
    ) -> str:
        """Claim `expected -> new` in the database, then mirror it in memory. Returns "won" (this caller may announce
        it), "lost" (someone else already moved the row), "busy" (a claim for this row is already running here) or
        "error" (database problem: nothing changed, it is retried on the next tick/timer run)."""
        if row.id in self._inflight:
            return "busy"
        self._inflight.add(row.id)
        try:
            claimed = await self._claim(row.id, expected, new, values, **conditions)
        except Exception as error:
            logger.error(f"[SmtDivergence] Could not move row #{row.id} {expected} -> {new}: {error}")
            return "error"
        finally:
            self._inflight.discard(row.id)
        if not claimed:
            self._rows.pop(row.id, None)  # another instance/path owns it now
            return "lost"
        row.status = new
        for key, attribute in (
            ("leader_pair", "leader_pair"), ("leader_touched_at", "leader_touched_at"), ("leader_price", "leader_price"),
            ("leader_touch_basis", "leader_touch_basis"), ("deadline_at", "deadline_at"), ("detected_late", "detected_late"),
        ):
            if key in values:
                setattr(row, attribute, values[key])
        if new not in (ACTIVE, LEADER_TOUCHED):
            self._rows.pop(row.id, None)
        return "won"

    async def check_tick(self, symbol: str, price: float, previous_price: float | None) -> None:
        if symbol not in PAIRS:
            return
        now = self.clock()
        self._ticks[symbol] = (price, now)
        if sweep.is_market_closed(now):
            return  # weekend quotes are not trading
        for row in list(self._rows.values()):
            if row.id in self._inflight or now >= row.expires_at:
                continue  # (an expired row is the timer's to close)
            try:
                await self._on_tick(row, symbol, price, now)
            except Exception as error:
                logger.error(f"[SmtDivergence] Tick handling failed for row #{row.id}: {error}")

    async def _on_tick(self, row: RowState, symbol: str, price: float, now: datetime.datetime) -> None:
        if row.status == ACTIVE:
            outcome = first_touch_outcome(row.side, row.levels, symbol, price, self._fresh_price(other_pair(symbol), now))
            if outcome == LEADER_TOUCHED:
                laggard = other_pair(symbol)
                values = dict(
                    leader_pair=symbol, leader_touched_at=now, leader_price=price, leader_touch_basis=BASIS_TICK,
                    deadline_at=window_deadline(now, row.expires_at), laggard_best_price=self._fresh_price(laggard, now),
                )
                if await self._transition(row, ACTIVE, LEADER_TOUCHED, values) == "won":
                    row.laggard_best, row.best_dirty = values["laggard_best_price"], False
                    logger.info(
                        f"[SmtDivergence] {level_name(row.side)}: {symbol} touched its level {row.levels[symbol]} @ {price}; "
                        f"{laggard} has until {row.deadline_at}"
                    )
            elif outcome == CONFIRMED:
                values = dict(resolved_at=now, leader_touched_at=now, leader_touch_basis=BASIS_TICK, laggard_touched_at=now)
                if await self._transition(row, ACTIVE, CONFIRMED, values) == "won":
                    logger.info(f"[SmtDivergence] {level_name(row.side)}: both pairs are beyond their level — in sync, silent")
            return

        if row.status != LEADER_TOUCHED:
            return
        if now >= row.deadline_at:
            late_touch = symbol == row.laggard and touched(row.side, row.levels[symbol], price, symbol)
            await self._resolve_divergence(row, now, laggard_touch_price=price if late_touch else None)
            return
        if symbol != row.laggard:
            return
        if better_approach(row.side, price, row.laggard_best):  # closest approach so far
            row.laggard_best, row.best_dirty = price, True
        if touched(row.side, row.levels[symbol], price, symbol):
            values = dict(resolved_at=now, laggard_touched_at=now, laggard_price=price, laggard_best_price=row.laggard_best)
            outcome = laggard_touch_outcome(row.deadline_at, now)
            if outcome == CONFIRMED and await self._transition(row, LEADER_TOUCHED, CONFIRMED, values, deadline_gt=now) == "won":
                logger.info(f"[SmtDivergence] {level_name(row.side)}: {symbol} touched its level in time — in sync, silent")

    async def _resolve_divergence(
        self, row: RowState, now: datetime.datetime, laggard_touch_price: float | None = None
    ) -> None:
        """The window is over and the laggard hasn't touched in time: ONE alert, only if this caller wins the claim.
        `laggard_touch_price` is set when the laggard's own tick (arriving before the timer ran) touched its level
        after the deadline: the alert then says how long after, instead of "did not touch"."""
        late_touch = laggard_touch_price is not None
        overdue = (now - row.deadline_at).total_seconds()
        detected_late = row.detected_late or (not late_touch and overdue > 2 * DEADLINE_CHECK_INTERVAL_SECONDS)
        price = laggard_touch_price if late_touch else self._latest_price(row.laggard)
        values = dict(
            resolved_at=now, laggard_price=price, laggard_best_price=row.laggard_best,
            laggard_touched_at=now if late_touch else None, detected_late=detected_late,
        )
        if await self._transition(row, LEADER_TOUCHED, DIVERGENCE, values, deadline_le=now) == "won":
            await self._send_divergence(row, now, price, now if late_touch else None, detected_late)

    async def _send_divergence(
        self,
        row: RowState,
        now: datetime.datetime,
        laggard_price: float | None,
        laggard_touched_at: datetime.datetime | None,
        detected_late: bool,
    ) -> None:
        logger.info(
            f"[SmtDivergence] DIVERGENCE {level_name(row.side)}: {row.leader_pair} took its level, {row.laggard} "
            f"{'touched it late' if laggard_touched_at else 'did not touch it'} (closest approach {row.laggard_best})"
        )
        message = format_divergence(
            row.side, row.leader_pair, row.levels, row.leader_touched_at, row.leader_price, row.leader_touch_basis or BASIS_TICK,
            row.deadline_at, now, laggard_price, row.laggard_best, laggard_touched_at, detected_late,
        )
        for user in await self.users_service.find_divergence_subscribers():
            await self.send_message(user.chat_id, message)

    async def check_deadlines(self) -> None:
        """The periodic job (every DEADLINE_CHECK_INTERVAL_SECONDS): resolves windows whose deadline has passed even
        if no tick arrives, closes rows nobody touched, and persists the laggards' closest approaches."""
        now = self.clock()
        for row in list(self._rows.values()):
            try:
                if row.status == LEADER_TOUCHED and now >= row.deadline_at:
                    await self._resolve_divergence(row, now)
                elif row.status == ACTIVE and now >= row.expires_at:
                    if await self._transition(row, ACTIVE, EXPIRED, dict(resolved_at=now), expires_le=now) == "won":
                        logger.info(f"[SmtDivergence] {level_name(row.side)} {row.trading_day}: nobody touched — expired")
            except Exception as error:
                logger.error(f"[SmtDivergence] Deadline check failed for row #{row.id}: {error}")
        for row in list(self._rows.values()):
            if row.status == LEADER_TOUCHED and row.best_dirty and row.id not in self._inflight:
                row.best_dirty = False
                try:
                    await self._flush_best(row.id, row.laggard_best)
                except Exception as error:
                    row.best_dirty = True
                    logger.error(f"[SmtDivergence] Could not save row #{row.id}'s closest approach: {error}")
        for row_id in await self._expire_in_db(now):
            self._rows.pop(row_id, None)

    # ── Commands ─────────────────────────────────────────────────────────────

    async def get_status_message(self) -> str:
        return format_status_message(await self._select_current(self.clock()), self.clock())
