"""Forex sessions and per-pair trading windows.

Every time here is a timezone-aware datetime, and each session is defined in
its own city's local hours so daylight saving moves it the way the real market
moves. entry_block_reason is the rule the cTrader forex order path will call - nothing
here touches Deriv, so forex tickers sent to Deriv are not affected.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ForexBot, ForexPair

NEW_YORK = ZoneInfo("America/New_York")
# The trading week runs Sunday 17:00 to Friday 17:00 New York time, and the
# same 17:00 is the daily rollover, when spreads widen.
WEEK_BOUNDARY = time(17)
ROLLOVER_MARGIN = timedelta(minutes=5)

Period = tuple[datetime, datetime]


@dataclass(frozen=True)
class FxSession:
    name: str
    zone: str
    opens: int
    closes: int


SESSIONS = {
    "sydney": FxSession("Sydney", "Australia/Sydney", 7, 16),
    "tokyo": FxSession("Tokyo", "Asia/Tokyo", 9, 18),
    "london": FxSession("London", "Europe/London", 8, 17),
    "new_york": FxSession("New York", "America/New_York", 8, 17),
}

# Pair (cTrader's name, e.g. EURUSD) -> the sessions it trades in by default.
DEFAULT_PAIRS = {
    "EURUSD": ("london", "new_york"),
    "GBPUSD": ("london", "new_york"),
    "EURGBP": ("london", "new_york"),
    "USDCHF": ("london", "new_york"),
    "USDJPY": ("tokyo", "london", "new_york"),
    "EURJPY": ("tokyo", "london"),
    "GBPJPY": ("tokyo", "london"),
    "AUDUSD": ("sydney", "tokyo"),
    "NZDUSD": ("sydney", "tokyo"),
    "USDCAD": ("new_york",),
}


LOT_UNITS = 100_000


def pip_size(symbol: str) -> float:
    """A pip is the 4th decimal place, or the 2nd for yen pairs."""
    return 0.01 if symbol.endswith("JPY") else 0.0001


def pair_symbol(value: str) -> str:
    """EURUSD, EUR/USD, eur_usd or FX:EURUSD as the pair name EURUSD."""
    symbol = "".join(ch for ch in value.split(":")[-1].upper() if ch.isalpha())
    if len(symbol) != 6 or symbol not in DEFAULT_PAIRS:
        raise ValueError(f"{value.strip()} isn't one of the forex pairs on the Overview page.")
    return symbol


def normalize_pairs(value: str | None) -> str | None:
    """A typed allow-list ("eurusd, GBP/USD") as stored pair names, or None for all."""
    names = [pair_symbol(part) for part in (value or "").split(",") if part.strip()]
    return ",".join(dict.fromkeys(names)) or None


def get_bot(db: Session, name: str) -> ForexBot | None:
    return db.scalar(select(ForexBot).where(ForexBot.name == name))


def bot_out(bot: ForexBot) -> dict:
    return {
        "id": bot.id,
        "name": bot.name,
        "symbols": bot.symbols.split(",") if bot.symbols else [],
        "lots": bot.lots,
        "units": round(bot.lots * LOT_UNITS),
        "stop_loss_pips": bot.stop_loss_pips,
        "take_profit_pips": bot.take_profit_pips,
        "trailing_stop_pips": bot.trailing_stop_pips,
        "enabled": bot.enabled,
        "created_at": bot.created_at.isoformat(),
    }


def list_bots(db: Session) -> list[dict]:
    return [bot_out(bot) for bot in db.scalars(select(ForexBot).order_by(ForexBot.name))]


def session_periods(key: str, start: datetime, end: datetime) -> list[Period]:
    """A session's open periods overlapping start-end, in UTC. Monday's Sydney
    session starts before the week does, so each is cut to the trading week."""
    session = SESSIONS[key]
    zone = ZoneInfo(session.zone)
    day = start.astimezone(zone).date() - timedelta(days=1)
    last_day = end.astimezone(zone).date()
    periods = []
    while day <= last_day:
        if day.weekday() < 5:
            monday = day - timedelta(days=day.weekday())
            week_open = datetime.combine(monday - timedelta(days=1), WEEK_BOUNDARY, NEW_YORK)
            week_close = datetime.combine(monday + timedelta(days=4), WEEK_BOUNDARY, NEW_YORK)
            opens = max(datetime.combine(day, time(session.opens), zone), week_open)
            closes = min(datetime.combine(day, time(session.closes), zone), week_close)
            if opens < closes and opens < end and closes > start:
                periods.append((opens.astimezone(timezone.utc), closes.astimezone(timezone.utc)))
        day += timedelta(days=1)
    return periods


def merged_periods(keys, start: datetime, end: datetime) -> list[Period]:
    """The combined open periods of several sessions, joined where they touch."""
    periods = sorted(period for key in keys for period in session_periods(key, start, end))
    merged: list[Period] = []
    for opens, closes in periods:
        if merged and opens <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], closes))
        else:
            merged.append((opens, closes))
    return merged


def overlap_periods(first: list[Period], second: list[Period]) -> list[Period]:
    overlaps = []
    for a_open, a_close in first:
        for b_open, b_close in second:
            opens, closes = max(a_open, b_open), min(a_close, b_close)
            if opens < closes:
                overlaps.append((opens, closes))
    return overlaps


def is_market_open(now: datetime) -> bool:
    local = now.astimezone(NEW_YORK)
    weekday = local.weekday()
    if weekday == 5:
        return False
    if weekday == 6:
        return local.time() >= WEEK_BOUNDARY
    if weekday == 4:
        return local.time() < WEEK_BOUNDARY
    return True


def rollover_end(now: datetime) -> datetime | None:
    """When the current rollover pause ends, or None outside one."""
    local = now.astimezone(NEW_YORK)
    rollover = datetime.combine(local.date(), WEEK_BOUNDARY, NEW_YORK).astimezone(timezone.utc)
    if rollover - ROLLOVER_MARGIN <= now < rollover + ROLLOVER_MARGIN:
        return rollover + ROLLOVER_MARGIN
    return None


def next_rollover(now: datetime) -> Period:
    """The current or next rollover pause, in UTC."""
    day = now.astimezone(NEW_YORK).date()
    while True:
        rollover = datetime.combine(day, WEEK_BOUNDARY, NEW_YORK).astimezone(timezone.utc)
        if rollover + ROLLOVER_MARGIN > now:
            return rollover - ROLLOVER_MARGIN, rollover + ROLLOVER_MARGIN
        day += timedelta(days=1)


def next_open(now: datetime) -> datetime:
    """The next Sunday 17:00 New York at or after now."""
    day = now.astimezone(NEW_YORK).date()
    while True:
        candidate = datetime.combine(day, WEEK_BOUNDARY, NEW_YORK).astimezone(timezone.utc)
        if day.weekday() == 6 and candidate >= now:
            return candidate
        day += timedelta(days=1)


def next_close(now: datetime) -> datetime:
    """The next Friday 17:00 New York after now."""
    day: date = now.astimezone(NEW_YORK).date()
    while True:
        candidate = datetime.combine(day, WEEK_BOUNDARY, NEW_YORK).astimezone(timezone.utc)
        if day.weekday() == 4 and candidate > now:
            return candidate
        day += timedelta(days=1)


def window_label(keys) -> str:
    return " + ".join(SESSIONS[key].name for key in keys)


def pair_label(symbol: str) -> str:
    return f"{symbol[:3]}/{symbol[3:]}"


def pair_status(symbol: str, keys, now: datetime) -> dict:
    """Whether a pair may enter right now, why not, and when that next changes."""
    label = pair_label(symbol)
    periods = merged_periods(keys, now - timedelta(days=1), now + timedelta(days=8))
    following = next((opens for opens, _ in periods if opens > now), None)
    if not is_market_open(now):
        reason = f"{label} closed: the forex market is closed for the weekend."
        return {"open": False, "reason": reason, "next_change": following}
    pause_end = rollover_end(now)
    if pause_end:
        reason = f"{label} closed: paused around the 5pm New York rollover."
        return {"open": False, "reason": reason, "next_change": pause_end}
    current = next((closes for opens, closes in periods if opens <= now < closes), None)
    if current:
        return {"open": True, "reason": None, "next_change": current}
    reason = f"{label} closed: outside its {window_label(keys)} trading window."
    return {"open": False, "reason": reason, "next_change": following}


def pair_sessions(db: Session) -> dict[str, tuple[str, ...]]:
    """Each pair's sessions: the default unless changed on the Forex page."""
    pairs = dict(DEFAULT_PAIRS)
    for row in db.scalars(select(ForexPair)):
        if row.symbol in pairs:
            pairs[row.symbol] = tuple(key for key in SESSIONS if key in row.sessions.split(","))
    return pairs


