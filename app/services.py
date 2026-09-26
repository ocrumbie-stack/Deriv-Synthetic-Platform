import json
import logging
from dataclasses import dataclass
from datetime import datetime, time

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.config import ENV_EXECUTION_MODE, settings
from app.deriv import DerivClient, DerivExecutionError
from app.models import BotPair, ExecutionStatus, PositionStatus, RiskSettings, Signal, SignalAction, SignalBot, Strategy, SymbolLeverage, Trade
from app.schemas import ManualExitSignal, WebhookSignal

logger = logging.getLogger("uvicorn.error")

# A standard Deriv Multiplier contract's loss is capped at the stake by
# Deriv's own automatic stop-out (barring a rare gap-through-stop-out on a
# violent price jump). A close_order response has been observed to report a
# wildly implausible "profit" for one contract (a $149 loss on a $7 stake -
# 213x - when the real ledger showed -$0.41), which silently corrupted that
# trade's and its bot's session P&L. Anything beyond this multiple of the
# stake is treated as an untrustworthy API response rather than real.
IMPLAUSIBLE_PROFIT_STAKE_MULTIPLE = 10


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
    if not risk:
        risk = RiskSettings(
            id=1,
            emergency_stop=settings.emergency_stop,
            duplicate_blocking=True,
        )
        db.add(risk)
        db.flush()
    # The dashboard's execution-mode toggle is stored here rather than in
    # .env, so every place that reads settings.execution_mode needs it
    # applied here rather than threading a db session through DerivClient.
    settings.execution_mode = risk.execution_mode_override or ENV_EXECUTION_MODE
    return risk


def daily_strategy_net(db: Session, strategy_id: int) -> float:
    start = datetime.combine(datetime.utcnow().date(), time.min)
    total = db.scalar(
        select(func.coalesce(func.sum(Trade.net_result), 0.0)).where(
            Trade.strategy_id == strategy_id,
            Trade.closed_at >= start,
            Trade.execution_mode == settings.execution_mode.lower(),
        )
    )
    return float(total or 0.0)


def daily_account_net(db: Session) -> float:
    """Scoped to the currently active execution mode - a bad demo test run
    must never trip the live daily loss limit, or vice versa."""
    start = datetime.combine(datetime.utcnow().date(), time.min)
    total = db.scalar(
        select(func.coalesce(func.sum(Trade.net_result), 0.0)).where(
            Trade.closed_at >= start,
            Trade.execution_mode == settings.execution_mode.lower(),
        )
    )
    return float(total or 0.0)


def account_exposure(db: Session) -> float:
    """Scoped to the currently active execution mode, for the same reason
    as daily_account_net."""
    total = db.scalar(
        select(func.coalesce(func.sum(Trade.size), 0.0)).where(
            Trade.status == PositionStatus.open,
            Trade.execution_mode == settings.execution_mode.lower(),
        )
    )
    return float(total or 0.0)


def get_signal_bot(db: Session, name: str) -> SignalBot | None:
    return db.scalar(select(SignalBot).where(SignalBot.name == name))


def get_symbol_leverage(db: Session, symbol: str) -> int | None:
    row = db.scalar(select(SymbolLeverage).where(SymbolLeverage.symbol == symbol))
    return row.leverage if row else None


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
    # Size and leverage are always platform-controlled (from the bot's own
    # settings, or the Leverage page) - a webhook signal only ever triggers
    # execution, never dictates sizing. That requires a bot to exist for
    # every strategy that's allowed to open a position.
    if payload.action == SignalAction.entry and bot is None:
        return "No signal bot is configured for this strategy - create one on the Signal Bots page before it can execute entries."
    # Pausing a bot/strategy must only stop it opening new positions - it
    # must never trap an already-open position by also blocking the
    # strategy's own exit signal for it.
    if bot and not bot.enabled and payload.action == SignalAction.entry:
        return "Signal bot is disabled."
    if bot and bot.symbol:
        allowed = [s.strip().upper() for s in bot.symbol.split(",")]
        if payload.symbol.upper() not in allowed:
            return f"Symbol {payload.symbol} is not in this bot's allowed list ({bot.symbol})."
    if bot and payload.action == SignalAction.entry:
        pair = db.scalar(select(BotPair).where(BotPair.bot_id == bot.id, BotPair.symbol == payload.symbol))
        if pair and not pair.enabled:
            return f"{payload.symbol} is paused for this bot."

    if not strategy.enabled and payload.action == SignalAction.entry:
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
        existing = find_open_trade(db, strategy.id, payload.symbol)
        # An entry in the same direction as an already-open position is a
        # duplicate/pyramiding attempt and stays rejected. An entry in the
        # opposite direction is a reversal - process_webhook_signal handles
        # it by closing the existing position first, so the strategy only
        # ever needs to send plain entry/exit signals, never an explicit
        # close before flipping.
        if existing and existing.direction == payload.direction:
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


