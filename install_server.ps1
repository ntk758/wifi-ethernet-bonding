# Run from an elevated PowerShell on the SERVER PC. Registers the server supervisor at startup.
# Keep these files in a folder readable by SYSTEM (e.g. C:\dualproxy), not in a user profile.
# ASCII only: Windows PowerShell 5.1 misreads UTF-8 files that have no BOM.
param(
    [string]$Listen = '0.0.0.0',
    [int]$Port = 7443,
    [string]$SecretFile = 'dualproxy.key'
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$interpreter = (Get-Command python.exe -ErrorAction Stop).Source
$script = Join-Path $PSScriptRoot 'start_server.ps1'

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
    '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Listen {1} -Port {2} -SecretFile "{3}" -Python "{4}"' -f $script, $Listen, $Port, $SecretFile, $interpreter
) -WorkingDirectory $PSScriptRoot

$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultiInstancePolicy Ignore
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName 'dualproxy-server' -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName 'dualproxy-server'
Write-Output "dualproxy-server registered (python=$interpreter, port=$Port)"
Write-Output 'server events are written to server.log in this folder'
