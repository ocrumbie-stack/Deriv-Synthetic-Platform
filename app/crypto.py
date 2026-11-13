"""Webhooks sent to /webhook/blofin, and the Crypto page's data.

BloFin is the source of truth for crypto positions: nothing here writes a
Trade row, so Deriv's reconcile, trailing and audit loops never touch them.
Crypto follows the demo/live switch - demo trades BloFin's demo-trading
environment - and each signal records the mode it ran in, so the page only
shows the current mode's signals.

Entries need an enabled crypto bot named after the strategy, which sets the
size and leverage. An entry against an open opposite position closes it
first (a reversal); an entry in the same direction as an open position is
refused. Exits go through even when the bot is paused or missing.
"""

import asyncio
import logging
import time
import uuid
from decimal import Decimal

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import notify
from app.blofin import BlofinClient, BlofinError, configured, contract_symbol, contracts_for, missing_settings, pair_label
from app.config import settings
from app.models import CryptoBot, CryptoSignal, Direction, ExecutionStatus, SignalAction
from app.schemas import WebhookSignal
from app.services import get_risk_settings

logger = logging.getLogger(__name__)

# One crypto order at a time, so a reversal's close and the entry after it
# can't interleave with another signal for the same pair.
order_lock = asyncio.Lock()

POSITION_SIDE = {Direction.long: "LONG", Direction.short: "SHORT"}


def _mode() -> str:
    return settings.execution_mode.lower()


def normalize_symbols(value: str | None) -> str | None:
    """A typed allow-list ("btcusdt, ETHUSDT.P") as BloFin's instrument ids,
    or None for any. Raises BlofinError on a ticker that isn't a USDT pair."""
    names = [contract_symbol(part) for part in (value or "").split(",") if part.strip()]
    return ",".join(dict.fromkeys(names)) or None


def get_bot(db: Session, name: str) -> CryptoBot | None:
    return db.scalar(select(CryptoBot).where(CryptoBot.name == name))


def bot_out(bot: CryptoBot) -> dict:
    symbols = normalize_symbols(bot.symbols)
    return {
        "id": bot.id,
        "name": bot.name,
        "symbols": symbols.split(",") if symbols else [],
        "margin": bot.margin,
        "leverage": bot.leverage,
        "position_value": position_value(bot.margin, bot.leverage),
        "tp_pct": bot.tp_pct,
        "sl_pct": bot.sl_pct,
        "enabled": bot.enabled,
        "created_at": bot.created_at.isoformat(),
    }


def position_value(margin: float, leverage: int) -> float:
    """The USDT position an entry aims for: $12.50 margin at 10x is $125.
    BloFin trades whole lots, so the order is this rounded down to a lot."""
    return round(margin * leverage, 2)


def stop_prices(bot: CryptoBot, direction: Direction, price: float, tick_size) -> tuple[str | None, str | None]:
    """The bot's take profit and stop loss (% of margin) as BloFin trigger
    prices, measured from the last price: 50% of the margin at 10x is a 5%
    move. Rounded to the pair's tick and returned as plain decimal text."""
    tick = Decimal(str(tick_size or 0))

    def at(pct: float | None, gain: bool) -> str | None:
        if not pct:
            return None
        move = Decimal(str(pct)) / 100 / bot.leverage
        up = gain == (direction == Direction.long)
        level = Decimal(str(price)) * (1 + move if up else 1 - move)
        if tick > 0:
            level = (level / tick).to_integral_value() * tick
        return format(level, "f") if level > 0 else None

    return at(bot.tp_pct, True), at(bot.sl_pct, False)


def _client_order_id() -> str:
    # BloFin allows letters and digits only, up to 32.
    return f"wp{uuid.uuid4().hex[:24]}"


def _finish(db: Session, signal: CryptoSignal, status: ExecutionStatus, reason: str | None = None) -> CryptoSignal:
    signal.status = status
    signal.reason = reason
    db.commit()
    if status == ExecutionStatus.failed:
        notify.order_failed(signal.execution_mode, f"BloFin {signal.action.value} {signal.symbol}", reason)
    return signal


