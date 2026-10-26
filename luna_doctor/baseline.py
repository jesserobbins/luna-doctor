"""Before and after: the window just before this one, read beside it.

`luna-doctor fix` ends at advice. A user applies a change and has no way to
learn whether it helped, though both halves of the answer are already in the
same local logs: the window they are reading, and the equal-length window
immediately before it. This module builds that pairing and the honest
reading of it. It reads no clock, no file and no socket -- every timestamp
arrives as an argument -- so a test of the arithmetic is deterministic.

Three rules shape every string here:

- The floor decides whether the tool may speak. The verdict runs on the
  smaller of the two windows' request counts; under the floor it is
  `inconclusive`, because two or three samples must never print as an
  improvement.
- Pressure decides the verdict, in a fixed precedence: blocked at admission,
  then silent stalls, then 429s; a tie falls through to the next signal.
  Peak concurrency and admission conversion are reported beside the verdict
  and never decide it alone.
- Nothing implies causation. The two windows sit next to each other in the
  logs, so the counts moving is the whole claim, as of the window edge; no
  string here says `since`, `because` or `fixed`.

A missing denominator is never drawn as a number: no requests reads
`— (no requests)` and a null conversion reads `unknown`. Cells read
previous -> current with an ASCII arrow, because this text travels through
pipes and locales that may not carry a pointing glyph.
"""

from __future__ import annotations

SECONDS_PER_HOUR = 3600.0

# An em dash, not a zero: 0% is a measurement of a window that had traffic,
# and a window with no denominator is not one.
NO_REQUESTS = "\u2014 (no requests)"
UNKNOWN = "unknown"
ARROW = " -> "

# The pressure signals, strongest first. The order is the verdict's
# precedence: requests that never got in outrank requests that stalled,
# which outrank requests the gateway rejected with a 429. A signal that did
# not move hands the verdict to the next one.
PRESSURES = (
    ("blocked at admission", "admission_timeout"),
    ("silent stalls", "client_idle_timeout"),
    ("concurrency rejections (429)", "rate_limited"),
)

FLOOR = 8


def previous_window(now_ts, hours):
    """The equal-length window immediately before the current one.

    The current window is [now_ts - hours * 3600, now_ts]; this returns its
    predecessor as (start_ts, end_ts), which ends exactly where the current
    one starts -- no gap, no overlap, however many hours are asked for.
    Fractional hours are ordinary: 0.5 gives a 1800-second window.
    """
    span = hours * SECONDS_PER_HOUR
    end = now_ts - span
    return end - span, end


def _requests(metrics):
    """A window's request count; an absent one counts as no traffic."""
    return int((metrics or {}).get("requests") or 0)


def _outcome(metrics, outcome):
    return int(((metrics or {}).get("by_outcome") or {}).get(outcome, 0))


def _peak(metrics):
    return int((metrics or {}).get("peak_concurrency") or 0)


def _admitted_cell(metrics):
    """One side's admission conversion, or why there is no percentage.

    A percentage needs a denominator: a window with no requests reads
    `— (no requests)`, never 0 and never nan. A null percentage in a window
    that did carry traffic is `unknown` -- the dict said nothing, so the
    tool says nothing rather than inventing 0%.
    """
    conversion = (metrics or {}).get("admission_conversion") or {}
    conv = conversion.get("interactive") or {}
    pct = conv.get("admitted_pct")
    if pct is None:
        return NO_REQUESTS if _requests(metrics) == 0 else UNKNOWN
    return "%.1f%%" % pct


def _pair(before, after):
    """`previous -> current`, the one shape every measured cell takes."""
    return "%d%s%d" % (before, ARROW, after)


def change_rows(current, previous, floor=FLOOR):
    """The comparison as (label, value) rows, in their fixed order.

    Every measured cell reads previous -> current. The last row, "change",
    is the exception by design: its value is the verdict word itself --
    `better`, `worse`, `unchanged` or `inconclusive` -- because the row is
    the answer, not a measurement. `floor` is passed through to
    change_verdict() unchanged.
    """
    rows = [("requests", _pair(_requests(previous), _requests(current)))]
    for label, outcome in PRESSURES:
        rows.append((label, _pair(_outcome(previous, outcome),
                                  _outcome(current, outcome))))
    rows.append(("peak concurrency", _pair(_peak(previous), _peak(current))))
    rows.append(("admission conversion",
                 _admitted_cell(previous) + ARROW + _admitted_cell(current)))
    rows.append(("change", change_verdict(current, previous, floor)[0]))
    return rows


def change_verdict(current, previous, floor=FLOOR):
    """The verdict word and one plain sentence saying how it was reached.

    `current` and `previous` are analyse() dicts. The floor is applied to
    the smaller of the two request counts: under it the word is
    `inconclusive` and the reason names the small count, because a window
    that thin cannot carry a claim either way. At or above it, the first
    pressure signal that moved decides -- see PRESSURES for the order -- and
    the counts that moved it are quoted. When none of the three moved, the
    windows read `unchanged`; peak concurrency and admission conversion
    appear in the rows and never in this word.
    """
    small = min(_requests(previous), _requests(current))
    if small < floor:
        return ("inconclusive",
                "inconclusive: %d requests in one of the windows, below the "
                "floor of %d" % (small, floor))
    for label, outcome in PRESSURES:
        before = _outcome(previous, outcome)
        after = _outcome(current, outcome)
        if after == before:
            continue
        word = "better" if after < before else "worse"
        moved = "fell" if after < before else "rose"
        return word, "%s: %s %s from %d to %d" % (word, label, moved,
                                                   before, after)
    return ("unchanged",
            "unchanged: blocked at admission, silent stalls and concurrency "
            "rejections (429) sit at the same counts in both windows")
