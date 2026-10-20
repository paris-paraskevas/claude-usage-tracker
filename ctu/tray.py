"""Tray icon: a pure renderer (testable anywhere) and the pystray host (Windows).

The icon shows the binding limit's used % as two pixel-snapped digits in taskbar colours, a
bottom-right badge for watch (dot) / will-hit (triangle), a clock when limited, and grey digits
with a slashed badge when the data is stale. It is redrawn only when (digits, level, stale,
theme, size) change."""
from __future__ import annotations

import math
import os
import sys
import tempfile
import threading
import time

from ctu import APP_NAME, io, paths, winplat

SEG = {"0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc", "5": "afgcd", "6": "afgedc",
       "7": "abc", "8": "abcdefg", "9": "abcdfg", "-": "g"}
FG = {"light": (28, 28, 28, 255), "dark": (255, 255, 255, 255)}     # by taskbar theme
STALE = (138, 138, 138, 255)
WATCH = {"light": (230, 159, 0, 255), "dark": (242, 181, 68, 255)}
HIT = {"light": (196, 71, 10, 255), "dark": (240, 123, 63, 255)}


def digits_for(state: dict | None):
    """(digits | None, level, stale): the binding limit's used % as at most two digits."""
    if not state:
        return None, "ok", True
    d = state.get("decision") or {}
    lim = next((l for l in state.get("limits") or [] if l["id"] == d.get("binding")), None)
    stale = bool((state.get("source") or {}).get("stale"))
    if lim is None:
        return None, "ok", stale
    level = lim["level"]
    if level == "limited":
        return None, level, stale
    return str(min(99, int(math.floor(lim["used_pct"])))), level, stale


def icon_key(state: dict | None, theme: str, size: int) -> tuple:
    return (*digits_for(state), theme, size)


def _draw_digit(d, ch, x0, y0, w, h, t, color):
    ym = y0 + (h - t) // 2
    boxes = {"a": (x0, y0, x0 + w - 1, y0 + t - 1), "d": (x0, y0 + h - t, x0 + w - 1, y0 + h - 1),
             "g": (x0, ym, x0 + w - 1, ym + t - 1), "f": (x0, y0, x0 + t - 1, ym + t - 1),
             "b": (x0 + w - t, y0, x0 + w - 1, ym + t - 1), "e": (x0, ym, x0 + t - 1, y0 + h - 1),
             "c": (x0 + w - t, ym, x0 + w - 1, y0 + h - 1)}
    for s in SEG.get(ch, ""):
        d.rectangle(boxes[s], fill=color)


