@echo off
setlocal
cd /d "%~dp0"

where pyw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pyw.exe -3 "%~dp0launcher.py"
    exit /b 0
)

where pythonw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pythonw.exe "%~dp0launcher.py"
    exit /b 0
)

echo [ERROR] Python with Tkinter was not found.
echo Install Python 3 or add pyw.exe/pythonw.exe to PATH.
pause
exit /b 1

