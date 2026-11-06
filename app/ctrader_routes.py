"""Dashboard routes for the cTrader login and account pick (see app/ctrader.py).

/ctrader/connect sends the browser to cTrader to grant trading access,
/ctrader/callback stores the tokens and the accounts linked to that cTrader
ID, and the /api/ctrader endpoints show and pick the demo and live account.
The tokens never leave the server.
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
# After the cTrader ID login, back to Synthetic → CFDs → cTrader Account.
DONE_PAGE = "/#cfd-account"


def _redirect_uri(request: Request) -> str:
    """Must match one of the app's redirect URLs on openapi.ctrader.com."""
    if settings.public_url:
        return settings.public_url.rstrip("/") + "/ctrader/callback"
    scheme = "https" if _is_https(request) else "http"
    return f"{scheme}://{request.headers.get('host', request.url.netloc)}/ctrader/callback"


def _row(db: Session) -> CTraderConnection:
    row = db.get(CTraderConnection, 1)
    if not row:
        row = CTraderConnection(id=1)
        db.add(row)
        db.flush()
    return row


def _accounts(row: CTraderConnection) -> list[dict]:
    try:
        return json.loads(row.accounts or "[]")
    except ValueError:
        return []


def _done(result: str, detail: str | None = None) -> RedirectResponse:
    url = f"/?ctrader={result}" + (f"&detail={quote(detail[:300])}" if detail else "") + DONE_PAGE[1:]
    response = RedirectResponse(url, status_code=303)
    response.delete_cookie(STATE_COOKIE)
    return response


def pick_default_accounts(row: CTraderConnection, accounts: list[dict]) -> None:
    """Keep each mode's pick if it is still linked; otherwise take the only
    demo (or only live) account when there is exactly one."""
    for live, field in ((False, "demo_account_id"), (True, "live_account_id")):
        ids = [a["account_id"] for a in accounts if a["live"] == live]
        current = getattr(row, field)
        if current not in ids:
            setattr(row, field, ids[0] if len(ids) == 1 else None)


@router.get("/ctrader/connect")
def connect(request: Request) -> RedirectResponse:
    missing = ctrader.missing_settings()
    if missing:
        raise HTTPException(status_code=400, detail=f"Set {', '.join(missing)} first (from openapi.ctrader.com > Applications > Credentials).")
    state = secrets.token_urlsafe(24)
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
    if error:
        return _done("error", f"cTrader said: {error}")
    if not code or not state or not expected or not hmac.compare_digest(state, expected):
        return _done("error", "The cTrader login expired or didn't come from this dashboard - try Connect again.")
    try:
        tokens = await ctrader.exchange_code(code, _redirect_uri(request))
        row = _row(db)
        row.access_token = tokens["access_token"]
        row.refresh_token = tokens["refresh_token"]
        row.expires_at = tokens["expires_at"]
        db.commit()
        accounts = await ctrader.CTraderClient(account_id=0, live=False).list_accounts()
    except ctrader.CTraderError as exc:
        return _done("error", str(exc))
    row.accounts = json.dumps(accounts)
    pick_default_accounts(row, accounts)
    db.commit()
    return _done("connected")


def status_out(row: CTraderConnection | None) -> dict:
    connected = bool(row and row.access_token)
    return {
        "configured": ctrader.configured(),
        "missing": ctrader.missing_settings(),
        "connected": connected,
        "accounts": _accounts(row) if row else [],
        "demo_account_id": row.demo_account_id if row else None,
        "live_account_id": row.live_account_id if row else None,
        "expires_at": row.expires_at.isoformat() + "Z" if connected and row.expires_at else None,
    }


@router.get("/api/ctrader/status")
def status(db: Session = Depends(get_db)) -> dict:
    from app.services import get_risk_settings

    get_risk_settings(db)  # applies the dashboard's demo/live switch
    return {**status_out(db.get(CTraderConnection, 1)), "mode": settings.execution_mode.lower()}


class AccountPick(BaseModel):
    demo_account_id: int | None = None
    live_account_id: int | None = None


@router.patch("/api/ctrader/accounts")
def pick_accounts(data: AccountPick, db: Session = Depends(get_db)) -> dict:
    row = db.get(CTraderConnection, 1)
    if not row or not row.access_token:
        raise HTTPException(status_code=409, detail="Connect cTrader first.")
    accounts = {a["account_id"]: a for a in _accounts(row)}
    for field, live in (("demo_account_id", False), ("live_account_id", True)):
        if field not in data.model_fields_set:
            continue
        value = getattr(data, field)
        if value is not None:
            account = accounts.get(value)
            if not account:
                raise HTTPException(status_code=422, detail=f"Account {value} isn't linked to this cTrader ID.")
            if account["live"] != live:
                kind = "live" if account["live"] else "demo"
                raise HTTPException(status_code=422, detail=f"Account {account.get('login') or value} is a {kind} account.")
        setattr(row, field, value)
    db.commit()
    return status_out(row)


@router.post("/api/ctrader/refresh-accounts")
async def refresh_accounts(db: Session = Depends(get_db)) -> dict:
    row = db.get(CTraderConnection, 1)
    if not row or not row.access_token:
        raise HTTPException(status_code=409, detail="Connect cTrader first.")
    try:
        accounts = await ctrader.CTraderClient(account_id=0, live=False).list_accounts()
    except ctrader.CTraderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    db.refresh(row)
    row.accounts = json.dumps(accounts)
    pick_default_accounts(row, accounts)
    db.commit()
    return status_out(row)


@router.post("/api/ctrader/disconnect")
def disconnect(db: Session = Depends(get_db)) -> dict:
    row = db.get(CTraderConnection, 1)
    if row:
        row.access_token = row.refresh_token = row.expires_at = None
        db.commit()
    ctrader._Session.reset()
    return status_out(row)


@router.get("/api/ctrader/account")
async def account(mode: str | None = None) -> dict:
    """Balance and details of the demo or live account (current mode by default)."""
    mode = (mode or settings.execution_mode).lower()
    try:
        return await ctrader.CTraderClient(live=mode == "live").account_info()
    except ctrader.CTraderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/api/ctrader/symbol")
async def symbol(symbol: str, mode: str | None = None) -> dict:
    """Which symbol on the account an alert's symbol trades, and its lot size."""
    mode = (mode or settings.execution_mode).lower()
    try:
        found = await ctrader.CTraderClient(live=mode == "live").resolve_symbol(symbol)
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
