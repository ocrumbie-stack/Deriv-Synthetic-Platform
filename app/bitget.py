import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import settings
from app.schemas import WebhookSignal


class BitgetExecutionError(RuntimeError):
    pass


class BitgetClient:
    def __init__(self) -> None:
        self.base_url = settings.bitget_base_url.rstrip("/")

    def _timestamp(self) -> str:
        return str(int(datetime.now(timezone.utc).timestamp() * 1000))

    def _signature(self, timestamp: str, method: str, path: str, body: str) -> str:
        payload = f"{timestamp}{method.upper()}{path}{body}"
        digest = hmac.new(
            settings.bitget_api_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    async def _get(self, path: str, query: str = "") -> dict[str, Any] | None:
        full_path = f"{path}?{query}" if query else path
        timestamp = self._timestamp()
        headers = {
            "ACCESS-KEY": settings.bitget_api_key,
            "ACCESS-SIGN": self._signature(timestamp, "GET", full_path, ""),
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": settings.bitget_api_passphrase,
            "Content-Type": "application/json",
            "locale": "en-US",
        }
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{self.base_url}{full_path}", headers=headers)
        if response.status_code >= 400:
            return None
        data = response.json()
        if data.get("code") != "00000":
            return None
        return data

    async def get_account_balance(self) -> dict[str, Any] | None:
        if settings.execution_mode.lower() != "live":
            return None
        data = await self._get("/api/v2/mix/account/accounts", "productType=usdt-futures")
        if not data:
            return None
        accounts = data.get("data", [])
        return next((a for a in accounts if a.get("marginCoin") == "USDT"), None)

    async def get_positions(self) -> list[dict[str, Any]]:
        if settings.execution_mode.lower() != "live":
            return []
        data = await self._get("/api/v2/mix/position/all-position", "productType=usdt-futures&marginCoin=USDT")
        if not data:
            return []
        return data.get("data", [])

    async def set_leverage(self, symbol: str, leverage: int, hedge_mode: bool = False) -> None:
        holds = ["long", "short"] if hedge_mode else [None]
        for hold_side in holds:
            body_data: dict[str, Any] = {
                "symbol": symbol,
                "productType": "USDT-FUTURES",
                "marginCoin": "USDT",
                "leverage": str(leverage),
            }
            if hold_side:
                body_data["holdSide"] = hold_side
            body = json.dumps(body_data, separators=(",", ":"))
            timestamp = self._timestamp()
            path = "/api/v2/mix/account/set-leverage"
            headers = {
                "ACCESS-KEY": settings.bitget_api_key,
                "ACCESS-SIGN": self._signature(timestamp, "POST", path, body),
                "ACCESS-TIMESTAMP": timestamp,
                "ACCESS-PASSPHRASE": settings.bitget_api_passphrase,
                "Content-Type": "application/json",
                "locale": "en-US",
            }
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(f"{self.base_url}{path}", headers=headers, content=body)

    async def set_position_mode(self, hedge_mode: bool) -> None:
        mode = "hedge_mode" if hedge_mode else "one_way_mode"
        body_data = {"productType": "USDT-FUTURES", "posMode": mode}
        body = json.dumps(body_data, separators=(",", ":"))
        timestamp = self._timestamp()
        path = "/api/v2/mix/account/set-position-mode"
        headers = {
            "ACCESS-KEY": settings.bitget_api_key,
            "ACCESS-SIGN": self._signature(timestamp, "POST", path, body),
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": settings.bitget_api_passphrase,
            "Content-Type": "application/json",
            "locale": "en-US",
        }
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"{self.base_url}{path}", headers=headers, content=body)

    async def place_order(self, signal: WebhookSignal, hedge_mode: bool = False) -> dict[str, Any]:
        if settings.execution_mode.lower() != "live":
            return {
                "mode": "paper",
                "order_id": f"paper-{self._timestamp()}",
                "message": "Paper execution recorded. No exchange order was sent.",
            }

        missing = [
            name
            for name, value in {
                "BITGET_API_KEY": settings.bitget_api_key,
                "BITGET_API_SECRET": settings.bitget_api_secret,
                "BITGET_API_PASSPHRASE": settings.bitget_api_passphrase,
            }.items()
            if not value
        ]
        if missing:
            raise BitgetExecutionError(f"Live execution is missing credentials: {', '.join(missing)}")

        if signal.direction is None:
            raise BitgetExecutionError("Entry orders require a direction.")

        direction = signal.direction.value  # "long" or "short"
        path = "/api/v2/mix/order/place-order"
        body_data = {
            "symbol": signal.symbol,
            "productType": "USDT-FUTURES",
            "marginMode": "isolated",
            "marginCoin": "USDT",
            "size": str(signal.size),
            "price": str(signal.price) if signal.price else "",
            "side": "buy" if direction == "long" else "sell",
            "orderType": "limit" if signal.price else "market",
            "force": "gtc",
        }
        if signal.signal_id:
            body_data["clientOid"] = signal.signal_id
        if hedge_mode:
            body_data["tradeSide"] = "open"
            body_data["holdSide"] = direction
        body = json.dumps(body_data, separators=(",", ":"))
        timestamp = self._timestamp()
        headers = {
            "ACCESS-KEY": settings.bitget_api_key,
            "ACCESS-SIGN": self._signature(timestamp, "POST", path, body),
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": settings.bitget_api_passphrase,
            "Content-Type": "application/json",
            "locale": "en-US",
        }

        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(f"{self.base_url}{path}", headers=headers, content=body)

        if response.status_code >= 400:
            raise BitgetExecutionError(f"Bitget rejected the order: {response.text}")

        data = response.json()
        if data.get("code") not in (None, "00000"):
            raise BitgetExecutionError(f"Bitget returned an error: {data}")
        return data

    async def get_contracts(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"{self.base_url}/api/v2/mix/market/contracts",
                    params={"productType": "usdt-futures"},
                    headers={"Content-Type": "application/json", "locale": "en-US"},
                )
            if response.status_code >= 400:
                return []
            data = response.json()
            if data.get("code") != "00000":
                return []
            return sorted(c["symbol"] for c in data.get("data", []) if c.get("symbol"))
        except Exception:
            return []

    async def close_order(self, symbol: str, direction: str, size: float, hedge_mode: bool = False) -> dict[str, Any]:
        if settings.execution_mode.lower() != "live":
            return {"mode": "paper", "order_id": f"paper-close-{self._timestamp()}"}

        # Use flash-close-position to close the full position without specifying size,
        # which avoids minimum order quantity errors on low-liquidity or fractional pairs.
        path = "/api/v2/mix/order/flash-close-position"
        body_data: dict[str, Any] = {
            "symbol": symbol,
            "productType": "USDT-FUTURES",
            "marginCoin": "USDT",
        }
        if hedge_mode:
            body_data["holdSide"] = direction
        body = json.dumps(body_data, separators=(",", ":"))
        timestamp = self._timestamp()
        headers = {
            "ACCESS-KEY": settings.bitget_api_key,
            "ACCESS-SIGN": self._signature(timestamp, "POST", path, body),
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": settings.bitget_api_passphrase,
            "Content-Type": "application/json",
            "locale": "en-US",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(f"{self.base_url}{path}", headers=headers, content=body)
        if response.status_code >= 400:
            raise BitgetExecutionError(f"Bitget rejected the close order: {response.text}")
        data = response.json()
        if data.get("code") not in (None, "00000"):
            raise BitgetExecutionError(f"Bitget returned an error on close: {data}")
        return data