def entry_block_reason(db: Session, symbol: str, now: datetime | None = None) -> str | None:
    """Why a new entry on this pair must be refused, or None if it may go ahead.
    Exits never come through here, so a position can always be closed."""
    keys = pair_sessions(db).get(symbol)
    if not keys:
        return f"{symbol} is not a configured forex pair."
    return pair_status(symbol, keys, now or datetime.now(timezone.utc))["reason"]


def set_pair_sessions(db: Session, symbol: str, keys: list[str]) -> None:
    row = db.scalar(select(ForexPair).where(ForexPair.symbol == symbol))
    stored = ",".join(key for key in SESSIONS if key in keys)
    if row:
        row.sessions = stored
    else:
        db.add(ForexPair(symbol=symbol, sessions=stored))
    db.commit()


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def _periods_out(periods: list[Period]) -> list[dict]:
    return [{"start": _iso(opens), "end": _iso(closes)} for opens, closes in periods]


def overview(db: Session, zone_name: str, now: datetime | None = None) -> dict:
    """Everything the Forex page draws: today's sessions in the display time
    zone, plus each pair's window and status."""
    now = now or datetime.now(timezone.utc)
    try:
        zone = ZoneInfo(zone_name)
    except Exception:
        zone_name, zone = "UTC", ZoneInfo("UTC")
    today = now.astimezone(zone).date()
    day_start = datetime.combine(today, time(0), zone).astimezone(timezone.utc)
    day_end = datetime.combine(today + timedelta(days=1), time(0), zone).astimezone(timezone.utc)

    sessions = []
    for key, session in SESSIONS.items():
        upcoming = session_periods(key, now - timedelta(days=1), now + timedelta(days=8))
        opened, current = next(((opens, closes) for opens, closes in upcoming if opens <= now < closes), (None, None))
        sessions.append({
            "key": key,
            "name": session.name,
            "zone": session.zone,
            "open": current is not None,
            "opened_at": _iso(opened),
            "next_change": _iso(current or next((opens for opens, _ in upcoming if opens > now), None)),
            "periods": _periods_out(session_periods(key, day_start, day_end)),
        })

    overlaps = overlap_periods(session_periods("london", day_start, day_end), session_periods("new_york", day_start, day_end))
    market_open = is_market_open(now)
    pairs = []
    for symbol, keys in pair_sessions(db).items():
        status = pair_status(symbol, keys, now)
        pairs.append({
            "symbol": symbol,
            "label": pair_label(symbol),
            "pip": pip_size(symbol),
            "sessions": list(keys),
            "open": status["open"],
            "reason": status["reason"],
            "next_change": _iso(status["next_change"]),
        })
    return {
        "now": _iso(now),
        "timezone": zone_name,
        "day_start": _iso(day_start),
        "day_end": _iso(day_end),
        "market": {"open": market_open, "next_change": _iso(next_close(now) if market_open else next_open(now))},
        "rollover": _periods_out([next_rollover(now)])[0],
        "sessions": sessions,
        "overlaps": _periods_out(overlaps),
        "pairs": pairs,
    }


def rename_oanda_pairs(db: Session) -> None:
    """One-off: pairs used to be stored by OANDA name (EUR_USD); they are now
    the bare name cTrader brokers use (EURUSD)."""
    for row in db.scalars(select(ForexPair).where(ForexPair.symbol.contains("_"))):
        row.symbol = row.symbol.replace("_", "")
    for bot in db.scalars(select(ForexBot).where(ForexBot.symbols.contains("_"))):
        bot.symbols = bot.symbols.replace("_", "")
