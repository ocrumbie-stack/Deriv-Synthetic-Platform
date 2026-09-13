import json
from dataclasses import dataclass
from datetime import datetime, time

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.deriv import DerivClient, DerivExecutionError
from app.models import BotPair, ExecutionStatus, PositionStatus, RiskSettings, Signal, SignalAction, SignalBot, Strategy, Trade
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
        select(func.coalesce(func.sum(Trade.size), 0.0)).where(
            Trade.status == PositionStatus.open,
        )
    )
    return float(total or 0.0)


def get_signal_bot(db: Session, name: str) -> SignalBot | None:
    return db.scalar(select(SignalBot).where(SignalBot.name == name))


def get_or_create_bot_pair(db: Session, bot: SignalBot, symbol: str) -> BotPair:
    pair = db.scalar(select(BotPair).where(BotPair.bot_id == bot.id, BotPair.symbol == symbol))
    if not pair:
        pair = BotPair(
            bot_id=bot.id,
            symbol=symbol,
            tp_pct=bot.default_pair_tp_pct,
            sl_pct=bot.default_pair_sl_pct,
            max_cycles=bot.default_pair_max_cycles,
        )
        db.add(pair)
        db.flush()
    return pair


def update_pair_session(db: Session, pair: BotPair, bot: SignalBot, trade_net: float) -> str | None:
    pair.session_pnl = round((pair.session_pnl or 0.0) + trade_net, 8)
    pair.cycles_completed = (pair.cycles_completed or 0) + 1
    target_base = bot.size if bot.size > 0 else 1.0
    net_pct = (pair.session_pnl / target_base) * 100
    reason = None
    if pair.tp_pct and net_pct >= pair.tp_pct:
        reason = f"Pair TP reached ({net_pct:.1f}%)"
    elif pair.sl_pct and net_pct <= -abs(pair.sl_pct):
        reason = f"Pair SL reached ({net_pct:.1f}%)"
    elif pair.max_cycles and pair.cycles_completed >= pair.max_cycles:
        reason = f"Pair cycle limit reached ({pair.cycles_completed}/{pair.max_cycles})"
    if reason:
        pair.enabled = False
    db.flush()
    return reason


async def arm_pair_tpsl(
    symbol: str,
    direction: str,
    entry_price: float,
    tp_pct: float | None,
    sl_pct: float | None,
    hedge_mode: bool = False,
) -> None:
    if not entry_price or (not tp_pct and not sl_pct):
        return
    is_long = direction == "long"
    tp_price = entry_price * (1 + tp_pct / 100) if tp_pct and is_long else (entry_price * (1 - tp_pct / 100) if tp_pct else None)
    sl_price = entry_price * (1 - sl_pct / 100) if sl_pct and is_long else (entry_price * (1 + sl_pct / 100) if sl_pct else None)
    await DerivClient().place_tpsl(symbol, direction, tp_price, sl_price, hedge_mode)


def update_bot_session(db: Session, bot: SignalBot, trade_net: float) -> None:
    bot.session_pnl = round((bot.session_pnl or 0.0) + trade_net, 8)
    bot.cycles_completed = (bot.cycles_completed or 0) + 1
    db.flush()


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
    if bot and payload.action == SignalAction.entry:
        pair = db.scalar(select(BotPair).where(BotPair.bot_id == bot.id, BotPair.symbol == payload.symbol))
        if pair and not pair.enabled:
            return f"{payload.symbol} is paused for this bot."

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
        projected_exposure = account_exposure(db) + payload.size
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


async def process_webhook_signal(db: Session, payload: WebhookSignal) -> ProcessedSignal:
    strategy = get_or_create_strategy(db, payload.strategy)
    bot = get_signal_bot(db, payload.strategy)
    if bot:
        # Deriv contracts use a stake amount. TradingView's price is recorded for
        # analytics, but it does not determine the stake or contract quantity.
        payload = payload.model_copy(update={"size": bot.size, "leverage": 1.0})
    else:
        payload = payload.model_copy(update={"leverage": 1.0})
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
            client = DerivClient()
            hedge = bot.hedge_mode if bot else False
            result = await client.place_order(payload, hedge_mode=hedge)
            trade.exchange_order_id = str(result.get("order_id") or result.get("data", {}).get("orderId") or "")
            trade.execution_status = ExecutionStatus.executed
            signal.status = ExecutionStatus.executed
            # Arm pair-level TP/SL on Deriv if configured
            if bot and payload.direction and payload.price:
                pair = get_or_create_bot_pair(db, bot, payload.symbol)
                if pair.tp_pct or pair.sl_pct:
                    try:
                        await arm_pair_tpsl(
                            payload.symbol, payload.direction.value, payload.price,
                            pair.tp_pct, pair.sl_pct, hedge,
                        )
                    except DerivExecutionError as exc:
                        signal.rejection_reason = (signal.rejection_reason or "") + f" | TP/SL arm failed: {exc}"
        except DerivExecutionError as exc:
            trade.execution_status = ExecutionStatus.failed
            trade.status = PositionStatus.closed
            trade.closed_at = datetime.utcnow()
            signal.status = ExecutionStatus.failed
            signal.rejection_reason = str(exc)

    elif payload.action == SignalAction.exit:
        trade = find_open_trade(db, strategy.id, payload.symbol.upper())
        if trade:
            try:
                result = await DerivClient().close_order(trade.symbol, trade.direction.value, trade.size, hedge_mode=bot.hedge_mode if bot else False)
                if result.get("message") != "no_position":
                    trade.exchange_order_id = str(result.get("order_id") or result.get("data", {}).get("orderId") or "")
            except DerivExecutionError as exc:
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
                pair = get_or_create_bot_pair(db, bot, trade.symbol)
                update_pair_session(db, pair, bot, net)
                update_bot_session(db, bot, net)

    db.commit()
    db.refresh(signal)
    if trade:
        db.refresh(trade)
    return ProcessedSignal(signal=signal, trade=trade)
