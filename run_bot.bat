@echo off
REM Start the trading engine (reads DRY_RUN / SYMBOLS etc. from .env)
cd /d "%~dp0"
call .venv\Scripts\activate
python mt5_scalper_bot.py
pause
