"""BloFin USDT perpetual futures REST client.

Signed requests follow BloFin's spec: the prehash is path (with any query
string) + METHOD + timestamp (ms) + nonce + body, signed with HMAC SHA256
using the API secret; the signature is the base64 of the *hex* digest's
text. Key, signature, timestamp, nonce and passphrase go in ACCESS-*
headers. BloFin has a separate demo-trading environment with its own keys,
so the client follows the platform's demo/live switch.

Positions and the balance come back in the platform's own shape (see
_position) so the Crypto page never reads BloFin's field names.
"""

import base64
import hashlib
import hmac
import json
import time
import uuid
from decimal import ROUND_DOWN, Decimal
from urllib.parse import urlencode

import httpx

from app.config import settings

LIVE_URL = "https://openapi.blofin.com"
DEMO_URL = "https://demo-trading-openapi.blofin.com"
# Isolated, so a bot's margin is the most one position can lose.
MARGIN_MODE = "isolated"


class BlofinError(Exception):
    pass


def contract_symbol(symbol: str) -> str:
    """A TradingView ticker (BTCUSDT, BTCUSDT.P, BTCUSDT.PERP) to BloFin's
    instrument id (BTC-USDT). BTC-USDT passes, and so does the old
    BTC-SWAP-USDT form saved while the Crypto page was on Toobit."""
    symbol = symbol.strip().upper().replace("-SWAP-", "")
    for suffix in (".PERP", ".P"):
        if symbol.endswith(suffix):
            symbol = symbol[: -len(suffix)]
            break
    symbol = symbol.replace("-", "")
    if symbol.endswith("USDT") and len(symbol) > 4 and symbol.isalnum():
        return f"{symbol[:-4]}-USDT"
    raise BlofinError(f"{symbol} is not a USDT pair - only USDT perpetual futures trade on BloFin here.")


def pair_label(contract: str) -> str:
    """BTC-USDT as the BTCUSDT a TradingView alert sends."""
    return contract.replace("-", "")


def sign(prehash: str, secret: str) -> str:
    digest = hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).hexdigest()
    return base64.b64encode(digest.encode()).decode()


def credentials(mode: str | None = None) -> tuple[str, str, str]:
    """(key, secret, passphrase) for demo or live - the current mode by default."""
    if (mode or settings.execution_mode).lower() == "live":
        values = (settings.blofin_api_key, settings.blofin_api_secret, settings.blofin_api_passphrase)
    else:
        values = (settings.blofin_demo_api_key, settings.blofin_demo_api_secret, settings.blofin_demo_api_passphrase)
    return tuple(v.strip() for v in values)


def missing_settings(mode: str) -> list[str]:
    prefix = "BLOFIN_API_" if mode == "live" else "BLOFIN_DEMO_API_"
    return [prefix + name for name, value in zip(("KEY", "SECRET", "PASSPHRASE"), credentials(mode)) if not value]


def configured(mode: str | None = None) -> bool:
    return all(credentials(mode))


def contracts_for(value_usdt: float, price: float, contract_value: str, lot_size: str) -> Decimal:
    """How many contracts make a position worth value_usdt at price, rounded
    down to the lot size. BloFin sizes orders in contracts (BTC-USDT is
    0.001 BTC each), never in USDT."""
    lot = Decimal(lot_size)
    raw = Decimal(str(value_usdt)) / Decimal(str(price)) / Decimal(contract_value)
    return (raw / lot).to_integral_value(rounding=ROUND_DOWN) * lot


