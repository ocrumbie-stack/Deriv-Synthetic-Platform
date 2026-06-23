import json
from dataclasses import dataclass
from datetime import datetime, time

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.bitget import BitgetClient, BitgetExecutionError
from app.config import settings
from app.models import ExecutionStatus, PositionStatus, RiskSettings, Signal, SignalAction, SignalBot, Strategy, Trade
from app.schemas import WebhookSignal


@dataclass
class ProcessedSignal:
    signal: Signal
    trade: Trade | None


def get_or_create_strategy(db: Session, name: str) -> Strategy:
    strategy = db.scalar(select(Strategy).where(Strategy.name == name))
    if strategy:
        return strategy

    strategy = Strategy(name=name, enabled=True)
    db.add(strategy)
    db.flush()
    return strategy


def get_risk_settings(db: Session) -> RiskSettings:
    risk = db.get(RiskSettings, 1)
    if risk:
        return risk

    risk = RiskSettings(
        id=1,
        emergency_stop=settings.emergency_stop,
        duplicate_blocking=True,
    )
    db.add(risk)
    db.flush()
    return risk


def daily_strategy_net(db: Session, strategy_id: int) -> float:
    start = datetime.combine(datetime.utcnow().date(), time.min)
    total = db.scalar(
        select(func.coalesce(func.sum(Trade.net_result), 0.0)).where(
            Trade.strategy_id == strategy_id,
            Trade.closed_at >= start,
        )
    )
    return float(total or 0.0)


def daily_account_net(db: Session) -> float:
    start = datetime.combine(datetime.utcnow().date(), time.min)
    total = db.scalar(
        select(func.coalesce(func.sum(Trade.net_result), 0.0)).where(
            Trade.closed_at >= start,
        )
    )
    return float(total or 0.0)


def account_exposure(db: Session) -> float:
    total = db.scalar(
        select(func.coalesce(func.sum(Trade.size * Trade.leverage), 0.0)).where(
            Trade.status == PositionStatus.open,
        )
    )
    return float(total or 0.0)


def get_signal_bot(db: Session, name: str) -> SignalBot | None:
    return db.scalar(select(SignalBot).where(SignalBot.name == name))


def update_bot_session(db: Session, bot: SignalBot, trade_net: float) -> str | None:
    bot.session_pnl = round((bot.session_pnl or 0.0) + trade_net, 8)
    bot.cycles_completed = (bot.cycles_completed or 0) + 1

    target_base = bot.size if bot.size > 0 else 1.0
    net_pct = (bot.session_pnl / target_base) * 100
    reason = None

    if bot.tp_pct and net_pct >= bot.tp_pct:
        reason = f"Take profit target reached ({net_pct:.1f}% of allocated size)"
    elif bot.sl_pct and net_pct <= -abs(bot.sl_pct):
        reason = f"Stop loss target reached ({net_pct:.1f}% of allocated size)"
    elif bot.max_cycles and bot.cycles_completed >= bot.max_cycles:
        reason = f"Cycle limit reached ({bot.cycles_completed}/{bot.max_cycles} cycles)"

    if reason:
        bot.enabled = False

    db.flush()
    return reason


def find_open_trade(db: Session, strategy_id: int, symbol: str) -> Trade | None:
    return db.scalar(
        select(Trade).where(
            Trade.strategy_id == strategy_id,
            Trade.symbol == symbol,
            Trade.status == PositionStatus.open,
        )
    )


def validate_signal(db: Session, payload: WebhookSignal, strategy: Strategy, bot: SignalBot | None = None) -> str | None:
    risk = get_risk_settings(db)
    if payload.secret != settings.webhook_secret:
        return "Invalid webhook secret."
    if settings.emergency_stop or risk.emergency_stop:
        return "Emergency stop is active."
    if bot and not bot.enabled:
        return "Signal bot is disabled."
    if bot and bot.symbol:
        allowed = [s.strip().upper() for s in bot.symbol.split(",")]
        if payload.symbol.upper() not in allowed:
            return f"Symbol {payload.symbol} is not in this bot's allowed list ({bot.symbol})."
    if not strategy.enabled:
        return "Strategy is disabled."
    if risk.duplicate_blocking and payload.signal_id:
        duplicate = db.scalar(select(Signal.id).where(Signal.signal_id == payload.signal_id))
        if duplicate:
            return "Duplicate signal_id."
    if payload.action == SignalAction.entry:
        if payload.direction is None:
            return "Entry signals require direction."
        if payload.size <= 0:
            return "Entry signals require size greater than zero."
        if strategy.max_position_size and payload.size > strategy.max_position_size:
            return "Signal size exceeds the strategy maximum position size."
        if risk.account_daily_loss_limit and daily_account_net(db) <= -abs(risk.account_daily_loss_limit):
            return "Account daily loss limit has been reached."
        if strategy.daily_loss_limit and daily_strategy_net(db, strategy.id) <= -abs(strategy.daily_loss_limit):
            return "Strategy daily loss limit has been reached."
        projected_exposure = account_exposure(db) + (payload.size * payload.leverage)
        if risk.max_account_exposure and projected_exposure > risk.max_account_exposure:
            return "Signal would exceed max account exposure."
        if find_open_trade(db, strategy.id, payload.symbol):
            return "An open position already exists for this strategy and symbol."
    if payload.action == SignalAction.exit and not find_open_trade(db, strategy.id, payload.symbol):
        return "No open position exists for this strategy and symbol."
    return None


