"""The report model and its paste-safe plain renderer.

Analysis builds a `Report`: one headline, sections of `label: value` rows, a
list of adjustments and an optional footnote. `render_plain` turns it into
text that reads at any terminal width and pastes anywhere - no ANSI escapes,
no emoji, no column padding to break when a value is longer than the column.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Row:
    """One `label: value` pair; `emphasis` names how important it is."""

    label: str
    value: str
    emphasis: str = "normal"


@dataclass
class Section:
    """A titled group of rows."""

    title: str
    rows: list


@dataclass
class Report:
    """Everything a renderer may say, with no presentation in it."""

    headline: str
    sections: list
    adjustments: list = field(default_factory=list)
    footnote: Optional[str] = None


def render_plain(report: Report, style=None) -> str:
    """Render `report` as plain text ending in a newline.

    With no `style`, or one whose `color` is False, the output carries no ANSI
    escapes and no emoji, so it is safe to paste into a ticket or a chat. A
    style that allows color may paint headlines and emphasised rows.
    """
    paint = _painter(style)

    blocks = [paint(report.headline, "headline")]
    for section in report.sections:
        lines = []
        if section.title:
            lines.append(paint(section.title.upper(), "muted"))
        lines.extend(
            "{}: {}".format(paint(row.label, "normal"), paint(row.value, row.emphasis))
            for row in section.rows
        )
        blocks.append("\n".join(lines))
    if report.adjustments:
        blocks.append("\n".join("- " + a for a in report.adjustments))
    if report.footnote:
        blocks.append(paint(report.footnote, "muted"))
    return "\n\n".join(block for block in blocks if block) + "\n"


def _painter(style):
    """Return a (text, emphasis) -> text function honouring the style gate."""
    if style is not None and getattr(style, "color", False):
        return style.paint
    return lambda text, emphasis="normal": text
