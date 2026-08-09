@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-BiBaZuShortcuts.ps1"
if errorlevel 1 (
  echo.
  echo Installation fehlgeschlagen. Bitte die Fehlermeldung oben weitergeben.
  pause
  exit /b 1
)
echo.
echo Die Verknuepfungen sind jetzt auf dem Desktop und im Windows-Startmenue.
pause
