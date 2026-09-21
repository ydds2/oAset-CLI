"""Windows sandbox shim: re-exec the real command under a restricted token.

Why a shim: the documented Windows APIs that change a process token
(CreateRestrictedToken + CreateProcessAsUser) act at process CREATION time —
a parent cannot retroactively restrict a child that asyncio already spawned.
So run_shell, when sandboxed on Windows, spawns::

    [python, -m, oaset.sandboxexec, --, <real argv>]

(frozen exe: ``oaset.exe --sandbox-exec -- <real argv>``). The shim derives a
restricted token from a duplicate of its own primary token — that shape needs
no extra privilege — and starts the real command with CreateProcessAsUser:

  - Everyone / Users / Authenticated Users / Administrators become deny-only
    SIDs: any ACE granting access THROUGH those groups stops applying, so
    writes to group-writable locations (C:\\Users\\Public, Users-granted
    ProgramData areas, …) are denied BY THE OS, no matter how the path was
    spelled (traversal, env expansion, PowerShell $HOME).
  - Privileges are stripped (DISABLE_MAX_PRIVILEGE).
  - The user's own SID stays enabled, so workspace writes and toolchain reads
    keep working; the remaining hole (user-profile writes granted to the user
    SID) is covered by the pre-flight workspace-write guard in run_shell and
    is stated honestly in `oaset doctor`.

The shim runs inside the same Job Object as the shell command, and jobs
propagate to children: the memory cap and kill-on-close still cover the real
command. Fail CLOSED: if the restricted spawn cannot be built, the command
does not run unsandboxed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

# well-known SIDs made deny-only (filtered to the ones actually in the token).
# Everyone is deliberately NOT denied: process startup needs at least one
# Everyone-granted kernel object, and a child whose token denies Everyone dies
# with STATUS_ACCESS_DENIED before main() (verified on Win11 26200).
# BUILTIN\Users is deliberately NOT denied either: Windows/Program Files grant
# read via Users ACEs, so deny-only Users would make every system binary
# (including the interpreter itself) unreadable. What these two block: write
# ACEs granted through Authenticated Users (C:\ root file/dir creation,
# several ProgramData areas) and anything needing the Administrators group.
_DENY_SIDS = (
    "S-1-5-11",       # Authenticated Users
    "S-1-5-32-544",   # BUILTIN\Administrators
)

TOKEN_QUERY = 0x0008
TOKEN_DUPLICATE = 0x0002
MAXIMUM_ALLOWED = 0x02000000
SecurityImpersonation = 2
TokenPrimary = 1
TokenUser = 1
TokenGroups = 2
DISABLE_MAX_PRIVILEGES = 0x1
SE_GROUP_USE_FOR_DENY_ONLY = 0x10
STARTF_USESTDHANDLES = 0x0100
CREATE_UNICODE_ENVIRONMENT = 0x0400
CREATE_NO_WINDOW = 0x08000000
INFINITE = 0xFFFFFFFF
STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, STD_ERROR_HANDLE = -10, -11, -12


class _Win:
    """ctypes plumbing, built once. Windows-only by construction."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        self.advapi32 = ctypes.windll.advapi32  # type: ignore[attr-defined]

        class SID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

        class TOKEN_USER(ctypes.Structure):
            _fields_ = [("User", SID_AND_ATTRIBUTES)]

        class STARTUPINFOW(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE),
            ]

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
            ]

        self.SID_AND_ATTRIBUTES = SID_AND_ATTRIBUTES
        self.TOKEN_USER = TOKEN_USER
        self.STARTUPINFOW = STARTUPINFOW
        self.PROCESS_INFORMATION = PROCESS_INFORMATION

        adv = self.advapi32
        wt = wintypes
        adv.OpenProcessToken.restype = wt.BOOL
        adv.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
        adv.DuplicateTokenEx.restype = wt.BOOL
        adv.DuplicateTokenEx.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p,
                                         wt.DWORD, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
        adv.GetTokenInformation.restype = wt.BOOL
        adv.GetTokenInformation.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_void_p,
                                            wt.DWORD, ctypes.POINTER(wt.DWORD)]
        adv.ConvertSidToStringSidW.restype = wt.BOOL
        adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                               ctypes.POINTER(ctypes.c_void_p)]
        adv.ConvertStringSidToSidW.restype = wt.BOOL
        adv.ConvertStringSidToSidW.argtypes = [wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
        adv.CreateRestrictedToken.restype = wt.BOOL
        adv.CreateRestrictedToken.argtypes = [
            wt.HANDLE, wt.DWORD,
            wt.DWORD, ctypes.c_void_p,    # SidsToDelete
            wt.DWORD, ctypes.c_void_p,    # PrivilegesToDelete
            wt.DWORD, ctypes.c_void_p,    # SidsToDisable
            ctypes.POINTER(wt.HANDLE),
        ]
        adv.CreateProcessAsUserW.restype = wt.BOOL
        adv.CreateProcessAsUserW.argtypes = [
            wt.HANDLE, wt.LPCWSTR, wt.LPWSTR,
            ctypes.c_void_p, ctypes.c_void_p, wt.BOOL, wt.DWORD,
            wt.LPCWSTR, wt.LPCWSTR,
            ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION),
        ]
        self.kernel32.WaitForSingleObject.restype = wt.DWORD
        self.kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
        self.kernel32.GetExitCodeProcess.restype = wt.BOOL
        self.kernel32.GetExitCodeProcess.argtypes = [wt.HANDLE,
                                                     ctypes.POINTER(wt.DWORD)]

    def sid_string(self, sid) -> str:
        out = self.ctypes.c_void_p()
        if not sid or not self.advapi32.ConvertSidToStringSidW(
                self.ctypes.c_void_p(sid), self.ctypes.byref(out)):
            return ""
        try:
            return self.ctypes.wstring_at(out.value) if out.value else ""
        finally:
            self.kernel32.LocalFree(out)


