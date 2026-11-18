@echo off
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-protocol.ps1" -Uninstall
echo.
echo ==== Finished. ====
pause
