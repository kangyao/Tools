<#
    配置要自动解锁的盘符和密码（密码用 DPAPI 按"本机"加密后写入 config.json）

    用法（普通 PowerShell 即可）:
      .\Set-Drive.ps1 -DriveLetter D -Password '123456'      # 普通解锁密码
      .\Set-Drive.ps1 -DriveLetter E -RecoveryPassword       # 交互式输入 48 位恢复密码
      .\Set-Drive.ps1 -DriveLetter D -Password 'xxx' -PlainText  # 明文保存（不安全，不推荐）
      .\Set-Drive.ps1 -List                                  # 查看当前配置
      .\Set-Drive.ps1 -DriveLetter D -Remove                 # 删除某个盘符
#>

[CmdletBinding()]
param(
    [string]$DriveLetter,
    [string]$Password,
    [switch]$RecoveryPassword,
    [switch]$Remove,
    [switch]$List,
    [switch]$PlainText,
    [string]$ConfigPath
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path -Path $PSScriptRoot -ChildPath 'config.json'
}

# 准备 DPAPI：优先使用"本机(LocalMachine)"范围，这样计划任务以 SYSTEM 运行时也能解密
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

# 加密保存密码，避免明文泄露
function Protect-Text {
    param([string]$Text)

    if ($script:HasMachineDpapi) {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
        $enc = [System.Security.Cryptography.ProtectedData]::Protect(
            $bytes,
            $null,
            [System.Security.Cryptography.DataProtectionScope]::LocalMachine)
        return [Convert]::ToBase64String($enc)
    }
    # 回退方案：仅当前用户可解密（此时计划任务需要用 -RunAsUser 安装）
    $sec = ConvertTo-SecureString $Text -AsPlainText -Force
    return ($sec | ConvertFrom-SecureString)
}

function Get-EntryPropertyNames {
    param($Object)
    return @($Object.PSObject.Properties.Name)
}

function Get-PlainFromSecureString {
    param([System.Security.SecureString]$Secure)
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

# ---------- 读取/初始化配置 ----------
$cfg = $null
if (Test-Path -LiteralPath $ConfigPath) {
    try { $cfg = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $cfg = $null }
}
if ($null -eq $cfg) {
    $cfg = [pscustomobject]@{ WaitTimeoutSeconds = 90; Drives = @() }
}
if (-not ((Get-EntryPropertyNames $cfg) -contains 'Drives') -or $null -eq $cfg.Drives) {
    $cfg | Add-Member -MemberType NoteProperty -Name 'Drives' -Value @() -Force
}

# ---------- 查看 ----------
if ($List) {
    $items = @($cfg.Drives)
    if ($items.Count -eq 0) {
        Write-Host '当前没有配置任何盘符。'
    } else {
        foreach ($it in $items) {
            $store = '未设置密码'
            if ((Get-EntryPropertyNames $it) -contains 'SecurePassword' -and $it.SecurePassword) { $store = '密码已加密保存' }
            elseif ((Get-EntryPropertyNames $it) -contains 'Password' -and $it.Password) { $store = '明文密码（不安全）' }
            $scope = 'Machine'
            if ((Get-EntryPropertyNames $it) -contains 'Scope' -and $it.Scope) { $scope = [string]$it.Scope }
            Write-Host ('{0}    Method={1}    Scope={2}    {3}' -f $it.DriveLetter, $it.Method, $scope, $store)
        }
    }
    exit 0
}

# ---------- 校验盘符 ----------
if ([string]::IsNullOrWhiteSpace($DriveLetter)) {
    Write-Host '请用 -DriveLetter 指定盘符，例如：-DriveLetter D' -ForegroundColor Yellow
    exit 1
}
$letter = ($DriveLetter -replace '[^A-Za-z]', '').ToUpper()
if ($letter.Length -ne 1) {
    Write-Host "无效的盘符：$DriveLetter" -ForegroundColor Red
    exit 1
}

$items = @($cfg.Drives) | Where-Object { $_ -ne $null }
$existing = $items | Where-Object { $_.DriveLetter.ToString().ToUpper().Trim(':') -eq $letter }

# ---------- 删除 ----------
if ($Remove) {
    if (-not $existing) {
        Write-Host "配置中没有盘符 $letter 。"
        exit 0
    }
    $cfg.Drives = @($items | Where-Object { $_.DriveLetter.ToString().ToUpper().Trim(':') -ne $letter })
    Write-Host "已删除盘符 $letter 的配置。"
} else {
    # ---------- 新增/更新 ----------
    if ([string]::IsNullOrEmpty($Password)) {
        $tip = if ($RecoveryPassword) { "请输入 $letter 盘的 48 位 BitLocker 恢复密码" } else { "请输入 $letter 盘的解锁密码" }
        $secureInput = Read-Host $tip -AsSecureString
        $Password = Get-PlainFromSecureString $secureInput
    }
    if ([string]::IsNullOrEmpty($Password)) {
        Write-Host '密码不能为空。' -ForegroundColor Red
        exit 1
    }

    $method = if ($RecoveryPassword) { 'RecoveryPassword' } else { 'Password' }

    if ($PlainText) {
        Write-Host '注意：密码将以明文写入 config.json，任何人打开文件都能看到。' -ForegroundColor Yellow
        $entry = [pscustomobject]@{ DriveLetter = $letter; Method = $method; Password = $Password }
    } else {
        $scope = if ($script:HasMachineDpapi) { 'Machine' } else { 'User' }
        $entry = [pscustomobject]@{
            DriveLetter     = $letter
            Method          = $method
            Scope           = $scope
            SecurePassword  = (Protect-Text $Password)
        }
    }

    if ($existing) {
        $cfg.Drives = @($items | Where-Object { $_.DriveLetter.ToString().ToUpper().Trim(':') -ne $letter }) + $entry
        Write-Host "已更新盘符 $letter 的配置。"
    } else {
        $cfg.Drives = @($items) + $entry
        Write-Host "已添加盘符 $letter 的配置。"
    }
}

# ---------- 保存 ----------
$json = $cfg | ConvertTo-Json -Depth 6
[System.IO.File]::WriteAllText($ConfigPath, $json, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "配置已保存到：$ConfigPath"
if (-not (Get-ScheduledTask -TaskName 'BitLockerAutoUnlock' -ErrorAction SilentlyContinue)) {
    Write-Host '提示：还没有安装开机计划任务，请右键"以管理员身份运行" Install.bat / Install.ps1 进行安装。' -ForegroundColor Cyan
}
