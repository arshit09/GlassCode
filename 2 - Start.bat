@echo off
title GlassesClaude
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo GlassesClaude is not installed yet. Double-click "1 - Install" first.
  echo.
  pause
  exit /b 1
)
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m app.main
echo.
echo GlassesClaude has stopped.
pause
