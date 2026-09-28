import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import services
from app.config import settings
from app.database import Base
from app.models import PositionStatus, RiskSettings, Signal, SignalBot, Trade
from app.schemas import WebhookSignal


class ReversalTests(unittest.TestCase):
    """An opposite-direction entry must close the old position even when the
    new entry itself is blocked - but never for a signal an exit would reject."""

    def setUp(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.risk = RiskSettings(id=1, emergency_stop=False, duplicate_blocking=True, execution_mode_override="live")
        self.bot = SignalBot(name="v25", size=5.0, leverage=0, enabled=True, hedge_mode=False)
        self.db.add_all([self.risk, self.bot])
        self.db.commit()
        strategy = services.get_or_create_strategy(self.db, "v25")
        self.short = Trade(
            strategy_id=strategy.id, strategy_name="v25", symbol="VOLATILITY_25_INDEX",
            direction="short", entry_price=1.0, size=5.0, leverage=1, execution_mode="live",
            exchange_order_id="111",
        )
        self.db.add(self.short)
        self.db.commit()
        self._secret, self._mode = settings.webhook_secret, settings.execution_mode
        settings.webhook_secret = "s"

    def tearDown(self):
        settings.webhook_secret, settings.execution_mode = self._secret, self._mode
        self.db.close()

    def _send(self, secret="s", signal_id=None):
        client = AsyncMock()
        client.place_order.return_value = {"order_id": "222"}
        client.close_order.return_value = {"profit": 1.0}
        client.get_contract_status.return_value = {"is_sold": 0}
        payload = WebhookSignal(
            secret=secret, strategy="v25", symbol="VOLATILITY_25_INDEX", action="entry",
            direction="long", signal_id=signal_id,
        )
        with patch.object(services, "DerivClient", return_value=client):
            return asyncio.run(services.process_webhook_signal(self.db, payload)), client

    def _journal(self):
        return [(s.source, s.status.value) for s in self.db.scalars(select(Signal).order_by(Signal.id))]

    def test_reversal_within_exposure_limit(self):
        # One stake of exposure: the old position's stake is freed by the close.
        self.risk.max_account_exposure = 5.0
        self.db.commit()
        result, client = self._send()
        self.assertEqual(result.signal.status.value, "executed", result.signal.rejection_reason)
        client.close_order.assert_called_once_with("111")

    def test_paused_bot_still_closes_reversed_position(self):
        self.bot.enabled = False
        self.db.commit()
        result, client = self._send()
        self.assertEqual(result.signal.status.value, "rejected")
        self.assertIn("still closed", result.signal.rejection_reason)
        client.place_order.assert_not_called()
        self.db.refresh(self.short)
        self.assertEqual(self.short.status, PositionStatus.closed)
        self.assertEqual(self._journal(), [("strategy_entry", "rejected"), ("reversal", "closed")])

    def test_invalid_secret_never_closes(self):
        self.bot.enabled = False
        self.db.commit()
        _, client = self._send(secret="wrong")
        client.close_order.assert_not_called()

    def test_replayed_signal_id_never_closes(self):
        self.db.add(Signal(
            strategy_name="v25", symbol="VOLATILITY_25_INDEX", action="entry", size=5.0, leverage=1,
            signal_id="abc", status="executed", raw_payload="{}", execution_mode="live",
        ))
        self.db.commit()
        _, client = self._send(signal_id="abc")
        client.close_order.assert_not_called()

    def test_signal_id_does_not_block_its_own_reversal_close(self):
        self.bot.enabled = False
        self.db.commit()
        _, client = self._send(signal_id="fresh")
        client.close_order.assert_called_once_with("111")

    def test_exit_symbol_case_still_finds_open_position(self):
        client = AsyncMock()
        client.close_order.return_value = {"profit": 1.0}
        payload = WebhookSignal(secret="s", strategy="v25", symbol=" Volatility_25_Index ", action="exit")
        with patch.object(services, "DerivClient", return_value=client):
            result = asyncio.run(services.process_webhook_signal(self.db, payload))
        self.assertEqual(result.signal.status.value, "closed", result.signal.rejection_reason)
        client.close_order.assert_called_once_with("111")


if __name__ == "__main__":
    unittest.main()
