"""cTrader Open API client - lot-sized CFD trading on any cTrader broker.

Deriv's cTrader carries the synthetic indices (Volatility 75 Index, ...),
and other brokers' cTrader accounts carry forex, so nothing here is
Deriv-specific: a trader logs in once with their cTrader ID, the platform
gets every trading account linked to it, and the dashboard picks one demo
and one live account. Execution follows the platform's demo/live switch.

Protocol (help.ctrader.com/open-api): JSON over WebSocket on port 5036.
Each message is {"clientMsgId", "payloadType", "payload"}. The app
authorises once per connection (client id + secret), then each trading
account is authorised with the OAuth access token. Demo and live accounts
live on different hosts, so there is one shared connection per host.

Units: volume is in 0.01 of a unit ("cents"), a symbol's lotSize is in
the same cents, relative SL/TP distances are in 1/100000 of a price, and
money (balance, swap, commission, P/L) is an integer scaled by
10^moneyDigits.

Not yet confirmed against a live call (check on the first demo order):
whether JSON enums are numbers (sent as numbers here), whether
relativeStopLoss/TakeProfit are accepted on MARKET orders (the docs only
rule out the absolute stopLoss/takeProfit), and the sign of commission and
swap on a closing deal (treated as signed, charges negative).
"""

import asyncio
import itertools
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

import httpx
import websockets

from app.config import settings

logger = logging.getLogger("uvicorn.error")

DEMO_HOST = "wss://demo.ctraderapi.com:5036"
LIVE_HOST = "wss://live.ctraderapi.com:5036"
AUTHORIZE_URL = "https://id.ctrader.com/my/settings/openapi/grantingaccess/"
TOKEN_URL = "https://openapi.ctrader.com/apps/token"

# payloadType values (ProtoOAPayloadType / ProtoPayloadType).
ERROR_RES = 50
HEARTBEAT = 51
APP_AUTH_REQ, APP_AUTH_RES = 2100, 2101
ACCOUNT_AUTH_REQ, ACCOUNT_AUTH_RES = 2102, 2103
NEW_ORDER_REQ = 2106
AMEND_SLTP_REQ = 2110
CLOSE_POSITION_REQ = 2111
SYMBOLS_LIST_REQ, SYMBOLS_LIST_RES = 2114, 2115
SYMBOL_BY_ID_REQ, SYMBOL_BY_ID_RES = 2116, 2117
TRADER_REQ, TRADER_RES = 2121, 2122
RECONCILE_REQ, RECONCILE_RES = 2124, 2125
EXECUTION_EVENT = 2126
ORDER_ERROR_EVENT = 2132
DEAL_LIST_REQ, DEAL_LIST_RES = 2133, 2134
OA_ERROR_RES = 2142
TOKEN_INVALIDATED_EVENT = 2147
CLIENT_DISCONNECT_EVENT = 2148
ACCOUNT_LIST_REQ, ACCOUNT_LIST_RES = 2149, 2150
ACCOUNT_DISCONNECT_EVENT = 2164

ORDER_TYPE_MARKET = 1
SIDE_BUY, SIDE_SELL = 1, 2
# ProtoOAExecutionType
EXEC_ORDER_ACCEPTED = 2
EXEC_ORDER_FILLED = 3
EXEC_ORDER_REPLACED = 4
EXEC_ORDER_CANCELLED = 5
EXEC_ORDER_EXPIRED = 6
EXEC_ORDER_REJECTED = 7
EXEC_ORDER_PARTIAL_FILL = 11

PRICE_SCALE = 100_000
REQUEST_TIMEOUT = 20.0
HEARTBEAT_INTERVAL = 10.0
RECONNECT_COOLDOWN = 5.0
# Refresh the ~30 day access token this long before it runs out.
TOKEN_REFRESH_MARGIN = timedelta(days=2)


class CTraderError(Exception):
    pass


# --- OAuth ---------------------------------------------------------------

def configured() -> bool:
    return bool(settings.ctrader_client_id.strip() and settings.ctrader_client_secret.strip())


def missing_settings() -> list[str]:
    return [
        name for name, value in (
            ("CTRADER_CLIENT_ID", settings.ctrader_client_id),
            ("CTRADER_CLIENT_SECRET", settings.ctrader_client_secret),
        ) if not value.strip()
    ]


