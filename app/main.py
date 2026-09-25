import logging
import os
import re
from datetime import datetime, time, timedelta

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import ENV_EXECUTION_MODE, deriv_credential_names, settings
from app.database import Base, SessionLocal, engine, get_db, sync_schema
from app.deriv import DerivClient, DerivExecutionError
from app.models import BotPair, ExecutionStatus, PositionStatus, RiskSettings, Signal, SignalBot, Strategy, SymbolLeverage, Trade
from app.schemas import BotPairOut, BotPairUpdate, ManualExitSignal, SignalBotCreate, SignalBotOut, SignalBotUpdate, SignalOut, StrategyOut, SymbolLeverageOut, SymbolLeverageUpdate, TradeOut, WebhookSignal
from app.services import account_exposure, arm_pair_tpsl, close_trade, daily_account_net, get_live_unrealized_pnl, get_risk_settings, get_signal_bot, process_manual_exit, process_webhook_signal, reconcile_open_trade


logger = logging.getLogger("uvicorn.error")

Base.metadata.create_all(bind=engine)
sync_schema()

# Failed exchange requests must not remain as open platform positions after a restart.
with SessionLocal() as startup_db:
    startup_db.execute(
        update(Trade)
        .where(Trade.execution_status == ExecutionStatus.failed, Trade.status != PositionStatus.closed)
        .values(status=PositionStatus.closed, closed_at=datetime.utcnow())
    )
    # Apply any dashboard-toggled execution mode saved from a previous run,
    # since settings.execution_mode otherwise only reflects .env on boot.
    get_risk_settings(startup_db)
    startup_db.commit()

app = FastAPI(title=settings.app_name)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


def period_start(period: str) -> datetime | None:
    now = datetime.utcnow()
    today = datetime.combine(now.date(), time.min)
    if period == "today":
        return today
    if period == "week":
        return today - timedelta(days=today.weekday())
    if period == "month":
        return today.replace(day=1)
    if period == "all":
        return None
    raise HTTPException(status_code=400, detail="Unsupported period. Use today, week, month, or all.")


@app.get("/")
def dashboard() -> HTMLResponse:
    # Force browsers to fetch the current app.js instead of a stale cached
    # copy after a deploy, by stamping the script tag with app.js's own
    # mtime instead of a hand-maintained version number that's easy to
    # forget to bump.
    html_path = "app/static/index.html"
    app_js_path = "app/static/app.js"
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    version = int(os.path.getmtime(app_js_path))
    html = re.sub(r'(/static/app\.js)(\?v=\d+)?"', rf'\1?v={version}"', html)
    return HTMLResponse(
        content=html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "execution_mode": settings.execution_mode}


@app.get("/api/config")
def config() -> dict[str, str]:
    return {"webhook_secret": settings.webhook_secret}


async def _process_in_background(payload: WebhookSignal) -> None:
    db = SessionLocal()
    try:
        await process_webhook_signal(db, payload)
    except Exception:
        db.rollback()
        logger.exception("Failed to process webhook signal: %s", payload.model_dump(mode="json"))
    finally:
        db.close()


@app.post("/webhook", status_code=202)
async def receive_webhook(payload: WebhookSignal, background_tasks: BackgroundTasks) -> dict:
    # Normalise symbol — TradingView appends .P / .PERP for perpetuals
    symbol = payload.symbol.upper()
    for suffix in (".P", ".PERP", ".USD", "-PERP", "-USD"):
        if symbol.endswith(suffix):
            symbol = symbol[: -len(suffix)]
            break
    payload.symbol = symbol
    background_tasks.add_task(_process_in_background, payload)
    return {"status": "received"}


async def _process_exit_in_background(payload: ManualExitSignal) -> None:
    db = SessionLocal()
    try:
        await process_manual_exit(db, payload)
    except Exception:
        db.rollback()
        logger.exception("Failed to process manual exit webhook: %s", payload.model_dump(mode="json"))
    finally:
        db.close()


@app.post("/webhook/exit", status_code=202)
async def receive_exit_webhook(payload: ManualExitSignal, background_tasks: BackgroundTasks) -> dict:
    # A standalone exit trigger (e.g. a TradingView alert on a hand-drawn
    # trendline or a chosen price level) - independent of the strategy's own
    # entry/exit alert, so it needs its own webhook rather than piggybacking
    # on /webhook's action field.
    symbol = payload.symbol.upper()
    for suffix in (".P", ".PERP", ".USD", "-PERP", "-USD"):
        if symbol.endswith(suffix):
            symbol = symbol[: -len(suffix)]
            break
    payload.symbol = symbol
    background_tasks.add_task(_process_exit_in_background, payload)
    return {"status": "received"}


