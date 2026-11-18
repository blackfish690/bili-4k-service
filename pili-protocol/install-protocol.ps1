#requires -Version 5.1
<#
  Bili 4K 拉流 - pili:// 协议安装器（便携版）

  用法：
    双击「安装.cmd」安装；双击「自检.cmd」重跑检查；双击「卸载.cmd」卸载。
    也可以直接运行：
      powershell -NoProfile -ExecutionPolicy Bypass -File .\install-protocol.ps1
      powershell -NoProfile -ExecutionPolicy Bypass -File .\install-protocol.ps1 -Check
      powershell -NoProfile -ExecutionPolicy Bypass -File .\install-protocol.ps1 -Uninstall

  说明：
    - 只写当前用户 HKCU\Software\Classes\pili，不需要管理员权限。
    - 启动器与处理脚本会复制到 %LOCALAPPDATA%\pili4k\bin，安装包可以移动或删除。
    - 所有路径都按脚本自身所在目录定位，支持空格和中文目录。
#>
param(
    [switch]$Uninstall,
    [switch]$Check,
    [switch]$Force,
    [string]$SourceDir = ""
)

$ErrorActionPreference = "Stop"

$ProtocolName = "pili"
$RootKey = "HKCU:\Software\Classes\$ProtocolName"
$InstallDir = Join-Path (Join-Path $env:LOCALAPPDATA "pili4k") "bin"
$Wscript = Join-Path $env:SystemRoot "System32\wscript.exe"

$script:Results = @()

# ---------------------------------------------------------------------------
# 输出与结果收集
# ---------------------------------------------------------------------------
function Write-Banner([string]$Title) {
    Write-Host ""
    Write-Host "==================================================" -ForegroundColor DarkGray
    Write-Host ("  " + $Title) -ForegroundColor Cyan
    Write-Host "==================================================" -ForegroundColor DarkGray
    Write-Host ("  时间: " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + "    用户: " + $env:USERNAME + "    机器: " + $env:COMPUTERNAME) -ForegroundColor DarkGray
    Write-Host ""
}

function Add-Result([string]$Level, [string]$Name, [string]$Detail) {
    $script:Results += [pscustomobject]@{ Level = $Level; Name = $Name; Detail = $Detail }
    $color = "Gray"
    if ($Level -eq "OK") { $color = "Green" }
    elseif ($Level -eq "WARN") { $color = "Yellow" }
    elseif ($Level -eq "FAIL") { $color = "Red" }
    Write-Host ("[{0}] {1}" -f $Level, $Name) -ForegroundColor $color
    if ($Detail) {
        foreach ($line in ($Detail -split "`r?`n")) {
            Write-Host ("       " + $line) -ForegroundColor DarkGray
        }
    }
}

function Get-ScriptDir {
    if ($SourceDir) { return $SourceDir }
    if ($PSScriptRoot) { return $PSScriptRoot }
    $self = $MyInvocation.MyCommand.Path
    if ($self) { return (Split-Path -Parent $self) }
    return (Get-Location).Path
}

function Get-PlayLogPath {
    $dir = $env:BILI4K_LOG_DIR
    if (-not $dir) {
        if ($env:LOCALAPPDATA) { $dir = Join-Path $env:LOCALAPPDATA "pili4k" }
        else { $dir = Join-Path $env:TEMP "pili4k" }
    }
    return (Join-Path $dir "play.log")
}

function Get-MpvConfigPath {
    return (Join-Path (Split-Path -Parent (Get-PlayLogPath)) "mpv-path.txt")
}

