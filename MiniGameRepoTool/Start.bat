@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 exit /b 1
if not exist ".venv\Scripts\pythonw.exe" goto install
".venv\Scripts\python.exe" -c "from PySide6 import QtWidgets" >nul 2>&1
if errorlevel 1 goto install
goto launch

:install
call "%~dp0Install.bat"
if errorlevel 1 goto failed

:launch
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0run.py" %*
exit /b 0

:failed
pause
exit /b 1
