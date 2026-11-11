# Run from an elevated PowerShell. Registers the client supervisor to run at startup and at logon.
# ASCII only: Windows PowerShell 5.1 misreads UTF-8 files that have no BOM.
param(
    [string]$Server = '192.168.3.22',
    [int]$ServerPort = 7443,
    [int]$SocksPort = 1080
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# Resolve python once, here, and store the absolute path in the task. The WindowsApps
# alias does not resolve for other users, which silently stops the supervisor.
$interpreter = (Get-Command python.exe -ErrorAction Stop).Source
$script = Join-Path $PSScriptRoot 'start_client.ps1'

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
    '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Server {1} -ServerPort {2} -SocksPort {3} -Python "{4}"' -f $script, $Server, $ServerPort, $SocksPort, $interpreter
) -WorkingDirectory $PSScriptRoot

# Logon trigger alone is not enough: the client needs the user's Wi-Fi profile, so it runs
# at logon, and the at-startup trigger covers a machine that is already logged in.
$triggers = @(
    New-ScheduledTaskTrigger -AtStartup
    New-ScheduledTaskTrigger -AtLogOn
)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultiInstancePolicy Ignore
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName 'dualproxy-client' -Action $action -Trigger $triggers -Settings $settings `
    -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName 'dualproxy-client'
Write-Output "dualproxy-client registered (python=$interpreter, socks=$SocksPort)"
Write-Output 'client events are written to client.log in this folder'
