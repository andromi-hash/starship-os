@echo off
setlocal
title Starship OS — StarAgent Uninstaller

set "AGENT_DIR=C:\Program Files\Starship\Agent"
set "SERVICE_NAME=StarshipStarAgent"

echo ============================================
echo  Starship OS — StarAgent Uninstaller
echo ============================================
echo.

:: Check admin rights
net session >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo ERROR: This uninstaller must be run as Administrator.
    pause
    exit /b 1
)

:: Stop and delete service
echo [1/3] Stopping and removing service...
sc stop %SERVICE_NAME% >nul 2>&1
sc delete %SERVICE_NAME% >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo   Service removed.
) else (
    echo   Service not found or already removed.
)

:: Remove environment variables
echo [2/3] Removing environment variables...
setx STARSHIP_ROOT "" /m >nul 2>&1
setx STARAGENT_CONFIG "" /m >nul 2>&1
echo   Environment variables cleared.

:: Remove files
echo [3/3] Removing files...
if exist "%AGENT_DIR%" (
    rmdir /S /Q "%AGENT_DIR%"
    echo   Removed %AGENT_DIR%
) else (
    echo   Agent directory not found.
)

echo.
echo ============================================
echo  Uninstall Complete
echo ============================================
echo.
echo  Note: Logs in C:\ProgramData\Starship\ were NOT removed.
echo  To remove manually: rmdir /S /Q C:\ProgramData\Starship
echo.
pause
