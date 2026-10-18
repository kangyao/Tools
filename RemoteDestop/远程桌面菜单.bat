@echo off
chcp 936 >nul
title 远程桌面一键连接
rem 由 rdp_launcher.py 自动生成 2026-10-07 12:32
set "LAUNCHER=C:\Users\Admin\Desktop\rdp_launcher.py"
set "CONFIG=C:\Users\Admin\AppData\Roaming\RdpLauncher\hosts.json"

echo ============ 远程桌面 ============
echo [1] yaokang.miniwan.com   (yaokang.miniwan.com)
echo [2] 30.23.53.13   (30.23.53.13)
echo [3] 30.23.53.77   (30.23.53.77)
echo [4] 10.76.146.48   (10.76.146.48)
echo [5] 10.76.146.59   (10.76.146.59)
echo [6] 10.76.145.70   (10.76.145.70)
echo [7] 10.76.145.109   (10.76.145.109)
echo [0] 退出
echo.
set /p "SEL=请输入序号并回车: "
if "%SEL%"=="0" goto :eof
if "%SEL%"=="1" goto H1
if "%SEL%"=="2" goto H2
if "%SEL%"=="3" goto H3
if "%SEL%"=="4" goto H4
if "%SEL%"=="5" goto H5
if "%SEL%"=="6" goto H6
if "%SEL%"=="7" goto H7
echo 无效序号
pause
goto :eof

:H1
where python >nul 2>nul
if errorlevel 1 goto D1
if exist "%LAUNCHER%" (python "%LAUNCHER%" --connect "yaokang.miniwan.com" --config "%CONFIG%" & goto :eof)
:D1
start "" mstsc /v:yaokang.miniwan.com /f
goto :eof

:H2
where python >nul 2>nul
if errorlevel 1 goto D2
if exist "%LAUNCHER%" (python "%LAUNCHER%" --connect "30.23.53.13" --config "%CONFIG%" & goto :eof)
:D2
start "" mstsc /v:30.23.53.13
goto :eof

:H3
where python >nul 2>nul
if errorlevel 1 goto D3
if exist "%LAUNCHER%" (python "%LAUNCHER%" --connect "30.23.53.77" --config "%CONFIG%" & goto :eof)
:D3
start "" mstsc /v:30.23.53.77
goto :eof

:H4
where python >nul 2>nul
if errorlevel 1 goto D4
if exist "%LAUNCHER%" (python "%LAUNCHER%" --connect "10.76.146.48" --config "%CONFIG%" & goto :eof)
:D4
start "" mstsc /v:10.76.146.48
goto :eof

:H5
where python >nul 2>nul
if errorlevel 1 goto D5
if exist "%LAUNCHER%" (python "%LAUNCHER%" --connect "10.76.146.59" --config "%CONFIG%" & goto :eof)
:D5
start "" mstsc /v:10.76.146.59
goto :eof

:H6
where python >nul 2>nul
if errorlevel 1 goto D6
if exist "%LAUNCHER%" (python "%LAUNCHER%" --connect "10.76.145.70" --config "%CONFIG%" & goto :eof)
:D6
start "" mstsc /v:10.76.145.70
goto :eof

:H7
where python >nul 2>nul
if errorlevel 1 goto D7
if exist "%LAUNCHER%" (python "%LAUNCHER%" --connect "10.76.145.109" --config "%CONFIG%" & goto :eof)
:D7
start "" mstsc /v:10.76.145.109
goto :eof
