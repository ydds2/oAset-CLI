Add-Type @"
using System;
using System.Runtime.InteropServices;
public class FG {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll", CharSet=CharSet.Auto)] public static extern int GetWindowText(IntPtr h, System.Text.StringBuilder sb, int max);
}
"@
$h = [FG]::GetForegroundWindow()
$p = [uint32]0
[FG]::GetWindowThreadProcessId($h, [ref]$p) | Out-Null
$sb = New-Object System.Text.StringBuilder 256
[FG]::GetWindowText($h, $sb, 256) | Out-Null
$proc = Get-Process -Id $p -ErrorAction SilentlyContinue
Write-Output ("foreground pid=" + $p + " name=" + $(if ($proc) { $proc.ProcessName } else { "?" }) + " title=" + $sb.ToString())
