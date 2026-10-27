"""Before and after: the window just before this one, read beside it.

`luna-doctor fix` ends at advice. A user applies a change. Nothing then
tells that user whether the change helped, although both halves of the answer
already sit in the same local logs. Those halves are the window the user is
reading, and the equal-length window immediately before it. This module
builds that pairing and the honest reading of it. It reads no clock, no file
and no socket, because every timestamp arrives as an argument. A test of the
arithmetic is deterministic for that reason.

Three rules shape every string here:

- The floor decides whether the tool can speak. The verdict runs on the
  smaller of the two windows' request counts. Under the floor the verdict is
  `inconclusive`, because two or three samples must never print as an
  improvement.
- Pressure decides the verdict, in a fixed precedence: blocked at admission,
  then silent stalls, then 429s. A tie falls through to the next signal.
  The tool reports peak concurrency and admission conversion beside the
  verdict, and they never decide it alone.
- Nothing implies causation. The two windows sit next to each other in the
  logs, so the counts moving is the whole claim, as of the window edge. No
  string here says `since`, `because` or `fixed`.

The tool never draws a missing denominator as a number. No requests reads
`-- (no requests)`, and a null conversion reads `unknown`. Cells read
previous -> current with an ASCII arrow, because this text travels through
pipes and locales that can lack a pointing glyph.
"""

from __future__ import annotations

SECONDS_PER_HOUR = 3600.0

# A dash, not a zero: 0% is a measurement of a window that had traffic, and
# a window with no denominator is not one.
NO_REQUESTS = "-- (no requests)"
UNKNOWN = "unknown"
ARROW = " -> "

# The pressure signals, strongest first. The order is the verdict's
# precedence: requests that never got in outrank requests that stalled, which
# outrank requests the gateway rejected. A signal that did not move hands the
# verdict to the next one. The last signal is the named limit class
# (`CONCURRENT_REQUEST_LIMIT_EXCEEDED`), which `classify()` assigns before the
# bare 429 class: leaving it out made a window that went from zero named
# refusals to ten read "unchanged".
PRESSURES = (
    ("blocked at admission", "admission_timeout"),
    ("silent stalls", "client_idle_timeout"),
    ("concurrency rejections (429)", "rate_limited"),
    ("refusals at the account limit", "org_concurrency_limit"),
)

FLOOR = 8


def previous_window(now_ts, hours):
    """The equal-length window immediately before the current one.

    The current window is [now_ts - hours * 3600, now_ts]. This function
    returns its predecessor as (start_ts, end_ts). The predecessor ends
    exactly where the current one starts, with no gap and no overlap, no
    matter how many hours the caller asks for. Fractional hours are
    ordinary, and 0.5 gives a 1800-second window.
    """
    span = hours * SECONDS_PER_HOUR
    end = now_ts - span
    return end - span, end


def _requests(metrics):
    """A window's request count. An absent count is no traffic."""
    return int((metrics or {}).get("requests") or 0)


def _outcome(metrics, outcome):
    return int(((metrics or {}).get("by_outcome") or {}).get(outcome, 0))


def _peak(metrics):
    return int((metrics or {}).get("peak_concurrency") or 0)


def _admitted_cell(metrics):
    """One side's admission conversion, or why there is no percentage.

    A percentage needs a denominator. A window with no requests reads
    `-- (no requests)`, and never 0 and never nan. A null percentage in a
    window that did carry traffic is `unknown`. The dict said nothing, so
    the tool says nothing rather than inventing 0%.
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
    is the exception by design. Its value is the verdict word itself,
    `better`, `worse`, `unchanged` or `inconclusive`, because the row is the
    answer and not a measurement. `floor` passes through to change_verdict()
    unchanged.
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
    """The verdict word and one plain sentence that says how it was reached.

    `current` and `previous` are analyse() dicts. The floor applies to the
    smaller of the two request counts. Under the floor the word is
    `inconclusive`, and the reason names the small count, because a window that
    thin cannot carry a claim either way.

    At or above the floor, this function compares rates, not raw counts. The two
    windows rarely hold the same amount of traffic, so a raw count can invert the
    answer: 5 blocked of 40 is a worse rate than 2 blocked of 9, and a count-only
    comparison calls that an improvement. The rate is the count divided by the
    requests in that window. The first pressure signal that moved decides, in the
    order that PRESSURES gives.

    The reason quotes both rates and both counts, so a reader can make sure that
    the word follows the numbers. When no signal moved, the windows read
    `unchanged`. Peak concurrency and admission conversion appear in the rows and
    never in this word.
    """
    before_n = _requests(previous)
    after_n = _requests(current)
    small = min(before_n, after_n)
    if small < floor:
        return ("inconclusive",
                "inconclusive: %d requests in one of the windows, below the "
                "floor of %d" % (small, floor))
    for label, outcome in PRESSURES:
        before = _outcome(previous, outcome)
        after = _outcome(current, outcome)
        before_rate = _rate(before, before_n)
        after_rate = _rate(after, after_n)
        if before_rate == after_rate:
            continue
        word = "better" if after_rate < before_rate else "worse"
        moved = "fell" if after_rate < before_rate else "rose"
        return word, "%s: %s %s from %s (%d of %d requests) to %s (%d of %d requests)" % (
            word, label, moved,
            _percent(before_rate), before, before_n,
            _percent(after_rate), after, after_n)
    # Built from PRESSURES, so a signal added to the tuple cannot leave the
    # sentence naming three of four and reading as if the fourth was not examined.
    names = [label for label, _outcome_name in PRESSURES]
    listed = ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0]
    return ("unchanged",
            "unchanged: %s sit at the same rate in both windows" % listed)


def _rate(count, requests):
    """The share of a window's requests that hit one outcome, 0.0 when empty."""
    if not requests:
        return 0.0
    return float(count) / float(requests)


def _percent(rate):
    """A rate as copy. One decimal place, so a small move stays visible."""
    return "%.1f%%" % (100.0 * rate)