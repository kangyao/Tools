@echo off
setlocal
cd /d "%~dp0"

rem The launcher needs Python 3.8+ (typing.Protocol) with Tkinter.
rem "pyw -3" is NOT enough: it may resolve to an older Python such as 3.7.
set "PYW="

rem 1) Python Launcher, newest supported version first.
where pyw.exe >nul 2>nul
if not errorlevel 1 (
    for %%V in (3.13 3.12 3.11 3.10 3.9 3.8) do (
        if not defined PYW (
            pyw.exe -%%V -c "import sys,tkinter;sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>&1
            if not errorlevel 1 set "PYW=pyw.exe -%%V"
        )
    )
)

rem 2) Per-user Python 3.11 installation.
if not defined PYW if exist "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe" (
    set "PYW="%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe""
)

rem 3) pythonw.exe on PATH, but only if it is 3.8+ with Tkinter.
if not defined PYW (
    where pythonw.exe >nul 2>nul
    if not errorlevel 1 (
        pythonw.exe -c "import sys,tkinter;sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>&1
        if not errorlevel 1 set "PYW=pythonw.exe"
    )
)

if not defined PYW (
    echo [ERROR] No Python 3.8+ with Tkinter was found.
    echo Install Python 3.8 or newer, or add pyw.exe / pythonw.exe to PATH.
    echo To see the real error, run:  py -3.11 launcher.py
    pause
    exit /b 1
)

start "" %PYW% "%~dp0launcher.py"
exit /b 0
