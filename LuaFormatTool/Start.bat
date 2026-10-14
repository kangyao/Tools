@echo off
cd /d "%~dp0"
python lua_format_tool.py gui
if errorlevel 1 pause
