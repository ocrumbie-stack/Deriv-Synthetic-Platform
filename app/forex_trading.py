"""Webhooks sent to /webhook/forex, and the Forex Positions and History pages.

Forex trades on the cTrader account picked on the Forex Overview (e.g. Fusion
Markets) for the current demo/live mode. Like BloFin for Crypto, cTrader is
the source of truth: nothing here writes a Signal or Trade row, so Deriv's
reconcile, trailing and audit loops never touch forex. Each webhook is kept
in ForexSignal, and an executed entry records the position it opened so an
exit closes the strategy's own position, not one opened in the cTrader app.

Entries need an enabled forex bot named after the strategy (it sets the lots
and the stops in pips), an allowed pair, and the pair's trading window to be
open. An entry against an open opposite position closes it first (a
reversal); one in the same direction is refused. Exits always go through,
even when the bot is paused or missing or the window is shut.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import ctrader, forex, notify
from app.config import settings
from app.ctrader import CTraderClient, CTraderError
from app.models import Direction, ExecutionStatus, ForexBot, ForexSignal, SignalAction
from app.schemas import WebhookSignal
from app.services import get_risk_settings

logger = logging.getLogger(__name__)

MARKET = "forex"
# Every position the platform opens carries this label, and the strategy
# name as its comment.
LABEL = "wp-forex"
# A dashboard close is journalled as an exit with this signal_id.
MANUAL_CLOSE = "manual_close"
HISTORY_DAYS = 7  # cTrader's deal list allows a week per request

# One forex order at a time, so a reversal's close and the entry after it
# can't interleave with another signal (or a dashboard close).
order_lock = asyncio.Lock()

SOURCE_LABELS = {
    "strategy_exit": "Strategy exit",
    "reversal": "Reversal",
    "manual_close": "Closed from dashboard",
}


def _mode() -> str:
    return settings.execution_mode.lower()


def client_for_mode(mode: str | None = None) -> CTraderClient:
    return CTraderClient(live=(mode or _mode()) == "live", market=MARKET)


def pip_distance(pips: float, pair: str, symbol: dict) -> float:
    """Pips as a price distance. cTrader's pipPosition wins (4 = 0.0001);
    otherwise 0.0001, or 0.01 on yen pairs."""
    position = symbol.get("pipPosition")
    pip = 10 ** -int(position) if position is not None else forex.pip_size(pair)
    digits = int(symbol.get("digits") if symbol.get("digits") is not None else 5)
    return round(pips * pip, digits)


def pip_size_for(pair: str, symbol: dict | None) -> float:
    position = (symbol or {}).get("pipPosition")
    return 10 ** -int(position) if position is not None else forex.pip_size(pair)


def stop_distances(payload: WebhookSignal, bot: ForexBot, pair: str, symbol: dict) -> tuple[float | None, float | None]:
    """(stop loss, take profit) as price distances from the fill. A drawn
    trade's levels win (measured from its entry price); otherwise the bot's
    pips."""
    digits = int(symbol.get("digits") if symbol.get("digits") is not None else 5)

    def side(level: float | None, pips: float | None) -> float | None:
        if payload.price and level:
            return round(abs(payload.price - level), digits) or None
        if pips:
            return pip_distance(pips, pair, symbol) or None
        return None

    return side(payload.stop_loss, bot.stop_loss_pips), side(payload.take_profit, bot.take_profit_pips)


def _finish(db: Session, signal: ForexSignal, status: ExecutionStatus, reason: str | None = None) -> ForexSignal:
    signal.status = status
    signal.reason = reason
    db.commit()
    if status == ExecutionStatus.failed:
        notify.order_failed(signal.execution_mode, f"Forex {signal.action.value} {signal.symbol}", reason)
    return signal


def opened_by(db: Session, account_id: int) -> dict[int, str]:
    """Position id -> strategy, for every position the platform opened on
    this account."""
    rows = db.execute(
        select(ForexSignal.position_id, ForexSignal.strategy_name).where(
            ForexSignal.account_id == account_id, ForexSignal.position_id.is_not(None)
        )
    )
    return {int(position): strategy for position, strategy in rows}


def closed_by(db: Session, account_id: int) -> dict[int, str]:
    """Position id -> how the platform closed it (a SOURCE_LABELS key)."""
    out: dict[int, str] = {}
    rows = db.execute(
        select(ForexSignal.closed_positions, ForexSignal.action, ForexSignal.signal_id).where(
            ForexSignal.account_id == account_id, ForexSignal.closed_positions.is_not(None)
        )
    )
    for closed, action, signal_id in rows:
        if signal_id == MANUAL_CLOSE:
            source = "manual_close"
        else:
            source = "reversal" if action == SignalAction.entry else "strategy_exit"
        for part in closed.split(","):
            if part.strip().isdigit():
                out[int(part)] = source
    return out


def strategy_positions(positions: list[dict], owners: dict[int, str], strategy: str, symbol_id: int) -> list[dict]:
    """The open positions on this pair that belong to the strategy: ones the
    journal says it opened, or ones carrying the platform's label and its name."""
    return [
        p for p in positions
        if p["symbol_id"] == symbol_id
        and (owners.get(p["position_id"]) == strategy or (p.get("label") == LABEL and p.get("comment") == strategy))
    ]