async def process_signal(db: Session, payload: WebhookSignal, client: BlofinClient | None = None) -> CryptoSignal:
    mode = _mode()
    client = client or BlofinClient(mode)
    bot = get_bot(db, payload.strategy)
    signal = CryptoSignal(
        strategy_name=payload.strategy,
        symbol=payload.symbol,
        action=payload.action,
        direction=payload.direction,
        margin=bot.margin if bot else None,
        leverage=bot.leverage if bot else 1,
        take_profit=payload.take_profit,
        stop_loss=payload.stop_loss,
        signal_id=payload.signal_id,
        execution_mode=mode,
    )
    db.add(signal)
    db.commit()

    if payload.secret != settings.webhook_secret:
        return _finish(db, signal, ExecutionStatus.rejected, "Invalid webhook secret.")
    try:
        symbol = contract_symbol(payload.symbol)
    except BlofinError as e:
        return _finish(db, signal, ExecutionStatus.rejected, str(e))
    signal.symbol = symbol
    if not configured(mode):
        return _finish(db, signal, ExecutionStatus.rejected, f"BloFin {mode} isn't connected - set {', '.join(missing_settings(mode))}.")

    if payload.action == SignalAction.exit:
        return await _exit(db, client, signal, symbol)

    if payload.direction is None:
        return _finish(db, signal, ExecutionStatus.rejected, "Entry needs a direction (long or short).")
    risk = get_risk_settings(db)
    if settings.emergency_stop or risk.emergency_stop:
        return _finish(db, signal, ExecutionStatus.rejected, "Emergency stop is active.")
    if bot is None:
        return _finish(db, signal, ExecutionStatus.rejected, "No crypto bot is set up for this strategy - create one on Crypto > Signal Bots.")
    if not bot.enabled:
        return _finish(db, signal, ExecutionStatus.rejected, "Crypto bot is paused.")
    allowed = normalize_symbols(bot.symbols)
    if allowed and symbol not in allowed.split(","):
        return _finish(db, signal, ExecutionStatus.rejected, f"{pair_label(symbol)} isn't one of this bot's allowed pairs.")

    value = position_value(bot.margin, bot.leverage)
    try:
        instrument, price = await asyncio.gather(client.instrument(symbol), client.last_price(symbol))
        if instrument.get("state") not in (None, "live"):
            return _finish(db, signal, ExecutionStatus.rejected, f"{pair_label(symbol)} isn't trading on BloFin right now.")
        contracts = contracts_for(value, price, instrument["contractValue"], instrument["lotSize"])
        if contracts < float(instrument["minSize"]) or contracts <= 0:
            smallest = float(instrument["minSize"]) * float(instrument["contractValue"]) * price
            return _finish(db, signal, ExecutionStatus.rejected,
                           f"${value:,.2f} is under BloFin's smallest {pair_label(symbol)} position (about ${smallest:,.2f}) - raise the bot's margin or leverage.")

        open_positions = await client.positions(symbol)
        if any(p["side"] == POSITION_SIDE[payload.direction] for p in open_positions):
            return _finish(db, signal, ExecutionStatus.rejected, f"A {payload.direction.value} {pair_label(symbol)} position is already open.")
        for position in open_positions:
            await client.close_position(position, _client_order_id())
        hedge = await client.hedge_mode()
        position_side = payload.direction.value if hedge else "net"
        await client.set_leverage(symbol, bot.leverage, position_side if hedge else None)
        # Prices sent in the alert win over the bot's own % of margin.
        take_profit, stop_loss = stop_prices(bot, payload.direction, price, instrument.get("tickSize"))
        take_profit = payload.take_profit if payload.take_profit is not None else take_profit
        stop_loss = payload.stop_loss if payload.stop_loss is not None else stop_loss
        signal.take_profit = float(take_profit) if take_profit is not None else None
        signal.stop_loss = float(stop_loss) if stop_loss is not None else None
        order = await client.open_market(
            symbol, payload.direction.value, contracts, _client_order_id(), position_side=position_side,
            take_profit=take_profit, stop_loss=stop_loss,
        )
    except (BlofinError, OSError, KeyError, ValueError) as e:
        logger.warning("BloFin entry failed for %s: %s", symbol, e)
        return _finish(db, signal, ExecutionStatus.failed, str(e))
    signal.order_id = str(order.get("orderId") or "") or None
    reason = "Reversed the open position first." if open_positions else None
    return _finish(db, signal, ExecutionStatus.executed, reason)


