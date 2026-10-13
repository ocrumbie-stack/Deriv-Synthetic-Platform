import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import services, trailing
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import PositionStatus, RiskSettings, Signal, Trade


class CloseAllTargetBase:
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        strategy = services.get_or_create_strategy(self.db, "v25")
        self.trades = {
            order_id: Trade(
                strategy_id=strategy.id, strategy_name="v25", symbol=symbol, direction="long", entry_price=1.0,
                size=5.0, leverage=1, execution_mode=mode, exchange_order_id=order_id,
            )
            for order_id, symbol, mode in (
                ("1", "VOLATILITY_25_INDEX", "live"),
                ("2", "VOLATILITY_10_INDEX", "live"),
                ("3", "VOLATILITY_75_INDEX", "demo"),
            )
        }
        self.db.add_all(self.trades.values())
        self.db.commit()
        self._saved = {n: getattr(settings, n) for n in ("execution_mode", "google_client_id")}
        settings.google_client_id = ""
        self.switch_mode("live")

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(settings, name, value)
        self.db.close()

    def switch_mode(self, mode):
        # The way the dashboard toggle does it, so get_risk_settings keeps it.
        services.get_risk_settings(self.db).execution_mode_override = mode
        self.db.commit()
        services.get_risk_settings(self.db)

    def arm(self, take_profit=None, stop_loss=None, mode="live"):
        risk = services.get_risk_settings(self.db)
        risk.close_all_take_profit, risk.close_all_stop_loss, risk.close_all_mode = take_profit, stop_loss, mode
        self.db.commit()


class CloseAllTargetApiTests(CloseAllTargetBase, unittest.TestCase):
    """The target is saved against the current mode and only shown there."""

    def setUp(self):
        super().setUp()
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        super().tearDown()

    def test_set_and_clear(self):
        r = self.client.put("/api/open-positions/close-all-target", json={"take_profit": 50, "stop_loss": None})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["close_all_target"], {"take_profit": 50, "stop_loss": None})

        r = self.client.delete("/api/open-positions/close-all-target")
        self.assertIsNone(r.json()["close_all_target"])

    def test_rejects_empty_and_non_positive(self):
        self.assertEqual(self.client.put("/api/open-positions/close-all-target", json={}).status_code, 422)
        self.assertEqual(self.client.put("/api/open-positions/close-all-target", json={"take_profit": -5}).status_code, 422)

    def test_hidden_in_the_other_mode(self):
        self.client.put("/api/open-positions/close-all-target", json={"take_profit": 50})
        self.switch_mode("demo")
        self.assertIsNone(self.client.get("/api/risk").json()["close_all_target"])


class CloseAllTargetMonitorTests(CloseAllTargetBase, unittest.IsolatedAsyncioTestCase):
    """The monitor sums live profits and flattens the current mode once the total hits the target."""

    def setUp(self):
        super().setUp()
        self.monitor = trailing.TrailingStopMonitor()
        self.deriv = AsyncMock()
        self.deriv.get_contract_status.return_value = {"is_sold": 0}
        self.deriv.close_order.return_value = {"profit": 1.0}
        self._patches = [
            patch.object(trailing, "SessionLocal", self.Session),
            patch.object(services, "DerivClient", return_value=self.deriv),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        super().tearDown()

    def load(self):
        self.monitor._tracked = self.monitor._load_trailed("live")

    async def tick(self, contract_id, profit):
        self.monitor._handle({"proposal_open_contract": {"contract_id": contract_id, "profit": profit, "current_spot": 1.0}})
        await asyncio.gather(*self.monitor._background)

    def statuses(self):
        self.db.expire_all()
        return {o: self.db.get(Trade, t.id).status for o, t in self.trades.items()}

    async def test_take_profit_closes_current_mode_and_clears(self):
        self.arm(take_profit=10)
        self.load()
        self.assertEqual(set(self.monitor._tracked), {"1", "2"})  # no trail/close-at needed

        await self.tick("1", 12)  # one contract alone doesn't decide it
        self.assertEqual(self.statuses()["1"], PositionStatus.open)
        await self.tick("2", -3)  # total 9
        self.assertEqual(self.statuses()["1"], PositionStatus.open)
        await self.tick("2", -1)  # total 11

        self.assertEqual(self.statuses(), {"1": PositionStatus.closed, "2": PositionStatus.closed, "3": PositionStatus.open})
        sources = {s.source for s in self.db.scalars(select(Signal).where(Signal.status == "closed"))}
        self.assertEqual(sources, {"close_all_target"})
        risk = self.db.get(RiskSettings, 1)
        self.db.refresh(risk)
        self.assertIsNone(risk.close_all_take_profit)
        self.assertIsNone(risk.close_all_mode)

    async def test_stop_loss(self):
        self.arm(stop_loss=5)
        self.load()
        await self.tick("1", -2)
        await self.tick("2", -2.5)
        self.assertEqual(self.statuses()["1"], PositionStatus.open)
        await self.tick("2", -3)
        self.assertEqual(self.statuses()["1"], PositionStatus.closed)

    async def test_target_from_other_mode_is_ignored(self):
        self.arm(take_profit=1, mode="demo")
        self.load()
        self.assertEqual(self.monitor._tracked, {})

    async def test_cancelled_before_close_does_nothing(self):
        self.arm(take_profit=1)
        self.load()
        self.arm()  # user cleared it after the monitor loaded it
        await self.tick("1", 5)
        await self.tick("2", 5)
        self.assertEqual(self.statuses()["1"], PositionStatus.open)


if __name__ == "__main__":
    unittest.main()
