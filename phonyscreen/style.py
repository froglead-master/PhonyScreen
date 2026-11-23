"""Look of PhonyScreen's window: palette, Segoe UI fonts, antialiased shapes and icons.

pygame draws shapes without antialiasing, so every shape here is drawn as a white mask at
SS times the size, scaled down smoothly, then tinted (tinting a white mask avoids the dark
fringes you get when scaling coloured shapes on a transparent background). Results are cached:
drawing a frame only blits ready surfaces.
"""

from __future__ import annotations

import math
import os
from functools import lru_cache
from typing import Callable

import pygame

SS = 4  # supersampling factor

# Palette (dark, macOS/iOS-like)
BACKDROP = (22, 22, 24)
PANEL = (30, 30, 33)
PANEL_EDGE = (64, 64, 70)
HAIRLINE = (50, 50, 55)
CONTROL = (58, 58, 64)
CONTROL_OFF = (74, 74, 80)
TEXT = (245, 245, 247)
MUTED = (152, 152, 160)
ACCENT = (10, 132, 255)
GREEN = (48, 209, 88)
WARN = (255, 159, 10)
CLOSE_HOVER = (232, 17, 35)
PILL = (32, 32, 35)
PILL_HOVER = (66, 66, 72)
PILL_ACTIVE = (24, 78, 150)

_FONT_FILES = {"regular": "segoeui.ttf", "semibold": "seguisb.ttf", "bold": "segoeuib.ttf"}


@lru_cache(maxsize=None)
def font(size: int, weight: str = "regular") -> pygame.font.Font:
    """Segoe UI at ``size`` px. Loaded from its file: SysFont("segoeui") picks Segoe UI Light."""
    path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", _FONT_FILES[weight])
    if os.path.exists(path):
        return pygame.font.Font(path, size)
    return pygame.font.SysFont("segoeui,arial", size, bold=weight != "regular")


def text(s: str, size: int, color: tuple, weight: str = "regular") -> pygame.Surface:
    return font(size, weight).render(s, True, color)


def wrap(s: str, size: int, max_w: int, weight: str = "regular") -> list[str]:
    f = font(size, weight)
    lines: list[str] = []
    for paragraph in s.split("\n"):
        line = ""
        for word in paragraph.split():
            trial = f"{line} {word}".strip()
            if f.size(trial)[0] <= max_w or not line:
                line = trial
            else:
                lines.append(line)
                line = word
        if line:
            lines.append(line)
    return lines


# -- antialiased shapes --------------------------------------------------------------------------
def _mask(w: int, h: int, draw: Callable[[pygame.Surface, float], None]) -> pygame.Surface:
    """White AA mask: ``draw(big, scale)`` paints opaque white on a SS-times-larger surface."""
    w, h = max(1, w), max(1, h)
    big = pygame.Surface((w * SS, h * SS), pygame.SRCALPHA)
    big.fill((255, 255, 255, 0))
    draw(big, SS)
    return pygame.transform.smoothscale(big, (w, h))


def _tint(mask: pygame.Surface, color: tuple) -> pygame.Surface:
    out = mask.copy()
    rgba = tuple(color) + ((255,) if len(color) == 3 else ())
    out.fill(rgba, special_flags=pygame.BLEND_RGBA_MULT)
    return out


@lru_cache(maxsize=256)
def round_rect(w: int, h: int, radius: float, color: tuple, width: float = 0) -> pygame.Surface:
    """Filled (or ``width``-px outlined) rounded rectangle; ``color`` may carry alpha."""

    def draw(s: pygame.Surface, k: float) -> None:
        r = int(radius * k)
        pygame.draw.rect(s, (255, 255, 255, 255), s.get_rect(), border_radius=r)
        if width:
            inner = s.get_rect().inflate(-2 * int(width * k), -2 * int(width * k))
            pygame.draw.rect(s, (255, 255, 255, 0), inner, border_radius=max(0, r - int(width * k)))

    return _tint(_mask(w, h, draw), color)


def circle(d: int, color: tuple) -> pygame.Surface:
    return round_rect(d, d, d / 2, color)


