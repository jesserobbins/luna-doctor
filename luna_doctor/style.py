"""Decide how output may be decorated: color, emoji and motion.

`resolve_style` is the single gate every renderer asks before emitting
anything; renderers never re-implement the rules. Plain output
are never decorated, `NO_COLOR` beats an explicit `--style pretty`, and
`CLICOLOR_FORCE` colors a pipe that would otherwise stay plain.
"""
from __future__ import annotations

import os

_RESET = "\033[0m"
_ANSI = {
    "normal": "",
    "good": "\033[32m",
    "warn": "\033[33m",
    "bad": "\033[31m",
    "muted": "\033[2m",
    "headline": "\033[1m",
}
# The moons are the status vocabulary: a full moon for a healthy window, a
# quarter moon for pressure, a new moon for requests blocked at admission. The
# plain `moon` is what a surface draws when it names no status (and what every
# report drew before the scale existed), so it stays as it was. Each ASCII
# stand-in is 7-bit and shows how much of the disc is lit, so the scale
# survives a terminal that refuses the glyphs.
_EMOJI = {
    "moon": "\U0001F319",
    "moon_full": "\U0001F315",
    "moon_quarter": "\U0001F317",
    "moon_new": "\U0001F311",
}
_ASCII = {
    "moon": "(c)",
    "moon_full": "(O)",
    "moon_quarter": "(o)",
    "moon_new": "()",
}

# The status scale, in order of increasing trouble, with the words that go
# beside each glyph. One table: the legend a surface prints and the glyphs it
# prints it with cannot drift apart, and every surface says the same sentence.
MOON_STATUS = (
    ("moon_full", "healthy"),
    ("moon_quarter", "pressure"),
    ("moon_new", "blocked at admission"),
)


class Style:
    """A decoration verdict: which flourishes a stream may accept."""

    def __init__(self, color: bool, emoji: bool, motion: bool):
        self.color = color
        self.emoji = emoji
        self.motion = motion

    def paint(self, text: str, emphasis: str = "normal") -> str:
        """Wrap `text` in the ANSI code for `emphasis`, or return it bare."""
        code = _ANSI.get(emphasis, "")
        if not self.color or not code:
            return text
        return code + text + _RESET

    def symbol(self, name: str) -> str:
        """The named glyph, or its ASCII stand-in when emoji are disallowed."""
        if self.emoji:
            return _EMOJI.get(name, "")
        return _ASCII.get(name, "")

    def moon_legend(self) -> str:
        """The one-line legend for the status moons, in this gate's alphabet.

        Every surface that draws a phase moon prints this line once, so a
        reader meets the scale one time and never has to infer it. The
        separator is the house dot when glyphs are allowed and a plain pipe
        when they are not, so the degraded line stays 7-bit text.
        """
        sep = " \u00b7 " if self.emoji else " | "
        return sep.join("%s %s" % (self.symbol(name), word)
                        for name, word in MOON_STATUS)


def resolve_style(requested, stream, env=None):
    """Resolve a `--style` request against the stream and environment.

    `requested` is one of `None`, "auto", "pretty", "plain".
    `env` defaults to `os.environ`; an empty `NO_COLOR` counts as unset.
    """
    env = os.environ if env is None else env
    if requested == "plain":
        return Style(False, False, False)

    no_color = bool(env.get("NO_COLOR", ""))
    force = env.get("CLICOLOR_FORCE", "") not in ("", "0")
    color = ((not no_color) and (force or stream.isatty())) or (
        requested == "pretty" and not no_color
    )
    emoji = color
    motion = emoji and stream.isatty()
    return Style(color, emoji, motion)
