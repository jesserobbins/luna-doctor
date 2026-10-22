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


def render_plain(report: Report) -> str:
    """Render `report` as plain, undecorated text ending in a single newline.

    Output carries no ANSI escapes and no emoji, so it is safe to paste into a
    ticket or a chat. Decoration is not this renderer's job: `render_pretty`
    owns it, so a caller that wants color uses that instead. A value with
    embedded newlines keeps them, with each continuation line indented two
    spaces under its label.
    """
    blocks = [report.headline]
    for section in report.sections:
        lines = []
        if section.title:
            lines.append(section.title.upper())
        for row in section.rows:
            first, *continuation = row.value.split("\n")
            lines.append("{}: {}".format(row.label, first))
            lines.extend("  " + line for line in continuation)
        blocks.append("\n".join(lines))
    if report.adjustments:
        blocks.append("\n".join("- " + a for a in report.adjustments))
    if report.footnote:
        blocks.append(report.footnote)
    return "\n\n".join(block for block in blocks if block) + "\n"
