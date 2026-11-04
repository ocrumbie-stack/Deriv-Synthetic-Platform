import asyncio
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import home
from app.config import settings
from app.database import Base
from app.models import CryptoSignal, PositionStatus, RiskSettings, Signal, Strategy, Trade


def balance(equity=100.0):
    return AsyncMock(return_value={"equity": equity, "available": 90.0, "unrealized_pnl": 1.5})


class HomeTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.db.add(RiskSettings(id=1, emergency_stop=False, duplicate_blocking=True, execution_mode_override="live"))
        strategy = Strategy(name="v25")
        self.db.add(strategy)
        self.db.flush()
        now = datetime.utcnow()

        def trade(mode, status, net=0.0):
            return Trade(
                strategy_id=strategy.id, strategy_name="v25", symbol="R_25", direction="long", entry_price=1.0,
                size=5.0, execution_mode=mode, status=status, net_result=net,
                closed_at=now if status == PositionStatus.closed else None,
            )

        def signal(model, mode, name):
            return model(strategy_name=name, symbol="BTC-USDT", action="entry", direction="long",
                         status="executed", execution_mode=mode, **({"raw_payload": "{}"} if model is Signal else {}))

        self.db.add_all([
            trade("live", PositionStatus.open),
            trade("live", PositionStatus.closed, 4.0),
            trade("live", PositionStatus.closed, -1.0),
            trade("demo", PositionStatus.open),
            trade("demo", PositionStatus.closed, 50.0),
            signal(Signal, "live", "live-synthetic"),
            signal(Signal, "demo", "demo-synthetic"),
            signal(CryptoSignal, "live", "live-crypto"),
            signal(CryptoSignal, "demo", "demo-crypto"),
        ])
        self.db.commit()
        self._mode = settings.execution_mode
        settings.execution_mode = "demo"

    def tearDown(self):
        settings.execution_mode = self._mode
        self.db.close()

    def _summary(self, synthetic_balance=None, crypto_connected=False, crypto_overview=None):
        with patch.object(home, "crypto_configured", return_value=crypto_connected), \
             patch.object(home.crypto, "overview", crypto_overview or AsyncMock()):
            return asyncio.run(home.summary(self.db, synthetic_balance or balance()))

    def test_only_current_mode_is_counted(self):
        data = self._summary()
        self.assertEqual(data["mode"], "live")
        synthetic = data["markets"][0]
        self.assertEqual(synthetic["open_positions"], 1)
        self.assertEqual(synthetic["today"], {"trades": 2, "wins": 1, "net": 3.0})
        self.assertEqual({row["strategy_name"] for row in data["activity"]}, {"live-synthetic", "live-crypto"})

    def test_markets_are_named_generically(self):
        data = self._summary()
        self.assertEqual([m["label"] for m in data["markets"]], ["Synthetic", "Forex", "Crypto"])
        self.assertFalse(data["markets"][1]["connected"])

    def test_failed_market_does_not_block_others(self):
        overview = AsyncMock(side_effect=RuntimeError("down"))
        data = self._summary(crypto_connected=True, crypto_overview=overview)
        synthetic, _, crypto = data["markets"]
        self.assertEqual(synthetic["balance"]["equity"], 100.0)
        self.assertIsNone(synthetic["error"])
        self.assertEqual(crypto["error"], "down")
        self.assertIsNone(crypto["balance"])

    def test_slow_market_times_out(self):
        async def slow():
            await asyncio.sleep(1)

        with patch.object(home, "TIMEOUT", 0.01):
            data = self._summary(synthetic_balance=slow)
        self.assertEqual(data["markets"][0]["error"], "timed out")
        self.assertEqual(data["markets"][0]["open_positions"], 1)

    def test_crypto_balance_and_positions(self):
        overview = AsyncMock(return_value={"balance": {"equity": 20.0}, "positions": [{}, {}], "error": None})
        crypto = self._summary(crypto_connected=True, crypto_overview=overview)["markets"][2]
        self.assertEqual((crypto["balance"]["equity"], crypto["open_positions"]), (20.0, 2))


if __name__ == "__main__":
    unittest.main()