def blit_center(dst: pygame.Surface, src: pygame.Surface, center: tuple) -> None:
    dst.blit(src, (round(center[0] - src.get_width() / 2), round(center[1] - src.get_height() / 2)))


# -- icons (drawn on a 24-unit grid) ----------------------------------------------------------------
def _stroke(s: pygame.Surface, pts: list, w: float, u: float, closed: bool = False) -> None:
    """Polyline with round joins and caps; points in grid units, ``u`` = px per unit (supersampled)."""
    p = [(x * u, y * u) for x, y in pts]
    if closed:
        p.append(p[0])
    width = max(1, int(w * u))
    for a, b in zip(p, p[1:]):
        pygame.draw.line(s, (255, 255, 255, 255), a, b, width)
    for q in p:
        pygame.draw.circle(s, (255, 255, 255, 255), q, width / 2)


def _arc_pts(cx: float, cy: float, r: float, a0: float, a1: float, n: int = 40) -> list:
    return [(cx + r * math.cos(a0 + (a1 - a0) * i / n), cy - r * math.sin(a0 + (a1 - a0) * i / n)) for i in range(n + 1)]


def _rrect_pts(x: float, y: float, w: float, h: float, r: float) -> list:
    pts: list = []
    for cx, cy, a in ((x + w - r, y + r, 0), (x + r, y + r, 90), (x + r, y + h - r, 180), (x + w - r, y + h - r, 270)):
        pts += _arc_pts(cx, cy, r, math.radians(a), math.radians(a + 90), 8)
    return pts


_CLEAR = (255, 255, 255, 0)
_WHITE = (255, 255, 255, 255)


def _icon_home(s, u):
    _stroke(s, [(3.5, 11.5), (12, 4), (20.5, 11.5)], 1.9, u)
    _stroke(s, [(6.5, 10), (6.5, 19.5), (17.5, 19.5), (17.5, 10)], 1.9, u)
    _stroke(s, [(10.5, 19.5), (10.5, 14.5), (13.5, 14.5), (13.5, 19.5)], 1.6, u)


def _icon_switcher(s, u):
    _stroke(s, _rrect_pts(9, 3.5, 10.5, 14, 2.2), 1.7, u, closed=True)
    front = [(x * u, y * u) for x, y in _rrect_pts(4.5, 7, 10.5, 14, 2.2)]
    pygame.draw.polygon(s, _CLEAR, front)  # the front card hides the back one
    _stroke(s, _rrect_pts(4.5, 7, 10.5, 14, 2.2), 1.7, u, closed=True)


def _icon_rotate(s, u):
    _stroke(s, _arc_pts(12, 12.5, 7, math.radians(70), math.radians(380)), 1.8, u)
    tip = (12 + 7 * math.cos(math.radians(70)), 12.5 - 7 * math.sin(math.radians(70)))
    pygame.draw.polygon(s, _WHITE, [((tip[0] - 1) * u, (tip[1] - 4) * u), ((tip[0] + 4.2) * u, tip[1] * u), ((tip[0] - 1) * u, (tip[1] + 4) * u)])


def _icon_keypad(s, u):
    for gx in (6.5, 12, 17.5):
        for gy in (6.5, 12, 17.5):
            pygame.draw.circle(s, _WHITE, (gx * u, gy * u), 1.8 * u)


def _icon_settings(s, u):
    cx = cy = 12
    for k in range(8):
        a = k * math.pi / 4
        pts = [((cx + r * math.cos(a + da)) * u, (cy + r * math.sin(a + da)) * u)
               for r, da in ((6, -0.32), (9.6, -0.2), (9.6, 0.2), (6, 0.32))]
        pygame.draw.polygon(s, _WHITE, pts)
    pygame.draw.circle(s, _WHITE, (cx * u, cy * u), 7.4 * u)
    pygame.draw.circle(s, _CLEAR, (cx * u, cy * u), 3.1 * u)


def _icon_minimize(s, u):
    _stroke(s, [(6.5, 12.5), (17.5, 12.5)], 1.8, u)


def _icon_close(s, u):
    _stroke(s, [(7, 7), (17, 17)], 1.8, u)
    _stroke(s, [(17, 7), (7, 17)], 1.8, u)


