#!/usr/bin/env python3
"""Watch: the live view of one machine's concurrency pressure.

`analyze --now` reads like a live view. It is not one. It is a one-shot look
back over fifteen minutes. A user in an incident runs it again by hand and
compares the screens from memory. This module is the live view. It renders
three surfaces from the same two halves that `attribution` places side by
side:

  sample_line  one line per state change. It states what was refused, the
               peak that this machine held, and what the account reports in
               use. The line is append-only when a pipe receives it, so a
               watch is also a log.
  frame        the terminal view. A header carries the current numbers. The
               tail of the samples follows. The height is fixed and each
               sample takes one line.
  summary      the three lines that print when the watch ends.

Nothing here loops, reads a clock, opens a file or prints. The parent
command owns the loop, the signals and the cursor. The parent passes each
timestamp in. These are pure functions over the dicts that `cli.analyse()`
and the capacity payload produce. One input gives one frame.

The rules are the same ones that `attribution` keeps:

- LunaRoute bills *concurrent sessions*. A watch line counts the requests
  that contended and the requests that were refused. It never mentions
  tokens, credits or spend.
- Every number names its owner. `(this machine)` is what the log store of
  this machine was measured to hold. That number is a lower bound, because a
  request that nobody logged is invisible here. `(account)` is what
  GET /v1/capacity reports for the pool that every machine on the account
  draws on. The tool never adds the two halves. It never presents one as the
  other.
- A number that the tool does not have renders as unknown. It never renders
  as 0 and never as a nan.

`sample_line` and `summary` are ASCII only. Both cross a pipe, and a pipe
that is not UTF-8 turns a middle dot into a UnicodeEncodeError mid-incident.
`frame` is written for a terminal that the parent drives, so it separates
with "·" and with nothing else outside ASCII. No function here emits an ANSI
escape, because the parent owns cursor control. No call to a dict is made
without a fixed order, so two runs of one input render one output.
"""

from __future__ import annotations

from datetime import date
from typing import NamedTuple, Optional

from luna_doctor.attribution import CAPACITY_COMMAND

# sample_line's cap. The line is a glance and, when piped, an append-only log
# entry; past the width of a terminal it stops being either.
LINE_LIMIT = 100
# frame's default width: a terminal split beside a log tail.
FRAME_WIDTH = 72
# The clause separator, per surface. "·" is the one non-ASCII character any
# surface may carry, and only the terminal frame may carry it.
FRAME_SEPARATOR = " · "
PIPE_SEPARATOR = " | "

# The three ways a refusal reaches the log store, named the way the rest of
# the tool names them, in the order a line reports them. `blocked` is this
# machine waiting for room in the gateway's queue; `rate_limited` is a bare
# 429 and `account_limit` the named CONCURRENT_REQUEST_LIMIT_EXCEEDED class -
# the two ways the gateway itself says the account's pool was full.
_REFUSALS = (
    ("blocked", "blocked at admission"),
    ("rate_limited", "rate limited"),
    ("stalls", "silent stall(s)"),
    ("account_limit", "refused at the account limit"),
)


