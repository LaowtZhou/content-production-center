@echo off
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" scripts\launcher.py --open-browser
if errorlevel 1 (
    echo.
    echo 启动失败。请查看 data\logs\launcher.log 和 data\logs\server.log
    pause
)
