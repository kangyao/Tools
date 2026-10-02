<#
    安装 / 卸载 BitLocker 开机自动解锁计划任务（需要管理员权限）

    用法:
      .\Install.ps1                 # 安装：以 SYSTEM 身份开机自动运行（默认，无需输入密码）
      .\Install.ps1 -RunAsUser      # 安装：以当前用户身份、登录时运行
      .\Install.ps1 -RunNow         # 安装后立刻触发一次，验证是否能解锁
      .\Install.ps1 -Uninstall      # 卸载计划任务
#>

[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$RunNow,
    [switch]$RunAsUser,
    [string]$TaskName = 'BitLockerAutoUnlock'
)

$ErrorActionPreference = 'Stop'

$MainScript = Join-Path -Path $PSScriptRoot -ChildPath 'AutoUnlock.ps1'
$ConfigPath = Join-Path -Path $PSScriptRoot -ChildPath 'config.json'
$PowerShellExe = Join-Path -Path $env:WINDIR -ChildPath 'System32\WindowsPowerShell\v1.0\powershell.exe'
$LogFile = Join-Path -Path (Join-Path -Path $env:ProgramData -ChildPath 'BitLockerAutoUnlock') -ChildPath 'unlock.log'

function Assert-Admin {
    $p = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host '需要管理员权限：请右键 PowerShell 或 Install.bat，选择"以管理员身份运行"。' -ForegroundColor Yellow
        exit 1
    }
}

# ---------------- 卸载 ----------------
if ($Uninstall) {
    Assert-Admin
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "已卸载计划任务：$TaskName"
    } else {
        Write-Host "未找到计划任务：$TaskName"
    }
    exit 0
}

# ---------------- 安装 ----------------
Assert-Admin

if (-not (Test-Path -LiteralPath $MainScript)) {
    Write-Host "找不到主脚本：$MainScript" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    Write-Host "找不到配置文件：$ConfigPath（可先运行 Set-Drive.ps1 生成）" -ForegroundColor Yellow
}

$argument = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$MainScript`""

$action   = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $argument
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

if ($RunAsUser) {
    # 以当前用户身份运行（登录时触发，无需保存账户密码）
    $triggers  = @((New-ScheduledTaskTrigger -AtLogOn), (New-ScheduledTaskTrigger -AtStartup))
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
    $who = "$env:USERDOMAIN\$env:USERNAME"
} else {
    # 以 SYSTEM 身份运行（开机即运行，无需登录、无需密码）
    $triggers  = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn))
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $who = 'SYSTEM'
}

$task = New-ScheduledTask -Action $action -Trigger $triggers -Settings $settings -Principal $principal
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null

Write-Host "已安装计划任务：$TaskName"
Write-Host "  运行身份：$who（最高权限）"
Write-Host "  触发时机：系统启动 + 用户登录"
Write-Host "  执行脚本：$MainScript"

if ($RunNow) {
    Write-Host '正在立即触发一次执行...'
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 8
    if (Test-Path -LiteralPath $LogFile) {
        Write-Host "----- 日志尾部（$LogFile）-----" -ForegroundColor Cyan
        Get-Content -LiteralPath $LogFile -Tail 20
    }
}

Write-Host ''
Write-Host "完整日志：$LogFile"
Write-Host '提示：若重启后仍未解锁，先确认 config.json 里盘符/密码正确，再查看上面的日志。'
