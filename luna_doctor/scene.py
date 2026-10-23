"""Scene: a shape list that two renderers draw the same way.

A Scene is an ordered list of primitives -- Rect, Line, Circle, Text -- plus a
canvas size, a background colour and a font stack. `svg_from` here serialises
it to SVG; `luna_doctor.png` rasterises the same shapes to a PNG. Because both
renderers consume one Scene, the two formats cannot disagree.

The primitives stay deliberately dumb (no <path>), so a raster renderer only
needs a rect fill, a line walk and a circle scan, not a path engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

from luna_doctor.font import text_width


@dataclass
class Rect:
    x: float
    y: float
    w: float
    h: float
    fill: str | None = None
    stroke: str | None = None
    stroke_width: float = 1.0
    fill_opacity: float | None = None
    stroke_opacity: float | None = None


@dataclass
class Line:
    x1: float
    y1: float
    x2: float
    y2: float
    stroke: str
    stroke_width: float = 1.0
    dash: tuple = ()
    opacity: float | None = None


@dataclass
class Circle:
    cx: float
    cy: float
    r: float
    fill: str | None = None
    stroke: str | None = None
    stroke_width: float = 1.0
    fill_opacity: float | None = None
    title: str = ""


@dataclass
class Text:
    x: float
    y: float
    s: str
    size: float = 11
    fill: str = "#111827"
    anchor: str = "start"
    weight: str = "normal"
    title: str = ""


@dataclass
class Scene:
    width: int
    height: int
    background: str
    shapes: list
    font: str = ""


def _num(v) -> str:
    """Trim a number for markup: integral values lose the trailing '.0'."""
    text = f"{float(v):.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-", "-0") else text


def _attrs(pairs) -> str:
    return " ".join(f'{name}="{value}"' for name, value in pairs if value is not None)


def _title(shape) -> str:
    if not shape.title:
        return ""
    return f"<title>{escape(shape.title)}</title>"


def svg_from(scene: Scene) -> str:
    """Serialise a Scene to a standalone, well-formed SVG document."""
    out = []
    add = out.append
    font = f' font-family="{escape(scene.font)}"' if scene.font else ""
    add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{_num(scene.width)}" '
        f'height="{_num(scene.height)}" viewBox="0 0 {_num(scene.width)} {_num(scene.height)}"'
        f'{font}>')
    add(f'<rect width="{_num(scene.width)}" height="{_num(scene.height)}" '
        f'fill="{scene.background}"/>')
    for sh in scene.shapes:
        if isinstance(sh, Rect):
            add(f'<rect {_attrs((
                ("x", _num(sh.x)), ("y", _num(sh.y)),
                ("width", _num(sh.w)), ("height", _num(sh.h)),
                ("fill", sh.fill or "none"),
                ("fill-opacity", None if sh.fill_opacity is None else _num(sh.fill_opacity)),
                ("stroke", sh.stroke),
                ("stroke-width", None if sh.stroke_width == 1.0 else _num(sh.stroke_width)),
                ("stroke-opacity", None if sh.stroke_opacity is None else _num(sh.stroke_opacity)),
            ))}/>')
        elif isinstance(sh, Line):
            add(f'<line {_attrs((
                ("x1", _num(sh.x1)), ("y1", _num(sh.y1)),
                ("x2", _num(sh.x2)), ("y2", _num(sh.y2)),
                ("stroke", sh.stroke),
                ("stroke-width", None if sh.stroke_width == 1.0 else _num(sh.stroke_width)),
                ("stroke-dasharray", " ".join(_num(d) for d in sh.dash) or None),
                ("stroke-opacity", None if sh.opacity is None else _num(sh.opacity)),
            ))}/>')
        elif isinstance(sh, Circle):
            add(f'<circle {_attrs((
                ("cx", _num(sh.cx)), ("cy", _num(sh.cy)), ("r", _num(sh.r)),
                ("fill", sh.fill or "none"),
                ("fill-opacity", None if sh.fill_opacity is None else _num(sh.fill_opacity)),
                ("stroke", sh.stroke),
                ("stroke-width", None if sh.stroke_width == 1.0 else _num(sh.stroke_width)),
            ))}>{_title(sh)}</circle>')
        elif isinstance(sh, Text):
            add(f'<text {_attrs((
                ("x", _num(sh.x)), ("y", _num(sh.y)),
                ("font-size", _num(sh.size)),
                ("text-anchor", None if sh.anchor == "start" else sh.anchor),
                ("font-weight", None if sh.weight == "normal" else sh.weight),
                ("fill", sh.fill),
            ))}>{escape(sh.s)}{_title(sh)}</text>')
        else:
            raise TypeError(f"unknown shape: {sh!r}")
    add("</svg>")
    return "\n".join(out)


# ---- the WhatsApp share card -------------------------------------------------
# 1080x1350 (the 4:5 image WhatsApp shows largest in a chat). The card is
# built here as a Scene so the PNG writer and any later renderer draw the
# same shapes; the bitmap font is unreadable below scale 3 at this width,
# so nothing on the card asks for less than size 21.

CARD_PALETTE = {
    "sky": "#0f172a",     # night: the card's background
    "moon": "#fde68a",    # the crescent's ink
    "star": "#e2e8f0",
    "text": "#f1f5f9",
    "muted": "#94a3b8",
    "bad": "#f87171",     # admission deaths
    "warn": "#fbbf24",    # silent stalls
    "good": "#34d399",   # a clean zero
}


def human_window(hours: float) -> str:
    """The window in human words, for the card footer and the share text.

    A mirror of cli.human_window: scene sits below cli in the import graph
    (cli -> render_share -> scene) and cannot import it, so the two must be
    kept in sync by hand.
    """
    if hours * 60 < 60:
        return f"{round(hours * 60)}m"
    if hours < 24:
        return f"{round(hours)}h"
    return f"{hours / 24:g}d"


def _wrap_text(s: str, scale: int, max_w: int) -> list:
    """Greedy word wrap measured in bitmap-font pixels, so a PNG line fits."""
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


def _card_pairs(m: dict, p: dict) -> list:
    """The label/value pairs that earn a place on a phone screen.

    Measured numbers only. Swamp's configured fan-out, when swamp is in
    use, is a setting and gets its own pair labelled "configured" - it is
    never summed into the measured peak beside it.
    """
    by_outcome = m.get("by_outcome") or {}
    dead = by_outcome.get("admission_timeout", 0)
    stalls = by_outcome.get("client_idle_timeout", 0)
    pairs = [
        ("requests", str(m.get("requests", 0)), p["text"]),
        ("peak concurrency", str(m.get("peak_concurrency", 0)), p["text"]),
        ("admission deaths", str(dead), p["bad"] if dead else p["good"]),
        ("silent stalls", str(stalls), p["warn"] if stalls else p["good"]),
    ]
    lat = ((m.get("latency_s") or {}).get("interactive") or {})
    if lat.get("p50") is not None:
        pairs.append(("interactive latency p50", f"{lat['p50']:.1f}s", p["text"]))
    sw = m.get("swamp") or {}
    if sw.get("present") and sw.get("configuredConcurrency"):
        pairs.append(("swamp fan-out (configured)", str(sw["configuredConcurrency"]),
                      p["muted"]))
    return pairs


def build_card_scene(m: dict, headline: str) -> Scene:
    """The 1080x1350 WhatsApp share card as a Scene.

    A crescent moon, the headline, the numbers that earn a phone screen with
    their values right-aligned, and a provenance footer. The moon is two
    overlapping Circle fills - a disc in the moon colour and an offset disc
    in the sky colour - never an emoji glyph, so no font is involved and no
    renderer can disagree about the shape. Text is placed for the PNG
    raster, where (x, y) is the top-left of the glyph block.
    """
    p = CARD_PALETTE
    w, h = 1080, 1350
    margin, right = 90, 990
    shapes: list = []

    # crescent moon: the disc, then the sky-coloured disc that bites into it.
    # The bite is slightly larger than the moon and offset up-right, so it
    # carves through the far rim and leaves a clean crescent sliver along
    # the lower-left edge - a same-size bite would stop short and leave a
    # boat-shaped flare at the bottom of the disc.
    moon_r = 100
    moon_cx, moon_cy = margin + moon_r - 20, 160
    shapes.append(Circle(moon_cx, moon_cy, moon_r, fill=p["moon"]))
    shapes.append(Circle(moon_cx + 28, moon_cy - 21, moon_r + 10, fill=p["sky"]))
    for sx, sy, sr in ((460, 110, 4), (640, 70, 3), (810, 140, 4), (950, 90, 3),
                       (560, 190, 3), (990, 180, 2)):
        shapes.append(Circle(sx, sy, sr, fill=p["star"]))

    # headline, wrapped at the bitmap font's own advance so it cannot clip
    head_size = 42
    y = 300
    for line in _wrap_text(str(headline), head_size // 7, right - margin):
        shapes.append(Text(margin, y, line, size=head_size, fill=p["text"]))
        y += head_size + 16

    pairs = _card_pairs(m, p)
    pairs_top = y + 74
    gap = 116
    if pairs and pairs_top + (len(pairs) - 1) * gap + 42 > h - 80:
        gap = max(56, (h - 80 - 42 - pairs_top) // len(pairs))
    for i, (label, value, colour) in enumerate(pairs):
        py = pairs_top + i * gap
        shapes.append(Text(margin, py + 7, label, size=21, fill=p["muted"]))
        shapes.append(Text(right, py, value, size=35, fill=colour, anchor="end"))

    footer = "luna-doctor - measured on this account"
    window = human_window(m["window_hours"]) if m.get("window_hours") else ""
    if window:
        footer = f"luna-doctor - last {window} - measured on this account"
    shapes.append(Text(margin, h - 60, footer, size=21, fill=p["muted"]))
    return Scene(w, h, p["sky"], shapes)