# ---- the two halves ---------------------------------------------------------
def _int(value) -> Optional[int]:
    """An int, or None. A bool is not a count and a float is not a count."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _count(by_outcome, name: str) -> int:
    """One outcome's count. A missing outcome is zero requests. It is not
    unknown, because the store was read and carried none of them."""
    if not isinstance(by_outcome, dict):
        return 0
    return _int(by_outcome.get(name)) or 0


class _Local(NamedTuple):
    """This machine's half of one sample, reduced to what a watch line says.

    `peak` and `windows` are None when the dict does not carry them. A missing
    value renders as unknown. A peak that nobody measured is not a peak of 0.
    """

    blocked: int
    stalls: int
    rate_limited: int
    account_limit: int
    peak: Optional[int]
    windows: Optional[int]


def _local_facts(local) -> _Local:
    local = local if isinstance(local, dict) else {}
    by = local.get("by_outcome")
    return _Local(blocked=_count(by, "admission_timeout"),
                  stalls=_count(by, "client_idle_timeout"),
                  rate_limited=_count(by, "rate_limited"),
                  account_limit=_count(by, "org_concurrency_limit"),
                  peak=_int(local.get("peak_concurrency")),
                  windows=_int(local.get("pressure_windows")))


class _Account(NamedTuple):
    """The capacity payload's occupancy, reduced to what a watch line says.

    `saturated` is the word of the API for a full priority pool. The tool
    reads it only as a boolean. `in_use >= limit` also counts as saturated,
    because two fields of the payload agree and the report is still the
    account's. A null `now` block leaves each field None. That value is
    unknown, not zero.
    """

    in_use: Optional[int]
    limit: Optional[int]
    saturated: bool
    sampled_at: object


def _account_facts(capacity) -> _Account:
    """The account's occupancy, dropped when the sample no longer describes now.

    `now_fresh` is decided once, by the layer that knows the window. A sample
    that is no longer fresh is not current occupancy, so it is reported as
    absent with its timestamp in `sampled_at`, and the clause names the command
    that refreshes it. Presenting yesterday's saturation beside today's sample
    time would be the tool inventing a fact about right now.
    """
    capacity = capacity if isinstance(capacity, dict) else {}
    now = capacity.get("now")
    now = now if isinstance(now, dict) else {}
    fresh = capacity.get("now_fresh") is True
    in_use = _int(now.get("in_use")) if fresh else None
    limit = _int(now.get("limit")) if fresh else None
    saturated = fresh and (now.get("saturated") is True or (
        in_use is not None and limit is not None and in_use >= limit))
    return _Account(in_use=in_use, limit=limit, saturated=saturated,
                    sampled_at=now.get("sampled_at"))


# ---- timestamps -------------------------------------------------------------
def _ascii(text) -> str:
    """`text` with each character outside ASCII replaced by "?", so a pipe
    cannot choke.

    Nothing that this module builds contains a non-ASCII character. The only
    foreign input is the timestamp of the caller. This function is a guard on
    that input. It is not a license to emit a non-ASCII character.
    """
    return "".join(ch if ord(ch) < 128 else "?" for ch in str(text))


def _clock(ts_iso) -> str:
    """The HH:MM:SSZ that starts a line, or "" when the timestamp is unreadable.

    An ISO timestamp arrives in several shapes. This function accepts a
    trailing Z, a numeric offset, fractional seconds and a bare HH:MM. When a
    timestamp is unreadable, the line drops the prefix. It does not print a
    made-up time.
    """
    text = str(ts_iso if ts_iso is not None else "").strip()
    if "T" in text:
        text = text.split("T", 1)[1]
    if text.endswith("Z"):
        text = text[:-1]
    for cut in ("+", "-", " "):
        if cut in text:
            text = text.split(cut, 1)[0]
    text = text.split(".", 1)[0]
    if len(text) == 5:
        text += ":00"
    if len(text) < 8 or text[2] != ":" or text[5] != ":":
        return ""
    return text[:8] + "Z"


def _hour_minute(ts_iso) -> str:
    """HH:MMZ, for naming the time that the account sampled its numbers."""
    clock = _clock(ts_iso)
    return clock[:5] + "Z" if clock else ""


def _iso_seconds(ts_iso) -> Optional[int]:
    """A timestamp as seconds on a fixed ordinal scale, or None.

    This function is written out because `datetime.fromisoformat` cannot read
    the "Z" that the gateway sends on Python 3.9. A watch duration must not
    need an interpreter newer than the package claims.
    """
    text = str(ts_iso if ts_iso is not None else "").strip()
    if "T" not in text:
        return None
    day_part, _, rest = text.partition("T")
    offset = 0
    for sign in ("+", "-"):
        index = rest.find(sign)
        if index == -1:
            continue
        zone, rest = rest[index:], rest[:index]
        hours, _, minutes = zone[1:].partition(":")
        try:
            offset = int(hours) * 3600 + int(minutes or 0) * 60
        except ValueError:
            return None
        if sign == "-":
            offset = -offset
        break
    if rest.endswith("Z"):
        rest = rest[:-1]
    pieces = day_part.split("-")
    clock = rest.split(".", 1)[0].split(":")
    if len(pieces) != 3 or len(clock) not in (2, 3):
        return None
    try:
        ordinal = date(int(pieces[0]), int(pieces[1]), int(pieces[2])).toordinal()
        seconds = (int(clock[0]) * 3600 + int(clock[1]) * 60
                   + (int(clock[2]) if len(clock) == 3 else 0))
    except ValueError:
        return None
    return ordinal * 86400 + seconds - offset


def _duration(started_iso, ended_iso) -> str:
    """How long the watch ran. The result is "unknown" when the code cannot
    read an end or when the clock went backwards."""
    first, last = _iso_seconds(started_iso), _iso_seconds(ended_iso)
    if first is None or last is None or last < first:
        return "unknown"
    minutes, seconds = divmod(int(last - first), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return "%dh%02dm" % (hours, minutes)
    if minutes:
        return "%dm%02ds" % (minutes, seconds)
    return "%ds" % seconds


# ---- clauses ----------------------------------------------------------------
def _refusal_clause(facts: _Local, previous: Optional[_Local], notes: bool,
                    limit: Optional[int] = None) -> str:
    """The refusal classes of this sample, each named and in a fixed order.

    `notes` adds "(was N)" to a class that moved since `previous`. It adds the
    note only to a class that moved. A line that prints because the account's
    occupancy changed does not need "(was 11)" beside a count that did not
    move. `limit` stops after that many classes, for a line that must stay
    short.
    """
    bits = []
    for field, label in _REFUSALS:
        count = getattr(facts, field)
        if not count:
            continue
        was = getattr(previous, field) if previous is not None else None
        note = (" (was %d)" % was
                if notes and was is not None and was != count else "")
        bits.append("%d %s%s" % (count, label, note))
        if limit is not None and len(bits) >= limit:
            break
    return " and ".join(bits)


def _pressure(facts: _Local, previous: Optional[_Local], notes: bool,
              limit: Optional[int] = None) -> str:
    """The pressure clause, which holds the refusal classes of one sample.

    When this sample refused nothing, the clause states what the previous
    sample refused.
    """
    refusals = _refusal_clause(facts, previous, notes, limit)
    if refusals:
        return refusals
    if notes and previous is not None:
        for field, label in _REFUSALS:
            was = getattr(previous, field)
            if was:
                return "none (was %d %s)" % (was, label)
    return "none"


def _peak_clause(facts: _Local) -> str:
    """This machine's peak, labelled. A missing peak is unknown, never 0."""
    value = "unknown" if facts.peak is None else "%d" % facts.peak
    return "peak %s (this machine)" % value