def authorize_url(redirect_uri: str, state: str) -> str:
    """Where to send the browser so the trader can grant trading access.
    cTrader comes back to redirect_uri with ?code=... (valid one minute)."""
    query = urlencode({
        "client_id": settings.ctrader_client_id.strip(),
        "redirect_uri": redirect_uri,
        "scope": "trading",
        "product": "web",
        "state": state,
    })
    return f"{AUTHORIZE_URL}?{query}"


def _token_result(data: Any) -> dict:
    if not isinstance(data, dict) or data.get("errorCode") or not data.get("accessToken"):
        detail = (data or {}).get("description") or (data or {}).get("errorCode") if isinstance(data, dict) else data
        raise CTraderError(f"cTrader login failed: {detail or 'no access token returned'}")
    expires_in = int(data.get("expiresIn") or 2_628_000)
    return {
        "access_token": data["accessToken"],
        "refresh_token": data.get("refreshToken"),
        "expires_at": datetime.utcnow() + timedelta(seconds=expires_in),
    }


async def _token_request(params: dict) -> dict:
    params = {**params, "client_id": settings.ctrader_client_id.strip(), "client_secret": settings.ctrader_client_secret.strip()}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(TOKEN_URL, params=params)
    except httpx.HTTPError as exc:
        raise CTraderError(f"Could not reach cTrader to log in: {exc}") from exc
    try:
        data = response.json()
    except ValueError:
        raise CTraderError(f"cTrader login failed ({response.status_code}): {response.text[:200]}")
    return _token_result(data)


async def exchange_code(code: str, redirect_uri: str) -> dict:
    return await _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})


async def refresh_tokens(refresh_token: str) -> dict:
    return await _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})


# --- Token store (the single CTraderConnection row) ---------------------

def _load_row():
    from app.database import SessionLocal
    from app.models import CTraderConnection

    with SessionLocal() as db:
        row = db.get(CTraderConnection, 1)
        if row:
            db.expunge(row)
        return row


def _save_tokens(tokens: dict) -> None:
    from app.database import SessionLocal
    from app.models import CTraderConnection

    with SessionLocal() as db:
        row = db.get(CTraderConnection, 1) or CTraderConnection(id=1)
        row.access_token = tokens["access_token"]
        row.refresh_token = tokens.get("refresh_token") or row.refresh_token
        row.expires_at = tokens["expires_at"]
        db.add(row)
        db.commit()


_refresh_lock = asyncio.Lock()


async def stored_access_token(force_refresh: bool = False) -> str:
    """The saved access token, refreshed first when it is close to running
    out (or when cTrader just rejected it). Refreshing invalidates the old
    pair, so only one refresh runs at a time."""
    async with _refresh_lock:
        row = _load_row()
        if not row or not row.access_token:
            raise CTraderError("cTrader is not connected - log in with your cTrader ID on the Synthetic Overview first.")
        due = row.expires_at is None or row.expires_at - TOKEN_REFRESH_MARGIN <= datetime.utcnow()
        if (force_refresh or due) and row.refresh_token:
            tokens = await refresh_tokens(row.refresh_token)
            _save_tokens(tokens)
            return tokens["access_token"]
        return row.access_token


def account_id_for_mode(mode: str | None = None) -> int:
    """The cTrader account picked for demo or live (the current mode by
    default). Anything but exactly "live" is demo, as everywhere else."""
    live = (mode or settings.execution_mode).lower() == "live"
    row = _load_row()
    account_id = (row.live_account_id if live else row.demo_account_id) if row else None
    if not account_id:
        label = "live" if live else "demo"
        raise CTraderError(f"No cTrader {label} account is selected - pick one on the Synthetic Overview.")
    return int(account_id)


# --- Symbols and sizing ---------------------------------------------------

def symbol_key(value: str) -> str:
    """Comparable form of a symbol name: "Volatility 75 (1s) Index" and
    TradingView's VOLATILITY_75_1S_INDEX both become VOLATILITY_75_1S_INDEX.
    A TradingView exchange prefix (DERIV:, OANDA:) is dropped."""
    value = value.strip().rsplit(":", 1)[-1]
    return re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")


