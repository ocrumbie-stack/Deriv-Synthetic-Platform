"""Dashboard routes for the cTrader logins and account picks (see app/ctrader.py).

Each market (Synthetic → CFDs, Forex) has its own login: /ctrader/connect
?market=... sends the browser to cTrader to grant trading access,
/ctrader/callback stores that market's tokens and the accounts linked to
the cTrader ID, and the /api/ctrader endpoints (?market=..., Synthetic when
left out) show and pick the market's demo and live account. Synthetic only
offers Deriv accounts; Forex offers every other broker's. The tokens never
leave the server.
"""

import hmac
import json
import secrets
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import ctrader
from app.auth import _is_https
from app.config import settings
from app.database import get_db
from app.models import CTraderConnection

router = APIRouter()

STATE_COOKIE = "ctrader_state"
STATE_MAX_AGE = 600
# After the cTrader ID login, back to the market's Overview.
DONE_PAGES = {"synthetic": "/#cfd-overview", "forex": "/#forex-overview"}
NO_ACCOUNTS = {
    "synthetic": "This cTrader ID has no Deriv accounts. Synthetic trades Deriv's synthetic indices, so log in with the cTrader ID that holds your Deriv cTrader account.",
    "forex": "This cTrader ID only has Deriv accounts. Deriv's cTrader belongs under Synthetic → CFDs; log in here with your forex broker's cTrader ID.",
}


def _market(market: str | None) -> str:
    market = (market or "synthetic").lower()
    if market not in ctrader.MARKETS:
        raise HTTPException(status_code=422, detail="market must be synthetic or forex.")
    return market


def _redirect_uri(request: Request) -> str:
    """Must match one of the app's redirect URLs on openapi.ctrader.com."""
    if settings.public_url:
        return settings.public_url.rstrip("/") + "/ctrader/callback"
    scheme = "https" if _is_https(request) else "http"
    return f"{scheme}://{request.headers.get('host', request.url.netloc)}/ctrader/callback"


def _row(db: Session, market: str) -> CTraderConnection:
    row = db.get(CTraderConnection, ctrader.MARKETS[market])
    if not row:
        row = CTraderConnection(id=ctrader.MARKETS[market])
        db.add(row)
        db.flush()
    return row


def _all_accounts(row: CTraderConnection) -> list[dict]:
    try:
        return json.loads(row.accounts or "[]")
    except ValueError:
        return []


def _accounts(row: CTraderConnection, market: str) -> list[dict]:
    """The linked accounts this market may pick from."""
    return ctrader.accounts_for_market(market, _all_accounts(row))


def _done(result: str, market: str, detail: str | None = None) -> RedirectResponse:
    url = f"/?ctrader={result}&market={market}" + (f"&detail={quote(detail[:300])}" if detail else "") + DONE_PAGES[market][1:]
    response = RedirectResponse(url, status_code=303)
    response.delete_cookie(STATE_COOKIE)
    return response


def pick_default_accounts(row: CTraderConnection, accounts: list[dict]) -> None:
    """Keep each mode's pick if it is still linked; otherwise take the only
    demo (or only live) account when there is exactly one. `accounts` is
    already narrowed to the market."""
    for live, field in ((False, "demo_account_id"), (True, "live_account_id")):
        ids = [a["account_id"] for a in accounts if a["live"] == live]
        current = getattr(row, field)
        if current not in ids:
            setattr(row, field, ids[0] if len(ids) == 1 else None)


def move_forex_login_off_synthetic(db: Session) -> None:
    """One-off: before Forex had its own login, every cTrader login was kept
    as Synthetic's. A Synthetic login with linked accounts but no Deriv ones
    (e.g. a Fusion Markets login) is moved to Forex, if Forex has never had
    a login of its own."""
    synthetic = db.get(CTraderConnection, ctrader.MARKETS["synthetic"])
    if not synthetic or not synthetic.access_token or db.get(CTraderConnection, ctrader.MARKETS["forex"]):
        return
    accounts = _all_accounts(synthetic)
    if not accounts or ctrader.accounts_for_market("synthetic", accounts):
        return
    forex = CTraderConnection(
        id=ctrader.MARKETS["forex"],
        access_token=synthetic.access_token,
        refresh_token=synthetic.refresh_token,
        expires_at=synthetic.expires_at,
        accounts=synthetic.accounts,
        demo_account_id=synthetic.demo_account_id,
        live_account_id=synthetic.live_account_id,
    )
    db.add(forex)
    synthetic.access_token = synthetic.refresh_token = synthetic.expires_at = None
    synthetic.accounts = None
    synthetic.demo_account_id = synthetic.live_account_id = None


@router.get("/ctrader/connect")
def connect(request: Request, market: str | None = None) -> RedirectResponse:
    market = _market(market)
    missing = ctrader.missing_settings()
    if missing:
        raise HTTPException(status_code=400, detail=f"Set {', '.join(missing)} first (from openapi.ctrader.com > Applications > Credentials).")
    # The market rides in the state, which cTrader sends back unchanged.
    state = f"{market}.{secrets.token_urlsafe(24)}"
    response = RedirectResponse(ctrader.authorize_url(_redirect_uri(request), state), status_code=303)
    response.set_cookie(STATE_COOKIE, state, max_age=STATE_MAX_AGE, httponly=True, samesite="lax", secure=_is_https(request))
    return response


