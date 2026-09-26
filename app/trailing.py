"""Trailing stop for open bot trades.

Deriv multiplier contracts only support a fixed TP/SL, so the trailing part
runs here: one long-lived WebSocket subscribes to proposal_open_contract for
each open trade whose bot has a trail configured. Deriv then pushes the live
profit on every tick - no per-trade polling - and the trade is sold once
profit falls trail_distance below its peak. The fixed Deriv-side SL stays as
the backstop for any time this connection is down.
"""

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass

import websockets
from sqlalchemy import select, update

from app.config import settings
from app.database import SessionLocal
from app.deriv import DerivClient, DerivExecutionError
from app.models import PositionStatus, SignalBot, Trade
from app.services import IMPLAUSIBLE_PROFIT_STAKE_MULTIPLE, close_position_with_signal, reconcile_open_trade

logger = logging.getLogger("uvicorn.error")

# How often the set of subscribed trades is re-read from the DB (new entries,
# closed trades, edited bot trail settings) and peaks are persisted.
SYNC_INTERVAL = 5.0
RECONNECT_DELAY = 10.0
# A contract Deriv refused to stream isn't retried on every sync.
SUBSCRIBE_RETRY_DELAY = 60.0


@dataclass
class _Tracked:
    trade_id: int
    stake: float
    start: float
    distance: float
    peak: float | None
    saved_peak: float | None
    subscription_id: str | None = None


