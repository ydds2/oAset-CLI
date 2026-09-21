# Tap a key (down+up) via SendInput - passes through the real input pipeline (TSF/IME).
param([uint16]$Vk = 0xA0, [int]$DelayMs = 300)
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class SI {
  [StructLayout(LayoutKind.Sequential)]
  public struct INPUT { public uint type; public InputUnion u; }
  [StructLayout(LayoutKind.Explicit)]
  public struct InputUnion { [FieldOffset(0)] public KEYBDINPUT ki; [FieldOffset(0)] public MOUSEINPUT mi; }
  [StructLayout(LayoutKind.Sequential)]
  public struct KEYBDINPUT { public ushort wVk; public ushort wScan; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }
  [StructLayout(LayoutKind.Sequential)]
  public struct MOUSEINPUT { public int dx, dy; public uint mouseData, dwFlags, time; public IntPtr dwExtraInfo; }
  [DllImport("user32.dll", SetLastError=true)]
  public static extern uint SendInput(uint n, INPUT[] inputs, int size);
  public static void Tap(ushort vk) {
    INPUT[] inp = new INPUT[2];
    inp[0].type = 1; inp[0].u.ki.wVk = vk; inp[0].u.ki.dwFlags = 0;
    inp[1].type = 1; inp[1].u.ki.wVk = vk; inp[1].u.ki.dwFlags = 2; // KEYEVENTF_KEYUP
    SendInput(2, inp, Marshal.SizeOf(typeof(INPUT)));
  }
}
"@
[SI]::Tap($Vk)
Start-Sleep -Milliseconds $DelayMs
Write-Output "tapped vk=0x$('{0:X}' -f $Vk)"
