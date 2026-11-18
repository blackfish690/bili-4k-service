param(
    [Parameter(Mandatory = $true)]
    [string]$Uri
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# 日志：每次调用都记录「原始 URI / 解析结果 / 后端响应摘要 / 最终命令行 / 退出码」
# 位置：%LOCALAPPDATA%\pili4k\play.log
# 可用环境变量 BILI4K_LOG_DIR 覆盖目录；mpv 自己的日志写在同目录 mpv.log
# ---------------------------------------------------------------------------
$LogDir = $env:BILI4K_LOG_DIR
if (-not $LogDir) {
    if ($env:LOCALAPPDATA) { $LogDir = Join-Path $env:LOCALAPPDATA "pili4k" }
    else { $LogDir = Join-Path $env:TEMP "pili4k" }
}
$LogFile = Join-Path $LogDir "play.log"
$MpvLog = Join-Path $LogDir "mpv.log"
# 安装器把检测到的 mpv 路径写在这里，运行时不依赖浏览器进程的环境变量快照
$MpvConfig = Join-Path $LogDir "mpv-path.txt"

try { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null } catch { }

function Write-Log([string]$Message) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff"
    try {
        if ((Test-Path -LiteralPath $LogFile) -and ((Get-Item -LiteralPath $LogFile).Length -gt 1MB)) {
            Move-Item -LiteralPath $LogFile -Destination ($LogFile + ".1") -Force
        }
    } catch {
        # 轮转失败不影响写入
    }
    try {
        Add-Content -LiteralPath $LogFile -Value ("{0} {1}" -f $stamp, $Message) -Encoding UTF8
    } catch {
        # 日志失败绝不能影响播放
    }
}

function Show-Error([string]$Message) {
    Write-Log ("ERROR: " + ($Message -replace "`r?`n", " | "))
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        [System.Windows.Forms.MessageBox]::Show(
            ($Message + "`r`n`r`n日志: " + $LogFile),
            "Bili 4K Play",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Warning
        ) | Out-Null
    } catch {
        Write-Log ("ERROR: 弹窗失败（可能处于非交互会话）: " + $_.Exception.Message)
    }
}

# 任何未捕获异常都要留痕 + 弹窗，避免「点了没反应」
trap {
    Write-Log ("FATAL: " + $_.Exception.Message)
    try {
        Write-Log ("FATAL-TRACE: " + ((($_.ScriptStackTrace) | Out-String).Trim() -replace "`r?`n", " | "))
    } catch { }
    Show-Error ("脚本异常退出：" + $_.Exception.Message + "`r`n请把日志发出来排查。")
    exit 99
}

# ---------------------------------------------------------------------------
# 可覆盖的播放器路径（也可用环境变量 BILI4K_POTPLAYER / BILI4K_MPV 指定）
# ---------------------------------------------------------------------------
$PotPlayerPath = [string]$env:BILI4K_POTPLAYER
$MpvPath = [string]$env:BILI4K_MPV

function Test-Exe([string]$Path) {
    return ($Path -and (Test-Path -LiteralPath $Path))
}

# ---------------------------------------------------------------------------
# 定位 PotPlayer
# ---------------------------------------------------------------------------
function Find-PotPlayer {
    if (Test-Exe $PotPlayerPath) {
        return $PotPlayerPath
    }
    $candidates = @(
        "$env:ProgramFiles\DAUM\PotPlayer\PotPlayerMini64.exe",
        "${env:ProgramFiles(x86)}\DAUM\PotPlayer\PotPlayerMini.exe",
        "$env:LOCALAPPDATA\Programs\DAUM\PotPlayer\PotPlayerMini64.exe",
        "C:\Program Files\DAUM\PotPlayer\PotPlayerMini64.exe",
        "C:\Program Files (x86)\DAUM\PotPlayer\PotPlayerMini.exe",
        "C:\Program Files\Potplayer\PotPlayerMini64.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Exe $candidate) {
            Write-Log ("PotPlayer: " + $candidate)
            return $candidate
        }
    }
    Write-Log "PotPlayer: not found"
    return ""
}