async def _exit(db: Session, client: BlofinClient, signal: CryptoSignal, symbol: str) -> CryptoSignal:
    try:
        positions = await client.positions(symbol)
        # An exit with a direction only closes that side.
        if signal.direction:
            positions = [p for p in positions if p["side"] == POSITION_SIDE[signal.direction]]
        if not positions:
            return _finish(db, signal, ExecutionStatus.rejected, "No open position to close.")
        for position in positions:
            await client.close_position(position, _client_order_id())
    except (BlofinError, OSError) as e:
        logger.warning("BloFin exit failed for %s: %s", symbol, e)
        return _finish(db, signal, ExecutionStatus.failed, str(e))
    return _finish(db, signal, ExecutionStatus.closed)


def _signal_out(signal: CryptoSignal) -> dict:
    return {
        "id": signal.id,
        "strategy_name": signal.strategy_name,
        "symbol": signal.symbol,
        "action": signal.action.value,
        "direction": signal.direction.value if signal.direction else None,
        "margin": signal.margin,
        "leverage": signal.leverage,
        "take_profit": signal.take_profit,
        "stop_loss": signal.stop_loss,
        "order_id": signal.order_id,
        "status": signal.status.value,
        "reason": signal.reason,
        "created_at": signal.created_at.isoformat(),
    }


def bot_pairs(db: Session, mode: str) -> tuple[dict[str, list[dict]], dict[str, CryptoSignal]]:
    """What each strategy has traded in this mode - per pair, how many
    entries were filled and the latest one's side and TP/SL - and, per pair,
    the newest filled entry of any strategy (who an open position belongs to)."""
    rows = db.execute(
        select(CryptoSignal.strategy_name, CryptoSignal.symbol, func.count(), func.max(CryptoSignal.id))
        .where(
            CryptoSignal.execution_mode == mode,
            CryptoSignal.action == SignalAction.entry,
            CryptoSignal.status == ExecutionStatus.executed,
        )
        .group_by(CryptoSignal.strategy_name, CryptoSignal.symbol)
    ).all()
    latest = {s.id: s for s in db.scalars(select(CryptoSignal).where(CryptoSignal.id.in_([r[3] for r in rows])))}
    pairs: dict[str, list[dict]] = {}
    newest: dict[str, CryptoSignal] = {}
    for strategy, symbol, entries, last_id in rows:
        last = latest[last_id]
        pairs.setdefault(strategy, []).append({
            "symbol": symbol,
            "entries": entries,
            "direction": last.direction.value if last.direction else None,
            "take_profit": last.take_profit,
            "stop_loss": last.stop_loss,
            "last_entry_at": last.created_at.isoformat(),
        })
        if symbol not in newest or last.id > newest[symbol].id:
            newest[symbol] = last
    return pairs, newest


