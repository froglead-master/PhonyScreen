"""Gesture lab (dev mode): `python phonyscreen.py gesturelab`.

Mirrors the phone as usual, plus:
  * records every input event, every touch / button we send to the phone, and the gesture
    code's decisions to a JSONL file (one object per line, "t" = ms since start, µs resolution)
  * reads the touchpad's raw contacts (padraw) so finger lift is known, not guessed
  * estimates how far the phone's content moved between video frames (phase correlation
    on downscaled grayscale frames; numpy needed, otherwise off)
  * walks through a script of tasks in a panel beside the phone: Space starts / ends a
    trial, 1 / 2 / 3 rates it (wrong / okay / felt right), Backspace discards it

Recordings go to %APPDATA%\\PhonyScreen\\gesturelab. They hold input traces and frame-shift
numbers only, never pictures. While the passcode keypad shows, nothing is recorded.
Analyze with `python phonyscreen.py gesturelab analyze [FILE ...]`.
"""

from __future__ import annotations

import json
import logging
import platform
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import pygame

logger = logging.getLogger("phonyscreen.gesturelab")

REC: Optional["Recorder"] = None  # set while the lab runs; every hook checks it first
SESSION: Optional["LabSession"] = None
PANEL_W = 340


# -- recording ---------------------------------------------------------------------------------------
class Recorder:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.f = open(path, "w", encoding="utf-8", buffering=1 << 16)
        self.lock = threading.Lock()
        self.t0 = time.perf_counter_ns()
        self.paused = False
        self.post_seq = 0
        self.pending: dict[int, int] = {}  # id(input event tuple) -> post number

    def ms(self, perf_seconds: float) -> float:
        """A time.perf_counter() value on this file's clock."""
        return round((perf_seconds * 1e9 - self.t0) / 1e6, 3)

    def write(self, kind: str, fields: dict[str, Any], force: bool = False) -> None:
        if self.paused and not force:
            return
        t = round((time.perf_counter_ns() - self.t0) / 1e6, 3)
        line = json.dumps({"t": t, "k": kind, **fields}, separators=(",", ":"), default=_jsonable)
        with self.lock:
            if not self.f.closed:
                self.f.write(line + "\n")

    def flush(self) -> None:
        with self.lock:
            if not self.f.closed:
                self.f.flush()

    def close(self) -> None:
        with self.lock:
            self.f.close()


def _jsonable(o: Any) -> Any:
    if isinstance(o, (tuple, set)):
        return list(o)
    return str(o)


def rec(kind: str, **fields: Any) -> None:
    r = REC
    if r is not None:
        r.write(kind, fields)


POSTED_KINDS = ("move", "pointer", "multi", "button", "swipe_up", "app_switcher")


def on_post(event: tuple) -> None:
    """LeanMirror.post_input (window thread): an input event queued for the phone."""
    r = REC
    if r is None or event[0] not in POSTED_KINDS:
        return
    with r.lock:
        r.post_seq += 1
        n = r.post_seq
        r.pending[id(event)] = n
    r.write("post", {"n": n, "e": list(event)})


def on_dequeue(event: tuple, skipped: bool) -> None:
    """LeanMirror._input_worker (asyncio thread): the event is handled now, or dropped (coalesced)."""
    r = REC
    if r is None or event[0] not in POSTED_KINDS:
        return
    with r.lock:
        n = r.pending.pop(id(event), None)
    r.write("deq", {"n": n, "skip": skipped})