# ---------------------------------------------------------------------------
# mpv 检测：找到并能运行 / 找到但打不开 / 完全没找到
# ---------------------------------------------------------------------------
function Get-MpvCandidates {
    $items = New-Object System.Collections.ArrayList
    function Add-Candidate([string]$Path, [string]$Source) {
        if (-not $Path) { return }
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
        $full = (Resolve-Path -LiteralPath $Path).Path
        foreach ($existing in $items) {
            if ($existing.Path -ieq $full) { return }
        }
        [void]$items.Add([pscustomobject]@{ Path = $full; Source = $Source })
    }

    Add-Candidate $env:BILI4K_MPV "BILI4K_MPV 环境变量"
    foreach ($name in @("mpv.exe", "mpv.com")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { Add-Candidate $cmd.Source "PATH" }
    }

    $fixed = @(
        (Join-Path $env:ProgramFiles "mpv\mpv.exe"),
        (Join-Path $env:ProgramFiles "mpv\mpv.com"),
        (Join-Path ${env:ProgramFiles(x86)} "mpv\mpv.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "mpv\mpv.com"),
        (Join-Path (Join-Path $env:LOCALAPPDATA "Programs") "mpv\mpv.exe"),
        (Join-Path (Join-Path $env:USERPROFILE "scoop") "shims\mpv.exe"),
        (Join-Path (Join-Path (Join-Path $env:USERPROFILE "scoop") "apps") "mpv\current\mpv.exe"),
        (Join-Path (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links") "mpv.exe"),
        "C:\ProgramData\chocolatey\bin\mpv.exe"
    )
    foreach ($path in $fixed) { Add-Candidate $path "常见安装位置" }

    # 再看一眼 Program Files / Programs 下名字带 mpv 的目录
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, (Join-Path $env:LOCALAPPDATA "Programs"))) {
        if (-not $base -or -not (Test-Path -LiteralPath $base)) { continue }
        try {
            Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -match "mpv" } |
                ForEach-Object {
                    Add-Candidate (Join-Path $_.FullName "mpv.exe") ("目录扫描：" + $_.FullName)
                }
        } catch { }
    }
    return $items
}

# 真正启动一次 --version：能启动、5 秒内退出、退出码为 0 才算可用
function Test-MpvRun([string]$Path) {
    $outFile = Join-Path $env:TEMP ("pili4k_mpv_out_" + [guid]::NewGuid().ToString("N") + ".txt")
    $errFile = Join-Path $env:TEMP ("pili4k_mpv_err_" + [guid]::NewGuid().ToString("N") + ".txt")
    try {
        $proc = Start-Process -FilePath $Path -ArgumentList "--version" -PassThru -WindowStyle Hidden `
            -RedirectStandardOutput $outFile -RedirectStandardError $errFile -ErrorAction Stop
        if (-not $proc.WaitForExit(5000)) {
            try { $proc.Kill() } catch { }
            return @{ Ok = $false; Detail = "启动后 5 秒内没有退出，可能在等待弹窗或被安全软件拦截" }
        }
        # PS 5.1 里带超时的 WaitForExit 之后 ExitCode 可能还没刷新，再等一次
        $proc.WaitForExit()
        $exitCode = $null
        try { $exitCode = $proc.ExitCode } catch { $exitCode = $null }
        $text = ""
        if (Test-Path -LiteralPath $outFile) { $text += [string](Get-Content -LiteralPath $outFile -Raw -Encoding UTF8 -ErrorAction SilentlyContinue) }
        if (Test-Path -LiteralPath $errFile) { $text += [string](Get-Content -LiteralPath $errFile -Raw -Encoding UTF8 -ErrorAction SilentlyContinue) }
        $firstLine = (($text -split "`r?`n") | Where-Object { $_.Trim() } | Select-Object -First 1)
        if (-not $firstLine) { $firstLine = "" }
        if ($null -ne $exitCode -and $exitCode -ne 0) {
            return @{ Ok = $false; Detail = ("mpv --version 退出码 " + $exitCode + "；输出：" + $firstLine.Trim()) }
        }
        return @{ Ok = $true; Detail = $firstLine.Trim() }
    } catch {
        return @{ Ok = $false; Detail = ("无法启动进程：" + $_.Exception.Message) }
    } finally {
        Remove-Item -LiteralPath $outFile -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $errFile -Force -ErrorAction SilentlyContinue
    }
}

function Test-MpvStatus {
    $candidates = Get-MpvCandidates
    $firstError = $null
    foreach ($item in $candidates) {
        $run = Test-MpvRun $item.Path
        if ($run.Ok) {
            $detail = $item.Path + "（来源：" + $item.Source + "）"
            if ($run.Detail) { $detail += "`r`n版本：" + $run.Detail }
            Add-Result "OK" "mpv 可用" $detail
            return $item.Path
        }
        if (-not $firstError) {
            $firstError = [pscustomobject]@{ Path = $item.Path; Detail = $run.Detail }
        }
    }
    if ($firstError) {
        Add-Result "WARN" "mpv 找到但打不开" (
            $firstError.Path + "`r`n" +
            "原因：" + $firstError.Detail + "`r`n" +
            "排查：1) 先手动运行 `"mpv --version`" 看报错；2) 装 VC++ 2015-2022 x64 运行库；" +
            "3) 32 位系统要用 32 位 mpv；4) 杀软隔离了 mpv.exe；5) 实在不行改用 PotPlayer 代理模式。"
        )
        return $null
    }
    Add-Result "WARN" "没有找到 mpv" (
        "直连播放需要 mpv，协议本身仍可使用（PotPlayer 代理模式不受影响）。`r`n" +
        "安装方式：命令行执行 winget install mpv 或 scoop install mpv，也可直接解压官方压缩包。`r`n" +
        "如果装在非标准目录：把 mpv.exe 的完整路径填到环境变量 BILI4K_MPV，再双击「自检.cmd」。"
    )
    return $false
}