@app.get("/api/unrealized-pnl")
async def unrealized_pnl(db: Session = Depends(get_db)) -> dict:
    return await get_live_unrealized_pnl(db)


@app.get("/api/symbols")
async def list_symbols() -> dict:
    catalog = await DerivClient().get_symbol_catalog()
    symbols = sorted(
        (
            {"symbol": item.get("underlying_symbol"), "display_name": item.get("underlying_symbol_name")}
            for item in catalog
            if item.get("underlying_symbol")
        ),
        key=lambda row: row["symbol"],
    )
    return {"symbols": symbols, "error": None if symbols else DerivClient._symbol_catalog_error}


_SYNTHETIC_INDEX_PREFIXES = ("R_", "1HZ", "BOOM", "CRASH", "JD", "STPRNG", "RB", "RDBEAR", "RDBULL")


def _is_synthetic_index(code: str) -> bool:
    return code.upper().startswith(_SYNTHETIC_INDEX_PREFIXES)


@app.get("/api/symbol-leverage", response_model=list[SymbolLeverageOut])
async def list_symbol_leverage(db: Session = Depends(get_db)) -> list[SymbolLeverageOut]:
    client = DerivClient()
    catalog = await client.get_symbol_catalog()  # cached after the first call, not the bottleneck
    rows_by_symbol = {row.symbol: row for row in db.scalars(select(SymbolLeverage))}
    out: list[SymbolLeverageOut] = []
    dirty = False

    for item in catalog:
        code = str(item.get("underlying_symbol") or "")
        if not code or not _is_synthetic_index(code):
            continue

        row = rows_by_symbol.get(code)
        if row and row.allowed_multipliers:
            # Fast path: already known, no Deriv round trip needed - this is
            # what keeps this endpoint fast after every restart, instead of
            # re-fetching all ~39 symbols from Deriv on every cold start.
            allowed = [int(v) for v in row.allowed_multipliers.split(",")]
        else:
            allowed = await client.get_multiplier_range(code, "MULTUP")
            if not allowed:
                continue  # Multipliers aren't offered on this symbol at all (e.g. RDBEAR/RDBULL).
            if not row:
                row = SymbolLeverage(symbol=code, leverage=min(allowed))
                db.add(row)
            row.allowed_multipliers = ",".join(str(v) for v in allowed)
            dirty = True

        out.append(
            SymbolLeverageOut(
                symbol=code,
                display_name=str(item.get("underlying_symbol_name") or code),
                leverage=row.leverage,
                allowed_multipliers=allowed,
            )
        )
    if dirty:
        db.commit()
    return sorted(out, key=lambda row: min(row.allowed_multipliers))


@app.patch("/api/symbol-leverage/{symbol}", response_model=SymbolLeverageOut)
async def update_symbol_leverage(symbol: str, updates: SymbolLeverageUpdate, db: Session = Depends(get_db)) -> SymbolLeverageOut:
    client = DerivClient()
    try:
        code = await client.resolve_symbol(symbol)
    except DerivExecutionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    catalog = await client.get_symbol_catalog()
    match = next((item for item in catalog if item.get("underlying_symbol") == code), None)

    row = db.scalar(select(SymbolLeverage).where(SymbolLeverage.symbol == code))
    if row and row.allowed_multipliers:
        allowed = [int(v) for v in row.allowed_multipliers.split(",")]
    else:
        allowed = await client.get_multiplier_range(code, "MULTUP")

    if allowed and updates.leverage not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"{code} only accepts a multiplier of {', '.join(str(v) for v in allowed)}.",
        )

    if not row:
        row = SymbolLeverage(symbol=code, leverage=updates.leverage)
        db.add(row)
    else:
        row.leverage = updates.leverage
    if allowed:
        row.allowed_multipliers = ",".join(str(v) for v in allowed)
    db.commit()
    db.refresh(row)
    return SymbolLeverageOut(
        symbol=code,
        display_name=str((match or {}).get("underlying_symbol_name") or code),
        leverage=row.leverage,
        allowed_multipliers=allowed,
    )


@app.get("/api/signal-bots/{bot_id}/pairs", response_model=list[BotPairOut])
def list_bot_pairs(bot_id: int, db: Session = Depends(get_db)) -> list[BotPair]:
    if not db.get(SignalBot, bot_id):
        raise HTTPException(status_code=404, detail="Bot not found.")
    return list(db.scalars(select(BotPair).where(BotPair.bot_id == bot_id).order_by(BotPair.symbol)))