def record_event(ev: Any, fb: Callable[[tuple[int, int]], tuple[int, int]], win_size: tuple[int, int]) -> None:
    """Raw pygame input (window thread). ``fb`` maps a window position to phone-picture pixels."""
    r = REC
    if r is None or r.paused:
        return
    t = ev.type
    if t in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP, pygame.MOUSEMOTION) and getattr(ev, "touch", False):
        return  # SDL's copies of touchscreen input; the FINGER events are recorded
    if t == pygame.MOUSEWHEEL:
        pos = pygame.mouse.get_pos()
        r.write("wheel", {
            "x": ev.x, "y": ev.y, "px": round(float(getattr(ev, "precise_x", ev.x)), 5),
            "py": round(float(getattr(ev, "precise_y", ev.y)), 5), "flip": bool(getattr(ev, "flipped", False)),
            "which": getattr(ev, "which", None), "pos": pos, "fb": fb(pos),
            "ctrl": bool(pygame.key.get_mods() & pygame.KMOD_CTRL),
        })
    elif t == pygame.MOUSEMOTION:
        r.write("mmove", {"pos": ev.pos, "rel": ev.rel, "b": ev.buttons, "fb": fb(ev.pos)})
    elif t in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP):
        r.write("mdown" if t == pygame.MOUSEBUTTONDOWN else "mup", {"b": ev.button, "pos": ev.pos, "fb": fb(ev.pos)})
    elif t in (pygame.FINGERDOWN, pygame.FINGERMOTION, pygame.FINGERUP):
        kind = {pygame.FINGERDOWN: "fdown", pygame.FINGERMOTION: "fmove", pygame.FINGERUP: "fup"}[t]
        wx, wy = int(ev.x * win_size[0]), int(ev.y * win_size[1])
        r.write(kind, {"f": ev.finger_id, "x": round(ev.x, 5), "y": round(ev.y, 5), "pos": (wx, wy), "fb": fb((wx, wy)),
                       "p": round(float(getattr(ev, "pressure", 0.0)), 3)})


# -- what the phone did: content motion between frames ------------------------------------------------
class MotionTracker:
    """Phase correlation between consecutive decoded frames (downscaled grayscale, the middle of the
    screen without status bar and bottom bar). Records the content shift in phone-picture pixels."""

    TARGET_ROWS = 200
    CROP = (0.12, 0.84)

    def __init__(self) -> None:
        __import__("numpy")  # ImportError -> the lab runs without motion

        self.lock = threading.Lock()
        self.evt = threading.Event()
        self.latest: Optional[tuple] = None
        self.stopped = False
        self._window: dict[tuple[int, int], Any] = {}
        self.thread = threading.Thread(target=self._run, daemon=True, name="lab-motion")
        self.thread.start()

    def offer(self, data: bytes, w: int, h: int, vw: int, vh: int, t_in: float, seq: int) -> None:
        with self.lock:
            self.latest = (data, w, h, vw, vh, t_in, seq)
        self.evt.set()

    def stop(self) -> None:
        self.stopped = True
        self.evt.set()

    def gray(self, data: bytes, w: int, h: int, vw: int, vh: int) -> tuple[Any, int]:
        import numpy as np

        k = max(1, vh // self.TARGET_ROWS)
        img = np.frombuffer(data, np.uint8).reshape(h, w, 3)[:vh, :vw, 1]
        top, bottom = int(vh * self.CROP[0]), int(vh * self.CROP[1])
        img = img[top:bottom]
        rows, cols = img.shape[0] // k, img.shape[1] // k
        small = img[: rows * k, : cols * k].reshape(rows, k, cols, k).mean(axis=(1, 3), dtype=np.float32)
        return small, k

    def shift(self, a: Any, b: Any) -> tuple[float, float, float]:
        """Content displacement (dx, dy) from a to b in downscaled pixels, and the peak height (0..1)."""
        import numpy as np

        win = self._window.get(a.shape)
        if win is None:
            win = self._window[a.shape] = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1])).astype(np.float32)
        fa = np.fft.rfft2((a - a.mean()) * win)
        fb = np.fft.rfft2((b - b.mean()) * win)
        cross = fb * np.conj(fa)
        cross /= np.abs(cross) + 1e-9
        r = np.fft.irfft2(cross, s=a.shape)
        iy, ix = np.unravel_index(int(np.argmax(r)), r.shape)
        peak = float(r[iy, ix])

        def sub(c: int, n: int, axis: int) -> float:
            idx = [iy, ix]
            idx[axis] = (c - 1) % n
            lo = r[tuple(idx)]
            idx[axis] = (c + 1) % n
            hi = r[tuple(idx)]
            den = lo - 2 * r[iy, ix] + hi
            off = 0.5 * (lo - hi) / den if den else 0.0
            v = c + off
            return v - n if v > n / 2 else v

        return sub(ix, r.shape[1], 1), sub(iy, r.shape[0], 0), peak

    def _run(self) -> None:
        prev: Optional[tuple] = None  # (gray, k, seq, t_in)
        while not self.stopped:
            self.evt.wait(0.5)
            self.evt.clear()
            with self.lock:
                item, self.latest = self.latest, None
            if item is None or REC is None:
                continue
            data, w, h, vw, vh, t_in, seq = item
            try:
                g, k = self.gray(data, w, h, vw, vh)
            except Exception:
                logger.exception("motion: frame")
                continue
            if prev is not None and prev[0].shape == g.shape:
                dx, dy, peak = self.shift(prev[0], g)
                rec("frame", seq=seq, gap=seq - prev[2], tin=REC.ms(t_in), dx=round(dx * k, 2), dy=round(dy * k, 2),
                    peak=round(peak, 3), fb=(vw, vh))
            else:
                rec("frame", seq=seq, tin=REC.ms(t_in), reset=True, fb=(vw, vh))
            prev = (g, k, seq, t_in)