def _account_clause(acct: _Account, compact: bool = False) -> str:
    """The account's occupancy with the (account) label, or unknown and why.

    `compact` selects the one-line form. That form shares a 100-character
    budget with two other clauses, so it drops "of" and the command. The frame
    and the summary have room for the long form. The long form names the
    command that fetches the missing half.
    """
    if acct.in_use is not None and acct.limit is not None:
        text = ("%d/%d in use" if compact else "%d of %d in use") % (
            acct.in_use, acct.limit)
        # The compact form is on a shared 100-character budget. It drops
        # ", saturated" because "12/12" already says the pool is full, and the
        # characters it frees are what keeps "(account)" from being clipped off
        # the end. A count without its owner label is the one thing this line
        # must never show.
        if acct.saturated and not compact:
            text += ", saturated"
        return text + " (account)"
    if acct.in_use is not None:
        return "%d in use, limit unknown (account)" % acct.in_use
    if acct.limit is not None:
        return "in use unknown, limit %d (account)" % acct.limit
    if compact:
        return "in use unknown (account)"
    return "in use unknown (account). Run %s" % CAPACITY_COMMAND


def _account_at(acct: _Account) -> str:
    """The account's occupancy, with the time the account sampled it."""
    at = _hour_minute(acct.sampled_at)
    if acct.in_use is not None and acct.limit is not None and at:
        text = "%d of %d in use at %s" % (acct.in_use, acct.limit, at)
        if acct.saturated:
            text += ", saturated"
        return text + " (account)"
    return _account_clause(acct)