# ---------------------------------------------------------------------------
# 定位 mpv（直连模式用）
# ---------------------------------------------------------------------------
function Find-Mpv {
    if (Test-Exe $MpvPath) {
        Write-Log ("mpv: " + $MpvPath + " (from BILI4K_MPV)")
        return $MpvPath
    }
    # 次选：安装器写入的配置文件（跨进程可靠，不受环境变量快照影响）
    if (Test-Path -LiteralPath $MpvConfig) {
        $saved = ""
        try { $saved = ([string](Get-Content -LiteralPath $MpvConfig -TotalCount 1 -Encoding UTF8)).Trim() } catch { }
        if ($saved -and (Test-Exe $saved)) {
            Write-Log ("mpv: " + $saved + " (from mpv-path.txt)")
            return $saved
        }
        Write-Log ("mpv: mpv-path.txt 内容不可用：" + $saved)
    }
    $cmd = Get-Command mpv -ErrorAction SilentlyContinue
    if ($cmd) {
        Write-Log ("mpv: " + $cmd.Source + " (from PATH)")
        return $cmd.Source
    }
    $candidates = @(
        "$env:ProgramFiles\mpv\mpv.exe",
        "${env:ProgramFiles(x86)}\mpv\mpv.exe",
        "$env:LOCALAPPDATA\Programs\mpv\mpv.exe",
        "$env:USERPROFILE\scoop\shims\mpv.exe",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links\mpv.exe",
        "C:\Program Files\mpv\mpv.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Exe $candidate) {
            Write-Log ("mpv: " + $candidate)
            return $candidate
        }
    }
    Write-Log ("mpv: not found; BILI4K_MPV='" + $env:BILI4K_MPV + "'; config=" + $MpvConfig + "; PATH=" + $env:PATH)
    return ""
}

function New-Line([string[]]$Items) {
    return (($Items | ForEach-Object { '"' + ([string]$_ -replace '"', '\"') + '"' }) -join ' ')
}

# 把后端 probe 明细压成一行：mcdn(ok 120ms)、upos-sz-302ppio(HTTP 403)
function Format-Probe($Items) {
    $list = @($Items)
    if ($list.Count -eq 0) { return "<none>" }
    return (($list | ForEach-Object {
        if ($_.ok) { "{0}(ok {1}ms)" -f $_.host, $_.ms }
        else { "{0}({1})" -f $_.host, $_.error }
    }) -join "、")
}

# ---------------------------------------------------------------------------
# 会话头：先记录，再解析，任何失败都能对照原始参数
# ---------------------------------------------------------------------------
Write-Log "==================== invoke ===================="
Write-Log ("RAW URI: " + $Uri)
try {
    Write-Log ("ENV: ps={0} os={1} policy={2} user={3} cwd={4}" -f `
        $PSVersionTable.PSVersion, [Environment]::OSVersion.VersionString, `
        (Get-ExecutionPolicy), $env:USERNAME, (Get-Location))
} catch {
    Write-Log ("ENV: 读取失败: " + $_.Exception.Message)
}

# 自检入口：install-protocol.ps1 用它验证「注册表 → powershell → 脚本」整条链路
if ($Uri -match '^pili://check') {
    $probe = Find-Mpv
    Write-Log ("CHECK: handler ok; mpv=" + $(if ($probe) { $probe } else { "<not found>" }))
    exit 0
}

# ---------------------------------------------------------------------------
# 解析 pili://play?...
#   pili://play?url=<mpd>                     → PotPlayer 打开 MPD（代理模式）
#   pili://play?mode=mpv&api=<direct api url> → mpv 直连视频+音频（直连模式）
#
# 注意：api / url 都是「取到字符串末尾」，因为扩展用 encodeURIComponent 编码，
# 其中的 & 已是 %26；万一某处漏编码，这样也能把整条 URL 取回来而不是被截断。
# ---------------------------------------------------------------------------
$query = ""
if ($Uri -match '^pili://play/?\?(.+)$') {
    $query = $Matches[1]
} else {
    Show-Error ("无法识别的 pili:// 地址，应为 pili://play?url=... 或 pili://play?mode=mpv&api=...`r`n收到: " + $Uri)
    exit 1
}
# 个别浏览器会在末尾补一个 /
if ($query.EndsWith("/")) {
    $query = $query.Substring(0, $query.Length - 1)
    Write-Log "PARSE: 去掉结尾多余的 /"
}

