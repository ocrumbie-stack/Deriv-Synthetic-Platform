# Deriv Synthetic Trading Platform

Railway-ready MVP for receiving TradingView strategy webhooks, validating signals, logging strategy-linked trades, and monitoring strategy performance before optionally executing on Deriv synthetic markets.

## What is included

- FastAPI webhook endpoint at `POST /webhook`
- Strategy authorization through enable/disable state and shared webhook secret
- Duplicate signal blocking with `signal_id`
- Open-position checks per strategy and symbol
- Strategy-level maximum position size and daily loss guardrails
- Paper execution by default, with a gated Deriv live execution client
- Signal journal, open positions, trade history, and strategy performance dashboard
- Period views for today, this week, this month, and all time
- Railway deployment files: `Procfile` and `railway.json`

## Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`.

## TradingView webhook payload

Set TradingView's webhook URL to:

```text
https://web-production-a0f10.up.railway.app/webhook
```

Example entry:

```json
{
  "secret": "change-me",
  "strategy": "Synthetic Momentum",
  "symbol": "R_100",
  "action": "entry",
  "direction": "long",
  "price": 65000,
  "size": 1,
  "signal_id": "{{strategy.order.id}}-{{time}}"
}
```

Example exit:

```json
{
  "secret": "change-me",
  "strategy": "Synthetic Momentum",
  "symbol": "R_100",
  "action": "exit",
  "price": 66250,
  "signal_id": "exit-{{time}}"
}
```

## Configuration

Copy `.env.example` to `.env` locally or set these variables in Railway.

- `WEBHOOK_SECRET`: shared secret TradingView must send with every signal.
- `EXECUTION_MODE`: `paper` or `live`. Keep this as `paper` until the dashboard and rules are verified.
- `EMERGENCY_STOP`: set to `true` to reject all incoming signals.
- `DATABASE_URL`: defaults to SQLite. On Railway, point this at a managed Postgres database when ready.
- `DERIV_APP_ID`, `DERIV_API_TOKEN`: required only for `EXECUTION_MODE=live`.
- `DERIV_ACCOUNT_ID`: required in live mode. Set this to the TradingView account login ID so balance reads and all trades target that account.
- `DERIV_WS_URL`: optional override for the Deriv WebSocket endpoint.

## API

- `GET /api/summary`
- `GET /api/strategies`
- `PATCH /api/strategies/{strategy_id}`
- `GET /api/signals`
- `GET /api/open-positions`
- `GET /api/trade-history`
- `GET /api/performance?period=today|week|month|all`

## Live trading note

Live exchange execution is deliberately behind `EXECUTION_MODE=live` and requires valid Deriv credentials. Before enabling it, test webhook formatting, strategy limits, duplicate handling, entry/exit lifecycle, and Railway database persistence in paper mode.
