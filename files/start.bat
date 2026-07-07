@echo off
cd /d "%~dp0"
echo Starting Trade Cockpit server...
echo Keep this window OPEN while you use the app.
echo (Close this window to stop the server.)
python server.py
pause
