"""Window placement maths (pure): edge snapping, anchored resize, flyout positioning."""
from ctu import windows as w

WORK = (0, 0, 1920, 1040)          # 1920x1080 with a 40 px bottom taskbar


def test_snap_position():
    assert w.snap_position(8, 500, 300, 30, WORK) == (0, 500)                 # near left edge
    assert w.snap_position(1610, 3, 300, 30, WORK) == (1620, 0)               # near right + top
    assert w.snap_position(700, 1000, 300, 30, WORK) == (700, 1010)           # near bottom
    assert w.snap_position(700, 500, 300, 30, WORK) == (700, 500)             # free
    assert w.snap_position(-500, 5000, 300, 30, WORK) == (0, 1010)            # dragged off-screen: back on
    assert w.snap_position(100, 100, 300, 30, (1920, 0, 3840, 1080)) == (1920, 100)   # second monitor


def test_anchored_resize_grows_away_from_the_snapped_edge():
    assert w.anchored_resize((1620, 0, 1920, 30), 340, 260, WORK) == (1580, 0, 340, 260)    # top-right: grow left/down
    assert w.anchored_resize((0, 1010, 300, 1040), 340, 260, WORK) == (0, 780, 340, 260)    # bottom-left: grow up
    assert w.anchored_resize((700, 500, 1000, 530), 340, 260, WORK) == (700, 500, 340, 260)
    assert w.anchored_resize((1580, 0, 1920, 260), 300, 30, WORK) == (1620, 0, 300, 30)     # collapse back


def test_flyout_position_for_each_taskbar_edge():
    fw, fh = 320, 400
    assert w.flyout_position((1800, 1048, 1824, 1072), WORK, fw, fh) == (1588, 628)        # bottom
    assert w.flyout_position((1800, 8, 1824, 32), (0, 40, 1920, 1080), fw, fh) == (1588, 52)   # top
    assert w.flyout_position((1888, 900, 1912, 924), (0, 0, 1880, 1080), fw, fh) == (1548, 668)  # right, clamped
    assert w.flyout_position((8, 900, 32, 924), (40, 0, 1920, 1080), fw, fh) == (52, 668)      # left
    assert w.flyout_position((1700, 900, 1700, 900), WORK, fw, fh) == (1540, 488)          # cursor in the overflow
    assert w.flyout_position(None, WORK, fw, fh) == (1588, 628)


def test_scale():
    assert w.scale(320, 96) == 320 and w.scale(320, 144) == 480 and w.scale(30, 120) == 38