def _clip(text: str, limit: int) -> str:
    """`text` cut to `limit`, with "..." at the end when the text does not fit.

    A watch never wraps, because one sample is one line. A count of lines is
    then a count of samples, and the parent can move the cursor by counting
    them.
    """
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:max(0, limit)]
    return text[:limit - 3] + "..."


def _pressure_segment(facts: _Local, width: int) -> str:
    """The pressure clause of the header, shortened until it fits a `width` frame.

    A header row carries no owner label. A clause that is cut mid-list leaves
    counts with nothing that says whose counts they are. This function drops
    whole refusal classes instead, from the last one backward. The classes
    that survive keep their names.
    """
    segment = "pressure: %s" % _pressure(facts, None, False, 1)
    for limit in (None, 2):
        candidate = "pressure: %s" % _pressure(facts, None, False, limit)
        if len(candidate) <= width:
            segment = candidate
            break
    return segment


def _history_line(line, width: int) -> str:
    """One sample line fitted to the width of the frame.

    A sample line is allowed 100 characters. A frame can be narrower, so a
    history line gives up whole clauses. The middle clause goes first,
    because the pressure number and the account's occupancy are what a
    watcher reads. A line that is clipped to its first clause alone leaves
    every row of the frame looking the same. The text is cut only when the
    two outer clauses still do not fit.
    """
    text = _ascii(line).strip()
    if len(text) <= width:
        return text
    parts = text.split(PIPE_SEPARATOR)
    if len(parts) > 2:
        return _clip(PIPE_SEPARATOR.join([parts[0]] + parts[2:]), width)
    return _clip(text, width)


def _wrap(segments: list, width: int) -> list:
    """The segments as a few "·"-joined lines, each inside `width`.

    Whole segments move to the next line before one is broken. A wrapped
    header therefore keeps "(this machine)" on the line with the number that
    it labels.
    """
    lines, current = [], ""
    for segment in segments:
        joined = segment if not current else current + FRAME_SEPARATOR + segment
        if current and len(joined) > width:
            lines.append(current)
            current = segment
        else:
            current = joined
    if current:
        lines.append(current)
    return lines


# ---- the surfaces -----------------------------------------------------------
def sample_line(local, capacity, ts_iso, previous=None) -> str:
    """One plain line for one sample. No newline, no ANSI, ASCII, 100
    characters at most.

    `local` is the dict that `cli.analyse()` returns. `capacity` is the
    capacity payload, and its "now" block carries the account's occupancy.
    `previous` is the `local` dict of the sample before, when there is one.
    That dict makes a count legible as a change: "(was 0)" says that a refusal
    just started. A class that did not move carries no "(was ...)" note. A
    line that prints for the account therefore does not carry an unchanged
    refusal count.

    The line is composed from the pressure clause, the peak clause and the
    account clause. When they do not fit, the "(was ...)" notes go first. The
    trailing refusal classes go next. The function drops the peak and the
    account clause last. A pressure number with no peak beside it cannot say
    whose pool was full. If even that does not fit, the line is clipped with
    "...", because the cap is a promise and every other clause is a detail.
    """
    facts = _local_facts(local)
    last = None if previous is None else _local_facts(previous)
    acct = _account_facts(capacity)
    clock = _clock(ts_iso)
    peak = _peak_clause(facts)
    account = _account_clause(acct, compact=True)

    line = ""
    for pressure in (_pressure(facts, last, True),
                     _pressure(facts, last, False),
                     _pressure(facts, last, False, limit=1)):
        body = PIPE_SEPARATOR.join(("pressure: %s" % pressure, peak, account))
        line = "%s %s" % (clock, body) if clock else body
        if len(line) <= LINE_LIMIT:
            return _ascii(line)
    return _ascii(_clip(line, LINE_LIMIT))


