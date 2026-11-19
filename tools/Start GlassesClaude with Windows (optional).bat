@echo off
title GlassesClaude - start with Windows
cd /d "%~dp0.."
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
(
  echo @echo off
  echo cd /d "%CD%"
  echo start "GlassesClaude" /min "%CD%\2 - Start.bat"
) > "%STARTUP%\GlassesClaude.bat"
echo.
echo Done. GlassesClaude will start (minimized) every time you sign in to Windows.
echo To stop that, delete this file: "%STARTUP%\GlassesClaude.bat"
echo.
pause
