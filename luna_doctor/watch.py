#!/usr/bin/env python3
"""Watch: the live view of one machine's concurrency pressure.

`analyze --now` reads like a live view and is not one: it is a one-shot
fifteen-minute lookback, so a user in an incident re-runs it by hand and
diffs the screens in their head. This module is the live view. It renders
three surfaces from the same two halves `attribution` places side by side:

  sample_line  one line per state change: what was refused, what peak this
               machine held, and what the account says is in use. Append-only
               when piped, so a watch is also a log.
  frame        the terminal view: a header of the current numbers, then the
               tail of the samples. Fixed height, one line per sample.
  summary      the three lines printed when the watch ends.

Nothing here loops, reads a clock, opens a file or prints: the parent
command owns the loop, the signals and the cursor, and passes every
timestamp in. These are pure functions over the dicts `cli.analyse()` and
the capacity payload already produce, so a frame is deterministic for the
same inputs.

The rules, the same ones `attribution` keeps:

- LunaRoute bills *concurrent sessions*. A watch line counts requests that
  contended and requests that were refused; it never mentions tokens,
  credits or spend.
- Every number names its owner. `(this machine)` is what this machine's log
  store was measured to hold - a lower bound, since a request nobody logged
  is invisible here. `(account)` is what GET /v1/capacity reports for the
  pool every machine on the account draws on. The two halves are never
  added, and neither is ever presented as the other.
- A number the tool does not have renders as unknown, never as 0 and never
  as a nan.

`sample_line` and `summary` are ASCII only: both cross a pipe, and a pipe
that is not UTF-8 turns a middle dot into a UnicodeEncodeError mid-incident.
`frame` is written for a terminal the parent is driving, so it separates
with "·" and with nothing else outside ASCII. No function here emits an ANSI
escape - the parent owns cursor control - and no call to a dict is made
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
    ("account_limit", "refused at the account limit"),
)


# ---- the two halves ---------------------------------------------------------
def _int(value) -> Optional[int]:
    """An int, or None. A bool is not a count and a float is not a count."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _count(by_outcome, name: str) -> int:
    """One outcome's count. A missing outcome is zero requests, not unknown:
    the store was read and carried none of them."""
    if not isinstance(by_outcome, dict):
        return 0
    return _int(by_outcome.get(name)) or 0


class _Local(NamedTuple):
    """This machine's half of one sample, reduced to what a watch line says.

    `peak` and `windows` are None when the dict does not carry them, and are
    rendered unknown - a peak nobody measured is not a peak of 0.
    """

    blocked: int
    rate_limited: int
    account_limit: int
    peak: Optional[int]
    windows: Optional[int]


def _local_facts(local) -> _Local:
    local = local if isinstance(local, dict) else {}
    by = local.get("by_outcome")
    return _Local(blocked=_count(by, "admission_timeout"),
                  rate_limited=_count(by, "rate_limited"),
                  account_limit=_count(by, "org_concurrency_limit"),
                  peak=_int(local.get("peak_concurrency")),
                  windows=_int(local.get("pressure_windows")))


class _Account(NamedTuple):
    """The capacity payload's occupancy, reduced to what a watch line says.

    `saturated` is the API's own word for a full priority pool, believed only
    as a boolean; `in_use >= limit` counts as saturated too, because two
    fields of the account's own payload agreeing is still the account's
    report. A null `now` block leaves every field None: unknown, not zero.
    """

    in_use: Optional[int]
    limit: Optional[int]
    saturated: bool
    sampled_at: object


def _account_facts(capacity) -> _Account:
    capacity = capacity if isinstance(capacity, dict) else {}
    now = capacity.get("now")
    now = now if isinstance(now, dict) else {}
    in_use = _int(now.get("in_use"))
    limit = _int(now.get("limit"))
    saturated = now.get("saturated") is True or (
        in_use is not None and limit is not None and in_use >= limit)
    return _Account(in_use=in_use, limit=limit, saturated=saturated,
                    sampled_at=now.get("sampled_at"))


# ---- timestamps -------------------------------------------------------------
def _ascii(text) -> str:
    """`text` with everything outside ASCII replaced, so a pipe cannot choke.

    Nothing this module builds contains a non-ASCII character, and the one
    foreign input is the caller's timestamp: this is the belt to that
    suspender, not a license to emit one.
    """
    return "".join(ch if ord(ch) < 128 else "?" for ch in str(text))


