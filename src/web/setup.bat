@echo off
REM ============================================================
REM One-time setup: create venv and install backend dependencies.
REM ============================================================

cd /d "%~dp0"

IF NOT EXIST .venv (
    echo Creating virtual environment at .venv ...
    python -m venv .venv
    IF ERRORLEVEL 1 (
        echo [ERROR] Could not create virtual environment.
        echo Make sure Python 3.10+ is installed and on PATH.
        pause
        exit /b 1
    )
)

echo Installing dependencies ...
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
IF ERRORLEVEL 1 (
    echo [ERROR] pip install failed.
    pause
    exit /b 1
)

echo.
echo Setup complete. Run "run.bat" to start the backend.
pause
