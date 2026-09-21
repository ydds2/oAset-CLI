Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class WinEnum2 {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  public static System.Collections.Generic.List<string> Results = new System.Collections.Generic.List<string>();
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll", CharSet=CharSet.Auto)] public static extern int GetWindowText(IntPtr h, StringBuilder sb, int max);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  public struct RECT { public int Left, Top, Right, Bottom; }
  public static bool Callback(IntPtr h, IntPtr l) {
    if (!IsWindowVisible(h)) return true;
    RECT r; GetWindowRect(h, out r);
    int w = r.Right - r.Left, ht = r.Bottom - r.Top;
    if (w <= 400 || ht <= 300) return true;
    StringBuilder sb = new StringBuilder(256);
    GetWindowText(h, sb, 256);
    uint pid; GetWindowThreadProcessId(h, out pid);
    Results.Add("hwnd=" + h + " pid=" + pid + " rect=" + r.Left + "," + r.Top + " " + w + "x" + ht + " title=" + sb.ToString());
    return true;
  }
  public static void Run() { EnumWindows(Callback, IntPtr.Zero); }
}
"@
[WinEnum2]::Run()
[WinEnum2]::Results | ForEach-Object { Write-Output $_ }
