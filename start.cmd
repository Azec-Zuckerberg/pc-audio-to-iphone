@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   AirPods WiFi Audio Bridge
echo ============================================
echo.

rem First run: create the virtual environment and install dependencies.
if not exist ".venv\Scripts\python.exe" (
    echo First run detected - setting up Python environment...
    echo.
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo ERROR: could not create the virtual environment.
        echo Make sure Python 3.12 is installed and on your PATH.
        echo.
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\pip.exe" install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo ERROR: dependency install failed. See messages above.
        echo.
        pause
        exit /b 1
    )
    echo.
    echo Setup complete.
    echo.
)

echo Starting server... open the printed URL in Safari on your iPhone, then use
echo the Listen / Mic toggle on the page (Listen = PC audio to your AirPods,
echo Mic = your phone's microphone into the PC).
echo.
echo The URL is https:// - Safari will warn the certificate is not trusted the
echo first time. That is expected (it is your own PC, self-signed). Tap
echo "Show Details" then "visit this website" to continue. Microphone mode does
echo not work without this - browsers only allow the mic on a secure page.
echo.
echo To make the phone a real system microphone (for Discord/OBS/games), install
echo VB-CABLE and start with:  start.cmd --mic-device ^<CABLE Input index^>
echo (run:  .venv\Scripts\python.exe server.py --list-devices  to find it)
echo.
echo Press Ctrl+C in this window to stop.
echo.

".venv\Scripts\python.exe" server.py %*

echo.
echo Server stopped.
pause
