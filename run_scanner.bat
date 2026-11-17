@echo off
REM Scan the market and push setups to Telegram/SMS every 5 minutes
cd /d "%~dp0"
call .venv\Scripts\activate
python scanner.py --loop 300 --notify
pause