@app.patch("/api/signal-bots/{bot_id}/pairs/{symbol}", response_model=BotPairOut)
async def update_bot_pair(bot_id: int, symbol: str, updates: BotPairUpdate, db: Session = Depends(get_db)) -> BotPair:
    bot = db.get(SignalBot, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found.")
    sym = symbol.upper()
    pair = db.scalar(select(BotPair).where(BotPair.bot_id == bot_id, BotPair.symbol == sym))
    if not pair:
        raise HTTPException(status_code=404, detail="Pair not found.")
    if updates.enabled is True and not pair.enabled:
        pair.session_pnl = 0.0
        pair.cycles_completed = 0
    for field, value in updates.model_dump(exclude_unset=True).items():
        setattr(pair, field, value)
    db.commit()
    db.refresh(pair)
    # Arm TP/SL on Deriv if an open trade exists for this pair
    open_trade = db.scalar(
        select(Trade).where(
            Trade.strategy_name == bot.name,
            Trade.symbol == sym,
            Trade.status == PositionStatus.open,
            Trade.execution_mode == settings.execution_mode.lower(),
        )
    )
    if open_trade and (pair.tp_pct or pair.sl_pct):
        await arm_pair_tpsl(
            sym, open_trade.direction.value, open_trade.entry_price,
            pair.tp_pct, pair.sl_pct, bot.hedge_mode,
        )
    return pair


@app.get("/api/signal-bots", response_model=list[SignalBotOut])
def list_signal_bots(db: Session = Depends(get_db)) -> list[SignalBot]:
    return list(db.scalars(select(SignalBot).order_by(SignalBot.name)))


@app.post("/api/signal-bots", response_model=SignalBotOut, status_code=201)
def create_signal_bot(data: SignalBotCreate, db: Session = Depends(get_db)) -> SignalBot:
    if db.scalar(select(SignalBot).where(SignalBot.name == data.name)):
        raise HTTPException(status_code=409, detail="A bot with this name already exists.")
    bot = SignalBot(**data.model_dump())
    db.add(bot)
    db.commit()
    db.refresh(bot)
    return bot


@app.patch("/api/signal-bots/{bot_id}", response_model=SignalBotOut)
def update_signal_bot(bot_id: int, updates: SignalBotUpdate, db: Session = Depends(get_db)) -> SignalBot:
    bot = db.get(SignalBot, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="Signal bot not found.")
    data = updates.model_dump(exclude_unset=True)
    if data.get("enabled") is True and not bot.enabled:
        bot.session_pnl = 0.0
        bot.cycles_completed = 0
    for field, value in data.items():
        setattr(bot, field, value)
    db.commit()
    db.refresh(bot)
    return bot


@app.delete("/api/signal-bots/{bot_id}")
def delete_signal_bot(bot_id: int, db: Session = Depends(get_db)) -> dict:
    bot = db.get(SignalBot, bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="Signal bot not found.")
    db.delete(bot)
    db.commit()
    return {"deleted": True}


@app.get("/api/strategies", response_model=list[StrategyOut])
def list_strategies(db: Session = Depends(get_db)) -> list[Strategy]:
    return list(db.scalars(select(Strategy).order_by(Strategy.name)))


@app.patch("/api/strategies/{strategy_id}", response_model=StrategyOut)
def update_strategy(strategy_id: int, updates: dict, db: Session = Depends(get_db)) -> Strategy:
    strategy = db.get(Strategy, strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")
    for field in ("enabled", "max_position_size", "daily_loss_limit"):
        if field in updates:
            setattr(strategy, field, updates[field])
    db.commit()
    db.refresh(strategy)
    return strategy


@app.get("/api/risk")
def risk_settings(db: Session = Depends(get_db)) -> dict:
    risk = get_risk_settings(db)
    db.commit()
    return {
        "emergency_stop": settings.emergency_stop or risk.emergency_stop,
        "runtime_emergency_stop": risk.emergency_stop,
        "env_emergency_stop": settings.emergency_stop,
        "duplicate_blocking": risk.duplicate_blocking,
        "max_account_exposure": risk.max_account_exposure,
        "account_daily_loss_limit": risk.account_daily_loss_limit,
        "current_account_exposure": round(account_exposure(db), 8),
        "today_account_net": round(daily_account_net(db), 8),
        "execution_mode": settings.execution_mode,
        "env_execution_mode": ENV_EXECUTION_MODE,
        "updated_at": risk.updated_at.isoformat() if risk.updated_at else None,
    }


@app.patch("/api/risk")
def update_risk_settings(updates: dict, db: Session = Depends(get_db)) -> dict:
    risk = get_risk_settings(db)
    for field in ("emergency_stop", "duplicate_blocking", "max_account_exposure", "account_daily_loss_limit"):
        if field in updates:
            setattr(risk, field, updates[field])
    if "execution_mode" in updates:
        mode = str(updates["execution_mode"] or "").strip().lower()
        if mode not in ("demo", "live"):
            raise HTTPException(status_code=400, detail="execution_mode must be 'demo' or 'live'.")
        missing = [name for name, value in deriv_credential_names(mode).items() if not value.strip()]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot switch to {mode}: missing " + ", ".join(missing) + " in .env.",
            )
        risk.execution_mode_override = mode
    db.commit()
    return risk_settings(db)


@app.get("/api/signals", response_model=list[SignalOut])
def list_signals(limit: int = 100, db: Session = Depends(get_db)) -> list[Signal]:
    mode = settings.execution_mode.lower()
    return list(
        db.scalars(
            select(Signal)
            .where(Signal.execution_mode == mode)
            .order_by(Signal.created_at.desc())
            .limit(limit)
        )
    )


@app.get("/api/open-positions", response_model=list[TradeOut])
async def open_positions(db: Session = Depends(get_db)) -> list[Trade]:
    mode = settings.execution_mode.lower()
    trades = list(
        db.scalars(
            select(Trade)
            .where(Trade.status == PositionStatus.open, Trade.execution_mode == mode)
            .order_by(Trade.opened_at.desc())
        )
    )
    # Deriv may have already stopped-out/sold a contract without a matching
    # exit webhook ever arriving, so double-check each open trade against
    # Deriv's real status before showing it as open.
    reconciled = False
    for trade in trades:
        if await reconcile_open_trade(db, trade):
            reconciled = True
    if reconciled:
        db.commit()
    return [trade for trade in trades if trade.status == PositionStatus.open]


@app.post("/api/open-positions/{trade_id}/close", response_model=TradeOut)
async def close_position(trade_id: int, db: Session = Depends(get_db)) -> Trade:
    trade = db.get(Trade, trade_id)
    if not trade or trade.execution_mode != settings.execution_mode.lower():
        raise HTTPException(status_code=404, detail="Open position not found.")
    if trade.status != PositionStatus.open:
        raise HTTPException(status_code=409, detail="Position is already closed.")

    # Deriv may have already stopped-out/sold this contract without a
    # matching exit webhook - reconcile instead of attempting a redundant close.
    if await reconcile_open_trade(db, trade):
        db.commit()
        db.refresh(trade)
        return trade

    bot = get_signal_bot(db, trade.strategy_name)
    try:
        await close_trade(db, trade, None, bot)
    except DerivExecutionError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to close position on Deriv: {exc}") from exc
    db.commit()
    db.refresh(trade)
    return trade


@app.get("/api/trade-history", response_model=list[TradeOut])
def trade_history(limit: int = 200, db: Session = Depends(get_db)) -> list[Trade]:
    mode = settings.execution_mode.lower()
    return list(
        db.scalars(
            select(Trade)
            .where(Trade.execution_mode == mode)
            .order_by(Trade.opened_at.desc())
            .limit(limit)
        )
    )


@app.get("/api/performance")
def strategy_performance(period: str = "today", db: Session = Depends(get_db)) -> list[dict]:
    start = period_start(period)
    strategies = list(db.scalars(select(Strategy).order_by(Strategy.name)))
    rows = []

    mode = settings.execution_mode.lower()
    for strategy in strategies:
        trade_query = select(Trade).where(Trade.strategy_id == strategy.id, Trade.execution_mode == mode)
        if start:
            trade_query = trade_query.where(Trade.opened_at >= start)
        trades = list(db.scalars(trade_query))
        closed = [trade for trade in trades if trade.status == PositionStatus.closed]
        wins = [trade for trade in closed if trade.net_result > 0]
        losses = [trade for trade in closed if trade.net_result < 0]
        total_net = sum(trade.net_result for trade in closed)
        total_fees = sum(trade.fees for trade in closed)
        open_count = db.scalar(
            select(func.count(Trade.id)).where(
                Trade.strategy_id == strategy.id,
                Trade.status == PositionStatus.open,
                Trade.execution_mode == mode,
            )
        )

        rows.append(
            {
                "strategy_id": strategy.id,
                "strategy_name": strategy.name,
                "enabled": strategy.enabled,
                "max_position_size": strategy.max_position_size,
                "daily_loss_limit": strategy.daily_loss_limit,
                "period": period,
                "profit_loss": round(sum(trade.profit_loss for trade in closed), 8),
                "net_profit_after_fees": round(total_net, 8),
                "fees": round(total_fees, 8),
                "number_of_trades": len(closed),
                "win_rate": round((len(wins) / len(closed)) * 100, 2) if closed else 0.0,
                "loss_rate": round((len(losses) / len(closed)) * 100, 2) if closed else 0.0,
                "average_win": round(sum(trade.net_result for trade in wins) / len(wins), 8) if wins else 0.0,
                "average_loss": round(sum(trade.net_result for trade in losses) / len(losses), 8) if losses else 0.0,
                "open_positions": int(open_count or 0),
            }
        )

    return rows


@app.get("/api/analytics")
def analytics(period: str = "all", db: Session = Depends(get_db)) -> dict:
    start = period_start(period)
    mode = settings.execution_mode.lower()
    trade_query = select(Trade).where(Trade.execution_mode == mode).order_by(Trade.closed_at, Trade.opened_at)
    signal_query = select(Signal).where(Signal.execution_mode == mode)
    if start:
        trade_query = trade_query.where(Trade.opened_at >= start)
        signal_query = signal_query.where(Signal.created_at >= start)

    trades = list(db.scalars(trade_query))
    signals = list(db.scalars(signal_query))
    closed = [trade for trade in trades if trade.status == PositionStatus.closed and trade.closed_at]
    running_net = 0.0
    equity_curve = []
    for trade in sorted(closed, key=lambda item: item.closed_at or item.opened_at):
        running_net += trade.net_result
        equity_curve.append(
            {
                "time": (trade.closed_at or trade.opened_at).isoformat(),
                "strategy_name": trade.strategy_name,
                "net_result": round(trade.net_result, 8),
                "cumulative_net": round(running_net, 8),
            }
        )

    status_counts: dict[str, int] = {}
    for signal in signals:
        status_counts[signal.status.value] = status_counts.get(signal.status.value, 0) + 1

    symbol_exposure: dict[str, float] = {}
    for trade in trades:
        if trade.status == PositionStatus.open:
            symbol_exposure[trade.symbol] = symbol_exposure.get(trade.symbol, 0.0) + trade.size

    return {
        "equity_curve": equity_curve,
        "status_counts": status_counts,
        "symbol_exposure": [
            {"symbol": symbol, "exposure": round(exposure, 8)}
            for symbol, exposure in sorted(symbol_exposure.items(), key=lambda item: item[1], reverse=True)
        ],
    }


@app.get("/api/account-balance")
async def account_balance(db: Session = Depends(get_db)) -> dict:
    mode = settings.execution_mode.lower()
    try:
        data = await DerivClient().get_account_balance()
        if not data:
            return {"mode": mode, "equity": None, "available": None, "unrealized_pnl": None, "error": "fetch_failed"}
        # data["unrealized_pnl"] comes from the same unreliable portfolio
        # enumeration as get_positions() (can silently omit real open
        # contracts) - recompute it from our own tracked open trades instead.
        per_position = await get_live_unrealized_pnl(db)
        return {
            "mode": mode,
            "equity": float(data.get("equity") or 0),
            "available": float(data.get("available") or 0),
            "unrealized_pnl": round(sum(per_position.values()), 8),
        }
    except DerivExecutionError as exc:
        return {
            "mode": mode,
            "equity": None,
            "available": None,
            "unrealized_pnl": None,
            "error": str(exc),
        }
    except Exception:
        return {"mode": mode, "equity": None, "available": None, "unrealized_pnl": None, "error": "fetch_failed"}


@app.get("/api/summary")
def summary(db: Session = Depends(get_db)) -> dict:
    get_risk_settings(db)
    db.commit()
    mode = settings.execution_mode.lower()
    open_count = db.scalar(select(func.count(Trade.id)).where(Trade.status == PositionStatus.open, Trade.execution_mode == mode)) or 0
    signal_count = db.scalar(select(func.count(Signal.id)).where(Signal.execution_mode == mode)) or 0
    rejected_count = db.scalar(select(func.count(Signal.id)).where(Signal.status == ExecutionStatus.rejected, Signal.execution_mode == mode)) or 0
    net = db.scalar(select(func.coalesce(func.sum(Trade.net_result), 0.0)).where(Trade.execution_mode == mode)) or 0.0
    return {
        "open_positions": int(open_count),
        "signals_logged": int(signal_count),
        "signals_rejected": int(rejected_count),
        "net_profit_after_fees": round(float(net), 8),
        "execution_mode": settings.execution_mode,
        "emergency_stop": settings.emergency_stop,
    }
