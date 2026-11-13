from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models import Direction, ExecutionStatus, PositionStatus, SignalAction


def normalize_symbol(value: str) -> str:
    """Trades are stored with an upper-cased symbol, so every lookup has to
    use the same form - otherwise an exit sent as e.g. "Volatility_25_Index"
    never finds the open position its entry created."""
    return value.strip().upper()


class ForexPairUpdate(BaseModel):
    sessions: list[str] = Field(..., min_length=1)


class ForexBotCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    symbols: str | None = Field(default=None, max_length=400)
    # 0.01 is a micro lot (1,000 units), the usual smallest step on cTrader.
    lots: float = Field(default=0.01, ge=0.01, le=100)
    stop_loss_pips: float | None = Field(default=None, gt=0)
    take_profit_pips: float | None = Field(default=None, gt=0)
    # cTrader trails the stop loss at its own distance, so it needs one.
    trailing_stop: bool = False


class ForexBotUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    symbols: str | None = Field(default=None, max_length=400)
    lots: float | None = Field(default=None, ge=0.01, le=100)
    stop_loss_pips: float | None = Field(default=None, gt=0)
    take_profit_pips: float | None = Field(default=None, gt=0)
    trailing_stop: bool | None = None
    enabled: bool | None = None


class CryptoBotCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    symbols: str | None = Field(default=None, max_length=400)
    margin: float = Field(default=10.0, gt=0)
    leverage: int = Field(default=1, ge=1, le=125)
    # % of the margin per trade. A stop at 100% is the whole margin, which
    # isolated margin liquidates before it is reached.
    tp_pct: float | None = Field(default=None, gt=0)
    sl_pct: float | None = Field(default=None, gt=0, lt=100)


class CryptoBotUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    symbols: str | None = Field(default=None, max_length=400)
    margin: float | None = Field(default=None, gt=0)
    leverage: int | None = Field(default=None, ge=1, le=125)
    tp_pct: float | None = Field(default=None, gt=0)
    sl_pct: float | None = Field(default=None, gt=0, lt=100)
    enabled: bool | None = None


class WebhookSignal(BaseModel):
    secret: str | None = None
    strategy: str = Field(..., min_length=1, max_length=120)
    symbol: str = Field(..., min_length=1, max_length=40)
    action: SignalAction
    market_position: str | None = None
    direction: Direction | None = None
    price: float | None = None
    size: float = Field(default=0.0, ge=0)
    leverage: float = Field(default=1.0, ge=1)
    signal_id: str | None = Field(default=None, max_length=160)
    stop_loss: float | None = None
    take_profit: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    _normalize_symbol = field_validator("symbol")(normalize_symbol)

    @model_validator(mode="before")
    @classmethod
    def normalize_tradingview(cls, values: dict) -> dict:
        action = str(values.get("action", "")).lower()
        mp = str(values.get("market_position", "")).lower()
        if action in ("buy", "sell") and mp:
            if mp == "flat":
                values["action"] = "exit"
            elif mp in ("long", "short"):
                values["action"] = "entry"
                values["direction"] = mp
        return values


class ManualExitSignal(BaseModel):
    """A standalone exit trigger, independent of the strategy's own entry/exit
    logic - e.g. a TradingView alert on a hand-drawn trendline or a chosen
    price level. Closes whatever position is currently open for the given
    strategy/symbol, the same way the strategy's own exit signal would."""

    secret: str | None = None
    strategy: str = Field(..., min_length=1, max_length=120)
    symbol: str = Field(..., min_length=1, max_length=40)
    price: float | None = None

    _normalize_symbol = field_validator("symbol")(normalize_symbol)


class SignalStatusQuery(BaseModel):
    """Asks what became of a webhook sent with this signal_id, so a sender
    (the chart extension) can confirm the trade without opening the dashboard."""

    secret: str | None = None
    signal_id: str = Field(..., min_length=1, max_length=160)


class SecretQuery(BaseModel):
    """Webhook-secret auth for the chart extension's position calls, which
    can't use the dashboard's Google sign-in."""

    secret: str | None = None


class PositionAction(SecretQuery):
    trade_id: int


class PositionTpSlAction(PositionAction):
    take_profit: float | None = Field(default=None, gt=0)
    stop_loss: float | None = Field(default=None, gt=0)


class PositionStopAction(PositionAction):
    """A platform-watched stop price. None cancels it."""

    price: float | None = Field(default=None, gt=0)


class PositionTrailAction(PositionAction):
    """A per-trade trailing stop, in dollars of profit. None cancels it."""

    distance: float | None = Field(default=None, gt=0)


class PositionTpSl(BaseModel):
    """Dollar TP/SL for one open contract - profit/loss amounts, the unit
    Deriv's limit_order takes. None removes that side."""

    take_profit: float | None = Field(default=None, gt=0)
    stop_loss: float | None = Field(default=None, gt=0)


class PositionTpSlDetail(PositionTpSl):
    """PositionTpSl plus what the dialog needs to show and convert prices:
    the price Deriv says each side sits at, and the contract figures that
    turn a typed price into a dollar amount (and back)."""

    take_profit_price: float | None = None
    stop_loss_price: float | None = None
    entry_spot: float | None = None
    stake: float | None = None
    multiplier: float | None = None
    commission: float | None = None
    direction: str | None = None


