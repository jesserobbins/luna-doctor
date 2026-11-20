"""The triage screen: a dashboard of nineteen rows, in every one of its states.

Bare `luna-doctor` on a terminal opens this instead of argparse help: it fixes
four regions on the screen - the status header, LOAD, one evidence group and
NEXT - and fills them the moment the scan of the last hour lands. `[w]` cycles
the window 1h -> 6h -> 24h, and every number and the picture are re-read for
the window it lands on.

Everything that decides what a reader sees is a pure function: the header and
its moons, the three LOAD rows, the evidence groups, the NEXT region, the two
key lines, and `dashboard_lines`, which assembles them into the eighteen rows
the screen draws. The Textual `Footer` renders the bindings and is the
nineteenth. Each builder is a function of a metrics dict, the previous
window, a Capacity payload and the chosen answer, so the suite can test the
whole screen offline and without a terminal. Only TriageApp needs Textual,
which is the optional `luna-doctor[tui]` extra: this module imports it at the
top, and the caller (cli.main) guards the import so a machine without the
extra falls back to the linear path.

The dashboard is not a report: a data row is a label, a badge, a number, a
delta and one fact, exactly one row carries an imperative, and the detail stays
in `analyze`, `explain`, `capacity` and `chart`, which the screen shells out
to.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from typing import NamedTuple

from rich.cells import cell_len
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Label, ListItem, ListView, Static

from luna_doctor import attribution, badges, baseline, cli, explain, report, sources
from luna_doctor.style import Style

# One hour is the dashboard's window: it is the widest window whose picture is
# one cell per minute, so the shape is a measurement rather than a re-bin.
WINDOW_HOURS = 1
SCAN_HARNESSES = ("pi", "claude-code")

# The windows `[w]` cycles, shortest first. Anything else lands back on 1h.
WINDOW_CYCLE = (1, 6, 24)

# The dashboard is drawn for about 80 columns, so the sparkline is 60 wide.
SPARK_WIDTH = 60

# Eight blocks, lowest first: the ramp cli.duration_sparkline prints.
_SPARK_BLOCKS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"

# The six on-screen answers, in the order the design draws them.
TRIAGE_CHOICES: list[tuple[str, str]] = [
    ("Requests failing or hanging", "failing"),
    ("Things are slow", "slow"),
    ('"Stream ended without finish_reason"', "finish_reason"),
    ("429 / concurrency errors", "concurrency"),
    ("Agent seems stuck mid-task", "stuck"),
    ("Nothing yet, just checking", "checking"),
]

# The answer the dashboard opens on: the group a quiet screen shows. A person
# answers before the scan lands, and the dashboard fills with that answer's
# group either way.
DEFAULT_CHOICE = "checking"

# Which evidence group each answer selects. The answer chooses which three rows
# stand under the picture; it never gates the screen, and it never changes
# anything above them. The values are the region kinds, spelled here as the
# strings they are because `_LEAD` is read at import time and the region names
# are declared further down.
_LEAD = {
    "failing": "stalls",
    "slow": "latency",
    "finish_reason": "stalls",
    "concurrency": "refusals",
    "stuck": "stalls",
    "checking": "latency",
}

# The three regions an answer can select, in the order the scale rises. `load` is
# not one of them: it is on the screen in every state.
EVIDENCE_GROUPS = ("latency", "refusals", "stalls")

# The answer each group is named by when a person steps through the groups
# themselves (`[g]`): the answer that reports that group's own symptom. Spelled
# as strings for the same reason `_LEAD` is - this table is read at import time
# and the region names are declared further down.
_ANSWER_FOR_GROUP = {"latency": "slow", "refusals": "concurrency",
                     "stalls": "stuck"}

_BADGE_UNKNOWN = badges.badge_for(sources.PROV_UNKNOWN)
_BADGE_MEASURED = badges.badge_for(sources.PROV_MEASURED)

# The status moons and their legend, through the one gate that owns glyphs: a
# screen is not a byte stream, and Textual draws UTF-8, so the screen asks for
# the emoji form - the same glyphs `--style pretty` prints.
_GLYPH_STYLE = Style(color=False, emoji=True, motion=False)


def pick_lead(choice_key: str) -> str:
    """The evidence group one on-screen answer selects."""
    return _LEAD[choice_key]


def window_cycle(hours) -> int:
    """The window `[w]` moves to: 1h -> 6h -> 24h -> 1h.

    The cycle is a fixed ring, so a screen that is already on a window outside
    it (a stale state, a bad number) lands on the 1h default instead of on a
    window the dashboard never offers.
    """
    try:
        current = int(hours)
    except (TypeError, ValueError):
        return WINDOW_CYCLE[0]
    if current not in WINDOW_CYCLE:
        return WINDOW_CYCLE[0]
    return WINDOW_CYCLE[(WINDOW_CYCLE.index(current) + 1) % len(WINDOW_CYCLE)]


def next_group(group_key: str) -> str:
    """The evidence group `[g]` moves to: latency -> refusals -> stalls -> back.

    A ring like `window_cycle`'s, and closed the same way: a group the dashboard
    does not offer lands on the first one rather than on a group the screen
    never draws.
    """
    if group_key not in EVIDENCE_GROUPS:
        return EVIDENCE_GROUPS[0]
    return EVIDENCE_GROUPS[(EVIDENCE_GROUPS.index(group_key) + 1)
                           % len(EVIDENCE_GROUPS)]


def _count(value):
    """An int count, or None. A bool is not a count and a float is not a count."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


# ---- the header: the conclusion, its direction, and the width -------------
#
# Row one answers the first two questions in the order a person under pressure
# asks them: how bad is it (a status moon and its word, read from the CLI's own
# verdict) and is it getting worse (a trend word and its arrow, read from the
# previous equal-length window). Everything after that is the window, the volume,
# the plan and the lane count the verdict was judged against, with the age of the
# data right-aligned at the same edge the rules stop at.
#
# Two alphabets, two jobs. Provenance badges (● ○ ?) ride the numbers. Status
# moons (🌕 🌗 🌑 🌙) ride the conclusion and each region's title, never a row and
# never an arrow: an arrow says direction, a moon says severity.

ROW_CELLS = 78         # the dashboard's hard budget, in terminal cells (§3.2)
HEADER_CELLS = 77      # the header's own width: the rules stop one cell short

# The plain moon is what a surface draws when it claims no status at all. It is
# report's own name for that state, which is why it is not in MOON_PHASES.
_PLAIN_MOON = "moon"

# Four glyph names, four header words, one-to-one (§4.2). The severity scale of
# §7 rule 4 is the first three, one word per phase; the fourth is the word for a
# surface that claims no status at all. No synonym is a status word (`fine`,
# `busy`, `degraded` are banned) and no status word is lowercase.
_STATUS_WORDS = {
    report.MOON_FULL: "OK",
    report.MOON_QUARTER: "PRESSURE",
    report.MOON_NEW: "OVER CAPACITY",
    _PLAIN_MOON: "NOT ESTABLISHED",
}
SEVERITY_WORDS = ("OK", "PRESSURE", "OVER CAPACITY", "NOT ESTABLISHED")

# baseline.change_verdict's four words, and the arrow each one puts beside it.
# `inconclusive` points nowhere - the comparison failed, it did not go anywhere -
# so it draws no arrow and the header drops the cell (§2.5: the empty window's
# header carries no trend). `·` is the delta column's placeholder for a row with
# no comparable window, and it is not a trend word.
TREND_WORDS = ("better", "worse", "unchanged", "inconclusive")
TREND_ARROWS = {"better": "▾", "worse": "▲", "unchanged": "▬", "inconclusive": ""}


def moon_glyph(name: str) -> str:
    """The glyph for one status moon name, in the screen's own alphabet."""
    return _GLYPH_STYLE.symbol(name)


def status_word(moon_name: str) -> str:
    """The header word for one `report.status_moon` name (§4.2).

    One-to-one: four glyph names become four words, so the screen introduces no
    vocabulary the CLI does not already print. A name outside the table claims no
    status, which reads `NOT ESTABLISHED`.
    """
    return _STATUS_WORDS.get(moon_name, _STATUS_WORDS[_PLAIN_MOON])


def header_moon(metrics: dict) -> str:
    """The status moon name this window's verdict earns, or the plain moon.

    `report.status_moon(*cli.verdict(metrics))` is the one conversion from a
    window's verdict to a status, so the header and the CLI cannot disagree about
    this window. The regions say where the conclusion came from; they never
    derive it.
    """
    headline, _adjustments, exit_code = cli.verdict(metrics)
    return report.status_moon(exit_code, headline)


def trend_word(metrics: dict, previous) -> str:
    """`baseline.change_verdict`'s word for this window against the previous one.

    `previous` is the equal-length window the scan reads beside these metrics.
    Without one there is no comparable window, and the answer is `inconclusive` -
    never `unchanged`, which would claim two windows were measured when one was.
    """
    if not isinstance(previous, dict):
        return "inconclusive"
    return baseline.change_verdict(metrics, previous)[0]


def trend_cell(metrics: dict, previous) -> str:
    """`worse ▲`, or "" when the comparison named no direction."""
    word = trend_word(metrics, previous)
    arrow = TREND_ARROWS.get(word, "")
    return "%s %s" % (word, arrow) if arrow else ""


def header_line(metrics: dict, previous, cap, window_hours,
                wall_now=None) -> str:
    """The dashboard's first row: state, direction, window, volume, width, age.

    `🌑 OVER CAPACITY · worse ▲ · 1h · 42 req · apex · ●12 lanes     age 2m`. Six
    cells and a right-aligned age, one cell per fact a person in an incident
    needs before the rows: how bad (the moon's word), which way (the trend word),
    what window this is, how much traffic is in it, whose plan the numbers are,
    how wide the lane count is, and how old what they are reading is.

    The width comes from `resolved_lanes`, the same call the LOAD region and the
    picture's lane scale make, so `●12 lanes` in the header and the wall in the
    caption cannot drift. The age is `age_cell` over the scan's own reading and
    `wall_now`, so the row a person reads is a pure function of its arguments and
    the one-second tick that keeps the age honest re-derives nothing here but
    that cell. A metrics dict with no clock loses the cell rather than taking a
    second reading of the wall clock, and a caller that passes no `wall_now` gets
    the age of a scan that has just closed - the honest answer for a call that
    names no other moment. A row is never truncated to fit the budget (§3.2) - a
    shortened status word would be a wrong answer - so the line runs long
    instead; the tests hold the real windows under 78 cells.

    `previous` is the previous equal-length window's analyse() dict, which the
    scan reads beside these metrics. Without it the header carries no trend cell
    rather than a direction nobody measured.
    """
    name = header_moon(metrics)
    cells = ["%s %s" % (moon_glyph(name), status_word(name))]
    trend = trend_cell(metrics, previous)
    if trend:
        cells.append(trend)
    cells.append(_window_cell(window_hours))
    cells.append(_volume_cell(metrics))
    cells.append(_plan_cell(metrics, cap))
    cells.append(_lanes_cell(metrics, cap))
    line = " \u00b7 ".join(cells)
    scanned_at = metrics.get("scanned_at")
    stamp = age_cell(scanned_at, scanned_at if wall_now is None else wall_now)
    if not stamp:
        return line
    gap = max(1, HEADER_CELLS - cell_len(line) - cell_len(stamp))
    return line + " " * gap + stamp


