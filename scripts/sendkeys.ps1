# Send a keystroke sequence to the foreground window via SendKeys.
param([string]$Keys, [int]$DelayMs = 300)
$ws = New-Object -ComObject WScript.Shell
$ws.SendKeys($Keys)
Start-Sleep -Milliseconds $DelayMs
Write-Output "sent: $Keys"
