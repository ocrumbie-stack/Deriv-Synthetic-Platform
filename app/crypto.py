"""Webhooks sent to /webhook/blofin, and the Crypto page's data.

BloFin is the source of truth for crypto positions: nothing here writes a
Trade row, so Deriv's reconcile, trailing and audit loops never touch them.
Crypto follows the demo/live switch - demo trades BloFin's demo-trading
environment - and each signal records the mode it ran in, so the page only
shows the current mode's signals.

Entries need an enabled crypto bot named after the strategy, which sets the
size and leverage. A bot with leverage 0 (Auto) uses the pair's leverage on
Crypto -> Leverage, or the default set there; either way it's capped at the
pair's BloFin maximum. An entry whose stop loss sits too close to the
liquidation price is refused. An entry against an open opposite position closes it
first (a reversal); an entry in the same direction as an open position is
refused. Exits go through even when the bot is paused or missing.
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import notify
from app.blofin import BlofinClient, BlofinError, configured, contract_symbol, contracts_for, missing_settings, pair_label
from app.config import settings
from app.models import CryptoBot, CryptoPairLeverage, CryptoSignal, Direction, ExecutionStatus, SignalAction
from app.schemas import WebhookSignal
from app.services import get_risk_settings, local_period_start

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
        # 0 is Auto: the leverage differs per pair (Crypto -> Leverage).
        "leverage": bot.leverage,
        "auto_leverage": not bot.leverage,
        "position_value": position_value(bot.margin, bot.leverage) if bot.leverage else None,
        "tp_pct": bot.tp_pct,
        "sl_pct": bot.sl_pct,
        "enabled": bot.enabled,
        "session_started_at": _utc_iso(bot.session_started_at),
        "created_at": bot.created_at.isoformat(),
    }


def _utc_iso(moment: datetime | None) -> str | None:
    return moment.replace(tzinfo=timezone.utc).isoformat() if moment else None


def position_value(margin: float, leverage: int) -> float:
    """The USDT position an entry aims for: $12.50 margin at 10x is $125.
    BloFin trades whole lots, so the order is this rounded down to a lot."""
    return round(margin * leverage, 2)


# --- Leverage (Crypto -> Leverage) -----------------------------------------
# Used when BloFin doesn't send a pair's maximum leverage.
FALLBACK_MAX_LEVERAGE = 100
# An entry is refused when its stop loss is further than this share of the
# way from the entry price to the liquidation price.
SL_LIQUIDATION_SHARE = 0.8


def max_leverage(instrument: dict) -> int | None:
    """The pair's highest leverage on BloFin, or None if it isn't sent."""
    try:
        return int(float(instrument.get("maxLeverage") or 0)) or None
    except (TypeError, ValueError):
        return None


def default_leverage(db: Session) -> int:
    return max(1, get_risk_settings(db).crypto_default_leverage or 1)


def leverage_for(db: Session, bot: CryptoBot, symbol: str, most: int | None = None) -> tuple[int, str, bool]:
    """(leverage, where it came from, whether it was capped) for an entry on
    `symbol`: the bot's own number, else the pair's row on Crypto ->
    Leverage, else the default there - at most the pair's BloFin maximum."""
    if bot.leverage:
        leverage, source = bot.leverage, "bot"
    else:
        saved = db.scalar(select(CryptoPairLeverage.leverage).where(CryptoPairLeverage.symbol == symbol))
        leverage, source = (saved, "pair") if saved else (default_leverage(db), "default")
    if most and leverage > most:
        return most, source, True
    return leverage, source, False


def liquidation_move(leverage: int, most: int | None) -> float:
    """About how far price (as a fraction) moves against an isolated position
    before BloFin liquidates it: the margin (1/leverage) less maintenance
    margin, estimated as half the initial margin at the pair's maximum
    leverage. It's an estimate - BloFin's own tiers differ a little."""
    return max(1 / leverage - 0.5 / (most or FALLBACK_MAX_LEVERAGE), 0.0)


def safe_sl_pct(leverage: int, most: int | None) -> float:
    """The largest stop loss (% of margin) an entry at `leverage` may have."""
    return round(SL_LIQUIDATION_SHARE * liquidation_move(leverage, most) * leverage * 100, 2)


