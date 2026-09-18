"""Windows-only helpers for locating and moving the Chrome window the app drives.

The uploader runs a headful Chrome (the WAF/Cloudflare gates need a human in
front of it), but that does not mean it is allowed to grab the desktop. This
module is the escape hatch: it finds the exact top-level window belonging to
*our* browser process and can restore/foreground it when a verification needs
solving.

Design rules:
  * stdlib only -- ctypes, no pywin32, nothing new in requirements.txt.
  * Every entry point no-ops (returns False/None) off Windows, when ctypes is
    unavailable, or when the window cannot be found. Nothing here raises.
  * The browser is identified by PID, so the user's own Chrome windows are
    never touched.
"""

from __future__ import annotations

import ctypes
import os
import sys

IS_WINDOWS = sys.platform == "win32"

SW_MINIMIZE = 6
SW_RESTORE = 9
SW_SHOW = 5

# The browser frame and the legacy widget class. Anything else Chrome opens
# (renderers, helper windows) is either invisible or not a top-level frame.
CHROME_WINDOW_CLASSES = ("Chrome_WidgetWin_1", "Chrome_WidgetWin_0")

if IS_WINDOWS:  # pragma: no cover - platform specific
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    _user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.IsIconic.restype = wintypes.BOOL
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.ShowWindow.restype = wintypes.BOOL
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.BringWindowToTop.argtypes = [wintypes.HWND]
    _user32.BringWindowToTop.restype = wintypes.BOOL
    _user32.keybd_event.argtypes = [
        ctypes.c_ubyte,
        ctypes.c_ubyte,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    _user32.keybd_event.restype = None
else:  # pragma: no cover - platform specific
    _user32 = None
    _kernel32 = None
    _WNDENUMPROC = None


# --------------------------------------------------------------------------
# Process resolution: Playwright context -> browser PID
# --------------------------------------------------------------------------

# id(context) -> pid. Tiny: one entry per browser we ever drove this session.
_PID_CACHE: dict[int, int] = {}


def browser_pid(context) -> int | None:
    """PID of the Chromium browser process behind a Playwright context.

    Preferred route is a browser-level CDP session (SystemInfo.getProcessInfo),
    which reports the browser process directly. If that is unavailable -- older
    Playwright, or a context whose ``browser`` handle is missing -- fall back to
    walking the process tree for a Chrome descendant of this Python process.
    """
    if context is None:
        return None

    key = id(context)
    cached = _PID_CACHE.get(key)
    if cached:
        return cached

    pid = _pid_via_cdp(context) or _pid_via_process_tree()
    if pid:
        _PID_CACHE[key] = pid
    return pid


def forget(context) -> None:
    """Drop a cached PID (call after a browser restart)."""
    _PID_CACHE.pop(id(context), None)


def _pid_via_cdp(context) -> int | None:
    browser = getattr(context, "browser", None)
    if browser is None:
        return None
    session = None
    try:
        session = browser.new_browser_cdp_session()
        info = session.send("SystemInfo.getProcessInfo") or {}
    except Exception:
        return None
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass
    try:
        for proc in info.get("processInfo", []):
            if proc.get("type") == "browser":
                return int(proc["id"])
    except Exception:
        pass
    return None


def _pid_via_process_tree() -> int | None:
    """Last resort: a chrome.exe whose ancestor chain reaches this process."""
    if not IS_WINDOWS:
        return None
    try:
        entries = _snapshot_processes()
    except Exception:
        return None
    if not entries:
        return None

    mine = os.getpid()
    by_parent: dict[int, list[int]] = {}
    names: dict[int, str] = {}
    for pid, ppid, name in entries:
        by_parent.setdefault(ppid, []).append(pid)
        names[pid] = name

    # Breadth-first from this process; a few levels covers Playwright's own
    # launch chain (python -> node driver -> chrome, or python -> chrome).
    frontier = [mine]
    for _ in range(6):
        nxt: list[int] = []
        for parent in frontier:
            for child in by_parent.get(parent, ()):
                if names.get(child, "").lower() == "chrome.exe":
                    return child
                nxt.append(child)
        frontier = nxt
        if not frontier:
            break
    return None


def _snapshot_processes() -> list[tuple[int, int, str]]:
    """[(pid, ppid, exe_name)] for every running process."""
    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ULONG_PTR),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    _kernel32.Process32NextW.restype = wintypes.BOOL

    snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return []
    out: list[tuple[int, int, str]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append(
                (int(entry.th32ProcessID), int(entry.th32ParentProcessID), entry.szExeFile)
            )
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        try:
            _kernel32.CloseHandle(snap)
        except Exception:
            pass
    return out


# --------------------------------------------------------------------------
# Window resolution: PID -> hwnd
# --------------------------------------------------------------------------


def find_window_for_pid(pid: int) -> int | None:
    """First visible Chrome frame owned by ``pid``.

    Minimized windows are still WS_VISIBLE, so this finds a minimized Chrome
    too -- which is exactly what we need in order to restore it.
    """
    if not IS_WINDOWS or not pid:
        return None
    hits: list[tuple[int, int]] = []  # (title_len, hwnd)

    def _cb(hwnd, _lparam):
        try:
            wpid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value != pid or not _user32.IsWindowVisible(hwnd):
                return True
            buf = ctypes.create_unicode_buffer(64)
            _user32.GetClassNameW(hwnd, buf, 64)
            if buf.value not in CHROME_WINDOW_CLASSES:
                return True
            hits.append((_user32.GetWindowTextLengthW(hwnd), int(hwnd)))
        except Exception:
            pass
        return True

    try:
        if not _user32.EnumWindows(_WNDENUMPROC(_cb), 0):
            return None
    except Exception:
        return None
    if not hits:
        return None
    # The real browser frame has a title; helper widget windows usually do not.
    hits.sort(key=lambda item: item[0], reverse=True)
    return hits[0][1]


# --------------------------------------------------------------------------
# Public window handle
# --------------------------------------------------------------------------


class ChromeWindow:
    """Resolves (and caches) the hwnd of the Chrome window behind a context."""

    def __init__(self, context, log=print):
        self._context = context
        self._log = log
        self._pid: int | None = None

    def pid(self) -> int | None:
        if self._pid is None:
            self._pid = browser_pid(self._context)
        return self._pid

    def hwnd(self) -> int | None:
        pid = self.pid()
        if pid is None:
            return None
        hwnd = find_window_for_pid(pid)
        if hwnd is None:
            # Browser died / restarted under us -- re-resolve next time.
            self._pid = None
            forget(self._context)
        return hwnd

    def is_minimized(self) -> bool:
        hwnd = self.hwnd()
        if not hwnd:
            return False
        try:
            return bool(_user32.IsIconic(hwnd))
        except Exception:
            return False

    def minimize(self) -> bool:
        hwnd = self.hwnd()
        if not hwnd:
            return False
        try:
            return bool(_user32.ShowWindow(hwnd, SW_MINIMIZE))
        except Exception:
            return False

    def show(self) -> bool:
        """Un-minimize without grabbing focus (used only if ever needed)."""
        hwnd = self.hwnd()
        if not hwnd:
            return False
        try:
            _user32.ShowWindow(hwnd, SW_SHOW)
            return True
        except Exception:
            return False

    def restore_and_focus(self) -> bool:
        """Bring the window back on screen and into the foreground."""
        hwnd = self.hwnd()
        if not hwnd:
            return False
        try:
            if _user32.IsIconic(hwnd):
                # SW_RESTORE un-minimizes and activates in one shot.
                _user32.ShowWindow(hwnd, SW_RESTORE)
            else:
                _user32.ShowWindow(hwnd, SW_SHOW)
            return _force_foreground(hwnd)
        except Exception:
            return False


def _force_foreground(hwnd) -> bool:
    """SetForegroundWindow, with the usual fallbacks.

    Windows refuses a foreground steal when the calling process is not itself
    foreground. BringWindowToTop covers the common case; the synthetic ALT
    press is the classic workaround for the hard lock. Best effort by design --
    a failure here only means the user has to click the taskbar icon.
    """
    try:
        if _user32.SetForegroundWindow(hwnd):
            return True
    except Exception:
        pass
    try:
        _user32.BringWindowToTop(hwnd)
        if _user32.SetForegroundWindow(hwnd):
            return True
    except Exception:
        pass
    try:
        VK_MENU = 0x12
        _user32.keybd_event(VK_MENU, 0, 0, None)  # ALT down
        _user32.SetForegroundWindow(hwnd)
        _user32.keybd_event(VK_MENU, 0, 0x0002, None)  # ALT up (KEYEVENTF_KEYUP)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Module-level convenience (what core/session/uploader actually call)
# --------------------------------------------------------------------------


def restore_and_focus_chrome(context, log=print) -> bool:
    """Un-minimize + foreground the Chrome window that needs a human."""
    if not IS_WINDOWS or context is None:
        return False
    try:
        return ChromeWindow(context, log=log).restore_and_focus()
    except Exception:
        return False


def minimize_chrome(context, log=print) -> bool:
    """Minimize the Chrome window. Not used by default -- see config."""
    if not IS_WINDOWS or context is None:
        return False
    try:
        return ChromeWindow(context, log=log).minimize()
    except Exception:
        return False