# -- the task script -----------------------------------------------------------------------------------
DEVICES = {
    "2f": "Touchpad · two-finger scroll",
    "drag": "Touchpad · press and drag",
    "pinch": "Touchpad · pinch",
    "touch": "Touchscreen · one finger",
    "cal": "Calibration",
}


@dataclass(frozen=True)
class Task:
    id: str
    device: str
    title: str
    setup: str
    do: str
    rate: bool = True
    repeat: Optional[int] = None  # None: the session's repeat count


TASKS: tuple[Task, ...] = (
    Task("cal_2f", "cal", "Two-finger slide",
         "Settings, scrolled to the middle of the list.",
         "Two fingers on the touchpad's top edge, slide them slowly and steadily to the bottom edge, lift.",
         rate=False, repeat=2),
    Task("cal_ptr", "cal", "Pointer slide",
         "Pointer at the top of the phone picture. Don't click.",
         "One finger: move the pointer slowly down to the bottom of the phone picture.",
         rate=False, repeat=2),
    Task("scroll_slow_2f", "2f", "Scroll slowly",
         "Settings, at the top.",
         "Scroll down slowly about one screen, watching whether the rows follow your fingers. Then back up slowly."),
    Task("nudge_2f", "2f", "Nudge by one row",
         "Settings, at the top.",
         "Move the list up by exactly one row, then back down by one row."),
    Task("flick_2f", "2f", "Flick and coast",
         "Settings, at the top.",
         "Flick up quickly a few times and let the list coast. Then flick back to the top."),
    Task("page_2f", "2f", "Flip Home Screen pages",
         "First Home Screen page.",
         "Flick sideways to the next page, then back to the previous page."),
    Task("safari_2f", "2f", "Scroll a web page",
         "A long article in Safari, at the top.",
         "Scroll to the bottom of the page, then back to the top, however feels natural."),
    Task("scroll_slow_drag", "drag", "Scroll slowly",
         "Settings, at the top.",
         "Drag the list up slowly about one screen, watching whether the rows follow the pointer. Then back."),
    Task("flick_drag", "drag", "Flick and coast",
         "Settings, at the top.",
         "Flick the list up quickly a few times (press, move fast, let go) and let it coast. Then back to the top."),
    Task("page_drag", "drag", "Flip Home Screen pages",
         "First Home Screen page.",
         "Flick sideways to the next page, then back to the previous page."),
    Task("safari_drag", "drag", "Scroll a web page",
         "A long article in Safari, at the top.",
         "Scroll to the bottom of the page, then back to the top."),
    Task("home_drag", "drag", "Go Home",
         "Any app open.",
         "Press on the bottom frame (or the screen's bottom edge) and drag up."),
    Task("back_drag", "drag", "Go back",
         "Settings > General (any page with a back button).",
         "Press on the left frame (or the screen's left edge) and drag right."),
    Task("edge_near_drag", "drag", "Scroll near the left edge",
         "Settings, at the top.",
         "Drag the list up and down with the pointer about a finger's width inside the left edge. "
         "It should scroll, not go back."),
    Task("nc_drag", "drag", "Notification Center",
         "Home Screen.",
         "Press on the top frame, left of the notch, and drag down. Press Space once it opens (or fails), then close it."),
    Task("cc_drag", "drag", "Control Center",
         "Home Screen.",
         "Press on the frame at the top-right corner and drag down. Press Space once it opens (or fails), then close it."),
    Task("pinch_pad", "pinch", "Zoom a photo",
         "A photo open in Photos.",
         "Pinch out on the touchpad to zoom in, then pinch in to zoom back out."),
    Task("home_touch", "touch", "Go Home",
         "Any app open.",
         "Swipe up from the bottom frame with one finger."),
    Task("scroll_touch", "touch", "Scroll and flick",
         "Settings, at the top.",
         "Scroll down slowly, then flick and let it coast, then back to the top."),
    Task("back_touch", "touch", "Go back",
         "Settings > General.",
         "Swipe right from the left frame with one finger."),
    Task("frame_tap", "drag", "Tap the frame",
         "Any app open.",
         "Click and tap on the frame around the screen in a few places (pointer and finger). Nothing should happen.",
         repeat=1),
)