def stop_block(symbol: str, direction: Direction, price: float, stop_loss, leverage: int, most: int | None) -> str | None:
    """Why an entry's stop loss is too close to liquidation, or None."""
    if stop_loss is None or not price:
        return None
    stop = float(stop_loss)
    move = (price - stop) / price if direction == Direction.long else (stop - price) / price
    if move <= 0:
        return None  # on the profit side - BloFin refuses it with its own reason
    liquidation = liquidation_move(leverage, most)
    if move <= SL_LIQUIDATION_SHARE * liquidation:
        return None
    return (f"Stop loss too close to liquidation: {pair_label(symbol)} at {leverage}x liquidates about "
            f"{liquidation * 100:.2f}% away and the stop is {move * 100:.2f}% away "
            f"(the most allowed is {SL_LIQUIDATION_SHARE * liquidation * 100:.2f}%). "
            "Lower the leverage on Crypto > Leverage or the bot's SL %.")


def stop_prices(bot: CryptoBot, direction: Direction, price: float, tick_size,
                leverage: int | None = None) -> tuple[str | None, str | None]:
    """The bot's take profit and stop loss (% of margin) as BloFin trigger
    prices, measured from the last price: 50% of the margin at 10x is a 5%
    move. Rounded to the pair's tick and returned as plain decimal text.
    `leverage` is the one the entry trades at (the bot's own by default)."""
    tick = Decimal(str(tick_size or 0))
    leverage = leverage or bot.leverage or 1

    def at(pct: float | None, gain: bool) -> str | None:
        if not pct:
            return None
        move = Decimal(str(pct)) / 100 / leverage
        up = gain == (direction == Direction.long)
        level = Decimal(str(price)) * (1 + move if up else 1 - move)
        if tick > 0:
            level = (level / tick).to_integral_value() * tick
        return format(level, "f") if level > 0 else None

    return at(bot.tp_pct, True), at(bot.sl_pct, False)


async def limit_block(client: BlofinClient, risk, other_positions: list[dict], margin: float) -> str | None:
    """Why the Crypto limits on Risk Controls refuse an entry putting up
    `margin`, or None. A loss limit that can't be checked refuses the entry
    rather than letting it through unchecked."""
    limit = abs(risk.crypto_daily_loss_limit or 0.0)
    if limit:
        since = local_period_start("today").replace(tzinfo=timezone.utc)
        try:
            closed = await client.closed_positions(int(since.timestamp() * 1000))
        except (BlofinError, OSError, httpx.HTTPError) as e:
            return f"Couldn't check the Crypto daily loss limit ({e}), so the entry was refused."
        net = sum(row["realized_pnl"] for row in closed)
        if net <= -limit:
            return f"Crypto daily loss limit reached: today's closed P/L is ${net:,.2f}, limit -${limit:,.2f}."
    cap = risk.crypto_max_margin or 0.0
    if cap:
        in_use = sum(p.get("margin") or 0.0 for p in other_positions)
        if in_use + margin > cap:
            return f"Crypto margin limit: ${in_use:,.2f} in open positions plus this bot's ${margin:,.2f} is over ${cap:,.2f}."
    return None


def _client_order_id() -> str:
    # BloFin allows letters and digits only, up to 32.
    return f"wp{uuid.uuid4().hex[:24]}"


def _finish(db: Session, signal: CryptoSignal, status: ExecutionStatus, reason: str | None = None) -> CryptoSignal:
    signal.status = status
    signal.reason = reason
    db.commit()
    _alert(signal)
    return signal


def _label(symbol: str) -> str:
    """BTCUSDT for a BloFin id; the ticker as sent when it never resolved."""
    return pair_label(symbol) if "-" in symbol else symbol