async def _close(client: CTraderClient, positions: list[dict]) -> tuple[list[int], float]:
    closed, net = [], 0.0
    for position in positions:
        result = await client.close_position(position["position_id"], position["volume"])
        closed.append(position["position_id"])
        net += result.get("net_profit") or 0.0
    return closed, round(net, 8)


async def process_signal(
    db: Session, payload: WebhookSignal, client: CTraderClient | None = None, now: datetime | None = None
) -> ForexSignal:
    mode = _mode()
    bot = forex.get_bot(db, payload.strategy)
    signal = ForexSignal(
        strategy_name=payload.strategy,
        symbol=payload.symbol.upper(),
        action=payload.action,
        direction=payload.direction,
        lots=bot.lots if bot else None,
        price=payload.price,
        stop_loss=payload.stop_loss,
        take_profit=payload.take_profit,
        signal_id=payload.signal_id,
        execution_mode=mode,
    )
    db.add(signal)
    db.commit()

    if payload.secret != settings.webhook_secret:
        return _finish(db, signal, ExecutionStatus.rejected, "Invalid webhook secret.")
    try:
        pair = forex.pair_symbol(payload.symbol)
    except ValueError as e:
        return _finish(db, signal, ExecutionStatus.rejected, str(e))
    signal.symbol = pair

    if payload.action == SignalAction.entry:
        if payload.direction is None:
            return _finish(db, signal, ExecutionStatus.rejected, "Entry needs a direction (long or short).")
        risk = get_risk_settings(db)
        if settings.emergency_stop or risk.emergency_stop:
            return _finish(db, signal, ExecutionStatus.rejected, "Emergency stop is active.")
        if bot is None:
            return _finish(db, signal, ExecutionStatus.rejected, "No forex bot is set up for this strategy - create one on Forex > Signal Bots.")
        if not bot.enabled:
            return _finish(db, signal, ExecutionStatus.rejected, "Forex bot is paused.")
        if bot.symbols and pair not in bot.symbols.split(","):
            return _finish(db, signal, ExecutionStatus.rejected, f"{forex.pair_label(pair)} isn't one of this bot's allowed pairs.")
        blocked = forex.entry_block_reason(db, pair, now)
        if blocked:
            return _finish(db, signal, ExecutionStatus.rejected, blocked)

    live = mode == "live"
    try:
        client = client or client_for_mode(mode)
        account_id = client.account_id
        signal.account_id = account_id
        ctrader.verify_account_mode(account_id, live, MARKET)
        symbol = await client.resolve_symbol(pair)
        positions = strategy_positions(await client.positions(), opened_by(db, account_id), payload.strategy, int(symbol["symbolId"]))
    except CTraderError as e:
        return _finish(db, signal, ExecutionStatus.failed, str(e))

    if payload.action == SignalAction.exit:
        # An exit with a direction only closes that side.
        if payload.direction:
            positions = [p for p in positions if p["direction"] == payload.direction.value]
        if not positions:
            return _finish(db, signal, ExecutionStatus.rejected, "No open position to close.")
        try:
            closed, net = await _close(client, positions)
        except CTraderError as e:
            return _finish(db, signal, ExecutionStatus.failed, str(e))
        signal.closed_positions = ",".join(map(str, closed))
        signal.net_profit = net
        return _finish(db, signal, ExecutionStatus.closed)

    if any(p["direction"] == payload.direction.value for p in positions):
        return _finish(db, signal, ExecutionStatus.rejected, f"A {payload.direction.value} {forex.pair_label(pair)} position is already open.")
    reversed_note = None
    if positions:
        try:
            closed, net = await _close(client, positions)
        except CTraderError as e:
            return _finish(db, signal, ExecutionStatus.failed, f"Failed to close the opposite position before reversing: {e}")
        signal.closed_positions = ",".join(map(str, closed))
        signal.net_profit = net
        db.commit()
        reversed_note = "Reversed the open position first."
    try:
        stop_loss, take_profit = stop_distances(payload, bot, pair, symbol)
        position = await client.open_market(
            symbol,
            payload.direction.value,
            bot.lots,
            stop_loss_distance=stop_loss,
            take_profit_distance=take_profit,
            trailing_stop=bool(bot.trailing_stop and stop_loss),
            label=LABEL,
            comment=payload.strategy,
        )
    except CTraderError as e:
        reason = f"{reversed_note} Opening the new position failed: {e}" if reversed_note else str(e)
        return _finish(db, signal, ExecutionStatus.failed, reason)
    signal.position_id = position["position_id"]
    signal.entry_price = position["entry_price"] or None
    signal.lots = ctrader.volume_to_lots(position["volume"], symbol) or bot.lots
    return _finish(db, signal, ExecutionStatus.executed, reversed_note)