# Deriv API codes alerts already send for the Options account, as the names
# Deriv lists them under on cTrader.
_DERIV_CODE_PATTERNS = (
    (re.compile(r"^R_(\d+)$"), "VOLATILITY_{}_INDEX"),
    (re.compile(r"^1HZ(\d+)V$"), "VOLATILITY_{}_1S_INDEX"),
)


def symbol_candidates(raw: str) -> list[str]:
    key = symbol_key(raw)
    keys = [key]
    bare = raw.strip().rsplit(":", 1)[-1].upper()
    for pattern, template in _DERIV_CODE_PATTERNS:
        match = pattern.match(bare)
        if match:
            keys.append(template.format(match.group(1)))
    # Deriv's own forex codes (frxEURUSD) to the bare pair other brokers use.
    if bare.startswith("FRX") and len(bare) > 3:
        keys.append(symbol_key(bare[3:]))
    return keys


def match_symbol(raw: str, symbols: list[dict]) -> dict | None:
    """The account's symbol (a ProtoOALightSymbol) an alert's symbol means,
    matched on symbolName first and then description."""
    candidates = symbol_candidates(raw)
    for field in ("symbolName", "description"):
        index = {symbol_key(str(s.get(field) or "")): s for s in symbols if s.get(field)}
        for key in candidates:
            if key in index:
                return index[key]
    return None


def lots_to_volume(lots: float, symbol: dict) -> int:
    """Lots to the protocol's volume (0.01 of a unit), floored to the
    symbol's step and checked against its min/max. lotSize, stepVolume,
    minVolume and maxVolume are all in the same cents."""
    lot_size = int(symbol.get("lotSize") or 0)
    if lot_size <= 0:
        raise CTraderError(f"cTrader did not report a lot size for {symbol.get('symbolName') or symbol.get('symbolId')}.")
    step = int(symbol.get("stepVolume") or 1)
    raw = Decimal(str(lots)) * lot_size
    volume = int((raw / step).quantize(Decimal(1), rounding=ROUND_DOWN)) * step
    minimum = int(symbol.get("minVolume") or 0)
    maximum = int(symbol.get("maxVolume") or 0)
    if volume <= 0 or (minimum and volume < minimum):
        raise CTraderError(f"{lots:g} lots is below this symbol's minimum of {volume_to_lots(minimum, symbol):g} lots.")
    if maximum and volume > maximum:
        raise CTraderError(f"{lots:g} lots is above this symbol's maximum of {volume_to_lots(maximum, symbol):g} lots.")
    return volume


def volume_to_lots(volume: int | float, symbol: dict) -> float:
    lot_size = int(symbol.get("lotSize") or 0)
    return round(float(volume) / lot_size, 8) if lot_size else 0.0


def relative_distance(distance: float) -> int:
    """A price distance as relativeStopLoss/TakeProfit (1/100000 of a price)."""
    return int(round(abs(distance) * PRICE_SCALE))


def money(value: Any, digits: Any) -> float:
    try:
        return float(value or 0) / (10 ** int(digits or 2))
    except (TypeError, ValueError):
        return 0.0


def position_out(position: dict) -> dict:
    """A ProtoOAPosition in the platform's own shape."""
    trade = position.get("tradeData") or {}
    digits = position.get("moneyDigits")
    opened = trade.get("openTimestamp")
    return {
        "position_id": int(position.get("positionId")),
        "symbol_id": int(trade.get("symbolId") or 0),
        "direction": "long" if int(trade.get("tradeSide") or SIDE_BUY) == SIDE_BUY else "short",
        "volume": int(trade.get("volume") or 0),
        "entry_price": float(position.get("price") or 0),
        "stop_loss": position.get("stopLoss"),
        "take_profit": position.get("takeProfit"),
        "trailing_stop": bool(position.get("trailingStopLoss")),
        "swap": money(position.get("swap"), digits),
        "commission": money(position.get("commission"), digits),
        "label": trade.get("label"),
        "opened_at": datetime.utcfromtimestamp(int(opened) / 1000) if opened else None,
    }


