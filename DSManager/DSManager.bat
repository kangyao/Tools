@echo off
setlocal
cd /d "%~dp0"
where pythonw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pythonw.exe "%~dp0ds_manager.py"
    exit /b 0
)
where pyw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pyw.exe -3 "%~dp0ds_manager.py"
    exit /b 0
)
python "%~dp0ds_manager.py"
if errorlevel 1 pause