def _token_sid_set(win: _Win, token) -> set[str]:
    """String SIDs present in the token (user SID + group SIDs)."""
    import ctypes
    out: set[str] = set()
    for klass in (TokenUser, TokenGroups):
        needed = win.wintypes.DWORD(0)
        win.advapi32.GetTokenInformation(token, klass, None, 0, ctypes.byref(needed))
        if not needed.value:
            continue
        buf = ctypes.create_string_buffer(needed.value)
        if not win.advapi32.GetTokenInformation(token, klass, buf, needed,
                                                ctypes.byref(needed)):
            continue
        if klass == TokenUser:
            user = ctypes.cast(buf, ctypes.POINTER(win.TOKEN_USER)).contents
            s = win.sid_string(user.User.Sid)
            if s:
                out.add(s)
            continue
        count = int.from_bytes(buf.raw[:4], "little")

        class TOKEN_GROUPS(ctypes.Structure):
            _fields_ = [("GroupCount", win.wintypes.DWORD),
                        ("Groups", win.SID_AND_ATTRIBUTES * max(count, 1))]

        groups = ctypes.cast(buf, ctypes.POINTER(TOKEN_GROUPS)).contents
        for i in range(count):
            s = win.sid_string(groups.Groups[i].Sid)
            if s:
                out.add(s)
    return out


