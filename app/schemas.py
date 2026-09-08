from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.models import Direction, ExecutionStatus, PositionStatus, SignalAction


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


class SignalBotCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    symbol: str | None = Field(default=None, max_length=200)
    size: float = Field(default=0.01, gt=0)
    leverage: float = Field(default=1.0, ge=1)
    hedge_mode: bool = False
    default_pair_tp_pct: float | None = Field(default=None, gt=0)
    default_pair_sl_pct: float | None = Field(default=None, gt=0)
    default_pair_max_cycles: int | None = Field(default=None, gt=0)


class SignalBotUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    symbol: str | None = Field(default=None, max_length=200)
    size: float | None = Field(default=None, gt=0)
    leverage: float | None = Field(default=None, ge=1)
    enabled: bool | None = None
    hedge_mode: bool | None = None
    default_pair_tp_pct: float | None = Field(default=None, gt=0)
    default_pair_sl_pct: float | None = Field(default=None, gt=0)
    default_pair_max_cycles: int | None = Field(default=None, gt=0)


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
    opened_at: datetime
    closed_at: datetime | None
    model_config = {"from_attributes": True}
