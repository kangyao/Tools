@echo off
chcp 65001 >nul
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw "%~dp0BitLocker配置工具.pyw" & exit /b)
where py >nul 2>nul && (start "" py -3 "%~dp0BitLocker配置工具.pyw" & exit /b)
where python >nul 2>nul && (start "" python "%~dp0BitLocker配置工具.pyw" & exit /b)
echo 未检测到 Python，请先安装 Python 3：https://www.python.org/downloads/
echo.
echo 若界面没有出现，请在本目录打开命令行执行：
echo     python BitLocker配置工具.pyw
echo 以查看具体错误信息（错误也会写入 gui_error.txt）
pause
