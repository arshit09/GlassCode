@echo off
title GlassesClaude - Check glasses (ADB)
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Double-click "1 - Install" first.
  pause
  exit /b 1
)
set PYTHONUTF8=1
".venv\Scripts\python.exe" -m app.adb_tools check
pause
