@echo off
REM Runs the bot and restarts it automatically if it crashes. Close this window to stop.
cd /d "%~dp0.."
:loop
.venv\Scripts\python.exe bot.py
echo Bot stopped. Restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto loop
