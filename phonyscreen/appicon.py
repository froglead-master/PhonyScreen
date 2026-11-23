"""PhonyScreen's app icon, "Mirror": the iPhone and its mirrored copy on a flat tile. Drawn with Pillow.

One drawing for every use: the window/taskbar icon and the connecting screen (viewer.py), and the
exe's icon file:  python -m phonyscreen.appicon assets/phonyscreen.ico
Sizes up to SMALL px get a simplified drawing (bigger phone, the copy as an outline, no fine
details), so the icon stays crisp in the taskbar and title bar.
"""

from __future__ import annotations

import math
import sys
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter

TILE = (13, 148, 136)  # flat tile colour (teal: the phone's blue-violet screen stands out on it)
BODY = (16, 18, 26)  # phone body
RIM = (128, 136, 158)  # thin metal rim
SCREEN = ((56, 189, 248), (99, 102, 241), (236, 72, 153))  # the phone's lit screen, top-left to bottom-right
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
SMALL = 32


def _superellipse(box: tuple, n: float = 5.0, steps: int = 360) -> list:
    """Continuous-corner "squircle" outline (the macOS/iOS app-tile shape)."""
    x0, y0, x1, y1 = box
    cx, cy, a, b = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
    pts = []
    for i in range(steps):
        t = 2 * math.pi * i / steps
        c, s = math.cos(t), math.sin(t)
        pts.append((cx + a * math.copysign(abs(c) ** (2 / n), c), cy + b * math.copysign(abs(s) ** (2 / n), s)))
    return pts


def _mask(size: int, draw) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    draw(ImageDraw.Draw(m))
    return m


def _fill(base: Image.Image, color, mask: Image.Image) -> None:
    layer = Image.new("RGBA", base.size, tuple(color[:3]) + (0,))
    layer.putalpha(mask if len(color) == 3 else mask.point(lambda v: v * color[3] // 255))
    base.alpha_composite(layer)


def _shadow(base: Image.Image, mask: Image.Image, dy: int, blur: float, alpha: int) -> None:
    sh = Image.new("RGBA", base.size, (0, 0, 0, 0))
    sh.putalpha(mask.point(lambda v: v * alpha // 255).filter(ImageFilter.GaussianBlur(blur)))
    base.alpha_composite(sh, (0, dy))


def _screen_gradient(size: int) -> Image.Image:
    """Diagonal three-stop gradient for the phone's screen."""
    ramp = Image.linear_gradient("L").rotate(45, resample=Image.BICUBIC, expand=True)
    inner = ramp.width / math.sqrt(2) / 2  # the rotated ramp's corners are empty: use its centre square
    c = ramp.width / 2
    ramp = ramp.crop((round(c - inner), round(c - inner), round(c + inner), round(c + inner))).resize((size, size), Image.BICUBIC)
    c0, c1, c2 = SCREEN

    def lut(ch: int) -> list:
        out = []
        for v in range(256):
            f = v / 255
            a, b, k = (c0, c1, f * 2) if f < 0.5 else (c1, c2, f * 2 - 1)
            out.append(round(a[ch] + (b[ch] - a[ch]) * k))
        return out

    return Image.merge("RGB", [ramp.point(lut(ch)) for ch in range(3)]).convert("RGBA")


def _phone(base: Image.Image, box: tuple, radius: float, bezel: float, detail: bool, shadow: bool) -> None:
    n = base.width
    x0, y0, x1, y1 = box
    body = _mask(n, lambda d: d.rounded_rectangle(box, radius=radius, fill=255))
    if shadow:
        _shadow(base, body, int(n * 0.02), n * 0.028, 120)
    _fill(base, BODY, body)
    sbox = (x0 + bezel, y0 + bezel, x1 - bezel, y1 - bezel)
    screen = _mask(n, lambda d: d.rounded_rectangle(sbox, radius=max(1, radius - bezel), fill=255))
    lit = Image.new("RGBA", base.size, (0, 0, 0, 0))
    lit.paste(_screen_gradient(n), (0, 0), screen)
    base.alpha_composite(lit)
    if not detail:
        return
    w, h = sbox[2] - sbox[0], sbox[3] - sbox[1]
    sheen = _mask(n, lambda d: d.polygon([(sbox[0], sbox[1]), (sbox[2], sbox[1]), (sbox[2], sbox[1] + h * 0.28), (sbox[0], sbox[1] + h * 0.52)], fill=255))
    _fill(base, (255, 255, 255, 36), Image.composite(sheen, Image.new("L", base.size, 0), screen))
    iw, ih = (x1 - x0) * 0.30, (x1 - x0) * 0.085
    cx, iy = (x0 + x1) / 2, sbox[1] + w * 0.06
    _fill(base, (10, 10, 14), _mask(n, lambda d: d.rounded_rectangle((cx - iw / 2, iy, cx + iw / 2, iy + ih), radius=ih / 2, fill=255)))
    _fill(base, RIM, _mask(n, lambda d: d.rounded_rectangle(box, radius=radius, outline=255, width=max(2, round(bezel / 6)))))


def _draw(n: int, small: bool) -> Image.Image:
    """The icon on an n x n canvas (drawn large, scaled down by render())."""
    im = Image.new("RGBA", (n, n), TILE + (0,))  # transparent, but tile-coloured: no dark fringe when scaled
    k = n / 1024
    margin = 40 * k if not small else 16 * k
    tile = _mask(n, lambda d: d.polygon(_superellipse((margin, margin, n - margin, n - margin)), fill=255))
    if not small:
        _shadow(im, tile, int(14 * k), 22 * k, 80)
    _fill(im, TILE, tile)
    if small:  # bigger phone, the mirrored copy as a bold outline, no fine detail
        pw, ph, r = 400 * k, 680 * k, 92 * k
        gx, gy = 470 * k, 200 * k
        ghost = (gx, gy, gx + pw, gy + ph)
        _fill(im, (255, 255, 255, 190), _mask(n, lambda d: d.rounded_rectangle(ghost, radius=r, outline=255, width=round(44 * k))))
        x0, y0 = 170 * k, 150 * k
        _phone(im, (x0, y0, x0 + pw, y0 + ph), r, 40 * k, detail=False, shadow=False)
        return im
    pw, ph, r = 330 * k, 600 * k, 70 * k
    gx, gy = 470 * k, 230 * k  # the mirrored copy, behind and to the right
    ghost = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    _phone(ghost, (gx, gy, gx + pw, gy + ph), r, 20 * k, detail=True, shadow=False)
    ghost.putalpha(ghost.getchannel("A").point(lambda v: v * 100 // 255))
    im.alpha_composite(ghost)
    x0, y0 = 220 * k, 200 * k
    _phone(im, (x0, y0, x0 + pw, y0 + ph), r, 20 * k, detail=True, shadow=True)
    return im


@lru_cache(maxsize=16)
def render(size: int) -> Image.Image:
    """The icon at size x size px (RGBA)."""
    small = size <= SMALL
    canvas = 512 if small else (1024 if size > 128 else 512)
    return _draw(canvas, small).resize((size, size), Image.LANCZOS)


def write_ico(path: str) -> None:
    """A Windows .ico with every size Explorer and the taskbar ask for, each drawn for its size."""
    largest = render(max(ICO_SIZES))
    largest.save(path, format="ICO", sizes=[(s, s) for s in ICO_SIZES],
                 append_images=[render(s) for s in ICO_SIZES if s != max(ICO_SIZES)])


if __name__ == "__main__":
    write_ico(sys.argv[1] if len(sys.argv) > 1 else "assets/phonyscreen.ico")
