import asyncio
import json
import unittest
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import ctrader
from app.config import settings
from app.ctrader import CTraderClient, CTraderError, close_out, lots_to_volume, match_symbol, money, position_out, relative_distance
from fastapi import HTTPException

from app.ctrader_routes import AccountPick, move_forex_login_off_synthetic, pick_accounts, pick_default_accounts, status_out
from app.database import Base
from app.models import CTraderConnection, RiskSettings, SignalBot, Strategy
from app.schemas import SignalBotCreate, WebhookSignal
from app.services import validate_signal

DERIV_SYMBOLS = [
    {"symbolId": 1, "symbolName": "Volatility 75 Index", "enabled": True},
    {"symbolId": 2, "symbolName": "Volatility 75 (1s) Index", "enabled": True},
    {"symbolId": 3, "symbolName": "Volatility 10 Index", "enabled": True},
    {"symbolId": 4, "symbolName": "EURUSD", "description": "Euro vs US Dollar", "enabled": True},
]
# lotSize 100 cents = 1 unit per lot; volume steps of 0.01 lots.
V75 = {"symbolId": 1, "symbolName": "Volatility 75 Index", "lotSize": 100, "stepVolume": 1, "minVolume": 1, "maxVolume": 100000}


class SymbolTests(unittest.TestCase):
    def test_matches_deriv_codes_tradingview_tickers_and_names(self):
        for raw, symbol_id in (
            ("R_75", 1),
            ("1HZ75V", 2),
            ("VOLATILITY_75_INDEX", 1),
            ("VOLATILITY_75_1S_INDEX", 2),
            ("Volatility 75 (1s) Index", 2),
            ("DERIV:VOLATILITY_10_INDEX", 3),
            ("EURUSD", 4),
            ("frxEURUSD", 4),
            ("OANDA:EURUSD", 4),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(match_symbol(raw, DERIV_SYMBOLS)["symbolId"], symbol_id)

    def test_unknown_symbol(self):
        self.assertIsNone(match_symbol("R_100", DERIV_SYMBOLS))


class SizingTests(unittest.TestCase):
    def test_lots_to_volume(self):
        self.assertEqual(lots_to_volume(0.5, V75), 50)
        self.assertEqual(lots_to_volume(0.1, V75), 10)
        # Floored to the step, never rounded up.
        self.assertEqual(lots_to_volume(0.257, {**V75, "stepVolume": 5}), 25)
        fx = {"symbolName": "EURUSD", "lotSize": 10_000_000, "stepVolume": 100_000, "minVolume": 100_000, "maxVolume": 10_000_000_000}
        self.assertEqual(lots_to_volume(0.01, fx), 100_000)

    def test_lots_outside_limits(self):
        with self.assertRaisesRegex(CTraderError, "below"):
            lots_to_volume(0.001, V75)
        with self.assertRaisesRegex(CTraderError, "above"):
            lots_to_volume(5000, V75)

    def test_units(self):
        self.assertEqual(relative_distance(1.23), 123000)
        self.assertEqual(relative_distance(-534.23782), 53423782)
        self.assertEqual(money(12345, 2), 123.45)
        self.assertEqual(money(150000000, 8), 1.5)

    def test_position_and_close_shapes(self):
        position = position_out({
            "positionId": "77", "price": 412345.12, "swap": -25, "commission": -10, "moneyDigits": 2,
            "tradeData": {"symbolId": 1, "volume": 50, "tradeSide": 2, "openTimestamp": 1_700_000_000_000, "label": "bot"},
        })
        self.assertEqual((position["position_id"], position["direction"], position["volume"]), (77, "short", 50))
        self.assertEqual((position["swap"], position["commission"]), (-0.25, -0.1))
        closed = close_out({
            "positionId": 77, "executionPrice": 410000.0,
            "closePositionDetail": {"entryPrice": 412345.12, "grossProfit": 1172, "swap": -25, "commission": -10, "moneyDigits": 2, "balance": 100000},
        })
        self.assertEqual(closed["net_profit"], 11.37)
        self.assertEqual(closed["exit_price"], 410000.0)


class FakeServer:
    """A cTrader host in memory: answers each request with `respond`."""

    def __init__(self, respond):
        self.respond = respond
        self.sent: list[dict] = []
        self.queue: asyncio.Queue = asyncio.Queue()

    async def connect(self, host):
        self.host = host
        return self

    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        for reply in self.respond(message) or []:
            await self.queue.put(json.dumps(reply))

    async def close(self):
        await self.queue.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


def reply(message, payload_type, payload=None, **extra):
    return {"clientMsgId": message.get("clientMsgId"), "payloadType": payload_type, "payload": payload or {}, **extra}


def standard_responses(message, order_events=None):
    kind = message["payloadType"]
    if kind == ctrader.APP_AUTH_REQ:
        return [reply(message, ctrader.APP_AUTH_RES)]
    if kind == ctrader.ACCOUNT_AUTH_REQ:
        return [reply(message, ctrader.ACCOUNT_AUTH_RES, {"ctidTraderAccountId": 42})]
    if kind == ctrader.NEW_ORDER_REQ and order_events:
        return order_events(message)
    return []


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ctrader._Session.reset()
        self._id, self._secret = settings.ctrader_client_id, settings.ctrader_client_secret
        settings.ctrader_client_id, settings.ctrader_client_secret = "id", "secret"

    async def asyncTearDown(self):
        for session in ctrader._Session._sessions.values():
            if session.connection:
                await session.connection.close()
        ctrader._Session.reset()
        settings.ctrader_client_id, settings.ctrader_client_secret = self._id, self._secret

    def client(self, server, token=None):
        async def default_token(force=False):
            return "token"

        return CTraderClient(account_id=42, live=False, connect=server.connect, token=token or default_token)

    async def test_account_summary_covers_every_position(self):
        def respond(message):
            kind = message["payloadType"]
            if kind == ctrader.TRADER_REQ:
                return [reply(message, ctrader.TRADER_RES, {"trader": {
                    "balance": 1000000, "moneyDigits": 2, "depositAssetId": 7, "accountType": 0,
                    "leverageInCents": 50000, "brokerName": "fusionmarkets", "traderLogin": 10151676,
                }})]
            if kind == ctrader.RECONCILE_REQ:
                # One opened by a bot, one opened by hand in the cTrader app.
                return [reply(message, ctrader.RECONCILE_RES, {"position": [
                    {"positionId": 1, "moneyDigits": 2, "usedMargin": 20000, "tradeData": {"symbolId": 1, "volume": 100000, "tradeSide": 1, "label": "bot"}},
                    {"positionId": 2, "moneyDigits": 2, "usedMargin": 5000, "tradeData": {"symbolId": 2, "volume": 100000, "tradeSide": 2}},
                ]})]
            if kind == ctrader.UNREALIZED_PNL_REQ:
                return [reply(message, ctrader.UNREALIZED_PNL_RES, {"moneyDigits": 2, "positionUnrealizedPnL": [
                    {"positionId": 1, "grossUnrealizedPnL": 1600, "netUnrealizedPnL": 1500},
                    {"positionId": 2, "grossUnrealizedPnL": -400, "netUnrealizedPnL": -500},
                ]})]
            if kind == ctrader.DEAL_LIST_REQ:
                return [reply(message, ctrader.DEAL_LIST_RES, {"deal": [
                    {"positionId": 3, "closePositionDetail": {"grossProfit": 2500, "swap": 0, "commission": -50, "moneyDigits": 2}},
                    {"positionId": 4, "closePositionDetail": {"grossProfit": -1000, "swap": 0, "commission": -50, "moneyDigits": 2}},
                    {"positionId": 5},  # an opening deal - not counted
                ]})]
            if kind == ctrader.ASSET_LIST_REQ:
                return [reply(message, ctrader.ASSET_LIST_RES, {"asset": [{"assetId": 7, "name": "USD", "displayName": "USD"}]})]
            return standard_responses(message)

        CTraderClient._deposit_assets.clear()
        out = await self.client(FakeServer(respond)).account_summary(datetime(2026, 11, 9))
        self.assertEqual((out["balance"], out["unrealized_pnl"], out["equity"]), (10000.0, 10.0, 10010.0))
        self.assertEqual((out["margin_used"], out["free_margin"], out["margin_level"]), (250.0, 9760.0, 4004.0))
        self.assertEqual((out["open_positions"], out["longs"], out["shorts"]), (2, 1, 1))
        self.assertEqual(out["today"], {"trades": 2, "wins": 1, "net": 14.0})
        self.assertEqual((out["currency"], out["leverage"], out["hedging"]), ("USD", 500.0, True))

    async def test_account_summary_without_unrealized_pnl(self):
        def respond(message):
            kind = message["payloadType"]
            if kind == ctrader.TRADER_REQ:
                return [reply(message, ctrader.TRADER_RES, {"trader": {"balance": 50000, "moneyDigits": 2}})]
            if kind == ctrader.RECONCILE_REQ:
                return [reply(message, ctrader.RECONCILE_RES, {})]
            if kind in (ctrader.UNREALIZED_PNL_REQ, ctrader.DEAL_LIST_REQ, ctrader.ASSET_LIST_REQ):
                return [reply(message, ctrader.OA_ERROR_RES, {"errorCode": "UNSUPPORTED", "description": "nope"})]
            return standard_responses(message)

        CTraderClient._deposit_assets.clear()
        out = await self.client(FakeServer(respond)).account_summary(datetime(2026, 11, 9))
        self.assertEqual(out["balance"], 500.0)
        self.assertIsNone(out["equity"])
        self.assertIsNone(out["free_margin"])
        self.assertIsNone(out["today"])
        self.assertIsNone(out["currency"])

    async def test_market_order_waits_for_the_fill(self):
        def events(message):
            order_id = message["payload"]["clientOrderId"]
            # Events matched on the order's clientOrderId, not clientMsgId.
            accepted = {"payloadType": ctrader.EXECUTION_EVENT, "payload": {"executionType": ctrader.EXEC_ORDER_ACCEPTED, "order": {"clientOrderId": order_id}}}
            filled = {"payloadType": ctrader.EXECUTION_EVENT, "payload": {
                "executionType": ctrader.EXEC_ORDER_FILLED,
                "order": {"clientOrderId": order_id},
                "deal": {"executionPrice": 412000.5},
                "position": {"positionId": 9, "price": 411999.0, "moneyDigits": 2, "tradeData": {"symbolId": 1, "volume": 50, "tradeSide": 1}},
            }}
            return [accepted, filled]

        server = FakeServer(lambda m: standard_responses(m, events))
        position = await self.client(server).open_market(V75, "long", 0.5, stop_loss_distance=1500.0, take_profit_distance=3000.0)
        self.assertEqual(position["position_id"], 9)
        self.assertEqual(position["entry_price"], 412000.5)
        self.assertEqual(server.host, ctrader.DEMO_HOST)
        order = next(m for m in server.sent if m["payloadType"] == ctrader.NEW_ORDER_REQ)["payload"]
        self.assertEqual(order["ctidTraderAccountId"], 42)
        self.assertEqual((order["orderType"], order["tradeSide"], order["volume"]), (1, 1, 50))
        self.assertEqual((order["relativeStopLoss"], order["relativeTakeProfit"]), (150000000, 300000000))
        self.assertNotIn("stopLoss", order)
        kinds = [m["payloadType"] for m in server.sent]
        self.assertEqual(kinds[:2], [ctrader.APP_AUTH_REQ, ctrader.ACCOUNT_AUTH_REQ])

    async def test_order_error_is_raised(self):
        def events(message):
            return [{"payloadType": ctrader.ORDER_ERROR_EVENT, "clientMsgId": message["clientMsgId"],
                     "payload": {"errorCode": "NOT_ENOUGH_MONEY", "description": "Not enough money"}}]

        server = FakeServer(lambda m: standard_responses(m, events))
        with self.assertRaisesRegex(CTraderError, "NOT_ENOUGH_MONEY"):
            await self.client(server).open_market(V75, "short", 0.1)

    async def test_expired_token_is_refreshed_once(self):
        calls = []

        async def token(force=False):
            calls.append(force)
            return "fresh" if force else "stale"

        def respond(message):
            if message["payloadType"] == ctrader.ACCOUNT_AUTH_REQ and message["payload"]["accessToken"] == "stale":
                return [reply(message, ctrader.OA_ERROR_RES, {"errorCode": "CH_ACCESS_TOKEN_INVALID"})]
            if message["payloadType"] == ctrader.TRADER_REQ:
                return [reply(message, ctrader.TRADER_RES, {"trader": {"balance": 31343, "moneyDigits": 2, "accountType": 0, "traderLogin": 1228815}})]
            return standard_responses(message)

        info = await self.client(FakeServer(respond), token).account_info()
        self.assertEqual(calls, [False, True])
        self.assertEqual(info["balance"], 313.43)
        self.assertTrue(info["hedging"])

    async def test_app_login_refused(self):
        server = FakeServer(lambda m: [reply(m, ctrader.ERROR_RES, {"errorCode": "CH_CLIENT_AUTH_FAILURE"})])
        with self.assertRaisesRegex(CTraderError, "app login"):
            await self.client(server).positions()

    async def test_close_matches_on_position_id(self):
        def respond(message):
            if message["payloadType"] == ctrader.CLOSE_POSITION_REQ:
                return [{"payloadType": ctrader.EXECUTION_EVENT, "payload": {
                    "executionType": ctrader.EXEC_ORDER_FILLED,
                    "position": {"positionId": 9},
                    "deal": {"positionId": 9, "executionPrice": 413000.0, "closePositionDetail": {"grossProfit": 500, "swap": 0, "commission": -20, "moneyDigits": 2}},
                }}]
            return standard_responses(message)

        result = await self.client(FakeServer(respond)).close_position(9, 50)
        self.assertEqual(result["net_profit"], 4.8)


class AccountPickTests(unittest.TestCase):
    def test_single_account_per_mode_is_picked(self):
        row = CTraderConnection(id=1)
        accounts = [
            {"account_id": 1, "live": True, "login": 1228815, "broker": "Deriv"},
            {"account_id": 2, "live": False, "login": 555, "broker": "Deriv"},
        ]
        pick_default_accounts(row, accounts)
        self.assertEqual((row.demo_account_id, row.live_account_id), (2, 1))

    def test_ambiguous_or_unlinked_picks_are_cleared(self):
        row = CTraderConnection(id=1, demo_account_id=9, live_account_id=1)
        accounts = [
            {"account_id": 1, "live": True},
            {"account_id": 2, "live": False},
            {"account_id": 3, "live": False},
        ]
        pick_default_accounts(row, accounts)
        self.assertEqual((row.demo_account_id, row.live_account_id), (None, 1))


class MarketTests(unittest.TestCase):
    ACCOUNTS = [
        {"account_id": 1, "live": True, "login": 1228815, "broker": "Deriv"},
        {"account_id": 2, "live": False, "login": 555, "broker": "", "broker_name": "deriv"},
        {"account_id": 3, "live": False, "login": 10151676, "broker": "Fusion Markets"},
    ]

    def setUp(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()

    def tearDown(self):
        self.db.close()

    def test_synthetic_gets_deriv_and_forex_gets_the_rest(self):
        ids = lambda market: [a["account_id"] for a in ctrader.accounts_for_market(market, self.ACCOUNTS)]
        self.assertEqual(ids("synthetic"), [1, 2])
        self.assertEqual(ids("forex"), [3])

    def test_status_only_lists_the_markets_accounts(self):
        row = CTraderConnection(id=1, access_token="t", accounts=json.dumps(self.ACCOUNTS), demo_account_id=2)
        out = status_out(row, "synthetic")
        self.assertEqual([a["account_id"] for a in out["accounts"]], [1, 2])
        self.assertEqual(out["other_accounts"], 1)

    def test_picking_another_markets_account_is_refused(self):
        self.db.add(CTraderConnection(id=1, access_token="t", accounts=json.dumps(self.ACCOUNTS)))
        self.db.commit()
        with self.assertRaises(HTTPException):
            pick_accounts(AccountPick(demo_account_id=3), "synthetic", self.db)
        self.assertEqual(pick_accounts(AccountPick(demo_account_id=2), "synthetic", self.db)["demo_account_id"], 2)

    def test_a_non_deriv_login_moves_to_forex_once(self):
        fusion = [self.ACCOUNTS[2]]
        self.db.add(CTraderConnection(id=1, access_token="t", refresh_token="r", accounts=json.dumps(fusion), demo_account_id=3))
        self.db.commit()
        move_forex_login_off_synthetic(self.db)
        self.db.commit()
        synthetic, forex_row = self.db.get(CTraderConnection, 1), self.db.get(CTraderConnection, 2)
        self.assertIsNone(synthetic.access_token)
        self.assertIsNone(synthetic.demo_account_id)
        self.assertEqual((forex_row.access_token, forex_row.refresh_token, forex_row.demo_account_id), ("t", "r", 3))

    def test_a_deriv_login_stays_on_synthetic(self):
        self.db.add(CTraderConnection(id=1, access_token="t", accounts=json.dumps(self.ACCOUNTS)))
        self.db.commit()
        move_forex_login_off_synthetic(self.db)
        self.assertIsNone(self.db.get(CTraderConnection, 2))
        self.assertEqual(self.db.get(CTraderConnection, 1).access_token, "t")

    def test_unknown_market(self):
        with self.assertRaises(CTraderError):
            CTraderClient(account_id=1, live=False, market="stocks")


class BotAccountTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.db.add(RiskSettings(id=1))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_ctrader_bot_needs_lots(self):
        with self.assertRaises(ValidationError):
            SignalBotCreate(name="Bot", account="ctrader")
        self.assertEqual(SignalBotCreate(name="Bot", account="ctrader", lots=0.5).lots, 0.5)
        self.assertEqual(SignalBotCreate(name="Bot").account, "options")

    def test_ctrader_entries_never_reach_the_options_account(self):
        strategy = Strategy(name="Bot")
        bot = SignalBot(name="Bot", size=5, account="ctrader", lots=0.5, enabled=True)
        self.db.add_all([strategy, bot])
        self.db.commit()
        payload = WebhookSignal(secret=settings.webhook_secret, strategy="Bot", symbol="R_75", action="entry", direction="long", size=5)
        self.assertIn("cTrader", validate_signal(self.db, payload, strategy, bot))


if __name__ == "__main__":
    unittest.main()
