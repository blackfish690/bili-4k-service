@echo off
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-protocol.ps1" -Check
echo.
echo ==== Finished. Screenshot this window if you need help. ====
pause
