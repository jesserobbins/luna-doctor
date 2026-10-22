"""Rasterise a Scene to PNG bytes.

`png_from` is the second renderer over `luna_doctor.scene`: it draws the
same Rect, Line, Circle and Text list that `svg_from` serialises, so the
chart and the share card cannot disagree. The output is the simplest PNG
a browser or phone will accept -- 8-bit RGB, no interlacing, filter byte 0
before every scanline, one IDAT -- built with zlib and struct alone.

Two deliberate divergences from the SVG, each noted where it happens:

* `Text.title` and `Circle.title` are SVG tooltips; a raster has no hover,
  so they are ignored.
* SVG places text by its baseline; a bitmap font has no baseline metrics,
  so (x, y) is the top-left corner of the glyph block instead.

Everything is drawn with integer pixels: coordinates are rounded once at
the door, every write is clipped to the canvas, and opacity blends against
whatever is already in the buffer (background, fill, earlier shape).
"""

from __future__ import annotations

import struct
import zlib
from math import isqrt

from luna_doctor.font import ADVANCE, FONT, GLYPH_H, text_width
from luna_doctor.scene import Circle, Line, Rect, Text

# Characters outside the font's ASCII range (the chart legend marks use
# \u2715 and \u25cf) draw as a hollow box rather than as nothing at all.
_UNKNOWN_GLYPH = (
    "#####",
    "#...#",
    "#...#",
    "#...#",
    "#...#",
    "#...#",
    "#####",
)


def _rgb(spec: str) -> bytes:
    """Parse '#rgb' or '#rrggbb' into three bytes."""
    s = spec.lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        raise ValueError(f"unsupported colour: {spec!r}")
    return bytes.fromhex(s)


def _bresenham(x1: int, y1: int, x2: int, y2: int):
    """Yield the pixels of a line, walking the shallower axis."""
    steep = abs(y2 - y1) > abs(x2 - x1)
    if steep:                        # walk x: swap so the long axis is first
        x1, y1 = y1, x1
        x2, y2 = y2, x2
    if x1 > x2:
        x1, x2, y1, y2 = x2, x1, y2, y1
    dx, dy = x2 - x1, abs(y2 - y1)
    err = dx // 2
    y, ystep = y1, 1 if y1 < y2 else -1
    for x in range(x1, x2 + 1):
        yield (y, x) if steep else (x, y)
        err -= dy
        if err < 0:
            y += ystep
            err += dx


