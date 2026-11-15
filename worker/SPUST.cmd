@echo off
rem gta6bananaclips YouTube worker: dvojklik na flashce spusti instalaci.
rem Pozada o spravce (UAC), aby uloha bezela 24/7 i bez prihlaseni.
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0instaluj.ps1"
pause
