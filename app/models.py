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
    # Open Positions' close-all target: once the summed unrealized P/L of every
    # open position reaches +take_profit or -stop_loss, all of them are closed
    # and the target clears - unless close_all_repeat keeps it for the next
    # set of positions. It only applies in the mode it was set in.
    close_all_take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    close_all_stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    close_all_mode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    close_all_repeat: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AppSettings(Base):
    """Platform-wide settings edited on the dashboard's Settings page (a
    single row, id 1). A NULL column means "use the .env value"."""

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    max_multiplier_floor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Comma-separated Telegram alert events switched off (keys of
    # app.notify.EVENTS) - NULL means every event is on.
    telegram_muted: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ForexPair(Base):
    """A forex pair whose trading window was changed on the Forex page. Pairs
    with no row use app.forex.DEFAULT_PAIRS."""

    __tablename__ = "forex_pairs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    # Comma-separated keys of app.forex.SESSIONS, e.g. "london,new_york".
    sessions: Mapped[str] = mapped_column(String(80))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ForexBot(Base):
    """A strategy allowed to trade forex on the Forex page's cTrader account.
    Sized in lots (1 lot = 100,000 units of the base currency); stops are in
    pips. Leverage isn't per bot - the broker sets the margin for each pair.
    trailing_stop has cTrader trail the stop loss at its own distance;
    trailing_stop_pips is no longer used (cTrader can't trail at a separate
    distance) and is moved onto the stop loss at startup."""

    __tablename__ = "forex_bots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    # Comma-separated keys of app.forex.DEFAULT_PAIRS (EURUSD,...); NULL allows all.
    symbols: Mapped[str | None] = mapped_column(String(400), nullable=True)
    lots: Mapped[float] = mapped_column(Float, default=0.01)
    stop_loss_pips: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_pips: Mapped[float | None] = mapped_column(Float, nullable=True)
    trailing_stop_pips: Mapped[float | None] = mapped_column(Float, nullable=True)
    trailing_stop: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ForexSignal(Base):
    """A webhook sent to /webhook/forex and what became of it. Kept apart
    from Signal/Trade like CryptoSignal: the cTrader account holds the
    positions, and the Deriv reconcile, trailing and audit loops must never
    see them. An executed entry records the position it opened, so exits and
    history know which strategy a position belongs to."""

    __tablename__ = "forex_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_name: Mapped[str] = mapped_column(String(120), index=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    action: Mapped[SignalAction] = mapped_column(SqlEnum(SignalAction))
    direction: Mapped[Direction | None] = mapped_column(SqlEnum(Direction), nullable=True)
    lots: Mapped[float | None] = mapped_column(Float, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[ExecutionStatus] = mapped_column(SqlEnum(ExecutionStatus), default=ExecutionStatus.received)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # cTrader account and position: the one opened by an entry, or the ones
    # (comma-separated) closed by an exit or a reversal.
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    closed_positions: Mapped[str | None] = mapped_column(String(200), nullable=True)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    execution_mode: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class CryptoBot(Base):
    """A strategy allowed to trade on BloFin, and how big its entries are:
    `margin` USDT put up at `leverage`, so a position worth margin x leverage.
    The webhook's own size and leverage are ignored, as on Deriv."""

    __tablename__ = "crypto_bots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    # Comma-separated BloFin instruments (BTC-USDT,...); NULL allows any.
    symbols: Mapped[str | None] = mapped_column(String(400), nullable=True)
    margin: Mapped[float] = mapped_column(Float, default=10.0)
    leverage: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CryptoSignal(Base):
    """A webhook sent to /webhook/blofin and what became of it. Kept apart
    from Signal/Trade: BloFin holds the positions itself, and the Deriv
    reconcile, trailing and audit loops must never see them."""

    __tablename__ = "crypto_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_name: Mapped[str] = mapped_column(String(120))
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    action: Mapped[SignalAction] = mapped_column(SqlEnum(SignalAction))
    direction: Mapped[Direction | None] = mapped_column(SqlEnum(Direction), nullable=True)
    margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    leverage: Mapped[int] = mapped_column(Integer, default=1)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[ExecutionStatus] = mapped_column(SqlEnum(ExecutionStatus), default=ExecutionStatus.received)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    order_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # demo or live - the page only shows the current mode's signals.
    execution_mode: Mapped[str | None] = mapped_column(String(10), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class CTraderConnection(Base):
    """One market's cTrader ID login (id 1 = Synthetic → CFDs, Deriv
    accounts only; id 2 = Forex, other brokers - see ctrader.MARKETS): its
    OAuth tokens, the trading accounts linked to it, and which one trades in
    demo and in live mode. The tokens grant trading on those accounts - never shown or sent
    back to the dashboard."""

    __tablename__ = "ctrader_connection"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # JSON list from CTraderClient.list_accounts, refreshed on each login.
    accounts: Mapped[str | None] = mapped_column(Text, nullable=True)
    demo_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    live_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    # How this signal was triggered, shown as the Signal Journal's reason.
    # Entries are always strategy_entry; a position can close several ways
    # (the strategy's own exit webhook, a reversing entry, the standalone
    # manual exit webhook, a dashboard manual close with or without a chosen
    # price, the trailing stop, or Deriv closing the contract itself). One of:
    # strategy_entry, strategy_exit, reversal, webhook_exit, manual_close,
    # price_exit, trailing_stop, close_all_target, deriv_close.
    source: Mapped[str | None] = mapped_column(String(30), nullable=True)
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
    # Highest live profit seen while open - kept so a trailing stop survives
    # a restart instead of re-measuring its peak from scratch.
    peak_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Dashboard "close at price": sold once the spot reaches this level.
    # close_at_above records which side of the market it was set on, so the
    # trigger is "spot >= target" or "spot <= target" accordingly.
    close_at_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    close_at_above: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Extension's per-trade trailing stop: sold once profit falls trail_distance
    # dollars below trail_peak, the highest profit since it was set. Separate
    # from the bot's trail and peak_profit, which can run alongside it.
    trail_distance: Mapped[float | None] = mapped_column(Float, nullable=True)
    trail_peak: Mapped[float | None] = mapped_column(Float, nullable=True)
    # True once a dollar TP/SL was set on this contract from the dashboard.
    # The amounts themselves live on Deriv; this only lets the Bots page
    # show that a manual TP/SL is in place.
    manual_tpsl: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # How the position closed, using the same values as Signal.source for
    # exits (strategy_exit, reversal, webhook_exit, manual_close, price_exit,
    # trailing_stop, close_all_target, deriv_close). Shown as Trade History's reason.
    close_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # Which Synthetic account holds it (see SignalBot.account). For cTrader,
    # exchange_order_id is the position id and size is in lots.
    account: Mapped[str] = mapped_column(String(10), default="options", index=True)
    # The cTrader account a CFD trade was opened on, so it is always closed
    # there - even after demo/live is switched or a different account picked.
    ctrader_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

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
    # Trailing stop, both % of each trade's stake: once profit reaches
    # trail_start_pct (defaults to trail_distance_pct when unset), the trade
    # is closed if profit falls trail_distance_pct below its peak.
    trail_start_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    trail_distance_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    # When session_pnl/cycles_completed last restarted (null = never since
    # this column was added), so a later P&L correction only adjusts the
    # session total if that session actually counted the trade.
    session_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Which Synthetic account the bot trades: "options" (Deriv Multipliers,
    # sized by the stake in `size` and `leverage`) or "ctrader" (lot-sized
    # CFDs on the picked cTrader account, sized by `lots`).
    account: Mapped[str] = mapped_column(String(10), default="options")
    lots: Mapped[float | None] = mapped_column(Float, nullable=True)
    # CFD bots only: take profit / stop loss in dollars per trade, turned into
    # price distances from the lots and attached to each cTrader order, and
    # whether cTrader trails that stop loss on its own servers.
    tp_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    sl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    trailing_stop: Mapped[bool] = mapped_column(Boolean, default=False)
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
    session_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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
