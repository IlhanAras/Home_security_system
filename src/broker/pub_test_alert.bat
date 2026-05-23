@echo off
REM ============================================================
REM Publish a sample motion alert to the local broker.
REM Use this to test the subscriber without retyping long commands.
REM ============================================================

SET "MOSQ_DIR=C:\Program Files\mosquitto"
IF NOT EXIST "%MOSQ_DIR%\mosquitto_pub.exe" SET "MOSQ_DIR=C:\Program Files (x86)\Mosquitto"

IF NOT EXIST "%MOSQ_DIR%\mosquitto_pub.exe" (
    echo [ERROR] mosquitto_pub.exe not found.
    pause
    exit /b 1
)

"%MOSQ_DIR%\mosquitto_pub.exe" -h 127.0.0.1 -p 1883 -t security/alert -m "{\"event\":\"motion\",\"device_id\":\"esp32-cam-01\"}"
echo Published to security/alert.