def state_changed(previous, current, capacity_previous=None,
                  capacity_current=None) -> bool:
    """Whether anything that `sample_line` reports moved between two samples.

    A watch prints on change, so this function is the only gate in the loop.
    It compares exactly what the line shows: the three refusal counts, the
    peak, and the in_use, limit and saturated fields of the account. The first
    sample always changed, because there is nothing to compare it to. A sample
    also changed when the account half appeared or disappeared, because a
    missing payload is news about the pool.
    """
    if previous is None:
        return True
    return (_local_state(previous) != _local_state(current)
            or _account_state(capacity_previous) != _account_state(capacity_current))


def _local_state(local) -> tuple:
    """What a line reports about this machine, for comparison."""
    facts = _local_facts(local)
    return (facts.blocked, facts.stalls, facts.rate_limited, facts.account_limit, facts.peak)


def _account_state(capacity) -> tuple:
    """What a line reports about the account, for comparison."""
    acct = _account_facts(capacity)
    return (acct.in_use, acct.limit, acct.saturated)


def frame(local, capacity, ts_iso, history, width: int = FRAME_WIDTH) -> list:
    """The terminal view: a header of the current numbers, then the history tail.

    `history` is the last `sample_line()` strings, oldest first. The caller
    keeps that buffer, and this function holds no state between calls. A
    caller that keeps N lines gets a frame of fixed height. That fixed height
    is the reason to draw a frame instead of printing each line.

    Each line fits `width`, no line is empty, and no line carries an ANSI
    escape. The parent moves the cursor, and this function returns the text to
    put there. "·" separates clauses here and on no other surface.
    """
    facts = _local_facts(local)
    acct = _account_facts(capacity)
    clock = _clock(ts_iso)
    head = ["luna-doctor watch" + (" " + clock if clock else "")]
    head.extend(_wrap([_pressure_segment(facts, width),
                       _peak_clause(facts),
                       _account_clause(acct)], width))
    tail = [_history_line(line, width) for line in (history or [])
            if line is not None and str(line).strip()]
    lines = [_clip(line, width) for line in head] + tail
    return [line for line in lines if line]


def summary(local, capacity, samples, started_iso, ended_iso) -> list:
    """The three lines that print when the watch ends: how long, how much, how wide.

    `samples` is the number of samples taken. It is either the count or the
    list of `sample_line()` strings whose length is that count. The caller can
    then hand over its buffer without measuring it first. The duration comes
    from the two timestamps that the caller recorded. No clock is read here.

    The block is ASCII only, like `sample_line`, because a user pastes it into
    a bug report. A report that renders as mojibake is a report that nobody
    can read.
    """
    facts = _local_facts(local)
    acct = _account_facts(capacity)
    count = _sample_count(samples)
    windows = ("pressure windows unknown" if facts.windows is None else
               "%d pressure window%s" % (facts.windows,
                                         "" if facts.windows == 1 else "s"))
    watched = "watched %s, %d sample%s" % (_duration(started_iso, ended_iso),
                                          count, "" if count == 1 else "s")
    peak = "peak %s (this machine)" % (
        "unknown" if facts.peak is None else "%d" % facts.peak)
    refusals = _refusal_clause(facts, None, False) or "nothing refused"
    return [
        _ascii(PIPE_SEPARATOR.join((watched, windows + " (this machine)"))),
        _ascii(PIPE_SEPARATOR.join((peak, refusals + " (this machine)"))),
        _ascii(_account_at(acct)),
    ]


def _sample_count(samples) -> int:
    """How many samples were taken: a count, or the sample lines themselves."""
    if isinstance(samples, int) and not isinstance(samples, bool):
        return max(0, samples)
    try:
        return len(samples)
    except TypeError:
        return 0
