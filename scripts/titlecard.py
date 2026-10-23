#!/usr/bin/env python3
"""Compose the GitHub social-preview card from the LunaRoute moon screenshot.

The moon art is a ready-made screenshot (lunaroute.com's own grey ASCII
moon on black, 1120x1072 RGBA). It already looks right, so this script is
purely paste-and-caption: decode the PNG, composite it over opaque black,
crop to the moon's own circle, scale it, and set it on the right of a
1280x640 card next to the wordmark. No ASCII re-rendering, no density
ramp, no tone mapping, no aspect-ratio correction -- the screenshot's
pixels ship as they came. Standard library only.

Layout, 1280x640, background #0B0B0B (the pasted block's dithered black
is rewritten to that exact colour, so the block leaves no visible seam):

* the moon, scaled to 560px tall, right edge 60px from the canvas edge,
  roughly vertically centred;
* the "luna-doctor" wordmark upper left in the package's bitmap font,
  scale 5 (330px wide), off-white;
* the subtitle beneath it, scale 2, wrapped to two lines, dim blue-grey;
* a flat red #D7263D badge over the moon's upper-left shoulder, reading
  as a sticker on the art: a chunky plus (two overlapping rects) or a
  chunky X (two thick diagonals). Both variants are written on every
  run, so the two card images cannot drift apart.

    python3 scripts/titlecard.py --moon /tmp/moon-screenshot.png
"""

from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The raster side of `luna_doctor.png` is the repo's own pixel engine; the
# private `_Canvas`/`_encode` helpers are used directly because a decoded
# screenshot is a pixmap, which the Scene/Shape list has no primitive for.
from luna_doctor.font import GLYPH_H, text_width
from luna_doctor.png import _Canvas, _encode, _rgb

CARD_W, CARD_H = 1280, 640
BACKGROUND = "#0B0B0B"

# Moon: crop with a threshold that splits the screenshot's dithered black
# noise (measured <= 25) from the moon's own ink (>= 40), pad the crop box
# with a small margin, then scale so the moon is 560px tall. Scaling is
# nearest-neighbour with one factor for both axes, so the circle stays a
# circle and the ASCII stays crisp. After scaling, the crop's near-black
# dither is rewritten to the canvas colour: a dithered dark band does not
# match a flat background, and without this the crop's square would read
# as a visible rectangle on the card.
MOON_INK_THRESHOLD = 40
MOON_NOISE_MAX = 25  # brightest dither-noise channel; the moon's edge starts above this
MOON_CROP_MARGIN = 12
MOON_TARGET_H = 560
MOON_RIGHT_GAP = 60  # moon's right edge sits this far from the card edge

WORDMARK = "luna-doctor"
SUBTITLE = "concurrency diagnostics for LunaRoute agents"

WORDMARK_INK = "#F5F1E8"  # off-white
SUBTITLE_INK = "#9AA4B2"  # dim blue-grey
BADGE_RED = "#D7263D"  # medical red

MARGIN = 64
WORDMARK_Y = 56  # scale 5 -> 330x35, ends y=91
SUBTITLE_Y = 120  # scale 2, two wrapped lines at y=120 and y=142

# The badge, 120x120, centred on the moon's upper-left shoulder so it
# overlaps the limb and reads as a sticker. Set after measuring the
# placed moon: circle centre ~(938, 320), radius ~274.
BADGE_CX, BADGE_CY = 744, 126
BADGE_SIZE = 120
BADGE_ARM = 40
BADGE_STROKE = 34  # X variant: thickness of each diagonal

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _paeth(a: int, b: int, c: int) -> int:
    """PNG filter type 4 predictor."""
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode_png(data: bytes) -> tuple:
    """Decode an 8-bit RGB or RGBA, non-interlaced PNG to flat pixels.

    Returns `(width, height, channels, pixels)` with `pixels` as a bytes
    object in row-major order. Anything the card does not need -- other
    bit depths, palette or greyscale, interlacing -- fails loudly rather
    than rendering wrong art.
    """
    if data[:8] != PNG_SIGNATURE:
        raise ValueError("not a PNG: bad signature")
    pos, idat, header = 8, b"", None
    while pos < len(data):
        length, tag = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
        pos += 12 + length  # length + tag + body + crc
    if header is None:
        raise ValueError("not a PNG: missing IHDR")
    w, h, depth, ctype, comp, filt, interlace = header
    if depth != 8:
        raise ValueError(f"unsupported bit depth: {depth} (need 8)")
    if ctype not in (2, 6):  # 2=RGB, 6=RGBA
        raise ValueError(f"unsupported colour type: {ctype}"
                         " (need RGB or RGBA)")
    if comp != 0 or filt != 0:
        raise ValueError("unsupported compression/filter method:"
                         f" {comp}/{filt}")
    if interlace != 0:
        raise ValueError(f"unsupported interlace: {interlace} (need none)")

    nch = 3 if ctype == 2 else 4
    stride = w * nch
    raw = zlib.decompress(idat)
    if len(raw) != h * (stride + 1):
        raise ValueError(f"corrupt PNG: expected {h * (stride + 1)} filtered"
                         f" bytes, got {len(raw)}")
    px = bytearray(h * stride)
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        ftype = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if ftype == 0:
            pass
        elif ftype == 1:
            for i in range(nch, stride):
                line[i] = (line[i] + line[i - nch]) & 255
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif ftype == 3:
            for i in range(stride):
                left = line[i - nch] if i >= nch else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif ftype == 4:
            for i in range(stride):
                left = line[i - nch] if i >= nch else 0
                up = prev[i]
                upleft = prev[i - nch] if i >= nch else 0
                line[i] = (line[i] + _paeth(left, up, upleft)) & 255
        else:
            raise ValueError(f"unsupported scanline filter: {ftype}")
        px[y * stride:(y + 1) * stride] = line
        prev = line
    return w, h, nch, bytes(px)