async def get_live_unrealized_pnl(db: Session) -> dict[str, float]:
    """Per-open-trade unrealized P&L, queried directly per contract_id.

    Deriv's portfolio enumeration (used by DerivClient.get_positions) has
    been observed to silently omit contracts that are genuinely still open,
    which would make this look empty even with real positions running. A
    direct proposal_open_contract lookup per known contract_id is reliable.
    """
    client = DerivClient()
    open_trades = list(db.scalars(select(Trade).where(Trade.status == PositionStatus.open)))
    result: dict[str, float] = {}
    for trade in open_trades:
        if not trade.exchange_order_id:
            continue
        poc = await client.get_contract_status(trade.exchange_order_id)
        if not poc:
            continue
        result[f"{trade.symbol}_{trade.direction.value}"] = round(float(poc.get("profit") or 0), 8)
    return result


async def reconcile_open_trade(db: Session, trade: Trade) -> bool:
    """Close a DB trade that Deriv has already sold/stopped-out.

    We only learn a trade closed when a matching exit webhook arrives from
    TradingView. If Deriv's own guaranteed stop-out closed the contract first,
    the trade would otherwise stay "open" here forever and block every new
    entry signal for that strategy/symbol. Returns True if the trade was
    closed as a result of this check.
    """
    if not trade.exchange_order_id:
        return False
    poc = await DerivClient().get_contract_status(trade.exchange_order_id)
    if not poc or not poc.get("is_sold"):
        return False

    profit = float(poc.get("profit") or 0)
    sell_time = poc.get("sell_time")
    trade.exit_price = float(poc.get("sell_spot") or poc.get("current_spot") or trade.entry_price)
    trade.profit_loss = profit
    trade.net_result = profit - trade.fees
    trade.status = PositionStatus.closed
    trade.execution_status = ExecutionStatus.closed
    trade.closed_at = datetime.utcfromtimestamp(float(sell_time)) if sell_time else datetime.utcnow()
    db.flush()
    return True


async def close_trade(db: Session, trade: Trade, exit_price_hint: float | None, bot: SignalBot | None) -> dict:
    """Close an open trade on Deriv and update its record - shared by an
    explicit exit signal and an entry that reverses an opposite position.

    Raises DerivExecutionError on failure; callers decide how to record that.
    """
    # Target the exact contract we already recorded rather than searching
    # Deriv's portfolio by symbol/direction - that search has been observed
    # to silently miss genuinely open contracts, which would wrongly
    # conclude "no position" and leave the real contract running, untracked.
    result = await DerivClient().close_order(trade.exchange_order_id)
    if result.get("message") not in ("no_position", "already_sold"):
        trade.exchange_order_id = str(result.get("order_id") or result.get("data", {}).get("orderId") or "")

    exit_price = exit_price_hint or trade.entry_price
    # Use Deriv's own reported P&L for the contract rather than recomputing
    # it from raw underlying prices, which don't share a unit with the
    # dollar stake - unless it's implausibly large relative to the stake,
    # which means the API response itself was bad rather than the trade
    # actually moving that much (see IMPLAUSIBLE_PROFIT_STAKE_MULTIPLE).
    live_profit = result.get("profit")
    stake_bound = max(trade.size, 1.0) * IMPLAUSIBLE_PROFIT_STAKE_MULTIPLE
    if isinstance(live_profit, (int, float)):
        if abs(live_profit) <= stake_bound:
            gross = float(live_profit)
        else:
            # Trust the sign, distrust the magnitude, and clamp rather than
            # falling back to calculate_trade_result - that formula ignores
            # the contract's multiplier entirely, so for a Multiplier
            # contract it would likely reproduce an equally wrong number
            # instead of a trustworthy one. This still needs a human to
            # reconcile the real figure against Deriv's own statement.
            logger.warning(
                "Deriv reported an implausible profit (%.2f) for trade %s (contract %s, stake %.2f) "
                "- exceeds %sx stake, clamping to +/-%.2f instead. Needs manual review against Deriv's statement.",
                live_profit, trade.id, trade.exchange_order_id, trade.size, IMPLAUSIBLE_PROFIT_STAKE_MULTIPLE, stake_bound,
            )
            gross = stake_bound if live_profit > 0 else -stake_bound
        net = gross - trade.fees
    else:
        gross, net = calculate_trade_result(trade, exit_price)
    trade.exit_price = exit_price
    trade.profit_loss = gross
    trade.net_result = net
    trade.status = PositionStatus.closed
    trade.execution_status = ExecutionStatus.closed
    trade.closed_at = datetime.utcnow()
    if bot:
        pair = get_or_create_bot_pair(db, bot, trade.symbol)
        update_pair_session(db, pair, bot, net)
        update_bot_session(db, bot, net)
    return result


