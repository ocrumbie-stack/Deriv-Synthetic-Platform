import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main, services
from app.config import settings
from app.database import Base, get_db
from app.deriv import DerivExecutionError
from app.models import RiskSettings, SignalBot, SymbolLeverage, Trade
from app.schemas import WebhookSignal


class LeverageTests(unittest.TestCase):
    """The multiplier an entry requests always comes from the platform - the
    bot's own override, else the Leverage page - never from the webhook."""

    def setUp(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.bot = SignalBot(name="v25", size=5.0, leverage=0, enabled=True, hedge_mode=False)
        self.db.add_all([
            RiskSettings(id=1, emergency_stop=False, duplicate_blocking=True, execution_mode_override="demo"),
            self.bot,
        ])
        self.db.commit()
        self._secret, self._mode = settings.webhook_secret, settings.execution_mode
        settings.webhook_secret = "s"

    def tearDown(self):
        settings.webhook_secret, settings.execution_mode = self._secret, self._mode
        self.db.close()

    def _requested_leverage(self, symbol="VOLATILITY_25_INDEX", leverage=10, resolve=None):
        client = AsyncMock()
        if resolve is None:
            client.resolve_symbol.return_value = "R_25"
        else:
            client.resolve_symbol.side_effect = resolve
        client.place_order.return_value = {"order_id": "1"}
        client.get_contract_status.return_value = {"is_sold": 0}
        payload = WebhookSignal(
            secret="s", strategy="v25", symbol=symbol, action="entry", direction="long", leverage=leverage
        )
        with patch.object(services, "DerivClient", return_value=client):
            result = asyncio.run(services.process_webhook_signal(self.db, payload))
        self.assertEqual(result.signal.status.value, "executed", result.signal.rejection_reason)
        return client.place_order.call_args.args[0].leverage

    def test_leverage_page_applies_to_an_aliased_ticker(self):
        self.db.add(SymbolLeverage(symbol="R_25", leverage=200))
        self.db.commit()
        self.assertEqual(self._requested_leverage(), 200)

    def test_leverage_page_change_applies_to_the_next_entry(self):
        row = SymbolLeverage(symbol="R_25", leverage=200)
        self.db.add(row)
        self.db.commit()
        self.assertEqual(self._requested_leverage(), 200)
        self.db.query(Trade).delete()  # Free the slot so the next entry isn't a duplicate.
        row.leverage = 400
        self.db.commit()
        self.assertEqual(self._requested_leverage(), 400)

    def test_bot_override_beats_leverage_page(self):
        self.db.add(SymbolLeverage(symbol="R_25", leverage=200))
        self.bot.leverage = 50
        self.db.commit()
        self.assertEqual(self._requested_leverage(), 50)

    def test_clearing_bot_override_falls_back_to_leverage_page(self):
        self.db.add(SymbolLeverage(symbol="R_25", leverage=200))
        self.bot.leverage = 50
        self.db.commit()
        self.bot.leverage = 0
        self.db.commit()
        self.assertEqual(self._requested_leverage(), 200)

    def test_webhook_leverage_is_ignored_without_a_leverage_row(self):
        # 1x snaps to the symbol's minimum - what the Leverage page shows.
        self.assertEqual(self._requested_leverage(leverage=500), 1)

    def test_unresolvable_symbol_falls_back_to_raw_ticker(self):
        self.db.add(SymbolLeverage(symbol="R_25", leverage=300))
        self.db.commit()
        leverage = self._requested_leverage(symbol="R_25", resolve=DerivExecutionError("catalog down"))
        self.assertEqual(leverage, 300)


class LeveragePageSaveTests(unittest.TestCase):
    """F3: a dashboard refresh seeding a symbol's row while a save awaits
    Deriv must not fail the save with UNIQUE constraint failed."""

    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        main.app.dependency_overrides[get_db] = lambda: self.db
        self._google = settings.google_client_id
        settings.google_client_id = ""

    def tearDown(self):
        main.app.dependency_overrides.pop(get_db, None)
        settings.google_client_id = self._google
        self.db.close()

    def test_save_survives_a_concurrent_seed(self):
        other = self.Session()

        async def seed_meanwhile(code, contract_type):
            # What the dashboard's GET /api/symbol-leverage does for a new symbol.
            other.add(SymbolLeverage(symbol=code, leverage=50, allowed_multipliers="50,100"))
            other.commit()
            return [50, 100]

        client = AsyncMock()
        client.resolve_symbol.return_value = "frxXAUUSD"
        client.get_symbol_catalog.return_value = []
        client.get_multiplier_range.side_effect = seed_meanwhile
        with patch.object(main, "DerivClient", return_value=client):
            r = TestClient(main.app).patch("/api/symbol-leverage/XAUUSD", json={"leverage": 100})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["leverage"], 100)
        rows = self.db.scalars(select(SymbolLeverage)).all()
        self.assertEqual([(row.symbol, row.leverage) for row in rows], [("frxXAUUSD", 100)])
        other.close()


if __name__ == "__main__":
    unittest.main()
