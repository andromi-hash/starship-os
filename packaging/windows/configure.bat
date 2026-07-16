@echo off
setlocal enabledelayedexpansion
title Starship OS — StarAgent Configurator

set "AGENT_DIR=C:\Program Files\Starship\Agent"
set "CONFIG=%AGENT_DIR%\staragent.yaml"

echo ============================================
echo  Starship OS — StarAgent Configurator
echo ============================================
echo.

net session >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo NOTE: Some operations require Administrator.
)

if not exist "%CONFIG%" (
    echo WARNING: Config file not found at %CONFIG%
    echo Run install.bat first, or create the config manually.
    echo.
)

echo Current settings (if config exists):
echo.
if exist "%CONFIG%" (
    type "%CONFIG%"
) else (
    echo   (no config file found)
)
echo.

echo Enter new connection details (leave blank to keep current):
echo.

:: Read current values
set "CURRENT_URL="
set "CURRENT_TOKEN="
if exist "%CONFIG%" (
    for /f "tokens=*" %%a in ('findstr "url:" "%CONFIG%"') do set "CURRENT_LINE=%%a"
    if defined CURRENT_LINE (
        for /f "tokens=2 delims= " %%b in ("!CURRENT_LINE!") do set "CURRENT_URL=%%b"
    )
    for /f "tokens=*" %%a in ('findstr "token:" "%CONFIG%"') do set "CURRENT_LINE=%%a"
    if defined CURRENT_LINE (
        for /f "tokens=2 delims= " %%b in ("!CURRENT_LINE!") do set "CURRENT_TOKEN=%%b"
    )
)

set /p "NATS_URL=  NATS Hub URL [%CURRENT_URL%]: "
if "!NATS_URL!"=="" set "NATS_URL=!CURRENT_URL!"
if "!NATS_URL!"=="" set "NATS_URL=nats://127.0.0.1:4222"

set /p "NATS_TOKEN=  NATS Token: "
if "!NATS_TOKEN!"=="" set "NATS_TOKEN=!CURRENT_TOKEN!"

:: Write config
(
    echo # Starship OS — StarAgent Configuration
    echo # Updated by configure.bat on %DATE% %TIME%
    echo.
    echo nats:
    echo   url: "!NATS_URL!"
    if not "!NATS_TOKEN!"=="" echo   token: "!NATS_TOKEN!"
    echo.
    echo telemetry:
    echo   interval_secs: 10
    echo.
    echo commands:
    echo   subscribe:
    echo     - "starship.agent.staragent.command.^>"
    echo     - "agnetic.agent.staragent.command.^>"
) > "%CONFIG%"

echo.
echo Config updated. Restarting service...
sc stop StarshipStarAgent >nul 2>&1
sc start StarshipStarAgent >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo Service restarted successfully.
) else (
    echo WARNING: Could not restart service. Start manually:
    echo   sc start StarshipStarAgent
)
echo.
pause
