@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 exit /b 1
if exist ".venv\Scripts\python.exe" goto install
py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if errorlevel 1 goto system_python
py -3 -m venv ".venv"
if errorlevel 1 goto failed
goto install

:system_python
python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if errorlevel 1 goto missing_python
python -m venv ".venv"
if errorlevel 1 goto failed

:install
".venv\Scripts\python.exe" -m pip install -e "."
if errorlevel 1 goto failed
echo Installation completed. Run Start.bat to open the application.
exit /b 0

:missing_python
echo Python 3.11 or later is required. Install Python and run Install.bat again.
exit /b 1

:failed
echo Installation failed. See the error above.
exit /b 1
