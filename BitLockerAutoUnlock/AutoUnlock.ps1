<#
    BitLocker 自动解锁主脚本
    由计划任务在开机/登录时调用：读取 config.json 中配置的盘符与密码，逐个解锁。

    手动测试（管理员 PowerShell）:
      powershell -NoProfile -ExecutionPolicy Bypass -File .\AutoUnlock.ps1
#>

[CmdletBinding()]
param(
    # 配置文件路径，默认与本脚本同目录的 config.json
    [string]$ConfigPath,

    # 等待卷出现的超时秒数；<0 表示使用配置文件中的 WaitTimeoutSeconds
    [int]$TimeoutSeconds = -1,

    # 不等待，直接尝试一次
    [switch]$NoWait
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path -Path $PSScriptRoot -ChildPath 'config.json'
}

$LogFile = Join-Path -Path (Join-Path -Path $env:ProgramData -ChildPath 'BitLockerAutoUnlock') -ChildPath 'unlock.log'

function Write-Log {
    param(
        [string]$Message,
        [string]$Level = 'INFO'
    )
    $line = '[{0}] [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    try {
        $dir = Split-Path -Path $LogFile -Parent
        if (-not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir -Force | Out-Null
        }
        Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
    } catch {
        # 日志写失败不能影响解锁
    }
    Write-Host $line
}

# 准备 DPAPI：优先使用"本机(LocalMachine)"范围，这样 SYSTEM 账户也能解密
$script:HasMachineDpapi = $false
if ('System.Security.Cryptography.ProtectedData' -as [type]) {
    $script:HasMachineDpapi = $true
} else {
    try {
        Add-Type -AssemblyName System.Security -ErrorAction Stop
        $script:HasMachineDpapi = $true
    } catch {
        $script:HasMachineDpapi = $false
    }
}

# 解密 Set-Drive.ps1 保存的密码
function Unprotect-Text {
    param(
        [string]$Value,
        [string]$Scope = 'Machine'
    )

    if ($Scope -eq 'User') {
        $sec = ConvertTo-SecureString $Value -ErrorAction Stop
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
        try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    }

    if (-not $script:HasMachineDpapi) {
        throw '当前环境不支持 DPAPI 本机解密（缺少 System.Security.Cryptography.ProtectedData），请用 Set-Drive.ps1 重新保存密码。'
    }
    $bytes = [Convert]::FromBase64String($Value)
    $raw = [System.Security.Cryptography.ProtectedData]::Unprotect(
        $bytes,
        $null,
        [System.Security.Cryptography.DataProtectionScope]::LocalMachine)
    return [System.Text.Encoding]::UTF8.GetString($raw)
}

function Get-EntryPropertyNames {
    param($Object)
    return @($Object.PSObject.Properties.Name)
}

function Get-DrivePassword {
    param($Entry, [string]$Mount)

    $names = Get-EntryPropertyNames $Entry

    $scope = 'Machine'
    if (($names -contains 'Scope') -and $Entry.Scope) { $scope = [string]$Entry.Scope }

    if (($names -contains 'SecurePassword') -and $Entry.SecurePassword) {
        try {
            return (Unprotect-Text $Entry.SecurePassword $scope)
        } catch {
            throw "盘符 $Mount 的加密密码无法解密：$($_.Exception.Message)。请在本机用 Set-Drive.ps1 重新保存密码。"
        }
    }
    if (($names -contains 'Password') -and $Entry.Password) {
        return [string]$Entry.Password
    }
    throw "盘符 $Mount 未配置密码（config.json 中需要 Password 或 SecurePassword 字段）。"
}

