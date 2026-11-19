@echo off
title GlassesClaude - Install
cd /d "%~dp0"
echo Setting up GlassesClaude. This window shows the progress.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\install.ps1"
echo.
pause
