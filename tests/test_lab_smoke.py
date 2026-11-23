"""Runs the real window loop in gesture lab mode against a fake phone (SDL dummy video driver):
two-finger scroll, a pointer Home drag from the bottom frame, a touchscreen Home swipe from the
bottom frame, a tap on the frame; then checks what reached the "phone" and that the recording
can be analyzed."""

import os
import threading
import time

import pytest

os.environ["SDL_VIDEODRIVER"] = "dummy"

import pygame  # noqa: E402

from phonyscreen import config, gesturelab, gesturelab_analyze, viewer  # noqa: E402

W, H, PAD_W = 420, 912, 448  # visible picture, decoded width (multiple of 64)


class FakeMirror:
    def __init__(self):
        self.posted = []
        self.phone_clip = self.phone_image = None
        self.status = ""
        self.backlight = "activeOn"
        self.stats = viewer.Stats()
        self.slot_lock = threading.Lock()
        self.newframe_pending = threading.Event()
        self.display_map = None
        self._rotation = 0
        self._display_size = (1206, 2622)
        self.target_height = H
        self.slot = (bytes(PAD_W * H * 3), PAD_W, H, W, H, time.perf_counter(), 1)

    @property
    def last_feed_t(self):
        return time.perf_counter()

    def post_input(self, *event):
        if gesturelab.REC is not None:
            gesturelab.on_post(event)
        self.posted.append((time.perf_counter(), event))

    def request_stop(self):
        pass

    def request_restart(self, *a, **k):
        pass

    def set_sound(self, on):
        pass

    def set_mute_phone(self, on):
        pass

    def pull_phone_clipboard(self, delay=0.0):
        pass


