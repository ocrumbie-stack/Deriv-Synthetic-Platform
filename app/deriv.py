import json
from datetime import datetime, timezone
from typing import Any

import websockets
import httpx

from app.config import settings
from app.schemas import WebhookSignal


class DerivExecutionError(RuntimeError):
    pass


class DerivClient:
    def _timestamp(self) -> str:
        return str(int(datetime.now(timezone.utc).timestamp() * 1000))

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if settings.execution_mode.lower() != "live":
            return {}

        app_id = settings.deriv_app_id.strip()
        api_token = settings.deriv_api_token.strip()
        account_id = settings.deriv_account_id.strip()
        missing = [
            name
            for name, value in {
                "DERIV_APP_ID": app_id,
                "DERIV_API_TOKEN": api_token,
                "DERIV_ACCOUNT_ID": account_id,
                "DERIV_APP_ID": app_id,
            }.items()
            if not value
        ]
        if missing:
            raise DerivExecutionError(
                "Live execution is missing Deriv credentials: " + ", ".join(missing)
            )

        request = dict(params or {})
        request[method] = request.pop(method, 1)
        try:
            if method in {"active_symbols", "proposal"}:
                uri = f"{settings.deriv_ws_url}?app_id={app_id or '1089'}"
            else:
                async with httpx.AsyncClient(timeout=15) as client:
                    otp_response = await client.post(
                        f"https://api.derivws.com/trading/v1/options/accounts/{account_id}/otp",
                        headers={
                            "Authorization": f"Bearer {api_token}",
                            "Deriv-App-ID": app_id,
                        },
                    )
                if otp_response.is_error:
                    raise DerivExecutionError(
                        f"Deriv OTP request failed ({otp_response.status_code}): {otp_response.text}"
                    )
                otp_data = otp_response.json()
                uri = ((otp_data.get("data") or {}).get("url")) if isinstance(otp_data, dict) else None
                if not uri:
                    raise DerivExecutionError("Deriv OTP response did not include a WebSocket URL.")

            async with websockets.connect(uri, open_timeout=15, close_timeout=5) as socket:
                await socket.send(json.dumps(request))
                data = json.loads(await socket.recv())
        except DerivExecutionError:
            raise
        except Exception as exc:
            if "HTTP 401" in str(exc):
                raise DerivExecutionError(
                    "Deriv rejected DERIV_APP_ID (HTTP 401). Use a valid Deriv app ID, such as 1089."
                ) from exc
            raise DerivExecutionError(f"Deriv {method} request failed: {exc}") from exc
        if data.get("error"):
            raise DerivExecutionError(f"Deriv {method} error: {data['error']}")
        return data

    async def get_account_balance(self) -> dict[str, Any] | None:
        if settings.execution_mode.lower() != "live":
            return None
        result = await self._rpc("balance")

        if not isinstance(result, dict):
            return None
        balance = result.get("balance", result)
        if not isinstance(balance, dict):
            return None
        equity = float(balance.get("balance", balance.get("equity", 0)) or 0)
        available = float(balance.get("available", balance.get("available_balance", equity)) or 0)
        return {
            "equity": equity,
            "available": available,
            "unrealized_pnl": float(balance.get("unrealized_pnl", 0) or 0),
            "currency": str(balance.get("currency", "")),
        }

    async def get_positions(self) -> list[dict[str, Any]]:
        if settings.execution_mode.lower() != "live":
            return []
        try:
            result = await self._rpc("portfolio")
        except DerivExecutionError:
            return []

        portfolio = result.get("portfolio", result) if isinstance(result, dict) else None
        entries = portfolio.get("contracts") if isinstance(portfolio, dict) else None
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
            result = await self._rpc("active_symbols", {"active_symbols": "brief"})
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
        proposal = await self._rpc("proposal", {
            "proposal": 1,
            "underlying_symbol": symbol,
            "contract_type": contract_type,
            "currency": "USD",
            "amount": float(signal.size),
            "basis": "stake",
            "duration": 1,
            "duration_unit": "t",
        })
        proposal_data = proposal.get("proposal", proposal)
        proposal_id = proposal_data.get("id") if isinstance(proposal_data, dict) else None
        ask_price = proposal_data.get("ask_price") if isinstance(proposal_data, dict) else None
        if not proposal_id or ask_price is None:
            raise DerivExecutionError("Deriv returned an incomplete contract proposal.")

        result = await self._rpc("buy", {"buy": proposal_id, "price": float(ask_price)})
        buy_data = result.get("buy", result)
        order_id = buy_data.get("contract_id") if isinstance(buy_data, dict) else None
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

        result = await self._rpc("sell", {"sell": contract_id, "price": 0})
        sell_data = result.get("sell", result)
        order_id = sell_data.get("transaction_id") if isinstance(sell_data, dict) else None
        return {"mode": "live", "order_id": str(order_id or contract_id), "result": result}

    async def place_tpsl(
        self,
        symbol: str,
        direction: str,
        tp_price: float | None,
        sl_price: float | None,
        hedge_mode: bool = False,
    ) -> None:
        return None