class CloseAllTarget(BaseModel):
    """Dollar levels for the summed unrealized P/L of all open positions.
    None leaves that side unset. `repeat` keeps the target armed after it
    fires instead of clearing it."""

    take_profit: float | None = Field(default=None, gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    repeat: bool = False


class SignalBotCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    symbol: str | None = Field(default=None, max_length=200)
    size: float = Field(default=0.01, gt=0)
    # 0 (the default) means "inherit the per-symbol leverage set on the
    # Leverage page" - only set this to explicitly override it for this bot.
    leverage: float = Field(default=0.0, ge=0)
    hedge_mode: bool = False
    default_pair_tp_pct: float | None = Field(default=None, gt=0)
    default_pair_sl_pct: float | None = Field(default=None, gt=0)
    default_pair_max_cycles: int | None = Field(default=None, gt=0)
    trail_start_pct: float | None = Field(default=None, gt=0)
    trail_distance_pct: float | None = Field(default=None, gt=0)
    # "options" trades Deriv Multipliers sized by `size` (the stake);
    # "ctrader" trades lot-sized CFDs on the cTrader account, sized by `lots`.
    account: Literal["options", "ctrader"] = "options"
    lots: float | None = Field(default=None, gt=0)
    # CFD bots: dollars per trade, and cTrader's own trailing stop.
    tp_usd: float | None = Field(default=None, gt=0)
    sl_usd: float | None = Field(default=None, gt=0)
    trailing_stop: bool = False

    @model_validator(mode="after")
    def ctrader_needs_lots(self) -> "SignalBotCreate":
        if self.account == "ctrader" and not self.lots:
            raise ValueError("A cTrader bot needs a lot size.")
        if self.trailing_stop and not self.sl_usd:
            raise ValueError("A trailing stop needs a stop loss to trail.")
        return self


class SignalBotUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    symbol: str | None = Field(default=None, max_length=200)
    size: float | None = Field(default=None, gt=0)
    leverage: float | None = Field(default=None, ge=0)
    enabled: bool | None = None
    hedge_mode: bool | None = None
    default_pair_tp_pct: float | None = Field(default=None, gt=0)
    default_pair_sl_pct: float | None = Field(default=None, gt=0)
    default_pair_max_cycles: int | None = Field(default=None, gt=0)
    trail_start_pct: float | None = Field(default=None, gt=0)
    trail_distance_pct: float | None = Field(default=None, gt=0)
    account: Literal["options", "ctrader"] | None = None
    lots: float | None = Field(default=None, gt=0)
    tp_usd: float | None = Field(default=None, gt=0)
    sl_usd: float | None = Field(default=None, gt=0)
    trailing_stop: bool | None = None


class SymbolLeverageOut(BaseModel):
    symbol: str
    display_name: str
    leverage: int
    allowed_multipliers: list[int]


class SymbolLeverageUpdate(BaseModel):
    leverage: int = Field(..., ge=1)


class BotPairUpdate(BaseModel):
    tp_pct: float | None = Field(default=None, gt=0)
    sl_pct: float | None = Field(default=None, gt=0)
    max_cycles: int | None = Field(default=None, gt=0)
    enabled: bool | None = None


class BotPairOut(BaseModel):
    id: int
    bot_id: int
    symbol: str
    tp_pct: float | None
    sl_pct: float | None
    max_cycles: int | None
    cycles_completed: int
    session_pnl: float
    enabled: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class SignalBotOut(BaseModel):
    id: int
    name: str
    symbol: str | None
    size: float
    leverage: float
    enabled: bool
    hedge_mode: bool
    cycles_completed: int
    session_pnl: float
    default_pair_tp_pct: float | None
    default_pair_sl_pct: float | None
    default_pair_max_cycles: int | None
    trail_start_pct: float | None
    trail_distance_pct: float | None
    account: str = "options"
    lots: float | None = None
    tp_usd: float | None = None
    sl_usd: float | None = None
    trailing_stop: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}


class StrategyOut(BaseModel):
    id: int
    name: str
    enabled: bool
    max_position_size: float
    daily_loss_limit: float
    created_at: datetime

    model_config = {"from_attributes": True}


class SignalOut(BaseModel):
    id: int
    strategy_name: str
    symbol: str
    action: SignalAction
    direction: Direction | None
    price: float | None
    size: float
    leverage: float
    signal_id: str | None
    status: ExecutionStatus
    rejection_reason: str | None
    source: str | None
    execution_mode: str
    created_at: datetime

    model_config = {"from_attributes": True}


class TradeOut(BaseModel):
    id: int
    strategy_name: str
    symbol: str
    direction: Direction
    entry_price: float
    exit_price: float | None
    size: float
    leverage: float
    fees: float
    profit_loss: float
    net_result: float
    status: PositionStatus
    execution_status: ExecutionStatus
    exchange_order_id: str | None
    execution_mode: str
    opened_at: datetime
    closed_at: datetime | None
    close_at_price: float | None = None
    trail_distance: float | None = None
    manual_tpsl: bool | None = None
    close_reason: str | None = None
    # "options" (Multipliers) or "ctrader" (CFDs) - which Synthetic pages list it.
    account: str = "options"

    model_config = {"from_attributes": True}
