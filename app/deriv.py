import json
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import websockets
import httpx

from app.config import settings
from app.schemas import WebhookSignal


class DerivExecutionError(RuntimeError):
    pass


def _normalize_symbol_key(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")


class DerivClient:
    _symbol_catalog: list[dict[str, Any]] | None = None
    _symbol_catalog_error: str | None = None
    _multiplier_ranges: dict[str, list[int]] = {}

    def _timestamp(self) -> str:
        return str(int(datetime.now(timezone.utc).timestamp() * 1000))

    async def _authenticated_uri(self) -> str:
        app_id = settings.deriv_app_id.strip()
        api_token = settings.deriv_api_token.strip()
        account_id = settings.deriv_account_id.strip()
        missing = [
            name
            for name, value in {
                "DERIV_APP_ID": app_id,
                "DERIV_API_TOKEN": api_token,
                "DERIV_ACCOUNT_ID": account_id,
            }.items()
            if not value
        ]
        if missing:
            raise DerivExecutionError(
                "Live execution is missing Deriv credentials: " + ", ".join(missing)
            )

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
        return uri

    @staticmethod
    def _build_request(method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        request = dict(params or {})
        request[method] = request.pop(method, 1)
        return request

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if settings.execution_mode.lower() != "live":
            return {}
        try:
            uri = await self._authenticated_uri()
            async with websockets.connect(uri, open_timeout=15, close_timeout=5) as socket:
                await socket.send(json.dumps(self._build_request(method, params)))
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

    async def _rpc_chain(
        self,
        first_method: str,
        first_params: dict[str, Any],
        second_builder: Callable[[dict[str, Any]], tuple[str, dict[str, Any]]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Run two RPCs on the same authenticated connection.

        Deriv scopes things like proposal IDs to the connection that created
        them, so proposal -> buy must share one WebSocket session instead of
        each going through separate _rpc() calls (which would each open a
        fresh connection and make the second request fail).
        """
        if settings.execution_mode.lower() != "live":
            return {}, {}
        second_method = first_method
        try:
            uri = await self._authenticated_uri()
            async with websockets.connect(uri, open_timeout=15, close_timeout=5) as socket:
                await socket.send(json.dumps(self._build_request(first_method, first_params)))
                first_data = json.loads(await socket.recv())
                if first_data.get("error"):
                    raise DerivExecutionError(f"Deriv {first_method} error: {first_data['error']}")

                second_method, second_params = second_builder(first_data)
                await socket.send(json.dumps(self._build_request(second_method, second_params)))
                second_data = json.loads(await socket.recv())
        except DerivExecutionError:
            raise
        except Exception as exc:
            if "HTTP 401" in str(exc):
                raise DerivExecutionError(
                    "Deriv rejected DERIV_APP_ID (HTTP 401). Use a valid Deriv app ID, such as 1089."
                ) from exc
            raise DerivExecutionError(f"Deriv {first_method}/{second_method} request failed: {exc}") from exc
        if second_data.get("error"):
            raise DerivExecutionError(f"Deriv {second_method} error: {second_data['error']}")
        return first_data, second_data

    async def get_account_balance(self) -> dict[str, Any] | None:
        if settings.execution_mode.lower() != "live":
            return None
        app_id = settings.deriv_app_id.strip()
        api_token = settings.deriv_api_token.strip()
        account_id = settings.deriv_account_id.strip()
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    "https://api.derivws.com/trading/v1/options/accounts",
                    headers={
                        "Authorization": f"Bearer {api_token}",
                        "Deriv-App-ID": app_id,
                    },
                )
            if response.is_error:
                raise DerivExecutionError(
                    f"Deriv accounts request failed ({response.status_code}): {response.text}"
                )
            payload = response.json()
            accounts = payload.get("data") if isinstance(payload, dict) else None
            balance = next(
                (item for item in accounts or [] if item.get("account_id") == account_id),
                None,
            )
        except DerivExecutionError:
            raise
        except Exception as exc:
            raise DerivExecutionError(f"Deriv accounts request failed: {exc}") from exc

        if not isinstance(balance, dict):
            raise DerivExecutionError(f"Deriv account {account_id} was not returned.")
        equity = float(balance.get("balance") or 0)
        available = equity
        positions = await self.get_positions()
        unrealized = sum(float(p.get("unrealizedPL") or 0) for p in positions)
        return {
            "equity": equity,
            "available": available,
            "unrealized_pnl": unrealized,
            "currency": str(balance.get("currency", "")),
        }

    async def get_positions(self) -> list[dict[str, Any]]:
        if settings.execution_mode.lower() != "live":
            return []
        try:
            uri = await self._authenticated_uri()
            async with websockets.connect(uri, open_timeout=15, close_timeout=5) as socket:
                await socket.send(json.dumps(self._build_request("portfolio", None)))
                portfolio_data = json.loads(await socket.recv())
                if portfolio_data.get("error"):
                    raise DerivExecutionError(f"Deriv portfolio error: {portfolio_data['error']}")

                portfolio = portfolio_data.get("portfolio", portfolio_data)
                entries = portfolio.get("contracts") if isinstance(portfolio, dict) else None
                if not isinstance(entries, list):
                    return []

                positions: list[dict[str, Any]] = []
                for item in entries:
                    if not isinstance(item, dict):
                        continue
                    symbol = str(item.get("symbol") or item.get("display_name") or "")
                    contract_id = item.get("contract_id") or item.get("contractId")
                    if not symbol or contract_id is None:
                        continue

                    # portfolio only returns static contract metadata (buy price,
                    # payout, etc). Live/per-tick profit requires a dedicated
                    # proposal_open_contract lookup for this specific contract.
                    profit: float = 0.0
                    direction = ""
                    amount = item.get("buy_price") or 0
                    try:
                        await socket.send(
                            json.dumps(
                                self._build_request(
                                    "proposal_open_contract", {"contract_id": contract_id}
                                )
                            )
                        )
                        poc_data = json.loads(await socket.recv())
                        poc = poc_data.get("proposal_open_contract") if isinstance(poc_data, dict) else None
                        if isinstance(poc, dict):
                            profit = float(poc.get("profit") or 0)
                            amount = poc.get("buy_price") or amount
                            contract_type = str(poc.get("contract_type") or "")
                            direction = "long" if contract_type == "MULTUP" else "short" if contract_type == "MULTDOWN" else ""
                    except Exception:
                        pass

                    positions.append(
                        {
                            "symbol": symbol,
                            "holdSide": direction,
                            "total": amount,
                            "available": amount,
                            "unrealizedPL": profit,
                            "contract_id": contract_id,
                        }
                    )
                return positions
        except DerivExecutionError:
            return []
        except Exception:
            return []

    async def get_contract_status(self, contract_id: str) -> dict[str, Any] | None:
        """Look up a specific contract's current state on Deriv.

        Used to detect contracts Deriv has already closed (e.g. via its
        guaranteed stop-out) that our own DB doesn't know about yet, since we
        otherwise only learn of a close when a matching exit webhook arrives.
        """
        if settings.execution_mode.lower() != "live" or not contract_id:
            return None
        try:
            result = await self._rpc("proposal_open_contract", {"contract_id": contract_id})
        except DerivExecutionError:
            return None
        poc = result.get("proposal_open_contract") if isinstance(result, dict) else None
        return poc if isinstance(poc, dict) else None

    async def get_symbol_catalog(self) -> list[dict[str, Any]]:
        if settings.execution_mode.lower() != "live":
            return []
        if DerivClient._symbol_catalog is not None:
            return DerivClient._symbol_catalog
        try:
            result = await self._rpc("active_symbols", {"active_symbols": "brief"})
        except DerivExecutionError as exc:
            DerivClient._symbol_catalog_error = str(exc)
            return []

        symbols = result.get("active_symbols") if isinstance(result, dict) else None
        catalog = [item for item in symbols if isinstance(item, dict)] if isinstance(symbols, list) else []
        if catalog:
            DerivClient._symbol_catalog = catalog
            DerivClient._symbol_catalog_error = None
        else:
            DerivClient._symbol_catalog_error = "Deriv returned zero active symbols."
        return catalog

    async def get_contracts(self) -> list[str]:
        catalog = await self.get_symbol_catalog()
        return sorted(str(item.get("underlying_symbol", "")) for item in catalog if item.get("underlying_symbol"))

    async def resolve_symbol(self, raw_symbol: str) -> str:
        candidate = raw_symbol.strip().upper()
        catalog = await self.get_symbol_catalog()
        if not catalog:
            raise DerivExecutionError(
                "Could not load the Deriv symbol catalog to validate the requested symbol."
            )

        for item in catalog:
            code = str(item.get("underlying_symbol") or "")
            if code.upper() == candidate:
                return code

        candidate_key = _normalize_symbol_key(candidate)
        for item in catalog:
            code = str(item.get("underlying_symbol") or "")
            display_name = str(item.get("underlying_symbol_name") or "")
            if code and display_name and _normalize_symbol_key(display_name) == candidate_key:
                return code

        raise DerivExecutionError(
            f"Unknown Deriv symbol '{raw_symbol}'. It did not match any Deriv symbol code or display name."
        )

    async def get_multiplier_range(self, symbol: str, contract_type: str) -> list[int]:
        """Deriv only accepts a fixed, per-symbol set of multiplier values for
        Multiplier contracts (e.g. Step indices only accept 100/300/500/700/1000,
        while 1s Volatility indices accept a completely different set). Look it
        up via contracts_for instead of guessing.
        """
        cache_key = f"{symbol}:{contract_type}"
        if cache_key in DerivClient._multiplier_ranges:
            return DerivClient._multiplier_ranges[cache_key]

        try:
            result = await self._rpc("contracts_for", {"contracts_for": symbol})
        except DerivExecutionError:
            return []
        available = result.get("contracts_for", {}).get("available") if isinstance(result, dict) else None
        if not isinstance(available, list):
            return []

        # One contracts_for call returns both MULTUP and MULTDOWN entries, so
        # cache whichever of the two we find rather than re-fetching per
        # direction - halves the round trips when both get requested.
        found: list[int] = []
        for item in available:
            if not isinstance(item, dict) or "MULT" not in str(item.get("contract_type", "")):
                continue
            values = item.get("multiplier_range")
            if not (isinstance(values, list) and values):
                continue
            parsed = sorted(int(v) for v in values)
            DerivClient._multiplier_ranges[f"{symbol}:{item['contract_type']}"] = parsed
            if item.get("contract_type") == contract_type:
                found = parsed
        return found

    async def place_order(self, signal: WebhookSignal, hedge_mode: bool = False) -> dict[str, Any]:
        if settings.execution_mode.lower() != "live":
            return {
                "mode": "paper",
                "order_id": f"paper-{self._timestamp()}",
                "message": "Paper execution recorded. No Deriv order was sent.",
            }

        if signal.direction is None:
            raise DerivExecutionError("Entry orders require a direction.")

        symbol = await self.resolve_symbol(signal.symbol)
        # Multipliers (not fixed-duration digital options) so the position
        # stays open with live-moving P&L until a matching exit signal sells it.
        contract_type = "MULTUP" if signal.direction.value == "long" else "MULTDOWN"

        requested_multiplier = max(1, round(signal.leverage))
        allowed_multipliers = await self.get_multiplier_range(symbol, contract_type)
        if allowed_multipliers and min(allowed_multipliers) > settings.max_multiplier_floor:
            raise DerivExecutionError(
                f"{symbol} requires at least {min(allowed_multipliers)}x leverage, "
                f"above the configured safety cap of {settings.max_multiplier_floor}x."
            )
        multiplier = (
            min(allowed_multipliers, key=lambda value: abs(value - requested_multiplier))
            if allowed_multipliers
            else requested_multiplier
        )

        def _build_buy(proposal_response: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            proposal_data = proposal_response.get("proposal", proposal_response)
            proposal_id = proposal_data.get("id") if isinstance(proposal_data, dict) else None
            ask_price = proposal_data.get("ask_price") if isinstance(proposal_data, dict) else None
            if not proposal_id or ask_price is None:
                raise DerivExecutionError("Deriv returned an incomplete contract proposal.")
            return "buy", {"buy": proposal_id, "price": float(ask_price)}

        _, result = await self._rpc_chain(
            "proposal",
            {
                "proposal": 1,
                "underlying_symbol": symbol,
                "contract_type": contract_type,
                "currency": "USD",
                "amount": float(signal.size),
                "basis": "stake",
                "multiplier": multiplier,
            },
            _build_buy,
        )
        buy_data = result.get("buy", result)
        order_id = buy_data.get("contract_id") if isinstance(buy_data, dict) else None
        return {"order_id": str(order_id or self._timestamp()), "mode": "live", "result": result}

    async def close_order(self, contract_id: str) -> dict[str, Any]:
        if settings.execution_mode.lower() != "live":
            return {"mode": "paper", "order_id": f"paper-close-{self._timestamp()}"}
        if not contract_id:
            return {"mode": "live", "message": "no_position"}

        # portfolio (used by get_positions) has been observed to silently omit
        # genuinely open contracts, which would make a symbol/direction search
        # wrongly conclude "no position" and sell nothing while the real
        # contract keeps running. Target this exact contract_id directly
        # instead - we already know it from the trade we're closing.
        poc = await self.get_contract_status(contract_id)
        if poc and poc.get("is_sold"):
            # Deriv already closed this itself (e.g. a stop-out) - nothing to sell.
            return {
                "mode": "live",
                "message": "already_sold",
                "order_id": contract_id,
                "profit": float(poc.get("profit") or 0),
            }

        result = await self._rpc("sell", {"sell": contract_id, "price": 0})
        sell_data = result.get("sell", result)
        order_id = sell_data.get("transaction_id") if isinstance(sell_data, dict) else None

        # Confirm the real realized profit from Deriv's own post-sale record
        # rather than trusting a pre-sale snapshot.
        final = await self.get_contract_status(contract_id)
        profit = float(final["profit"]) if final and final.get("profit") is not None else None

        return {
            "mode": "live",
            "order_id": str(order_id or contract_id),
            "result": result,
            "profit": profit,
        }

    async def place_tpsl(
        self,
        symbol: str,
        direction: str,
        tp_price: float | None,
        sl_price: float | None,
        hedge_mode: bool = False,
    ) -> None:
        return None
