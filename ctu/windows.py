"""Desktop windows, all in one pywebview instance: the dashboard, the overlay and the tray flyout.
The pages come from the local API (ctu/ui); this module only hosts and places them.

Geometry helpers at the top are pure (physical pixels, rects as (left, top, right, bottom))."""
from __future__ import annotations

import threading
import time
import webbrowser

from ctu import APP_NAME, io, paths, winplat

SNAP_PX = 12
FLYOUT_W, FLYOUT_H = 300, 232          # logical px; the page reports its real size (flyout_resize)
DASH_W, DASH_H, DASH_MIN = 1100, 760, (720, 560)
OVERLAY_DEFAULT = (360, 30)
BG = {"light": "#F3F3F3", "dark": "#202020"}


# ---------------------------------------------------------------- pure geometry

def _clamp(v, lo, hi):
    return lo if hi < lo else max(lo, min(v, hi))


def snap_position(x, y, w, h, work, threshold=SNAP_PX):
    """Keep a w x h window inside `work`, snapping to an edge within `threshold`."""
    left, top, right, bottom = work
    x, y = _clamp(x, left, right - w), _clamp(y, top, bottom - h)
    if x - left <= threshold:
        x = left
    elif right - (x + w) <= threshold:
        x = right - w
    if y - top <= threshold:
        y = top
    elif bottom - (y + h) <= threshold:
        y = bottom - h
    return x, y


def anchored_resize(rect, w, h, work):
    """New (x, y, w, h) for a resize that keeps the edge nearest the work-area edge fixed
    (so an overlay snapped right/bottom grows left/up), clamped on screen."""
    left, top, right, bottom = rect
    L, T, R, B = work
    x = right - w if (R - right) < (left - L) else left
    y = bottom - h if (B - bottom) < (top - T) else top
    return _clamp(x, L, R - w), _clamp(y, T, B - h), w, h


def flyout_position(anchor, work, w, h, margin=12):
    """Top-left for a w x h flyout next to the tray icon `anchor` (rect or a point as a 0-size
    rect), on whichever side of the work area the taskbar is."""
    L, T, R, B = work
    if anchor is None:
        anchor = (R, B, R, B)
    al, at, ar, ab = anchor
    cx, cy = (al + ar) // 2, (at + ab) // 2
    if cy >= B:                       # taskbar at the bottom
        x, y = cx - w // 2, B - h - margin
    elif cy <= T:                     # top
        x, y = cx - w // 2, T + margin
    elif cx >= R:                     # right
        x, y = R - w - margin, cy - h // 2
    elif cx <= L:                     # left
        x, y = L + margin, cy - h // 2
    else:                             # overflow flyout / auto-hidden taskbar: above the point
        x, y = cx - w // 2, at - h - margin
    return _clamp(x, L + margin, R - w - margin), _clamp(y, T + margin, B - h - margin)


def scale(v, dpi) -> int:
    return int(round(v * dpi / 96))


# ---------------------------------------------------------------- pywebview shell

def available() -> bool:
    import importlib.util
    return importlib.util.find_spec("webview") is not None


def _create(webview, *args, **kw):
    """webview.create_window without the keyword arguments this pywebview version lacks."""
    try:
        import inspect
        params = inspect.signature(webview.create_window).parameters
        kw = {k: v for k, v in kw.items() if k in params}
    except Exception:
        pass
    return webview.create_window(*args, **kw)


def _hwnd(win):
    try:
        return int(win.native.Handle.ToInt64())
    except Exception:
        try:
            return int(win.native.Handle.ToInt32())
        except Exception:
            return None


