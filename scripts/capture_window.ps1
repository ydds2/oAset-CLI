# Capture a window to a PNG file. Pass -Hwnd (preferred, stable) or -TargetPid.
param([long]$Hwnd = 0, [int]$TargetPid = 0, [Parameter(Mandatory=$true)][string]$Out,
      [switch]$TopMost)
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32Cap2 {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
  public struct RECT { public int Left, Top, Right, Bottom; }
}
"@
[Win32Cap2]::SetProcessDPIAware() | Out-Null
if ($Hwnd -eq 0) {
  $p = Get-Process -Id $TargetPid -ErrorAction Stop
  $Hwnd = $p.MainWindowHandle.ToInt64()
  if ($Hwnd -eq 0) { Write-Error "no main window"; exit 1 }
}
$h = [IntPtr]$Hwnd
if ($TopMost) {
  # HWND_TOPMOST (-1), keep position/size (SWP_NOMOVE|SWP_NOSIZE = 0x3)
  [Win32Cap2]::SetWindowPos($h, [IntPtr](-1), 0, 0, 0, 0, 0x3) | Out-Null
  Start-Sleep -Milliseconds 600
}
$r = New-Object Win32Cap2+RECT
[Win32Cap2]::GetWindowRect($h, [ref]$r) | Out-Null
$w = $r.Right - $r.Left; $ht = $r.Bottom - $r.Top
$bmp = New-Object System.Drawing.Bitmap($w, $ht)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($r.Left, $r.Top, 0, 0, (New-Object System.Drawing.Size($w, $ht)))
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
if ($TopMost) {
  # HWND_NOTOPMOST (-2)
  [Win32Cap2]::SetWindowPos($h, [IntPtr](-2), 0, 0, 0, 0, 0x3) | Out-Null
}
Write-Output "saved $Out ($w x $ht)"
