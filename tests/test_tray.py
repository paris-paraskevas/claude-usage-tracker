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
    assert tray.icon_key(st, "dark", 20) == ("number", None, "limited", False, "dark", 20)


def test_tooltip_fits_sztip():
    assert tray.tooltip({"decision": {"title": "On pace", "detail": "5-hour 38% · refills 16:40."}}) == \
        "Claude usage: On pace\n5-hour 38% · refills 16:40."
    long = tray.tooltip({"decision": {"title": "x" * 80, "detail": "y" * 80}})
    assert len(long) == 127 and long.endswith("…")


def test_app_icon():
    im = tray.make_app_icon(64)
    assert im.size == (64, 64) and im.getpixel((32, 32))[3] == 255


def _px(img, x, y):
    return img.getpixel((x, y))


def test_bars_fill_height_and_colour_follow_the_forecast():
    for size in (16, 20, 24, 32):
        imgs = {lv: tray.render_bars(size, (50, lv), (20, "ok"), False, "dark").tobytes()
                for lv in ("ok", "watch", "will_hit")}
        assert len(set(imgs.values())) == 3                         # each level looks different
    half = tray.render_bars(16, (50, "ok"), (0, "ok"), False, "dark")
    full = tray.render_bars(16, (40, "limited"), (0, "ok"), False, "dark")
    # a limited window is drawn full and vermillion regardless of the reported %
    assert _px(full, 3, 3)[:3] == tray.HIT["dark"][:3]
    assert _px(half, 3, 3)[3] < 60 and _px(half, 3, 12) == tray.FG["dark"]   # empty above, filled below
    stale = tray.render_bars(16, (50, "will_hit"), (20, "ok"), True, "dark")
    assert _px(stale, 3, 12)[:3] == tray.STALE[:3]                 # stale: grey, never red
    light = tray.render_bars(16, (50, "ok"), (20, "ok"), False, "light")
    assert _px(light, 3, 12)[:3] == tray.FG["light"][:3]             # dark fill on a light taskbar


def test_bars_sliver_for_tiny_usage_and_empty_when_unknown():
    tiny = tray.render_bars(16, (1, "ok"), None, False, "dark")
    empty = tray.render_bars(16, (0, "ok"), None, False, "dark")
    assert tiny.tobytes() != empty.tobytes()


def test_icon_key_styles():
    st = {"decision": {"binding": "five_hour"}, "source": {"stale": False},
          "limits": [{"id": "five_hour", "used_pct": 64.4, "level": "will_hit"},
                     {"id": "seven_day", "used_pct": 41.0, "level": "ok"}]}
    assert tray.icon_key(st, "dark", 16, "bars") == ("bars", (64, "will_hit"), (41, "ok"), False, "dark", 16)
    assert tray.icon_key(st, "dark", 16, "number")[:2] == ("number", "64")
    for style in ("bars", "number"):
        assert tray.render_key(tray.icon_key(st, "light", 24, style)).size == (24, 24)
