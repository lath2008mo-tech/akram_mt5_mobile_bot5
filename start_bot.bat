@echo off
REM Dubbelklicka HAR. Klistra in Telegram-token en gang, sen skoter den allt.
cd /d "%~dp0"
title MT5-boten - Telegram-signaler

if not exist .venv (
  echo Skapar miljö första gången, vänligen vänta...
  py -3.11 -m venv .venv 2>nul || python -m venv .venv
)
call .venv\Scripts\activate
pip install -q -r requirements.txt

python setup_wizard.py %*

echo.
echo Fönstret kan stängas.
pause
