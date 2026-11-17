# MT5 Mobile Bot

Trading engine + phone remote control for MetaTrader 5, in one repo.

A FastAPI service runs next to MetaTrader 5 on Windows, serves a mobile web app,
and shares start/stop/emergency state with the trading engine. No APK, no app
store, no build step — the phone just opens a URL.

* **Svensk steg-för-steg-guide:** [MOBIL.md](MOBIL.md)
* **Strategi, risk och källor:** [RESEARCH.md](RESEARCH.md)

## Architecture

```
Phone browser ──HTTP──► FastAPI (main.py) ──► SQLite (bot_state.db) ◄── mt5_scalper_bot.py
                              │                                              │
                              └────────────► MetaTrader5 Python ◄───────────┘
                                                (Windows, logged in)
```

| File | Role |
|---|---|
| `main.py` | REST API + serves the mobile UI |
| `web/index.html` | the phone app (single file, PWA manifest) |
| `control.py` | shared state: start/stop flags, settings, audit log |
| `strategy.py` | indicators, regime detection, setup scoring, R maths (pure pandas) |
| `execution.py` | live management: break-even, partials, trailing, circuit breaker |
| `backtest.py` | event-driven backtester + metrics |
| `mt5_scalper_bot.py` | trading engine; refuses to trade when stopped from the phone |

## Strategy and risk

Full detail in [RESEARCH.md](RESEARCH.md). Short version:

* four setups (trend continuation, breakout, pullback, range reversion) behind
  one scoring function — a trade needs `MIN_SCORE` agreeing filters
* ADX regime gate: trend setups in a trend, reversion in a range, nothing in between
* session, spread-in-ATR, higher-timeframe and correlation filters
* risk in R: partial profit at +1R, stop to break-even at +1R, ATR trail after 1.2R
* persisted daily loss limit and a circuit breaker after 3 losses in a row
* backtest with the *same* decision code the bot trades

```bash
python backtest.py --symbol EURUSD --tf 5 --bars 50000   # Windows + MT5
python backtest.py --csv data.csv                        # exported history
python backtest.py --synthetic                           # smoke test
python -m unittest discover -s tests -v                  # 18 tests
```

No strategy can promise profit. Judge a setting by expectancy in R, profit
factor and drawdown — and keep `DRY_RUN=true` until it holds up out of sample.

## Fastest start on Windows: `start_bot.bat`

Double-click it. It creates the virtualenv, asks for the bot token **once**,
finds the chat id itself, saves `.env`, sends a test message and starts the
scanner. `check_signals.bat` shows the current state without starting anything.

```bash
python setup_wizard.py --check              # am I configured?
python setup_wizard.py --token 123:AAF…     # skip the question
python setup_wizard.py --token … --chat-id 42 --no-start
```

## Connect Telegram from the phone (no file editing)

The app can save the token for you and figure out the chat id itself:

1. In Telegram: open your bot, send `hej`.
2. In the app: **Inställningar → Telegram-signaler**, paste the token from
   @BotFather, leave *Chat id* empty, tap **Anslut Telegram**.
3. The server calls `getUpdates`, finds your chat, writes `.env` and sends a
   test message. Done — no restart, no Notepad.

Under **Scanner** there is also **Starta automatisk skanning** / **Stoppa**, which
runs `scanner.py --loop …` on the machine that has MetaTrader 5.

| Endpoint | Purpose |
|---|---|
| `GET /notify/status` | is Telegram/SMS configured, masked token, chat id |
| `POST /notify/configure` | `{"token": "...", "chat_id": null}` → detect, save, test |
| `GET /scanner/status` | running, pid, channels, log tail |
| `POST /scanner/start` | `{"loop_seconds":300,"min_score":6,"timeframes":"5,15,60"}` |
| `POST /scanner/stop` | stop the background scanner |

## Market scanner + Telegram/SMS alerts

`scanner.py` walks every symbol in Market Watch, scores the setups on M5/M15/H1
with the *same* decision code as the bot, and pushes the best ones with entry,
SL, TP and R:R.

```bash
python scanner.py --once                     # one pass, table in the console
python scanner.py --loop 300 --notify       # every 5 min, push new setups
python scanner.py --symbols EURUSD,XAUUSD --min-score 6 --once --notify
```

In the phone app: **Scanner** tab → *Skanna marknaden*, and *Testa notiser*.

Channels (`.env`, all optional):

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | free, instant, recommended |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM`, `SMS_TO` | SMS, costs per message |
| `NOTIFY_CHANNELS` | `telegram,sms` to pick; empty = everything configured |
| `SCAN_MIN_SCORE`, `SCAN_TIMEFRAMES`, `SCAN_MAX_SYMBOLS` | what counts as an A-setup |

Each setup is announced once per symbol/timeframe/direction/candle — no spam
every polling loop. Nothing is executed: these are signals, you decide.

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

```bash
./serve.sh          # recreates .venv and installs deps if needed, then serves :8000
```

MT5 fields simply return `null` there, so the UI and API stay testable without
Windows. On Windows keep using `run_server.bat`.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | Public |
| GET | `/ping` | Plain-text "OK" – use it to check that the tunnel works |
| GET | `/status` | Balance, equity, P/L, mode |
| GET | `/positions` | Open positions |
| POST | `/positions/{ticket}/close` | Close one position |
| POST | `/positions/close-all` | `{"confirm":"CLOSE ALL"}` |
| POST | `/bot/start` `/bot/stop` `/bot/emergency-stop` | Engine control |
| GET/PUT | `/settings` | Persisted in SQLite |
| GET | `/signals` | Indicator + regime snapshot per symbol |
| GET | `/scan` | Market scanner: entry/SL/TP per setup (`min_score`, `timeframes`, `push`) |
| POST | `/notify/test` | Send a test message to the configured channels |
| GET | `/history` | Audit log |
| POST | `/paper-trade` | Simulated order |

Auth: `X-API-Token` header, `Authorization: Bearer <token>`, or `?token=<token>`.
On machines without MetaTrader5 (Linux/macOS) reads are open (`PUBLIC_READ_ONLY`)
so the UI can be inspected; every write (start/stop/close/settings) always needs
the token.

## Static copy (GitHub Pages)

`./build_pages.sh` copies the UI into `docs/`. Point Pages at `/docs` and you get
a phone-friendly URL that works without the sandbox preview. The page runs in
demo mode with sample data, or you give it your API address in the login screen
(`?api=http://192.168.1.42:8000&token=...`).

## Before going live

* Keep `DRY_RUN=true` until the whole loop is verified, then trade a demo account.
* Never expose port 8000 without TLS — use Tailscale, Cloudflare Tunnel or a reverse proxy.
* Rotate `.api_token`, keep `CORS_ORIGINS` narrow, and read the audit log daily.
* No strategy guarantees profit; slippage, spread, swaps and outages cause losses.