# 把找到的 mpv 路径落盘：运行时不依赖浏览器进程的环境变量快照
function Set-MpvConfig([string]$Path) {
    $configFile = Get-MpvConfigPath
    try {
        $dir = Split-Path -Parent $configFile
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        [IO.File]::WriteAllText($configFile, $Path, (New-Object System.Text.UTF8Encoding($false)))
        Add-Result "OK" "已记录 mpv 路径" ($configFile + "`r`n内容：" + $Path + "`r`n（运行时优先读它，环境变量没生效也能播放）")
    } catch {
        Add-Result "WARN" "写入 mpv 路径失败" ($configFile + "`r`n" + $_.Exception.Message)
    }
}

# ---------------------------------------------------------------------------
# 注册与自检
# ---------------------------------------------------------------------------
function Get-ExpectedCommand([string]$Launcher) {
    return ('"{0}" //nologo "{1}" "%1"' -f $Wscript, $Launcher)
}

function Test-HandlerChain([string]$Launcher) {
    $logFile = Get-PlayLogPath
    $before = [datetime]::MinValue
    if (Test-Path -LiteralPath $logFile) { $before = (Get-Item -LiteralPath $logFile).LastWriteTimeUtc }
    try {
        $probe = '//nologo "{0}" "pili://check?self={1}"' -f $Launcher, [guid]::NewGuid().ToString("N")
        Start-Process -FilePath $Wscript -ArgumentList $probe -Wait -ErrorAction Stop | Out-Null
    } catch {
        return @{ Ok = $false; Detail = ("启动 wscript 失败：" + $_.Exception.Message) }
    }
    # VBS 拉起隐藏的 PowerShell 后立即退出，这里轮询日志（最多 12 秒）
    for ($i = 0; $i -lt 24; $i++) {
        Start-Sleep -Milliseconds 500
        if (-not (Test-Path -LiteralPath $logFile)) { continue }
        if ((Get-Item -LiteralPath $logFile).LastWriteTimeUtc -le $before) { continue }
        $tail = (Get-Content -LiteralPath $logFile -Tail 15 -ErrorAction SilentlyContinue) -join "`n"
        if ($tail -match "CHECK: handler ok") {
            $seconds = [math]::Round(($i + 1) * 0.5, 1)
            return @{ Ok = $true; Detail = ("耗时约 " + $seconds + " 秒，日志：" + $logFile) }
        }
    }
    return @{ Ok = $false; Detail = ("12 秒内日志没有出现 CHECK: handler ok。日志：" + $logFile +
        "`r`n排查：执行策略被组策略限制 / 杀软拦截 powershell.exe / 启动器被杀软隔离。") }
}