$params = @{}
foreach ($pair in ($query -split '&')) {
    $kv = $pair -split '=', 2
    if ($kv.Count -eq 2 -and -not $params.ContainsKey($kv[0])) {
        $params[$kv[0]] = [System.Uri]::UnescapeDataString($kv[1])
    }
}
if ($query -match '(?:^|&)api=(.+)$') {
    $params["api"] = [System.Uri]::UnescapeDataString($Matches[1])
}
if ($query -match '(?:^|&)url=(.+)$') {
    $params["url"] = [System.Uri]::UnescapeDataString($Matches[1])
}

$mode = "potplayer"
if ($params.ContainsKey("mode") -and $params["mode"]) {
    $mode = $params["mode"].Trim().ToLower()
}
Write-Log ("PARSE: keys=[" + (($params.Keys | Sort-Object) -join ",") + "] mode=" + $mode)
if ($params.ContainsKey("api")) { Write-Log ("PARSE: api=" + $params["api"]) }
if ($params.ContainsKey("url")) { Write-Log ("PARSE: url=" + $params["url"]) }

# 视频标题：扩展从后端 video.title 透传，用于 --force-media-title（mpv 窗口/OSC 顶部）
# 缺省时保持原行为（显示 URL）；只做最小转义，避免引号、换行、结尾反斜杠破坏命令行
function ConvertTo-MpvTitle([string]$Text) {
    if (-not $Text) { return "" }
    $clean = ($Text -replace '[\x00-\x1F\x7F]+', ' ') -replace '"', "'"
    $clean = $clean.Trim()
    # 结尾反斜杠会转义掉 New-Line 补的引号
    $clean = $clean -replace '\\+$', ''
    return $clean.Trim()
}

$mediaTitle = ConvertTo-MpvTitle ([string]$params["title"])
if ($mediaTitle) { Write-Log ("TITLE: " + $mediaTitle) } else { Write-Log "TITLE: <none>" }

# ---------------------------------------------------------------------------
# 去重与单实例控制（防止连点拉起多个播放器）
#   - 目标签名：直连用 api URL，代理用播放 URL。换清晰度/换视频签名不同。
#   - 5 秒冷却标记：同一点击在上一次启动成功后又来一次，直接忽略。
#   - 命名互斥体：只覆盖「再查标记 → 启动进程 → 写标记」这一小段，
#     避免两个启动过程同时通过检查；不会长时间挡住下一次正常播放。
#   - 3 秒内启动失败会删除标记，允许立刻重试。
# ---------------------------------------------------------------------------
$guardSig = ""
if ($mode -eq "mpv" -and $params.ContainsKey("api")) {
    $guardSig = "mpv|" + [string]$params["api"]
} elseif ($params.ContainsKey("url")) {
    $guardSig = "potplayer|" + [string]$params["url"]
}
$guardFile = Join-Path $LogDir "launch-guard.txt"
$launchLock = $null
$launchLockHeld = $false

