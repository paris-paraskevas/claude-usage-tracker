"""Tray icon renderer (pure) and its redraw key / tooltip."""
import pytest

pytest.importorskip("PIL")

from ctu import tray  # noqa: E402

STATES = [("64", "ok", False), ("64", "watch", False), ("64", "will_hit", False), (None, "limited", False),
          ("64", "ok", True), (None, "ok", False), ("7", "ok", False)]


@pytest.mark.parametrize("size", [16, 20, 24, 32])
def test_each_state_and_theme_renders_distinctly(size):
    for theme in ("light", "dark"):
        imgs = [tray.render_icon(size, *st, theme) for st in STATES]
        assert all(im.size == (size, size) and im.mode == "RGBA" for im in imgs)
        assert len({im.tobytes() for im in imgs}) == len(STATES)
    for st in STATES[:4]:                                             # taskbar colours follow the theme
        assert tray.render_icon(size, *st, "light").tobytes() != tray.render_icon(size, *st, "dark").tobytes()


def test_digits_use_taskbar_colours_and_badges_only_for_watch_and_hit():
    def colours(im):
        b = im.tobytes()
        return {tuple(b[i:i + 4]) for i in range(0, len(b), 4) if b[i + 3] == 255}
    light, dark = tray.render_icon(16, "64", "ok", False, "light"), tray.render_icon(16, "64", "ok", False, "dark")
    assert colours(light) == {tray.FG["light"]} and colours(dark) == {tray.FG["dark"]}
    stale = tray.render_icon(16, "64", "ok", True, "dark")
    assert tray.FG["dark"] not in colours(stale)                      # greyed
    watch = tray.render_icon(32, "64", "watch", False, "dark")
    assert tray.WATCH["dark"] in colours(watch)
    hit = tray.render_icon(32, "64", "will_hit", False, "light")
    assert tray.HIT["light"] in colours(hit)


def test_digits_for_binding_limit():
    st = {"decision": {"binding": "five_hour"}, "source": {"stale": False},
          "limits": [{"id": "five_hour", "used_pct": 64.7, "level": "watch"}]}
    assert tray.digits_for(st) == ("64", "watch", False)
    st["limits"][0].update(used_pct=99.9)
    assert tray.digits_for(st)[0] == "99"                             # never three digits
    st["limits"][0].update(used_pct=100.0, level="limited")
    assert tray.digits_for(st) == (None, "limited", False)            # clock glyph
    assert tray.digits_for({}) == (None, "ok", True)
    assert tray.icon_key(st, "dark", 20) == (None, "limited", False, "dark", 20)


def test_tooltip_fits_sztip():
    assert tray.tooltip({"decision": {"title": "On pace", "detail": "5-hour 38% · refills 16:40."}}) == \
        "Claude usage: On pace\n5-hour 38% · refills 16:40."
    long = tray.tooltip({"decision": {"title": "x" * 80, "detail": "y" * 80}})
    assert len(long) == 127 and long.endswith("…")


def test_app_icon():
    im = tray.make_app_icon(64)
    assert im.size == (64, 64) and im.getpixel((32, 32))[3] == 255
