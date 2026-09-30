import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import services
from app.config import settings
from app.database import Base
from app.models import PositionStatus, RiskSettings, SignalBot, Trade
from app.schemas import WebhookSignal


class ModeIsolationTests(unittest.TestCase):
    """A position left open in one execution mode must not affect the other."""

    def setUp(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.db.add(RiskSettings(id=1, emergency_stop=False, duplicate_blocking=True, execution_mode_override="live"))
        self.db.add(SignalBot(name="v25", size=5.0, leverage=0, enabled=True, hedge_mode=False))
        self.db.commit()
        strategy = services.get_or_create_strategy(self.db, "v25")
        self.demo_short = Trade(
            strategy_id=strategy.id, strategy_name="v25", symbol="VOLATILITY_25_INDEX",
            direction="short", entry_price=1.0, size=5.0, leverage=1, execution_mode="demo",
            exchange_order_id="111",
        )
        self.db.add(self.demo_short)
        self.db.commit()
        self._secret, self._mode = settings.webhook_secret, settings.execution_mode
        settings.webhook_secret = "s"
        # A restart leaves the env default in place until the DB override is read.
        settings.execution_mode = "demo"

    def tearDown(self):
        settings.webhook_secret, settings.execution_mode = self._secret, self._mode
        self.db.close()

    def _send(self, direction):
        client = AsyncMock()
        client.resolve_symbol.return_value = "R_25"
        client.place_order.return_value = {"order_id": "222"}
        client.get_contract_status.return_value = {"is_sold": 0}
        payload = WebhookSignal(
            secret="s", strategy="v25", symbol="VOLATILITY_25_INDEX", action="entry", direction=direction
        )
        with patch.object(services, "DerivClient", return_value=client):
            return asyncio.run(services.process_webhook_signal(self.db, payload)), client

    def test_demo_position_does_not_block_live_entry(self):
        result, client = self._send("short")
        self.assertEqual(result.signal.status.value, "executed", result.signal.rejection_reason)
        self.assertEqual(result.trade.execution_mode, "live")
        client.close_order.assert_not_called()

    def test_live_reversal_does_not_sell_demo_contract(self):
        result, client = self._send("long")
        self.assertEqual(result.signal.status.value, "executed", result.signal.rejection_reason)
        client.close_order.assert_not_called()
        self.db.refresh(self.demo_short)
        self.assertEqual(self.demo_short.status, PositionStatus.open)


if __name__ == "__main__":
    unittest.main()