def _clock(ts_iso) -> str:
    """The HH:MM:SSZ a line starts with, "" when the timestamp is unreadable.

    Tolerant of the shapes an ISO timestamp arrives in: a trailing Z, a
    numeric offset, fractional seconds, or a bare HH:MM. An unreadable one
    drops the prefix rather than printing a made-up time.
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
    """HH:MMZ, for naming when the account sampled its own numbers."""
    clock = _clock(ts_iso)
    return clock[:5] + "Z" if clock else ""


def _iso_seconds(ts_iso) -> Optional[int]:
    """A timestamp as seconds on a fixed ordinal scale, or None.

    Written out because `datetime.fromisoformat` cannot read the "Z" the
    gateway sends on Python 3.9, and a watch duration must not need a newer
    interpreter than the package claims.
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
    """How long the watch ran, or "unknown" when an end cannot be read or the
    clock went backwards."""
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
    """The refusal classes this sample carries, each named and in a fixed order.

    `notes` adds "(was N)" to a class that moved since `previous`, and only
    to a class that moved: a line printed because the account's occupancy
    changed does not need "(was 11)" beside a count that did not. `limit`
    stops after that many classes, for a line that has to stay short.
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
    """The pressure clause: what was refused, and what it was in the last one."""
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
    """The account's occupancy, labelled (account), or unknown and why.

    `compact` is the one-line form: it shares a 100-character budget with two
    other clauses, so it drops "of" and the command. The frame and the
    summary have room for the long form, which names the command that
    fetches the missing half.
    """
    if acct.in_use is not None and acct.limit is not None:
        text = ("%d/%d in use" if compact else "%d of %d in use") % (
            acct.in_use, acct.limit)
        if acct.saturated:
            text += ", saturated"
        return text + " (account)"
    if acct.in_use is not None:
        return "%d in use, limit unknown (account)" % acct.in_use
    if acct.limit is not None:
        return "in use unknown, limit %d (account)" % acct.limit
    if compact:
        return "in use unknown (account)"
    return "in use unknown (account); run %s" % CAPACITY_COMMAND


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
    """`text` cut to `limit`, ending in "..." when anything was cut.

    A watch never wraps: one sample is one line, so a count of lines is a
    count of samples and the parent can move the cursor by counting them.
    """
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:max(0, limit)]
    return text[:limit - 3] + "..."


def _pressure_segment(facts: _Local, width: int) -> str:
    """The header's pressure clause, shortened until it fits a `width` frame.

    A header row carries no owner label of its own, so a clause cut mid-list
    would leave counts with nothing saying whose they are. Whole refusal
    classes are dropped instead, from the last one backward, and the classes
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
    """One sample line fitted to the frame's width.

    A sample line is allowed 100 characters and a frame may be narrower, so a
    history line has to give something up. It gives up whole clauses, the
    middle one first: the pressure number and the account's occupancy are
    what a watcher is looking at, and a line clipped to its first clause
    alone would leave every row of the frame looking the same. Only when the
    outer two clauses still do not fit is the text cut.
    """
    text = _ascii(line).strip()
    if len(text) <= width:
        return text
    parts = text.split(PIPE_SEPARATOR)
    if len(parts) > 2:
        return _clip(PIPE_SEPARATOR.join([parts[0]] + parts[2:]), width)
    return _clip(text, width)


def _wrap(segments: list, width: int) -> list:
    """The segments as as few "·"-joined lines as fit `width`.

    Whole segments move to the next line before one is broken, so a wrapped
    header never splits "(this machine)" from the number it labels.
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
    """One plain line for one sample. No newline, no ANSI, ASCII, <= 100 chars.

    `local` is the dict `cli.analyse()` returns, `capacity` the capacity
    payload (its "now" block carries the account's occupancy), and `previous`
    the `local` of the sample before, when there is one: it is what makes a
    count legible as a change - "(was 0)" says a refusal just started. A
    class that did not move carries no "(was ...)", so a line printed for
    the account's sake is not decorated with an unchanged refusal count.

    The line is composed from four clauses - the pressure, the peak, the
    account's occupancy - and when they do not fit, the "(was ...)" notes go
    first, then the trailing refusal classes. The peak and the account clause
    are the last things dropped: a pressure number with no peak beside it
    cannot say whose pool was full. If even that does not fit, the line is
    clipped with "...", because the cap is a promise and every other clause
    is a detail.
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
    """Whether anything `sample_line` reports moved between two samples.

    A watch prints on change, so this is the loop's only gate. It compares
    exactly what the line shows: the three refusal counts, the peak, and the
    account's in_use, limit and saturated flag. The first sample always
    changed - there is nothing to compare it to - and so does a sample whose
    account half appeared or disappeared, because "the payload is gone" is
    news about the pool.
    """
    if previous is None:
        return True
    return (_local_state(previous) != _local_state(current)
            or _account_state(capacity_previous) != _account_state(capacity_current))


def _local_state(local) -> tuple:
    """What a line reports about this machine, for comparison."""
    facts = _local_facts(local)
    return (facts.blocked, facts.rate_limited, facts.account_limit, facts.peak)


def _account_state(capacity) -> tuple:
    """What a line reports about the account, for comparison."""
    acct = _account_facts(capacity)
    return (acct.in_use, acct.limit, acct.saturated)


def frame(local, capacity, ts_iso, history, width: int = FRAME_WIDTH) -> list:
    """The terminal view: a header of current numbers, then the history tail.

    `history` is the last `sample_line()` strings, oldest first. The caller
    keeps that buffer - this function holds no state between calls - and a
    caller that keeps N lines gets a frame whose height does not move, which
    is the whole point of drawing one instead of printing each line.

    Every line fits `width`, none is empty, and no line carries an ANSI
    escape: the parent moves the cursor, this returns what to put there. "·"
    separates clauses here and on no other surface.
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
    """The three lines printed when the watch ends: how long, how much, how wide.

    `samples` is how many samples were taken - either the count or the list
    of `sample_line()` strings whose length is that count, so the caller can
    hand over its buffer without measuring it first. The duration comes from
    the two timestamps the caller recorded, never from a clock read here.

    ASCII only, like `sample_line`: this block is pasted into a bug report,
    and a report that renders as mojibake is a report nobody can read.
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