@router.get("/ctrader/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    expected = request.cookies.get(STATE_COOKIE)
    market = (expected or "").split(".", 1)[0]
    if market not in ctrader.MARKETS:
        market = "synthetic"
    if error:
        return _done("error", market, f"cTrader said: {error}")
    if not code or not state or not expected or not hmac.compare_digest(state, expected):
        return _done("error", market, "The cTrader login expired or didn't come from this dashboard - try Connect again.")
    try:
        tokens = await ctrader.exchange_code(code, _redirect_uri(request))
        row = _row(db, market)
        row.access_token = tokens["access_token"]
        row.refresh_token = tokens["refresh_token"]
        row.expires_at = tokens["expires_at"]
        db.commit()
        accounts = await ctrader.CTraderClient(account_id=0, live=False, market=market).list_accounts()
    except ctrader.CTraderError as exc:
        return _done("error", market, str(exc))
    row.accounts = json.dumps(accounts)
    usable = ctrader.accounts_for_market(market, accounts)
    pick_default_accounts(row, usable)
    db.commit()
    if not usable:
        return _done("error", market, NO_ACCOUNTS[market])
    return _done("connected", market)


def status_out(row: CTraderConnection | None, market: str = "synthetic") -> dict:
    connected = bool(row and row.access_token)
    accounts = _accounts(row, market) if row else []
    return {
        "market": market,
        "configured": ctrader.configured(),
        "missing": ctrader.missing_settings(),
        "connected": connected,
        "accounts": accounts,
        # Linked to the login but belonging to the other market.
        "other_accounts": len(_all_accounts(row)) - len(accounts) if row else 0,
        "demo_account_id": row.demo_account_id if row else None,
        "live_account_id": row.live_account_id if row else None,
        "expires_at": row.expires_at.isoformat() + "Z" if connected and row.expires_at else None,
    }


@router.get("/api/ctrader/status")
def status(market: str | None = None, db: Session = Depends(get_db)) -> dict:
    from app.services import get_risk_settings

    market = _market(market)
    get_risk_settings(db)  # applies the dashboard's demo/live switch
    row = db.get(CTraderConnection, ctrader.MARKETS[market])
    return {**status_out(row, market), "mode": settings.execution_mode.lower()}


class AccountPick(BaseModel):
    demo_account_id: int | None = None
    live_account_id: int | None = None


@router.patch("/api/ctrader/accounts")
def pick_accounts(data: AccountPick, market: str | None = None, db: Session = Depends(get_db)) -> dict:
    market = _market(market)
    row = db.get(CTraderConnection, ctrader.MARKETS[market])
    if not row or not row.access_token:
        raise HTTPException(status_code=409, detail="Connect cTrader first.")
    accounts = {a["account_id"]: a for a in _accounts(row, market)}
    for field, live in (("demo_account_id", False), ("live_account_id", True)):
        if field not in data.model_fields_set:
            continue
        value = getattr(data, field)
        if value is not None:
            account = accounts.get(value)
            if not account:
                raise HTTPException(status_code=422, detail=f"Account {value} isn't linked to this login or can't trade here.")
            if account["live"] != live:
                kind = "live" if account["live"] else "demo"
                raise HTTPException(status_code=422, detail=f"Account {account.get('login') or value} is a {kind} account.")
        setattr(row, field, value)
    db.commit()
    return status_out(row, market)


@router.post("/api/ctrader/refresh-accounts")
async def refresh_accounts(market: str | None = None, db: Session = Depends(get_db)) -> dict:
    market = _market(market)
    row = db.get(CTraderConnection, ctrader.MARKETS[market])
    if not row or not row.access_token:
        raise HTTPException(status_code=409, detail="Connect cTrader first.")
    try:
        accounts = await ctrader.CTraderClient(account_id=0, live=False, market=market).list_accounts()
    except ctrader.CTraderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    db.refresh(row)
    row.accounts = json.dumps(accounts)
    pick_default_accounts(row, ctrader.accounts_for_market(market, accounts))
    db.commit()
    return status_out(row, market)


@router.post("/api/ctrader/disconnect")
def disconnect(market: str | None = None, db: Session = Depends(get_db)) -> dict:
    market = _market(market)
    row = db.get(CTraderConnection, ctrader.MARKETS[market])
    if row:
        row.access_token = row.refresh_token = row.expires_at = None
        db.commit()
    ctrader._Session.reset()
    return status_out(row, market)


@router.get("/api/ctrader/account")
async def account(mode: str | None = None, market: str | None = None) -> dict:
    """Balance and details of the market's demo or live account (current mode by default)."""
    market = _market(market)
    mode = (mode or settings.execution_mode).lower()
    try:
        return await ctrader.CTraderClient(live=mode == "live", market=market).account_info()
    except ctrader.CTraderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/api/ctrader/symbol")
async def symbol(symbol: str, mode: str | None = None, market: str | None = None) -> dict:
    """Which symbol on the market's account an alert's symbol trades, and its lot size."""
    market = _market(market)
    mode = (mode or settings.execution_mode).lower()
    try:
        found = await ctrader.CTraderClient(live=mode == "live", market=market).resolve_symbol(symbol)
    except ctrader.CTraderError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {
        "symbol_id": int(found["symbolId"]),
        "name": found.get("symbolName"),
        "description": found.get("description"),
        "digits": found.get("digits"),
        "lot_size_units": int(found.get("lotSize") or 0) / 100,
        "min_lots": ctrader.volume_to_lots(int(found.get("minVolume") or 0), found),
        "max_lots": ctrader.volume_to_lots(int(found.get("maxVolume") or 0), found),
        "step_lots": ctrader.volume_to_lots(int(found.get("stepVolume") or 0), found),
    }
