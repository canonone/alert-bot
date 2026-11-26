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


class SmtLevelColumns:
    """The columns every SMT divergence table shares. A row is one side (LOW = previous-day lows, HIGH =
    previous-day highs) of one monitored period for one pair group; each group has its own table, which adds the two
    level columns, named <pair>_level. See SmtDivergenceService for the state machine."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # The COMPLETED trading day (labelled by the New York date it closes on) whose high/low are the levels;
    # the period monitored runs from that close until the next trading day closes (expires_at).
    trading_day: Mapped[datetime.date] = mapped_column(Date)
    side: Mapped[str] = mapped_column(String(4))  # "LOW" | "HIGH"

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


class SmtDivergenceLevel(SmtLevelColumns, Base):
    """GBPUSD / EURUSD."""

    __tablename__ = "smt_divergence_levels"
    __table_args__ = (UniqueConstraint("trading_day", "side", name="uq_smt_day_side"),)

    gbpusd_level: Mapped[float] = mapped_column(Numeric(18, 6, asdecimal=False))
    eurusd_level: Mapped[float] = mapped_column(Numeric(18, 6, asdecimal=False))


class SmtJpyDivergenceLevel(SmtLevelColumns, Base):
    """EURJPY / GBPJPY."""

    __tablename__ = "smt_jpy_divergence_levels"
    __table_args__ = (UniqueConstraint("trading_day", "side", name="uq_smt_jpy_day_side"),)

    eurjpy_level: Mapped[float] = mapped_column(Numeric(18, 6, asdecimal=False))
    gbpjpy_level: Mapped[float] = mapped_column(Numeric(18, 6, asdecimal=False))


class InviteCode(Base):
    __tablename__ = "invite_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String, unique=True)
    used: Mapped[bool] = mapped_column(Boolean, default=False)

    # The chat_id of whoever redeemed this code
    used_by: Mapped[str | None] = mapped_column(String, nullable=True)
    used_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