def _alert(signal: CryptoSignal) -> None:
    """The Telegram message for a finished signal."""
    mode, pair, status = signal.execution_mode, _label(signal.symbol), signal.status
    direction = signal.direction.value if signal.direction else ""
    if status == ExecutionStatus.failed:
        notify.order_failed(mode, f"Crypto {signal.action.value} {pair}", signal.reason)
    elif status == ExecutionStatus.rejected:
        notify.market_rejected(mode, "Crypto", f"{signal.action.value} {direction}".strip(), pair, signal.strategy_name, signal.reason)
    elif status == ExecutionStatus.executed:
        detail = f"${signal.margin:.2f} margin · {signal.leverage}x"
        if signal.take_profit or signal.stop_loss:
            detail += f" · TP {signal.take_profit or '—'} / SL {signal.stop_loss or '—'}"
        if signal.reason:
            detail += f" · {signal.reason}"
        notify.market_opened(mode, "Crypto", direction, pair, signal.strategy_name, detail)
    elif status == ExecutionStatus.closed:
        notify.market_closed(mode, "Crypto", pair, signal.strategy_name, "strategy exit")


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
        leverage=(bot.leverage or 1) if bot else 1,
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
    risk = get_risk_settings(db)
    if risk.duplicate_blocking and payload.signal_id:
        seen = db.scalar(select(CryptoSignal.id).where(CryptoSignal.signal_id == payload.signal_id, CryptoSignal.id != signal.id))
        if seen:
            return _finish(db, signal, ExecutionStatus.rejected, "Duplicate signal_id.")
    if not configured(mode):
        return _finish(db, signal, ExecutionStatus.rejected, f"BloFin {mode} isn't connected - set {', '.join(missing_settings(mode))}.")

    if payload.action == SignalAction.exit:
        return await _exit(db, client, signal, symbol)

    if payload.direction is None:
        return _finish(db, signal, ExecutionStatus.rejected, "Entry needs a direction (long or short).")
    if settings.emergency_stop or risk.emergency_stop:
        return _finish(db, signal, ExecutionStatus.rejected, "Emergency stop is active.")
    if bot is None:
        return _finish(db, signal, ExecutionStatus.rejected, "No crypto bot is set up for this strategy - create one on Crypto > Signal Bots.")
    if not bot.enabled:
        return _finish(db, signal, ExecutionStatus.rejected, "Crypto bot is paused.")
    allowed = normalize_symbols(bot.symbols)
    if allowed and symbol not in allowed.split(","):
        return _finish(db, signal, ExecutionStatus.rejected, f"{pair_label(symbol)} isn't one of this bot's allowed pairs.")

    try:
        instrument, price = await asyncio.gather(client.instrument(symbol), client.last_price(symbol))
        if instrument.get("state") not in (None, "live"):
            return _finish(db, signal, ExecutionStatus.rejected, f"{pair_label(symbol)} isn't trading on BloFin right now.")
        most = max_leverage(instrument)
        leverage, _source, capped = leverage_for(db, bot, symbol, most)
        signal.leverage = leverage
        value = position_value(bot.margin, leverage)
        contracts = contracts_for(value, price, instrument["contractValue"], instrument["lotSize"])
        if contracts < float(instrument["minSize"]) or contracts <= 0:
            smallest = float(instrument["minSize"]) * float(instrument["contractValue"]) * price
            return _finish(db, signal, ExecutionStatus.rejected,
                           f"${value:,.2f} is under BloFin's smallest {pair_label(symbol)} position (about ${smallest:,.2f}) - raise the bot's margin or leverage.")

        account_positions = await client.positions()
        open_positions = [p for p in account_positions if p["symbol"] == symbol]
        if any(p["side"] == POSITION_SIDE[payload.direction] for p in open_positions):
            return _finish(db, signal, ExecutionStatus.rejected, f"A {payload.direction.value} {pair_label(symbol)} position is already open.")
        # A reversal closes this pair's position first, freeing its margin.
        others = [p for p in account_positions if p["symbol"] != symbol]
        blocked = await limit_block(client, risk, others, bot.margin)
        if blocked:
            return _finish(db, signal, ExecutionStatus.rejected, blocked)
        # Prices sent in the alert win over the bot's own % of margin.
        take_profit, stop_loss = stop_prices(bot, payload.direction, price, instrument.get("tickSize"), leverage)
        take_profit = payload.take_profit if payload.take_profit is not None else take_profit
        stop_loss = payload.stop_loss if payload.stop_loss is not None else stop_loss
        signal.take_profit = float(take_profit) if take_profit is not None else None
        signal.stop_loss = float(stop_loss) if stop_loss is not None else None
        blocked = stop_block(symbol, payload.direction, price, stop_loss, leverage, most)
        if blocked:
            return _finish(db, signal, ExecutionStatus.rejected, blocked)
        for position in open_positions:
            await client.close_position(position, _client_order_id())
        hedge = await client.hedge_mode()
        position_side = payload.direction.value if hedge else "net"
        await client.set_leverage(symbol, leverage, position_side if hedge else None)
        order = await client.open_market(
            symbol, payload.direction.value, contracts, _client_order_id(), position_side=position_side,
            take_profit=take_profit, stop_loss=stop_loss,
        )
    except (BlofinError, OSError, KeyError, ValueError) as e:
        logger.warning("BloFin entry failed for %s: %s", symbol, e)
        return _finish(db, signal, ExecutionStatus.failed, str(e))
    signal.order_id = str(order.get("orderId") or "") or None
    notes = []
    if open_positions:
        notes.append("Reversed the open position first.")
    if capped:
        notes.append(f"Leverage capped at BloFin's {most}x maximum for {pair_label(symbol)}.")
    return _finish(db, signal, ExecutionStatus.executed, " ".join(notes) or None)


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