async def overview(db: Session, client: BlofinClient | None = None) -> dict:
    """Connection state, wallet, open positions and recent signals for the
    current demo/live mode only."""
    mode = _mode()
    signals = db.scalars(
        select(CryptoSignal).where(CryptoSignal.execution_mode == mode).order_by(CryptoSignal.id.desc()).limit(200)
    )
    bots = db.scalars(select(CryptoBot).order_by(CryptoBot.name))
    pairs, newest = bot_pairs(db, mode)
    data = {
        "mode": mode,
        "connected": configured(mode),
        "missing": missing_settings(mode),
        "bots": [{**bot_out(b), "pairs": pairs.get(b.name, [])} for b in bots],
        "balance": None,
        "positions": [],
        "error": None,
        "api_key": None,
        "signals": [_signal_out(s) for s in signals],
    }
    if not data["connected"]:
        return data
    client = client or BlofinClient(mode)
    try:
        data["balance"], data["positions"] = await asyncio.gather(client.balance(), client.positions())
    except (BlofinError, OSError) as e:
        data["error"] = str(e)
    # A position belongs to the strategy whose entry on that pair was filled
    # last, if it was the same side; otherwise it was opened on BloFin itself.
    for position in data["positions"]:
        last = newest.get(position["symbol"])
        owned = last is not None and last.direction is not None and POSITION_SIDE[last.direction] == position["side"]
        position["strategy"] = last.strategy_name if owned else None
    # Only decorates the header, so a failure here hides the badges rather
    # than flagging the connection.
    try:
        data["api_key"] = await _cached(("api_key", mode), API_KEY_TTL, client.api_key)
    except (BlofinError, OSError, httpx.HTTPError):
        data["api_key"] = None
    return data


# Market data is the same for everyone and changes slowly next to a 10s page
# poll, so it's cached per mode (demo and live quote separately).
TICKER_TTL, FUNDING_TTL, CANDLE_TTL, API_KEY_TTL = 5, 60, 300, 600
MAX_MARKET_PAIRS = 8
_cache: dict[tuple, tuple[float, object]] = {}


async def _cached(key: tuple, ttl: float, fetch):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    value = await fetch()
    _cache[key] = (time.monotonic(), value)
    return value


async def market(symbols: str | None, client: BlofinClient | None = None) -> dict:
    """Price, 24h stats, funding and a 24h sparkline for each pair asked for
    (typed like an alert ticker or as BloFin's id). Pairs BloFin doesn't
    list are left out. Public data, so it works before keys are set."""
    mode = _mode()
    client = client or BlofinClient(mode)
    pairs = []
    for part in (symbols or "").split(","):
        try:
            pairs.append(contract_symbol(part))
        except BlofinError:
            continue
    pairs = list(dict.fromkeys(pairs))[:MAX_MARKET_PAIRS]
    data = {"mode": mode, "pairs": [], "error": None}
    if not pairs:
        return data
    try:
        tickers, funding = await asyncio.gather(
            _cached(("tickers", mode), TICKER_TTL, client.tickers),
            _cached(("funding", mode), FUNDING_TTL, client.funding_rates),
        )
        pairs = [p for p in pairs if p in tickers]
        # A missing sparkline shouldn't hide the pair's price.
        closes = await asyncio.gather(
            *(_cached(("closes", mode, p), CANDLE_TTL, lambda p=p: client.closes(p)) for p in pairs),
            return_exceptions=True,
        )
    except (BlofinError, OSError, httpx.HTTPError) as e:
        data["error"] = str(e) or "BloFin market data is unavailable."
        return data
    for pair, spark in zip(pairs, closes):
        ticker = tickers[pair]
        last, opened = ticker["last"], ticker["open_24h"]
        data["pairs"].append({
            "symbol": pair,
            **ticker,
            "change_24h": (last - opened) / opened if last and opened else None,
            "funding": funding.get(pair),
            "sparkline": spark if isinstance(spark, list) else [],
        })
    return data


async def close_position(symbol: str, side: str, client: BlofinClient | None = None) -> None:
    client = client or BlofinClient(_mode())
    symbol = contract_symbol(symbol)
    async with order_lock:
        positions = [p for p in await client.positions(symbol) if p["side"] == side]
        if not positions:
            raise BlofinError(f"No open {side.lower()} {pair_label(symbol)} position.")
        for position in positions:
            await client.close_position(position, _client_order_id())