def composite_over_black(w: int, h: int, nch: int, px: bytes) -> bytes:
    """Flatten RGBA (or pass RGB through) onto opaque black, as RGB bytes."""
    if nch == 3:
        return px
    out = bytearray(w * h * 3)
    for i in range(w * h):
        r, g, b, a = px[i * 4:i * 4 + 4]
        out[i * 3] = (r * a + 127) // 255
        out[i * 3 + 1] = (g * a + 127) // 255
        out[i * 3 + 2] = (b * a + 127) // 255
    return bytes(out)


def crop_bbox(w: int, h: int, rgb: bytes) -> tuple:
    """Bounding box of the bright moon ink, with a small margin.

    `MOON_INK_THRESHOLD` sits above the screenshot's black-dither noise
    and below the moon's dimmest ink, so the box is the moon's own
    circle, not the frame.
    """
    xs, ys = [], []
    for y in range(h):
        row = rgb[y * w * 3:(y + 1) * w * 3]
        for x in range(w):
            i = x * 3
            if max(row[i], row[i + 1], row[i + 2]) > MOON_INK_THRESHOLD:
                xs.append(x)
                ys.append(y)
    if not xs:
        raise ValueError("no moon found: nothing brighter than the"
                         f" {MOON_INK_THRESHOLD} ink threshold")
    x0 = max(0, min(xs) - MOON_CROP_MARGIN)
    y0 = max(0, min(ys) - MOON_CROP_MARGIN)
    x1 = min(w, max(xs) + 1 + MOON_CROP_MARGIN)
    y1 = min(h, max(ys) + 1 + MOON_CROP_MARGIN)
    return x0, y0, x1, y1


