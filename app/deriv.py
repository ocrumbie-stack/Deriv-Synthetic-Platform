import json
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import settings
from app.schemas import WebhookSignal


class DerivExecutionError(RuntimeError):
    pass


class DerivClient:
    def __init__(self) -> None:
        self.base_url = settings.deriv_api_url.rstrip("/")

    def _timestamp(self) -> str:
        return str(int(datetime.now(timezone.utc).timestamp() * 1000))

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if settings.execution_mode.lower() != "live":
            return {}

        missing = [
            name
            for name, value in {
                "DERIV_APP_ID": settings.deriv_app_id,
                "DERIV_API_TOKEN": settings.deriv_api_token,
            }.items()
            if not value
        ]
        if missing:
            raise DerivExecutionError(
                "Live execution is missing Deriv credentials: " + ", ".join(missing)
            )

        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or {},
        }
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{self.base_url}/api/v3",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        if response.status_code >= 400:
            raise DerivExecutionError(f"Deriv rejected {method}: {response.text}")

        data = response.json()
        if data.get("error"):
            raise DerivExecutionError(f"Deriv {method} error: {data['error']}")
        return data.get("result") or {}

    async def get_account_balance(self) -> dict[str, Any] | None:
        if settings.execution_mode.lower() != "live":
            return None
        try:
            result = await self._rpc("balance")
        except DerivExecutionError:
            return None

        balance = result.get("balance") if isinstance(result, dict) else None
        if isinstance(balance, dict):
            return {
                "equity": float(balance.get("balance", 0) or 0),
                "available": float(balance.get("available", 0) or 0),
                "unrealized_pnl": 0.0,
            }
        return {"equity": 0.0, "available": 0.0, "unrealized_pnl": 0.0}

    async def get_positions(self) -> list[dict[str, Any]]:
        if settings.execution_mode.lower() != "live":
            return []
        try:
            result = await self._rpc("portfolio")
        except DerivExecutionError:
            return []

        entries = result.get("contracts") if isinstance(result, dict) else None
        if not isinstance(entries, list):
            return []

        positions: list[dict[str, Any]] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or item.get("display_name") or "")
            if not symbol:
                continue
            positions.append(
                {
                    "symbol": symbol,
                    "holdSide": item.get("direction") or "",
                    "total": item.get("amount") or 0,
                    "available": item.get("amount") or 0,
                    "unrealizedPL": item.get("profit") or 0,
                    "contract_id": item.get("contract_id") or item.get("contractId"),
                }
            )
        return positions

    async def get_contracts(self) -> list[str]:
        if settings.execution_mode.lower() != "live":
            return []
        try:
            result = await self._rpc("active_symbols", {"product_type": "synthetic_index"})
        except DerivExecutionError:
            return []

        symbols = result.get("active_symbols") if isinstance(result, dict) else None
        if not isinstance(symbols, list):
            return []
        return sorted(str(item.get("symbol", "")) for item in symbols if item.get("symbol"))

    async def place_order(self, signal: WebhookSignal, hedge_mode: bool = False) -> dict[str, Any]:
        if settings.execution_mode.lower() != "live":
            return {
                "mode": "paper",
                "order_id": f"paper-{self._timestamp()}",
                "message": "Paper execution recorded. No Deriv order was sent.",
            }

        if signal.direction is None:
            raise DerivExecutionError("Entry orders require a direction.")

        symbol = signal.symbol.upper()
        contract_type = "CALL" if signal.direction.value == "long" else "PUT"
        params = {
            "symbol": symbol,
            "contract_type": contract_type,
            "currency": "USD",
            "amount": float(signal.size),
            "basis": "stake",
        }
        if signal.price and signal.price > 0:
            params["price"] = float(signal.price)

        result = await self._rpc("buy_contract", params)
        order_id = result.get("contract_id") or result.get("contractId") or result.get("buy")
        return {"order_id": str(order_id or self._timestamp()), "mode": "live", "result": result}

    async def close_order(self, symbol: str, direction: str, size: float, hedge_mode: bool = False) -> dict[str, Any]:
        if settings.execution_mode.lower() != "live":
            return {"mode": "paper", "order_id": f"paper-close-{self._timestamp()}"}

        positions = await self.get_positions()
        target = None
        for item in positions:
            if item.get("symbol", "").upper() != symbol.upper():
                continue
            if direction and item.get("holdSide") and str(item.get("holdSide")).lower() != str(direction).lower():
                continue
            target = item
            break

        if target is None:
            return {"mode": "live", "message": "no_position"}

        contract_id = target.get("contract_id")
        if contract_id is None:
            return {"mode": "live", "message": "no_position"}

        result = await self._rpc("sell_contract", {"contract_id": contract_id, "price": 0})
        return {"mode": "live", "order_id": str(result.get("sell") or contract_id), "result": result}

    async def set_leverage(self, symbol: str, leverage: int, hedge_mode: bool = False) -> None:
        return None

    async def set_position_mode(self, hedge_mode: bool) -> None:
        return None

    async def place_tpsl(
        self,
        symbol: str,
        direction: str,
        tp_price: float | None,
        sl_price: float | None,
        hedge_mode: bool = False,
    ) -> None:
        return None