function Test-LaunchMark {
    if (-not $guardSig) { return $false }
    try {
        if (-not (Test-Path -LiteralPath $guardFile)) { return $false }
        $raw = ([string](Get-Content -LiteralPath $guardFile -Raw -ErrorAction Stop)).Trim()
        $idx = $raw.IndexOf("|")
        if ($idx -le 0) { return $false }
        $oldTs = [long]0
        if (-not [long]::TryParse($raw.Substring(0, $idx), [ref]$oldTs)) { return $false }
        if ($raw.Substring($idx + 1) -ne $guardSig) { return $false }
        return (([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - $oldTs) -lt 5)
    } catch {
        return $false
    }
}

function Save-LaunchMark {
    if (-not $guardSig) { return }
    try {
        $ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        Set-Content -LiteralPath $guardFile -Value ("{0}|{1}" -f $ts, $guardSig) -Encoding UTF8 -NoNewline
    } catch {
        Write-Log ("DEDUPE: 写冷却标记失败: " + $_.Exception.Message)
    }
}

function Clear-LaunchMark {
    try {
        if (Test-Path -LiteralPath $guardFile) {
            Remove-Item -LiteralPath $guardFile -Force -ErrorAction Stop
        }
    } catch { }
}

function Enter-LaunchLock {
    try {
        $script:launchLock = New-Object System.Threading.Mutex($false, "Local\Bili4kPlayLaunch")
        try {
            $script:launchLockHeld = $script:launchLock.WaitOne(0)
        } catch [System.Threading.AbandonedMutexException] {
            $script:launchLockHeld = $true
        }
    } catch {
        Write-Log ("DEDUPE: 互斥体不可用，跳过启动锁: " + $_.Exception.Message)
        $script:launchLockHeld = $true
    }
    return [bool]$script:launchLockHeld
}

function Release-LaunchLock {
    if ($script:launchLockHeld) {
        try { $script:launchLock.ReleaseMutex() } catch { }
        $script:launchLockHeld = $false
    }
}

# 快速路径：同一次点击已经启动过，连后端都不必再请求
if (Test-LaunchMark) {
    Write-Log "DEDUPE: 同一目标 5 秒内已启动，忽略本次调用"
    exit 0
}

# ---------------------------------------------------------------------------
# 直连模式：向后端要视频/音频直链，用 edl:// 交给 mpv
# ---------------------------------------------------------------------------
if ($mode -eq "mpv") {
    $api = [string]$params["api"]
    if (-not $api -or $api -notmatch '^https?://') {
        Show-Error ("缺少或非法的 api 参数（应以 http:// 或 https:// 开头）。`r`n收到: " + $Uri)
        exit 1
    }

    $mpv = Find-Mpv
    if (-not $mpv) {
        Show-Error @"
未找到 mpv，无法直连播放（PotPlayer 不能同时接收独立的视频/音频地址）。

安装后重试：
    winget install mpv

或自行下载 https://mpv.io/installation/ ，然后设置环境变量 BILI4K_MPV 指向 mpv.exe，
也可在 $PSCommandPath 顶部填写 `$MpvPath。
如果环境变量已经设置但仍报这个错：把 mpv.exe 的完整路径写进
$MpvConfig （一行，就是路径），或者重新双击安装包里的「安装.cmd」。
"@
        exit 2
    }

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $resp = Invoke-RestMethod -Uri $api -TimeoutSec 30
    } catch {
        $sw.Stop()
        $status = ""
        try {
            if ($_.Exception.Response) { $status = "HTTP " + [int]$_.Exception.Response.StatusCode }
        } catch { }
        $detail = [string]$_.Exception.Message
        try {
            if ($_.ErrorDetails.Message) { $detail = $detail + " | body=" + $_.ErrorDetails.Message }
        } catch { }
        Write-Log ("BACKEND FAIL: {0} after {1}ms" -f $status, $sw.ElapsedMilliseconds)
        Show-Error ("无法访问后端（" + $status + "）。`r`n" + $detail + "`r`n`r`nAPI: " + $api)
        exit 3
    }
    $sw.Stop()

    if (-not $resp.ok) {
        $code = ""
        $msg = ""
        if ($resp.error) {
            $code = [string]$resp.error.code
            $msg = [string]$resp.error.message
        }
        Write-Log ("BACKEND REFUSED: code={0} msg={1}" -f $code, $msg)
        Show-Error ("后端拒绝生成直链：" + $msg + "`r`n`r`ncode=" + $code + "`r`nAPI: " + $api)
        exit 4
    }

    $data = $resp.data
    $selected = $data.selected
    $direct = $data.direct
    $dok = ""
    if ($direct) { $dok = [string]$direct.ok }
    Write-Log ("BACKEND OK: {0}ms qn={1} label={2} direct.ok={3} expires_in={4}" -f `
        $sw.ElapsedMilliseconds, $selected.qn, $selected.label, $dok, $direct.expires_in)
    if ($data.requested_qn -ne $null) {
        Write-Log ("BACKEND QN: requested={0} effective={1} actual={2}" -f `
            $data.requested_qn, $data.effective_qn, $data.actual_qn)
    }
    if ($data.trial) {
        Write-Log ("BACKEND TRIAL: able={0} remaining={1} time_length={2}" -f `
            $data.trial.trial_able, $data.trial.remaining_times, $data.trial.time_length)
    }
    if ($data.qualities) {
        $qtext = (@($data.qualities) | ForEach-Object {
            $flag = "-"
            if ($_.available) { $flag = "+" }
            "{0}{1}{2}" -f $_.qn, $flag, $_.label
        }) -join " "
        Write-Log ("BACKEND QUALITIES: " + $qtext)
    }
    if ($data.notice) { Write-Log ("BACKEND NOTICE: " + $data.notice) }
    if ($direct -and $direct.probe) {
        Write-Log ("BACKEND PROBE video: " + (Format-Probe $direct.probe.video))
        Write-Log ("BACKEND PROBE audio: " + (Format-Probe $direct.probe.audio))
    }

    if (-not $direct -or $direct.ok -eq $false) {
        $why = [string]$data.notice
        if (-not $why) { $why = "后端未给出原因（可能是旧版本后端，请更新镜像）" }
        $probeText = ""
        if ($direct -and $direct.probe) {
            $probeText = "`r`n`r`n节点探活：`r`n  视频: " + (Format-Probe $direct.probe.video) +
                         "`r`n  音频: " + (Format-Probe $direct.probe.audio)
        }
        $trialText = ""
        if ($data.trial) {
            $trialText = "`r`n`r`n试看: able=" + $data.trial.trial_able +
                         " 剩余=" + $data.trial.remaining_times + " 次"
        }
        Show-Error ("该档位无法直连播放：`r`n  清晰度: qn=" + $selected.qn + " " + $selected.label +
            "`r`n  原因: " + $why + $probeText + $trialText +
            "`r`n`r`n建议：换 4K 档位重试；仍不行请把日志发出来。`r`nAPI: " + $api)
        exit 5
    }
    if (-not $direct.video_url) {
        Show-Error "后端返回的 direct 数据缺少 video_url，无法播放。"
        exit 5
    }

    # 优先使用后端拼好的参数（避免两处逻辑漂移），缺失时本地兜底
    # App 流的 referer 为空（后端不再给 upos-sz 发 B 站 Referer），此时不传 --referrer
    $common = @()
    if ([string]$direct.referer) {
        $common += ("--referrer=" + [string]$direct.referer)
    }
    $common += @(
        ("--user-agent=" + [string]$direct.user_agent),
        "--cache=yes",
        "--demuxer-max-bytes=512MiB",
        "--audio-file-auto=no"
    )
    $argList = @()
    if ($data.mpv -and $data.mpv.args) {
        $argList = @($data.mpv.args | ForEach-Object { [string]$_ })
    } elseif ($data.mpv -and $data.mpv.edl) {
        $argList = $common + @([string]$data.mpv.edl)
    } elseif ($direct.audio_url) {
        $argList = $common + @([string]$direct.video_url, ("--audio-file=" + [string]$direct.audio_url))
    } else {
        $argList = $common + @([string]$direct.video_url)
    }
    # mpv 自身日志，便于定位「启动即退出」
    if (-not ($argList | Where-Object { $_ -like "--log-file=*" })) {
        $argList = @("--log-file=$MpvLog") + $argList
    }

    $line = New-Line $argList
    if ($line.Length -gt 30000) {
        Write-Log ("WARN: 命令行过长（{0} 字符），改用 --audio-file" -f $line.Length)
        $argList = @("--log-file=$MpvLog") + $common
        if ($direct.audio_url) {
            $argList += @([string]$direct.video_url, ("--audio-file=" + [string]$direct.audio_url))
        } else {
            $argList += @([string]$direct.video_url)
        }
        $line = New-Line $argList
        if ($line.Length -gt 30000) {
            Show-Error ("命令行仍然过长（" + $line.Length + " 字符），超过 Windows 上限 32767。`r`n请改用代理模式。")
            exit 6
        }
    }

    # 标题统一在最终参数上注入：后端若直接给了 mpv.args，$common 并不参与
    if ($mediaTitle -and -not ($argList | Where-Object { $_ -like "--force-media-title=*" })) {
        $withTitle = @("--force-media-title=" + $mediaTitle) + $argList
        $titleLine = New-Line $withTitle
        if ($titleLine.Length -le 32000) {
            $argList = $withTitle
            $line = $titleLine
        } else {
            Write-Log "WARN: 加入标题后命令行过长，已省略 --force-media-title"
        }
    }
    Write-Log ("CMD({0} args, {1} chars): {2}" -f $argList.Count, $line.Length, $line)

    # 启动前加锁并复查标记：两个进程同时走到这里时，只有一个能真正拉起播放器
    if (-not (Enter-LaunchLock)) {
        Write-Log "DEDUPE: 另一个启动过程正在进行，忽略本次调用"
        exit 0
    }
    if (Test-LaunchMark) {
        Release-LaunchLock
        Write-Log "DEDUPE: 同一目标刚启动过，忽略本次调用"
        exit 0
    }

    $proc = $null
    try {
        $proc = Start-Process -FilePath $mpv -ArgumentList $line -PassThru -ErrorAction Stop
    } catch {
        Release-LaunchLock
        Write-Log ("START FAIL: " + $_.Exception.Message)
        Show-Error ("启动 mpv 失败：" + $_.Exception.Message + "`r`n`r`nmpv: " + $mpv + "`r`n命令: " + $line)
        exit 7
    }
    Write-Log ("STARTED: pid=" + $proc.Id + " qn=" + $selected.qn)
    Save-LaunchMark
    Release-LaunchLock

    # 启动后立即退出是最常见的失败形态（参数/EDL/解码器问题），必须抓出来
    Start-Sleep -Seconds 3
    if ($proc.HasExited) {
        $code = $proc.ExitCode
        $tail = ""
        try {
            if (Test-Path -LiteralPath $MpvLog) {
                $tail = (Get-Content -LiteralPath $MpvLog -Tail 15 -ErrorAction SilentlyContinue) -join "`r`n"
            }
        } catch { }
        Write-Log ("EXITED within 3s: code=" + $code)
        if ($code -ne 0) {
            # 启动失败：撤掉冷却标记，允许用户立刻重试
            Clear-LaunchMark
            Show-Error ("mpv 启动后立即退出（退出码 " + $code + "）。`r`n`r`n--- mpv.log 尾部 ---`r`n" + $tail + "`r`n`r`n完整日志: " + $MpvLog)
            exit 8
        }
    } else {
        Write-Log "mpv 运行中（3 秒后仍未退出）"
    }
    exit 0
}

# ---------------------------------------------------------------------------
# 代理模式：PotPlayer 打开后端生成的分片 MPD
# ---------------------------------------------------------------------------
$target = [string]$params["url"]
if (-not $target) {
    Show-Error ("缺少 url 参数。`r`n收到: " + $Uri)
    exit 1
}

if ($target -notmatch '^https?://') {
    Show-Error ("只允许 http/https 播放地址。`r`n收到: " + $target)
    exit 1
}

Write-Log ("PROXY target: " + $target)

$pot = Find-PotPlayer
if ($pot) {
    $line = New-Line @($target)
    Write-Log ("CMD: " + $pot + " " + $line)
    if (-not (Enter-LaunchLock)) {
        Write-Log "DEDUPE: 另一个启动过程正在进行，忽略本次调用"
        exit 0
    }
    if (Test-LaunchMark) {
        Release-LaunchLock
        Write-Log "DEDUPE: 同一目标刚启动过，忽略本次调用"
        exit 0
    }
    try {
        $proc = Start-Process -FilePath $pot -ArgumentList $line -PassThru -ErrorAction Stop
        Write-Log ("STARTED PotPlayer: pid=" + $proc.Id)
        Save-LaunchMark
        Release-LaunchLock
        exit 0
    } catch {
        Release-LaunchLock
        Write-Log ("START FAIL PotPlayer: " + $_.Exception.Message)
        Show-Error ("启动 PotPlayer 失败：" + $_.Exception.Message + "`r`n`r`n路径: " + $pot)
        exit 7
    }
}

$mpv = Find-Mpv
if ($mpv) {
    $line = New-Line @("--demuxer-lavf-o=protocol_whitelist=file,http,https,tcp,tls", $target)
    Write-Log ("PotPlayer 未找到，改用 mpv: " + $line)
    if (-not (Enter-LaunchLock)) {
        Write-Log "DEDUPE: 另一个启动过程正在进行，忽略本次调用"
        exit 0
    }
    if (Test-LaunchMark) {
        Release-LaunchLock
        Write-Log "DEDUPE: 同一目标刚启动过，忽略本次调用"
        exit 0
    }
    try {
        $proc = Start-Process -FilePath $mpv -ArgumentList $line -PassThru -ErrorAction Stop
        Write-Log ("STARTED mpv: pid=" + $proc.Id)
        Save-LaunchMark
        Release-LaunchLock
        exit 0
    } catch {
        Release-LaunchLock
        Write-Log ("START FAIL mpv: " + $_.Exception.Message)
        Show-Error ("启动 mpv 失败：" + $_.Exception.Message)
        exit 7
    }
}

Show-Error @"
未找到 PotPlayer，也未找到 mpv。

装一个即可（任选）：
    winget install PotPlayer
    winget install mpv

也可用环境变量 BILI4K_POTPLAYER / BILI4K_MPV 指定可执行文件路径。
手动播放地址：$target
"@
exit 2
