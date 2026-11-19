@echo off
title GlassesClaude - Set up away mode
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Double-click "1 - Install" first.
  pause
  exit /b 1
)
where npx >nul 2>nul
if errorlevel 1 (
  echo Installing Node.js [needed once for Cloudflare's free tool] ...
  winget install -e --id OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements
  set "PATH=%ProgramFiles%\nodejs;%PATH%"
)
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m app.away_setup %*
pause
