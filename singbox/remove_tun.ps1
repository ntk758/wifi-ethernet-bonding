# Run from an elevated PowerShell. Stops the TUN and removes its startup task.
# ASCII only: Windows PowerShell 5.1 misreads UTF-8 files that have no BOM.
Stop-ScheduledTask -TaskName 'dualproxy-tun' -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName 'dualproxy-tun' -Confirm:$false -ErrorAction SilentlyContinue
Get-Process -Name 'sing-box' -ErrorAction SilentlyContinue | Stop-Process -Force
Write-Output 'dualproxy-tun removed'
