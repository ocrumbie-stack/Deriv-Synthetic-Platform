"""The Home page: every market's balance, open positions and today's
results on one screen, for the current demo/live mode only.

Markets are named generically (Synthetic, Forex, Crypto) - which broker runs
each one is the market pages' business, not this page's. Each market's
broker calls run side by side under their own timeout, so one slow or failed
broker shows as unavailable on its card instead of holding up the page.
"""

import asyncio
from collections.abc import Awaitable, Callable

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app import crypto
from app.blofin import configured as crypto_configured
from app.config import settings
from app.models import CryptoSignal, PositionStatus, Signal, Trade
from app.services import account_exposure, daily_account_net, get_risk_settings, local_period_start

TIMEOUT = 8  # seconds per market
ACTIVITY_LIMIT = 10


def _iso(moment) -> str | None:
    return moment.isoformat() if moment else None


async def _within(call: Awaitable) -> tuple[object, str | None]:
    """The call's result and no error, or None and why it failed."""
    try:
        return await asyncio.wait_for(call, TIMEOUT), None
    except asyncio.TimeoutError:
        return None, "timed out"
    except Exception as e:
        return None, str(e) or "fetch failed"


def _market(key: str, label: str, currency: str | None) -> dict:
    return {
        "key": key,
        "label": label,
        "currency": currency,
        "connected": False,
        "balance": None,
        "open_positions": None,
        "today": None,
        "last_signal_at": None,
        "error": None,
    }


def _synthetic_today(db: Session, mode: str) -> dict:
    trades, wins, net = db.execute(
        select(
            func.count(Trade.id),
            func.coalesce(func.sum(case((Trade.net_result > 0, 1), else_=0)), 0),
            func.coalesce(func.sum(Trade.net_result), 0.0),
        ).where(
            Trade.status == PositionStatus.closed,
            Trade.closed_at >= local_period_start("today"),
            Trade.execution_mode == mode,
        )
    ).one()
    return {"trades": int(trades), "wins": int(wins), "net": round(float(net), 8)}


async def _synthetic(db: Session, mode: str, balance: Callable[[], Awaitable[dict]]) -> dict:
    market = _market("synthetic", "Synthetic", "USD")
    market["connected"] = True
    market["open_positions"] = int(db.scalar(
        select(func.count(Trade.id)).where(Trade.status == PositionStatus.open, Trade.execution_mode == mode)
    ) or 0)
    market["today"] = _synthetic_today(db, mode)
    market["last_signal_at"] = _iso(db.scalar(select(func.max(Signal.created_at)).where(Signal.execution_mode == mode)))
    data, error = await _within(balance())
    if data and data.get("equity") is not None:
        market["balance"] = {key: data.get(key) for key in ("equity", "available", "unrealized_pnl")}
    else:
        market["error"] = error or (data or {}).get("error") or "fetch failed"
    return market


async def _crypto(db: Session, mode: str) -> dict:
    market = _market("crypto", "Crypto", "USDT")
    market["connected"] = crypto_configured(mode)
    market["last_signal_at"] = _iso(db.scalar(
        select(func.max(CryptoSignal.created_at)).where(CryptoSignal.execution_mode == mode)
    ))
    if not market["connected"]:
        return market
    data, error = await _within(crypto.overview(db))
    if data and not data.get("error"):
        market["balance"] = data.get("balance")
        market["open_positions"] = len(data.get("positions") or [])
    else:
        market["error"] = error or data.get("error")
    return market


def _forex() -> dict:
    # No forex broker is wired up yet.
    return _market("forex", "Forex", None)


def _risk(db: Session) -> dict:
    risk = get_risk_settings(db)
    return {
        "emergency_stop": bool(settings.emergency_stop or risk.emergency_stop),
        "today_net": round(daily_account_net(db), 8),
        "daily_loss_limit": risk.account_daily_loss_limit,
        "exposure": round(account_exposure(db), 8),
        "max_exposure": risk.max_account_exposure,
    }


def _activity(db: Session, mode: str) -> list[dict]:
    """The latest signals from every market, newest first."""
    synthetic = db.scalars(
        select(Signal).where(Signal.execution_mode == mode).order_by(Signal.id.desc()).limit(ACTIVITY_LIMIT)
    )
    crypto_signals = db.scalars(
        select(CryptoSignal).where(CryptoSignal.execution_mode == mode).order_by(CryptoSignal.id.desc()).limit(ACTIVITY_LIMIT)
    )
    rows = [
        {
            "market": "synthetic",
            "created_at": s.created_at,
            "strategy_name": s.strategy_name,
            "symbol": s.symbol,
            "action": s.action.value,
            "direction": s.direction.value if s.direction else None,
            "status": s.status.value,
            "reason": s.rejection_reason,
        }
        for s in synthetic
    ] + [
        {
            "market": "crypto",
            "created_at": s.created_at,
            "strategy_name": s.strategy_name,
            "symbol": s.symbol,
            "action": s.action.value,
            "direction": s.direction.value if s.direction else None,
            "status": s.status.value,
            "reason": s.reason,
        }
        for s in crypto_signals
    ]
    rows.sort(key=lambda row: row["created_at"], reverse=True)
    return [{**row, "created_at": _iso(row["created_at"])} for row in rows[:ACTIVITY_LIMIT]]


async def summary(db: Session, synthetic_balance: Callable[[], Awaitable[dict]]) -> dict:
    # Loading the risk row applies the dashboard's demo/live toggle, so it
    # must come before reading the mode.
    risk = _risk(db)
    mode = settings.execution_mode.lower()
    activity = _activity(db, mode)
    db.commit()
    synthetic, crypto_market = await asyncio.gather(_synthetic(db, mode, synthetic_balance), _crypto(db, mode))
    return {
        "mode": mode,
        "markets": [synthetic, _forex(), crypto_market],
        "risk": risk,
        "activity": activity,
    }
