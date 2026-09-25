---
name: run-deriv-synthetic-platform
description: Build, run, and visually verify the Deriv Synthetic Trading Platform (FastAPI + server-rendered dashboard). Use when asked to start the app, seed test signals, take a screenshot of the dashboard, or check that a UI/API change actually works end to end.
---

This is a FastAPI server (`app/main.py`) serving a single-page,
hash-routed vanilla-JS dashboard (`app/static/`). Drive it via
`.claude/skills/run-deriv-synthetic-platform/driver.sh` under bash: it
launches the server on a throwaway SQLite DB, fires webhook signals
through the real `/webhook` endpoint to populate data, then screenshots
the dashboard with headless Chrome/Edge (no `chromium-cli`/Playwright
installed in this environment - raw browser CLI flags do the job).

All paths below are relative to the repo root.

## Prerequisites

Python 3.12+ and a project venv with deps installed:

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # .venv/bin/python.exe on Linux/Mac
```

A Chromium-family browser for screenshots - Chrome or Edge on Windows
(checked at `C:\Program Files (x86)\Google\Chrome\...` /
`...\Microsoft\Edge\...`), or `google-chrome`/`chromium` on Linux.
Skip this if you only need the API-level checks (`start` + `seed`).

## Run (agent path)

```bash
.claude/skills/run-deriv-synthetic-platform/driver.sh all
```

This runs all three steps below in sequence. Each is also usable on
its own:

| command | what it does |
|---|---|
| `driver.sh start` | Launches uvicorn on `127.0.0.1:8125` against a fresh `./run-check.db`, `EXECUTION_MODE=demo`, and fake-but-well-formed Deriv credentials (real API calls are attempted and fail at Deriv's end with a 401 - fine for wiring/UI checks; pass real tokens via env if you need actual execution). Blocks until `/health` responds. Logs at `/tmp/run-deriv-uvicorn.log`. |
| `driver.sh seed` | Fires a demo entry+exit, flips to live mode, fires a live entry, flips back to demo - via the real `/webhook` and `/api/risk` endpoints, so Trade History/Signal Journal have both demo- and live-tagged rows to inspect. |
| `driver.sh shots` | Screenshots Overview, Trade History, Signal Journal, Open Positions, and Risk Controls (navigated via URL hash, e.g. `#trading-history` - see `pageIds` in `app.js`) into `/tmp/run-deriv-shots/*.png`. |
| `driver.sh stop` | Kills the server and deletes `./run-check.db`. Always run this when done. |

Override the port with `PORT=8200 driver.sh all`, or point `seed`/`shots`
at an already-running instance by exporting the same `PORT` before
calling them individually.

Example full loop:

```bash
.claude/skills/run-deriv-synthetic-platform/driver.sh start
.claude/skills/run-deriv-synthetic-platform/driver.sh seed
.claude/skills/run-deriv-synthetic-platform/driver.sh shots
# inspect /tmp/run-deriv-shots/*.png
.claude/skills/run-deriv-synthetic-platform/driver.sh stop
```

## Run (human path)

```bash
uvicorn app.main:app --reload
```

Opens on `http://127.0.0.1:8000` against `./trading_platform.db` (the
real dev DB, not a throwaway one) using whatever `.env` has configured.
Ctrl-C to stop. Copy `.env.example` to `.env` first if you don't have one.

## Test

```bash
.venv/Scripts/python.exe -m unittest discover -s tests -v   # .venv/bin/python.exe on Linux/Mac
```

1 test, should pass (`test_deriv_settings_are_present`).

## Gotchas

- **`sqlite3.OperationalError: unable to open database file` on Windows** -
  happens if `DATABASE_URL` is built from an absolute git-bash path like
  `/c/New Folder/...` - sqlite3 doesn't understand that POSIX-style path
  on Windows. Use a relative path (`sqlite:///./run-check.db`), which is
  what the driver does.
- **Invoke the venv's `python`/`uvicorn` by explicit path**
  (`.venv/Scripts/python.exe -m uvicorn ...`), not bare `uvicorn` on
  `PATH` - a fresh shell may not have the venv activated even if your
  current one does.
- **No `chromium-cli`/Playwright in this environment.** The driver shells
  out to a local Chrome/Edge binary directly with
  `--headless=new --disable-gpu --no-sandbox --virtual-time-budget=8000`.
  The `--virtual-time-budget` matters - without it, the screenshot fires
  right after the `load` event, before the dashboard's `refresh()` (which
  does ~9 parallel API calls, one of which - `/api/account-balance` -
  makes a real outbound Deriv API call) has resolved, so you get the
  pre-fetch skeleton state instead of real data.
- **The dashboard is single-page, hash-routed** (`pageIds` in
  `app/static/app.js`) - navigate straight to `BASE/#trading-history`
  etc. instead of trying to script sidebar clicks.
- **`.claude/` is gitignored by default in this repo** except
  `.claude/skills/` (see `.gitignore`) - if you add more skills, they'll
  be tracked; anything else under `.claude/` (settings, caches) stays local.

## Troubleshooting

- **`No .venv found`**: run the venv setup under Prerequisites first.
- **`Server did not come up - see /tmp/run-deriv-uvicorn.log`**: tail
  that log - almost always a port already in use (change `PORT=`) or a
  stale `./run-check.db` locked by a previous unstopped run (`driver.sh
  stop` first, or `rm -f run-check.db`).
- **`No Chrome/Edge found`**: install one, or skip `shots` and use
  `start`/`seed` + `curl` against the API endpoints directly.
