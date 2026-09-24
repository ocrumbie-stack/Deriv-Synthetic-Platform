# Deriv Synthetic Trading Platform

Railway-ready MVP for receiving TradingView strategy webhooks, validating signals, logging strategy-linked trades, and monitoring strategy performance before optionally executing on Deriv synthetic markets.

## What is included

- FastAPI webhook endpoint at `POST /webhook`
- Strategy authorization through enable/disable state and shared webhook secret
- Duplicate signal blocking with `signal_id`
- Open-position checks per strategy and symbol
- Strategy-level maximum position size and daily loss guardrails
- Demo execution by default (real Deriv API calls against a virtual/demo account), with a dashboard toggle to switch to live (real money)
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
- `EXECUTION_MODE`: `demo` or `live`. Both make real Deriv API calls (real order execution, real balance reads) — `demo` targets your Deriv virtual account, `live` targets your real-money account. Anything other than exactly `live` (including no value at all) is treated as `demo`, so a missing/misconfigured value can never accidentally route to real money. This can also be switched from the dashboard's Risk Controls page instead of editing `.env` — see "Execution mode toggle" below.
- `EMERGENCY_STOP`: set to `true` to reject all incoming signals.
- `DATABASE_URL`: defaults to SQLite. On Railway, point this at a managed Postgres database when ready.
- `DERIV_APP_ID`: shared app ID, used for both demo and live.
- `DERIV_API_TOKEN`, `DERIV_ACCOUNT_ID`: your real account's API token and login ID. Required for `EXECUTION_MODE=live`.
- `DERIV_DEMO_API_TOKEN`, `DERIV_DEMO_ACCOUNT_ID`: your demo/virtual account's API token and login ID, from `app.deriv.com/account/api-token` while logged into the demo account. Required for `EXECUTION_MODE=demo`.
- `DERIV_WS_URL`: optional override for the Deriv WebSocket endpoint.

## Execution mode toggle

The Risk Controls page has a Demo/Live switch so you don't have to edit `.env` and redeploy every time you want to move between accounts. It's backed by the database (persists across restarts) and overrides `EXECUTION_MODE` at runtime. Switching to a mode that's missing its Deriv credentials is rejected with a clear error naming what's missing. Switching to live also asks for confirmation in the UI, since it sends real orders with real money.

## Persisting data on Railway

Railway's filesystem is ephemeral, so the SQLite database is wiped on every
redeploy unless a volume is attached. To make signal bots, strategies, and
trade history survive deploys:

1. In the Railway dashboard, open this service and add a Volume (Command
   Palette or right-click the service on the canvas).
2. Set its mount path to `/data`.

That's it — the app detects Railway's `RAILWAY_VOLUME_MOUNT_PATH` automatically
and stores the SQLite file there. No environment variables need to be set by
hand. This has to be done once through the dashboard; Railway does not support
declaring volumes in `railway.json`.

## API

- `GET /api/summary`
- `GET /api/strategies`
- `PATCH /api/strategies/{strategy_id}`
- `GET /api/signals`
- `GET /api/open-positions`
- `GET /api/trade-history`
- `GET /api/performance?period=today|week|month|all`

## Live trading note

Real-money execution is deliberately behind `EXECUTION_MODE=live` and requires valid real-account Deriv credentials. Before enabling it, test webhook formatting, strategy limits, duplicate handling, entry/exit lifecycle, and Railway database persistence in demo mode against your Deriv virtual account.
