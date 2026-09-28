import asyncio
import json
import itertools
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import websockets
import httpx

from app.config import deriv_credential_names, settings
from app.schemas import WebhookSignal


class DerivExecutionError(RuntimeError):
    pass


def _normalize_symbol_key(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")


# Names alerts send that no longer match any Deriv code or display name,
# keyed by _normalize_symbol_key. Deriv renamed the original "Step Index" to
# "Step Index 100" when it added the 200-500 variants.
SYMBOL_ALIASES = {
    "STEP_INDEX": "stpRNG",
    "STEPINDEX": "stpRNG",
}

# Deriv prefixes FX/metal and crypto codes with their market (frxEURUSD,
# cryBTCUSD), while TradingView's {{ticker}} sends the bare pair (EURUSD).
MARKET_CODE_PREFIXES = ("frx", "cry")


def _active_credentials() -> tuple[str, str, str]:
    """(app_id, api_token, account_id) for whichever Deriv account the
    current execution mode targets - the real one, or the demo/virtual one.

    Anything other than exactly "live" resolves to demo, so a stale or
    unrecognized mode value can never accidentally route to real money.
    """
    app_id = settings.deriv_app_id.strip()
    if settings.execution_mode.lower() == "live":
        return app_id, settings.deriv_api_token.strip(), settings.deriv_account_id.strip()
    return app_id, settings.deriv_demo_api_token.strip(), settings.deriv_demo_account_id.strip()


# Deriv rate-limits the OTP login that every WebSocket connection needs.
# Opening a fresh connection per request (dashboard polls check every open
# contract every 10s, per tab) exhausted that limit and made a live reversal
# fail to close its old position with a 429. Everything now shares one
# long-lived connection per account instead, tagging each request with a
# req_id so concurrent callers each get their own response.
REQUEST_TIMEOUT = 20.0
KEEPALIVE_INTERVAL = 30.0
# After a failed connect, fail fast instead of every waiting caller spending
# another OTP login on it in turn.
RECONNECT_COOLDOWN = 5.0
# Safe to resend on a fresh connection if the old one dropped mid-request.
# Never buy/sell/contract_update - a retry could execute twice.
_IDEMPOTENT_METHODS = {"proposal_open_contract", "active_symbols", "contracts_for", "portfolio", "proposal", "ping"}


class _ConnectionLost(Exception):
    pass


class _DerivConnection:
    """One authenticated socket, its reader task, and in-flight requests."""

    def __init__(self, socket) -> None:
        self.socket = socket
        self._pending: dict[int, asyncio.Future] = {}
        self._req_ids = itertools.count(1)
        self._reader = asyncio.create_task(self._read())
        self._keepalive = asyncio.create_task(self._ping())

    @property
    def alive(self) -> bool:
        return not self._reader.done()

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.alive:
            raise _ConnectionLost("Deriv connection closed")
        req_id = next(self._req_ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        try:
            await self.socket.send(json.dumps({**payload, "req_id": req_id}))
            return await asyncio.wait_for(future, timeout=REQUEST_TIMEOUT)
        except (websockets.ConnectionClosed, OSError) as exc:
            raise _ConnectionLost(str(exc)) from exc
        finally:
            self._pending.pop(req_id, None)

    async def _read(self) -> None:
        error: Exception = _ConnectionLost("Deriv connection closed")
        try:
            async for raw in self.socket:
                data = json.loads(raw)
                future = self._pending.get(data.get("req_id"))
                if future and not future.done():
                    future.set_result(data)
        except Exception as exc:
            error = _ConnectionLost(str(exc))
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            self._keepalive.cancel()

    async def _ping(self) -> None:
        # Deriv drops idle connections after a couple of minutes.
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            try:
                await self.request({"ping": 1})
            except Exception:
                return


class _DerivSession:
    """Hands out the shared connection for one account, reconnecting when it drops."""

    _sessions: dict[tuple[str, str], "_DerivSession"] = {}

    def __init__(self) -> None:
        self._connection: _DerivConnection | None = None
        self._lock = asyncio.Lock()
        self._failed_at = 0.0
        self._failure: Exception | None = None

    @classmethod
    def for_account(cls, account_id: str) -> "_DerivSession":
        key = (settings.execution_mode.lower(), account_id)
        if key not in cls._sessions:
            cls._sessions[key] = cls()
        return cls._sessions[key]

    async def connection(self, client: "DerivClient") -> _DerivConnection:
        async with self._lock:
            if self._connection is None or not self._connection.alive:
                if self._failure and time.monotonic() - self._failed_at < RECONNECT_COOLDOWN:
                    raise self._failure
                try:
                    uri = await client._authenticated_uri()
                    socket = await websockets.connect(uri, open_timeout=15, close_timeout=5)
                except Exception as exc:
                    self._failure, self._failed_at = exc, time.monotonic()
                    raise
                self._failure = None
                self._connection = _DerivConnection(socket)
            return self._connection


class DerivClient:
    _symbol_catalog: list[dict[str, Any]] | None = None
    _symbol_catalog_error: str | None = None
    _multiplier_ranges: dict[str, list[int]] = {}
    # Every RPC call opens a brand-new authenticated WebSocket connection, so
    # polling several open contracts every dashboard refresh multiplies fast
    # and can trip Deriv's rate limit. A short cache collapses repeated
    # lookups of the same contract within one refresh cycle (and across the
    # unrealized-pnl and open-positions endpoints, which both check the same
    # contracts) into a single real call.
    _contract_status_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
    _CONTRACT_STATUS_CACHE_TTL = 5.0
    # Keyed by execution mode so a demo<->live switch never serves the other
    # account's cached balance for the remainder of the TTL.
    _account_balance_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def _timestamp(self) -> str:
        return str(int(datetime.now(timezone.utc).timestamp() * 1000))

    async def _authenticated_uri(self) -> str:
        app_id, api_token, account_id = _active_credentials()
        label = "Live" if settings.execution_mode.lower() == "live" else "Demo"
        missing = [name for name, value in deriv_credential_names(settings.execution_mode).items() if not value.strip()]
        if missing:
            raise DerivExecutionError(
                f"{label} execution is missing Deriv credentials: " + ", ".join(missing)
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

    def _session(self) -> _DerivSession:
        return _DerivSession.for_account(_active_credentials()[2])

    async def _request(self, connection: _DerivConnection, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        try:
            return await connection.request(self._build_request(method, params))
        except asyncio.TimeoutError as exc:
            raise DerivExecutionError(f"Deriv {method} request timed out.") from exc

    async def _connect(self) -> _DerivConnection:
        try:
            return await self._session().connection(self)
        except DerivExecutionError:
            raise
        except Exception as exc:
            if "HTTP 401" in str(exc):
                raise DerivExecutionError(
                    "Deriv rejected DERIV_APP_ID (HTTP 401). Use a valid Deriv app ID, such as 1089."
                ) from exc
            raise DerivExecutionError(f"Could not connect to Deriv: {exc}") from exc

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        attempts = 2 if method in _IDEMPOTENT_METHODS else 1
        for attempt in range(attempts):
            connection = await self._connect()
            try:
                data = await self._request(connection, method, params)
                break
            except _ConnectionLost as exc:
                if attempt + 1 == attempts:
                    raise DerivExecutionError(f"Deriv {method} request failed: connection lost ({exc})") from exc
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
        them, so proposal -> buy must share one WebSocket session.
        """
        second_method = first_method
        connection = await self._connect()
        try:
            first_data = await self._request(connection, first_method, first_params)
            if first_data.get("error"):
                raise DerivExecutionError(f"Deriv {first_method} error: {first_data['error']}")
            second_method, second_params = second_builder(first_data)
            second_data = await self._request(connection, second_method, second_params)
        except _ConnectionLost as exc:
            raise DerivExecutionError(
                f"Deriv {first_method}/{second_method} request failed: connection lost ({exc})"
            ) from exc
        if second_data.get("error"):
            raise DerivExecutionError(f"Deriv {second_method} error: {second_data['error']}")
        return first_data, second_data

    async def get_account_balance(self) -> dict[str, Any] | None:
        mode = settings.execution_mode.lower()
        now = time.monotonic()
        cached = DerivClient._account_balance_cache.get(mode)
        if cached and now - cached[0] < DerivClient._CONTRACT_STATUS_CACHE_TTL:
            return cached[1]
        app_id, api_token, account_id = _active_credentials()
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
        # Unrealized P&L isn't included here - callers with DB access should
        # sum it from their own tracked open trades via get_contract_status
        # per contract, since the portfolio-wide lookup this used to use
        # (get_positions) has been observed to silently miss genuinely open
        # contracts.
        result = {
            "equity": equity,
            "available": equity,
            "currency": str(balance.get("currency", "")),
        }
        DerivClient._account_balance_cache[mode] = (now, result)
        return result

    async def get_positions(self) -> list[dict[str, Any]]:
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

    async def get_contract_status(self, contract_id: str, use_cache: bool = True) -> dict[str, Any] | None:
        """Look up a specific contract's current state on Deriv.

        Used to detect contracts Deriv has already closed (e.g. via its
        guaranteed stop-out) that our own DB doesn't know about yet, since we
        otherwise only learn of a close when a matching exit webhook arrives.

        Pass use_cache=False when the result must be guaranteed fresh (e.g.
        confirming a sale that was just made) - everywhere else this collapses
        repeated lookups of the same contract into one real Deriv call.
        """
        if not contract_id:
            return None
        now = time.monotonic()
        if use_cache:
            cached = DerivClient._contract_status_cache.get(contract_id)
            if cached and now - cached[0] < DerivClient._CONTRACT_STATUS_CACHE_TTL:
                return cached[1]
        try:
            result = await self._rpc("proposal_open_contract", {"contract_id": contract_id})
        except DerivExecutionError:
            return None
        poc = result.get("proposal_open_contract") if isinstance(result, dict) else None
        poc = poc if isinstance(poc, dict) else None
        DerivClient._contract_status_cache[contract_id] = (now, poc)
        return poc

    async def get_symbol_catalog(self) -> list[dict[str, Any]]:
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
        # Drop a TradingView exchange prefix (OANDA:EURUSD -> EURUSD).
        candidate = raw_symbol.strip().upper().rsplit(":", 1)[-1]
        catalog = await self.get_symbol_catalog()
        if not catalog:
            raise DerivExecutionError(
                "Could not load the Deriv symbol catalog to validate the requested symbol."
            )

        for item in catalog:
            code = str(item.get("underlying_symbol") or "")
            if code.upper() == candidate:
                return code

        for item in catalog:
            code = str(item.get("underlying_symbol") or "")
            if code.startswith(MARKET_CODE_PREFIXES) and code[3:].upper() == candidate:
                return code

        candidate_key = _normalize_symbol_key(candidate)
        alias = SYMBOL_ALIASES.get(candidate_key)
        if alias:
            candidate_key = _normalize_symbol_key(alias)
            for item in catalog:
                code = str(item.get("underlying_symbol") or "")
                if _normalize_symbol_key(code) == candidate_key:
                    return code

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

    async def place_order(
        self,
        signal: WebhookSignal,
        hedge_mode: bool = False,
        take_profit: float | None = None,
        stop_loss: float | None = None,
    ) -> dict[str, Any]:
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

        proposal = {
            "proposal": 1,
            "underlying_symbol": symbol,
            "contract_type": contract_type,
            "currency": "USD",
            "amount": float(signal.size),
            "basis": "stake",
            "multiplier": multiplier,
        }
        # Per-trade TP/SL lives on the contract itself, so Deriv closes it
        # even if the platform or the strategy's exit signal never arrives.
        limit_order = {
            key: round(value, 2)
            for key, value in (("take_profit", take_profit), ("stop_loss", stop_loss))
            if value
        }
        if limit_order:
            proposal["limit_order"] = limit_order

        _, result = await self._rpc_chain("proposal", proposal, _build_buy)
        buy_data = result.get("buy", result)
        order_id = buy_data.get("contract_id") if isinstance(buy_data, dict) else None
        return {
            "order_id": str(order_id or self._timestamp()),
            "mode": settings.execution_mode.lower(),
            # Snapped to Deriv's allowed set, so it can differ from the
            # requested leverage (e.g. "Auto" requests 1x).
            "multiplier": multiplier,
            "result": result,
        }

    async def close_order(self, contract_id: str) -> dict[str, Any]:
        mode = settings.execution_mode.lower()
        if not contract_id:
            return {"mode": mode, "message": "no_position"}

        # portfolio (used by get_positions) has been observed to silently omit
        # genuinely open contracts, which would make a symbol/direction search
        # wrongly conclude "no position" and sell nothing while the real
        # contract keeps running. Target this exact contract_id directly
        # instead - we already know it from the trade we're closing.
        poc = await self.get_contract_status(contract_id)
        if poc and poc.get("is_sold"):
            # Deriv already closed this itself (e.g. a stop-out) - nothing to sell.
            return {
                "mode": mode,
                "message": "already_sold",
                "order_id": contract_id,
                "profit": float(poc.get("profit") or 0),
            }

        result = await self._rpc("sell", {"sell": contract_id, "price": 0})
        sell_data = result.get("sell", result)
        sell_data = sell_data if isinstance(sell_data, dict) else {}
        order_id = sell_data.get("transaction_id")

        # Confirm the real realized profit from Deriv's own post-sale record
        # rather than trusting a pre-sale snapshot. Must bypass the cache -
        # the pre-sale check above may have just cached a stale "still open"
        # result for this exact contract_id moments ago. That lookup is a
        # separate request that can fail or come back empty, so retry once,
        # then fall back to the sale proceeds minus what the contract cost.
        profit = None
        for attempt in range(2):
            if attempt:
                await asyncio.sleep(1.0)
            final = await self.get_contract_status(contract_id, use_cache=False)
            if final and final.get("is_sold") and final.get("profit") is not None:
                profit = float(final["profit"])
                break
        if profit is None and sell_data.get("sold_for") is not None and poc and poc.get("buy_price") is not None:
            profit = round(float(sell_data["sold_for"]) - float(poc["buy_price"]), 2)

        return {
            "mode": mode,
            "order_id": str(order_id or contract_id),
            "result": result,
            "profit": profit,
        }

    async def get_profit_table(self, date_from: float) -> list[dict[str, Any]]:
        """Every contract closed since date_from (epoch seconds), with Deriv's
        own buy and sell prices - the account's ledger of realized P&L."""
        transactions: list[dict[str, Any]] = []
        page = 500
        while True:
            result = await self._rpc(
                "profit_table",
                {
                    "profit_table": 1,
                    "description": 1,
                    "date_from": str(int(date_from)),
                    "limit": page,
                    "offset": len(transactions),
                    "sort": "ASC",
                },
            )
            batch = (result.get("profit_table") or {}).get("transactions") or []
            transactions.extend(t for t in batch if isinstance(t, dict))
            if len(batch) < page:
                return transactions

    async def update_contract_tpsl(
        self,
        contract_id: str,
        take_profit: float | None,
        stop_loss: float | None,
    ) -> dict[str, Any]:
        """Set (or, with None, cancel) TP/SL on an already-open contract."""
        return await self._rpc(
            "contract_update",
            {
                "contract_update": 1,
                "contract_id": int(contract_id),
                "limit_order": {
                    "take_profit": round(take_profit, 2) if take_profit else None,
                    "stop_loss": round(stop_loss, 2) if stop_loss else None,
                },
            },
        )
