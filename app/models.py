import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

AlertType = str  # one of "SL", "TP", "TARGET"


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

    user_alert_id: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

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