def calculate_trade_result(trade: Trade, exit_price: float) -> tuple[float, float]:
    if trade.direction.value == "long":
        gross = (exit_price - trade.entry_price) * trade.size
    else:
        gross = (trade.entry_price - exit_price) * trade.size
    net = gross - trade.fees
    return gross, net


_configured_symbols: set[tuple[str, int, bool]] = set()  # (symbol, leverage, hedge_mode)


async def process_webhook_signal(db: Session, payload: WebhookSignal) -> ProcessedSignal:
    strategy = get_or_create_strategy(db, payload.strategy)
    bot = get_signal_bot(db, payload.strategy)
    if bot:
        if payload.action == SignalAction.entry and (not payload.price or payload.price <= 0):
            # Can't size the position without a price — the TradingView template always sends {{close}}
            rejection = "Bot entry signals must include price for USDT-to-contracts conversion."
            signal = Signal(
                strategy_id=strategy.id, strategy_name=strategy.name,
                symbol=payload.symbol.upper(), action=payload.action,
                direction=payload.direction, price=payload.price,
                size=payload.size, leverage=payload.leverage,
                signal_id=payload.signal_id,
                status=ExecutionStatus.rejected, rejection_reason=rejection,
                raw_payload=json.dumps(payload.model_dump(mode="json")),
            )
            db.add(signal)
            db.commit()
            db.refresh(signal)
            return ProcessedSignal(signal=signal, trade=None)
        # bot.size is USDT margin; position contracts = (margin × leverage) / price
        if payload.price and payload.price > 0:
            size_contracts = round((bot.size * bot.leverage) / payload.price, 8)
        else:
            size_contracts = bot.size
        payload = payload.model_copy(update={"size": size_contracts, "leverage": bot.leverage})
    rejection = validate_signal(db, payload, strategy, bot)
    signal = Signal(
        strategy_id=strategy.id,
        strategy_name=strategy.name,
        symbol=payload.symbol.upper(),
        action=payload.action,
        direction=payload.direction,
        price=payload.price,
        size=payload.size,
        leverage=payload.leverage,
        signal_id=payload.signal_id,
        status=ExecutionStatus.rejected if rejection else ExecutionStatus.accepted,
        rejection_reason=rejection,
        raw_payload=json.dumps(payload.model_dump(mode="json")),
    )
    db.add(signal)
    db.flush()

    if rejection:
        db.commit()
        db.refresh(signal)
        return ProcessedSignal(signal=signal, trade=None)

    trade = None
    if payload.action == SignalAction.entry:
        trade = Trade(
            strategy_id=strategy.id,
            strategy_name=strategy.name,
            symbol=payload.symbol.upper(),
            direction=payload.direction,
            entry_price=payload.price or 0.0,
            size=payload.size,
            leverage=payload.leverage,
            execution_status=ExecutionStatus.accepted,
        )
        db.add(trade)
        db.flush()

        try:
            client = BitgetClient()
            hedge = bot.hedge_mode if bot else False
            cache_key = (payload.symbol, int(payload.leverage), hedge)
            if cache_key not in _configured_symbols:
                await client.set_position_mode(hedge)
                await client.set_leverage(payload.symbol, int(payload.leverage), hedge)
                _configured_symbols.add(cache_key)
            result = await client.place_order(payload, hedge_mode=hedge)
            trade.exchange_order_id = str(result.get("order_id") or result.get("data", {}).get("orderId") or "")
            trade.execution_status = ExecutionStatus.executed
            signal.status = ExecutionStatus.executed
        except BitgetExecutionError as exc:
            trade.execution_status = ExecutionStatus.failed
            signal.status = ExecutionStatus.failed
            signal.rejection_reason = str(exc)

    elif payload.action == SignalAction.exit:
        trade = find_open_trade(db, strategy.id, payload.symbol.upper())
        if trade:
            try:
                result = await BitgetClient().close_order(trade.symbol, trade.direction.value, trade.size, hedge_mode=bot.hedge_mode if bot else False)
                trade.exchange_order_id = str(result.get("order_id") or result.get("data", {}).get("orderId") or "")
            except BitgetExecutionError as exc:
                signal.status = ExecutionStatus.failed
                signal.rejection_reason = str(exc)
                db.commit()
                db.refresh(signal)
                return ProcessedSignal(signal=signal, trade=trade)
            exit_price = payload.price or trade.entry_price
            gross, net = calculate_trade_result(trade, exit_price)
            trade.exit_price = exit_price
            trade.profit_loss = gross
            trade.net_result = net
            trade.status = PositionStatus.closed
            trade.execution_status = ExecutionStatus.closed
            trade.closed_at = datetime.utcnow()
            signal.status = ExecutionStatus.closed
            if bot:
                update_bot_session(db, bot, net)

    db.commit()
    db.refresh(signal)
    if trade:
        db.refresh(trade)
    return ProcessedSignal(signal=signal, trade=trade)
