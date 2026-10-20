"""Windows helpers: the pure colour converters, and safe defaults off Windows."""
import pytest

from ctu import winplat


def test_colour_converters():
    assert winplat.abgr_to_hex(0xFFB85F00) == "#005FB8"          # AccentColor is ABGR
    assert winplat.argb_to_hex(0xC4005FB8) == "#005FB8"          # ColorizationColor is ARGB
    pal = bytes([0x99, 0xEB, 0xFF, 0, 0x60, 0xCD, 0xFF, 0, 0x00, 0x9C, 0xEA, 0, 0x00, 0x78, 0xD4, 0,
                 0x00, 0x5F, 0xB8, 0, 0x00, 0x42, 0x81, 0, 0x00, 0x26, 0x42, 0, 0, 0, 0, 0])
    assert winplat.accent_from_palette(pal) == {"light": "#005FB8", "dark": "#60CDFF"}
    assert winplat.accent_from_palette(b"short") is None
    a = winplat.accent_from_base("#0078D4")
    assert a["light"] < "#0078D4" and set(a) == {"light", "dark"}


@pytest.mark.skipif(winplat.IS_WIN, reason="off-Windows defaults")
def test_defaults_off_windows():
    assert winplat.accent() == winplat.DEFAULT_ACCENT
    assert winplat.taskbar_light() is True and winplat.apps_light() is True
    assert winplat.small_icon_size(96) == 16 and winplat.small_icon_size(144) == 24
    assert winplat.notification_state() is None and winplat.notify_icon_rect(1, 0) is None
    assert winplat.set_ex_style(1, add=winplat.WS_EX_LAYERED) is False
    assert winplat.work_area_at(0, 0) is None and winplat.register_protocol("x") is False
