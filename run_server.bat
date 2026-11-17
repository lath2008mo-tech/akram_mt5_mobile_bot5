@echo off
REM Start the MT5 mobile API on Windows (double-click this file)
cd /d "%~dp0"

if not exist .env copy .env.example .env >nul

if not exist .venv (
  echo Creating virtual environment...
  py -3.11 -m venv .venv || python -m venv .venv
)
call .venv\Scripts\activate

echo Installing/updating dependencies...
pip install -q -r requirements.txt

echo.
echo ============================================================
echo  Open this on your phone (same Wi-Fi):
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do echo   http://%%a:8000
echo  API token is in .api_token (or API_TOKEN in .env)
echo ============================================================
echo.

uvicorn main:app --host 0.0.0.0 --port 8000
pause