# BloFin doesn't say who opened a closed position, so it goes to the bot
# whose filled entry on that pair came last before the position opened -
# an entry fills within seconds of its alert, so one opened more than
# CLOSED_MATCH_AFTER later (or a little before, for clock drift) was opened
# some other way.
CLOSED_MATCH_AFTER, CLOSED_MATCH_BEFORE, CLOSED_TTL = 300, 60, 30


async def closed_by_bot(db: Session, mode: str, client: BlofinClient,
                        session_starts: dict[str, datetime | None] | None = None) -> dict[str, dict[str, dict]]:
    """Realized P/L and close count per bot and pair, for positions the
    bot's entries opened in this mode: {strategy: {symbol: {pnl, closed}}}.
    A strategy in session_starts only counts what closed after its start."""
    entries = db.execute(
        select(CryptoSignal.strategy_name, CryptoSignal.symbol, CryptoSignal.created_at)
        .where(
            CryptoSignal.execution_mode == mode,
            CryptoSignal.action == SignalAction.entry,
            CryptoSignal.status == ExecutionStatus.executed,
        )
        .order_by(CryptoSignal.created_at)
    ).all()
    if not entries:
        return {}
    to_ms = lambda dt: int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
    by_symbol: dict[str, list[tuple[int, str]]] = {}
    for strategy, symbol, created_at in entries:
        by_symbol.setdefault(symbol, []).append((to_ms(created_at), strategy))
    starts = {name: to_ms(at) for name, at in (session_starts or {}).items() if at}
    since = to_ms(entries[0].created_at) - CLOSED_MATCH_BEFORE * 1000
    closed = await _cached(("closed", mode, since), CLOSED_TTL, lambda: client.closed_positions(since))
    out: dict[str, dict[str, dict]] = {}
    for row in closed:
        opened = row.get("opened_at_ms")
        if not opened:
            continue
        owner = None
        for at, strategy in by_symbol.get(row["symbol"], []):
            if at > opened + CLOSED_MATCH_BEFORE * 1000:
                break
            owner = strategy if opened - at <= CLOSED_MATCH_AFTER * 1000 else None
        if owner is None or row.get("closed_at_ms", 0) < starts.get(owner, 0):
            continue
        pair = out.setdefault(owner, {}).setdefault(row["symbol"], {"pnl": 0.0, "closed": 0})
        pair["pnl"] += row["realized_pnl"]
        pair["closed"] += 1
    return out


async def overview(db: Session, client: BlofinClient | None = None) -> dict:
    """Connection state, wallet, open positions and recent signals for the
    current demo/live mode only."""
    mode = _mode()
    signals = db.scalars(
        select(CryptoSignal).where(CryptoSignal.execution_mode == mode).order_by(CryptoSignal.id.desc()).limit(200)
    )
    bots = list(db.scalars(select(CryptoBot).order_by(CryptoBot.name)))
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
    # Closed P/L adds to each bot's running P/L; a failure leaves the bots on
    # running P/L only, flagged by closed_error.
    data["closed_error"] = None
    try:
        closed = await closed_by_bot(db, mode, client, {b.name: b.session_started_at for b in bots})
    except (BlofinError, OSError, httpx.HTTPError) as e:
        closed, data["closed_error"] = {}, str(e)
    for bot in data["bots"]:
        mine = closed.get(bot["name"], {})
        bot["closed_pnl"] = round(sum(p["pnl"] for p in mine.values()), 8)
        bot["closed_count"] = sum(p["closed"] for p in mine.values())
        for pair in bot["pairs"]:
            done = mine.get(pair["symbol"], {"pnl": 0.0, "closed": 0})
            pair["closed_pnl"], pair["closed_count"] = round(done["pnl"], 8), done["closed"]
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
    notify.market_closed(client.mode, "Crypto", pair_label(symbol), "", f"{side.lower()} closed from the dashboard")


