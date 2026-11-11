# Runs sing-box only while the tunnel works end-to-end, and stops it after a sustained failure.
# Two probes: the SOCKS probe tests client+server+origin, the plain probe tests the tunnel itself.
# ASCII only: Windows PowerShell 5.1 misreads UTF-8 files that have no BOM.
param(
    [string]$ProbeUrl = 'https://example.com/',
    [int]$SocksPort = 1080,
    [int]$ProbeSeconds = 4,
    [int]$CheckSeconds = 5,
    [int]$StartAfter = 3,
    [int]$StopAfter = 5,
    [int]$CooldownSeconds = 20,
    [string]$LogPath = 'tun-watch.log'
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Log([string]$Message) {
    if ((Test-Path $LogPath) -and (Get-Item $LogPath).Length -gt 524288) {
        Move-Item -Force $LogPath ($LogPath + '.old')
    }
    Add-Content -Path $LogPath -Value ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message)
}

function Invoke-Curl([string[]]$ExtraArgs) {
    if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) { return $null }
    $arguments = @('--silent', '--max-time', "$ProbeSeconds", '-o', 'NUL', '-w', '%{http_code}') + $ExtraArgs + @($ProbeUrl)
    return (curl.exe @arguments)
}

function Test-Socks {
    $code = Invoke-Curl @("--socks5-hostname=127.0.0.1:$SocksPort")
    if ($null -eq $code) { Log 'curl.exe not available: cannot probe the tunnel'; return $false }
    return $code -eq '200'
}

# Without --socks the request follows the default route, i.e. it goes through the TUN.
function Test-Tunnel {
    return (Invoke-Curl @()) -eq '200'
}

function Start-Tun {
    # sing-box has no log rotation of its own.
    $singLog = Join-Path $PSScriptRoot 'sing-box.log'
    if ((Test-Path $singLog) -and (Get-Item $singLog).Length -gt 2097152) {
        Move-Item -Force $singLog ($singLog + '.old')
    }
    $proc = Start-Process -FilePath (Join-Path $PSScriptRoot 'sing-box.exe') -ArgumentList 'run', '-c', 'config.json' `
        -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
    Log "sing-box started pid=$($proc.Id)"
    return $proc
}

$proc = $null
$good = 0
$bad = 0
$cooldown = Get-Date
Log "watchdog start probe=$ProbeUrl socks=$SocksPort start_after=$StartAfter stop_after=$StopAfter"
while ($true) {
    $socksOk = Test-Socks
    $tunOk = if ($proc) { Test-Tunnel } else { $socksOk }
    if ($tunOk) {
        $good++
        $bad = 0
    } else {
        $bad++
        $good = 0
    }

    if ($proc -and $proc.HasExited) {
        Log "sing-box exited code=$($proc.ExitCode)"
        $proc = $null
        $cooldown = (Get-Date).AddSeconds($CooldownSeconds)
    }

    # Hysteresis in both directions: one lucky probe must not start the tunnel,
    # and a short blip must not tear it down (that would kill every app connection).
    if (-not $proc -and $socksOk -and $good -ge $StartAfter -and (Get-Date) -ge $cooldown) {
        $proc = Start-Tun
        $good = 0
    }
    if ($proc -and $bad -ge $StopAfter) {
        Log "tunnel failed $bad checks in a row: stopping sing-box pid=$($proc.Id)"
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        $proc = $null
        $bad = 0
        $cooldown = (Get-Date).AddSeconds($CooldownSeconds)
    }

    Start-Sleep -Seconds $CheckSeconds
}
