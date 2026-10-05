@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 exit /b 1

echo ============================================
echo  MiniGame Repo Tool - environment setup
echo  Folder: %CD%
echo ============================================
echo.

rem ---- 1. Look for an existing Python >= 3.11 ----
call :find_python
if not defined PY goto try_install
goto have_python

:find_python
set "PY="
for %%V in (3.13 3.12 3.11) do (
    if not defined PY (
        py -%%V -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
        if not errorlevel 1 set "PY=py -%%V"
    )
)
if not defined PY (
    py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
)
exit /b 0

rem ---- 2. Not found: install with winget (no admin needed) ----
:try_install
echo [1/4] Python 3.11 not found. Installing via winget ...
where winget >nul 2>&1
if errorlevel 1 goto install_manual

winget install -e --id Python.Python.3.11 --scope user --disable-interactivity --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto install_manual

echo.
echo Installed. Re-detecting ...
call :find_python
if not defined PY goto install_manual
goto have_python

:install_manual
echo.
echo Automatic install failed or winget is unavailable.
echo Install Python 3.11 from:
echo   https://www.python.org/downloads/release/python-3119/
echo Tick "Add python.exe to PATH", then run this script again.
pause
exit /b 1

rem ---- 3. Virtual environment ----
:have_python
echo [1/4] Python found:
%PY% -V
echo.

if exist ".venv\Scripts\python.exe" (
    echo [2/4] .venv already exists, reusing it.
) else (
    echo [2/4] Creating .venv ...
    %PY% -m venv ".venv"
    if errorlevel 1 goto failed
)

echo [3/4] Upgrading pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto failed

echo [4/4] Installing project + test dependencies ...
".venv\Scripts\python.exe" -m pip install -e ".[test]"
if errorlevel 1 goto failed

echo.
echo Environment ready.
".venv\Scripts\python.exe" -V
".venv\Scripts\python.exe" -c "import PySide6; print('PySide6', PySide6.__version__)"
echo.
echo Run Start.bat to launch the application.
pause
exit /b 0

:failed
echo.
echo Setup failed. See the error above.
pause
exit /b 1
