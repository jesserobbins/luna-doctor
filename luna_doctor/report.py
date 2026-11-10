"""The report model and its paste-safe plain renderer.

Analysis builds a `Report`: one headline, sections of `label: value` rows, a
list of adjustments and an optional footnote. `render_plain` turns it into
text that reads at any terminal width and pastes anywhere - no ANSI escapes,
no emoji, no column padding to break when a value is longer than the column.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# The verdict's status names, which are also the moon glyphs `style` draws.
# `status_moon` is the one place a verdict becomes a status, so the pretty
# renderer and the triage screen cannot disagree about which moon a window
# earns. The plain `moon` name is what a surface draws when it claims no
# status at all; it is not one of the phases, so it carries no legend.
MOON_FULL = "moon_full"
MOON_QUARTER = "moon_quarter"
MOON_NEW = "moon_new"
MOON_PHASES = (MOON_FULL, MOON_QUARTER, MOON_NEW)


def status_moon(exit_code: int | None, headline: str | None) -> str:
    """The moon glyph name for one verdict, or "moon" when there is none.

    The scale: a clean window is the full moon, pressure the quarter moon, and
    a window where requests were blocked at admission the new moon - the
    darkest moon for the state that most needs a person. The exit code carries
    the first two (0 clean, 1 pressure) and the headline carries the third:
    `cli.verdict` names blocked admissions in that line, and its healthy
    headline -- "none blocked at admission" -- is why the code is read first.
    Any other code (2: nothing was measured, or the account's claim without
    local evidence) and a report that carries no verdict keep the plain moon:
    no status is claimed, so no legend is printed.
    """
    if exit_code is None:
        return "moon"
    if exit_code == 0:
        return MOON_FULL
    if exit_code == 1:
        if "blocked at admission" in (headline or ""):
            return MOON_NEW
        return MOON_QUARTER
    return "moon"


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
    """Everything a renderer may say, with no presentation in it.

    `exit_code` is the verdict's own code when the report speaks for a verdict
    (0 clean, 1 pressure, 2 nothing established). None means the report is not
    a verdict - the capacity, latency and discover reports - and then no
    status is claimed. The status moon `render_pretty` leads the headline with
    is read from it.
    """

    headline: str
    sections: list
    adjustments: list = field(default_factory=list)
    footnote: Optional[str] = None
    exit_code: Optional[int] = None


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