async def close_position(db: Session, position_id: int, client: CTraderClient | None = None) -> ForexSignal:
    """Close one position on the current mode's account from the dashboard -
    any position, whoever opened it. Raises CTraderError."""
    mode = _mode()
    client = client or client_for_mode(mode)
    account_id = client.account_id
    found = next((p for p in await client.positions() if p["position_id"] == position_id), None)
    if not found:
        raise CTraderError("That position is no longer open.")
    signal = ForexSignal(
        strategy_name=opened_by(db, account_id).get(position_id) or found.get("comment") or "",
        symbol=await _symbol_name(client, found["symbol_id"]),
        action=SignalAction.exit,
        direction=Direction(found["direction"]),
        signal_id=MANUAL_CLOSE,
        account_id=account_id,
        execution_mode=mode,
    )
    db.add(signal)
    db.commit()
    closed, net = await _close(client, [found])
    signal.closed_positions = ",".join(map(str, closed))
    signal.net_profit = net
    return _finish(db, signal, ExecutionStatus.closed, "Closed from the dashboard.")


async def _symbol_name(client: CTraderClient, symbol_id: int) -> str:
    for symbol in await client.symbols():
        if int(symbol.get("symbolId") or 0) == symbol_id:
            return str(symbol.get("symbolName") or symbol_id)
    return str(symbol_id)


async def _symbol_info(client: CTraderClient, symbol_ids: set[int]) -> dict[int, dict]:
    """Name and details for each symbol id; a symbol cTrader won't describe
    still gets its name so the row can be drawn."""
    names = {int(s.get("symbolId") or 0): str(s.get("symbolName") or "") for s in await client.symbols()}
    out = {}
    for symbol_id in symbol_ids:
        try:
            details = await client.symbol_details(symbol_id)
        except CTraderError:
            details = {}
        out[symbol_id] = {**details, "symbolName": names.get(symbol_id) or str(symbol_id)}
    return out


