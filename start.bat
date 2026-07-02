@echo off
setlocal
cd /d "%~dp0"
title VALORANT AI Coach

echo ============================================
echo   VALORANT AI Coach - Launcher
echo ============================================
echo   Japanese guide: see the .txt file in this folder.
echo.

set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if defined PY goto :checkver
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY goto :nopython

:checkver
%PY% -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if errorlevel 1 goto :nopython

echo Using Python:
%PY% --version
echo.

if exist .venv goto :havevenv
echo [1/3] Creating virtual environment (first time only)...
%PY% -m venv .venv
if errorlevel 1 goto :fail

:havevenv
call ".venv\Scripts\activate.bat"
if errorlevel 1 goto :fail

echo [2/3] Installing dependencies (first time takes a few minutes)...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
if errorlevel 1 goto :fail

echo [3/3] Starting server...
echo.
echo   Open  http://localhost:8000  in your browser.
echo   The admin token is shown below.
echo   Press Ctrl+C (or close this window) to stop the server.
echo.
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
echo.
echo Server stopped.
pause
exit /b 0

:nopython
echo.
echo [ERROR] Python 3.10 or newer was not found on this PC.
echo.
echo   1. Download Python from:  https://www.python.org/downloads/
echo   2. IMPORTANT: in the installer, check "Add python.exe to PATH"
echo      before clicking "Install Now".
echo   3. After installing, double-click this start.bat again.
echo.
echo   (Japanese instructions: see the .txt file in this folder)
echo.
pause
exit /b 1

:fail
echo.
echo [ERROR] Setup failed. Please read the message above.
echo   If it keeps failing, delete the ".venv" folder and try again.
echo.
pause
exit /b 1
