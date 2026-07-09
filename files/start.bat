@echo off
cd /d "%~dp0"

rem If a previous server did not shut down cleanly, it can keep holding port
rem 8765, causing the next startup to fail until it is killed manually via
rem Task Manager. Kill anything already listening on 8765 before starting.
for /f "tokens=5" %%p in ('netstat -aon ^| findstr ":8765" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%p >nul 2>&1
)

echo Starting Trade Cockpit server...
echo Keep this window OPEN while you use the app.
echo (Close this window to stop the server.)
python server.py
pause
