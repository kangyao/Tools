@echo off
chcp 936 >nul
title 远程桌面 - yaokang.miniwan.com
rem 由 rdp_launcher.py 自动生成 2026-10-07 12:34
set "LAUNCHER=C:\Users\Admin\Desktop\rdp_launcher.py"
set "CONFIG=C:\Users\Admin\AppData\Roaming\RdpLauncher\hosts.json"

:H1
where python >nul 2>nul
if errorlevel 1 goto D1
if exist "%LAUNCHER%" (python "%LAUNCHER%" --connect "yaokang.miniwan.com" --config "%CONFIG%" & goto :eof)
:D1
start "" mstsc /v:yaokang.miniwan.com /f
goto :eof