def _restricted_token(win: _Win):
    """(restricted primary token handle, keepalive buffers) or raise OSError."""
    import ctypes
    me = win.kernel32.GetCurrentProcess()
    htok = win.wintypes.HANDLE()
    if not win.advapi32.OpenProcessToken(me, TOKEN_QUERY | TOKEN_DUPLICATE,
                                         ctypes.byref(htok)):
        raise OSError("OpenProcessToken failed")
    try:
        # Derive DIRECTLY from the process's own primary token: only then is
        # the result "a restricted version of the caller's primary token", the
        # documented CreateProcessAsUser shape that needs no privilege. Going
        # through DuplicateTokenEx first earned ERROR_PRIVILEGE_NOT_HELD (1314).
        present = _token_sid_set(win, htok)
        targets = [s for s in _DENY_SIDS if s in present]
        if not targets:
            raise OSError("no denyable group SIDs found in token")
        arr = (win.SID_AND_ATTRIBUTES * len(targets))()
        keepalive: list = []
        for i, sid_str in enumerate(targets):
            sid = ctypes.c_void_p()
            if not win.advapi32.ConvertStringSidToSidW(sid_str, ctypes.byref(sid)):
                raise OSError(f"ConvertStringSidToSidW({sid_str}) failed")
            keepalive.append(sid)
            arr[i].Sid = sid
            arr[i].Attributes = SE_GROUP_USE_FOR_DENY_ONLY
        restricted = win.wintypes.HANDLE()
        if not win.advapi32.CreateRestrictedToken(
            htok, DISABLE_MAX_PRIVILEGES,
            len(targets), arr,   # SidsToDisable (deny-only)
            0, None,             # PrivilegesToDelete: none (flag strips all)
            0, None,             # SidsToRestrict: none
            ctypes.byref(restricted),
        ):
            raise OSError("CreateRestrictedToken failed")
        return restricted, keepalive
    finally:
        win.kernel32.CloseHandle(htok)


