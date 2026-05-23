@echo off
REM ============================================================
REM Start Mosquitto broker with project config (verbose mode)
REM Run this from the broker/ directory
REM ============================================================

SET "MOSQ_DIR=C:\Program Files\mosquitto"
IF NOT EXIST "%MOSQ_DIR%\mosquitto.exe" SET "MOSQ_DIR=C:\Program Files (x86)\Mosquitto"

IF NOT EXIST "%MOSQ_DIR%\mosquitto.exe" (
    echo [ERROR] mosquitto.exe not found in either:
    echo   C:\Program Files\mosquitto\
    echo   C:\Program Files ^(x86^)\Mosquitto\
    echo Install Mosquitto from https://mosquitto.org/download/
    pause
    exit /b 1
)

echo Using: %MOSQ_DIR%\mosquitto.exe
echo Starting Mosquitto on ports 1883 (MQTT) and 9001 (WebSocket)...
"%MOSQ_DIR%\mosquitto.exe" -c mosquitto.conf -v