class Shell:
    """Owns the pywebview windows. run() blocks the main thread until quit()."""

    def __init__(self, engine):
        self.engine = engine
        self.webview = None
        self.dashboard = self.overlay = self.flyout = None
        self.tray = None
        self._flyout_visible = False
        self._flyout_hidden_at = 0.0
        self._flyout_size = (FLYOUT_W, FLYOUT_H)
        self._overlay_auto_hidden = False
        self._overlay_visible = False
        self._move_timer = None
        self._lock = threading.RLock()

    # ---- helpers ----
    def url(self, page: str) -> str:
        return f"http://127.0.0.1:{self.engine.port}{page}"

    def bg(self) -> str:
        return BG.get(self.engine.platform.get("theme"), BG["light"])

    # ---- lifecycle ----
    def run(self) -> None:
        import webview
        self.webview = webview
        try:
            webview.settings["ALLOW_DOWNLOADS"] = True     # Team pool > CSV export
        except Exception:
            pass
        self.flyout = _create(
            webview, "Claude usage", self.url("/flyout"), width=FLYOUT_W, height=FLYOUT_H, resizable=False,
            frameless=True, easy_drag=False, on_top=True, hidden=True, background_color=self.bg())
        if (self.engine.cfg.get("overlay") or {}).get("shown"):
            self._create_overlay()
        webview.start(self._started, private_mode=False, storage_path=str(paths.WEBVIEW_DIR),
                      icon=str(paths.ICO_PATH) if paths.ICO_PATH.exists() else None)

    def _started(self) -> None:
        threading.Thread(target=self._watch, name="ctu-shell-watch", daemon=True).start()

    def quit(self) -> None:
        for w in (self.dashboard, self.overlay, self.flyout):
            try:
                if w:
                    w.destroy()
            except Exception:
                pass
        if self.tray:
            self.tray.stop()

    # ---- dashboard ----
    def open_dashboard(self, view: str = "now") -> None:
        with self._lock:
            self.close_flyout()
            if self.webview is None:
                webbrowser.open(self.url(f"/#/{view}"))
                return
            if self.dashboard is not None:
                try:
                    self.dashboard.evaluate_js(f"location.hash = '#/{view}'")
                    self.dashboard.restore()
                    self.dashboard.show()
                    return
                except Exception:
                    self.dashboard = None
            w = _create(self.webview, APP_NAME, self.url(f"/#/{view}"), width=DASH_W, height=DASH_H,
                        min_size=DASH_MIN, background_color=self.bg())
            w.events.closed += self._dashboard_closed
            self.dashboard = w

    def _dashboard_closed(self, *_):
        self.dashboard = None              # recreated on demand: no WebView2 kept around while closed

    # ---- overlay ----
    def _create_overlay(self) -> None:
        cfg = self.engine.cfg
        w, h = cfg.get("overlay_size") or OVERLAY_DEFAULT
        self.overlay = _create(
            self.webview, "Claude usage overlay", self.url("/overlay"), width=int(w), height=int(h), resizable=False,
            frameless=True, easy_drag=False, on_top=True, focus=False, shadow=False, background_color=self.bg())
        self.overlay.events.shown += self._overlay_shown
        self.overlay.events.moved += self._overlay_moved
        self.overlay.events.closed += self._overlay_closed
        self._overlay_visible = True

    def _overlay_closed(self, *_):
        self.overlay, self._overlay_visible = None, False

    def _overlay_shown(self, *_):
        hwnd = _hwnd(self.overlay)
        self._apply_styles(hwnd, self.engine.cfg.get("overlay") or {})
        pos = self.engine.cfg.get("overlay_pos")
        work = winplat.work_area_at(*pos) if pos else None
        r = winplat.window_rect(hwnd)
        if hwnd and r:
            ww, hh = r[2] - r[0], r[3] - r[1]
            if work is None:
                work = winplat.work_area_of(hwnd)
                if work:                                  # first run: top-right, below the edge
                    pos = (work[2] - ww - 24, work[1] + 24)
            if work and pos:
                x, y = snap_position(pos[0], pos[1], ww, hh, work)
                winplat.set_window_pos(hwnd, x, y, topmost=True)

    def _apply_styles(self, hwnd, ov: dict) -> None:
        if not hwnd:
            return
        winplat.set_ex_style(hwnd, add=winplat.WS_EX_TOOLWINDOW | winplat.WS_EX_NOACTIVATE,
                             remove=winplat.WS_EX_APPWINDOW)
        click = bool(ov.get("click_through"))
        opacity = int(ov.get("opacity") or 100)
        if click or opacity < 100:
            winplat.set_ex_style(hwnd, add=winplat.WS_EX_LAYERED | (winplat.WS_EX_TRANSPARENT if click else 0),
                                 remove=0 if click else winplat.WS_EX_TRANSPARENT)
            winplat.set_alpha(hwnd, opacity)
        else:
            winplat.set_ex_style(hwnd, remove=winplat.WS_EX_LAYERED | winplat.WS_EX_TRANSPARENT)

    def _overlay_moved(self, *_):
        """Snap to the work-area edges once a drag settles, and remember the spot."""
        if self._move_timer:
            self._move_timer.cancel()
        self._move_timer = threading.Timer(0.4, self._settle_overlay)
        self._move_timer.daemon = True
        self._move_timer.start()

    def _settle_overlay(self) -> None:
        hwnd = _hwnd(self.overlay) if self.overlay else None
        r = winplat.window_rect(hwnd)
        work = winplat.work_area_of(hwnd)
        if not (r and work):
            return
        x, y = snap_position(r[0], r[1], r[2] - r[0], r[3] - r[1], work)
        if (x, y) != (r[0], r[1]):
            winplat.set_window_pos(hwnd, x, y, topmost=True)
        self.engine.save_internal(overlay_pos=[x, y])

    def apply_overlay(self, ov: dict) -> None:
        with self._lock:
            if self.webview is None:
                return
            if ov.get("shown"):
                if self.overlay is None:
                    self._create_overlay()
                    return
                if not self._overlay_visible:
                    self.overlay.show()
                    self._overlay_visible = True
                self._apply_styles(_hwnd(self.overlay), ov)
            elif self.overlay is not None and self._overlay_visible:
                self.overlay.hide()
                self._overlay_visible = False

    def resize_overlay(self, w: int, h: int) -> None:
        """overlay_resize: logical px from the page; grows away from the snapped edge."""
        if self.overlay is None:
            return
        hwnd = _hwnd(self.overlay)
        r, work = winplat.window_rect(hwnd), winplat.work_area_of(hwnd)
        if r and work:
            dpi = winplat.dpi_of(hwnd)
            x, y, pw, ph = anchored_resize(r, scale(w, dpi), scale(h, dpi), work)
            if winplat.set_window_pos(hwnd, x, y, pw, ph, topmost=True):
                return
        try:
            self.overlay.resize(w, h)
        except Exception as exc:
            io.log(f"overlay resize failed: {exc}")

    # ---- flyout ----
    def resize_flyout(self, w: int, h: int) -> None:
        """flyout_resize: the page's rendered size in logical px. Applied now if the flyout is open
        (keeping the edge nearest the taskbar fixed), otherwise used on the next open."""
        with self._lock:
            self._flyout_size = (w, h)
            if self.flyout is None or not self._flyout_visible:
                return
            hwnd = _hwnd(self.flyout)
            r, work = winplat.window_rect(hwnd), winplat.work_area_of(hwnd)
            if r and work:
                dpi = winplat.dpi_of(hwnd)
                winplat.set_window_pos(hwnd, *anchored_resize(r, scale(w, dpi), scale(h, dpi), work), topmost=True)

    def toggle_flyout(self, anchor=None) -> None:
        with self._lock:
            if self.flyout is None:
                return
            if self._flyout_visible:
                self.close_flyout()
                return
            if time.time() - self._flyout_hidden_at < 0.35:
                return                     # this click is what just closed it (deactivation)
            hwnd = _hwnd(self.flyout)
            pt = winplat.cursor_pos()
            if anchor is None and pt:
                anchor = (pt[0], pt[1], pt[0], pt[1])
            work = (winplat.work_area_at((anchor[0] + anchor[2]) // 2, (anchor[1] + anchor[3]) // 2)
                    if anchor else None)
            if hwnd and work:
                dpi = winplat.dpi_of(hwnd)
                w, h = scale(self._flyout_size[0], dpi), scale(self._flyout_size[1], dpi)
                x, y = flyout_position(anchor, work, w, h, margin=scale(12, dpi))
                winplat.set_window_pos(hwnd, x, y, w, h, topmost=True)
            self.flyout.show()
            self._flyout_visible = True
            self._flyout_shown_at = time.time()

    def close_flyout(self) -> None:
        with self._lock:
            if self.flyout is not None and self._flyout_visible:
                try:
                    self.flyout.hide()
                except Exception:
                    pass
                self._flyout_visible = False
                self._flyout_hidden_at = time.time()

    # ---- background checks ----
    def _watch(self) -> None:
        """Flyout: hide on deactivation. Overlay: hide while a full-screen app or presentation runs."""
        last_busy_check = 0.0
        while not self.engine.stopping:
            time.sleep(0.25)
            if self._flyout_visible and time.time() - getattr(self, "_flyout_shown_at", 0) > 0.5:
                fg = winplat.foreground_window()
                if fg and fg != _hwnd(self.flyout):
                    self.close_flyout()
            now = time.time()
            if now - last_busy_check < 2:
                continue
            last_busy_check = now
            ov = self.engine.cfg.get("overlay") or {}
            if not (self.overlay and ov.get("shown") and ov.get("auto_hide_fullscreen", True)):
                continue
            busy = winplat.notification_state() in ("busy", "fullscreen", "presentation")
            try:
                if busy and self._overlay_visible:
                    self.overlay.hide()
                    self._overlay_visible, self._overlay_auto_hidden = False, True
                elif not busy and self._overlay_auto_hidden:
                    self.overlay.show()
                    self._overlay_visible, self._overlay_auto_hidden = True, False
            except Exception:
                pass


class BrowserHost:
    """No pywebview: the dashboard opens in the browser; Quit stops the tray."""

    def __init__(self, engine, on_quit=None):
        self.engine, self.tray, self.on_quit = engine, None, on_quit

    def open_dashboard(self, view="now"):
        webbrowser.open(f"http://127.0.0.1:{self.engine.port}/#/{view}")

    def apply_overlay(self, ov):
        pass

    def resize_overlay(self, w, h):
        pass

    def resize_flyout(self, w, h):
        pass

    def close_flyout(self):
        pass

    def quit(self):
        if self.tray:
            self.tray.stop()
        if self.on_quit:
            self.on_quit()
