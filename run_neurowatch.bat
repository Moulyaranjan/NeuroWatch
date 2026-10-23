@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=%~dp0.venv311\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo Python 3.11 environment not found at .venv311.
    echo Create it with: py -3.11 -m venv .venv311
    echo Then install the project dependencies into that environment.
    exit /b 1
)

"%PYTHON%" -c "import asyncio; asyncio.run(asyncio.sleep(0))" >nul 2>&1
if errorlevel 1 (
    echo The .venv311 Python runtime failed its asyncio startup check.
    exit /b 1
)

"%PYTHON%" -m uvicorn main:app --host 0.0.0.0 --port 8000