def _ss_layer(size: int, draw_fn, ss: int = 4):
    """Draw at `ss`x and downsample: smooth circles / triangles at 16 px."""
    from PIL import Image, ImageDraw
    big = Image.new("RGBA", (size * ss, size * ss), (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(big), size * ss)
    return big.resize((size, size), Image.LANCZOS)


def render_icon(size: int, digits, level: str, stale: bool, theme: str = "dark"):
    """The tray image for one state. `theme` is the taskbar's ('light' -> dark digits)."""
    from PIL import Image, ImageDraw
    theme = theme if theme in FG else "dark"
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    fg = STALE if stale else FG[theme]
    if level == "limited" and not stale:
        col = HIT[theme]

        def clock(d, S):
            w = max(4, S // 9)
            d.ellipse([w // 2, w // 2, S - 1 - w // 2, S - 1 - w // 2], outline=col, width=w)
            c = S // 2
            d.line([c, c, c, int(S * 0.24)], fill=col, width=w)
            d.line([c, c, int(S * 0.72), c], fill=col, width=w)
        return Image.alpha_composite(img, _ss_layer(size, clock))
    d = ImageDraw.Draw(img)
    gap = max(1, size // 16)
    t = max(2, round(size / 8))
    my = max(1, size // 16)
    h = size - 2 * my
    w = (size - gap) // 2
    text = digits if digits is not None else "--"
    if len(text) == 1:
        _draw_digit(d, text, (size - w) // 2, my, w, h, t, fg)
    else:
        _draw_digit(d, text[0], 0, my, w, h, t, fg)
        _draw_digit(d, text[1], w + gap, my, w, h, t, fg)
    badge = "stale" if stale else level if level in ("watch", "will_hit") else None
    if badge:
        b = max(6, round(size * 0.4))
        x0 = y0 = size - b
        d.rectangle([x0 - 1, y0 - 1, size - 1, size - 1], fill=(0, 0, 0, 0))     # cut-out ring

        def draw_badge(dd, S):
            k = S / size
            bx, by, bb = x0 * k, y0 * k, b * k
            if badge == "watch":
                dd.ellipse([bx, by, bx + bb - 1, by + bb - 1], fill=WATCH[theme])
            elif badge == "will_hit":
                dd.polygon([(bx + bb / 2, by), (bx + bb - 1, by + bb - 1), (bx, by + bb - 1)], fill=HIT[theme])
            else:
                lw = max(2, round(bb / 6))
                dd.ellipse([bx + lw / 2, by + lw / 2, bx + bb - 1 - lw / 2, by + bb - 1 - lw / 2], outline=STALE, width=lw)
                dd.line([bx + bb * 0.2, by + bb * 0.8, bx + bb * 0.8, by + bb * 0.2], fill=STALE, width=lw)
        img = Image.alpha_composite(img, _ss_layer(size, draw_badge))
    return img


def tooltip(state: dict | None) -> str:
    """'Claude usage: <title>\\n<detail>' within szTip's 127 characters."""
    d = (state or {}).get("decision") or {}
    text = f"Claude usage: {d.get('title') or 'starting…'}"
    if d.get("detail"):
        text += "\n" + d["detail"]
    return text if len(text) <= 127 else text[:126] + "…"


def make_app_icon(size: int = 256):
    """The app / window / toast icon: a coral tile with a cream sunburst (rendered at 4x)."""
    from PIL import Image, ImageDraw
    ss = 4
    S = size * ss
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    pad, radius = int(S * 0.045), int(S * 0.225)
    top, bot = (228, 138, 105), (196, 101, 72)
    grad = Image.new("RGB", (1, S))
    for y in range(S):
        f = y / (S - 1)
        grad.putpixel((0, y), tuple(int(top[i] + (bot[i] - top[i]) * f) for i in range(3)))
    grad = grad.resize((S, S))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([pad, pad, S - pad, S - pad], radius=radius, fill=255)
    img.paste(grad, (0, 0), mask)
    cx = cy = S / 2.0
    half_w, reach, cream = S * 0.049, S * 0.31, (245, 243, 236, 255)
    burst = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    for k in range(6):
        petal = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ImageDraw.Draw(petal).ellipse([cx - half_w, cy - reach, cx + half_w, cy + reach], fill=cream)
        burst = Image.alpha_composite(burst, petal.rotate(k * 30.0, resample=Image.BICUBIC, center=(cx, cy)))
    ImageDraw.Draw(burst).ellipse([cx - S * 0.08, cy - S * 0.08, cx + S * 0.08, cy + S * 0.08], fill=cream)
    return Image.alpha_composite(img, burst).resize((size, size), Image.LANCZOS)


def ensure_app_icon() -> None:
    try:
        if paths.ICON_PATH.exists() and paths.ICO_PATH.exists():
            return
        paths.ensure_data_dir()
        base = make_app_icon(256)
        base.save(paths.ICON_PATH)
        base.save(paths.ICO_PATH, sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48),
                                         (64, 64), (128, 128), (256, 256)])
    except Exception as exc:
        io.log(f"icon generation failed: {exc}")


# ---------------------------------------------------------------- pystray host (Windows)

def _icon_class():
    import pystray
    base = pystray.Icon
    if sys.platform != "win32":
        return base
    from pystray._util import win32

    class CtuIcon(base):
        on_left_click = None

        def _assert_icon_handle(self):
            # pystray loads with LR_DEFAULTSIZE (the 32 px SM_CXICON) and Pillow drops frames
            # larger than 16 px from a 20 px image: both blur. Write one exact frame, load it as is.
            if self._icon_handle:
                return
            s = self.icon.size[0]
            fd, p = tempfile.mkstemp(suffix=".ico")
            os.close(fd)
            try:
                self.icon.save(p, format="ICO", sizes=[(s, s)])
                self._icon_handle = win32.LoadImage(None, p, win32.IMAGE_ICON, s, s, win32.LR_LOADFROMFILE)
            finally:
                try:
                    os.unlink(p)
                except Exception:
                    pass

        def _on_notify(self, wparam, lparam):
            if lparam == win32.WM_LBUTTONUP and self.on_left_click:
                try:
                    self.on_left_click()
                except Exception:
                    io.log("tray click failed")
                return
            return super()._on_notify(wparam, lparam)

    return CtuIcon


class TrayHost:
    """The notification-area icon. `shell` (windows.Shell) provides the flyout / overlay."""

    def __init__(self, engine, shell=None):
        self.engine, self.shell = engine, shell
        self.icon = None
        self._key = None
        self._tip = None
        self._menu_key = None
        self._theme = "dark"
        self._size = 16
        self._lock = threading.Lock()

    def anchor(self):
        """The tray icon's screen rect (for the flyout), or None."""
        hwnd = getattr(self.icon, "_hwnd", None)
        return winplat.notify_icon_rect(hwnd, 0) if hwnd else None

    def _menu(self):
        import pystray
        e = self.engine
        Item, Menu = pystray.MenuItem, pystray.Menu
        overlay_on = lambda i: bool((e.cfg.get("overlay") or {}).get("shown"))   # noqa: E731
        return Menu(
            Item("Open dashboard", lambda i, it: e.open_dashboard("now"), default=True),
            Item("Show overlay", lambda i, it: e.action("overlay", {"show": not overlay_on(i)}), checked=overlay_on,
                 visible=self.shell is not None),
            Item("Mute alerts", Menu(
                Item("For 1 hour", lambda i, it: e.action("mute", {"for": "hour"})),
                Item("Until refill", lambda i, it: e.action("mute", {"for": "refill"})),
                Item("Unmute", lambda i, it: e.action("mute", {"for": "off"}),
                     enabled=lambda i: bool((e.state.get("mute") or {}).get("until"))))),
            Item("Refresh now", lambda i, it: e.refresh()),
            Item("Settings", lambda i, it: e.open_dashboard("settings")),
            Menu.SEPARATOR,
            Item("Quit", lambda i, it: e.quit()),
        )

    def run(self, on_ready=None) -> None:
        """Blocking: runs the icon's message loop on the calling thread."""
        ensure_app_icon()
        self._theme = "light" if winplat.taskbar_light() else "dark"
        self._size = winplat.small_icon_size()
        st = self.engine.snapshot()
        self._key = icon_key(st, self._theme, self._size)
        cls = _icon_class()
        self.icon = cls(APP_NAME, icon=render_icon(self._size, *self._key[:3], self._theme),
                        title=tooltip(st), menu=self._menu())
        self._tip = tooltip(st)
        self.icon.on_left_click = self._left_click
        self.engine.listeners.append(self._on_state)
        threading.Thread(target=self._watch_platform, name="ctu-tray-theme", daemon=True).start()

        def setup(icon):
            icon.visible = True
            if on_ready:
                on_ready()
        self.icon.run(setup=setup)

    def stop(self) -> None:
        try:
            if self.icon:
                self.icon.stop()
        except Exception:
            pass

    def _left_click(self) -> None:
        if self.shell is not None:
            self.shell.toggle_flyout(self.anchor())
        else:
            self.engine.open_dashboard("now")

    def _on_state(self, st) -> None:
        self._redraw(st)

    def _redraw(self, st=None) -> None:
        st = st or self.engine.state
        with self._lock:
            if not self.icon:
                return
            key = icon_key(st, self._theme, self._size)
            try:
                if key != self._key:
                    self._key = key
                    self.icon.icon = render_icon(self._size, *key[:3], self._theme)
                tip = tooltip(st)
                if tip != self._tip:
                    self._tip = tip
                    self.icon.title = tip
                mk = ((st.get("mute") or {}).get("until"), (st.get("app") or {}).get("overlay"))
                if mk != self._menu_key:
                    self._menu_key = mk
                    self.icon.update_menu()        # checked / enabled states
            except Exception as exc:
                io.log(f"tray redraw failed: {exc}")

    def _watch_platform(self) -> None:
        """Taskbar theme and DPI, re-checked every 30 s (no message hook)."""
        while not self.engine.stopping:
            time.sleep(30)
            theme = "light" if winplat.taskbar_light() else "dark"
            size = winplat.small_icon_size()
            if (theme, size) != (self._theme, self._size):
                self._theme, self._size = theme, size
                self._redraw()
