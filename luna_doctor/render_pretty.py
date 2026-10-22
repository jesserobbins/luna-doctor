"""The decorated renderer: color, the moon, and a rule under each section.

`render_pretty` consumes the same `Report` as `render_plain`; where plain is
built to paste anywhere, pretty is built to read at a terminal. Every
decoration decision goes through the `Style` the caller resolved once - this
module never re-implements the gate - and the moon glyph degrades to its ASCII
stand-in whenever the stream cannot take it, so a `LANG=C` terminal gets a
report, never a `UnicodeEncodeError`.
"""

from __future__ import annotations

from luna_doctor.style import Style, resolve_style

MOON = "\U0001F319"


def utf8_capable(stream) -> bool:
    """Whether `stream` can encode the moon glyph.

    Decided by encode-test rather than codec name: `ascii` and `cp1252` both
    refuse `🌙`, and an unknown codec name must degrade, not crash. A stream
    that names no encoding is taken as capable - `StringIO`-like captures
    never raise on write.
    """
    enc = getattr(stream, "encoding", None) or "utf-8"
    try:
        MOON.encode(enc)
    except (UnicodeEncodeError, LookupError, TypeError):
        return False
    return True


def style_for(requested, stream) -> Style:
    """Resolve the `--style` request once, then degrade what the stream refuses.

    `resolve_style` owns every color/emoji/motion rule; this adds the one fact
    it cannot see - the stream's codec. Emoji and motion go when the stream
    cannot encode the moon; color stays, because ANSI escapes are ASCII.
    """
    style = resolve_style(requested, stream)
    if style.emoji and not utf8_capable(stream):
        style = Style(style.color, False, False)
    return style


def render_pretty(report, style, width: int = 80) -> str:
    """Render `report` for a terminal, ending in a single newline.

    The moon leads the headline (the one line a report is scanned for),
    section titles sit dim over an ASCII rule, and values right-align in
    one column while the widest row fits `width` - past that the rows fall
    back to `label: value` lines, so a long value never loses its label.
    Decoration is `style`'s to allow: with the gate closed this emits the
    same layout with no ANSI codes, and the moon becomes `(c)`.
    """
    lines = []
    head = style.symbol("moon")
    headline = f"{head} {report.headline}" if head else report.headline
    lines.append(style.paint(headline, "headline"))
    for section in report.sections:
        if not section.rows:
            continue
        lines.append("")
        if section.title:
            title = section.title.upper()
            lines.append(style.paint(title, "muted"))
            lines.append(style.paint("-" * len(title), "muted"))
        lines.extend(_rows(section.rows, style, width))
    if report.adjustments:
        lines.append("")
        lines.extend("- " + a for a in report.adjustments)
    if report.footnote:
        lines.append("")
        lines.append(style.paint(report.footnote, "muted"))
    return "\n".join(lines) + "\n"


def _rows(rows, style, width: int) -> list:
    """Rows as lines: aligned in one column while they fit, `label: value` after.

    Padding happens on the bare text and painting after, so the ANSI codes
    never count toward the alignment.
    """
    label_w = max((len(r.label) for r in rows), default=0)
    value_w = max((len(line) for r in rows for line in r.value.split("\n")),
                  default=0)
    aligned = 2 + label_w + 2 + value_w <= width
    out = []
    for r in rows:
        first, *rest = r.value.split("\n")
        if aligned:
            out.append("  " + r.label.ljust(label_w) + "  "
                       + style.paint(first.rjust(value_w), r.emphasis))
            under = " " * (2 + label_w + 2)
            out.extend(under + style.paint(line, r.emphasis) for line in rest)
        else:
            out.append(r.label + ": " + style.paint(first, r.emphasis))
            out.extend("  " + style.paint(line, r.emphasis) for line in rest)
    return out


def reveal(text: str, style, stream) -> None:
    """Write one progress line to `stream`, with a carriage return only under motion.

    The only cursor movement luna-doctor ever emits is a single `\r`, and only
    when the style gate allows it: the carriage returns to column 0 before the
    text lands, so a half-written line cannot leave the new text mid-row. Each
    line ends at a newline, nothing scrolls and nothing loops. `style` may be
    `None` - the `--json` path builds no Style, and its lines print plain.
    """
    stream.write(("\r" if style is not None and style.motion else "") + text + "\n")
    stream.flush()
