@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo First run: powershell -ExecutionPolicy Bypass -File setup.ps1
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -X utf8 app.py %*
pause