function Test-Installation([string]$Handler, [string]$Launcher) {
    # 文件
    $filesOk = $true
    foreach ($path in @($Handler, $Launcher)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            $filesOk = $false
            Add-Result "FAIL" "缺少文件" $path
        }
    }
    if ($filesOk) {
        Add-Result "OK" "安装文件" ($Handler + "`r`n" + $Launcher)
    }

    # 注册表
    $expected = Get-ExpectedCommand $Launcher
    $stored = ""
    $hasUrlProtocol = $false
    try {
        $stored = [string](Get-ItemProperty -LiteralPath ($RootKey + "\shell\open\command") -ErrorAction Stop).'(default)'
        $hasUrlProtocol = ((Get-Item -LiteralPath $RootKey -ErrorAction Stop).Property -contains "URL Protocol")
    } catch { }
    if ($stored -and $stored -eq $expected -and $hasUrlProtocol) {
        Add-Result "OK" "注册表 HKCU\Software\Classes\pili" ("command: " + $stored)
    } elseif ($stored) {
        $detail = "command 与当前文件不一致。`r`n实际：" + $stored + "`r`n应为：" + $expected
        if (-not $hasUrlProtocol) { $detail += "`r`n缺少 `"URL Protocol`" 值。" }
        Add-Result "FAIL" "注册表内容不对" $detail
    } else {
        Add-Result "FAIL" "没有注册 pili://" ("读不到 " + $RootKey + "。请双击「安装.cmd」。")
    }

    # 调用链
    if ($filesOk) {
        $chain = Test-HandlerChain $Launcher
        if ($chain.Ok) { Add-Result "OK" "调用链 wscript → VBS → pili-play.ps1" $chain.Detail }
        else { Add-Result "FAIL" "调用链不通" $chain.Detail }
    }

    # mpv
    $mpvPath = Test-MpvStatus
    if ($mpvPath) { Set-MpvConfig $mpvPath }
}

function Show-Summary([string]$Mode) {
    $fail = @($script:Results | Where-Object { $_.Level -eq "FAIL" }).Count
    $warn = @($script:Results | Where-Object { $_.Level -eq "WARN" }).Count
    Write-Host ""
    Write-Host "==================================================" -ForegroundColor DarkGray
    if ($fail -gt 0) {
        Write-Host ("结论：失败 " + $fail + " 项，安装/检查未通过。") -ForegroundColor Red
        Write-Host "请按上面的 [FAIL] 处理，然后双击「自检.cmd」再试；仍是失败就把本窗口截图。" -ForegroundColor Red
    } elseif ($warn -gt 0) {
        Write-Host ("结论：pili:// 已可用，但有 " + $warn + " 项需要留意。") -ForegroundColor Yellow
        Write-Host "协议注册与启动链路正常；mpv 不可用时直连播放会失败，可先用 PotPlayer 代理模式。" -ForegroundColor Yellow
    } else {
        if ($Mode -eq "check") {
            Write-Host "结论：检查通过，pili:// 可以直接使用。" -ForegroundColor Green
        } else {
            Write-Host "结论：安装成功，pili:// 可以直接使用。" -ForegroundColor Green
        }
        Write-Host "到 B 站视频页点扩展的「打开 mpv 播放 / 直连播放（mpv）」即可。" -ForegroundColor Green
    }
    Write-Host "==================================================" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host ("日志：" + (Get-PlayLogPath))
    Write-Host ("安装位置：" + $InstallDir)
    Write-Host ("注册表：HKCU\Software\Classes\" + $ProtocolName + "（当前用户，无需管理员）")
    Write-Host ""
    if ($fail -gt 0) { exit 1 }
    exit 0
}

# ---------------------------------------------------------------------------
# 卸载
# ---------------------------------------------------------------------------
function Invoke-Uninstall {
    Write-Banner "卸载 pili:// 协议"
    Write-Host ("将删除注册表：HKCU\Software\Classes\" + $ProtocolName)
    Write-Host ("将删除安装目录：" + $InstallDir)
    Write-Host "日志文件会保留（便于排查），卸载完会给出路径。"
    Write-Host ""
    if (-not $Force) {
        $answer = Read-Host "确认卸载请输入 y 后回车（其他任意键取消）"
        if ($answer -notmatch '^(y|Y|yes|YES)$') {
            Write-Host "已取消，没有做任何修改。" -ForegroundColor Yellow
            exit 0
        }
    }

    if (Test-Path -LiteralPath $RootKey) {
        Remove-Item -LiteralPath $RootKey -Recurse -Force
        Add-Result "OK" "已删除注册表" $RootKey
    } else {
        Add-Result "OK" "注册表本来就没有" $RootKey
    }

    if (Test-Path -LiteralPath $InstallDir) {
        Remove-Item -LiteralPath $InstallDir -Recurse -Force
        Add-Result "OK" "已删除安装目录" $InstallDir
    } else {
        Add-Result "OK" "安装目录本来就不存在" $InstallDir
    }

    $configFile = Get-MpvConfigPath
    if (Test-Path -LiteralPath $configFile) {
        Remove-Item -LiteralPath $configFile -Force
        Add-Result "OK" "已删除 mpv 路径记录" $configFile
    }

    if (Test-Path -LiteralPath ("HKLM:\Software\Classes\" + $ProtocolName)) {
        Add-Result "WARN" "HKLM 里还有一份机器级注册" (
            ("HKLM\Software\Classes\" + $ProtocolName + " 存在（可能是更早的机器级安装），删除它需要管理员权限。`r`n" +
             "当前用户已无 HKCU 注册，一般不会再触发；如需彻底清除，用管理员 PowerShell 执行：`r`n" +
             "  Remove-Item -LiteralPath `"HKLM:\Software\Classes\" + $ProtocolName + "`" -Recurse")
        )
    }

    Write-Host ""
    Write-Host "卸载完成。可选清理（不影响系统）：" -ForegroundColor Green
    Write-Host ("  删除日志：" + (Join-Path (Join-Path $env:LOCALAPPDATA "pili4k") "play.log"))
    Write-Host ("  删除整个配置目录：" + (Join-Path $env:LOCALAPPDATA "pili4k"))
    Write-Host ""
    exit 0
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
if ($Uninstall) {
    Invoke-Uninstall
}

if (-not (Test-Path -LiteralPath $Wscript)) {
    Write-Banner "安装 pili:// 协议"
    Add-Result "FAIL" "找不到 wscript.exe" $Wscript
    Show-Summary "install"
}

$source = Get-ScriptDir
$handlerSource = Join-Path $source "pili-play.ps1"
$launcherSource = Join-Path $source "pili-launch.vbs"

if ($Check) {
    Write-Banner "检查 pili:// 安装状态"
    Test-Installation (Join-Path $InstallDir "pili-play.ps1") (Join-Path $InstallDir "pili-launch.vbs")
    Show-Summary "check"
}

Write-Banner "安装 pili:// 协议（当前用户，无需管理员权限）"

foreach ($path in @($handlerSource, $launcherSource)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        Add-Result "FAIL" "安装包不完整，缺少文件" ($path + "`r`n请确认解压完整，不要只复制其中一部分。")
    }
}
if (@($script:Results | Where-Object { $_.Level -eq "FAIL" }).Count -gt 0) {
    Show-Summary "install"
}

# 1. 复制到固定目录：安装包可以移动/删除，注册表不会指向临时位置
try {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    Copy-Item -LiteralPath $handlerSource -Destination (Join-Path $InstallDir "pili-play.ps1") -Force
    Copy-Item -LiteralPath $launcherSource -Destination (Join-Path $InstallDir "pili-launch.vbs") -Force
} catch {
    Add-Result "FAIL" "复制文件失败" ($_.Exception.Message + "`r`n目标：" + $InstallDir)
    Show-Summary "install"
}

$handler = Join-Path $InstallDir "pili-play.ps1"
$launcher = Join-Path $InstallDir "pili-launch.vbs"
Add-Result "OK" "已复制安装文件" ("pili-play.ps1`r`npili-launch.vbs`r`n目录：" + $InstallDir)

# 2. 写注册表（HKCU，不需要管理员）
try {
    New-Item -Path $RootKey -Force | Out-Null
    Set-Item -Path $RootKey -Value "URL:Bili 4K Play Protocol"
    New-ItemProperty -Path $RootKey -Name "URL Protocol" -Value "" -PropertyType String -Force | Out-Null
    New-Item -Path ($RootKey + "\DefaultIcon") -Force | Out-Null
    Set-Item -Path ($RootKey + "\DefaultIcon") -Value "$env:SystemRoot\System32\shell32.dll,1"
    New-Item -Path ($RootKey + "\shell\open\command") -Force | Out-Null
    Set-Item -Path ($RootKey + "\shell\open\command") -Value (Get-ExpectedCommand $launcher)
} catch {
    Add-Result "FAIL" "写注册表失败" ($_.Exception.Message + "`r`n如果注册表被安全软件锁定，请放行后重试。")
    Show-Summary "install"
}
Add-Result "OK" "已注册 pili://" ("HKCU\Software\Classes\" + $ProtocolName + "`r`ncommand: " + (Get-ExpectedCommand $launcher))

# 3. 自检
Test-Installation $handler $launcher
Show-Summary "install"
