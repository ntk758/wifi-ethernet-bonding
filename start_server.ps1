# Keeps the dualproxy server running on the home server PC. Restart with backoff, log events.
# ASCII only: Windows PowerShell 5.1 misreads UTF-8 files that have no BOM.
param(
    [string]$Listen = '0.0.0.0',
    [int]$Port = 7443,
    [string]$Python = '',
    [string]$SecretFile = 'dualproxy.key',
    [int]$CheckSeconds = 10,
    [string]$LogPath = 'server.log'
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$ReportLog = 'server.report.log'

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

$interpreter = Get-PythonPath
if (-not $interpreter) {
    Log 'python.exe not found: pass -Python <absolute path> or install python'
    exit 1
}
if (-not (Test-Path $SecretFile)) {
    Log "missing shared key: $SecretFile (copy dualproxy.key from the PC)"
    exit 1
}
Log "supervisor start listen=$Listen port=$Port key=$SecretFile python=$interpreter"

$server = $null
$backoff = 5
while ($true) {
    if ($server) {
        if ($server.HasExited) {
            Log "server exited code=$($server.ExitCode); restarting in $backoff s"
            $server = $null
            Start-Sleep -Seconds $backoff
            $backoff = [Math]::Min($backoff * 2, 60)
        }
    } else {
        $arguments = 'dualproxy.py server --listen {0} --port {1} --secret-file {2}' -f $Listen, $Port, $SecretFile
        $server = Start-Process $interpreter -ArgumentList $arguments -PassThru -WindowStyle Hidden `
            -WorkingDirectory $PSScriptRoot -RedirectStandardOutput $ReportLog -RedirectStandardError ($LogPath + '.err')
        Log "server started pid=$($server.Id) port=$Port"
        $backoff = 5
    }
    Start-Sleep -Seconds $CheckSeconds
}