async def process_webhook_signal(db: Session, payload: WebhookSignal) -> ProcessedSignal:
    strategy = get_or_create_strategy(db, payload.strategy)
    bot = get_signal_bot(db, payload.strategy)
    if bot:
        # Deriv contracts use a stake amount. TradingView's price is recorded for
        # analytics, but it does not determine the stake or contract quantity.
        payload = payload.model_copy(update={"size": bot.size})

    # A bot's own leverage (when explicitly set above 0) overrides the
    # platform default; otherwise every signal - bot or not - inherits
    # whatever's configured on the Leverage page for this symbol.
    if bot and bot.leverage > 0:
        payload = payload.model_copy(update={"leverage": bot.leverage})
    else:
        symbol_leverage = get_symbol_leverage(db, payload.symbol.upper())
        if symbol_leverage is not None:
            payload = payload.model_copy(update={"leverage": symbol_leverage})

    if payload.action == SignalAction.entry:
        existing = find_open_trade(db, strategy.id, payload.symbol.upper())
        if existing:
            await reconcile_open_trade(db, existing)

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
        execution_mode=settings.execution_mode.lower(),
    )
    db.add(signal)
    db.flush()

    if rejection:
        db.commit()
        db.refresh(signal)
        return ProcessedSignal(signal=signal, trade=None)

    trade = None
    if payload.action == SignalAction.entry:
        # validate_signal only lets an entry through here while a position
        # is already open when that position is in the opposite direction
        # (a reversal) - close it first so the strategy never has to send
        # an explicit exit of its own before flipping.
        opposite = find_open_trade(db, strategy.id, payload.symbol.upper())
        if opposite:
            try:
                await close_trade(db, opposite, payload.price, bot)
            except DerivExecutionError as exc:
                signal.status = ExecutionStatus.failed
                signal.rejection_reason = f"Failed to close opposite position before reversing: {exc}"
                db.commit()
                db.refresh(signal)
                return ProcessedSignal(signal=signal, trade=opposite)
            # The reversal closed a position, but the Signal row being built in
            # this call is for the new entry - log the close itself too, so the
            # Signal Journal shows why the old position ended.
            db.add(
                Signal(
                    strategy_id=strategy.id,
                    strategy_name=strategy.name,
                    symbol=payload.symbol.upper(),
                    action=SignalAction.exit,
                    price=payload.price,
                    status=ExecutionStatus.closed,
                    source="reversal",
                    raw_payload=json.dumps(payload.model_dump(mode="json")),
                    execution_mode=settings.execution_mode.lower(),
                )
            )

        trade = Trade(
            strategy_id=strategy.id,
            strategy_name=strategy.name,
            symbol=payload.symbol.upper(),
            direction=payload.direction,
            entry_price=payload.price or 0.0,
            size=payload.size,
            leverage=payload.leverage,
            execution_status=ExecutionStatus.accepted,
            execution_mode=settings.execution_mode.lower(),
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
            signal.source = "strategy_exit"
            try:
                await close_trade(db, trade, payload.price, bot)
                signal.status = ExecutionStatus.closed
            except DerivExecutionError as exc:
                signal.status = ExecutionStatus.failed
                signal.rejection_reason = str(exc)
                db.commit()
                db.refresh(signal)
                return ProcessedSignal(signal=signal, trade=trade)

    db.commit()
    db.refresh(signal)
    if trade:
        db.refresh(trade)
    return ProcessedSignal(signal=signal, trade=trade)


async def process_manual_exit(db: Session, payload: ManualExitSignal) -> ProcessedSignal:
    """Close whatever position is open for this strategy/symbol, triggered
    independently of the strategy's own exit signal (e.g. a TradingView alert
    on a hand-drawn trendline or a chosen price level).

    Deliberately skips validate_signal's strategy/bot-enabled and emergency
    stop checks - this is a discretionary "flatten if needed" escape hatch,
    not a strategy action, so it must still work while the strategy or bot is
    paused or the emergency stop is on.
    """
    get_risk_settings(db)
    mode = settings.execution_mode.lower()
    symbol = payload.symbol.upper()

    rejection = None
    if payload.secret != settings.webhook_secret:
        rejection = "Invalid webhook secret."

    trade = None
    if not rejection:
        trade = db.scalar(
            select(Trade).where(
                Trade.strategy_name == payload.strategy,
                Trade.symbol == symbol,
                Trade.status == PositionStatus.open,
                Trade.execution_mode == mode,
            )
        )
        if not trade:
            rejection = "No open position exists for this strategy and symbol."

    signal = Signal(
        strategy_id=trade.strategy_id if trade else None,
        strategy_name=payload.strategy,
        symbol=symbol,
        action=SignalAction.exit,
        price=payload.price,
        status=ExecutionStatus.rejected if rejection else ExecutionStatus.accepted,
        rejection_reason=rejection,
        source="webhook_exit",
        raw_payload=json.dumps(payload.model_dump(mode="json")),
        execution_mode=mode,
    )
    db.add(signal)
    db.flush()

    if rejection:
        db.commit()
        db.refresh(signal)
        return ProcessedSignal(signal=signal, trade=None)

    bot = get_signal_bot(db, payload.strategy)
    try:
        await close_trade(db, trade, payload.price, bot)
        signal.status = ExecutionStatus.closed
    except DerivExecutionError as exc:
        signal.status = ExecutionStatus.failed
        signal.rejection_reason = str(exc)

    db.commit()
    db.refresh(signal)
    db.refresh(trade)
    return ProcessedSignal(signal=signal, trade=trade)
