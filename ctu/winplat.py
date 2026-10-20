"""Windows platform helpers. Every call is guarded: on other platforms, or if an API is missing,
they return a neutral default. The pure converters at the top are unit-tested everywhere."""
from __future__ import annotations

import os
import sys

IS_WIN = os.name == "nt"
DEFAULT_ACCENT = {"light": "#005FB8", "dark": "#60CDFF"}
_PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"


# ---- pure ----

def _hex(r, g, b) -> str:
    return f"#{r & 255:02X}{g & 255:02X}{b & 255:02X}"


def abgr_to_hex(v: int) -> str:
    """DWM AccentColor (0xAABBGGRR) -> #RRGGBB."""
    return _hex(v, v >> 8, v >> 16)


def argb_to_hex(v: int) -> str:
    """DWM ColorizationColor (0xAARRGGBB) -> #RRGGBB."""
    return _hex(v >> 16, v >> 8, v)


def accent_from_palette(data) -> dict | None:
    """Explorer AccentPalette (8 RGBA swatches: Light3, Light2, Light1, Base, Dark1, Dark2, Dark3, -)
    -> the WinUI accent fills: Dark1 on light themes, Light2 on dark ones."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 32:
        return None
    sw = [tuple(data[i * 4:i * 4 + 3]) for i in range(8)]
    return {"light": _hex(*sw[4]), "dark": _hex(*sw[1])}


def _mix(hexv: str, other: tuple, t: float) -> str:
    r, g, b = (int(hexv[i:i + 2], 16) for i in (1, 3, 5))
    return _hex(*(round(c + (o - c) * t) for c, o in zip((r, g, b), other)))


def accent_from_base(hexv: str) -> dict:
    """No palette: darken the base for light themes, lighten it for dark ones."""
    return {"light": _mix(hexv, (0, 0, 0), 0.2), "dark": _mix(hexv, (255, 255, 255), 0.45)}


# ---- registry ----

def reg_value(subkey: str, name: str):
    if not IS_WIN:
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as k:
            return winreg.QueryValueEx(k, name)[0]
    except Exception:
        return None


def taskbar_light() -> bool:
    """Taskbar / tray colours: SystemUsesLightTheme (missing = light)."""
    v = reg_value(_PERSONALIZE, "SystemUsesLightTheme")
    return v is None or bool(v)


def apps_light() -> bool:
    v = reg_value(_PERSONALIZE, "AppsUseLightTheme")
    return v is None or bool(v)


def accent() -> dict:
    pal = accent_from_palette(reg_value(r"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent", "AccentPalette"))
    if pal:
        return pal
    v = reg_value(r"Software\Microsoft\Windows\DWM", "AccentColor")
    if isinstance(v, int):
        return accent_from_base(abgr_to_hex(v))
    v = reg_value(r"Software\Microsoft\Windows\DWM", "ColorizationColor")
    if isinstance(v, int):
        return accent_from_base(argb_to_hex(v))
    return dict(DEFAULT_ACCENT)


def protocol_registered() -> bool:
    return reg_value(r"Software\Classes\claude-usage-tracker\shell\open\command", "") is not None


def register_protocol(command: str) -> bool:
    """Per-user `claude-usage-tracker:` URL protocol (toast buttons). No admin rights needed."""
    if not IS_WIN:
        return False
    try:
        import winreg
        base = r"Software\Classes\claude-usage-tracker"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:Claude Usage Tracker")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, command)
        return True
    except Exception:
        return False


# ---- user32 / shell32 ----

_u32 = None


def _user32():
    global _u32
    if _u32 is None and IS_WIN:
        import ctypes
        from ctypes import wintypes
        u = ctypes.WinDLL("user32", use_last_error=True)
        H = wintypes.HWND
        for name, args, res in (
            ("GetWindowRect", [H, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            ("SetWindowPos", [H, H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint], wintypes.BOOL),
            ("GetForegroundWindow", [], H),
            ("MonitorFromPoint", [wintypes.POINT, wintypes.DWORD], wintypes.HMONITOR),
            ("MonitorFromWindow", [H, wintypes.DWORD], wintypes.HMONITOR),
            ("GetCursorPos", [ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            ("FindWindowW", [wintypes.LPCWSTR, wintypes.LPCWSTR], H),
            ("SetLayeredWindowAttributes", [H, wintypes.DWORD, ctypes.c_ubyte, wintypes.DWORD], wintypes.BOOL),
            ("IsWindowVisible", [H], wintypes.BOOL),
        ):
            try:
                f = getattr(u, name)
                f.argtypes, f.restype = args, res
            except Exception:
                pass
        getl = getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW
        setl = getattr(u, "SetWindowLongPtrW", None) or u.SetWindowLongW
        getl.argtypes, getl.restype = [H, ctypes.c_int], ctypes.c_ssize_t
        setl.argtypes, setl.restype = [H, ctypes.c_int, ctypes.c_ssize_t], ctypes.c_ssize_t
        u._getl, u._setl = getl, setl
        _u32 = u
    return _u32


def set_dpi_awareness() -> None:
    """Per-monitor v2 DPI awareness, before any window exists (crisp tray icon and windows)."""
    if not IS_WIN:
        return
    try:
        import ctypes
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def set_app_user_model_id(appid: str = "ClaudeUsageTracker.App") -> None:
    if not IS_WIN:
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(appid)
    except Exception:
        pass


def tray_dpi() -> int:
    """DPI of the monitor holding the taskbar (the tray icon's size follows it)."""
    if not IS_WIN:
        return 96
    try:
        import ctypes
        u = ctypes.windll.user32
        tray = _user32().FindWindowW("Shell_TrayWnd", None)
        dpi = u.GetDpiForWindow(tray) if tray else 0
        return int(dpi or u.GetDpiForSystem() or 96)
    except Exception:
        return 96


def small_icon_size(dpi: int | None = None) -> int:
    """SM_CXSMICON at the taskbar's DPI: 16 / 20 / 24 / 32 ..."""
    dpi = dpi or tray_dpi()
    if IS_WIN:
        try:
            import ctypes
            n = ctypes.windll.user32.GetSystemMetricsForDpi(49, int(dpi))
            if n > 0:
                return int(n)
        except Exception:
            pass
    return max(16, round(16 * dpi / 96))


def notification_state() -> str | None:
    """SHQueryUserNotificationState: away / busy / fullscreen / presentation / quiet / ok, or None."""
    if not IS_WIN:
        return None
    try:
        import ctypes
        st = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(st)) != 0:
            return None
        return {1: "away", 2: "busy", 3: "fullscreen", 4: "presentation", 5: "ok", 6: "quiet", 7: "fullscreen"}.get(st.value)
    except Exception:
        return None


