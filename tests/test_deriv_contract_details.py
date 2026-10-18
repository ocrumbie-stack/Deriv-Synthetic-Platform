import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import services
from app.config import settings
from app.database import Base
from app.models import RiskSettings, SignalBot, Trade
from app.schemas import WebhookSignal


class DerivContractDetailsTests(unittest.TestCase):
    """Entry price and fees come from Deriv's contract, not the alert price,
    and gross P/L is Deriv's net profit with the commission added back."""

    def setUp(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.db.add_all([
            RiskSettings(id=1, emergency_stop=False, duplicate_blocking=True, execution_mode_override="live"),
            SignalBot(name="v25", size=10.0, leverage=0, enabled=True, hedge_mode=False),
        ])
        self.db.commit()
        self.strategy = services.get_or_create_strategy(self.db, "v25")
        self._secret, self._mode = settings.webhook_secret, settings.execution_mode
        settings.webhook_secret = "s"

    def tearDown(self):
        settings.webhook_secret, settings.execution_mode = self._secret, self._mode
        self.db.close()

    def test_entry_records_deriv_fill_and_commission(self):
        client = AsyncMock()
        client.resolve_symbol.return_value = "1HZ25V"
        client.place_order.return_value = {
            "order_id": "222", "multiplier": 160, "commission": "0.48", "entry_spot": "865100.5",
        }
        payload = WebhookSignal(
            secret="s", strategy="v25", symbol="VOLATILITY_25_1S_INDEX", action="entry",
            direction="long", price=867940.0,
        )
        with patch.object(services, "DerivClient", return_value=client):
            result = asyncio.run(services.process_webhook_signal(self.db, payload))
        self.assertEqual(result.trade.entry_price, 865100.5)
        self.assertEqual(result.trade.fees, 0.48)
        self.assertEqual(result.trade.leverage, 160)

    def test_entry_keeps_alert_price_until_deriv_has_filled(self):
        client = AsyncMock()
        client.resolve_symbol.return_value = "1HZ25V"
        client.place_order.return_value = {"order_id": "222", "multiplier": 160, "entry_spot": None}
        payload = WebhookSignal(
            secret="s", strategy="v25", symbol="VOLATILITY_25_1S_INDEX", action="entry",
            direction="long", price=867940.0,
        )
        with patch.object(services, "DerivClient", return_value=client):
            result = asyncio.run(services.process_webhook_signal(self.db, payload))
        self.assertEqual(result.trade.entry_price, 867940.0)

    def _open_trade(self):
        trade = Trade(
            strategy_id=self.strategy.id, strategy_name="v25", symbol="VOLATILITY_25_1S_INDEX",
            direction="short", entry_price=867940.0, size=10.0, leverage=160, execution_mode="live",
            exchange_order_id="111",
        )
        self.db.add(trade)
        self.db.commit()
        return trade

    def test_deriv_close_uses_contract_prices_and_splits_gross_net(self):
        trade = self._open_trade()
        client = AsyncMock()
        client.get_contract_status.return_value = {
            "is_sold": 1, "profit": -3.01, "commission": 0.48,
            "entry_spot": 867700.0, "sell_spot": 867856.69, "sell_time": 1760666400,
        }
        with patch.object(services, "DerivClient", return_value=client):
            self.assertTrue(asyncio.run(services.reconcile_open_trade(self.db, trade)))
        self.assertEqual(trade.entry_price, 867700.0)
        self.assertEqual(trade.exit_price, 867856.69)
        self.assertEqual(trade.net_result, -3.01)
        self.assertEqual(trade.fees, 0.48)
        self.assertAlmostEqual(trade.profit_loss, -2.53)

    def test_strategy_exit_prefers_deriv_sell_spot(self):
        trade = self._open_trade()
        client = AsyncMock()
        client.close_order.return_value = {
            "profit": 1.52,
            "contract": {"entry_spot": 867700.0, "sell_spot": 867600.0, "commission": 0.48},
        }
        with patch.object(services, "DerivClient", return_value=client):
            asyncio.run(services.close_trade(self.db, trade, 867500.0, None, "strategy_exit"))
        self.assertEqual(trade.entry_price, 867700.0)
        self.assertEqual(trade.exit_price, 867600.0)
        self.assertEqual(trade.net_result, 1.52)
        self.assertAlmostEqual(trade.profit_loss, 2.0)


if __name__ == "__main__":
    unittest.main()