def _signal_out(signal: ForexSignal) -> dict:
    return {
        "id": signal.id,
        "strategy_name": signal.strategy_name,
        "symbol": signal.symbol,
        "action": signal.action.value,
        "direction": signal.direction.value if signal.direction else None,
        "lots": signal.lots,
        "price": signal.price,
        "entry_price": signal.entry_price,
        "position_id": signal.position_id,
        "net_profit": signal.net_profit,
        "manual": signal.signal_id == MANUAL_CLOSE,
        "status": signal.status.value,
        "reason": signal.reason,
        "created_at": signal.created_at.isoformat(),
    }


def _iso(moment: datetime | None) -> str | None:
    return moment.replace(tzinfo=timezone.utc).isoformat() if moment else None


async def trading(db: Session, client: CTraderClient | None = None) -> dict:
    """Open positions (every one on the account), the last week's closed
    trades and recent signals, for the current demo/live mode only."""
    mode = _mode()
    signals = db.scalars(
        select(ForexSignal).where(ForexSignal.execution_mode == mode).order_by(ForexSignal.id.desc()).limit(200)
    )
    data = {
        "mode": mode,
        "connected": False,
        "account_id": None,
        "error": None,
        "positions": [],
        "history": [],
        "history_days": HISTORY_DAYS,
        "signals": [_signal_out(s) for s in signals],
    }
    try:
        client = client or client_for_mode(mode)
        account_id = client.account_id
    except CTraderError as e:
        data["error"] = str(e)
        return data
    data["connected"] = True
    data["account_id"] = account_id
    owners, closers = opened_by(db, account_id), closed_by(db, account_id)
    try:
        positions = await client.positions()
        deals = await client.closing_deals(datetime.utcnow() - timedelta(days=HISTORY_DAYS))
        try:
            pnl = await client.unrealized_pnl()
        except CTraderError as e:
            logger.warning("Forex unrealized P/L unavailable: %s", e)
            pnl = {}
        info = await _symbol_info(client, {p["symbol_id"] for p in positions} | {d["symbol_id"] for d in deals})
    except CTraderError as e:
        data["error"] = str(e)
        return data

    for p in sorted(positions, key=lambda p: p["opened_at"] or datetime.min, reverse=True):
        symbol = info.get(p["symbol_id"], {})
        data["positions"].append({
            "position_id": p["position_id"],
            "strategy": owners.get(p["position_id"]) or (p.get("comment") if p.get("label") == LABEL else None),
            "symbol": symbol.get("symbolName"),
            "direction": p["direction"],
            "lots": ctrader.volume_to_lots(p["volume"], symbol) if symbol.get("lotSize") else None,
            "entry_price": p["entry_price"] or None,
            "stop_loss": p["stop_loss"],
            "take_profit": p["take_profit"],
            "trailing_stop": p["trailing_stop"],
            "used_margin": p["used_margin"],
            "swap": p["swap"],
            "commission": p["commission"],
            "unrealized_pnl": pnl.get(p["position_id"]),
            "opened_at": _iso(p["opened_at"]),
        })

    for d in sorted(deals, key=lambda d: d["closed_at"] or datetime.min, reverse=True):
        symbol = info.get(d["symbol_id"], {})
        name = symbol.get("symbolName") or ""
        pips = None
        if d["entry_price"] and d["exit_price"]:
            move = d["exit_price"] - d["entry_price"]
            pips = round((move if d["direction"] == "long" else -move) / pip_size_for(name, symbol), 1)
        source = closers.get(d["position_id"])
        data["history"].append({
            "position_id": d["position_id"],
            "strategy": owners.get(d["position_id"]),
            "symbol": name,
            "direction": d["direction"],
            "lots": ctrader.volume_to_lots(d["volume"], symbol) if symbol.get("lotSize") else None,
            "entry_price": d["entry_price"],
            "exit_price": d["exit_price"],
            "pips": pips,
            "swap": d["swap"],
            "commission": d["commission"],
            "net_profit": d["net_profit"],
            "reason": SOURCE_LABELS.get(source, "cTrader (stop, target or app)"),
            "opened_at": _iso(d.get("opened_at")),
            "closed_at": _iso(d["closed_at"]),
        })
    return data