def scale_nearest(w: int, h: int, rgb: bytes, nw: int, nh: int) -> bytes:
    """Nearest-neighbour resample of an RGB buffer to `(nw, nh)`."""
    out = bytearray(nw * nh * 3)
    for dy in range(nh):
        sy = min(h - 1, dy * h // nh)
        srow = rgb[sy * w * 3:(sy + 1) * w * 3]
        for dx in range(nw):
            sx = min(w - 1, dx * w // nw)
            o = (dy * nw + dx) * 3
            out[o:o + 3] = srow[sx * 3:sx * 3 + 3]
    return bytes(out)


def _wrap(s: str, scale: int, max_w: int) -> list:
    """Greedy word wrap measured in bitmap-font pixels, so a line fits."""
    lines, cur = [], ""
    for word in s.split():
        trial = f"{cur} {word}" if cur else word
        if text_width(trial, scale) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def blit(canvas: _Canvas, w: int, h: int, rgb: bytes,
         x0: int, y0: int) -> None:
    """Copy an RGB buffer onto the canvas, clipped at every edge."""
    for sy in range(h):
        dy = y0 + sy
        if not 0 <= dy < canvas.h:
            continue
        sx0 = max(0, -x0)
        sx1 = min(w, canvas.w - x0)
        if sx1 <= sx0:
            continue
        src = rgb[(sy * w + sx0) * 3:(sy * w + sx1) * 3]
        i = (dy * canvas.w + x0 + sx0) * 3
        canvas.buf[i:i + len(src)] = src


def flatten_sky(w: int, h: int, rgb: bytes) -> bytes:
    """Rewrite the pasted block's near-black dither to the canvas colour.

    The screenshot's background is dithered near-black, which does not match
    the flat card background, so the pasted crop would show as a rectangle.
    Every pixel whose brightest channel stays below MOON_NOISE_MAX is that
    dither -- measured: the noise never exceeds 24 on any channel, while
    the moon's own ink and its antialiased edge start at 25 -- so it can be
    replaced wholesale with BACKGROUND. The moon keeps its edges, and the
    block disappears into the canvas.
    """
    bg = _rgb(BACKGROUND)
    out = bytearray(w * h * 3)
    for i in range(w * h):
        r, g, b = rgb[i * 3], rgb[i * 3 + 1], rgb[i * 3 + 2]
        out[i * 3:i * 3 + 3] = bg if max(r, g, b) < MOON_NOISE_MAX else rgb[i * 3:i * 3 + 3]
    return bytes(out)


def prepare_moon(path: Path) -> tuple:
    """Decode, flatten, crop and scale the moon screenshot.

    Returns `(width, height, pixels, crop_box)` with the crop box in the
    screenshot's own coordinates, for the run report.
    """
    w, h, nch, px = decode_png(path.read_bytes())
    flat = composite_over_black(w, h, nch, px)
    x0, y0, x1, y1 = crop_bbox(w, h, flat)
    cw, ch = x1 - x0, y1 - y0
    # Row-by-row slice: a crop is per-row segments of a wider buffer, not
    # one contiguous byte range.
    crop = bytearray()
    for row in range(y0, y1):
        crop += flat[row * w * 3 + x0 * 3:row * w * 3 + x1 * 3]
    # One scale factor, from the target height, applied to both axes so
    # the circle is not stretched. Width follows the crop's own aspect.
    nh = MOON_TARGET_H
    nw = max(1, round(cw * nh / ch))
    scaled = scale_nearest(cw, ch, bytes(crop), nw, nh)
    scaled = flatten_sky(nw, nh, scaled)
    return nw, nh, scaled, (x0, y0, x1, y1)


def draw_type(canvas: _Canvas) -> None:
    """Wordmark and subtitle, in the package bitmap font."""
    canvas.text(MARGIN, WORDMARK_Y, WORDMARK, _rgb(WORDMARK_INK),
                5 * GLYPH_H, "start")
    y = SUBTITLE_Y
    for line in _wrap(SUBTITLE, 2, 280):  # "concurrency diagnostics" fits
        canvas.text(MARGIN, y, line, _rgb(SUBTITLE_INK), 2 * GLYPH_H, "start")
        y += 2 * GLYPH_H + 8


def draw_badge(canvas: _Canvas, badge: str) -> None:
    """The red sticker on the moon's upper-left shoulder."""
    red = _rgb(BADGE_RED)
    left = BADGE_CX - BADGE_SIZE // 2
    top = BADGE_CY - BADGE_SIZE // 2
    if badge == "cross":
        # two overlapping rects: the vertical bar, then the horizontal
        inset = (BADGE_SIZE - BADGE_ARM) // 2
        canvas.fill_rect(left + inset, top, BADGE_ARM, BADGE_SIZE, red)
        canvas.fill_rect(left, top + inset, BADGE_SIZE, BADGE_ARM, red)
    else:
        # a chunky X: two thick diagonals across the badge square
        pad = BADGE_STROKE // 2 + 4
        canvas.line(left + pad, top + pad,
                    left + BADGE_SIZE - pad, top + BADGE_SIZE - pad,
                    red, 1.0, BADGE_STROKE, ())
        canvas.line(left + BADGE_SIZE - pad, top + pad,
                    left + pad, top + BADGE_SIZE - pad,
                    red, 1.0, BADGE_STROKE, ())


def build_card(moon: tuple, badge: str) -> bytes:
    """One 1280x640 card: background, moon, type, then the badge on top."""
    nw, nh, pixels, _ = moon
    canvas = _Canvas(CARD_W, CARD_H, _rgb(BACKGROUND))
    x0 = CARD_W - MOON_RIGHT_GAP - nw  # right edge 60px from the card edge
    y0 = (CARD_H - nh) // 2            # roughly vertically centred
    blit(canvas, nw, nh, pixels, x0, y0)
    draw_type(canvas)
    draw_badge(canvas, badge)
    return _encode(canvas)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Compose the title card from the moon screenshot.")
    parser.add_argument("--moon", required=True,
                        help="moon screenshot PNG (8-bit RGB/RGBA,"
                             " non-interlaced)")
    args = parser.parse_args(argv)

    moon = prepare_moon(Path(args.moon))
    nw, nh, _, (cx0, cy0, cx1, cy1) = moon
    x0 = CARD_W - MOON_RIGHT_GAP - nw
    y0 = (CARD_H - nh) // 2

    outs = []
    for badge, name in (("cross", "cross"), ("x", "x")):
        out = Path(f"/tmp/titlecard-final-{name}.png")
        out.write_bytes(build_card(moon, badge))
        outs.append(f"{badge}: {out} ({out.stat().st_size} bytes)")

    print(f"moon crop box: x {cx0}..{cx1}, y {cy0}..{cy1}"
          f" ({cx1 - cx0}x{cy1 - cy0})")
    print(f"moon placed at ({x0}, {y0}), size {nw}x{nh},"
          f" right edge {x0 + nw} (gap {CARD_W - x0 - nw})")
    for line in outs:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