# --- Crypto -> Leverage page ------------------------------------------------
# Every USDT pair on BloFin, each on the default leverage until given its
# own. A crypto bot with Leverage left blank (Auto) trades the leverage here.
INSTRUMENTS_TTL = 600


async def _instruments(client: BlofinClient) -> dict[str, dict]:
    rows = await _cached(("instruments", client.mode), INSTRUMENTS_TTL, client.instruments)
    return {row["instId"]: row for row in rows}


def _auto_bots(db: Session) -> list[tuple[CryptoBot, set | None]]:
    """Bots on Auto leverage, each with its allowed pairs (None for any)."""
    out = []
    for bot in db.scalars(select(CryptoBot).where(CryptoBot.leverage == 0).order_by(CryptoBot.name)):
        try:
            allowed = normalize_symbols(bot.symbols)
        except BlofinError:
            allowed = None
        out.append((bot, set(allowed.split(",")) if allowed else None))
    return out


def _leverage_row(instrument: dict, saved: int | None, default: int, auto_bots: list[tuple[CryptoBot, set | None]]) -> dict:
    symbol = instrument["instId"]
    most = max_leverage(instrument)
    wanted = saved or default
    leverage = min(wanted, most) if most else wanted
    safe = safe_sl_pct(leverage, most)
    return {
        "symbol": symbol,
        "state": instrument.get("state") or "live",
        "max_leverage": most,
        "saved_leverage": saved,
        "wanted_leverage": wanted,
        "leverage": leverage,
        "capped": leverage < wanted,
        "liquidation_pct": round(liquidation_move(leverage, most) * 100, 2),
        "safe_sl_pct": safe,
        # Auto bots allowed on this pair whose stop loss would be refused.
        "too_close": [bot.name for bot, allowed in auto_bots
                      if bot.sl_pct and bot.sl_pct > safe and (allowed is None or symbol in allowed)],
    }


async def leverage_page(db: Session, client: BlofinClient | None = None) -> dict:
    mode = _mode()
    client = client or BlofinClient(mode)
    default = default_leverage(db)
    saved = {row.symbol: row.leverage for row in db.scalars(select(CryptoPairLeverage))}
    bots = db.scalars(select(CryptoBot).order_by(CryptoBot.name))
    data = {
        "mode": mode,
        "default_leverage": default,
        "sl_share": SL_LIQUIDATION_SHARE,
        "error": None,
        "pairs": [],
        "bots": [{"id": b.id, "name": b.name, "leverage": b.leverage, "tp_pct": b.tp_pct,
                  "sl_pct": b.sl_pct, "enabled": b.enabled} for b in bots],
    }
    try:
        instruments = await _instruments(client)
    except (BlofinError, OSError, httpx.HTTPError) as e:
        data["error"] = str(e) or "BloFin's pair list is unavailable."
        return data
    auto_bots = _auto_bots(db)
    rows = [_leverage_row(inst, saved.get(symbol), default, auto_bots) for symbol, inst in instruments.items()]
    # Pairs given their own leverage first, then A-Z.
    data["pairs"] = sorted(rows, key=lambda r: (r["saved_leverage"] is None, r["symbol"]))
    return data


async def set_pair_leverage(db: Session, symbol: str, leverage: int | None, client: BlofinClient | None = None) -> dict:
    """Save a pair's leverage (capped at its BloFin maximum), or with None put
    it back on the default. Raises BlofinError for a pair BloFin doesn't list."""
    client = client or BlofinClient(_mode())
    symbol = contract_symbol(symbol)
    instrument = (await _instruments(client)).get(symbol)
    if instrument is None:
        raise BlofinError(f"BloFin doesn't list {pair_label(symbol)}.")
    most = max_leverage(instrument)
    row = db.scalar(select(CryptoPairLeverage).where(CryptoPairLeverage.symbol == symbol))
    saved = None
    if leverage is None:
        if row:
            db.delete(row)
    else:
        if not row:
            row = CryptoPairLeverage(symbol=symbol)
            db.add(row)
        row.leverage = saved = min(leverage, most) if most else leverage
    db.commit()
    return _leverage_row(instrument, saved, default_leverage(db), _auto_bots(db))


def set_default_leverage(db: Session, leverage: int) -> int:
    risk = get_risk_settings(db)
    risk.crypto_default_leverage = leverage
    db.commit()
    return leverage
