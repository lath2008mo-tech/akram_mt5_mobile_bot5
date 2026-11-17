# MT5 Mobile Bot Backend

Remote control for the supplied `mt5_scalper_bot.py` from your phone.
A small FastAPI service runs next to MetaTrader 5 on Windows, serves a
mobile web app, and shares start/stop/emergency state with the trading engine.

**Svensk guide: [MOBIL.md](MOBIL.md)** — steg för steg från tom Windows-dator till
boten i mobilen.

## Architecture

```
Phone browser ──HTTP──► FastAPI (main.py) ──► SQLite (bot_state.db) ◄── mt5_scalper_bot.py
                              │                                              │
                              └────────────► MetaTrader5 Python ◄───────────┘
                                                (Windows, logged in)
```

* `main.py` – REST API + mobile web app (`web/index.html`)
* `control.py` – shared flags/settings/audit log in SQLite
* `mt5_scalper_bot.py` – trading engine; refuses to trade when stopped from the phone
* `run_server.bat` / `run_bot.bat` – Windows launchers

## Run (Windows with MT5 installed)

1. Install Python 3.11+ and MetaTrader 5 desktop, log into a **DEMO** account.
2. Double-click `run_server.bat`, then `run_bot.bat` (separate windows).
3. Copy `.env.example` to `.env` if you want to change defaults; the API token is
   generated into `.api_token` on first start.
4. Open `http://<pc-ip>:8000` on the phone, paste the token, "Add to Home Screen".

Manual start:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Linux/macOS (no MT5 available) still boots for UI/API development:
`uvicorn main:app --host 0.0.0.0 --port 8000` — MT5 fields simply return `null`.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | Public |
| GET | `/status` | Balance, equity, P/L, mode |
| GET | `/positions` | Open positions |
| POST | `/positions/{ticket}/close` | Close one position |
| POST | `/positions/close-all` | `{"confirm":"CLOSE ALL"}` |
| POST | `/bot/start` `/bot/stop` `/bot/emergency-stop` | Engine control |
| GET/PUT | `/settings` | Persisted in SQLite |
| GET | `/signals` | Indicator snapshot per symbol |
| GET | `/history` | Audit log |
| POST | `/paper-trade` | Simulated order |

Auth: `X-API-Token` header, `Authorization: Bearer <token>`, or `?token=<token>`.
Swagger UI at `/docs`.

## Before going live

* Keep `DRY_RUN=true` until the whole loop is verified.
* Never expose port 8000 without TLS — use Tailscale, Cloudflare Tunnel or a reverse proxy.
* Add persistent daily-loss tracking (currently resets on restart), rotating tokens,
  and a hard kill switch at the broker level.
* No strategy guarantees profit; slippage, spread and outages cause losses.