def foreground_window():
    try:
        return _user32().GetForegroundWindow() if IS_WIN else None
    except Exception:
        return None


def window_rect(hwnd):
    """(left, top, right, bottom) in physical px, or None."""
    if not (IS_WIN and hwnd):
        return None
    try:
        from ctypes import byref, wintypes
        r = wintypes.RECT()
        if _user32().GetWindowRect(hwnd, byref(r)):
            return (r.left, r.top, r.right, r.bottom)
    except Exception:
        pass
    return None


def _monitor_info(hmon):
    import ctypes
    from ctypes import wintypes

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if not ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return None
    w = mi.rcWork
    return (w.left, w.top, w.right, w.bottom)


def work_area_at(x: int, y: int, nearest: bool = True):
    """Work area (taskbar excluded) of the monitor at a physical point, or None."""
    if not IS_WIN:
        return None
    try:
        from ctypes import wintypes
        hmon = _user32().MonitorFromPoint(wintypes.POINT(int(x), int(y)), 2 if nearest else 0)
        return _monitor_info(hmon) if hmon else None
    except Exception:
        return None


def work_area_of(hwnd):
    if not (IS_WIN and hwnd):
        return None
    try:
        hmon = _user32().MonitorFromWindow(hwnd, 2)
        return _monitor_info(hmon) if hmon else None
    except Exception:
        return None


def dpi_of(hwnd) -> int:
    try:
        import ctypes
        return int(ctypes.windll.user32.GetDpiForWindow(hwnd) or 96)
    except Exception:
        return 96


SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x1, 0x2, 0x4, 0x10, 0x20
HWND_TOPMOST = -1


def set_window_pos(hwnd, x=None, y=None, w=None, h=None, topmost=False) -> bool:
    if not (IS_WIN and hwnd):
        return False
    try:
        flags = SWP_NOACTIVATE | (SWP_NOMOVE if x is None else 0) | (SWP_NOSIZE if w is None else 0)
        flags |= 0 if topmost else SWP_NOZORDER
        return bool(_user32().SetWindowPos(hwnd, HWND_TOPMOST if topmost else None,
                                           int(x or 0), int(y or 0), int(w or 0), int(h or 0), flags))
    except Exception:
        return False


GWL_EXSTYLE = -20
WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_LAYERED, WS_EX_APPWINDOW, WS_EX_NOACTIVATE = 0x20, 0x80, 0x80000, 0x40000, 0x8000000


def set_ex_style(hwnd, add: int = 0, remove: int = 0) -> bool:
    if not (IS_WIN and hwnd):
        return False
    try:
        u = _user32()
        ex = u._getl(hwnd, GWL_EXSTYLE)
        new = (ex | add) & ~remove
        if new != ex:
            u._setl(hwnd, GWL_EXSTYLE, new)
            u.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        return True
    except Exception:
        return False


def set_alpha(hwnd, opacity_pct: int) -> bool:
    """Requires WS_EX_LAYERED. LWA_ALPHA."""
    try:
        return bool(_user32().SetLayeredWindowAttributes(hwnd, 0, max(0, min(255, round(opacity_pct * 2.55))), 2))
    except Exception:
        return False


def cursor_pos():
    if not IS_WIN:
        return None
    try:
        from ctypes import byref, wintypes
        p = wintypes.POINT()
        if _user32().GetCursorPos(byref(p)):
            return (p.x, p.y)
    except Exception:
        pass
    return None


def notify_icon_rect(hwnd, uid: int):
    """Screen rect of our tray icon (Shell_NotifyIconGetRect), or None (e.g. in the overflow)."""
    if not (IS_WIN and hwnd):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]

        class NOTIFYICONIDENTIFIER(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT), ("guidItem", GUID)]
        ident = NOTIFYICONIDENTIFIER()
        ident.cbSize = ctypes.sizeof(NOTIFYICONIDENTIFIER)
        ident.hWnd = hwnd
        ident.uID = uid & 0xFFFFFFFF
        r = wintypes.RECT()
        fn = ctypes.windll.shell32.Shell_NotifyIconGetRect
        fn.argtypes = [ctypes.POINTER(NOTIFYICONIDENTIFIER), ctypes.POINTER(wintypes.RECT)]
        fn.restype = ctypes.c_long
        if fn(ctypes.byref(ident), ctypes.byref(r)) == 0 and r.right > r.left:
            return (r.left, r.top, r.right, r.bottom)
    except Exception:
        pass
    return None


def open_path(p) -> None:
    try:
        if IS_WIN:
            os.startfile(str(p))  # noqa: S606
        else:
            import subprocess
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(p)])
    except Exception:
        pass
