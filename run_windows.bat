@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Creating Python virtual environment...
  python -m venv .venv
  if errorlevel 1 goto error
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
if errorlevel 1 goto error
python -m pip install -r requirements.txt
if errorlevel 1 goto error
if "%FRAUDGUARD_INGEST_API_KEY%"=="" set "FRAUDGUARD_INGEST_API_KEY=local-demo-key"
echo.
echo FraudGuard Live opens at http://127.0.0.1:8000
python -m uvicorn app:app --host 127.0.0.1 --port 8000
if errorlevel 1 goto error
goto :eof
:error
echo Startup failed. Confirm Python 3.10+ is installed and available as python.
pause
