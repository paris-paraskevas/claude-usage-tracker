"""The desktop shell's logic against fake pywebview / pystray modules (the real ones are Windows
GUI code; the ctypes geometry helpers no-op off Windows)."""
import sys
import time
import types

import pytest

from ctu import tray, windows


class FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self

    def fire(self):
        for fn in self.handlers:
            fn()


class FakeWindow:
    def __init__(self, title, url, **kw):
        self.title, self.url, self.kw, self.calls = title, url, kw, []
        self.events = types.SimpleNamespace(closed=FakeEvent(), shown=FakeEvent(), moved=FakeEvent())

    def __getattr__(self, name):
        return lambda *a: self.calls.append((name, a))


@pytest.fixture
def fake_webview(monkeypatch):
    mod = types.ModuleType("webview")
    mod.windows, mod.started = [], []

    def create_window(title, url=None, width=800, height=600, min_size=(200, 100), hidden=False, frameless=False,
                      easy_drag=True, on_top=False, focus=True, background_color="#FFFFFF", resizable=True):
        w = FakeWindow(title, url, width=width, height=height, hidden=hidden, on_top=on_top, focus=focus,
                       frameless=frameless, background_color=background_color)
        mod.windows.append(w)
        return w
    mod.create_window = create_window
    mod.start = lambda func=None, **kw: mod.started.append(kw)
    monkeypatch.setitem(sys.modules, "webview", mod)
    return mod


def test_shell_windows_lifecycle(make_engine, fake_webview):
    e = make_engine()
    e.port = 8787
    sh = windows.Shell(e)
    sh.run()
    flyout, overlay = fake_webview.windows
    assert flyout.url.endswith("/flyout") and flyout.kw["hidden"] and flyout.kw["on_top"] and flyout.kw["frameless"]
    assert overlay.url.endswith("/overlay") and overlay.kw["focus"] is False and overlay.kw["on_top"]
    assert "shadow" not in overlay.kw                                  # unsupported kwargs are dropped
    assert fake_webview.started[0]["private_mode"] is False

    sh.open_dashboard("history")
    dash = fake_webview.windows[2]
    assert dash.url == "http://127.0.0.1:8787/#/history" and dash.kw["width"] == 1100
    sh.open_dashboard("team")                                          # reused, not recreated
    assert len(fake_webview.windows) == 3 and ("evaluate_js", ("location.hash = '#/team'",)) in dash.calls
    dash.events.closed.fire()
    sh.open_dashboard("now")
    assert len(fake_webview.windows) == 4                              # recreated after a close

    sh.toggle_flyout((100, 1000, 120, 1020))
    assert ("show", ()) in flyout.calls and sh._flyout_visible
    sh.toggle_flyout(None)                                             # second click closes
    assert ("hide", ()) in flyout.calls and not sh._flyout_visible
    sh.toggle_flyout(None)                                             # the click that just closed it: ignored
    assert not sh._flyout_visible
    time.sleep(0.4)
    sh.toggle_flyout(None)
    assert sh._flyout_visible
    sh.close_flyout()

    sh.apply_overlay({"shown": False})
    assert ("hide", ()) in overlay.calls and not sh._overlay_visible
    sh.apply_overlay({"shown": True})
    assert ("show", ()) in overlay.calls
    sh.resize_overlay(340, 260)                                        # no Win32 here: falls back to pywebview
    assert ("resize", (340, 260)) in overlay.calls
    sh.quit()
    assert all(("destroy", ()) in w.calls for w in (flyout, overlay))


def test_engine_actions_drive_the_shell(make_engine, fake_webview):
    e = make_engine()
    e.port = 8787
    sh = windows.Shell(e)
    e.host = sh
    sh.run()
    overlay = fake_webview.windows[1]
    e.action("overlay", {"show": False})
    assert ("hide", ()) in overlay.calls
    e.action("open_dashboard", {"view": "settings"})
    assert fake_webview.windows[-1].url.endswith("/#/settings")
    e.action("flyout_close", {})


@pytest.fixture
def fake_pystray(monkeypatch):
    mod = types.ModuleType("pystray")

    class MenuItem:
        def __init__(self, text, action, checked=None, radio=False, default=False, visible=True, enabled=True):
            self.text, self.action, self.checked, self.default, self.visible, self.enabled = \
                text, action, checked, default, visible, enabled

    class Menu:
        SEPARATOR = object()

        def __init__(self, *items):
            self.items = items

    class Icon:
        def __init__(self, name, icon=None, title=None, menu=None):
            self.name, self._icon, self._title, self.menu, self.menu_updates = name, icon, title, menu, 0
            self.icons = [icon]

        icon = property(lambda s: s._icon, lambda s, v: (setattr(s, "_icon", v), s.icons.append(v)))
        title = property(lambda s: s._title, lambda s, v: setattr(s, "_title", v))

        def update_menu(self):
            self.menu_updates += 1

        def run(self, setup=None):
            if setup:
                setup(self)

        def stop(self):
            pass
    mod.MenuItem, mod.Menu, mod.Icon = MenuItem, Menu, Icon
    monkeypatch.setitem(sys.modules, "pystray", mod)
    return mod


def test_tray_redraws_only_on_change(make_engine, fake_pystray):
    pytest.importorskip("PIL")
    from conftest import feed
    e = make_engine()
    t = tray.TrayHost(e, None)
    t.run()
    icon = t.icon
    menu = icon.menu.items
    assert menu[0].text == "Open dashboard" and menu[0].default is True
    assert [m.text for m in menu if m is not fake_pystray.Menu.SEPARATOR] == [
        "Open dashboard", "Show overlay", "Mute alerts", "Refresh now", "Settings", "Quit"]
    now = time.time()
    feed(e, "five_hour", [(now - 30, 64.2)], int(now + 3600))
    t._redraw(e.state)
    n = len(icon.icons)
    assert icon.title.startswith("Claude usage: On pace") and len(icon.title) <= 127
    for _ in range(3):
        t._redraw(e.state)                     # same digits / level / theme / size: no redraw
    assert len(icon.icons) == n
    feed(e, "five_hour", [(now - 20, 64.9)], int(now + 3600))
    t._redraw(e.state)
    assert len(icon.icons) == n                # still "64"
    feed(e, "five_hour", [(now - 10, 65.0)], int(now + 3600))
    t._redraw(e.state)
    assert len(icon.icons) == n + 1
    opened = []
    e.open_dashboard = lambda view="now": opened.append(view)
    t._left_click()                            # no shell: left-click opens the dashboard
    menu[4].action(icon, menu[4])
    assert opened == ["now", "settings"]