function Test-IsAdmin {
    $p = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

try {
    Write-Log '==== BitLocker 自动解锁开始 ===='
    Write-Log "当前身份：$([Security.Principal.WindowsIdentity]::GetCurrent().Name)；配置：$ConfigPath"

    if (-not (Get-Command Get-BitLockerVolume -ErrorAction SilentlyContinue)) {
        Write-Log '系统不支持 BitLocker 命令（可能没有管理员权限，或为不支持 BitLocker 的 Windows 版本）。' 'ERROR'
        exit 1
    }
    if (-not (Test-IsAdmin)) {
        Write-Log '当前不是管理员权限，无法解锁 BitLocker。请确认计划任务勾选了"使用最高权限运行"。' 'ERROR'
        exit 1
    }
    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        Write-Log "配置文件不存在：$ConfigPath" 'ERROR'
        exit 1
    }

    $cfg = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $cfgNames = Get-EntryPropertyNames $cfg

    if ($TimeoutSeconds -lt 0) {
        $TimeoutSeconds = 90
        if (($cfgNames -contains 'WaitTimeoutSeconds') -and $cfg.WaitTimeoutSeconds) {
            $TimeoutSeconds = [int]$cfg.WaitTimeoutSeconds
        }
    }

    $drives = @()
    if (($cfgNames -contains 'Drives') -and $cfg.Drives) { $drives = @($cfg.Drives) }
    if ($drives.Count -eq 0) {
        Write-Log '配置中没有需要解锁的盘符（Drives 为空），请用 Set-Drive.ps1 添加。' 'WARN'
    }

    $failed = 0

    foreach ($d in $drives) {
        if ($null -eq $d) { continue }
        $dNames = Get-EntryPropertyNames $d
        if (-not ($dNames -contains 'DriveLetter') -or -not $d.DriveLetter) { continue }

        $letter = ($d.DriveLetter.ToString() -replace '[^A-Za-z]', '').ToUpper()
        if ($letter.Length -ne 1) {
            Write-Log "无效的盘符：$($d.DriveLetter)" 'ERROR'
            $failed++
            continue
        }
        $mount = $letter + ':'

        $method = 'Password'
        if (($dNames -contains 'Method') -and $d.Method) { $method = [string]$d.Method }

        # 等卷出现（开机时盘符可能还没分配好）
        $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
        $vol = $null
        while ($true) {
            $vol = Get-BitLockerVolume -MountPoint $mount -ErrorAction SilentlyContinue
            if ($vol) { break }
            if ($NoWait -or ((Get-Date) -ge $deadline)) { break }
            Start-Sleep -Seconds 3
        }

        if (-not $vol) {
            Write-Log "找不到卷 $mount（已等待 $TimeoutSeconds 秒），跳过。" 'ERROR'
            $failed++
            continue
        }

        try {
            if ($vol.VolumeStatus -eq 'FullyDecrypted') {
                Write-Log "$mount 未启用 BitLocker，跳过。"
                continue
            }
            if ($vol.LockStatus -eq 'Unlocked') {
                Write-Log "$mount 已经处于解锁状态，跳过。"
                continue
            }

            $plain = Get-DrivePassword -Entry $d -Mount $mount
            $secure = ConvertTo-SecureString $plain -AsPlainText -Force
            $ok = $false

            for ($i = 1; $i -le 3; $i++) {
                try {
                    if ($method -match 'Recovery') {
                        Unlock-BitLocker -MountPoint $mount -RecoveryPassword $secure -ErrorAction Stop
                    } else {
                        Unlock-BitLocker -MountPoint $mount -Password $secure -ErrorAction Stop
                    }
                    $ok = $true
                    break
                } catch {
                    Write-Log "$mount 第 $i 次解锁失败：$($_.Exception.Message)" 'WARN'
                    Start-Sleep -Seconds 3
                }
            }

            if ($ok) {
                Write-Log "$mount 解锁成功（方式：$method）。"
            } else {
                Write-Log "$mount 解锁失败，请检查配置的密码/恢复密码是否正确。" 'ERROR'
                $failed++
            }
        } catch {
            Write-Log "$mount 处理异常：$($_.Exception.Message)" 'ERROR'
            $failed++
        }
    }

    Write-Log "==== 执行结束，失败 $failed 项 ===="
    if ($failed -gt 0) { exit 1 } else { exit 0 }

} catch {
    Write-Log "脚本执行出错：$($_.Exception.Message)" 'ERROR'
    exit 1
}
