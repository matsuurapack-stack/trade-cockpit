@echo off
cd /d "%~dp0"
python fetch_data.py
echo.
echo ==== finished. read the messages above ====
pause