def _window_cell(window_hours) -> str:
    """The window as the label prints it: `1h`, `6h`, `1.5h`, `?` when unnamed."""
    if isinstance(window_hours, bool) or not isinstance(window_hours, (int, float)):
        return "?"
    return "%dh" % window_hours if float(window_hours).is_integer() else "%gh" % window_hours


def _volume_cell(metrics: dict) -> str:
    """The window's request count: `42 req`, or `? req` when nobody counted."""
    n = _count(metrics.get("requests"))
    return "%d req" % n if n is not None else "? req"


def _plan_cell(metrics: dict, cap) -> str:
    """The plan the width belongs to, or `? plan` when nobody named one.

    The account's plan is the first authority. A `--plan` declaration names one
    when the cache holds no payload, and attribution calls that `plan <name>`,
    which is the only shape in which a declaration is a plan name rather than a
    number. Rule 8: unknown is `?` plus a word, never a bare `unknown` and never
    a zero.
    """
    plan_name = cli._cap_field(cli._cap_field(cap, "config"), "plan")
    if isinstance(plan_name, str) and plan_name.strip():
        return plan_name.strip()
    _value, source = resolved_lanes(metrics, cap)
    if isinstance(source, str) and source.startswith("plan "):
        return source[len("plan "):]
    return "? plan"


def _lanes_cell(metrics: dict, cap) -> str:
    """The lane count with the badge that says whose number it is: `●12 lanes`.

    ● when the account reports it, ◌ when a person declared it, and `? lanes` when
    nobody did: one badge per number (§4.1), and a number that does not exist is
    not printed. It asks `resolved_lanes`, the same call the picture's lane scale
    makes, so the header's `●12 lanes` and the wall in the caption cannot drift.
    """
    value, source = resolved_lanes(metrics, cap)
    badge = badges.badge_for(source)
    return "%s lanes" % badge if value is None else "%s%d lanes" % (badge, value)


# How old the dashboard's data is, live. Row one carries the age of the scan it
# drew, so a reader never subtracts a clock to learn whether what they are
# looking at is current. `AGE_STALE_AFTER_S` is the capacity freshness window
# `cli.CAPACITY_FRESH_S` - the same window that decides whether the `now` row
# keeps its ● - so the age gains its stale marker at the moment the occupancy
# stops being authoritative, and the two markers on one screen cannot disagree.
AGE_STALE_AFTER_S = cli.CAPACITY_FRESH_S


