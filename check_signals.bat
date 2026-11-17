@echo off
REM Visar om Telegram är uppsatt utan att starta nagot
cd /d "%~dp0"
call .venv\Scripts\activate
python setup_wizard.py --check
pause
