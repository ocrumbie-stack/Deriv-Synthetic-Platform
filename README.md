# Deriv Synthetic Trading Platform

Clean, isolated Railway app for receiving TradingView strategy webhooks, validating signals, logging strategy-linked trades, and monitoring strategy performance before optionally executing on Deriv synthetic markets.

## Project purpose

- FastAPI webhook endpoint at `POST /webhook`
- Strategy authorization through enable/disable state and shared webhook secret
- Duplicate signal blocking with `signal_id`
- Open-position checks per strategy and symbol
- Strategy-level maximum position size and daily loss guardrails
- Paper execution by default, with an optional live Deriv execution client
- Signal journal, open positions, trade history, and strategy dashboard
- Railway deployment setup via `Procfile` and `railway.json`

## Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`.

## Required environment variables

Copy `.env.example` to `.env` locally or set these in Railway.

- `WEBHOOK_SECRET`: shared secret TradingView must send with every signal
- `EXECUTION_MODE`: `paper` or `live`
- `EMERGENCY_STOP`: `true` or `false`
- `DATABASE_URL`: defaults to SQLite; use PostgreSQL in Railway when ready
- `DERIV_APP_ID`, `DERIV_API_TOKEN`: required only for live execution
- `DERIV_API_URL`: optional override for the Deriv API endpoint

## Deployment note

This project is intentionally kept separate from any crypto or legacy backup folders. It is meant to be a clean Deriv synthetic deployment project.
