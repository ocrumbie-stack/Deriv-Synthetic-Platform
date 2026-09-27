"""Daily check of recorded trade P&L against Deriv's own profit table.

Every closed trade from the last few days is matched to its Deriv contract
and, where the platform's figure differs from what Deriv actually paid out,
corrected - along with the bot/pair session totals it fed into. Anything
that can't be matched with confidence is only flagged in the log, never
guessed at.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.deriv import DerivClient
from app.models import BotPair, ExecutionStatus, PositionStatus, SignalBot, Trade

logger = logging.getLogger("uvicorn.error")

FIRST_RUN_DELAY = 300.0
RUN_INTERVAL = 24 * 3600.0
# Covers a missed day (e.g. a restart right before the scheduled run).
LOOKBACK = timedelta(days=3)
TOLERANCE = 0.01
# Deriv has been seen filling a buy ~30s after the signal that triggered it.
PURCHASE_WINDOW_BEFORE = 5
PURCHASE_WINDOW_AFTER = 90


@dataclass
class AuditResult:
    ran_at: datetime | None = None
    execution_mode: str | None = None
    checked: int = 0
    matched: int = 0
    corrected: list[dict] = field(default_factory=list)
    flagged: list[dict] = field(default_factory=list)
    error: str | None = None


last_result = AuditResult()


def _utc_ts(value: datetime) -> float:
    return value.replace(tzinfo=timezone.utc).timestamp()


def _find_contract(trade: Trade, transactions: list[dict], claimed: set) -> tuple[dict | None, str | None]:
    """Return (transaction, None) for a confident match, else (None, reason)."""
    by_id = [t for t in transactions if str(t.get("contract_id")) == str(trade.exchange_order_id or "")]
    if len(by_id) == 1:
        return by_id[0], None

    # Trades closed before contract ids were kept have only the sale's
    # transaction id stored, so fall back to what identifies the buy.
    contract_type = "MULTUP" if trade.direction.value == "long" else "MULTDOWN"
    opened = _utc_ts(trade.opened_at)
    candidates = [
        t for t in transactions
        if t.get("contract_id") not in claimed
        and str(t.get("shortcode") or "").upper().startswith(contract_type + "_")
        and abs(float(t.get("buy_price") or 0) - trade.size) < TOLERANCE
        and opened - PURCHASE_WINDOW_BEFORE <= float(t.get("purchase_time") or 0) <= opened + PURCHASE_WINDOW_AFTER
    ]
    if len(candidates) == 1:
        return candidates[0], None
    if not candidates:
        return None, "no matching Deriv contract"
    return None, f"{len(candidates)} possible Deriv contracts"


def _apply_correction(db, trade: Trade, real_net: float) -> float:
    delta = round(real_net - trade.net_result, 8)
    trade.profit_loss = round(real_net + trade.fees, 8)
    trade.net_result = real_net
    bot = db.scalar(select(SignalBot).where(SignalBot.name == trade.strategy_name))
    if bot:
        # Only totals whose current session includes this trade - a session
        # restarted after the trade closed never counted it.
        if bot.session_started_at is None or trade.closed_at >= bot.session_started_at:
            bot.session_pnl = round((bot.session_pnl or 0.0) + delta, 8)
        pair = db.scalar(select(BotPair).where(BotPair.bot_id == bot.id, BotPair.symbol == trade.symbol))
        if pair and (pair.session_started_at is None or trade.closed_at >= pair.session_started_at):
            pair.session_pnl = round((pair.session_pnl or 0.0) + delta, 8)
    return delta


async def run_audit() -> AuditResult:
    global last_result
    mode = settings.execution_mode.lower()
    result = AuditResult(ran_at=datetime.utcnow(), execution_mode=mode)
    since = datetime.utcnow() - LOOKBACK
    try:
        # A little extra history so trades opened just before the window
        # still find their contract.
        transactions = await DerivClient().get_profit_table(_utc_ts(since - timedelta(days=1)))
        with SessionLocal() as db:
            trades = list(db.scalars(
                select(Trade).where(
                    Trade.status == PositionStatus.closed,
                    Trade.execution_status != ExecutionStatus.failed,
                    Trade.execution_mode == mode,
                    Trade.closed_at >= since,
                ).order_by(Trade.opened_at)
            ))
            claimed: set = set()
            for trade in trades:
                result.checked += 1
                tx, reason = _find_contract(trade, transactions, claimed)
                if not tx:
                    result.flagged.append({"trade_id": trade.id, "symbol": trade.symbol, "reason": reason})
                    continue
                claimed.add(tx.get("contract_id"))
                result.matched += 1
                real_net = round(float(tx["sell_price"]) - float(tx["buy_price"]) - trade.fees, 2)
                trade.exchange_order_id = str(tx["contract_id"])
                if abs(real_net - trade.net_result) > TOLERANCE:
                    old_net = trade.net_result
                    delta = _apply_correction(db, trade, real_net)
                    result.corrected.append({"trade_id": trade.id, "old_net": old_net, "new_net": real_net, "delta": delta})
            db.commit()
    except Exception as exc:
        result.error = str(exc)
        logger.exception("Trade audit failed")
    for item in result.corrected:
        logger.warning(
            "Trade audit corrected trade %s: %.2f -> %.2f (Deriv profit table)",
            item["trade_id"], item["old_net"], item["new_net"],
        )
    for item in result.flagged:
        logger.warning("Trade audit could not verify trade %s (%s): %s - needs manual review", item["trade_id"], item["symbol"], item["reason"])
    logger.info(
        "Trade audit (%s): %s checked, %s matched, %s corrected, %s flagged",
        mode, result.checked, result.matched, len(result.corrected), len(result.flagged),
    )
    last_result = result
    return result


class TradeAuditScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        await asyncio.sleep(FIRST_RUN_DELAY)
        while True:
            await run_audit()
            await asyncio.sleep(RUN_INTERVAL)


trade_audit_scheduler = TradeAuditScheduler()