def close_out(deal: dict) -> dict:
    """The P/L of a closing deal. Net = gross + swap + commission, with
    charges negative (unconfirmed - see the module docstring)."""
    detail = deal.get("closePositionDetail") or {}
    digits = detail.get("moneyDigits", deal.get("moneyDigits"))
    gross = money(detail.get("grossProfit"), digits)
    swap = money(detail.get("swap"), digits)
    commission = money(detail.get("commission"), digits)
    return {
        "position_id": int(deal.get("positionId") or 0),
        "exit_price": float(deal.get("executionPrice") or 0) or None,
        "entry_price": float(detail.get("entryPrice") or 0) or None,
        "gross_profit": gross,
        "swap": swap,
        "commission": commission,
        "net_profit": round(gross + swap + commission, 8),
        "balance": money(detail.get("balance"), digits) if detail.get("balance") is not None else None,
    }


# --- Connection -----------------------------------------------------------

class _ConnectionLost(Exception):
    pass


class _Pending:
    """One in-flight request. Matches its response by clientMsgId and, for
    execution events, by the order's clientOrderId or the position id."""

    def __init__(self, msg_id: str, done: Callable[[dict], bool], position_id: int | None = None) -> None:
        self.msg_id = msg_id
        self.done = done
        self.position_id = position_id
        self.future: asyncio.Future = asyncio.get_running_loop().create_future()

    def matches(self, message: dict) -> bool:
        if message.get("clientMsgId") == self.msg_id:
            return True
        if message.get("payloadType") not in (EXECUTION_EVENT, ORDER_ERROR_EVENT):
            return False
        payload = message.get("payload") or {}
        if (payload.get("order") or {}).get("clientOrderId") == self.msg_id:
            return True
        if self.position_id is None:
            return False
        ids = (payload.get("positionId"), (payload.get("position") or {}).get("positionId"), (payload.get("deal") or {}).get("positionId"))
        return any(i is not None and int(i) == self.position_id for i in ids)


def _error_text(message: dict) -> str | None:
    payload = message.get("payload") or {}
    kind = message.get("payloadType")
    if kind in (ERROR_RES, OA_ERROR_RES, ORDER_ERROR_EVENT):
        code = payload.get("errorCode") or "ERROR"
        return f"{code}: {payload.get('description') or ''}".rstrip(": ")
    if kind == EXECUTION_EVENT and int(payload.get("executionType") or 0) in (EXEC_ORDER_REJECTED, EXEC_ORDER_CANCELLED, EXEC_ORDER_EXPIRED):
        code = payload.get("errorCode") or "ORDER_REJECTED"
        return f"{code}: order was not filled"
    return None