def test_lab_window_loop(tmp_path, monkeypatch):
    mirror = FakeMirror()
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config, "load_config", lambda: {})
    monkeypatch.setattr(config, "update_config", lambda **kw: None)
    monkeypatch.setattr(gesturelab, "lab_dir", lambda: tmp_path / "lab")

    def fake_backend(box, target_h, ready, rsd_addr, prepare):
        box["mirror"] = mirror
        ready.set()
        pygame.event.post(pygame.event.Event(viewer.NEWFRAME))

    monkeypatch.setattr(viewer, "run_backend", fake_backend)
    mouse = {"pos": (0, 0)}
    monkeypatch.setattr(pygame.mouse, "get_pos", lambda: mouse["pos"])

    y0 = viewer.phone_origin_y()
    bz = viewer.bezel_width(W, H)
    marks = {}
    errors = []

    def post(type_, **kw):
        pygame.event.post(pygame.event.Event(type_, **kw))

    def key(k, ch=""):
        post(pygame.KEYDOWN, key=k, mod=0, unicode=ch, scancode=0)
        post(pygame.KEYUP, key=k, mod=0, unicode=ch, scancode=0)

    def mouse_ev(type_, pos, **kw):
        mouse["pos"] = pos
        post(type_, pos=pos, touch=False, **kw)

    def script():
        try:
            time.sleep(0.6)
            # 1: two-finger scroll in the middle of the screen
            key(pygame.K_SPACE, " ")
            mouse["pos"] = (bz + W // 2, y0 + bz + H // 2)
            for _ in range(25):
                post(pygame.MOUSEWHEEL, x=0, y=0, precise_x=0.0, precise_y=-0.35, flipped=False, which=0, touch=False)
                time.sleep(0.008)
            time.sleep(0.35)
            key(pygame.K_SPACE, " ")
            key(pygame.K_3, "3")
            # 2: pointer press on the bottom frame, drag up
            key(pygame.K_SPACE, " ")
            marks["drag0"] = time.perf_counter()
            x, yb = bz + W // 2, y0 + bz + H + bz // 2
            mouse_ev(pygame.MOUSEBUTTONDOWN, (x, yb), button=1)
            for i in range(1, 12):
                mouse_ev(pygame.MOUSEMOTION, (x, yb - i * 30), rel=(0, -30), buttons=(1, 0, 0))
                time.sleep(0.016)
            mouse_ev(pygame.MOUSEBUTTONUP, (x, yb - 330), button=1)
            time.sleep(0.2)
            marks["drag1"] = time.perf_counter()
            key(pygame.K_SPACE, " ")
            key(pygame.K_2, "2")
            # 3: touchscreen finger from the bottom frame, swipe up
            key(pygame.K_SPACE, " ")
            win_w, win_h = W + 2 * bz + gesturelab.PANEL_W, y0 + H + 2 * bz
            fx, fy = (bz + W // 2) / win_w, (y0 + bz + H + bz // 2) / win_h
            post(pygame.FINGERDOWN, touch_id=1, finger_id=5, x=fx, y=fy, dx=0, dy=0, pressure=1.0)
            for i in range(1, 12):
                post(pygame.FINGERMOTION, touch_id=1, finger_id=5, x=fx, y=fy - i * 30 / win_h, dx=0, dy=0, pressure=1.0)
                time.sleep(0.016)
            post(pygame.FINGERUP, touch_id=1, finger_id=5, x=fx, y=fy - 330 / win_h, dx=0, dy=0, pressure=0.0)
            time.sleep(0.2)
            key(pygame.K_SPACE, " ")
            key(pygame.K_1, "1")
            # 4: taps on the frame do nothing
            key(pygame.K_SPACE, " ")
            marks["tap0"] = time.perf_counter()
            mouse_ev(pygame.MOUSEBUTTONDOWN, (bz // 2, y0 + bz + 300), button=1)
            mouse_ev(pygame.MOUSEMOTION, (bz // 2 + 2, y0 + bz + 301), rel=(2, 1), buttons=(1, 0, 0))
            mouse_ev(pygame.MOUSEBUTTONUP, (bz // 2 + 2, y0 + bz + 301), button=1)
            post(pygame.FINGERDOWN, touch_id=1, finger_id=6, x=fx, y=fy, dx=0, dy=0, pressure=1.0)
            post(pygame.FINGERUP, touch_id=1, finger_id=6, x=fx, y=fy, dx=0, dy=0, pressure=0.0)
            time.sleep(0.2)
            marks["tap1"] = time.perf_counter()
            key(pygame.K_SPACE, " ")
            time.sleep(0.2)
        except Exception as e:  # pragma: no cover
            errors.append(e)
        finally:
            post(pygame.QUIT)

    threading.Thread(target=script, daemon=True).start()
    assert viewer.main(["--lab", "--lab-tasks", "scroll_slow_2f,home_drag,home_touch,frame_tap", "--lab-repeat", "1"]) == 0
    assert not errors

    def posted(t0, t1):
        return [e for t, e in mirror.posted if t0 <= t <= t1]

    drag = posted(marks["drag0"], marks["drag1"])
    assert drag[0] == ("pointer", 1, W // 2, H - 1), drag[:3]  # starts at the bottom edge
    assert drag[-1][0] == "pointer" and drag[-1][1] == 0
    touch = [e for _, e in mirror.posted if e[0] == "multi"]
    assert touch[0][1] == [(0, W // 2, H - 1, True)]
    assert ("button", viewer.HOME_BUTTON) in [e for _, e in mirror.posted]  # touchscreen Home swipe
    assert posted(marks["tap0"], marks["tap1"]) == []  # taps on the frame send nothing
    scrolls = [e for _, e in mirror.posted if e[0] in ("pointer", "move")]
    assert scrolls[0][0] == "pointer"  # the wheel scroll pressed a finger

    files = list((tmp_path / "lab").glob("lab-*.jsonl"))
    assert len(files) == 1
    info, trials = gesturelab_analyze.split_trials(gesturelab_analyze.load(files[0]))
    assert [t["task"] for t in trials] == ["scroll_slow_2f", "home_drag", "home_touch", "frame_tap"]
    assert [t["rating"] for t in trials] == [3, 2, 1, None]
    rows = [gesturelab_analyze.analyze_trial(t, info) for t in trials]
    assert rows[0]["wheel"]["events"] == 25
    assert rows[1]["decisions"].get("bezel_drag_start") == 1
    assert rows[3]["decisions"].get("bezel_tap_ignored") == 2
    assert gesturelab_analyze.main([str(files[0])]) == 0


@pytest.fixture(autouse=True)
def _quit_pygame():
    yield
    pygame.quit()
