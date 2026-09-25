from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, Enum as SqlEnum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Direction(str, Enum):
    long = "long"
    short = "short"


class SignalAction(str, Enum):
    entry = "entry"
    exit = "exit"


class ExecutionStatus(str, Enum):
    received = "received"
    accepted = "accepted"
    rejected = "rejected"
    executed = "executed"
    failed = "failed"
    closed = "closed"


class PositionStatus(str, Enum):
    open = "open"
    closing = "closing"
    closed = "closed"


class RiskSettings(Base):
    __tablename__ = "risk_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    emergency_stop: Mapped[bool] = mapped_column(Boolean, default=False)
    duplicate_blocking: Mapped[bool] = mapped_column(Boolean, default=True)
    execution_mode_override: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    max_account_exposure: Mapped[float] = mapped_column(Float, default=0.0)
    account_daily_loss_limit: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    max_position_size: Mapped[float] = mapped_column(Float, default=0.0)
    daily_loss_limit: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    signals: Mapped[list["Signal"]] = relationship(back_populates="strategy")
    trades: Mapped[list["Trade"]] = relationship(back_populates="strategy")


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_id: Mapped[int | None] = mapped_column(ForeignKey("strategies.id"), nullable=True)
    strategy_name: Mapped[str] = mapped_column(String(120), index=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    action: Mapped[SignalAction] = mapped_column(SqlEnum(SignalAction))
    direction: Mapped[Direction | None] = mapped_column(SqlEnum(Direction), nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    size: Mapped[float] = mapped_column(Float, default=0.0)
    leverage: Mapped[float] = mapped_column(Float, default=1.0)
    signal_id: Mapped[str | None] = mapped_column(String(160), index=True, nullable=True)
    status: Mapped[ExecutionStatus] = mapped_column(SqlEnum(ExecutionStatus), default=ExecutionStatus.received)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[str] = mapped_column(Text)
    # "demo" or "live" - whichever execution mode was active when this signal
    # arrived, so demo testing never mixes into live performance numbers.
    execution_mode: Mapped[str] = mapped_column(String(10), default="demo", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    strategy: Mapped[Strategy | None] = relationship(back_populates="signals")


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"))
    strategy_name: Mapped[str] = mapped_column(String(120), index=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    direction: Mapped[Direction] = mapped_column(SqlEnum(Direction))
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    size: Mapped[float] = mapped_column(Float)
    leverage: Mapped[float] = mapped_column(Float, default=1.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    profit_loss: Mapped[float] = mapped_column(Float, default=0.0)
    net_result: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[PositionStatus] = mapped_column(SqlEnum(PositionStatus), default=PositionStatus.open, index=True)
    execution_status: Mapped[ExecutionStatus] = mapped_column(SqlEnum(ExecutionStatus), default=ExecutionStatus.accepted)
    exchange_order_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # "demo" or "live" - whichever execution mode was active when this trade
    # was placed, so demo testing never mixes into live P&L/exposure/limits.
    execution_mode: Mapped[str] = mapped_column(String(10), default="demo", index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    strategy: Mapped[Strategy] = relationship(back_populates="trades")


class SignalBot(Base):
    __tablename__ = "signal_bots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    symbol: Mapped[str | None] = mapped_column(String(200), nullable=True)
    size: Mapped[float] = mapped_column(Float, default=0.01)
    # 0 means "inherit whatever's set on the Leverage page for this symbol"
    # (kept NOT NULL - the existing column can't be relaxed without a manual
    # SQLite table rebuild, and 0 is otherwise never a valid leverage).
    # Only set this above 0 to override the platform default for this bot.
    leverage: Mapped[float] = mapped_column(Float, default=0.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    hedge_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    cycles_completed: Mapped[int] = mapped_column(Integer, default=0)
    session_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    default_pair_tp_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    default_pair_sl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    default_pair_max_cycles: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    pairs: Mapped[list["BotPair"]] = relationship(back_populates="bot", cascade="all, delete-orphan")


class BotPair(Base):
    __tablename__ = "bot_pairs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("signal_bots.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    tp_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    sl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_cycles: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cycles_completed: Mapped[int] = mapped_column(Integer, default=0)
    session_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    bot: Mapped["SignalBot"] = relationship(back_populates="pairs")


class SymbolLeverage(Base):
    __tablename__ = "symbol_leverage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    leverage: Mapped[int] = mapped_column(Integer)
    # Comma-separated ints, e.g. "40,100,200,300,400" - cached from Deriv's
    # contracts_for so a process restart doesn't need 39 fresh Deriv round
    # trips just to render this list again.
    allowed_multipliers: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
