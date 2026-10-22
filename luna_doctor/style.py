"""Decide how output may be decorated: color, emoji and motion.

`resolve_style` is the single gate every renderer asks before emitting
anything; renderers never re-implement the rules. Plain and share output
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
_EMOJI = {"moon": "\U0001F319"}
_ASCII = {"moon": "(c)"}


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


def resolve_style(requested, stream, env=None):
    """Resolve a `--style` request against the stream and environment.

    `requested` is one of `None`, "auto", "pretty", "plain", "share".
    `env` defaults to `os.environ`; an empty `NO_COLOR` counts as unset.
    """
    env = os.environ if env is None else env
    if requested in ("plain", "share"):
        return Style(False, False, False)

    no_color = bool(env.get("NO_COLOR", ""))
    force = env.get("CLICOLOR_FORCE", "") not in ("", "0")
    color = ((not no_color) and (force or stream.isatty())) or (
        requested == "pretty" and not no_color
    )
    emoji = color
    motion = emoji and stream.isatty()
    return Style(color, emoji, motion)