class _Canvas:
    """A clipped RGB framebuffer with opacity-aware writes."""

    def __init__(self, w: int, h: int, background: bytes):
        self.w = w
        self.h = h
        self.buf = bytearray(background * (w * h))

    def blend(self, x: int, y: int, rgb: bytes, alpha: float) -> None:
        """Blend one pixel against whatever is already in the buffer."""
        if not (0 <= x < self.w and 0 <= y < self.h):
            return
        i = (y * self.w + x) * 3
        if alpha >= 1.0:
            self.buf[i:i + 3] = rgb
            return
        inv = 1.0 - alpha
        for k in range(3):
            self.buf[i + k] = int(round(self.buf[i + k] * inv + rgb[k] * alpha))

    def fill_rect(self, x: int, y: int, w: int, h: int, rgb: bytes,
                  alpha: float = 1.0) -> None:
        """Fill an integer rectangle, clipped; alpha < 1 blends per pixel."""
        if w <= 0 or h <= 0:
            return
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.w, x + w), min(self.h, y + h)
        if x0 >= x1 or y0 >= y1:
            return
        if alpha >= 1.0:
            span = rgb * (x1 - x0)
            stride = self.w * 3
            for yy in range(y0, y1):
                i = yy * stride + x0 * 3
                self.buf[i:i + len(span)] = span
            return
        inv = 1.0 - alpha
        r, g, b = rgb
        for yy in range(y0, y1):
            i = (yy * self.w + x0) * 3
            for _ in range(x0, x1):
                self.buf[i] = int(round(self.buf[i] * inv + r * alpha))
                self.buf[i + 1] = int(round(self.buf[i + 1] * inv + g * alpha))
                self.buf[i + 2] = int(round(self.buf[i + 2] * inv + b * alpha))
                i += 3

    def stroke_rect(self, x: int, y: int, w: int, h: int, rgb: bytes,
                    alpha: float, thickness: int) -> None:
        """Stroke a band centred on the rect's edge, as SVG does."""
        if thickness <= 0:
            return
        out = thickness // 2                   # pixels outside the edge
        x0, y0 = x - out, y - out
        x1, y1 = x + w + out, y + h + out     # exclusive outer bounds
        self.fill_rect(x0, y0, x1 - x0, thickness, rgb, alpha)        # top
        self.fill_rect(x0, y1 - thickness, x1 - x0, thickness, rgb, alpha)
        self.fill_rect(x0, y0, thickness, y1 - y0, rgb, alpha)        # left
        self.fill_rect(x1 - thickness, y0, thickness, y1 - y0, rgb, alpha)

    def line(self, x1: int, y1: int, x2: int, y2: int, rgb: bytes,
             alpha: float, thickness: int, dash: tuple) -> None:
        """Walk a Bresenham line with a square brush, honouring dash gaps.

        Dash lengths are counted in pixels walked, not geometric distance,
        so a diagonal dash is slightly denser than a horizontal one.
        """
        pattern = [max(0, int(round(d))) for d in dash]
        if len(pattern) % 2 == 1:              # SVG repeats an odd pattern
            pattern = pattern + pattern
        solid = not pattern or not any(pattern)
        seg, remaining = 0, pattern[0] if pattern else 0
        half = thickness // 2                  # brush is centred on the walk
        for px, py in _bresenham(x1, y1, x2, y2):
            if solid or (seg % 2 == 0 and remaining > 0):
                self.fill_rect(px - half, py - half, thickness, thickness,
                               rgb, alpha)
            if not solid:
                remaining -= 1
                if remaining <= 0:
                    seg = (seg + 1) % len(pattern)
                    remaining = pattern[seg]

    def fill_disc(self, cx: int, cy: int, r: int, rgb: bytes,
                  alpha: float) -> None:
        """Fill every pixel whose centre lies within r of (cx, cy)."""
        if r < 0:
            return
        if r == 0:
            self.blend(cx, cy, rgb, alpha)
            return
        for dy in range(-r, r + 1):
            half = isqrt(r * r - dy * dy)
            self.fill_rect(cx - half, cy + dy, 2 * half + 1, 1, rgb, alpha)

    def stroke_circle(self, cx: int, cy: int, r: int, rgb: bytes,
                      alpha: float, thickness: int) -> None:
        """Outline a circle with the midpoint algorithm.

        Thickness is one midpoint ring per pixel of stroke width, centred
        on the radius the way SVG centres a stroke.
        """
        if r < 0:
            return
        if r == 0:
            self.blend(cx, cy, rgb, alpha)
            return
        for rad in range(max(1, r - (thickness - 1) // 2), r + thickness // 2 + 1):
            self._midpoint_ring(cx, cy, rad, rgb, alpha)

    def _midpoint_ring(self, cx: int, cy: int, r: int, rgb: bytes,
                       alpha: float) -> None:
        x, y, d = r, 0, 1 - r
        while x >= y:
            for px, py in ((cx + x, cy + y), (cx + y, cy + x),
                           (cx - y, cy + x), (cx - x, cy + y),
                           (cx - x, cy - y), (cx - y, cy - x),
                           (cx + y, cy - x), (cx + x, cy - y)):
                self.blend(px, py, rgb, alpha)
            y += 1
            if d < 0:
                d += 2 * y + 1
            else:
                x -= 1
                d += 2 * (y - x) + 1

    def text(self, x: int, y: int, s: str, rgb: bytes, size: float,
             anchor: str) -> None:
        """Draw `s` with the bitmap font, top-left at (x, y).

        `size` maps to an integer font scale (size 7 is 1px per cell);
        `weight` has no bitmap equivalent and is ignored.
        """
        scale = max(1, int(size) // GLYPH_H)
        if anchor == "middle":
            x -= text_width(s, scale) // 2
        elif anchor == "end":
            x -= text_width(s, scale)
        pen = x
        for ch in s:
            glyph = FONT.get(ch, _UNKNOWN_GLYPH)
            for gy, row in enumerate(glyph):
                for gx, cell in enumerate(row):
                    if cell == "#":
                        self.fill_rect(pen + gx * scale, y + gy * scale,
                                       scale, scale, rgb)
            pen += ADVANCE * scale


def _draw_rect(canvas: _Canvas, sh: Rect) -> None:
    x, y = int(round(sh.x)), int(round(sh.y))
    w, h = int(round(sh.w)), int(round(sh.h))
    if sh.fill is not None:
        canvas.fill_rect(x, y, w, h, _rgb(sh.fill),
                         1.0 if sh.fill_opacity is None else sh.fill_opacity)
    if sh.stroke is not None:
        canvas.stroke_rect(x, y, w, h, _rgb(sh.stroke),
                           1.0 if sh.stroke_opacity is None else sh.stroke_opacity,
                           max(1, int(round(sh.stroke_width))))


def _draw_line(canvas: _Canvas, sh: Line) -> None:
    canvas.line(int(round(sh.x1)), int(round(sh.y1)),
                int(round(sh.x2)), int(round(sh.y2)),
                _rgb(sh.stroke),
                1.0 if sh.opacity is None else sh.opacity,
                max(1, int(round(sh.stroke_width))), sh.dash)


def _draw_circle(canvas: _Canvas, sh: Circle) -> None:
    # sh.title is an SVG tooltip; a raster has no hover, so it is ignored.
    cx, cy = int(round(sh.cx)), int(round(sh.cy))
    r = int(round(sh.r))
    if sh.fill is not None:
        canvas.fill_disc(cx, cy, r, _rgb(sh.fill),
                         1.0 if sh.fill_opacity is None else sh.fill_opacity)
    if sh.stroke is not None:
        canvas.stroke_circle(cx, cy, r, _rgb(sh.stroke), 1.0,
                             max(1, int(round(sh.stroke_width))))


def _draw_text(canvas: _Canvas, sh: Text) -> None:
    # sh.title is an SVG tooltip; a raster has no hover, so it is ignored.
    canvas.text(int(round(sh.x)), int(round(sh.y)), sh.s, _rgb(sh.fill),
                sh.size, sh.anchor)


def _scanlines(canvas: _Canvas) -> bytes:
    """Prepend a filter byte 0 to each row, as the PNG format requires."""
    out = bytearray()
    stride = canvas.w * 3
    for y in range(canvas.h):
        out.append(0)
        i = y * stride
        out += canvas.buf[i:i + stride]
    return bytes(out)


def _chunk(tag: bytes, body: bytes) -> bytes:
    return (struct.pack(">I", len(body)) + tag + body
            + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))


def _encode(canvas: _Canvas) -> bytes:
    ihdr = struct.pack(">IIBBBBB", canvas.w, canvas.h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(_scanlines(canvas), 6))
            + _chunk(b"IEND", b""))


def png_from(scene) -> bytes:
    """Rasterise a Scene to a complete PNG image, as bytes."""
    w, h = int(scene.width), int(scene.height)
    canvas = _Canvas(w, h, _rgb(scene.background))
    for sh in scene.shapes:
        if isinstance(sh, Rect):
            _draw_rect(canvas, sh)
        elif isinstance(sh, Line):
            _draw_line(canvas, sh)
        elif isinstance(sh, Circle):
            _draw_circle(canvas, sh)
        elif isinstance(sh, Text):
            _draw_text(canvas, sh)
        else:
            raise TypeError(f"unknown shape: {sh!r}")
    return _encode(canvas)
