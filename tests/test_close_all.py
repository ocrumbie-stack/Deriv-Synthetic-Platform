import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import services
from app.config import settings
from app.database import Base, get_db
from app.deriv import DerivExecutionError
from app.main import app
from app.models import PositionStatus, Signal, Trade


class CloseAllTests(unittest.TestCase):
    """Close all flattens the current mode only, and one failure doesn't stop the rest."""

    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
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
        app.dependency_overrides[get_db] = lambda: self.db
        self._saved = {n: getattr(settings, n) for n in ("execution_mode", "google_client_id")}
        settings.execution_mode, settings.google_client_id = "live", ""

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        for name, value in self._saved.items():
            setattr(settings, name, value)
        self.db.close()

    def test_closes_current_mode_and_reports_failures(self):
        client = AsyncMock()
        client.get_contract_status.return_value = {"is_sold": 0}

        async def close_order(contract_id):
            if contract_id == "2":
                raise DerivExecutionError("market closed")
            return {"profit": 1.0}

        client.close_order.side_effect = close_order
        with patch.object(services, "DerivClient", return_value=client):
            r = TestClient(app).post("/api/open-positions/close-all")

        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["closed"], 1)
        self.assertEqual([f["symbol"] for f in body["failed"]], ["VOLATILITY_10_INDEX"])
        for order_id in self.trades:
            self.db.refresh(self.trades[order_id])
        self.assertEqual(self.trades["1"].status, PositionStatus.closed)
        self.assertEqual(self.trades["2"].status, PositionStatus.open)
        self.assertEqual(self.trades["3"].status, PositionStatus.open)
        client.close_order.assert_any_call("1")
        self.assertNotIn("3", [c.args[0] for c in client.close_order.call_args_list])
        sources = [s.source for s in self.db.scalars(select(Signal).where(Signal.status == "closed"))]
        self.assertEqual(sources, ["manual_close"])


if __name__ == "__main__":
    unittest.main()
