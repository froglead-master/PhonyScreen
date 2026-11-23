"""The frame around the phone screen: presses on it start touches at the nearest screen edge."""

import asyncio

import pytest

from phonyscreen import viewer as v

PORTRAIT = (420, 912)
LANDSCAPE = (912, 420)
Y0 = v.phone_origin_y()


def frame_point(disp, bz, side):
    """A window point in the middle of one side of the frame (or a corner)."""
    w, h = disp
    mid_x, mid_y = bz + w // 2, Y0 + bz + h // 2
    half = bz // 2
    return {
        "top": (mid_x, Y0 + half),
        "bottom": (mid_x, Y0 + bz + h + half),
        "left": (half, mid_y),
        "right": (bz + w + half, mid_y),
        "top_right": (bz + w + half, Y0 + half),
    }[side]


@pytest.mark.parametrize("disp", [PORTRAIT, LANDSCAPE])
def test_frame_maps_to_nearest_edge(disp):
    bz = v.bezel_width(*disp)
    w, h = disp
    expect = {"top": (w // 2, 0), "bottom": (w // 2, h - 1), "left": (0, h // 2), "right": (w - 1, h // 2),
              "top_right": (w - 1, 0)}
    for side, edge in expect.items():
        pos = frame_point(disp, bz, side)
        assert v.on_bezel(pos, disp, bz), side
        assert v.window_to_picture(pos, disp, bz) == edge, side


@pytest.mark.parametrize("disp", [PORTRAIT, LANDSCAPE])
def test_screen_and_outside_are_not_frame(disp):
    bz = v.bezel_width(*disp)
    w, h = disp
    assert not v.on_bezel((bz + w // 2, Y0 + bz + h // 2), disp, bz)  # the screen
    assert not v.on_bezel((bz, Y0 + bz), disp, bz)  # first screen pixel
    assert not v.on_bezel((w + 2 * bz + 20, Y0 + 100), disp, bz)  # the gesture lab's panel
    assert not v.on_bezel((w // 2, 5), disp, bz)  # the pill bar
    assert v.window_to_picture((bz, Y0 + bz), disp, bz) == (0, 0)


def test_bezel_width_scales_and_stays_in_range():
    assert v.bezel_width(200, 430) == v.BEZEL_MIN
    assert v.bezel_width(420, 912) == 15
    assert v.bezel_width(900, 1950) == v.BEZEL_MAX
    assert v.bezel_width(*LANDSCAPE) == v.bezel_width(*PORTRAIT)


def test_tap_on_frame_does_nothing():
    b = v.BezelDrag()
    b.press("mouse", (100, 990), (85, 911))
    assert b.move("mouse", (103, 988)) is None  # a wobble, not a drag
    assert b.release("mouse") is True  # caller ignores it
    assert b.release("mouse") is False


def test_drag_from_frame_starts_at_edge_once():
    b = v.BezelDrag()
    b.press(7, (100, 990), (85, 911))
    assert b.move(7, (100, 984)) == (85, 911)
    assert not b.waiting(7)
    assert b.move(7, (100, 900)) is None  # from now on, ordinary finger moves
    assert b.release(7) is False


def test_touchscreen_home_from_bottom_frame():
    """A finger pressed on the bottom frame and swiped up counts as the Home swipe."""
    w, h = PORTRAIT
    bz = v.bezel_width(w, h)
    b, f = v.BezelDrag(), v.FingerTracker()
    start = frame_point(PORTRAIT, bz, "bottom")
    b.press(1, start, v.window_to_picture(start, PORTRAIT, bz))
    pos = (start[0], start[1] - 10)
    edge = b.move(1, pos)
    assert edge == (w // 2, h - 1)
    assert f.down(1, *edge, w, h) == [(0, w // 2, h - 1, True)]
    for y in range(h - 20, int(h * 0.7), -20):
        f.move(1, w // 2, y)
    f.up(1)
    assert f.home_swipe and not f.app_switcher


def test_pointer_home_from_bottom_frame():
    """The pointer path: the touch starts at the bottom edge, so _track_home_swipe presses Home."""
    w, h = PORTRAIT
    m = object.__new__(v.LeanMirror)
    m._fb_height = h
    pressed = []

    async def button(page, code, *, down):
        pressed.append((page, code, down))

    m._send_hid_button = button

    async def drag():
        await m._track_home_swipe(1, w // 2, h - 1, False)  # press at the edge
        for y in range(h - 30, int(h * 0.7), -30):
            await m._track_home_swipe(1, w // 2, y, True)
        await m._track_home_swipe(0, w // 2, int(h * 0.7), True)  # release while moving

    asyncio.run(drag())
    assert pressed == [(*v.HOME_BUTTON, True), (*v.HOME_BUTTON, False)]