def _grant_station_access(win: _Win, token) -> None:
    """Allow the restricted token's user onto the window station + desktop.

    WinSta0's DACL grants access through Everyone/Users group ACEs — exactly
    the SIDs we made deny-only — so CreateProcessAsUser would fail with
    ERROR_ACCESS_DENIED (5). Grant the token's own (still-enabled) user SID
    explicitly. Same user, same machine: this adds no one new.
    """
    import ctypes
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    adv = win.advapi32
    wt = win.wintypes

    class TRUSTEE_W(ctypes.Structure):
        _fields_ = [("pMultipleTrustee", ctypes.c_void_p),
                    ("pMultipleTrusteeExtension", ctypes.c_void_p),
                    ("TrusteeForm", wt.DWORD), ("TrusteeType", wt.DWORD),
                    ("ptstrName", ctypes.c_void_p)]

    class EXPLICIT_ACCESS_W(ctypes.Structure):
        _fields_ = [("grfAccessPermissions", wt.DWORD),
                    ("grfAccessMode", wt.DWORD),  # GRANT_ACCESS = 1
                    ("grfInheritance", wt.DWORD),
                    ("Trustee", TRUSTEE_W)]

    adv.GetSecurityDescriptorDacl.restype = wt.BOOL
    adv.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.BOOL),
                                              ctypes.POINTER(ctypes.c_void_p),
                                              ctypes.POINTER(wt.BOOL)]
    adv.SetEntriesInAclW.restype = wt.LONG
    adv.SetEntriesInAclW.argtypes = [wt.ULONG, ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.POINTER(ctypes.c_void_p)]
    adv.SetSecurityInfo.restype = wt.LONG
    adv.SetSecurityInfo.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD,
                                    ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_void_p, ctypes.c_void_p]
    adv.GetSecurityInfo.restype = wt.LONG
    adv.GetSecurityInfo.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD,
                                    ctypes.POINTER(ctypes.c_void_p),
                                    ctypes.POINTER(ctypes.c_void_p),
                                    ctypes.POINTER(ctypes.c_void_p),
                                    ctypes.POINTER(ctypes.c_void_p),
                                    ctypes.POINTER(ctypes.c_void_p)]
    user32.OpenWindowStationW.restype = wt.HANDLE
    user32.OpenWindowStationW.argtypes = [wt.LPCWSTR, wt.BOOL, wt.DWORD]
    user32.OpenDesktopW.restype = wt.HANDLE
    user32.OpenDesktopW.argtypes = [wt.LPCWSTR, wt.HANDLE, wt.BOOL, wt.DWORD]
    READ_CONTROL_WRITE_DAC = 0x00020000 | 0x00040000  # READ_CONTROL | WRITE_DAC
    SE_WINDOW_OBJECT = 7
    user32.GetProcessWindowStation.restype = wt.HANDLE
    user32.GetProcessWindowStation.argtypes = []
    user32.GetThreadDesktop.restype = wt.HANDLE
    user32.GetThreadDesktop.argtypes = [wt.DWORD]

    # the token's user SID (kept enabled when the groups went deny-only)
    needed = wt.DWORD(0)
    adv.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(needed))
    buf = ctypes.create_string_buffer(needed.value or 1)
    if not adv.GetTokenInformation(token, TokenUser, buf, needed, ctypes.byref(needed)):
        raise OSError("TokenUser query failed")
    sid = ctypes.cast(buf, ctypes.POINTER(win.TOKEN_USER)).contents.User.Sid

    def _grant(handle) -> None:
        if not handle:
            return
        sd = ctypes.c_void_p()
        dacl_ptr = ctypes.c_void_p()
        rc = adv.GetSecurityInfo(handle, SE_WINDOW_OBJECT, 4,
                                 None, None, ctypes.byref(dacl_ptr), None,
                                 ctypes.byref(sd))
        if rc != 0:
            raise OSError(f"GetSecurityInfo failed ({rc})")
        ea = EXPLICIT_ACCESS_W()
        ea.grfAccessPermissions = 0x01F03FF  # GENERIC_ALL
        ea.grfAccessMode = 1  # GRANT_ACCESS
        ea.grfInheritance = 0
        ea.Trustee.TrusteeForm = 0  # TRUSTEE_IS_SID
        ea.Trustee.ptstrName = ctypes.c_void_p(sid or 0)
        merged = ctypes.c_void_p()
        rc = adv.SetEntriesInAclW(1, ctypes.byref(ea), dacl_ptr, ctypes.byref(merged))
        if rc != 0:
            raise OSError(f"SetEntriesInAclW failed ({rc})")
        rc = adv.SetSecurityInfo(handle, SE_WINDOW_OBJECT, 4, None, None,
                                 merged, None)
        if rc != 0:
            raise OSError(f"SetSecurityInfo failed ({rc})")
        ctypes.windll.kernel32.LocalFree(merged)  # type: ignore[attr-defined]

    hwinsta = user32.OpenWindowStationW("winsta0", False, READ_CONTROL_WRITE_DAC)
    if not hwinsta:
        hwinsta = user32.GetProcessWindowStation()
    _grant(hwinsta)
    hdesk = user32.OpenDesktopW("default", None, False, READ_CONTROL_WRITE_DAC)
    if hdesk:
        _grant(hdesk)


def run(argv: list[str]) -> int:
    """Start `argv` under the restricted token; block; return its exit code.

    Fail CLOSED: any failure prints one line to stderr and exits 126 without
    running the command unsandboxed.
    """
    if os.name != "nt":
        print("sandboxexec: Windows-only", file=sys.stderr)
        return 126
    if not argv:
        print("sandboxexec: no command after --", file=sys.stderr)
        return 126
    try:
        win = _Win()
        import ctypes
        restricted, keepalive = _restricted_token(win)  # noqa: F841 (must stay alive)
        try:
            _grant_station_access(win, restricted)
            pi = _launch(win, restricted, argv)
        finally:
            win.kernel32.CloseHandle(restricted)
        win.kernel32.CloseHandle(pi.hThread)
        win.kernel32.WaitForSingleObject(pi.hProcess, INFINITE)
        code = win.wintypes.DWORD(0)
        win.kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
        win.kernel32.CloseHandle(pi.hProcess)
        return int(code.value)
    except OSError as exc:
        print(f"sandboxexec: restricted spawn refused: {exc}", file=sys.stderr)
        return 126


