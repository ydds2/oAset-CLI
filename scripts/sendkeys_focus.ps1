# Focus a window by pid, then send a SendKeys sequence to it.
param([int]$TargetPid, [string]$Keys, [int]$DelayMs = 400)
$ws = New-Object -ComObject WScript.Shell
$ok = $ws.AppActivate($TargetPid)
if (-not $ok) { Write-Error "AppActivate($TargetPid) failed"; exit 1 }
Start-Sleep -Milliseconds 250
$ws.SendKeys($Keys)
Start-Sleep -Milliseconds $DelayMs
Write-Output "focused pid $TargetPid and sent: $Keys"