class _Connection:
    """One socket to a cTrader host: the reader task, heartbeat, in-flight
    requests, and which trading accounts are already authorised on it."""

    def __init__(self, socket) -> None:
        self.socket = socket
        self.accounts: set[int] = set()
        self._pending: list[_Pending] = []
        self._ids = itertools.count(1)
        self._reader = asyncio.create_task(self._read())
        self._heartbeat = asyncio.create_task(self._beat())
        self.token_invalid = False

    @property
    def alive(self) -> bool:
        return not self._reader.done()

    def new_id(self) -> str:
        return f"wp{next(self._ids)}-{uuid.uuid4().hex[:12]}"

    async def request(
        self,
        payload_type: int,
        payload: dict,
        expect: int | Callable[[dict], bool],
        *,
        msg_id: str | None = None,
        position_id: int | None = None,
    ) -> dict:
        """Send a message and wait for the reply `expect` names (a
        payloadType, or a test on each matching message). An error reply
        raises CTraderError."""
        if not self.alive:
            raise _ConnectionLost("cTrader connection closed")
        done = (lambda m: m.get("payloadType") == expect) if isinstance(expect, int) else expect
        pending = _Pending(msg_id or self.new_id(), done, position_id)
        self._pending.append(pending)
        try:
            await self.socket.send(json.dumps({"clientMsgId": pending.msg_id, "payloadType": payload_type, "payload": payload}))
            message = await asyncio.wait_for(pending.future, timeout=REQUEST_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise CTraderError("cTrader did not answer in time.") from exc
        except (websockets.ConnectionClosed, OSError) as exc:
            raise _ConnectionLost(str(exc)) from exc
        finally:
            if pending in self._pending:
                self._pending.remove(pending)
        error = _error_text(message)
        if error:
            raise CTraderError(f"cTrader refused it - {error}")
        return message.get("payload") or {}

    def _dispatch(self, message: dict) -> None:
        kind = message.get("payloadType")
        if kind == TOKEN_INVALIDATED_EVENT:
            self.token_invalid = True
            self.accounts.clear()
        elif kind == ACCOUNT_DISCONNECT_EVENT:
            account = (message.get("payload") or {}).get("ctidTraderAccountId")
            if account is not None:
                self.accounts.discard(int(account))
        for pending in list(self._pending):
            if pending.future.done() or not pending.matches(message):
                continue
            if _error_text(message) or pending.done(message):
                pending.future.set_result(message)
            return

    async def _read(self) -> None:
        error: Exception = _ConnectionLost("cTrader connection closed")
        try:
            async for raw in self.socket:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if message.get("payloadType") == CLIENT_DISCONNECT_EVENT:
                    reason = (message.get("payload") or {}).get("reason") or "server closed the connection"
                    error = _ConnectionLost(reason)
                    break
                self._dispatch(message)
        except Exception as exc:
            error = _ConnectionLost(str(exc))
        finally:
            for pending in self._pending:
                if not pending.future.done():
                    pending.future.set_exception(error)
            self._heartbeat.cancel()

    async def _beat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                await self.socket.send(json.dumps({"payloadType": HEARTBEAT, "payload": {}}))
            except Exception:
                return

    async def close(self) -> None:
        self._heartbeat.cancel()
        try:
            await self.socket.close()
        except Exception:
            pass


Connector = Callable[[str], Awaitable[Any]]


async def _open_socket(host: str):
    return await websockets.connect(host, open_timeout=15, close_timeout=5, max_size=2**24)


class _Session:
    """The shared, app-authorised connection to one host (demo or live)."""

    _sessions: dict[str, "_Session"] = {}

    def __init__(self, host: str) -> None:
        self.host = host
        self.connection: _Connection | None = None
        self._lock = asyncio.Lock()
        self._failure: Exception | None = None
        self._failed_at = 0.0

    @classmethod
    def for_host(cls, host: str) -> "_Session":
        if host not in cls._sessions:
            cls._sessions[host] = cls(host)
        return cls._sessions[host]

    async def get(self, connect: Connector) -> _Connection:
        async with self._lock:
            if self.connection is None or not self.connection.alive or self.connection.token_invalid:
                if self._failure and time.monotonic() - self._failed_at < RECONNECT_COOLDOWN:
                    raise self._failure
                if self.connection is not None:
                    await self.connection.close()
                try:
                    socket = await connect(self.host)
                    connection = _Connection(socket)
                    await connection.request(
                        APP_AUTH_REQ,
                        {"clientId": settings.ctrader_client_id.strip(), "clientSecret": settings.ctrader_client_secret.strip()},
                        APP_AUTH_RES,
                    )
                except CTraderError as exc:
                    self._failure, self._failed_at = CTraderError(f"cTrader refused the app login - {exc}"), time.monotonic()
                    raise self._failure from exc
                except Exception as exc:
                    self._failure, self._failed_at = CTraderError(f"Could not connect to cTrader: {exc}"), time.monotonic()
                    raise self._failure from exc
                self._failure = None
                self.connection = connection
            return self.connection

    @classmethod
    def reset(cls) -> None:
        cls._sessions.clear()


# --- Client ---------------------------------------------------------------

class CTraderClient:
    """Trading on one cTrader account. `account_id` defaults to the account
    picked for the current demo/live mode; `live` says which host it is on."""

    # Per account: light symbol list, and full details by symbol id.
    _symbol_lists: dict[int, tuple[float, list[dict]]] = {}
    _symbol_details: dict[tuple[int, int], dict] = {}
    SYMBOL_LIST_TTL = 3600.0

    def __init__(
        self,
        account_id: int | None = None,
        live: bool | None = None,
        *,
        connect: Connector = _open_socket,
        token: Callable[[bool], Awaitable[str]] | None = None,
    ) -> None:
        if live is None:
            live = settings.execution_mode.lower() == "live"
        self.live = live
        self._account_id = account_id
        self._connect = connect
        self._token = token or (lambda force=False: stored_access_token(force))

    @property
    def account_id(self) -> int:
        if self._account_id is None:
            self._account_id = account_id_for_mode("live" if self.live else "demo")
        return self._account_id

    @property
    def host(self) -> str:
        return LIVE_HOST if self.live else DEMO_HOST

    async def _connection(self) -> _Connection:
        return await _Session.for_host(self.host).get(self._connect)

    async def _authorised(self) -> _Connection:
        connection = await self._connection()
        account = self.account_id
        if account in connection.accounts:
            return connection
        for attempt in range(2):
            token = await self._token(attempt > 0)
            try:
                await connection.request(ACCOUNT_AUTH_REQ, {"ctidTraderAccountId": account, "accessToken": token}, ACCOUNT_AUTH_RES)
                break
            except CTraderError as exc:
                text = str(exc)
                if "ALREADY_LOGGED_IN" in text:
                    break
                if attempt == 0 and ("TOKEN" in text.upper()):
                    continue
                raise
        connection.accounts.add(account)
        return connection

    async def _call(self, payload_type: int, payload: dict, expect, *, retry: bool = True, **kwargs) -> dict:
        """A request on the authorised account. Read-only requests are
        retried once on a fresh connection; orders never are, so a dropped
        connection can't place the same order twice."""
        attempts = 2 if retry else 1
        for attempt in range(attempts):
            connection = await self._authorised()
            try:
                return await connection.request(payload_type, {"ctidTraderAccountId": self.account_id, **payload}, expect, **kwargs)
            except _ConnectionLost as exc:
                if attempt + 1 == attempts:
                    raise CTraderError(f"Lost the cTrader connection: {exc}") from exc
        raise CTraderError("cTrader request failed.")

    # Accounts --------------------------------------------------------------

    async def list_accounts(self) -> list[dict]:
        """Every trading account linked to the stored login, on any broker."""
        connection = await self._connection()
        token = await self._token(False)
        try:
            payload = await connection.request(ACCOUNT_LIST_REQ, {"accessToken": token}, ACCOUNT_LIST_RES)
        except _ConnectionLost as exc:
            raise CTraderError(f"Lost the cTrader connection: {exc}") from exc
        return [
            {
                "account_id": int(a["ctidTraderAccountId"]),
                "login": a.get("traderLogin"),
                "live": bool(a.get("isLive")),
                "broker": a.get("brokerTitleShort") or "",
            }
            for a in payload.get("ctidTraderAccount") or []
        ]

    async def account_info(self) -> dict:
        trader = (await self._call(TRADER_REQ, {}, TRADER_RES)).get("trader") or {}
        digits = trader.get("moneyDigits")
        return {
            "account_id": self.account_id,
            "login": trader.get("traderLogin"),
            "broker": trader.get("brokerName") or "",
            "balance": money(trader.get("balance"), digits),
            # ProtoOAAccountType: HEDGED 0, NETTED 1
            "hedging": int(trader.get("accountType") or 0) == 0,
            "leverage": (int(trader["leverageInCents"]) / 100) if trader.get("leverageInCents") else None,
        }

    # Symbols ----------------------------------------------------------------

    async def symbols(self) -> list[dict]:
        cached = CTraderClient._symbol_lists.get(self.account_id)
        if cached and time.monotonic() - cached[0] < self.SYMBOL_LIST_TTL:
            return cached[1]
        payload = await self._call(SYMBOLS_LIST_REQ, {"includeArchivedSymbols": False}, SYMBOLS_LIST_RES)
        symbols = [s for s in payload.get("symbol") or [] if s.get("enabled", True)]
        CTraderClient._symbol_lists[self.account_id] = (time.monotonic(), symbols)
        return symbols

    async def symbol_details(self, symbol_id: int) -> dict:
        key = (self.account_id, int(symbol_id))
        if key not in CTraderClient._symbol_details:
            payload = await self._call(SYMBOL_BY_ID_REQ, {"symbolId": [int(symbol_id)]}, SYMBOL_BY_ID_RES)
            found = payload.get("symbol") or []
            if not found:
                raise CTraderError(f"cTrader has no details for symbol {symbol_id}.")
            CTraderClient._symbol_details[key] = found[0]
        return CTraderClient._symbol_details[key]

    async def resolve_symbol(self, raw: str) -> dict:
        """The account's symbol for an alert's symbol, with its full details
        and name: {"symbolId", "symbolName", "lotSize", ...}."""
        light = match_symbol(raw, await self.symbols())
        if not light:
            raise CTraderError(f"No symbol on this cTrader account matches '{raw}'.")
        details = await self.symbol_details(int(light["symbolId"]))
        return {**details, "symbolName": light.get("symbolName"), "description": light.get("description")}

    # Trading ----------------------------------------------------------------

    async def open_market(
        self,
        symbol: dict,
        direction: str,
        lots: float,
        *,
        stop_loss_distance: float | None = None,
        take_profit_distance: float | None = None,
        trailing_stop: bool = False,
        label: str | None = None,
        comment: str | None = None,
    ) -> dict:
        """Open a market position. SL/TP are price distances from the fill.
        Returns the filled position (see position_out)."""
        payload: dict[str, Any] = {
            "symbolId": int(symbol["symbolId"]),
            "orderType": ORDER_TYPE_MARKET,
            "tradeSide": SIDE_BUY if direction == "long" else SIDE_SELL,
            "volume": lots_to_volume(lots, symbol),
        }
        if stop_loss_distance:
            payload["relativeStopLoss"] = relative_distance(stop_loss_distance)
            if trailing_stop:
                payload["trailingStopLoss"] = True
        if take_profit_distance:
            payload["relativeTakeProfit"] = relative_distance(take_profit_distance)
        if label:
            payload["label"] = label[:100]
        if comment:
            payload["comment"] = comment[:512]
        connection = await self._authorised()
        msg_id = connection.new_id()[:50]
        payload["clientOrderId"] = msg_id

        def filled(message: dict) -> bool:
            body = message.get("payload") or {}
            return message.get("payloadType") == EXECUTION_EVENT and int(body.get("executionType") or 0) == EXEC_ORDER_FILLED

        result = await self._call(NEW_ORDER_REQ, payload, filled, retry=False, msg_id=msg_id)
        position = result.get("position")
        if not position:
            raise CTraderError("cTrader filled the order but did not report the position.")
        out = position_out(position)
        deal = result.get("deal") or {}
        if deal.get("executionPrice"):
            out["entry_price"] = float(deal["executionPrice"])
        return out

    async def close_position(self, position_id: int, volume: int) -> dict:
        """Close (all of) a position. Returns its P/L (see close_out)."""

        def closed(message: dict) -> bool:
            body = message.get("payload") or {}
            return (
                message.get("payloadType") == EXECUTION_EVENT
                and int(body.get("executionType") or 0) == EXEC_ORDER_FILLED
                and bool((body.get("deal") or {}).get("closePositionDetail"))
            )

        result = await self._call(
            CLOSE_POSITION_REQ,
            {"positionId": int(position_id), "volume": int(volume)},
            closed,
            retry=False,
            position_id=int(position_id),
        )
        return close_out(result.get("deal") or {})

    async def amend_stops(
        self,
        position_id: int,
        *,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        trailing_stop: bool | None = None,
    ) -> None:
        """Set a position's absolute SL/TP prices. A side left None is removed,
        as cTrader replaces both on every amend."""
        payload: dict[str, Any] = {"positionId": int(position_id)}
        if stop_loss is not None:
            payload["stopLoss"] = float(stop_loss)
        if take_profit is not None:
            payload["takeProfit"] = float(take_profit)
        if trailing_stop is not None:
            payload["trailingStopLoss"] = bool(trailing_stop)

        def amended(message: dict) -> bool:
            return message.get("payloadType") == EXECUTION_EVENT

        await self._call(AMEND_SLTP_REQ, payload, amended, retry=False, position_id=int(position_id))

    async def positions(self) -> list[dict]:
        payload = await self._call(RECONCILE_REQ, {}, RECONCILE_RES)
        return [position_out(p) for p in payload.get("position") or []]

    async def closing_deals(self, since: datetime, until: datetime | None = None) -> list[dict]:
        """Closing deals (with P/L) between two UTC times."""
        def ms(value: datetime) -> int:
            return int((value - datetime(1970, 1, 1)).total_seconds() * 1000)

        payload = await self._call(
            DEAL_LIST_REQ,
            {"fromTimestamp": ms(since), "toTimestamp": ms(until or datetime.utcnow())},
            DEAL_LIST_RES,
        )
        return [close_out(d) for d in payload.get("deal") or [] if d.get("closePositionDetail")]
