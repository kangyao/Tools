@echo off
rem Create directory junctions beside this script. Existing paths are never removed.
setlocal
set "LINK_SETUP_EXIT_CODE=0"

call :CreateDirectoryLink "%~dp0CoreAIRelease" "C:\MiniGame\Bin64\Installer\CoreAIRelease"
if errorlevel 1 set "LINK_SETUP_EXIT_CODE=1"
call :CreateDirectoryLink "%~dp0AIGamePlay" "C:\MiniGame\Source\AIGamePlay"
if errorlevel 1 set "LINK_SETUP_EXIT_CODE=1"
call :CreateDirectoryLink "%~dp0aicoreprojects" "C:\aicoreprojects"
if errorlevel 1 set "LINK_SETUP_EXIT_CODE=1"

pause
exit /b %LINK_SETUP_EXIT_CODE%

:CreateDirectoryLink
setlocal
set "LINK_PATH=%~1"
set "TARGET_PATH=%~2"

if not exist "%TARGET_PATH%\" (
    echo ERROR: Target directory does not exist: "%TARGET_PATH%"
    exit /b 1
)

if exist "%LINK_PATH%" (
    powershell.exe -NoProfile -NonInteractive -Command "$link = Get-Item -LiteralPath $env:LINK_PATH -Force -ErrorAction Stop; if ($link.LinkType -eq 'Junction' -and [IO.Path]::GetFullPath([string]$link.Target).TrimEnd('\') -eq [IO.Path]::GetFullPath($env:TARGET_PATH).TrimEnd('\')) { exit 0 }; exit 1"
    if errorlevel 1 (
        echo ERROR: Existing path is not the expected junction: "%LINK_PATH%"
        exit /b 1
    )
    echo Directory link already configured: "%LINK_PATH%"
    exit /b 0
)

mklink /J "%LINK_PATH%" "%TARGET_PATH%"
set "LINK_EXIT_CODE=%errorlevel%"
if "%LINK_EXIT_CODE%"=="0" (
    echo Directory link created successfully: "%LINK_PATH%"
) else (
    echo ERROR: Failed to create the directory link.
)
exit /b %LINK_EXIT_CODE%
