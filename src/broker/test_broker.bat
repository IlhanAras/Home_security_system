@echo off
REM ============================================================
REM Quick Mosquitto smoke test.
REM Subscribes to security/# on the local broker.
REM Publish from another terminal with:
REM
REM   mosquitto_pub -h 127.0.0.1 -p 1883 -t security/alert -m "{\"event\":\"motion\"}"
REM
REM ============================================================

SET "MOSQ_DIR=C:\Program Files\mosquitto"
IF NOT EXIST "%MOSQ_DIR%\mosquitto_sub.exe" SET "MOSQ_DIR=C:\Program Files (x86)\Mosquitto"

IF NOT EXIST "%MOSQ_DIR%\mosquitto_sub.exe" (
    echo [ERROR] mosquitto_sub.exe not found in either:
    echo   C:\Program Files\mosquitto\
    echo   C:\Program Files ^(x86^)\Mosquitto\
    pause
    exit /b 1
)

echo Subscribing to security/# on 127.0.0.1:1883 ...
echo Press Ctrl+C to stop.
"%MOSQ_DIR%\mosquitto_sub.exe" -h 127.0.0.1 -p 1883 -t "security/#" -v