def _num(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _position(row: dict) -> dict:
    """A BloFin position as the Crypto page shows it. In one-way (net) mode
    the sign of `positions` is the side."""
    size = float(row.get("positions") or 0)
    side = row.get("positionSide") or "net"
    is_long = side == "long" or (side == "net" and size > 0)
    margin = _num(row.get("margin")) or _num(row.get("initialMargin"))
    leverage = _num(row.get("leverage"))
    return {
        "symbol": row.get("instId"),
        "side": "LONG" if is_long else "SHORT",
        "position_side": side,
        "margin_mode": row.get("marginMode") or MARGIN_MODE,
        "contracts": abs(size),
        "margin": margin,
        "leverage": leverage,
        # Isolated margin is the entry value over leverage, so this is the
        # position's value at entry.
        "value": margin * leverage if margin and leverage else None,
        "entry_price": _num(row.get("averagePrice")),
        "mark_price": _num(row.get("markPrice")),
        "liquidation_price": _num(row.get("liquidationPrice")),
        "unrealized_pnl": _num(row.get("unrealizedPnl")),
    }


class BlofinClient:
    def __init__(self, mode: str | None = None, timeout: float = 10.0):
        self.mode = (mode or settings.execution_mode).lower()
        self.base_url = LIVE_URL if self.mode == "live" else DEMO_URL
        self.timeout = timeout

    async def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None, signed: bool = True):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if params:
            path = f"{path}?{urlencode(params)}"
        # Sign exactly the bytes sent: compact JSON, no spaces.
        payload = json.dumps({k: v for k, v in body.items() if v is not None}, separators=(",", ":")) if body is not None else ""
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if signed:
            key, secret, passphrase = credentials(self.mode)
            if not (key and secret and passphrase):
                raise BlofinError(f"BloFin {self.mode} isn't connected - set {', '.join(missing_settings(self.mode))}.")
            timestamp, nonce = str(int(time.time() * 1000)), str(uuid.uuid4())
            headers.update({
                "ACCESS-KEY": key,
                "ACCESS-SIGN": sign(f"{path}{method}{timestamp}{nonce}{payload}", secret),
                "ACCESS-TIMESTAMP": timestamp,
                "ACCESS-NONCE": nonce,
                "ACCESS-PASSPHRASE": passphrase,
            })
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(method, f"{self.base_url}{path}", content=payload or None, headers=headers)
        try:
            data = response.json()
        except ValueError:
            data = None
        # Errors come back as HTTP 200 with a non-"0" code; rate limits are 429.
        if isinstance(data, dict) and str(data.get("code", "0")) != "0":
            raise BlofinError(f"BloFin error {data['code']}: {data.get('msg') or 'request refused'}")
        if response.status_code >= 400 or not isinstance(data, dict):
            raise BlofinError(f"BloFin HTTP {response.status_code}: {response.text[:200]}")
        return data.get("data")

    async def instrument(self, symbol: str) -> dict:
        """contractValue, lotSize, minSize, maxMarketSize, state, ..."""
        rows = await self._request("GET", "/api/v1/market/instruments", {"instId": symbol}, signed=False)
        row = next((r for r in rows or [] if r.get("instId") == symbol), None)
        if row is None:
            raise BlofinError(f"BloFin doesn't list {pair_label(symbol)}.")
        return row

    async def last_price(self, symbol: str) -> float:
        rows = await self._request("GET", "/api/v1/market/tickers", {"instId": symbol}, signed=False)
        price = _num((rows or [{}])[0].get("last"))
        if not price:
            raise BlofinError(f"No BloFin price for {pair_label(symbol)}.")
        return price

    async def tickers(self) -> dict[str, dict]:
        """Every pair's last price and 24h stats, by instrument id. Volume
        is in USDT (base volume at the last price)."""
        rows = await self._request("GET", "/api/v1/market/tickers", signed=False)
        out = {}
        for row in rows or []:
            last, base_volume = _num(row.get("last")), _num(row.get("volCurrency24h"))
            out[row.get("instId")] = {
                "last": last,
                "open_24h": _num(row.get("open24h")),
                "high_24h": _num(row.get("high24h")),
                "low_24h": _num(row.get("low24h")),
                "volume_24h": base_volume * last if base_volume is not None and last else None,
            }
        return out

    async def funding_rates(self) -> dict[str, dict]:
        """Every perpetual's current funding rate (a fraction, positive when
        longs pay shorts), the next funding time (ms) and the interval."""
        rows = await self._request("GET", "/api/v1/market/funding-rate", signed=False)
        return {
            row.get("instId"): {
                "rate": _num(row.get("fundingRate")),
                "next_time": int(row["fundingTime"]) if str(row.get("fundingTime") or "").isdigit() else None,
                "interval_hours": _num(row.get("fundingInterval")) if row.get("fundingIntervalUnit", "hour") == "hour" else None,
            }
            for row in rows or []
        }

    async def closes(self, symbol: str, bar: str = "1H", limit: int = 24) -> list[float]:
        """Candle closes, oldest first. BloFin sends newest first, as
        [ts, open, high, low, close, ...]."""
        rows = await self._request("GET", "/api/v1/market/candles", {"instId": symbol, "bar": bar, "limit": limit}, signed=False)
        closes = [_num(row[4]) for row in reversed(rows or []) if len(row) > 4]
        return [c for c in closes if c is not None]

    async def api_key(self) -> dict:
        """The connected key's name, access, IP lock and expiry (ms, or None)."""
        data = await self._request("GET", "/api/v1/user/query-apikey") or {}
        expires = str(data.get("expireTime") or "")
        return {
            "name": data.get("apiName") or None,
            "read_only": str(data.get("readOnly")) == "1",
            "ip_locked": bool(data.get("ips")),
            "expires_at": int(expires) if expires.isdigit() and int(expires) > 0 else None,
            "sub_account": str(data.get("parentUid") or "0") != "0",
        }

    async def balance(self) -> dict | None:
        """The USDT futures account: equity, available and unrealized P/L."""
        data = await self._request("GET", "/api/v1/account/balance", {"productType": "USDT-FUTURES"}) or {}
        usdt = next((d for d in data.get("details") or [] if d.get("currency") == "USDT"), None)
        if usdt is None:
            return None
        return {
            "equity": _num(usdt.get("equity")),
            "available": _num(usdt.get("available")),
            "unrealized_pnl": _num(usdt.get("isolatedUnrealizedPnl")),
        }

    async def positions(self, symbol: str | None = None) -> list[dict]:
        rows = await self._request("GET", "/api/v1/account/positions", {"instId": symbol})
        return [_position(row) for row in rows or [] if float(row.get("positions") or 0) != 0]

    async def hedge_mode(self) -> bool:
        data = await self._request("GET", "/api/v1/account/position-mode") or {}
        return data.get("positionMode") == "long_short_mode"

    async def set_leverage(self, symbol: str, leverage: int, position_side: str | None = None) -> None:
        await self._request("POST", "/api/v1/account/set-leverage", body={
            "instId": symbol, "leverage": str(leverage), "marginMode": MARGIN_MODE, "positionSide": position_side,
        })

    async def open_market(self, symbol: str, direction: str, contracts: Decimal, client_order_id: str,
                          position_side: str = "net", take_profit: float | None = None,
                          stop_loss: float | None = None) -> dict:
        """A market entry of `contracts`. TP/SL are prices held on BloFin,
        triggered on the last price and filled at market ("-1")."""
        body = {
            "instId": symbol,
            "marginMode": MARGIN_MODE,
            "positionSide": position_side,
            "side": "buy" if direction == "long" else "sell",
            "orderType": "market",
            "size": format(contracts, "f"),
            "clientOrderId": client_order_id,
        }
        if take_profit is not None:
            body.update(tpTriggerPrice=str(take_profit), tpOrderPrice="-1", tpTriggerPriceType="last")
        if stop_loss is not None:
            body.update(slTriggerPrice=str(stop_loss), slOrderPrice="-1", slTriggerPriceType="last")
        rows = await self._request("POST", "/api/v1/trade/order", body=body)
        order = (rows or [{}])[0]
        if str(order.get("code", "0")) != "0":
            raise BlofinError(f"BloFin error {order['code']}: {order.get('msg') or 'order refused'}")
        return order

    async def close_position(self, position: dict, client_order_id: str) -> dict:
        """Close all of one position (as positions() returns it) at market."""
        return await self._request("POST", "/api/v1/trade/close-position", body={
            "instId": position["symbol"],
            "marginMode": position["margin_mode"],
            "positionSide": position["position_side"],
            "clientOrderId": client_order_id,
        })
