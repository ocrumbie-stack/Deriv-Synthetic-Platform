import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import forex, main
from app.database import Base
from app.schemas import ForexBotCreate, ForexBotUpdate, ForexPairUpdate


def ny(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)


class ForexTestCase(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()

    def tearDown(self):
        self.db.close()


class MarketHoursTests(unittest.TestCase):
    def test_week_runs_sunday_to_friday_5pm_new_york(self):
        self.assertFalse(forex.is_market_open(ny(2026, 11, 1, 16, 59)))  # Sunday
        self.assertTrue(forex.is_market_open(ny(2026, 11, 1, 17)))
        self.assertTrue(forex.is_market_open(ny(2026, 11, 6, 16, 59)))  # Friday
        self.assertFalse(forex.is_market_open(ny(2026, 11, 6, 17)))
        self.assertFalse(forex.is_market_open(ny(2026, 11, 7, 12)))  # Saturday

    def test_london_session_follows_daylight_saving(self):
        day = datetime(2026, 7, 15, tzinfo=timezone.utc)
        summer = forex.session_periods("london", day, day.replace(hour=23))
        self.assertEqual(summer[-1][0].hour, 7)  # 08:00 BST
        day = datetime(2027, 1, 13, tzinfo=timezone.utc)
        winter = forex.session_periods("london", day, day.replace(hour=23))
        self.assertEqual(winter[-1][0].hour, 8)  # 08:00 GMT

    def test_monday_sydney_session_waits_for_the_week_to_open(self):
        # 07:00 Monday in Sydney is 15:00 Sunday in New York this week.
        start, end = ny(2026, 11, 1, 12), ny(2026, 11, 2, 2)
        periods = forex.session_periods("sydney", start, end)
        self.assertEqual(periods[0][0], ny(2026, 11, 1, 17))

    def test_no_sessions_on_the_weekend(self):
        self.assertEqual(forex.session_periods("london", ny(2026, 11, 7, 0), ny(2026, 11, 8, 0)), [])
        self.assertEqual(forex.session_periods("new_york", ny(2026, 11, 7, 0), ny(2026, 11, 8, 0)), [])

    def test_overlapping_sessions_merge_into_one_window(self):
        periods = forex.merged_periods(("london", "new_york"), ny(2026, 11, 4, 0), ny(2026, 11, 4, 23))
        self.assertEqual(periods, [(ny(2026, 11, 4, 3), ny(2026, 11, 4, 17))])


class PairStatusTests(ForexTestCase):
    def test_entry_allowed_inside_the_window(self):
        self.assertIsNone(forex.entry_block_reason(self.db, "EURUSD", ny(2026, 11, 4, 9)))

    def test_entry_blocked_outside_the_window(self):
        reason = forex.entry_block_reason(self.db, "EURUSD", ny(2026, 11, 4, 20))
        self.assertEqual(reason, "EUR/USD closed: outside its London + New York trading window.")

    def test_entry_blocked_on_the_weekend(self):
        self.assertIn("weekend", forex.entry_block_reason(self.db, "AUDUSD", ny(2026, 11, 7, 12)))

    def test_entry_blocked_around_rollover(self):
        self.assertIn("rollover", forex.entry_block_reason(self.db, "AUDUSD", ny(2026, 11, 4, 16, 58)))
        self.assertIn("rollover", forex.entry_block_reason(self.db, "AUDUSD", ny(2026, 11, 4, 17, 4)))

    def test_unknown_pair_is_refused(self):
        self.assertIn("not a configured", forex.entry_block_reason(self.db, "XAUUSD", ny(2026, 11, 4, 9)))

    def test_changed_window_overrides_the_default(self):
        moment = ny(2026, 11, 4, 20)  # Tokyo hours, outside EUR/USD's default window
        forex.set_pair_sessions(self.db, "EURUSD", ["tokyo"])
        self.assertIsNone(forex.entry_block_reason(self.db, "EURUSD", moment))

    def test_next_change_when_closed_is_the_next_open(self):
        status = forex.pair_status("EURUSD", forex.DEFAULT_PAIRS["EURUSD"], ny(2026, 11, 4, 20))
        self.assertEqual(status["next_change"], ny(2026, 11, 5, 3))


class OverviewTests(ForexTestCase):
    def test_overview_shows_every_session_and_pair(self):
        data = forex.overview(self.db, "America/Jamaica", ny(2026, 11, 4, 9))
        self.assertEqual([s["name"] for s in data["sessions"]], ["Sydney", "Tokyo", "London", "New York"])
        self.assertEqual({s["name"] for s in data["sessions"] if s["open"]}, {"London", "New York"})
        self.assertEqual(len(data["pairs"]), len(forex.DEFAULT_PAIRS))
        self.assertTrue(data["overlaps"])
        self.assertTrue(data["market"]["open"])

    def test_bad_display_zone_falls_back_to_utc(self):
        self.assertEqual(forex.overview(self.db, "Not/AZone", ny(2026, 11, 4, 9))["timezone"], "UTC")

    def test_update_route_saves_in_session_order(self):
        out = main.update_forex_pair("EURUSD", ForexPairUpdate(sessions=["new_york", "tokyo"]), self.db)
        self.assertEqual(out["sessions"], ["tokyo", "new_york"])

    def test_update_route_refuses_unknown_pair_and_session(self):
        with self.assertRaises(HTTPException) as pair:
            main.update_forex_pair("XAUUSD", ForexPairUpdate(sessions=["tokyo"]), self.db)
        self.assertEqual(pair.exception.status_code, 404)
        with self.assertRaises(HTTPException) as session:
            main.update_forex_pair("EURUSD", ForexPairUpdate(sessions=["moon"]), self.db)
        self.assertEqual(session.exception.status_code, 422)


class RolloverTests(unittest.TestCase):
    def test_next_rollover(self):
        self.assertEqual(forex.next_rollover(ny(2026, 11, 2, 12)), (ny(2026, 11, 2, 16, 55), ny(2026, 11, 2, 17, 5)))
        self.assertEqual(forex.next_rollover(ny(2026, 11, 2, 17, 3))[0], ny(2026, 11, 2, 16, 55))
        self.assertEqual(forex.next_rollover(ny(2026, 11, 2, 17, 6))[0], ny(2026, 11, 3, 16, 55))


class ForexBotTests(ForexTestCase):
    def test_pip_size(self):
        self.assertEqual(forex.pip_size("EURUSD"), 0.0001)
        self.assertEqual(forex.pip_size("USDJPY"), 0.01)

    def test_pairs_accept_tradingview_spellings(self):
        self.assertEqual(forex.normalize_pairs("eurusd, GBP/USD, FX:EURUSD, usd_jpy"), "EURUSD,GBPUSD,USDJPY")
        self.assertIsNone(forex.normalize_pairs(" "))
        with self.assertRaises(ValueError):
            forex.normalize_pairs("XAUUSD")

    def test_bot_crud(self):
        bot = main.create_forex_bot(ForexBotCreate(name="London Breakout", lots=0.1, stop_loss_pips=15, symbols="eurusd"), self.db)
        self.assertEqual((bot["units"], bot["symbols"]), (10000, ["EURUSD"]))
        with self.assertRaises(HTTPException) as dup:
            main.create_forex_bot(ForexBotCreate(name="London Breakout"), self.db)
        self.assertEqual(dup.exception.status_code, 409)
        bot = main.update_forex_bot(bot["id"], ForexBotUpdate(stop_loss_pips=None, enabled=False), self.db)
        self.assertEqual((bot["stop_loss_pips"], bot["enabled"]), (None, False))
        main.delete_forex_bot(bot["id"], self.db)
        self.assertEqual(main.list_forex_bots(self.db), [])


class OandaRenameTests(ForexTestCase):
    def test_stored_oanda_names_become_ctrader_names(self):
        from app.models import ForexBot, ForexPair

        self.db.add_all([ForexPair(symbol="EUR_USD", sessions="tokyo"), ForexBot(name="Bot", symbols="EUR_USD,USD_JPY")])
        self.db.commit()
        forex.rename_oanda_pairs(self.db)
        self.db.commit()
        self.assertEqual(self.db.query(ForexPair).one().symbol, "EURUSD")
        self.assertEqual(forex.get_bot(self.db, "Bot").symbols, "EURUSD,USDJPY")
        self.assertEqual(forex.pair_sessions(self.db)["EURUSD"], ("tokyo",))


if __name__ == "__main__":
    unittest.main()
