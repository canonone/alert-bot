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
import datetime
from dataclasses import dataclass

import app.services.daily_sweep_service as sweep
from app.models import SmtDivergenceLevel

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