class TrailingStopMonitor:
    def __init__(self) -> None:
        self._tracked: dict[str, _Tracked] = {}  # keyed by Deriv contract_id
        self._closing: set[str] = set()
        self._subscribe_failed_at: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        # The event loop only holds weak references to tasks.
        self._background: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

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
        while True:
            try:
                if self._load_trailed(settings.execution_mode.lower()):
                    await self._session()
                else:
                    # Nothing to trail - don't hold a Deriv connection open.
                    await asyncio.sleep(SYNC_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Trailing stop connection failed; reconnecting in %ss", RECONNECT_DELAY)
                await asyncio.sleep(RECONNECT_DELAY)
            finally:
                # Subscriptions die with their connection.
                self._save_peaks()
                self._tracked.clear()

    async def _session(self) -> None:
        mode = settings.execution_mode.lower()
        uri = await DerivClient()._authenticated_uri()
        async with websockets.connect(uri, open_timeout=15, close_timeout=5) as ws:
            next_sync = 0.0
            # A demo<->live switch needs the other account's credentials, so
            # drop this connection and let _run open a fresh one.
            while settings.execution_mode.lower() == mode:
                if time.monotonic() >= next_sync:
                    await self._sync(ws, self._load_trailed(mode))
                    self._save_peaks()
                    if not self._tracked:
                        return
                    next_sync = time.monotonic() + SYNC_INTERVAL
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, next_sync - time.monotonic()))
                except asyncio.TimeoutError:
                    continue
                self._handle(json.loads(raw))

    @staticmethod
    def _load_trailed(mode: str) -> dict[str, _Tracked]:
        with SessionLocal() as db:
            rows = db.execute(
                select(Trade, SignalBot)
                .join(SignalBot, SignalBot.name == Trade.strategy_name)
                .where(
                    Trade.status == PositionStatus.open,
                    Trade.execution_mode == mode,
                    Trade.exchange_order_id.is_not(None),
                    Trade.exchange_order_id != "",
                    SignalBot.trail_distance_pct.is_not(None),
                    SignalBot.trail_distance_pct > 0,
                )
            ).all()
        trailed = {}
        for trade, bot in rows:
            distance = trade.size * bot.trail_distance_pct / 100
            start = trade.size * (bot.trail_start_pct or bot.trail_distance_pct) / 100
            trailed[trade.exchange_order_id] = _Tracked(
                trade_id=trade.id,
                stake=trade.size,
                start=start,
                distance=distance,
                peak=trade.peak_profit,
                saved_peak=trade.peak_profit,
            )
        return trailed

    async def _sync(self, ws, wanted: dict[str, _Tracked]) -> None:
        for contract_id in list(self._tracked):
            if contract_id not in wanted:
                tracked = self._tracked.pop(contract_id)
                if tracked.subscription_id:
                    await ws.send(json.dumps({"forget": tracked.subscription_id}))
        now = time.monotonic()
        for contract_id, info in wanted.items():
            tracked = self._tracked.get(contract_id)
            if tracked:
                # Pick up edited bot trail settings; keep the live peak.
                tracked.start, tracked.distance = info.start, info.distance
                continue
            if now - self._subscribe_failed_at.get(contract_id, -math.inf) < SUBSCRIBE_RETRY_DELAY:
                continue
            self._tracked[contract_id] = info
            await ws.send(json.dumps({
                "proposal_open_contract": 1,
                "contract_id": int(contract_id),
                "subscribe": 1,
                "req_id": info.trade_id,
            }))

    def _handle(self, msg: dict) -> None:
        if msg.get("error"):
            for contract_id, tracked in list(self._tracked.items()):
                if tracked.trade_id == msg.get("req_id"):
                    self._tracked.pop(contract_id)
                    self._subscribe_failed_at[contract_id] = time.monotonic()
                    logger.warning("Trailing stop: Deriv refused to stream contract %s: %s", contract_id, msg["error"])
            return
        poc = msg.get("proposal_open_contract")
        if not isinstance(poc, dict):
            return
        contract_id = str(poc.get("contract_id") or "")
        tracked = self._tracked.get(contract_id)
        if not tracked:
            return
        subscription_id = (msg.get("subscription") or {}).get("id")
        if subscription_id:
            tracked.subscription_id = subscription_id

        if poc.get("is_sold"):
            # Deriv closed it itself (TP/SL hit, stop-out) - record it now
            # rather than waiting for the next exit webhook or dashboard poll.
            self._tracked.pop(contract_id)
            if contract_id not in self._closing:
                self._spawn(self._reconcile(tracked.trade_id))
            return

        try:
            profit = float(poc.get("profit"))
        except (TypeError, ValueError):
            return
        # A bogus spike would otherwise set an unreachable peak and trigger
        # an immediate close (see IMPLAUSIBLE_PROFIT_STAKE_MULTIPLE).
        if not math.isfinite(profit) or abs(profit) > max(tracked.stake, 1.0) * IMPLAUSIBLE_PROFIT_STAKE_MULTIPLE:
            return
        if tracked.peak is None or profit > tracked.peak:
            tracked.peak = profit
        if (
            tracked.peak >= tracked.start
            and profit <= tracked.peak - tracked.distance
            and contract_id not in self._closing
        ):
            self._closing.add(contract_id)
            self._spawn(self._close(contract_id, tracked, profit))

    async def _close(self, contract_id: str, tracked: _Tracked, profit: float) -> None:
        try:
            with SessionLocal() as db:
                trade = db.get(Trade, tracked.trade_id)
                if not trade or trade.status != PositionStatus.open:
                    return
                trade.peak_profit = tracked.peak
                await close_position_with_signal(
                    db, trade, None, "trailing_stop",
                    {
                        "trade_id": trade.id,
                        "peak_profit": tracked.peak,
                        "trigger_profit": profit,
                        "trail_start": tracked.start,
                        "trail_distance": tracked.distance,
                    },
                )
            logger.info(
                "Trailing stop closed trade %s (peak %.2f, triggered at %.2f)", tracked.trade_id, tracked.peak, profit
            )
        except DerivExecutionError as exc:
            # Stays subscribed, so the next tick still below the trail retries.
            logger.warning("Trailing stop failed to close trade %s: %s", tracked.trade_id, exc)
        except Exception:
            logger.exception("Trailing stop failed to close trade %s", tracked.trade_id)
        finally:
            self._closing.discard(contract_id)

    @staticmethod
    async def _reconcile(trade_id: int) -> None:
        try:
            with SessionLocal() as db:
                trade = db.get(Trade, trade_id)
                if trade and trade.status == PositionStatus.open and await reconcile_open_trade(db, trade):
                    db.commit()
        except Exception:
            logger.exception("Trailing stop failed to record Deriv-side close of trade %s", trade_id)

    def _save_peaks(self) -> None:
        dirty = [t for t in self._tracked.values() if t.peak is not None and t.peak != t.saved_peak]
        if not dirty:
            return
        try:
            with SessionLocal() as db:
                for tracked in dirty:
                    db.execute(update(Trade).where(Trade.id == tracked.trade_id).values(peak_profit=tracked.peak))
                db.commit()
            for tracked in dirty:
                tracked.saved_peak = tracked.peak
        except Exception:
            logger.exception("Trailing stop failed to save peak profits")


trailing_monitor = TrailingStopMonitor()
