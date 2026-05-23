@echo off
REM ============================================================
REM Start the FastAPI backend (Uvicorn, with auto-reload).
REM Requires setup.bat to have been run at least once.
REM ============================================================

cd /d "%~dp0"

IF NOT EXIST .venv\Scripts\uvicorn.exe (
    echo [ERROR] .venv not found. Run setup.bat first.
    pause
    exit /b 1
)

echo Starting FastAPI on http://127.0.0.1:8000 ...
.venv\Scripts\uvicorn.exe app:app --host 0.0.0.0 --port 8000 --reload
