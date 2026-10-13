"""Telegram alerts for trading events.

Off until TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are both set. Each event
type can be switched off on the Settings page. Sending never blocks or breaks
trading: messages go out on a background task and failures are only logged.
"""

import asyncio
import logging
import time

import httpx

from app.config import settings

logger = logging.getLogger("uvicorn.error")

# Event key -> label shown on the Settings page.
EVENTS = {
    "trade_opened": "Trade opened",
    "trade_closed": "Trade closed",
    "order_failed": "Order failed",
    "signal_rejected": "Signal rejected",
    "close_all_target": "Close-all target hit",
    "emergency_stop": "Emergency stop changed",
}

# Applied from AppSettings by get_app_settings, like the other Settings-page
# values, so callers don't need a db session to check it.
enabled_events: set[str] = set(EVENTS)

CLOSE_REASONS = {
    "strategy_exit": "strategy exit",
    "reversal": "reversal",
    "webhook_exit": "exit webhook",
    "deriv_close": "closed on Deriv (TP/SL or stop-out)",
    "manual_close": "closed from the dashboard",
    "close_all_target": "close-all target",
    "price_exit": "close-at price",
    "trailing_stop": "trailing stop",
}

# A close that keeps failing (e.g. the trailing stop retrying every tick)
# would otherwise send the same message over and over.
REPEAT_WINDOW_SECONDS = 300
_recent: dict[str, float] = {}
# Anyone can POST to /webhook, and rejections are alerted on - cap the rate
# so a flood of forged signals can't flood the chat too.
MAX_PER_MINUTE = 20
_sent_times: list[float] = []
# Telegram's limit is 4096 characters per message.
MAX_LENGTH = 4000
_background: set[asyncio.Task] = set()
# The app's event loop, so sync endpoints (run in worker threads) can send too.
_loop: asyncio.AbstractEventLoop | None = None


def start() -> None:
    """Call from the app's lifespan, on the event loop."""
    global _loop
    _loop = asyncio.get_running_loop()


def configured() -> bool:
    return bool(settings.telegram_bot_token.strip() and settings.telegram_chat_id.strip())


def mode_tag(mode: str | None) -> str:
    return "[LIVE]" if (mode or "").lower() == "live" else "[DEMO]"


def money(value: float | None) -> str:
    value = value or 0.0
    return f"{'+' if value >= 0 else '-'}${abs(value):.2f}"


async def send_message(text: str) -> None:
    """Send now. Raises RuntimeError with Telegram's reason on failure - the
    error never includes the bot token, which is part of the request URL."""
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token.strip()}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, json={"chat_id": settings.telegram_chat_id.strip(), "text": text})
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Couldn't reach Telegram ({type(exc).__name__}).") from None
    if response.status_code != 200:
        try:
            reason = response.json().get("description") or response.text
        except ValueError:
            reason = response.text
        raise RuntimeError(f"Telegram refused the message: {reason}")


async def _send_logged(text: str) -> None:
    try:
        await send_message(text)
    except RuntimeError as exc:
        logger.warning("Telegram alert not sent: %s", exc)


def notify(event: str, text: str) -> None:
    """Queue an alert if Telegram is set up and `event` is switched on."""
    if event not in enabled_events or not configured():
        return
    now = time.monotonic()
    for key, sent_at in list(_recent.items()):
        if now - sent_at > REPEAT_WINDOW_SECONDS:
            del _recent[key]
    if text in _recent:
        return
    _sent_times[:] = [t for t in _sent_times if now - t < 60]
    if len(_sent_times) >= MAX_PER_MINUTE:
        logger.warning("Telegram alert dropped - over %s a minute: %s", MAX_PER_MINUTE, text.splitlines()[0])
        return
    _sent_times.append(now)
    _recent[text] = now
    text = text[:MAX_LENGTH]
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        task = loop.create_task(_send_logged(text))
        _background.add(task)
        task.add_done_callback(_background.discard)
    elif _loop is not None and _loop.is_running():
        asyncio.run_coroutine_threadsafe(_send_logged(text), _loop)


# ─── Messages ────────────────────────────────────────────────────────────────
# `trade` is an app.models.Trade and `signal` an app.models.Signal.


def trade_opened(trade) -> None:
    notify("trade_opened", (
        f"🟢 {mode_tag(trade.execution_mode)} Opened {trade.direction.value.upper()} {trade.symbol}\n"
        f"Bot: {trade.strategy_name} · Stake ${trade.size:.2f} · {trade.leverage:g}x"
    ))


def trade_closed(trade) -> None:
    net = trade.net_result or 0.0
    reason = CLOSE_REASONS.get(trade.close_reason or "", (trade.close_reason or "closed").replace("_", " "))
    notify("trade_closed", (
        f"{'✅' if net >= 0 else '🔻'} {mode_tag(trade.execution_mode)} Closed {trade.direction.value.upper()} {trade.symbol}\n"
        f"Bot: {trade.strategy_name} · P/L {money(net)} · {reason}"
    ))


def close_all_hit(mode: str, total: float, closed: int, failed: list[dict]) -> None:
    line = f"🎯 {mode_tag(mode)} Close-all target hit at {money(total)}\nClosed {closed} position{'' if closed == 1 else 's'}"
    if failed:
        line += f", {len(failed)} failed: " + ", ".join(f["symbol"] for f in failed)
    notify("close_all_target", line)


def emergency_stop(on: bool) -> None:
    notify("emergency_stop", "🛑 Emergency stop ON - new entries are blocked" if on else "▶️ Emergency stop OFF - entries allowed again")


def order_failed(mode: str | None, what: str, error) -> None:
    notify("order_failed", f"⚠️ {mode_tag(mode)} {what} failed\n{error}")


def signal_rejected(signal) -> None:
    action = signal.action.value
    direction = f" {signal.direction.value}" if signal.direction else ""
    notify("signal_rejected", (
        f"⛔ {mode_tag(signal.execution_mode)} Rejected {action}{direction} {signal.symbol}\n"
        f"Bot: {signal.strategy_name} · {signal.rejection_reason}"
    ))
