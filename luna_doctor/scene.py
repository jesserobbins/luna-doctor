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
