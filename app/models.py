import datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

AlertType = str  # one of "SL", "TP", "TARGET"
Direction = str  # one of "LONG", "SHORT"


class User(Base):
    __tablename__ = "users"

    # Telegram chat ID is the primary key
    chat_id: Mapped[str] = mapped_column(String, primary_key=True)

    first_name: Mapped[str | None] = mapped_column(String, nullable=True)
    username: Mapped[str | None] = mapped_column(String, nullable=True)

    # Their personal Twelve Data API key
    twelve_data_api_key: Mapped[str | None] = mapped_column(String, nullable=True)

    # Whether they've redeemed a valid invite code
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False)

    # Whether this user is the admin (set via ADMIN_CHAT_ID env var)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)

    # Whether they've completed API key setup
    is_setup: Mapped[bool] = mapped_column(Boolean, default=False)

    alert_id_counter: Mapped[int] = mapped_column(Integer, default=0)

    # Opt-in flags for Daily Sweep / Daily Continuation notifications (see DailySweepService)
    daily_sweep_alerts_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    daily_continuation_alerts_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    # Opt-in flag for SMT divergence notifications (see SmtDivergenceService)
    divergence_alerts_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    alerts: Mapped[list["PriceAlert"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class PriceAlert(Base):
    __tablename__ = "price_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    chat_id: Mapped[str] = mapped_column(ForeignKey("users.chat_id", ondelete="CASCADE"))
    user: Mapped["User"] = relationship(back_populates="alerts")

    symbol: Mapped[str] = mapped_column(String)
    type: Mapped[str] = mapped_column(String)  # AlertType: "SL" | "TP" | "TARGET"
    # asdecimal=False: return plain float, matching the TS `number` type used throughout
    target_price: Mapped[float] = mapped_column(Numeric(18, 6, asdecimal=False))
    # TARGET-only, optional: race second level — see PriceAlertService.is_triggered
    invalidation_price: Mapped[float | None] = mapped_column(Numeric(18, 6, asdecimal=False), nullable=True)
    # Optional free-text note, any alert type — shown in the trigger notification
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # SL/TP only: which side of the trade the level protects — see PriceAlertService.is_triggered.
    # TARGET rows keep the "LONG" default and ignore it.
    direction: Mapped[str] = mapped_column(String(10), nullable=False, default="LONG", server_default="LONG")

    user_alert_id: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DailySweepSetup(Base):
    __tablename__ = "daily_sweep_setups"
    __table_args__ = (UniqueConstraint("pair", "day2_date", name="uq_daily_sweep_pair_day2"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    pair: Mapped[str] = mapped_column(String, index=True)
    # "SWEEP" | "CONTINUATION" — both patterns live in this table (a Day 2 can only match one)
    kind: Mapped[str] = mapped_column(String(16), default="SWEEP", server_default="SWEEP")
    direction: Mapped[str] = mapped_column(String(10))  # "SELL" | "BUY"

    day1_open: Mapped[float] = mapped_column(Numeric(18, 6, asdecimal=False))
    day1_high: Mapped[float] = mapped_column(Numeric(18, 6, asdecimal=False))
    day1_low: Mapped[float] = mapped_column(Numeric(18, 6, asdecimal=False))
    day1_close: Mapped[float] = mapped_column(Numeric(18, 6, asdecimal=False))

    day2_open: Mapped[float] = mapped_column(Numeric(18, 6, asdecimal=False))
    day2_high: Mapped[float] = mapped_column(Numeric(18, 6, asdecimal=False))
    day2_low: Mapped[float] = mapped_column(Numeric(18, 6, asdecimal=False))
    day2_close: Mapped[float] = mapped_column(Numeric(18, 6, asdecimal=False))

    # SWEEP: Day 2's open — the price Day 3 must retrace to.
    # CONTINUATION: Level 1, the broken Day 1 high (BUY) / low (SELL).
    level: Mapped[float] = mapped_column(Numeric(18, 6, asdecimal=False))
    # CONTINUATION only: Level 2, Day 2's open (always beyond Level 1, hit second)
    level2: Mapped[float | None] = mapped_column(Numeric(18, 6, asdecimal=False), nullable=True)
    # Alerts already sent: SWEEP 0 -> 1 (done); CONTINUATION 0 -> 1 (Level 1 hit) -> 2 (done)
    stage: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # Trading day (labelled by the New York date it closes on) of the Day 2 candle
    day2_date: Mapped[datetime.date] = mapped_column(Date)
    # Close of Day 3 (UTC) — the setup is cancelled if the level isn't reached by then
    day3_expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))

    # ACTIVE | TRIGGERED (all stages sent) | EXPIRED | INVALIDATED (continuation only: Day 2's extreme was
    # swept before Level 1 was reached)
    status: Mapped[str] = mapped_column(String(16), index=True, default="ACTIVE")
    triggered_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trigger_price: Mapped[float | None] = mapped_column(Numeric(18, 6, asdecimal=False), nullable=True)
    # Set when status becomes INVALIDATED: when the sweep was DETECTED (not necessarily when it happened, e.g.
    # after a restart), and the price seen then
    invalidated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidated_price: Mapped[float | None] = mapped_column(Numeric(18, 6, asdecimal=False), nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DailySweepScanState(Base):
    """Singleton row (id=1): the latest trading day whose Daily Sweep scan fully completed."""

    __tablename__ = "daily_sweep_scan_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_scanned_day: Mapped[datetime.date] = mapped_column(Date)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SmtDivergenceLevel(Base):
    """One side (LOW = previous-day lows, HIGH = previous-day highs) of one monitored period for the GBPUSD/EURUSD
    SMT divergence check. See SmtDivergenceService for the state machine."""

    __tablename__ = "smt_divergence_levels"
    __table_args__ = (UniqueConstraint("trading_day", "side", name="uq_smt_day_side"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # The COMPLETED trading day (labelled by the New York date it closes on) whose high/low are the levels;
    # the period monitored runs from that close until the next trading day closes (expires_at).
    trading_day: Mapped[datetime.date] = mapped_column(Date)
    side: Mapped[str] = mapped_column(String(4))  # "LOW" | "HIGH"
    gbpusd_level: Mapped[float] = mapped_column(Numeric(18, 6, asdecimal=False))
    eurusd_level: Mapped[float] = mapped_column(Numeric(18, 6, asdecimal=False))

    # ACTIVE -> LEADER_TOUCHED -> CONFIRMED | DIVERGENCE;  ACTIVE -> EXPIRED  (every step an atomic claim)
    status: Mapped[str] = mapped_column(String(16), index=True, default="ACTIVE")

    leader_pair: Mapped[str | None] = mapped_column(String(8), nullable=True)
    leader_touched_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    leader_price: Mapped[float | None] = mapped_column(Numeric(18, 6, asdecimal=False), nullable=True)
    # How well the touch time is known: "tick" (observed live), "hour" (from a 1H bar) or "detected" (when we noticed)
    leader_touch_basis: Mapped[str | None] = mapped_column(String(8), nullable=True)
    deadline_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    resolved_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    laggard_touched_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    laggard_price: Mapped[float | None] = mapped_column(Numeric(18, 6, asdecimal=False), nullable=True)  # at resolution
    # The laggard's closest approach to its level while the window was open (lowest for LOW, highest for HIGH)
    laggard_best_price: Mapped[float | None] = mapped_column(Numeric(18, 6, asdecimal=False), nullable=True)
    detected_late: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InviteCode(Base):
    __tablename__ = "invite_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String, unique=True)
    used: Mapped[bool] = mapped_column(Boolean, default=False)

    # The chat_id of whoever redeemed this code
    used_by: Mapped[str | None] = mapped_column(String, nullable=True)
    used_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