def _icon_lock(s, u):
    _stroke(s, [(8, 11)] + _arc_pts(12, 8, 4, math.pi, 0, 24) + [(16, 11)], 2.0, u)
    pygame.draw.rect(s, _WHITE, pygame.Rect(5 * u, 10.5 * u, 14 * u, 10.5 * u), border_radius=int(2.6 * u))
    pygame.draw.circle(s, _CLEAR, (12 * u, 15 * u), 1.5 * u)
    pygame.draw.rect(s, _CLEAR, pygame.Rect(11.35 * u, 15 * u, 1.3 * u, 3 * u))


def _icon_phone(s, u):
    _stroke(s, _rrect_pts(6.5, 2.5, 11, 19, 2.8), 1.7, u, closed=True)
    _stroke(s, [(10.5, 5.2), (13.5, 5.2)], 1.6, u)


def _icon_warning(s, u):
    pygame.draw.circle(s, _WHITE, (12 * u, 12 * u), 10 * u)
    pygame.draw.rect(s, _CLEAR, pygame.Rect(10.8 * u, 6 * u, 2.4 * u, 8 * u), border_radius=int(1.2 * u))
    pygame.draw.circle(s, _CLEAR, (12 * u, 17.2 * u), 1.45 * u)


def _icon_minus(s, u):
    _stroke(s, [(6, 12), (18, 12)], 2.0, u)


def _icon_plus(s, u):
    _stroke(s, [(6, 12), (18, 12)], 2.0, u)
    _stroke(s, [(12, 6), (12, 18)], 2.0, u)


def _icon_backspace(s, u):
    _stroke(s, [(8.5, 5.5), (20.5, 5.5), (20.5, 18.5), (8.5, 18.5), (2.5, 12)], 1.7, u, closed=True)
    _stroke(s, [(11.5, 9), (17, 15)], 1.7, u)
    _stroke(s, [(17, 9), (11.5, 15)], 1.7, u)


_ICONS = {
    "home": _icon_home, "switcher": _icon_switcher, "rotate": _icon_rotate, "keypad": _icon_keypad,
    "settings": _icon_settings, "minimize": _icon_minimize, "close": _icon_close, "lock": _icon_lock,
    "phone": _icon_phone, "warning": _icon_warning, "minus": _icon_minus, "plus": _icon_plus,
    "backspace": _icon_backspace,
}


@lru_cache(maxsize=256)
def icon(name: str, size: int, color: tuple) -> pygame.Surface:
    draw = _ICONS[name]
    return _tint(_mask(size, size, lambda s, k: draw(s, size * k / 24)), color)


@lru_cache(maxsize=64)
def spinner(size: int, step: int, color: tuple, steps: int = 12) -> pygame.Surface:
    """Frame ``step`` of a spinning 270-degree arc."""
    a0 = -2 * math.pi * step / steps

    def draw(s: pygame.Surface, k: float) -> None:
        u = size * k / 24
        _stroke(s, _arc_pts(12, 12, 9, a0, a0 + math.radians(270), 48), 2.4, u)

    return _tint(_mask(size, size, draw), color)


@lru_cache(maxsize=8)
def vertical_gradient(w: int, h: int, top: tuple, bottom: tuple) -> pygame.Surface:
    s = pygame.Surface((max(1, w), max(1, h)))
    for y in range(s.get_height()):
        t = y / max(1, s.get_height() - 1)
        s.fill(tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)), pygame.Rect(0, y, s.get_width(), 1))
    return s


@lru_cache(maxsize=64)
def toast(s: str, size: int, color: tuple = TEXT, bg: tuple = (0, 0, 0, 175), weight: str = "regular") -> pygame.Surface:
    """Text on a rounded translucent capsule."""
    img = text(s, size, color, weight)
    pad_x, pad_y = int(size * 0.9), int(size * 0.35)
    w, h = img.get_width() + 2 * pad_x, img.get_height() + 2 * pad_y
    out = round_rect(w, h, h / 2, bg).copy()
    out.blit(img, (pad_x, pad_y))
    return out