def age_words(seconds: float) -> str:
    """A duration in the screen's own units: `47s` under a minute, then `2m`.

    Units live in the label, not the number (§7 rule 7): the caller writes
    `age 2m`, and this returns the `2m`.
    """
    whole = int(max(0.0, seconds))
    return "%ds" % whole if whole < 60 else "%dm" % (whole // 60)


def age_cell(scan_now, wall_now, stale_after_s: float = AGE_STALE_AFTER_S) -> str:
    """`age 2m`, `age 9m stale` past the freshness window, or "" with no clock.

    The age is the wall clock minus the scan's own clock: two numbers the screen
    already holds, so no cell reads a clock of its own and the tick that keeps
    this cell current re-scans nothing. A scan that carried no clock has no age
    to print, and the row loses the cell rather than inventing a zero.

    Past `stale_after_s` the cell keeps its number and gains the marker: a stale
    reading is still the last reading, and rule 8's shape is `?` plus a reason,
    which is how the `now` row already says `stale 47m`. A wall clock behind the
    scan's clamps to zero, because a clock skew is not a negative age.
    """
    scan = _epoch(scan_now)
    wall = _epoch(wall_now)
    if scan is None or wall is None:
        return ""
    age = max(0.0, wall - scan)
    cell = "age " + age_words(age)
    if stale_after_s is not None and age > stale_after_s:
        cell += " stale"
    return cell


# ---- the region moons: each title wears the moon its own rows earn ----------
# Four regions answer four questions, so each one's rows earn their own moon: a
# region that repeats the header moon says "the conclusion came from here", and
# the reader learns in one glance which dimension is dark. The reading is the
# four states of §4.2 applied to the region's own numbers, never a per-row
# severity:
#
#   any admission block in the rows      -> the new moon
#   a refusal, or a wall that cannot be
#   ruled out, or a latency moving up    -> the quarter moon
#   nothing measured at all              -> the plain moon
#   measured, and clean                  -> the full moon

REGION_LOAD = "load"
REGION_LATENCY = "latency"
REGION_REFUSALS = "refusals"
REGION_STALLS = "stalls"
REGION_KINDS = (REGION_LOAD, REGION_LATENCY, REGION_REFUSALS, REGION_STALLS)


class _RowFact(NamedTuple):
    """One region row, read: its label, its badge, its number, its own words."""

    label: str
    badge: str
    count: int | None
    text: str


# The row labels the regions draw (§3.3, §8 item 4), longest first so `at limit`
# never reads as a row called `at`. A row that starts with none of these is
# context this module cannot read a number out of, and it never moves a moon.
_ROW_LABELS = ("peak", "now", "blocked", "p50", "p90", "gateway", "429",
               "at limit", "background", "stalled", "starved", "slowest")
_ROW_LABEL = re.compile(
    r"^(%s)\b" % "|".join(re.escape(label) for label in
                          sorted(_ROW_LABELS, key=len, reverse=True)),
    re.IGNORECASE)
_FIRST_INT = re.compile(r"\d+")
_WINDOW_LABEL = re.compile(r"\d+h$")

# Rows that carry no severity: `now` is the account's instantaneous occupancy and
# has no previous window to move against, and `gateway` is a fixed wait. Neither
# can darken a region, and neither can lighten one.
_CONTEXT_LABELS = ("now", "gateway")

# What the rows have to say, as closed sets of labels. `blocked` is the darkest
# in the LOAD region, because those requests never got in at all; a stall is
# pressure in the region that reports one (§4.2).
_BLOCK_LABELS = ("blocked",)
_PRESSURE_LABELS = ("stalled", "starved")
_LATENCY_LABELS = ("p50", "p90")

# The two ways work never ran, as the REFUSALS region draws them (§2.4): the
# gateway's 429, and the refusal at the account's full pool. In that region these
# rows are the region's own subject, so a count above zero is the darkest moon -
# the rule the admission counts follow: severity comes from the counts, never
# from the qualifier printed beside them.
#
# The third row, `background`, is not here: a request sent to the slow tier RAN.
# `cli.verdict` calls that window healthy, so a demotion can never darken a
# region under a full header - and it is the exact configuration the NEXT row
# recommends, which must not read as over capacity.
_REFUSAL_LABELS = ("429", "at limit")

# A load row's own words: a peak that crossed the wall, or a peak with no wall to
# be measured against. `never over` is the clean case and never matches.
_PEAK_OVER = re.compile(r"(?<!never )\bover\b")
_LANES_UNKNOWN = "lane count unknown"
_UP_ARROW = "▲"


def _row_fact(badge, text) -> _RowFact:
    """One (badge, text) row, read as a label, a count and its own words.

    The count is the first integer after the label, because the number column
    precedes the delta column (§3.2) and a row prints its own number first. A
    `1h` window label is the label's unit, not a row's number.
    """
    body = (text or "").strip()
    match = _ROW_LABEL.match(body)
    label = match.group(1).lower() if match else ""
    tokens = body[match.end():].split() if match else []
    if label == "peak" and tokens and _WINDOW_LABEL.match(tokens[0]):
        tokens = tokens[1:]
    count = None
    for token in tokens:
        found = _FIRST_INT.search(token)
        if found:
            count = int(found.group(0))
            break
    return _RowFact(label, badge, count, body)


def region_moon(region_rows: list[tuple[str, str]], region_kind: str) -> str:
    """The status glyph one region's own rows earn (§4.2), as a glyph.

    `region_rows` are the region's (badge, text) data rows - the ones the reader
    sees, not a second set built for the moon - and `region_kind` says which of
    the four regions they belong to, because each answers a different question
    and so reads severity differently. Nothing here looks at the header: the
    regions are the causes, and the conclusion is `header_moon`'s.

    Two kinds of count earn the darkest moon, because both are work that never
    ran: a LOAD region's admission block, and a 429 or an at-limit refusal in a
    REFUSALS region. The second closes a hole the invariant would otherwise leave
    open: a window can be dark because the gateway refused the work, with no
    admission block in the LOAD rows, and the region that holds those refusals
    has to be able to say so. A request sent to the background tier ran, so a
    demotion there is clean rather than dark.
    """
    if region_kind not in REGION_KINDS:
        raise KeyError(region_kind)
    facts = [_row_fact(badge, text) for badge, text in region_rows]
    evidence = [fact for fact in facts
                if fact.label and fact.label not in _CONTEXT_LABELS]
    if not any(fact.count is not None for fact in evidence):
        # nothing this region measured: the plain moon claims no status (§2.5)
        return moon_glyph(_PLAIN_MOON)
    if any(fact.count for fact in evidence if fact.label in _BLOCK_LABELS):
        return moon_glyph(report.MOON_NEW)
    if region_kind == REGION_REFUSALS and any(
            fact.count for fact in evidence if fact.label in _REFUSAL_LABELS):
        return moon_glyph(report.MOON_NEW)
    if _region_pressure(evidence, region_kind):
        return moon_glyph(report.MOON_QUARTER)
    return moon_glyph(report.MOON_FULL)


def _region_pressure(evidence, region_kind: str) -> bool:
    """Whether a region's own rows say pressure, for this kind of region.

    A stall above zero is pressure in the region that reports it. A refusal is
    darker than pressure and belongs to the REFUSALS region, which `region_moon`
    reads before asking here. A load region adds the qualifier its peak wears:
    `over` means the wall was crossed and `lane count unknown` means there is no
    wall to measure against, so neither can read clean, while `never over` is a
    clean window whose peak happened to move (§2.2). A latency region adds its
    own direction, because a percentile has no wall to cross and the move is the
    whole story (§2.1: p90 240s rising reads pressure; §2.2: p90 58s falling
    reads clean).
    """
    for fact in evidence:
        if fact.label in _PRESSURE_LABELS and fact.count:
            return True
    if region_kind == REGION_LOAD:
        return any(_PEAK_OVER.search(fact.text) or _LANES_UNKNOWN in fact.text
                   for fact in evidence if fact.label == "peak")
    if region_kind == REGION_LATENCY:
        return any(_UP_ARROW in fact.text for fact in evidence
                   if fact.label in _LATENCY_LABELS)
    return False


def regions_satisfy_invariant(header_glyph: str,
                              region_glyphs: list[str]) -> bool:
    """§4.2's tie between the conclusion and its causes, as a testable predicate.

    Two implications, and they are the whole rule: a header at the darkest moon
    is explained by at least one region at the darkest moon, and a header at the
    full moon is contradicted by no region that claims trouble. A lighter region
    never explains a darker header, and a darker region never contradicts a clean
    window. The quarter moon and the plain moon carry no constraint, because a
    window can be under pressure for a reason no region on this screen measures.
    """
    glyphs = list(region_glyphs)
    if header_glyph == moon_glyph(report.MOON_NEW):
        return moon_glyph(report.MOON_NEW) in glyphs
    if header_glyph == moon_glyph(report.MOON_FULL):
        return all(glyph == moon_glyph(report.MOON_FULL) for glyph in glyphs)
    return True


# ---- the copy rules, as far as they are mechanical -------------------------
# Ten rules govern the dashboard's register (§7), and three of them are
# mechanical: no sentence in a data row, 78 cells per row, and no parenthetical
# naming a field. The rest (one imperative, active voice, units in the label, a
# one-clause qualifier) are read by a person. A helper the suite can call is what
# keeps the mechanical three from drifting.
# A full stop ends a sentence when it ends a word: the dot in a duration
# (`312.4s`) or a version is part of the number, not the end of a sentence.
_SENTENCE_STOP = re.compile(r"\.(?=\s|$)|!")
_FIELD_PAREN = re.compile(r"\([^)]*[`_][^)]*\)")


def dashboard_ok_row(text: str) -> bool:
    """Whether one row obeys the copy rules and the width budget (§3.2, §7).

    - No sentence-final stop: a full stop that ends a word, or an exclamation
      mark, means a sentence arrived, and a row is not a sentence. The dot in
      `312.4s` is a decimal, not a stop. `?` is allowed where it is the unknown
      glyph (`? lanes`, `? plan`) and rejected at the end of a row, where it
      would be a question.
    - No row wider than 78 terminal cells, measured in cells rather than code
      points: a phase moon is two cells and every other glyph in the palette is
      one.
    - No parenthetical naming a field: a bracket holding a backtick or a
      snake_case name is a report's field name carried onto a dashboard.
    """
    if not isinstance(text, str):
        return False
    if cell_len(text) > ROW_CELLS:
        return False
    if _SENTENCE_STOP.search(text) or text.rstrip().endswith("?"):
        return False
    return not _FIELD_PAREN.search(text)


def resolved_lanes(metrics: dict, cap) -> tuple:
    """The priority concurrency this screen judges against, and whose it is.

    attribution.resolve_priority_concurrency decides account first, then a
    --plan declaration. The header's lane count and the picture's lane scale
    are one number because both ask this function, so `●12` and the caption's
    `●12 lanes` cannot drift apart.
    """
    config = cli._cap_field(cap, "config")   # cap is None when nothing is cached
    served = config.to_dict() if hasattr(config, "to_dict") else None
    return attribution.resolve_priority_concurrency(
        served, (metrics or {}).get("declared_plan"))


# ---- the sparkline: the window's shape in one row of blocks ---------------
def _epoch(value):
    """A usable epoch second, or None: a bool is not a clock, and neither is a string."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def _minute_epoch(stamp):
    """Seconds since the epoch for one by_minute stamp ("2026-11-02T23:00Z")."""
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%MZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _minute_series(metrics: dict) -> list[tuple[float, int]]:
    """The window's active minutes as sorted (epoch, max_concurrent) pairs."""
    series = []
    for entry in metrics.get("by_minute") or []:
        if not isinstance(entry, dict):
            continue
        stamp = _minute_epoch(entry.get("minute"))
        value = _count(entry.get("max_concurrent"))
        if stamp is not None and value:
            series.append((stamp, value))
    series.sort()
    return series


class _Picture(NamedTuple):
    """One drawn window: the columns, and the facts its caption states."""

    columns: list[int]
    minutes_per_column: float
    total_minutes: int
    peak_at: float | None


def _window_picture(metrics: dict, width: int) -> _Picture | None:
    """The window re-binned onto `width` uniform columns, or None when quiet.

    by_minute is sparse - concurrency_by_minute drops every quiet minute - so
    one block per entry would draw a busy hour and a busy day at the same
    width. The series is re-binned instead: one column per slice of the
    window, and a column nobody was running in stays 0, which draws as a space
    rather than a low bar. A window that was quiet at its start reads as
    blanks, not as low bars.

    Each column carries the busiest minute inside it. The window closes at the
    scan's clock when the metrics carry one (scan sets scanned_at) and at the
    last active minute otherwise. The busiest minute of the drawn window comes
    back as `peak_at`, so the caption's clock and the picture cannot disagree.
    """
    series = _minute_series(metrics)
    if width < 1 or not series:
        return None
    hours = metrics.get("window_hours") or WINDOW_HOURS
    span = max(60.0, hours * 3600)
    end = _epoch(metrics.get("scanned_at")) or series[-1][0] + 60.0
    start = end - span
    per_column = span / width
    columns = [0] * width
    for stamp, value in series:
        if stamp < start:
            # A minute before the window is not this window's traffic, and
            # clamping it into column 0 would draw it as if it were.
            continue
        column = min(width - 1, int((stamp - start) / per_column))
        columns[column] = max(columns[column], value)
    peak = max(columns)
    peak_at = next((stamp for stamp, value in series
                    if value == peak and start <= stamp <= end), None)
    return _Picture(columns, per_column / 60.0, int(round(span / 60.0)), peak_at)


def _lane_block(value: int, lanes: int) -> str:
    """One minute's block on the lane scale: 7 at or over, the approach below.

    `█` is the one glyph on this screen that means a state rather than a
    magnitude: at or over the account's lane count, on every screen and in
    every window. Below it the ramp is the distance to the wall.
    """
    top = len(_SPARK_BLOCKS) - 1
    if value >= lanes:
        return _SPARK_BLOCKS[top]
    return _SPARK_BLOCKS[value * top // lanes]


def _peak_block(value: int, peak: int) -> str:
    """One minute's block scaled against this window's own worst minute."""
    top = len(_SPARK_BLOCKS) - 1
    return _SPARK_BLOCKS[min(top, value * len(_SPARK_BLOCKS) // peak)]


def concurrency_sparkline(metrics: dict, *, width: int = SPARK_WIDTH,
                          lanes: int | None = None) -> str:
    """The window's concurrency as one row of blocks, or "" when it is empty.

    With a lane count the ramp is the distance to the wall, so `█` means at or
    over the account's lanes and the picture reads the same whether the lanes
    are 12 or 120. Without one, the ramp self-scales to this window's peak -
    the picture then says `scale: this window`, because a reader is always
    entitled to know which scale they are looking at.
    """
    picture = _window_picture(metrics, width)
    if picture is None:
        return ""
    if lanes is None:
        peak = max(picture.columns)
        return "".join(" " if not value else _peak_block(value, peak)
                       for value in picture.columns)
    return "".join(" " if not value else _lane_block(value, lanes)
                   for value in picture.columns)


def spark_caption(metrics: dict, *, width: int = SPARK_WIDTH,
                  lanes: int | None = None,
                  source: str = sources.PROV_ACCOUNT) -> str:
    """The picture's one-line key: which scale it is, and when the peak was.

    A lane-scaled picture states what `█` means and converts the count of `█`
    cells into its unit - minutes over the lane count - so the row is
    quantitatively readable and not merely suggestive. A minute count of zero
    reads `never over`, not `over 0m of 60m`: a zero minutes-over is the clean
    case, and the caption says it in words. A peak-scaled picture says so
    instead, and names the peak's clock either way. A window with no active
    minute has no picture, and an empty picture has no caption.

    `source` is whose lane count the wall is - `resolved_lanes`'s own source, so
    the caption's badge is `●` only when the account reported the number and
    `◌` when a person declared it.
    """
    picture = _window_picture(metrics, width)
    if picture is None:
        return ""
    peak_at = local_hhmm(picture.peak_at)
    if lanes is None:
        return f"scale: this window · peak at {peak_at}"
    over = sum(1 for value in picture.columns if value >= lanes)
    minutes_over = int(round(over * picture.minutes_per_column))
    span = ("over %dm of %dm" % (minutes_over, picture.total_minutes)
            if minutes_over else "never over")
    return (f"{_SPARK_BLOCKS[-1]} = at or over {badges.badge_for(source)}{lanes} lanes"
            f" · {span} · peak at {peak_at}")


# ---- the LOAD region: three rows on one fixed grid ------------------------
#
# One geometry for every data row, measured off the mockups in the brief: one
# cell of indent, a 12-cell lowercase label, the badge in its own cell, an
# 8-cell right-aligned number, a 3-cell right-aligned delta, then one fact in
# the qualifier column. The fixed columns are what make the region scannable: a
# reader compares a row against the row above it, so no row is laid out to its
# own length.
#
# The three rows below are the whole LOAD region, and what is missing from them
# is the kill list rather than an oversight: the 30d/7d admission record, the
# overflow pool's per-day rollups, the queue wait on the account's most recent
# complete UTC day and the saturation-band comparison are drill-down facts, and
# the dashboard does not carry them at all.
LOAD_LABEL_CELLS = 12
LOAD_NUMBER_CELLS = 8
LOAD_DELTA_CELLS = 3

# The delta column's four cells. `▬` is a comparable window that did not move;
# `·` is no comparable window at all. No arrow carries a moon: the arrows say
# direction and the region moon says severity, and mashing them together turns
# a symbol system into stickers.
DELTA_UP = "▲"
DELTA_DOWN = "▾"
DELTA_LEVEL = "▬"
DELTA_NONE = "·"


def _grid_row(label: str, badge: str, number: str = "",
              delta: str = DELTA_NONE, qualifier: str = "") -> tuple[str, str]:
    """One data row as (badge, the line it prints), on the fixed grid.

    The badge is in the line the row prints, so the region draws each line as
    it arrives and no caller measures columns twice. Returning it beside the
    line is what lets a screen style by provenance and a test read the source
    without parsing the row.
    """
    line = " %s %s %s %s" % (label.ljust(LOAD_LABEL_CELLS), badge,
                              number.rjust(LOAD_NUMBER_CELLS),
                              delta.rjust(LOAD_DELTA_CELLS))
    if qualifier:
        line += " " + qualifier
    return badge, line


def _window_requests(metrics) -> int:
    """A window's request count. An absent count is no traffic."""
    return _count((metrics or {}).get("requests")) or 0


# The outcome classes a delta can compare, from baseline's one pressure table,
# so a signal added there is a signal the delta column can carry.
_OUTCOME_KEYS = tuple(name for _label, name in baseline.PRESSURES)


def _delta_value(metrics: dict, key: str):
    """One window's count for a delta key: an outcome class or a top-level metric.

    An outcome class the scan never recorded is a measured zero: analyse() drops
    a key nobody hit, and a window that was read and carried nothing of that
    kind is not the same fact as a window nobody read. A top-level metric stays
    strict - a missing peak is unknown, not a quiet window.
    """
    block = metrics or {}
    if key in _OUTCOME_KEYS:
        count = _count((block.get("by_outcome") or {}).get(key))
        return count if count is not None else (0 if _window_requests(block) else None)
    return _count(block.get(key))


def delta_cells(metrics: dict, previous, key: str) -> str:
    """The 3-cell delta for one count, against the equal-length window before it.

    A delta needs a window to compare against, and enough traffic in both for a
    move to mean anything, so it is gated by the same floor the verdict uses:
    under `baseline.FLOOR` requests in either window the cell is `·`, the
    placeholder for "no comparable window". `▬` is a comparable window that did
    not move, which is a claim about two windows that each carried the count; a
    count that was zero in both windows has no change to report and reads `·`
    like any other absent comparison. No moon ever lands in this cell -
    direction is a row fact and severity is a region fact - so the region title
    is the only severity here.
    """
    return _delta_text(_delta_move(_delta_value(metrics, key),
                                   _delta_value(previous, key), metrics, previous))


def _delta_move(now, before, metrics: dict, previous) -> int | None:
    """The signed move of one pair of counts, or None when there is none to show.

    None is every case the delta column reads `·`: no previous window, a window
    under `baseline.FLOOR`, a count nobody read, and a count that was zero in
    both windows - no change to report, which `▬` would overstate.
    """
    if not previous:
        return None
    if min(_window_requests(metrics), _window_requests(previous)) < baseline.FLOOR:
        return None
    if now is None or before is None or (not now and not before):
        return None
    return now - before


def _delta_text(move: int | None, digits: int | None = None) -> str:
    """One delta cell from a move `_delta_move` already read.

    `digits` is the width an evidence group's numbers share, so a group prints
    `▲  4` beside `▲ 11` (§2.4) and `▾ 4` in a group whose numbers are all one
    digit (§2.2). Without it the cell is the LOAD region's three cells: one cell
    of air up to nine, so a ten-request move still fits.
    """
    if move is None:
        return DELTA_NONE
    if move == 0:
        return DELTA_LEVEL
    arrow = DELTA_UP if move > 0 else DELTA_DOWN
    moved = abs(move)
    if digits:
        return "%s %s" % (arrow, str(moved).rjust(digits))
    return "%s %d" % (arrow, moved) if moved < 10 else "%s%d" % (arrow, moved)


def _stamp_epoch(stamp):
    """Seconds since the epoch for one ISO-8601 UTC stamp, or None."""
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def local_hhmm(stamp) -> str:
    """`14:32-08:00` for one epoch second, on this machine's own clock.

    The logs and the account API both speak UTC, so the offset is printed beside
    the minute rather than assumed: a person comparing this screen to a UTC log
    has to know which clock they are reading. One form for every timestamp on the
    screen - the sample's minute and the peak's - so two of them can be compared
    at a glance. A stamp that is not an epoch second prints nothing.
    """
    epoch = _epoch(stamp)
    if epoch is None:
        return ""
    local = time.localtime(epoch)
    offset = local.tm_gmtoff
    hours, minutes = divmod(abs(offset) // 60, 60)
    return "%02d:%02d%s%02d:%02d" % (local.tm_hour, local.tm_min,
                                      "-" if offset < 0 else "+",
                                      hours, minutes)


def _minute_of_day(stamp) -> str:
    """`HH:MM` in local time with its offset, or "" when it cannot be read.

    The microseconds the account sends are a fraction nobody acts on, and the
    day cannot change inside one dashboard window.
    """
    return local_hhmm(_stamp_epoch(stamp))


def _sample_age_s(metrics: dict, stamp, wall_now=None):
    """How old the account's sample is, in seconds, or None when it cannot be told.

    The clock is the scan's own, so this row is a function of the metrics and
    reads no clock of its own. `wall_now` is the moment the row is being drawn,
    which a screen left open for a while passes so the sample's age keeps counting
    up with the age in the header.
    """
    epoch = _stamp_epoch(stamp)
    clock = _epoch(wall_now if wall_now is not None
                   else (metrics or {}).get("scanned_at"))
    if epoch is None or clock is None:
        return None
    return max(0.0, clock - epoch)


def _background_cell(cap) -> str:
    """The background pool's live occupancy, in one clause (§3.3, the `now` row).

    Two pools answer "right now", and they are different pools: `in_use` on the
    flat sample counts priority requests only, and a request demoted to the slow
    tier lands in `background` instead. So the ratio in the number column is the
    priority pool and this clause is the other one, on the same row because both
    come from one sample and share its one badge.

    Three shapes, from the model's own null contract (`sources.CapacityPool`):

    - a measured cap prints `in_use/limit`, `?` for a count that was not read;
    - `limit: 0` is a **closed** pool - the account is entitled to nothing here,
      so the clause says `closed` and the count (a measured 0) rides beside it;
    - `limit: null` is a cap that could not be read, which is unknown and not a
      closed pool, so the clause says `cap unknown` rather than 0.

    No block at all is unknown too, never an idle pool. The `saturated` flag is
    never printed: the ratio and `closed` already state it, and a flag that reads
    `yes` is refusal language the dashboard does not speak.
    """
    pool = sources.capacity_overflow_now(cap)
    if not isinstance(pool, dict):
        return "background ? not reported"
    limit = _count(pool.get("limit"))
    in_use = _count(pool.get("in_use"))
    count = "?" if in_use is None else "%d" % in_use
    if limit == 0:
        return "background %s closed" % count
    if limit is None:
        return "background %s cap unknown" % count
    return "background %s/%d" % (count, limit)


def _now_row(metrics: dict, cap, wall_now=None) -> tuple[str, str]:
    """The account's live occupancy: the one number that answers "right now".

    The number column is the priority pool's `in_use/limit`; the qualifier
    carries the background pool's own occupancy and then the sample time, so a
    person sees both pools a request can be sitting in without reading a second
    row. Two numbers from one source wear one `●` (§4.1): the whole row is one
    sample, so one badge leads it and neither pool prints a badge of its own.

    Freshness is read from `metrics["capacity"]["now_fresh"]` and never from the
    Capacity payload, which carries no freshness field: a 47-minute-old sample
    that still wore `●` is the defect this row exists to fix. A sample that is
    not fresh keeps its numbers and loses the authority, reading `?` and
    `stale 47m`; a cache with no live sample prints no ratio and says why.

    `wall_now` dates the sample against the moment the row is drawn, so a screen
    left open long enough for the sample to age past the freshness window says so
    instead of wearing a `●` it earned when it opened. That is the same window the
    header's age cell gains its marker on (`AGE_STALE_AFTER_S`), which is why the
    two cannot disagree about one sample.
    """
    served = (metrics or {}).get("capacity") or {}
    now = cli._cap_field(cap, "now")
    in_use = _count(cli._cap_field(now, "in_use"))
    limit = _count(cli._cap_field(now, "limit"))
    if in_use is None or limit is None:
        reason = "no capacity cached" if now is None else "no live sample"
        return _grid_row("now", _BADGE_UNKNOWN, qualifier=reason)
    number = "%d/%d" % (in_use, limit)
    pool = _background_cell(cap)
    sampled_at = cli._cap_field(now, "sampled_at")
    sampled = _minute_of_day(sampled_at)
    age_s = _sample_age_s(metrics, sampled_at, wall_now)
    fresh = (served.get("now_fresh") is True and sampled
             and (age_s is None or age_s <= cli.CAPACITY_FRESH_S))
    if fresh:
        return _grid_row("now", badges.BADGE_ACCOUNT, number,
                         qualifier="%s \u00b7 sampled %s" % (pool, sampled))
    return _grid_row("now", _BADGE_UNKNOWN, number,
                     qualifier="%s \u00b7 %s" % (
                         pool, ("stale %dm" % int(age_s // 60))
                         if age_s is not None else "stale sample"))


def _peak_row(metrics: dict, previous, cap, window_hours) -> tuple[str, str]:
    """This machine's peak concurrency, against the lane count it is judged by.

    The peak is measured here and nowhere else, so the row wears `○` and the
    "a lower bound" caveat moves into the key line instead of riding the row.
    The qualifier is the one fact the number cannot carry alone: how far over
    the wall it went, that it never reached it, or that there is no wall.
    """
    label = "peak %gh" % window_hours
    peak = _count((metrics or {}).get("peak_concurrency"))
    if peak is None or not _window_requests(metrics):
        return _grid_row(label, _BADGE_UNKNOWN, qualifier="nothing measured here")
    lanes, _source = resolved_lanes(metrics, cap)
    if lanes is None:
        qualifier = "lane count unknown"
    elif peak > lanes:
        qualifier = "over ●%d by %d" % (lanes, peak - lanes)
    else:
        qualifier = "never over"
    return _grid_row(label, _BADGE_MEASURED, "%d" % peak,
                     delta_cells(metrics, previous, "peak_concurrency"), qualifier)


def _blocked_row(metrics: dict, previous) -> tuple[str, str]:
    """The local admission blocks, with one cross-check in the qualifier.

    `admission_timeout` is the count of requests that never got in, which is
    the number a person is losing work to; the API's field name stays in the
    drill-down. The `× 429` cell is the one cross-check the number needs - it
    says whether the gateway was refusing traffic at the same time, which is
    the difference between a pool that is full and a gateway that is broken -
    so it prints even at zero.

    A window this scan read carried no block of that kind when analyse() has no
    key for it, so an absent outcome is a measured zero and the row reads `○ 0`.
    `?` is kept for a window that was never measured at all: no scan, no
    traffic, no number to print.
    """
    if not _window_requests(metrics):
        return _grid_row("blocked", _BADGE_UNKNOWN)
    by_outcome = (metrics or {}).get("by_outcome") or {}
    blocked = _count(by_outcome.get("admission_timeout")) or 0
    rejected = _count(by_outcome.get("rate_limited")) or 0
    return _grid_row("blocked", _BADGE_MEASURED, "%d" % blocked,
                     delta_cells(metrics, previous, "admission_timeout"),
                     "%d × 429" % rejected)


def load_rows(metrics: dict, cap, window_hours, previous=None,
              wall_now=None) -> list[tuple[str, str]]:
    """The LOAD region's three data rows: now, peak, blocked - always three.

    The order is the order a person asks the questions in: what is happening at
    this instant, how bad it got in this window, and what that cost. Each row
    is (badge, the line it prints), with the badge cell already in the line.

    `previous` is the equal-length window before this one - the scan's own
    extra read. Without it every delta cell is `·`: a first scan has nothing to
    compare against, and the screen says that once, here, rather than printing
    a zero delta that reads as "and nothing changed".
    """
    return [_now_row(metrics, cap, wall_now),
            _peak_row(metrics, previous, cap, window_hours),
            _blocked_row(metrics, previous)]


# ---- the evidence group: the three rows that say what kind of trouble ------
# A group is the screen's third region: three data rows on the same grid the LOAD
# region draws, built from metrics keys the analysis already produces. The answer
# selects which group stands under the picture (§8 item 4), and its title then
# wears the moon those three rows earn (`region_moon`), so a group is one region
# and not a second scoring system.
#
# The delta column is the one place these rows differ from LOAD's: a group's
# numbers share one width, because the mockups print `▲  4` beside `▲ 11` in the
# same group (§2.4) and `▾ 4` in a group whose numbers are all one digit (§2.2).
# One width per group is what makes both read as columns rather than as steps.


class _EvidenceRow(NamedTuple):
    """One evidence row before it is drawn: its facts, in their columns.

    `move` is the signed change against the previous window: None when there is
    no comparable window (the row prints `·`), and 0 when both windows read the
    same (`▬`).
    """

    label: str
    badge: str
    number: str
    move: int | None
    qualifier: str


def _group_digits(moves) -> int:
    """The width one group's delta numbers share: one cell, or more for counts."""
    return max([len(str(abs(move))) for move in moves if move] or [1])


def _sampled(tier) -> bool:
    """Whether a latency tier measured anything: a percentile with no n is `?`."""
    return bool(_count((tier or {}).get("n")))


def _seconds_cell(value) -> str:
    """A duration as the label's unit says: seconds, an integer above 10s."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    return "%ds" % int(round(value)) if value >= 10 else "%.1fs" % value


def _latency_group(metrics: dict, previous: dict | None) -> list[_EvidenceRow]:
    """LATENCY: the interactive p50 and p90, then the gateway's fixed wait.

    The percentiles are completed interactive requests only, each badged ○
    because it measures these logs and so is a lower bound. A percentile with no
    sample prints `?` and no number: a suppressed percentile never prints its
    denominator (§6.3 item 19). The gateway row is the documented wait every
    request pays before headers, so it has no trend - `·`, never an arrow - and
    it is context, not severity, when the region reads its own moon.
    """
    tier = (metrics.get("latency_s") or {}).get("interactive") or {}
    before = ((previous or {}).get("latency_s") or {}).get("interactive") or {}
    measured = _sampled(tier)
    before_measured = _sampled(before)
    rows = []
    for label in ("p50", "p90"):
        number = _seconds_cell(tier.get(label)) if measured else ""
        earlier = _seconds_cell(before.get(label)) if before_measured else ""
        rows.append(_EvidenceRow(
            label,
            _BADGE_MEASURED if number else _BADGE_UNKNOWN,
            number,
            (_delta_move(round(tier[label]), round(before[label]), metrics, previous)
             if number and earlier else None),
            ""))
    rows.append(_EvidenceRow("gateway", _BADGE_MEASURED,
                             "%ds" % int(round(cli.PRE_HEADER_WAIT_S)), None,
                             "fixed wait before headers"))
    return rows


# What happened to the slowest request, in words. The explain digest carries the
# outcome as an API class name, and §7 rule 6 keeps those names in the
# drill-down: this row says the same fact in the register the screen speaks.
_OUTCOME_WORDS = {
    "completed": "it finished",
    "client_idle_timeout": "the idle budget killed it",
    "admission_timeout": "it was blocked at admission",
    "rate_limited": "the gateway refused it with a 429",
    "org_concurrency_limit": "the account's pool was full",
}


def _refusals_group(metrics: dict, previous: dict | None) -> list[_EvidenceRow]:
    """REFUSALS: the three ways the gateway refuses work, each with its delta.

    Every count is this machine's own outcome count, badged ○. The clauses say
    what the class means in words a person can act on and never name the field
    (§7 rule 6). In a window that carried traffic, a counter's absent class is a
    zero: nothing was refused, so nothing was unread. A window with no traffic at
    all is not evidence of health - zero refusals out of zero requests - so it
    reads `?` with no number, the shape §2.5 gives an empty window.
    """
    measured = bool(_window_requests(metrics))
    by_outcome = metrics.get("by_outcome") or {}
    by_tier = metrics.get("by_tier") or {}
    before = previous or {}
    rows = []
    for label, side, key, qualifier in (
            ("429", "by_outcome", "rate_limited", "the account's rate limit"),
            ("at limit", "by_outcome", "org_concurrency_limit", "account pool full"),
            ("background", "by_tier", "background", "demoted to the slow tier")):
        if not measured:
            rows.append(_EvidenceRow(label, _BADGE_UNKNOWN, "", None,
                                     "nothing measured"))
            continue
        now = _count(((metrics.get(side) or {})).get(key)) or 0
        then = _count(((before.get(side) or {})).get(key)) or 0
        rows.append(_EvidenceRow(label, _BADGE_MEASURED, "%d" % now,
                                 _delta_move(now, then, metrics, previous), qualifier))
    return rows


def _stalls_group(metrics: dict, previous: dict | None) -> list[_EvidenceRow]:
    """STALLS: what stalled, the slowest request, and the starved-peer count.

    A stall is a request the harness's own idle budget killed, and the clause
    says that rather than naming the class. The slowest row carries the duration
    explain ranked first and, in its clause, what happened to that request - the
    one fact a duration cannot carry on its own. The starved row is the
    cross-machine evidence: this request stalled while another request to the
    same model finished, so the model was healthy and this request was never
    served.

    Only the two counts have a previous window to compare against. The digest is
    ranked from this window's requests, so its delta is `·`: an arrow there would
    claim a comparison nobody measured.
    """
    measured = bool(_window_requests(metrics))
    by_outcome = metrics.get("by_outcome") or {}
    before = (previous or {}).get("by_outcome") or {}
    stalled = _count(by_outcome.get("client_idle_timeout")) or 0
    starved = _count(metrics.get("starved_peer_evidence")) or 0
    digest = metrics.get("slowest_request") or {}
    duration = digest.get("duration") if isinstance(digest.get("duration"), str) else ""
    stalled_row = _EvidenceRow("stalled", _BADGE_MEASURED, "%d" % stalled,
                               _delta_move(stalled,
                                           _count(before.get("client_idle_timeout")) or 0,
                                           metrics, previous),
                               "the harness's idle budget")
    starved_row = _EvidenceRow("starved", _BADGE_MEASURED, "%d" % starved,
                               _delta_move(starved,
                                           _count((previous or {}).get("starved_peer_evidence")) or 0,
                                           metrics, previous),
                               "stalled while a peer finished")
    if not measured:
        # A window that measured nothing did not measure a clean run of zeroes
        # either: the same `?` an empty refusals group prints.
        stalled_row = _EvidenceRow("stalled", _BADGE_UNKNOWN, "", None,
                                   "nothing measured")
        starved_row = _EvidenceRow("starved", _BADGE_UNKNOWN, "", None,
                                   "nothing measured")
    return [
        stalled_row,
        # An unknown with no reason is the bare `?` §7 rule 8 refuses: a window
        # that ranked nothing says so.
        _EvidenceRow("slowest", _BADGE_MEASURED if duration else _BADGE_UNKNOWN,
                     duration, None,
                     _OUTCOME_WORDS.get(digest.get("outcome"), "")
                     if duration else "nothing ranked"),
        starved_row,
    ]


_GROUPS = {
    REGION_LATENCY: _latency_group,
    REGION_REFUSALS: _refusals_group,
    REGION_STALLS: _stalls_group,
}


def group_rows(group_key: str,
               metrics: dict,
               previous: dict | None = None) -> list[tuple[str, str]]:
    """The three evidence rows for one group, on the fixed grid.

    The rows are built from metrics keys the analysis already produces: no group
    reads a log, ranks a request or re-derives a count. `previous` is the
    equal-length window before this one, and it decides nothing but the delta
    column - without it, or with either window under `baseline.FLOOR`, every row
    prints `·`.

    Each row comes back as (badge, the line it prints), the same shape the LOAD
    region returns: the badge cell is already in the line, so the screen draws
    each row as it arrives and no caller measures columns twice.
    """
    cells = _GROUPS[group_key](metrics or {}, previous)
    digits = _group_digits([cell.move for cell in cells])
    return [_grid_row(cell.label, cell.badge, cell.number,
                      _delta_text(cell.move, digits), cell.qualifier)
            for cell in cells]


# ---- the NEXT region: the one imperative, and the command that carries it --
NEXT_CELLS = 60        # the imperative's budget, §7 rule 2


def _window_hours(metrics) -> int:
    """The window the screen is reading; the dashboard's own is one hour."""
    hours = _count((metrics or {}).get("window_hours"))
    return hours if hours and hours > 0 else WINDOW_HOURS


def _command_line(command: str, *, hours: int | None = None,
                  key: str | None = None) -> str:
    """The command a reader types, its keypress right-aligned when one runs it.

    The key lands on the same edge the header's clock does, so the screen has one
    right margin and the eye finds the key in the same place on both rows.
    """
    line = "\u2192 luna-doctor %s" % command
    if hours is not None:
        line += " --since %gh" % hours
    if key:
        line = line.ljust(HEADER_CELLS - len("[%s]" % key)) + "[%s]" % key
    return line


def next_rows(metrics: dict, cap, group_key: str) -> list[tuple[str, str]]:
    """The NEXT region: one imperative, then the command that carries it.

    Four states and no fifth. They are read from the facts the header reads -
    `header_moon` decides the state, and `resolved_lanes` decides whether there
    is a lane count to act on at all - because this row is the last row of one
    decision, not a second opinion:

    - nothing measured (the plain moon): widen the window, and the command reads
      the next wider window `[w]` offers;
    - a window nobody can judge (no lane count): hold flat until the account
      answers, and the command fetches the account's width;
    - pressure or over capacity: hold the lane count in flight and send the rest
      to the -background tier, which is the per-agent fix;
    - otherwise: nothing to change, and `watch` keeps watch.

    `group_key` is the evidence group on screen, checked here so a typo fails
    loudly. It does not choose any of the four wordings: §2.4 draws the same
    imperative beside the refusals group that §2.1 draws beside the latency
    group, and a group-shaped fifth imperative is the sixth the brief refuses.
    """
    if group_key not in _GROUPS:
        raise KeyError(group_key)
    moon = header_moon(metrics)
    lanes, source = resolved_lanes(metrics, cap)
    hours = _window_hours(metrics)
    if moon == _PLAIN_MOON:
        imperative = "Nothing measured in the last hour \u00b7 widen the window"
        wider = window_cycle(hours)
        if wider > hours:
            command = _command_line("analyze", hours=wider, key="w")
        else:
            # The ring wraps to 1h from the widest window, which is not wider:
            # no key claims it, and the command still reads this window.
            command = _command_line("analyze", hours=hours)
    elif lanes is None:
        imperative = "Hold concurrency flat until the lane count is known"
        command = _command_line("capacity --online")
    elif moon in (report.MOON_NEW, report.MOON_QUARTER):
        imperative = "Hold %d in flight \u00b7 send the rest to -background" % lanes
        command = _command_line("fix", hours=hours, key="f")
    else:
        imperative = "Nothing to change \u00b7 %s%d lanes hold this load" % (
            badges.badge_for(source), lanes)
        command = _command_line("watch", hours=hours)
    return [("", "NEXT  " + imperative), ("", command)]


# ---- the keys: provenance, then the moons, one line each ------------------
_PROVENANCE_KEY = "%s account \u00b7 %s this machine (a lower bound) \u00b7 %s unknown" % (
    badges.BADGE_ACCOUNT, badges.BADGE_MEASURED, _BADGE_UNKNOWN)

# The moon key's words, in the order the scale rises: §4.2's own header words,
# one per glyph, so the key and the first row cannot use two words for one moon.
# `style.MOON_STATUS` keeps the words a report prints (`healthy`), which is the
# CLI's register and not this screen's.
_KEY_MOONS = (
    (report.MOON_FULL, "ok"),
    (report.MOON_QUARTER, "pressure"),
    (report.MOON_NEW, "over capacity"),
)
_KEY_PLAIN_MOON = "not established"


def _moon_key(not_established: bool) -> str:
    """The moon line, leading with the plain moon only when the screen drew it."""
    bits = ["%s %s" % (moon_glyph(name), word) for name, word in _KEY_MOONS]
    if not_established:
        bits.insert(0, "%s %s" % (moon_glyph(_PLAIN_MOON), _KEY_PLAIN_MOON))
    return " \u00b7 ".join(bits)


def key_lines(metrics: dict | None = None, cap=None) -> list[str]:
    """The two key lines the screen closes with: provenance, then the moons.

    Two lines replace 162 cells of key (§6.2 item 10). The `\u25cc` entry appears
    only when a `\u25cc` is on the screen, which is exactly when the lane count was
    declared rather than reported - `resolved_lanes` says whose number it is. The
    plain moon's entry appears only when the window claims no status at all,
    because a key to a glyph nobody drew is decoration.
    """
    line = _PROVENANCE_KEY
    _lanes, source = resolved_lanes(metrics or {}, cap)
    if badges.badge_for(source) == badges.BADGE_DECLARED:
        line += " \u00b7 %s declared" % badges.BADGE_DECLARED
    not_established = bool(metrics) and header_moon(metrics) == _PLAIN_MOON
    return [line, _moon_key(not_established)]


# ---- the screen: the eighteen rows the dashboard draws, in order ----------
#
# Nineteen rows, always, in the four regions §3.1 fixes: the header and its rule,
# LOAD's six, the evidence group's five, NEXT's three, and the two key lines. The
# Textual Footer is the nineteenth and renders the bindings. Positions never
# move between states; only the contents of the evidence group change with the
# answer, which is why the reading order is the decision order.
RULE_CELLS = 76      # the rule runs from the left edge to the header's own margin
RULE = "\u2500" * RULE_CELLS

# The words the region titles print, one per region kind. `LOAD` is the only one
# on the screen in every state.
_REGION_WORDS = {
    REGION_LOAD: "LOAD",
    REGION_LATENCY: "LATENCY",
    REGION_REFUSALS: "REFUSALS",
    REGION_STALLS: "STALLS",
}


def region_title(region_kind: str, region_rows: list[tuple[str, str]],
                 window_hours) -> str:
    """One region's title row: its name, the moon its own rows earn, its basis.

    The moon comes from `region_moon` over the very rows the region draws, so a
    title can never claim a state its numbers do not show. The right-hand cell
    names what the delta column compares against - the previous window of the
    same length - and lands at the header's own right edge, so the screen has
    one margin.
    """
    if region_kind not in _REGION_WORDS:
        raise KeyError(region_kind)
    name = "%s %s" % (_REGION_WORDS[region_kind],
                      region_moon(region_rows, region_kind))
    basis = "vs previous %s" % _window_cell(window_hours)
    gap = max(1, HEADER_CELLS - cell_len(name) - cell_len(basis))
    return name + " " * gap + basis


# The caption an empty window draws where the picture's key would go. A blank
# row under a blank picture reads as a rendering fault, so one clause names the
# fact that is missing; the position itself is the contract (§2.5).
NO_PICTURE_CAPTION = "no picture: nothing measured in the last hour"


def dashboard_lines(choice_key: str, metrics: dict, previous, cap,
                   wall_now=None) -> list[str]:
    """The dashboard's eighteen drawn rows, in the order the screen draws them.

    `choice_key` is the answer the person picked; it selects the evidence group
    and never withholds a row (§8 item 6). `previous` is the equal-length window
    the scan read before this one, and it decides the delta column and nothing
    else. The rows are the builders' own strings, verbatim: the LOAD and evidence
    rows carry their badges and indent, the NEXT region carries its imperative
    and command, and the two key lines carry the palette.

    `wall_now` is the moment the screen is being drawn, and it dates the two cells
    that are about time rather than about the window: the header's age and the
    `now` row's freshness. Without it both fall back to the scan's own clock, so a
    plain call builds the screen a scan has just filled and a running screen
    passes its own clock once a second and re-derives nothing else.

    A window with no active minute draws no picture: an empty picture is not a
    shape. Both rows stay where §3.1 puts them - the picture blank, its row
    carrying `NO_PICTURE_CAPTION` - because the position of every region under
    them is part of the contract.
    """
    metrics = metrics or {}
    hours = _window_hours(metrics)
    group_key = pick_lead(choice_key)
    load = load_rows(metrics, cap, hours, previous, wall_now)
    group = group_rows(group_key, metrics, previous)
    lanes, source = resolved_lanes(metrics, cap)
    blocks = concurrency_sparkline(metrics, lanes=lanes)
    caption = spark_caption(metrics, lanes=lanes, source=source)
    # The picture and its caption sit one cell in from the rule's left edge.
    if blocks:
        picture, caption_line = " " + blocks, " " + caption
    else:
        picture, caption_line = "", " " + NO_PICTURE_CAPTION
    lines = [header_line(metrics, previous, cap, hours, wall_now), RULE,
             region_title(REGION_LOAD, load, hours)]
    lines += [line for _badge, line in load]
    lines += [picture, caption_line, RULE,
              region_title(group_key, group, hours)]
    lines += [line for _badge, line in group]
    lines += [RULE]
    lines += [line for _badge, line in next_rows(metrics, cap, group_key)]
    return lines + key_lines(metrics, cap)


# ---- the screen's colour: one scale, spent on severity --------------------
#
# The brief spends exactly one accent colour, on the picture. The owner widened
# that deliberately, mactop-style: the moons carry their own terminal colour, so
# severity reads at a glance and the status word, each region title and the
# at-or-over run arrive coloured. The rule that survives is the one that matters:
# colour is redundancy, never the meaning. Every coloured mark is a glyph that
# already says the same thing, so a mono terminal and a NO_COLOR terminal lose
# nothing but speed - Textual's own Monochrome filter handles NO_COLOR, and a
# terminal Rich cannot colour gets the glyphs alone.
#
# Nothing here touches the plain rows: `dashboard_lines` and `help_lines` stay
# uncoloured strings, the width contract is measured on those strings, and this
# layer only adds spans over them. A styled cell counts as zero extra cells.
SEVERITY_COLORS = {
    report.MOON_FULL: "#3fb950",      # the window is clean
    report.MOON_QUARTER: "#d29922",   # pressure
    report.MOON_NEW: "#f85149",      # over capacity, and the at-or-over run
}

# The help overlay's section labels: the one accent the overlay spends, so the
# four sections read as four regions and not as nine loose lines.
HELP_LABEL_COLOR = "#58a6ff"
HELP_SECTIONS = ("KEYS", "MARKS", "DRILL-DOWN", "ACCOUNT")


def _glyph_name_at(line: str, position: int = 0) -> str:
    """The moon name whose glyph starts at `position`, or ""."""
    for name in (report.MOON_FULL, report.MOON_QUARTER, report.MOON_NEW,
                 _PLAIN_MOON):
        glyph = moon_glyph(name)
        if line.startswith(glyph, position):
            return name
    return ""


def _severity_style(name: str) -> str:
    """The bold colour one moon's glyph and word wear, "" for the plain moon.

    The plain moon claims no status, so it takes no colour: a status colour here
    would be a fourth severity the closed set of three does not have.
    """
    color = SEVERITY_COLORS.get(name)
    return "bold %s" % color if color else ""


def _moon_spans(line: str) -> list[tuple[int, int, str]]:
    """Every moon glyph in one row, in its own severity colour.

    A moon means the same thing wherever it is drawn - the header, a region
    title, or the key line that explains it - so one glyph is one colour on every
    row of the screen.
    """
    spans = []
    for name in (report.MOON_FULL, report.MOON_QUARTER, report.MOON_NEW):
        style = _severity_style(name)
        glyph = moon_glyph(name)
        start = line.find(glyph)
        while start != -1:
            spans.append((start, start + len(glyph), style))
            start = line.find(glyph, start + len(glyph))
    return spans


def _line_spans(line: str) -> list[tuple[int, int, str]]:
    """The coloured runs of one plain row, as (start, end, rich style) cells.

    Four marks carry colour and nothing else does: a moon glyph, the header's
    status word and each region title (both in the moon's own colour), the `NEXT`
    imperative in bold, and every `\u2588` - which means at or over the lane count
    wherever it appears, in the picture and in its own caption alike.
    """
    spans = _moon_spans(line)
    name = _glyph_name_at(line, 0)
    if name:
        style = _severity_style(name)
        word = status_word(name)
        start = len(moon_glyph(name)) + 1
        if style and line.startswith(word, start):
            spans.append((start, start + len(word), style))
    for region_word in _REGION_WORDS.values():
        if not line.startswith(region_word + " "):
            continue
        name = _glyph_name_at(line, len(region_word) + 1)
        style = _severity_style(name) if name else ""
        if style:
            spans.append((0, len(region_word) + 1 + len(moon_glyph(name)), style))
    if line.startswith("NEXT "):
        spans.append((0, len(line.rstrip()), "bold"))
    over = SEVERITY_COLORS[report.MOON_NEW]
    spans += [(index, index + 1, "bold %s" % over)
              for index, char in enumerate(line) if char == _SPARK_BLOCKS[-1]]
    return spans


def styled_lines(lines: list[str]) -> Text:
    """The plain rows as one rich Text, with the severity spans over them.

    The text is the plain rows character for character - the caller's own strings
    joined by newlines - so every width assertion and every `str(render())`
    comparison in the suite still reads the uncoloured screen, and the colours
    exist only as spans.
    """
    text = Text()
    for index, line in enumerate(lines):
        if index:
            text.append("\n")
        start = len(text)
        text.append(line)
        for begin, end, style in _line_spans(line):
            text.stylize(style, start + begin, start + end)
    return text


# ---- the help overlay: every key, every mark, and whose account -----------
#
# The dashboard carries one imperative and two gloss lines, and that is the point
# (rule 2): a screen that explains itself is a screen with no room for numbers.
# The vocabulary still has to live where a person can ask for it, so `[?]` opens
# one overlay: every binding in one line each, the two gloss lines spelled out,
# what the drill-downs run, and the account whose capacity the numbers are. It is
# a modal screen, so the eighteen rows under it are untouched and closing it
# restores exactly what was there - no re-scan, no moved row.
HELP_KEYS = (
    ("f", "show the fix for this window"),
    ("d", "show the analyze report"),
    ("c", "draw the chart"),
    ("g", "show the next evidence group"),
    ("w", "read the next window"),
    ("?", "open and close this help"),
    ("q", "quit"),
)


def _account_facts(metrics: dict, cap) -> list[tuple[str, str]]:
    """The account's own facts, in the order `luna-doctor capacity` leads with.

    Every value comes from what the screen already holds - the cached payload the
    badges read and `resolved_lanes` for the width - so the overlay cannot name an
    account the header does not. Rule 8 governs the nulls: `?` plus a one-to-three
    word reason, never a zero standing in for a number nobody reported. A
    `background_lanes` of 0 is the one measured zero here, and it is a real one:
    the account is entitled to no overflow, so the pool is closed.
    """
    config = cli._cap_field(cap, "config")
    plan = cli._cap_field(config, "plan")
    if not (isinstance(plan, str) and plan.strip()):
        plan = (metrics.get("capacity") or {}).get("plan")
    if not (isinstance(plan, str) and plan.strip()):
        plan = metrics.get("declared_plan")
    lanes, source = resolved_lanes(metrics, cap)
    background_lanes = cli._cap_field(config, "background_lanes")
    if isinstance(background_lanes, bool) or not isinstance(background_lanes, int):
        background = "? not reported"
    else:
        background = "0 closed" if background_lanes == 0 else "%d" % background_lanes
    routing = cli._cap_field(config, "overflow_routing")
    if cap is None:
        cache = "? not cached"
    else:
        cache = age_words(cap.age_s) + (" stale" if cap.stale else "")
    return [
        ("plan", plan if isinstance(plan, str) and plan.strip() else "? not reported"),
        ("priority lanes", "? not reported" if lanes is None
         else "%s%d" % (badges.badge_for(source), lanes)),
        ("background lanes", background),
        ("overflow routing", "? not reported" if routing is None else "%s" % routing),
        ("cache age", cache),
    ]


def help_lines(metrics: dict | None = None, cap=None) -> list[str]:
    """The overlay's rows: the keys, the marks, the drill-downs, the account.

    Plain rows on the same 78-cell budget as the dashboard, so the overlay is a
    region of this screen rather than a wall of prose: one line per binding, one
    line per mark, and the account's five facts as label/value pairs.
    """
    metrics = metrics or {}
    lines = [" luna-doctor help · ? or esc closes it", "", " KEYS"]
    lines += ["  %-3s %s" % (key, what) for key, what in HELP_KEYS]
    lines += ["", " MARKS",
              "  %s  the account reported it" % badges.BADGE_ACCOUNT,
              "  %s  measured on this machine, a lower bound" % badges.BADGE_MEASURED,
              "  %s  unknown, or declared and not measured" % _BADGE_UNKNOWN,
              "  " + _moon_key(True),
              "", " DRILL-DOWN",
              "  d runs analyze · c runs chart · f runs fix · w rereads the logs",
              "", " ACCOUNT"]
    lines += ["  %-16s %s" % (label, value)
              for label, value in _account_facts(metrics, cap)]
    return lines


def help_visual(lines: list[str]) -> Text:
    """The overlay's plain rows with its section labels picked out.

    The same rule as the dashboard: colour is redundancy on top of a word that is
    already there, so a mono terminal reads the labels exactly as well and only
    loses the grouping at a glance.
    """
    text = Text()
    for index, line in enumerate(lines):
        if index:
            text.append("\n")
        start = len(text)
        text.append(line)
        spans = _line_spans(line)
        if line.strip() in HELP_SECTIONS:
            indent = len(line) - len(line.lstrip())
            spans.append((indent, indent + len(line.strip()),
                          "bold %s" % HELP_LABEL_COLOR))
        for begin, end, style in spans:
            text.stylize(style, start + begin, start + end)
    return text


class HelpScreen(ModalScreen):
    """The help overlay: one screen, opened and closed by `[?]` or `esc`.

    A Textual `ModalScreen` rather than an inline panel, because the dashboard's
    nineteen rows are a contract: an overlay that pushed rows around would move
    every number under it, and one that carved a column out of it would break the
    grid. This one covers the screen and hands it back exactly as it was - the
    widgets underneath are never touched, and closing it re-scans nothing.
    """

    BINDINGS = [
        Binding("escape", "close_help", "close"),
        Binding("question_mark", "close_help", "close"),
    ]

    CSS = """
    HelpScreen { align: center middle; }
    #help { width: 78; height: auto; max-height: 100%; background: $surface;
            border: round $accent; padding: 0 1; }
    #help-text { width: auto; }
    """

    def __init__(self, lines: list[str]) -> None:
        super().__init__()
        self._lines = lines

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help"):
            # markup=False: the rows carry bracketed keys and moon glyphs, and
            # none of it is Rich markup.
            yield Static(help_visual(self._lines), id="help-text", markup=False)

    def action_close_help(self) -> None:
        """Hand the screen back to the dashboard it covered."""
        self.dismiss()


# ---- the scan: the same collect() and analyse() cmd_analyze calls ----------
def previous_window_metrics(now: float, hours: float = WINDOW_HOURS) -> dict:
    """The equal-length window before this one, read and analysed like this one.

    The pairing is baseline.previous_window's own - the window that ends
    exactly where this one starts, with no gap and no overlap - because the
    delta column and baseline.change_verdict(read here, previous) have to
    compare the same two windows the CLI's `fix` compares. It is one more read
    of the same local logs and opens no socket: `previous` is a measurement,
    never a fetch.
    """
    _start, end = baseline.previous_window(now, hours)
    reqs = cli.collect(hours, list(SCAN_HARNESSES), None, None, now=end)
    return cli.analyse(reqs, hours)


def scan(plan: str | None = None, hours: float = WINDOW_HOURS) -> tuple:
    """One scan of local logs plus the window before it and the cached capacity.

    collect() and analyse() are the CLI's own entry points, so the screen
    re-slices the same metrics `analyze` renders and never re-scans differently.
    The enrichment after them is the part of cmd_analyze the verdict reads: the
    declared plan, the account's cached capacity block, and the pressure-window
    count re-counted against the resolved width - without that re-count the
    verdict would measure "wide" against the tool's fallback constant and then
    tell a reader to fetch a width the account panel already shows.

    `previous` is the equal-length window immediately before this one, built by
    previous_window_metrics. It is what the delta column compares against, and
    only baseline.change_verdict turns the pair into a word.

    The capacity cache is read twice on purpose: `_capacity_metrics` builds the
    CLI's own block (the shape cli.verdict reads), and fetch_capacity returns
    the Capacity payload the badges read. Both are cache-only; neither opens
    the network, and neither re-derives the other's facts.
    """
    now = time.time()
    reqs = cli.collect(hours, list(SCAN_HARNESSES), None, None, now=now)
    metrics = cli.analyse(reqs, hours)
    metrics["declared_plan"] = plan
    # The sparkline re-bins by_minute across the window, so the window needs a
    # close it can trust: this scan's own clock, not the last active minute.
    metrics["scanned_at"] = now
    previous = previous_window_metrics(now, hours)
    try:
        metrics["peak_concurrency_priority"] = cli.max_concurrency(
            [r for r in cli.timed(reqs) if getattr(r, "tier", "") == "interactive"])[0]
    except Exception:
        metrics["peak_concurrency_priority"] = metrics.get("peak_concurrency")
    try:
        metrics["capacity"] = cli._capacity_metrics(
            now - hours * 3600, now, metrics, allow_network=False)
    except Exception as e:
        metrics["capacity"] = {
            "provenance": sources.PROV_UNKNOWN,
            "reason": f"collection failed: {type(e).__name__}: {e}"[:200]}
    width, _source = cli._priority_for_advice(metrics)
    if (isinstance(width, int) and not isinstance(width, bool) and width > 0
            and metrics.get("requests")):
        try:
            wide = cli.pressure_windows(cli._priority_tier(reqs), width)
            metrics["pressure_windows"] = len(wide)
            metrics["longest_pressure_window"] = (
                max(wide, key=lambda w: w["seconds"]) if wide else None)
            metrics["pressure_window_threshold"] = width
        except Exception:
            pass
    digest = slowest_request_digest(reqs, metrics)
    if digest is not None:
        metrics["slowest_request"] = digest
    return metrics, previous, sources.fetch_capacity(allow_network=False)


def slowest_request_digest(reqs, metrics: dict):
    """One bounded digest of the slowest request, from explain's own machinery.

    "Agent seems stuck" reaches the explain --slowest panel too, so the screen
    carries the slowest request's own why in one row. It calls explain.slowest
    and explain.explain rather than re-deriving a reason from the metrics, and
    it returns None when there is nothing to rank. It never raises: the screen
    loses one row, not the verdict.
    """
    if not reqs:
        return None
    try:
        slowest = explain.slowest(reqs, 1)[0]
        served = metrics.get("capacity") or {}
        if served.get("provenance") != sources.PROV_ACCOUNT:
            served = None
        doc = explain.explain(slowest, reqs, served, cli._priority_for_advice(metrics))
    except Exception:
        return None
    return {
        "duration": dict(doc["rows"]).get("duration", "unknown"),
        "outcome": doc.get("outcome"),
        "headline": doc.get("headline", ""),
    }


def _captured_main(argv: list[str]) -> tuple[int, str]:
    """Run one CLI command in-process with both streams captured.

    The screen owns the terminal while the command runs, so a notice, a report
    or a usage error has to come back as text. A SystemExit from argparse is
    that text too, with its own code: a bad flag is output on this screen,
    never a crash. Returns (exit code, captured text).
    """
    buf = io.StringIO()
    code = 0
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = cli.main(argv) or 0
    except SystemExit as e:      # a usage error exiting 2 is output on this screen, not a crash
        code = e.code if isinstance(e.code, int) else 2
    return code, buf.getvalue()


def _last_line(text: str) -> str:
    """The last non-blank line of captured CLI output, or ""."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


# analyze answers with 0 (clean), 1 (over capacity) or 2 (nothing measured),
# and all three print the report the screen shows. Only a capture that is not
# one of those answers, or that holds nothing but the tool naming its own
# error, is a command that failed.
_ANSWER_CODES = (0, 1, 2)
_ERROR_PREFIXES = ("luna-doctor:", "usage:")


def carries_report(code: int, text: str) -> bool:
    """Did the command print a report, or only its own error line?

    An exit code outside those answers is no report. So is a capture whose
    every non-blank line is the tool naming a usage or input error: that is a
    failed command, and a screen that announced it as a report would be
    telling a reader to read something that is not there. An empty capture is
    an empty report, which the panel says in its own words.
    """
    if code not in _ANSWER_CODES:
        return False
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return True
    return any(not line.startswith(_ERROR_PREFIXES) for line in lines)


def fix_output(plan: str | None = None, hours: float = WINDOW_HOURS) -> str:
    """The `fix` command's own output, cache-only, as text for the screen.

    It runs `fix` instead of re-wiring recommendations: the fix block owns its
    printing, and a re-render here would be a second copy that could drift.
    `--offline` keeps the promise that the TUI opens no new network behavior.
    The window is the scan's own, so the advice is about the rows on screen.
    """
    argv = ["fix", "--since", f"{hours:g}h", "--offline"]
    if plan:
        argv += ["--plan", plan]
    return _captured_main(argv)[1]


def detail_output(plan: str | None = None,
                  hours: float = WINDOW_HOURS) -> tuple[int, str]:
    """The `analyze` report's own text, cache-only and read-only.

    `[d]` shows the report the verdict was cut from, so it runs the command
    instead of re-rendering the metrics: one report, one wording, and the
    screen cannot invent a detail the CLI does not print. The window is the
    scan's own. `--offline` keeps the promise that the TUI opens no new network
    behavior, and `--no-store` keeps it read-only: a screen that a person opens
    and closes must not write a run extraction into the store behind them.

    Returns (exit code, captured text), so a report that failed is never
    announced as a report that arrived.
    """
    argv = ["analyze", "--since", f"{hours:g}h", "--offline", "--no-store"]
    if plan:
        argv += ["--plan", plan]
    return _captured_main(argv)


def chart_output(plan: str | None = None,
                 hours: float = WINDOW_HOURS) -> tuple[str, str | None, str]:
    """Draw this window's chart, then open the picture in the system viewer.

    The chart is the `chart` command's own work, so the screen asks for it
    instead of drawing a second one: same window, same scene, same file. Both
    streams are captured, so the CLI's notices and any usage error stay text on
    this screen instead of painting over it. `--offline` keeps the promise that
    the TUI opens no new network behavior, and `--out` names the file, so the
    screen can say where the chart went without reading the CLI's mind.

    The chart goes into a directory this press makes: the screen must not
    overwrite the stable SVG that a bare run leaves in `~/.luna-doctor/last`,
    and the viewer needs the file to outlive the keypress. The PNG is opened
    only when it was written, because a terminal cannot show a picture.

    A path this press asked for is not a file. The command's exit code and the
    file on disk are the evidence that a chart was written, so a run that
    failed reports its own last line back instead of a path nobody can open.
    Returns (svg path, png path or None, the failure reason or "").
    """
    folder = tempfile.mkdtemp(prefix="luna-doctor-chart-")
    svg_path = os.path.join(folder, "chart.svg")
    png_path = os.path.join(folder, "chart.png")
    argv = ["chart", "--since", f"{hours:g}h", "--offline", "--out", svg_path]
    if plan:
        argv += ["--plan", plan]
    code, text = _captured_main(argv)
    if code != 0 or not os.path.exists(svg_path):
        return svg_path, None, _last_line(text) or f"the chart command exited {code}"
    png = png_path if os.path.exists(png_path) else None
    if png:
        cli._open_png_quietly(png)
    return svg_path, png, ""


# ---- the screen -----------------------------------------------------------
class TriageApp(App):
    """The dashboard, mounted once the scan lands: nineteen rows, five states.

    The scan and the cache-only capacity read run in a thread worker while the
    screen says it is reading, so a person is never waiting on a blank terminal.
    The dashboard fills the moment the result arrives, with the group the current
    answer selects: the six answers choose which three evidence rows stand under
    the picture, and they never withhold a number (§8 item 6). The key bindings
    live in the `Footer`, which renders `BINDINGS` and is the nineteenth row.

    The app opens no network behavior of its own: run_triage(online=True)
    refreshes the cache once before the screen opens, exactly like `--online` on
    the CLI.
    """

    TITLE = "luna-doctor triage"

    CSS = """
    Screen { background: $surface; }
    #ask { height: 1fr; }
    #question { padding: 1 2 0 2; text-style: bold; }
    #choices { margin: 1 2 0 2; height: auto; max-height: 10; border: round $primary; }
    #status { padding: 0 2; color: $text-muted; height: auto; }
    #dashboard { display: none; height: 1fr; }
    #detail { display: none; border: round $secondary; padding: 0 1; }
    """

    BINDINGS = [
        Binding("f", "show_fix", "fix"),
        Binding("d", "show_detail", "detail"),
        Binding("c", "show_chart", "chart"),
        Binding("g", "cycle_group", "group"),
        Binding("w", "cycle_window", "window"),
        Binding("question_mark", "show_help", "help"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, plan: str | None = None, online: bool = False) -> None:
        super().__init__()
        self.plan = plan
        self.online = online
        self.window_hours: int = WINDOW_HOURS
        self.metrics: dict | None = None
        self.previous: dict | None = None
        self.capacity = None
        self.choice: str = DEFAULT_CHOICE
        self.scan_failed: str | None = None
        # The eighteen rows as last drawn, so the one-second age tick can tell
        # whether anything on the screen actually changed before repainting.
        self.drawn: list[str] = []

    def compose(self) -> ComposeResult:
        # The ask and the dashboard occupy the same region, so the Footer under
        # them never moves when the scan lands: a layout that shifts between the
        # question and the answer would redraw the row a person is pressing.
        with Vertical(id="ask"):
            yield Static("What are you seeing?", id="question")
            yield ListView(
                *[ListItem(Label(text), id=key) for text, key in TRIAGE_CHOICES],
                id="choices")
        # markup=False: this line repeats the CLI's words, and those name keys
        # as [f] and [w]. As Rich markup, the brackets and what they hold vanish.
        yield Static("", id="status", markup=False)
        # markup=False for the same reason: the dashboard's command row names
        # the key that runs it as [f], and a bracketed word is not markup here.
        yield Static("", id="dashboard", markup=False)
        with VerticalScroll(id="detail"):
            # markup=False: this panel prints a command's own words, and the
            # fix block may name a tier as [agents].
            yield Static("", id="detail-text", markup=False)
        # show_command_palette=False: the footer's job here is the five keys
        # this screen binds, and Textual's palette hint would push the row past
        # the 78-cell budget the dashboard is drawn to.
        yield Footer(show_command_palette=False)

    def on_mount(self) -> None:
        self._set_status(f"scanning local logs… (last {self.window_hours}h)")
        self.query_one("#choices", ListView).focus()
        # One tick a second, and it re-reads nothing: the scan's clock is fixed,
        # so the only cell that can move is the age in the header (and the `now`
        # row's own freshness, which ages with it). No disk read, no network.
        self.set_interval(1.0, self._age_tick)
        self._scan_worker()

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    # -- the window -------------------------------------------------------
    def action_cycle_window(self) -> None:
        """Move to the next window and read it: 1h -> 6h -> 24h -> 1h.

        A window is not a relabel. The rows, the deltas and the picture all
        come out of the logs, so a new window is a new scan; until it lands the
        status says it is reading. The old result stays until then, so a
        keypress never leaves a panel half-filled.
        """
        self.window_hours = window_cycle(self.window_hours)
        self._set_status(f"scanning local logs… (last {self.window_hours}h)")
        self._scan_worker()

    # -- the scan ---------------------------------------------------------
    @work(thread=True)
    def _scan_worker(self) -> None:
        hours = self.window_hours
        try:
            metrics, previous, cap = scan(self.plan, hours)
        except Exception as e:
            self.call_from_thread(self._scan_failed, f"{type(e).__name__}: {e}")
            return
        self.call_from_thread(self._scan_done, metrics, previous, cap)

    def _scan_failed(self, reason: str) -> None:
        self.scan_failed = reason
        self._set_status(f"the scan failed ({reason}); there is no dashboard to show")

    def _scan_done(self, metrics: dict, previous: dict | None, cap) -> None:
        self.previous = previous
        self.capacity = cap
        self._show_dashboard(metrics)
        self.call_after_refresh(self._land, metrics)

    def _land(self, metrics: dict) -> None:
        """Publish the scan the screen has drawn, once that frame is behind us.

        `self.metrics` is what the keys and the suite read to know the dashboard
        is ready, so it is set after the frame that drew it rather than in the
        middle of the frame: a reader then never sees a scan the screen has not
        shown, and the Footer's row is composed before anyone asks for it.
        """
        self.metrics = metrics

    # -- the dashboard ----------------------------------------------------
    def _show_dashboard(self, metrics: dict) -> None:
        """Draw the whole screen: the eighteen rows, and no question above them.

        The dashboard is up the moment the scan lands, holding the group the
        current answer selects. The answer list then stands down: nineteen rows
        are the contract, and a list of answers beside them would move every row
        under it. The status line is cleared with it - the scan status was a
        restatement of the header, and the header is on the screen now.
        """
        lines = dashboard_lines(self.choice, metrics, self.previous,
                                self.capacity)
        self.drawn = lines
        self.query_one("#dashboard", Static).update(styled_lines(lines))
        # The ask stands down to no rows instead of `display: none`: hiding the
        # focused list blurs it, and Textual then rebuilds the Footer a frame
        # later - a frame in which the screen is one row short of its nineteen.
        self.query_one("#ask").styles.height = 0
        self.query_one("#dashboard").display = True
        self._set_status("")

    def _age_tick(self) -> None:
        """Keep the screen's two clocks honest, once a second.

        The rows are rebuilt from the scan the screen is already showing, dated
        against the wall clock as it is now, and repainted only when that changed
        anything: a scan is never re-read, and a second in which the age still
        reads the same costs one comparison. That is the whole difference between
        a dashboard and a screenshot.
        """
        if self.metrics is None or not self.drawn:
            return
        lines = dashboard_lines(self.choice, self.metrics, self.previous,
                                self.capacity, wall_now=time.time())
        if lines == self.drawn:
            return
        self.drawn = lines
        self.query_one("#dashboard", Static).update(styled_lines(lines))

    # -- the help overlay --------------------------------------------------
    def action_show_help(self) -> None:
        """Open the overlay: every key, every mark, and the account it reads.

        One key both ways. On the dashboard `[?]` pushes `HelpScreen`, and on the
        overlay the same key is that screen's own binding, so it closes. The
        dashboard underneath is untouched: closing re-scans nothing and the group
        on screen is the group that comes back.
        """
        self.push_screen(HelpScreen(help_lines(self.metrics, self.capacity)))

    # -- the answer, and the group ring -----------------------------------
    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """The answer swaps the evidence group and NEXT, and nothing above them."""
        key = event.item.id
        if key is not None:
            self._select(key)

    def action_cycle_group(self) -> None:
        """Show the next evidence group: latency -> refusals -> stalls -> latency.

        `[g]` is for the person who wants the other three rows without answering
        the question again. It selects the answer that names the group - the list
        still holds the same six answers - and redraws, so the title, the three
        rows and NEXT move and nothing else on the screen does.
        """
        self._select(_ANSWER_FOR_GROUP[next_group(pick_lead(self.choice))])

    def _select(self, choice_key: str) -> None:
        """Point the dashboard at one answer's group, redrawing it if it can.

        An answer given before the scan lands is kept: the dashboard fills with
        that group the moment the result arrives, so the answer never has to be
        given twice.
        """
        self.choice = choice_key
        if self.scan_failed is not None:
            self._set_status(f"the scan failed ({self.scan_failed}); "
                             f"there is no dashboard to show")
            return
        if self.metrics is None:
            self._set_status("the scan is still running; the dashboard appears "
                             "when it finishes")
            return
        self._show_dashboard(self.metrics)

    # -- the detail panel: the analyze report, or the fix output ----------
    def _show_detail(self, command: str, subject: str, code: int, text: str) -> None:
        """Fill the one detail panel with a command's own words, and say so.

        A command that printed no report is not a report on screen: the panel
        keeps what it held and the status names that command's own last line
        instead. `analyze` answering 1 or 2 still printed its report, so those
        read as the report they are.
        """
        if not carries_report(code, text):
            reason = _last_line(text) or f"exit {code}"
            self._set_status(f"the {command} command failed: {reason}")
            return
        self.query_one("#detail-text", Static).update(text or f"the {subject} was empty")
        self.query_one("#detail").display = True
        self._set_status(f"the {subject} is on screen")

    def action_show_detail(self) -> None:
        """Show the whole `analyze` report the verdict was cut from."""
        if self.scan_failed is not None:
            self._set_status(f"the scan failed ({self.scan_failed}); the report needs a scan")
            return
        if self.metrics is None:
            self._set_status("the scan is still running; the report needs it")
            return
        self._set_status("rendering the analyze report (cache only)…")
        self._detail_worker(int(self.metrics.get("window_hours") or WINDOW_HOURS))

    @work(thread=True)
    def _detail_worker(self, hours: int) -> None:
        code, text = detail_output(self.plan, hours)
        self.call_from_thread(self._show_detail, "analyze", "analyze report", code, text)

    # -- the fix, in place, cache only ------------------------------------
    def action_show_fix(self) -> None:
        if self.scan_failed is not None:
            self._set_status(f"the scan failed ({self.scan_failed}); the fix output needs a scan")
            return
        if self.metrics is None:
            self._set_status("the scan is still running; the fix output needs it")
            return
        self._set_status("rendering the fix output (cache only)…")
        self._fix_worker()

    @work(thread=True)
    def _fix_worker(self) -> None:
        # fix_output hands back its text alone, so the code is the clean one;
        # what the capture holds still decides whether it was a report.
        hours = int((self.metrics or {}).get("window_hours") or WINDOW_HOURS)
        self.call_from_thread(self._show_detail, "fix", "fix output", 0,
                              fix_output(self.plan, hours))

    # -- the chart, in place, cache only ----------------------------------
    def action_show_chart(self) -> None:
        """Draw this window's chart and open the picture beside it."""
        if self.scan_failed is not None:
            self._set_status(f"the scan failed ({self.scan_failed}); the chart needs a scan")
            return
        if self.metrics is None:
            self._set_status("the scan is still running; the chart needs it")
            return
        self._set_status("drawing the chart (cache only)…")
        self._chart_worker(int(self.metrics.get("window_hours") or WINDOW_HOURS))

    @work(thread=True)
    def _chart_worker(self, hours: int) -> None:
        svg_path, png_path, error = chart_output(self.plan, hours)
        self.call_from_thread(self._chart_done, svg_path, png_path, error)

    def _chart_done(self, svg_path: str, png_path: str | None, error: str) -> None:
        if error:
            self._set_status(f"the chart failed: {error}")
            return
        written = png_path or svg_path
        opened = " · the PNG is open" if png_path else ""
        self._set_status(f"chart written: {written}{opened}")


def run_triage(plan: str | None = None, online: bool = False) -> int:
    """Open the triage screen and return 0.

    `online` is the bare run's `--online`: one capacity fetch refreshes the
    cache before the screen opens, and the screen itself reads only the cache.
    A failed fetch is never an error - the account panel then says the number
    is unknown. Without Textual installed this module fails to import, and the
    caller falls back to the linear path.
    """
    if online:
        # The refresh is a network round trip that can take seconds, and it
        # happens before the screen exists: say what it is doing first, so a
        # quiet terminal does not read as a hung app.
        cli.notice("refreshing the account's capacity cache before the screen opens")
        try:
            sources.fetch_capacity(allow_network=True)
        except Exception:
            pass
    TriageApp(plan=plan, online=online).run()
    return 0