class LabSession:
    """Task prompts, trial marks and ratings; drawn in a panel to the right of the phone."""

    def __init__(self, tasks: list[Task], repeat: int, path: Path) -> None:
        self.tasks = tasks
        self.repeat = repeat
        self.path = path
        self.idx = 0
        self.trial = 1
        self.state = "ready" if tasks else "done"  # ready | recording | rate | done
        self.trial_t0 = 0.0
        self.ratings: dict[str, list[Optional[int]]] = {}
        self.pad_contacts = 0
        self.pad_seen = False
        self.motion_on = False
        self._cache: tuple[Any, Optional[pygame.Surface]] = (None, None)

    @property
    def task(self) -> Optional[Task]:
        return self.tasks[self.idx] if self.idx < len(self.tasks) else None

    def repeats(self, task: Task) -> int:
        return task.repeat or self.repeat

    def _goto(self, idx: int) -> None:
        self.idx, self.trial = max(0, idx), 1
        self.state = "ready" if self.idx < len(self.tasks) else "done"
        if self.state == "done":
            rec("lab_done")
        if REC is not None:
            REC.flush()

    def _next_trial(self) -> None:
        task = self.task
        assert task is not None
        self.trial += 1
        if self.trial > self.repeats(task):
            self._goto(self.idx + 1)
        else:
            self.state = "ready"
        if REC is not None:
            REC.flush()

    def key(self, ev: Any) -> bool:
        """A key press in lab mode; True if the lab used it (then it isn't sent to the phone)."""
        task = self.task
        k = ev.key
        if k == pygame.K_TAB:
            if ev.mod & pygame.KMOD_SHIFT:
                self._goto(self.idx - 1)
            elif task is not None:
                rec("task_skip", task=task.id, trial=self.trial)
                self._goto(self.idx + 1)
            return True
        if k == pygame.K_SPACE:
            if task is None:
                return True
            if self.state == "ready":
                self.state, self.trial_t0 = "recording", time.perf_counter()
                rec("trial_start", task=task.id, device=task.device, trial=self.trial)
            elif self.state == "recording":
                rec("trial_end", task=task.id, trial=self.trial)
                if task.rate:
                    self.state = "rate"
                else:
                    self.ratings.setdefault(task.id, []).append(None)
                    self._next_trial()
            return True
        if self.state == "rate" and task is not None:
            digit = {pygame.K_1: 1, pygame.K_2: 2, pygame.K_3: 3, pygame.K_KP1: 1, pygame.K_KP2: 2, pygame.K_KP3: 3}.get(k)
            if digit:
                rec("rating", task=task.id, trial=self.trial, rating=digit)
                self.ratings.setdefault(task.id, []).append(digit)
                self._next_trial()
                return True
            if k == pygame.K_BACKSPACE:
                rec("trial_discard", task=task.id, trial=self.trial)
                self.state = "ready"
                return True
        return False

    def on_pad(self, count: int) -> None:
        self.pad_seen = True
        self.pad_contacts = count

    def busy(self) -> bool:
        return self.state == "recording"

    # -- drawing ---------------------------------------------------------------------------------------
    def draw(self, screen: pygame.Surface, x: int, y: int, h: int) -> None:
        from phonyscreen import style

        rec_s = int(time.perf_counter() - self.trial_t0) if self.state == "recording" else -1
        key = (self.idx, self.trial, self.state, rec_s, self.pad_contacts if self.pad_seen else None, self.motion_on, h)
        if self._cache[0] != key:
            self._cache = (key, self._build(h, rec_s, style))
        surf = self._cache[1]
        assert surf is not None
        screen.blit(surf, (x, y))

    def _build(self, h: int, rec_s: int, style: Any) -> pygame.Surface:
        w = PANEL_W - 10
        s = pygame.Surface((w, h), pygame.SRCALPHA)
        s.blit(style.round_rect(w, h, 26, style.PANEL), (0, 0))
        pad, y = 22, 22
        inner = w - 2 * pad

        def line(text: str, size: int, color: tuple, weight: str = "regular", gap: int = 4) -> None:
            nonlocal y
            for ln in style.wrap(text, size, inner, weight):
                img = style.text(ln, size, color, weight)
                s.blit(img, (pad, y))
                y += img.get_height()
            y += gap

        line("GESTURE LAB", 12, style.MUTED, "semibold", 10)
        task = self.task
        if task is None:
            line("All done. Thank you!", 22, style.TEXT, "semibold", 8)
            line("Close the window (Esc). Then run:", 14, style.MUTED)
            line("python phonyscreen.py gesturelab analyze", 14, style.TEXT, gap=16)
        else:
            line(f"Task {self.idx + 1} of {len(self.tasks)}  ·  {DEVICES.get(task.device, task.device)}", 13, style.ACCENT,
                 "semibold", 6)
            line(task.title, 24, style.TEXT, "semibold", 10)
            line("Set up", 12, style.MUTED, "semibold", 0)
            line(task.setup, 15, style.TEXT, gap=10)
            line("Do", 12, style.MUTED, "semibold", 0)
            line(task.do, 15, style.TEXT, gap=16)
            n = self.repeats(task)
            for i in range(n):
                color = style.GREEN if i + 1 < self.trial else style.ACCENT if i + 1 == self.trial else style.CONTROL
                s.blit(style.circle(12, color), (pad + i * 20, y + 4))
            s.blit(style.text(f"Trial {min(self.trial, n)} of {n}", 14, style.MUTED), (pad + n * 20 + 6, y))
            y += 30
            box_h = 96
            if self.state == "recording":
                s.blit(style.round_rect(inner, box_h, 14, (70, 24, 28)), (pad, y))
                s.blit(style.circle(14, (255, 69, 58)), (pad + 16, y + 20))
                s.blit(style.text(f"Recording  {rec_s} s", 18, style.TEXT, "semibold"), (pad + 40, y + 12))
                s.blit(style.text("Press Space when done", 15, style.MUTED), (pad + 16, y + 52))
            elif self.state == "rate":
                s.blit(style.round_rect(inner, box_h, 14, style.CONTROL), (pad, y))
                s.blit(style.text("How did it feel?", 18, style.TEXT, "semibold"), (pad + 16, y + 10))
                s.blit(style.text("1 wrong  ·  2 okay  ·  3 felt right", 15, style.TEXT), (pad + 16, y + 40))
                s.blit(style.text("Backspace: discard and redo", 13, style.MUTED), (pad + 16, y + 66))
            else:
                s.blit(style.round_rect(inner, box_h, 14, style.PILL_ACTIVE), (pad, y))
                s.blit(style.text("Set up, then press Space", 18, style.TEXT, "semibold"), (pad + 16, y + 14))
                s.blit(style.text("Space again when you're done", 15, style.MUTED), (pad + 16, y + 50))
            y += box_h + 16
        foot = [
            "Tab skip task  ·  Shift+Tab back  ·  Esc quit",
            f"Touchpad: {self.pad_contacts} finger(s)" if self.pad_seen else "Touchpad: no raw contacts yet",
            "Content motion: on" if self.motion_on else "Content motion: off (pip install numpy)",
            f"Saving to {self.path.name}",
        ]
        fy = h - 22 - len(foot) * 19
        for text in foot:
            s.blit(style.text(text, 12, style.MUTED), (pad, fy))
            fy += 19
        return s


