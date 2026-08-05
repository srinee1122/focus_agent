@echo off
:: ── Sri Ambikas ERP Dashboard Launcher ──
:: Works from any location via shortcut — resolves its own folder.

cd /d "%~dp0erp_dashboard"

:: Start the dashboard minimized
start "ERP Dashboard" /min cmd /c "python -m uvicorn main:app --port 8000"

:: Wait for server to come up, then open the browser
timeout /t 5 /nobreak > nul
start http://localhost:8000
