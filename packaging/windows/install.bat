@echo off
setlocal enabledelayedexpansion

title Starship OS — StarAgent Windows Installer

set "AGENT_DIR=C:\Program Files\Starship\Agent"
set "DATA_DIR=C:\ProgramData\Starship"
set "LOGS_DIR=%DATA_DIR%\logs"
set "SERVICE_NAME=StarshipStarAgent"
set "SERVICE_DISP=Starship OS StarAgent Telemetry Collector"

echo ============================================
echo  Starship OS — StarAgent Windows Installer
echo ============================================
echo.

:: Check admin rights
net session >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo ERROR: This installer must be run as Administrator.
    echo Right-click install.bat and select "Run as administrator".
    pause
    exit /b 1
)

:: Copy files
echo [1/4] Installing files...
if not exist "%AGENT_DIR%" mkdir "%AGENT_DIR%"
if not exist "%DATA_DIR%" mkdir "%DATA_DIR%"
if not exist "%LOGS_DIR%" mkdir "%LOGS_DIR%"
if not exist "%DATA_DIR%\etc" mkdir "%DATA_DIR%\etc"

copy /Y "%~dp0staragent.exe" "%AGENT_DIR%\staragent.exe" >nul
if %ERRORLEVEL% neq 0 (
    echo ERROR: Failed to copy staragent.exe
    pause
    exit /b 1
)
echo   Copied staragent.exe to %AGENT_DIR%

:: Configure
echo [2/4] Configuring agent...
echo.
echo Enter your Starship OS Hub connection details:
echo.

set /p "NATS_URL=  NATS Hub URL [nats://10.0.0.1:4222]: "
if "!NATS_URL!"=="" set "NATS_URL=nats://10.0.0.1:4222"

set /p "NATS_TOKEN=  NATS Token (from hub /etc/starship/nats/fleet-bus.conf): "

set /p "HOSTNAME=  Agent hostname [leave blank for auto]: "

if "!HOSTNAME!"=="" (
    set "HOSTNAME_CFG="
) else (
    set "HOSTNAME_CFG=hostname: !HOSTNAME!"
)

:: Generate config
(
    echo # Starship OS — StarAgent Configuration
    echo # Installed by install.bat on %DATE% %TIME%
    echo.
    echo nats:
    echo   url: "!NATS_URL!"
    echo   token: "!NATS_TOKEN!"
    echo.
    echo telemetry:
    echo   interval_secs: 10
    echo.
    echo commands:
    echo   subscribe:
    echo     - "starship.agent.staragent.command.^>"
    echo     - "agnetic.agent.staragent.command.^>"
    if not "!HOSTNAME_CFG!"=="" (
        echo.
        echo !HOSTNAME_CFG!
    )
) > "%AGENT_DIR%\staragent.yaml"

echo   Config written to %AGENT_DIR%\staragent.yaml

:: Install Windows service
echo [3/4] Installing Windows service...
sc stop %SERVICE_NAME% >nul 2>&1
sc delete %SERVICE_NAME% >nul 2>&1

sc create %SERVICE_NAME% ^
    binPath="%AGENT_DIR%\staragent.exe" ^
    DisplayName="%SERVICE_DISP%" ^
    start=auto ^
    obj=LocalSystem

if %ERRORLEVEL% neq 0 (
    echo ERROR: Failed to create service
    pause
    exit /b 1
)

sc description %SERVICE_NAME% "Collects system telemetry and publishes to Starship OS NATS bus" >nul

:: Set service recovery options
sc failure %SERVICE_NAME% reset=86400 actions=restart/5000/restart/10000/restart/30000 >nul

echo   Service "%SERVICE_DISP%" installed

:: Add STARSHIP_ROOT and STARAGENT_CONFIG env vars
echo [4/4] Setting environment variables...
setx STARSHIP_ROOT "%AGENT_DIR%" /m >nul 2>&1
setx STARAGENT_CONFIG "%AGENT_DIR%\staragent.yaml" /m >nul 2>&1
echo   System environment variables set

:: Start service
echo.
echo Starting service...
sc start %SERVICE_NAME% >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo SUCCESS: StarAgent service started.
) else (
    echo WARNING: Service installed but may need manual start.
    echo   Run: sc start %SERVICE_NAME%
)

echo.
echo ============================================
echo  Installation Complete
echo ============================================
echo.
echo  Binary:  %AGENT_DIR%\staragent.exe
echo  Config:  %AGENT_DIR%\staragent.yaml
echo  Logs:    %LOGS_DIR%
echo  Service: %SERVICE_NAME%
echo.
echo  To check status: sc query %SERVICE_NAME%
echo  To view logs:    type "%LOGS_DIR%\staragent.log"
echo  To uninstall:    run uninstall.bat as Administrator
echo.
pause