# -- start / stop ------------------------------------------------------------------------------------------
_pad_reader: Any = None
MOTION: Optional[MotionTracker] = None


def lab_dir() -> Path:
    from phonyscreen.config import CONFIG_PATH

    return CONFIG_PATH.parent / "gesturelab"


def start(task_ids: Optional[list[str]], repeat: int, settings: dict) -> LabSession:
    global REC, SESSION, MOTION, _pad_reader
    tasks = list(TASKS)
    if task_ids:
        known = {t.id: t for t in TASKS}
        missing = [t for t in task_ids if t not in known]
        if missing:
            raise SystemExit(f"unknown gesture lab task(s): {', '.join(missing)}. Known: {', '.join(known)}")
        tasks = [known[t] for t in task_ids]
    path = lab_dir() / time.strftime("lab-%Y%m%d-%H%M%S.jsonl")
    REC = Recorder(path)
    session = LabSession(tasks, repeat, path)
    SESSION = session
    from phonyscreen import __version__

    rec("session", version=__version__, python=sys.version.split()[0], platform=platform.platform(),
        tasks=[t.id for t in tasks], repeat=repeat, settings=settings, system=_system_info())
    try:
        MOTION = MotionTracker()
        session.motion_on = True
    except ImportError:
        logger.warning("gesture lab: numpy missing, content motion is off")
    if sys.platform == "win32":
        try:
            from phonyscreen.padraw import TouchpadReader

            def on_report(dev: int, contacts: list, count: int, size_mm: list) -> None:
                rec("pad", d=dev, n=count, c=contacts, size=size_mm)
                if count != 0:  # 0: a later report of the same frame (hybrid reporting)
                    session.on_pad(sum(1 for c in contacts if c[1]))

            _pad_reader = TouchpadReader(on_report)
            if not _pad_reader.start():
                _pad_reader = None
        except Exception:
            logger.exception("gesture lab: touchpad raw input")
    logger.info("gesture lab: recording to %s", path)
    return session


