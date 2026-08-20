@echo off
cd /d "%~dp0"
echo ============================================
echo   Trade Cockpit - First-time setup
echo ============================================
echo.
echo Installing required libraries. Please wait...
echo.
python -m pip install --upgrade pip
python -m pip install yfinance feedparser pypdf pycryptodome
echo.
echo ============================================
echo   Setup complete.
echo   From next time, just double-click run.bat
echo ============================================
pause
