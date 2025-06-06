::%1 mshta vbscript:CreateObject("Shell.Application").ShellExecute("cmd.exe","/c %~s0 ::","","runas",1)(window.close)&&exit cd /d "%~dp0"
echo WindowsCRLF GBK2312
chcp 65001
set workdir=%~dp0
set workdir=%workdir:~0,-1%
cd /d %workdir%

call copyExe.bat

"exeDir/Backup.exe" "D:/GitDocs/移动云盘同步盘/NetPan"		"D:/Temp/我的云端硬盘/NetPan"
"exeDir/Backup.exe" "D:/GitHub/Docs"						"D:/Temp/我的云端硬盘/Docs"
pause