# Keeps the dualproxy client running. The client discovers network paths itself,
# so a new Wi-Fi path only adds links: the client is never restarted for a path change.
# ASCII only: Windows PowerShell 5.1 misreads UTF-8 files that have no BOM.
param(
    [string]$Server = '192.168.3.22',
    [int]$ServerPort = 7443,
    [int]$SocksPort = 1080,
    [string]$Python = '',
    [int]$CheckSeconds = 10,
    [string]$LogPath = 'client.log'
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$ReportLog = 'client.report.log'

function Log([string]$Message) {
    if ((Test-Path $LogPath) -and (Get-Item $LogPath).Length -gt 524288) {
        Move-Item -Force $LogPath ($LogPath + '.old')
    }
    Add-Content -Path $LogPath -Value ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message)
}

function Get-PythonPath {
    if ($Python) { return $Python }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $cmd) { return $null }
    return $cmd.Source
}

function Test-Port([string]$TargetHost, [int]$Port, [int]$TimeoutMs) {
    $tcp = New-Object System.Net.Sockets.TcpClient
    try {
        return $tcp.ConnectAsync($TargetHost, $Port).Wait($TimeoutMs) -and $tcp.Connected
    } catch {
        return $false
    } finally {
        $tcp.Close()
    }
}

function Connect-Wifi {
    # Match the wireless adapter by media type, not by a hardcoded adapter name.
    $adapter = Get-NetAdapter -ErrorAction SilentlyContinue |
        Where-Object { $_.MediaType -eq 'Native 802.11' } | Select-Object -First 1
    if (-not $adapter) { Log 'no wireless adapter found'; return }
    if ($adapter.Status -eq 'Up') { return }
    $name = Get-WifiProfileName
    if (-not $name) { Log 'no saved wireless profile'; return }
    netsh wlan connect name="$name" | Out-Null
    Log "wifi reconnect requested: $name"
}

function Get-WifiProfileName {
    # netsh prints its labels in the console's language ("All User Profile" vs a Japanese label
    # under code page 932, which is what a scheduled task gets), so match the layout instead:
    # profile entries are the indented "label : name" lines.
    $line = netsh wlan show profiles 2>$null | Where-Object { $_ -match '^\s{4}\S.*\s:\s(.+)$' } |
        Select-Object -First 1
    if (-not $line) { return $null }
    return ($line -replace '^\s{4}\S.*\s:\s', '').Trim()
}

function Stop-StrayClients {
    $strays = Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*dualproxy.py client*' -and $_.CommandLine -like "*--socks-port $SocksPort*" }
    foreach ($proc in $strays) {
        Log ("killing stray client pid=$($proc.ProcessId)")
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Start-Client([string]$Interpreter) {
    $arguments = 'dualproxy.py client {0} --port {1} --secret-file dualproxy.key --socks-port {2}' -f $Server, $ServerPort, $SocksPort
    $client = Start-Process $Interpreter -ArgumentList $arguments -PassThru -WindowStyle Hidden `
        -WorkingDirectory $PSScriptRoot -RedirectStandardOutput $ReportLog -RedirectStandardError ($LogPath + '.err')
    Log "client started pid=$($client.Id) socks=$SocksPort server=$Server"
    return $client
}

$interpreter = Get-PythonPath
if (-not $interpreter) {
    Log 'python.exe not found: pass -Python <absolute path> or install python'
    exit 1
}
Log "supervisor start server=$Server socks=$SocksPort python=$interpreter"
Stop-StrayClients

$client = $null
$backoff = 5
while ($true) {
    Connect-Wifi
    if ((Test-Path $ReportLog) -and (Get-Item $ReportLog).Length -gt 1048576) {
        Set-Content -Path $ReportLog -Value ''
    }
    if ($client) {
        if ($client.HasExited) {
            Log "client exited code=$($client.ExitCode); restarting in $backoff s"
            $client = $null
            Start-Sleep -Seconds $backoff
            $backoff = [Math]::Min($backoff * 2, 60)
        }
    } elseif (Test-Port $Server $ServerPort 2000) {
        $client = Start-Client $interpreter
        $backoff = 5
    } else {
        Log 'server not reachable yet; waiting'
    }
    Start-Sleep -Seconds $CheckSeconds
}