def stop() -> None:
    global REC, MOTION, _pad_reader
    if MOTION is not None:
        MOTION.stop()
        MOTION = None
    if _pad_reader is not None:
        _pad_reader.stop()
        _pad_reader = None
    if REC is not None:
        rec("session_end")
        REC.close()
        logger.info("gesture lab: saved %s", REC.path)
        REC = None


def window_dpi() -> Optional[int]:
    """The window's DPI (96 = 100 % scaling): screen pixels per inch where the phone is shown."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        return int(ctypes.windll.user32.GetDpiForWindow(pygame.display.get_wm_info()["window"]))
    except Exception:
        return None


def _system_info() -> dict[str, Any]:
    info: dict[str, Any] = {}
    if sys.platform != "win32":
        return info
    try:
        import ctypes
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        v = wt.UINT()
        if user32.SystemParametersInfoW(0x0068, 0, ctypes.byref(v), 0):  # SPI_GETWHEELSCROLLLINES
            info["wheel_lines"] = v.value
        if user32.SystemParametersInfoW(0x006C, 0, ctypes.byref(v), 0):  # SPI_GETWHEELSCROLLCHARS
            info["wheel_chars"] = v.value
        mouse = (ctypes.c_int * 3)()
        if user32.SystemParametersInfoW(0x0003, 0, mouse, 0):  # SPI_GETMOUSE: thresholds + acceleration
            info["pointer_accel"] = list(mouse)
        if user32.SystemParametersInfoW(0x0070, 0, ctypes.byref(v), 0):  # SPI_GETMOUSESPEED (1..20)
            info["pointer_speed"] = v.value
        info["dpi"] = user32.GetDpiForSystem()
    except Exception as e:
        info["error"] = str(e)
    return info