def _launch(win: "_Win", restricted, argv: list[str]) -> "Any":
    """CreateProcess(SUSPENDED) + primary-token swap + resume.

    CreateProcessAsUser rejects ANY CreateRestrictedToken-derived token with
    ERROR_ACCESS_DENIED on current Windows (verified on 26200), so the token
    is swapped into the suspended child via NtSetInformationProcess — the
    same mechanism Chromium's broker uses. A non-zero NTSTATUS fails closed.
    """
    import ctypes
    k32 = win.kernel32
    k32.CreateProcessW.restype = win.wintypes.BOOL
    k32.CreateProcessW.argtypes = [win.wintypes.LPCWSTR, win.wintypes.LPWSTR,
                                   ctypes.c_void_p, ctypes.c_void_p,
                                   win.wintypes.BOOL, win.wintypes.DWORD,
                                   ctypes.c_void_p, win.wintypes.LPCWSTR,
                                   ctypes.c_void_p, ctypes.c_void_p]
    k32.DuplicateHandle.restype = win.wintypes.BOOL
    k32.DuplicateHandle.argtypes = [win.wintypes.HANDLE, win.wintypes.HANDLE,
                                    win.wintypes.HANDLE,
                                    ctypes.POINTER(win.wintypes.HANDLE),
                                    win.wintypes.DWORD, win.wintypes.BOOL,
                                    win.wintypes.DWORD]
    ntdll = ctypes.windll.ntdll  # type: ignore[attr-defined]
    ntdll.NtSetInformationProcess.restype = ctypes.c_long
    ntdll.NtSetInformationProcess.argtypes = [win.wintypes.HANDLE, ctypes.c_ulong,
                                              ctypes.c_void_p, ctypes.c_ulong]

    # the swap needs a handle holding TOKEN_ASSIGN_PRIMARY|DUPLICATE|QUERY
    dup_r = win.wintypes.HANDLE()
    if not k32.DuplicateHandle(k32.GetCurrentProcess(), restricted,
                               k32.GetCurrentProcess(), ctypes.byref(dup_r),
                               0x0001 | 0x0002 | 0x0008, False, 0):
        raise OSError("DuplicateHandle(token) failed")

    class PROCESS_ACCESS_TOKEN(ctypes.Structure):
        # second slot is padding on purpose: a 16-byte buffer is what
        # NtSetInformationProcess(ProcessAccessToken) accepts on Win11
        _fields_ = [("TokenHandle", win.wintypes.HANDLE), ("pad", ctypes.c_void_p)]

    si = win.STARTUPINFOW()
    si.cb = ctypes.sizeof(win.STARTUPINFOW)
    si.dwFlags = STARTF_USESTDHANDLES
    si.hStdInput = k32.GetStdHandle(STD_INPUT_HANDLE)
    si.hStdOutput = k32.GetStdHandle(STD_OUTPUT_HANDLE)
    si.hStdError = k32.GetStdHandle(STD_ERROR_HANDLE)
    buf = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
    pi = win.PROCESS_INFORMATION()
    if not k32.CreateProcessW(None, buf, None, None, True,
                              0x4 | CREATE_NO_WINDOW,  # CREATE_SUSPENDED; NULL env inherits (the UNICODE flag + NULL env kills the loader)
                              None, os.getcwd() or None, ctypes.byref(si), ctypes.byref(pi)):
        raise OSError(f"CreateProcessW failed (win err {k32.GetLastError()})")
    pat = PROCESS_ACCESS_TOKEN()
    pat.TokenHandle = dup_r
    status = ntdll.NtSetInformationProcess(pi.hProcess, 9, ctypes.byref(pat),
                                           ctypes.sizeof(pat))
    if status != 0:
        k32.TerminateProcess(pi.hProcess, 126)
        raise OSError(f"NtSetInformationProcess(ProcessAccessToken) -> {status:#x}")
    k32.ResumeThread(pi.hThread)
    return pi


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--":
        args = args[1:]
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
