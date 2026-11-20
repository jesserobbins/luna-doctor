"""The triage screen: one question, one hour, one provenance-badged verdict.

Bare `luna-doctor` on a terminal opens this instead of argparse help: it asks
what the person is seeing, scans the last hour of local logs and the cached
account capacity while the question is on screen, and lands on one verdict with
the next action. `[w]` cycles the window 1h -> 6h -> 24h, and every number and
the picture are re-read for the window it lands on.

Everything that decides what a reader sees - TRIAGE_CHOICES, pick_lead, the
three row builders and build_verdict_rows - is a pure function of a metrics
dict, a Capacity payload and the chosen answer, so the suite can test it
offline and without a terminal. Only TriageApp needs Textual, which is the
optional `luna-doctor[tui]` extra: this module imports it at the top, and the
caller (cli.main) guards the import so a machine without the extra falls back
to the linear path.

The panels are in ranked-trust order: what the account reports first (●), then
what this machine's logs show (○), then the verdict. Every number carries its
badge, and one legend line names all three.

The screen is an orientation dashboard, not a report: no panel holds more than
four rows, one sparkline carries the window's shape, and the detail stays in
`analyze`, `explain`, `capacity` and `chart`, which the screen shells out to.
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
from textual.containers import VerticalScroll
from textual.widgets import Label, ListItem, ListView, Static

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

PANEL_ACCOUNT = "What the account reports (authoritative)"
PANEL_LOCAL = "What this machine's logs show (informative, a lower bound)"
PANEL_VERDICT = "Verdict"

# The six on-screen answers, in the order the design draws them.
TRIAGE_CHOICES: list[tuple[str, str]] = [
    ("Requests failing or hanging", "failing"),
    ("Things are slow", "slow"),
    ('"Stream ended without finish_reason"', "finish_reason"),
    ("429 / concurrency errors", "concurrency"),
    ("Agent seems stuck mid-task", "stuck"),
    ("Nothing yet, just checking", "checking"),
]

# Which evidence leads the verdict for each answer. "admission" is the admission
# and saturation panel, "latency" the latency-and-queue-wait panel, "stuck" the
# idle-budget diagnosis, and "overview" the plain traffic summary.
_LEAD = {
    "failing": "admission",
    "slow": "latency",
    "finish_reason": "stuck",
    "concurrency": "admission",
    "stuck": "stuck",
    "checking": "overview",
}

_BADGE_UNKNOWN = badges.badge_for(sources.PROV_UNKNOWN)
_BADGE_MEASURED = badges.badge_for(sources.PROV_MEASURED)

# The status moons and their legend, through the one gate that owns glyphs: a
# screen is not a byte stream, and Textual draws UTF-8, so the screen asks for
# the emoji form - the same glyphs `--style pretty` prints.
_GLYPH_STYLE = Style(color=False, emoji=True, motion=False)


def pick_lead(choice_key: str) -> str:
    """The lead panel id for one on-screen answer."""
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


def _count(value):
    """An int count, or None. A bool is not a count and a float is not a count."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _sec(value) -> str:
    return "unknown" if value is None else f"{value}s"


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'\u2018\u201c])")


def first_sentence(text: str) -> str:
    """The first sentence of a piece of CLI prose, stripped.

    A sentence ends at a full stop, a question mark or an exclamation mark
    followed by whitespace and something that starts a sentence. The screen
    keeps one sentence of the verdict's prose, so this is the seam that
    decides where the report takes over.
    """
    return _SENTENCE_END.split((text or "").strip(), maxsplit=1)[0].strip()


# ---- the header: the conclusion, its direction, and the width -------------
#
# Row one answers the first two questions in the order a person under pressure
# asks them: how bad is it (a status moon and its word, read from the CLI's own
# verdict) and is it getting worse (a trend word and its arrow, read from the
# previous equal-length window). Everything after that is the window, the volume,
# the plan and the lane count the verdict was judged against, with the scan's own
# clock right-aligned at the same edge the rules stop at.
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


def header_line(metrics: dict, previous, cap, window_hours) -> str:
    """The dashboard's first row: state, direction, window, volume, width, clock.

    `🌑 OVER CAPACITY · worse ▲ · 1h · 42 req · apex · ●12 lanes    14:32Z`. Six
    cells and a right-aligned clock, one cell per fact a person in an incident
    needs before the rows: how bad (the moon's word), which way (the trend word),
    what window this is, how much traffic is in it, whose plan the numbers are,
    and how wide the lane count is.

    The width comes from `resolved_lanes`, the same call the LOAD region and the
    picture's lane scale make, so `●12 lanes` in the header and the wall in the
    caption cannot drift. The clock is the scan's own reading, so a header is a
    pure function of its arguments: a metrics dict with no clock loses the
    timestamp rather than taking a second reading of the wall clock. A row is
    never truncated to fit the budget (§3.2) - a shortened status word would be a
    wrong answer - so the line runs long instead; the tests hold the real windows
    under 78 cells.

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
    stamp = _clock_cell(metrics)
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


def _clock_cell(metrics: dict) -> str:
    """`HH:MMZ` for the scan's own clock, "" when the scan carried none.

    A dashboard that does not say when it was taken is a dashboard a reader has
    to guess about, and a microsecond stamp is a number nobody can act on; the
    header prints the minute, in UTC, of the moment the scan closed.
    """
    stamp = metrics.get("scanned_at")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or stamp <= 0:
        return ""
    return datetime.fromtimestamp(float(stamp), timezone.utc).strftime("%H:%MZ")


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

# What the rows have to say, as closed sets of labels. `blocked` is the darkest,
# because those requests never got in at all; a 429, an at-limit refusal or a
# demotion to the slow tier is pressure (§4.2).
_BLOCK_LABELS = ("blocked",)
_PRESSURE_LABELS = ("429", "at limit", "background", "stalled", "starved")
_LATENCY_LABELS = ("p50", "p90")

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
    if _region_pressure(evidence, region_kind):
        return moon_glyph(report.MOON_QUARTER)
    return moon_glyph(report.MOON_FULL)


def _region_pressure(evidence, region_kind: str) -> bool:
    """Whether a region's own rows say pressure, for this kind of region.

    A refusal count above zero is pressure in any region. A load region adds the
    qualifier its peak wears: `over` means the wall was crossed and `lane count
    unknown` means there is no wall to measure against, so neither can read clean,
    while `never over` is a clean window whose peak happened to move (§2.2). A
    latency region adds its own direction, because a percentile has no wall to
    cross and the move is the whole story (§2.1: p90 240s rising reads pressure;
    §2.2: p90 58s falling reads clean).
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
_SENTENCE_STOP = re.compile(r"[.!]")
_FIELD_PAREN = re.compile(r"\([^)]*[`_][^)]*\)")


def dashboard_ok_row(text: str) -> bool:
    """Whether one row obeys the copy rules and the width budget (§3.2, §7).

    - No sentence-final stop: a full stop or an exclamation mark means a sentence
      arrived, and a row is not a sentence. `?` is allowed where it is the unknown
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


# ---- the account panel: what the account reports (●) -----------------------
def account_rows(metrics: dict, cap) -> list[tuple[str, str]]:
    """The account's own numbers, one row per fact, badged ●.

    Every row is read from the Capacity payload with its nulls intact: a null
    is unknown and gets the "?" badge with its reason, never a zero. The
    priority pool's occupancy is qualified by its ledger, so a count served
    beside a ledger that is not "up" is advisory and never wears the
    authoritative badge. The two overflow-pool null shapes stay apart: a cap
    that could not be read is unknown, a zero cap is a closed pool. Refusal
    words appear only where the admission counts put them.

    A plan the person declared still gets its row when no payload is cached:
    --plan exists so the ◌ declared width can appear at the front door, and a
    missing payload is no reason to hide a declaration the reader typed.
    """
    if cap is None:
        rows = [(_BADGE_UNKNOWN,
                 "the account did not answer: no capacity payload is cached "
                 "(`luna-doctor capacity --online` fetches one)")]
        if (metrics or {}).get("declared_plan"):
            rows.append(_width_row(metrics, None))
        return rows
    rows = [_width_row(metrics, cap)]
    rows.extend(_occupancy_rows(cap))
    rows.extend(_overflow_rows(cap))
    rows.extend(_admission_rows(cap))
    return rows


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


def _width_row(metrics: dict, cap) -> tuple[str, str]:
    """The priority concurrency the verdict is judged against, and whose it is.

    attribution.resolve_priority_concurrency decides account first, then a
    --plan declaration, and this row wears the badge that decision reports:
    ● when the account said the number, ◌ when a person declared it.
    """
    value, source = resolved_lanes(metrics, cap)
    if value is None:
        return (_BADGE_UNKNOWN,
                "priority concurrency not reported by the account, and no --plan "
                "declared (unknown)")
    where = "" if source == "account" else f" ({source})"
    return (badges.badge_for(source),
            f"priority concurrency {value}, {badges.badge_help(source)}{where}")


def _occupancy_rows(cap) -> list[tuple[str, str]]:
    """The priority pool's live occupancy, with the ledger and sample behind it.

    The flat `now` fields cover the priority pool only. The occupancy is
    qualified by the ledger: when the ledger is not "up", the count is
    advisory and the row says it may be stale or missing, because a pool line
    that looks authoritative over an unhealthy ledger is a claim nobody
    measured. The ledger and the sample time share this row: the dashboard
    orients, and `capacity` prints the whole block.
    """
    now = cap.now
    if now is None:
        return [(_BADGE_UNKNOWN, "live occupancy not reported by the account (unknown)")]
    in_use, limit, available = (cli._cap_field(now, "in_use"),
                                cli._cap_field(now, "limit"),
                                cli._cap_field(now, "available"))
    if in_use is None and limit is None:
        occupancy = "occupancy unknown (not reported)"
    elif in_use is None:
        occupancy = f"in use unknown (not reported) · limit {limit}"
    elif limit is None:
        occupancy = f"in use {in_use} · limit unknown (not reported)"
    else:
        occupancy = f"in use {in_use} of {limit} priority concurrent requests"
    if available is not None:
        occupancy += f" · {available} available"
    ledger = cli._cap_field(now, "ledger")
    sampled = cli._cap_field(now, "sampled_at")
    saturated = cli._cap_field(now, "saturated")
    if saturated is not None:
        occupancy += f" · saturated: {cli._cap_flag(saturated)}"
    if ledger == "up":
        # A row that prints an unread count or an unread sample time is not a
        # line the account reported, so it must not wear the account's badge.
        known = in_use is not None and sampled is not None
        badge = (badges.badge_for(sources.PROV_ACCOUNT) if known else _BADGE_UNKNOWN)
        text = (f"priority pool {occupancy} · ledger up, sampled at "
                f"{cli._cap_unknown(sampled)}")
    else:
        badge = _BADGE_UNKNOWN
        text = (f"priority pool {occupancy}, but the ledger is "
                f"{cli._cap_unknown(ledger)}, so the count may be stale or "
                f"missing · sampled at {cli._cap_unknown(sampled)}")
    if getattr(cap, "stale", False):
        text += "; the cache is past its time to live, so run `luna-doctor capacity --online`"
    return [(badge, text)]


def _overflow_rows(cap) -> list[tuple[str, str]]:
    """The overflow pool, as its own row: overflow traffic never appears in the
    priority pool's in_use.

    The wording is the capacity report's own (`cli._cap_pool_now`), so both
    surfaces name the same shapes with the same words. An absent block is
    unknown, not a closed pool. The two null shapes are different facts and are
    rendered differently: `limit: null` is a cap that could not be read,
    `limit: 0` is a closed pool the account is entitled to no overflow on, which
    is never saturated. The saturated flag is reported as an observation
    ("saturated: yes"), never as a refusal.

    The authoritative badge follows the same rule as the priority pool's
    occupancy row: a cap beside a count nobody could read is not a line the
    account reported. A closed pool rests on its cap alone, so it keeps ●.
    """
    pool = cli._cap_field(getattr(cap, "now", None), "background")
    text = "overflow pool: " + cli._cap_pool_now(pool)
    limit = cli._cap_field(pool, "limit")
    if limit == 0 and not isinstance(limit, bool):
        return [(badges.badge_for(sources.PROV_ACCOUNT), text)]
    if pool is not None:
        text += f" · saturated: {cli._cap_flag(cli._cap_field(pool, 'saturated'))}"
    read = limit is not None and cli._cap_field(pool, "in_use") is not None
    badge = badges.badge_for(sources.PROV_ACCOUNT) if read else _BADGE_UNKNOWN
    return [(badge, text)]


# The admission counts as the report's prose names them: the API field name is
# a field name, and "denied" here is the same class the CLI calls a block.
_ADMISSION_CLASSES = (
    ("denied_after_wait", "denied after waiting"),
    ("denied", "blocked at admission"),
    ("deferred", "deferred"),
)


def _admission_rows(cap) -> list[tuple[str, str]]:
    """The admission record over complete UTC days, beside the days it covers.

    The counts are the API's own; the wording is the capacity report's ("denied
    after waiting", "blocked at admission"), so the same class reads the same
    way on both surfaces, and the days a count happened on are named where the
    list stays short. The in-progress day is never included by the API.
    """
    days = sources.capacity_admission_days(cap)
    if not days:
        return [(_BADGE_UNKNOWN,
                 "the account's admission record is not available (unknown)")]
    totals: dict[str, int] = {}
    for _day, entry in days:
        for name, _phrase in _ADMISSION_CLASSES:
            value = _count(entry.get(name))
            if value:
                totals[name] = totals.get(name, 0) + value
    window = f"admission last {len(days)} complete UTC day(s) through {days[-1][0]}"
    if not totals:
        return [(badges.badge_for(sources.PROV_ACCOUNT),
                 f"{window}: no request was blocked at admission or after waiting")]
    bits = []
    for name, phrase in _ADMISSION_CLASSES:
        if not totals.get(name):
            continue
        bit = f"{phrase} {totals[name]}"
        if name == "denied_after_wait":
            named = [day for day, entry in days if _count(entry.get(name))]
            if 0 < len(named) <= 4:
                bit += f" on {', '.join(named)}"
        bits.append(bit)
    return [(badges.badge_for(sources.PROV_ACCOUNT),
             f"{window}: " + " · ".join(bits))]


# ---- the local panel: what this machine's logs show (○) --------------------
def local_rows(choice_key: str, metrics: dict) -> list[tuple[str, str]]:
    """What this machine's logs show, badged ○: informative, a lower bound.

    Every count here is a measurement of these logs, so other machines are
    invisible and the peak is a floor, not a total. Nothing sums a measured
    count with a configured width. The answer picks which evidence follows the
    peak (pick_lead), so the classes the person named come first.
    """
    rows = []
    peak = _count(metrics.get("peak_concurrency"))
    if peak is None:
        rows.append((_BADGE_MEASURED,
                     "peak concurrency not measured in this window"))
    else:
        rows.append((_BADGE_MEASURED,
                     f"peak {peak} concurrent, {badges.badge_help(sources.PROV_MEASURED)} "
                     f"- other machines are invisible to these logs"))
    lead = pick_lead(choice_key)
    if lead == "latency":
        rows.extend(_latency_rows(metrics))
    elif lead == "stuck":
        rows.extend(_stuck_rows(metrics))
    elif lead == "admission":
        rows.extend(_admission_evidence_rows(metrics))
    else:
        rows.extend(_overview_rows(metrics))
    return rows


def _outcome_rows(metrics: dict, classes) -> list[tuple[str, str]]:
    """One row naming every outcome class the answer is about that counted.

    The classes share the row: the dashboard orients, and `analyze` still
    prints each class on its own line with its own count.
    """
    by_outcome = metrics.get("by_outcome") or {}
    bits = []
    for name, phrase in classes:
        count = _count(by_outcome.get(name))
        if count:
            bits.append(f"{count} request(s) {phrase}")
    if not bits:
        return []
    return [(_BADGE_MEASURED, " · ".join(bits))]


_PRESSURE_CLASSES = (
    ("admission_timeout", "blocked at admission here (`scheduler admission timeout`)"),
    ("rate_limited", "rejected with a 429 (`rate_limit_error`)"),
    ("org_concurrency_limit", "rejected at the account's concurrent-request limit"),
    ("client_idle_timeout", "stalled until the harness's own idle budget killed them"),
)


def _admission_evidence_rows(metrics: dict) -> list[tuple[str, str]]:
    """The local admission and saturation evidence behind the admission lead."""
    rows = _outcome_rows(metrics, _PRESSURE_CLASSES)
    saturated = _count(metrics.get("saturated"))
    if saturated:
        rows.append((_BADGE_MEASURED,
                     f"{saturated} request(s) sat at the concurrency ceiling here"))
    background = _count((metrics.get("by_tier") or {}).get("background"))
    if background:
        rows.append((_BADGE_MEASURED,
                     f"{background} request(s) ran on the -background tier"))
    if not rows:
        rows.append((_BADGE_MEASURED,
                     "no request here was blocked at admission, rejected with a 429, "
                     "or demoted to the -background tier"))
    return rows


def _latency_rows(metrics: dict) -> list[tuple[str, str]]:
    """The latency answer: the account's queue wait, then the local latencies.

    The queue wait is the account's own admission measure, so it wears the
    account badge even though it answers a latency question; a null there means
    nobody waited that day, and cli._cap_wait says exactly that rather than a
    zero. The local numbers are completed requests only, and both tiers share
    one row so the panel stays short. The band row is the one shape a reader
    cannot get from the percentiles, so it keeps a row of its own.
    """
    rows = [_queue_wait_row(metrics)]
    tiers = []
    for tier in ("interactive", "background"):
        lat = (metrics.get("latency_s") or {}).get(tier) or {}
        if _count(lat.get("n")):
            tiers.append(f"{tier} latency n={lat['n']} p50={_sec(lat.get('p50'))} "
                         f"p90={_sec(lat.get('p90'))}")
    if tiers:
        rows.append((_BADGE_MEASURED, " · ".join(tiers)))
    bands = metrics.get("saturation_bands") or {}
    if bands.get("top_inflation_flagged"):
        top = (bands.get("bands") or [])[-1]
        rows.append((_BADGE_MEASURED,
                     f"p90 {_sec(top.get('p90'))} at concurrency {top.get('band')} "
                     f"against a mid-band p90 of {_sec(bands.get('mid_p90_baseline_s'))}"))
    if len(rows) == 1:
        rows.append((_BADGE_MEASURED,
                     "no completed request in this window, so no latency was measured"))
    return rows


def _queue_wait_row(metrics: dict) -> tuple[str, str]:
    """The account's queue wait on the most recent complete UTC day it reports.

    avg_queue_wait_ms is the account's own measure, so the row wears the account
    badge. A window with no complete day in the account's record still gets a
    row: the dimension is unknown, and a missing queue-wait line would read as
    "nobody waited".
    """
    days = (metrics.get("capacity") or {}).get("window_days") or []
    for day in reversed(days):
        if isinstance(day, dict) and "avg_queue_wait_ms" in day:
            return (badges.badge_for(sources.PROV_ACCOUNT),
                    f"queue wait, the account's most recent complete UTC day "
                    f"({day.get('day')}): {cli._cap_wait(day.get('avg_queue_wait_ms'))}")
    return (_BADGE_UNKNOWN,
            "queue wait: the account reports no complete UTC day inside this window "
            "(unknown)")


def _stuck_rows(metrics: dict) -> list[tuple[str, str]]:
    """The idle-budget diagnosis, plus one row from `explain --slowest`.

    "Agent seems stuck" binds to the client_idle_timeout class *and* to the
    explain --slowest panel (the builder-check decision), so one bounded row
    carries the slowest request's own why, and the whole panel stays in
    `luna-doctor explain`.
    """
    stalled = _count((metrics.get("by_outcome") or {}).get("client_idle_timeout"))
    if stalled:
        rows = [(_BADGE_MEASURED,
                 f"{stalled} request(s) stalled until the harness's own idle budget killed "
                 f"them (`client_idle_timeout`: the stream ended without a finish_reason)")]
    else:
        rows = [(_BADGE_MEASURED,
                 "no request in these logs ended on `client_idle_timeout`, so nothing "
                 "here shows the harness's idle budget stopping a stream")]
    slow = metrics.get("slowest_request") or {}
    if slow.get("headline"):
        rows.append((_BADGE_MEASURED,
                     f"the slowest request here took {slow.get('duration', 'unknown')} "
                     f"({slow.get('outcome', 'unknown')}): {slow['headline']}"))
    else:
        # The digest is None for more than one reason - an empty window, an
        # explain that raised, a request set with nothing to rank - and this
        # row cannot tell them apart. It states the one fact it has: no request
        # was ranked, and why is not established here.
        rows.append((_BADGE_MEASURED,
                     "the slowest request here is not established (unknown): this "
                     "scan ranked none, and it recorded no reason why"))
    return rows


def _overview_rows(metrics: dict) -> list[tuple[str, str]]:
    """The plain traffic summary, for "nothing yet, just checking"."""
    hours = int(metrics.get("window_hours") or WINDOW_HOURS)
    n = _count(metrics.get("requests")) or 0
    by_harness = metrics.get("by_harness") or {}
    harnesses = ", ".join(f"{name} {count}" for name, count in sorted(by_harness.items()))
    text = f"{n} request(s) in the last {hours}h"
    if harnesses:
        text += f" ({harnesses})"
    rows = [(_BADGE_MEASURED, text)]
    by_tier = metrics.get("by_tier") or {}
    if by_tier:
        rows.append((_BADGE_MEASURED,
                     "tiers: " + ", ".join(f"{name} {count}"
                                           for name, count in sorted(by_tier.items()))))
    return rows


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
                  lanes: int | None = None) -> str:
    """The picture's one-line key: which scale it is, and when the peak was.

    A lane-scaled picture states what `█` means and converts the count of `█`
    cells into its unit - minutes over the lane count - so the row is
    quantitatively readable and not merely suggestive. A peak-scaled picture
    says so instead, and names the peak's clock either way. A window with no
    active minute has no picture, and an empty picture has no caption.
    """
    picture = _window_picture(metrics, width)
    if picture is None:
        return ""
    peak_at = ""
    if picture.peak_at is not None:
        peak_at = datetime.fromtimestamp(picture.peak_at, timezone.utc).strftime(
            "%H:%MZ")
    if lanes is None:
        return f"scale: this window · peak at {peak_at}"
    over = sum(1 for value in picture.columns if value >= lanes)
    minutes_over = int(round(over * picture.minutes_per_column))
    return (f"{_SPARK_BLOCKS[-1]} = at or over {badges.BADGE_ACCOUNT}{lanes} lanes"
            f" · over {minutes_over}m of {picture.total_minutes}m"
            f" · peak at {peak_at}")


def spark_text(metrics: dict, *, width: int = SPARK_WIDTH,
               lanes: int | None = None) -> Text:
    """The sparkline and its caption as one quiet block of two lines.

    Empty when the window holds no active minute: an empty picture is not a
    shape, and the screen hides the line rather than drawing a flat one.
    """
    blocks = concurrency_sparkline(metrics, width=width, lanes=lanes)
    if not blocks:
        return Text("")
    text = Text(blocks)
    text.append("\n" + spark_caption(metrics, width=width, lanes=lanes), style="dim")
    return text


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
# the dashboard does not carry them again. They keep their old rows in
# `account_rows` and `local_rows` until the screen swaps to these.
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
    if not previous:
        return DELTA_NONE
    if min(_window_requests(metrics), _window_requests(previous)) < baseline.FLOOR:
        return DELTA_NONE
    now, before = _delta_value(metrics, key), _delta_value(previous, key)
    if now is None or before is None:
        return DELTA_NONE
    if not now and not before:
        return DELTA_NONE
    if now == before:
        return DELTA_LEVEL
    arrow = DELTA_UP if now > before else DELTA_DOWN
    moved = abs(now - before)
    # One cell of air up to nine, so a ten-request move still fits three cells.
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


def _minute_of_day(stamp) -> str:
    """`HH:MMZ` for a sample stamp, or "" when it cannot be read.

    The microseconds the account sends are a fraction nobody acts on, and the
    day cannot change inside one dashboard window.
    """
    epoch = _stamp_epoch(stamp)
    if epoch is None:
        return ""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%H:%MZ")


def _stale_minutes(metrics: dict, stamp):
    """How old the account's sample is, in whole minutes, or None.

    The clock is the scan's own, so this row is a function of the metrics and
    reads no clock of its own.
    """
    epoch = _stamp_epoch(stamp)
    clock = _epoch((metrics or {}).get("scanned_at"))
    if epoch is None or clock is None:
        return None
    return max(0, int((clock - epoch) // 60))


def _now_row(metrics: dict, cap) -> tuple[str, str]:
    """The account's live occupancy: the one number that answers "right now".

    Two numbers from one source wear one `●`, and the sample time sits in the
    qualifier column so two glances can compare it. Freshness is read from
    `metrics["capacity"]["now_fresh"]` and never from the Capacity payload,
    which carries no freshness field: a 47-minute-old sample that still wore
    `●` is the defect this row exists to fix. A sample that is not fresh keeps
    its numbers and loses the authority, reading `?` and `stale 47m`; a cache
    with no live sample prints no ratio and says why.
    """
    served = (metrics or {}).get("capacity") or {}
    now = cli._cap_field(cap, "now")
    in_use = _count(cli._cap_field(now, "in_use"))
    limit = _count(cli._cap_field(now, "limit"))
    if in_use is None or limit is None:
        reason = "no capacity cached" if now is None else "no live sample"
        return _grid_row("now", _BADGE_UNKNOWN, qualifier=reason)
    number = "%d/%d" % (in_use, limit)
    sampled_at = cli._cap_field(now, "sampled_at")
    sampled = _minute_of_day(sampled_at)
    if served.get("now_fresh") is True and sampled:
        return _grid_row("now", badges.BADGE_ACCOUNT, number,
                         qualifier="sampled %s" % sampled)
    minutes = _stale_minutes(metrics, sampled_at)
    return _grid_row("now", _BADGE_UNKNOWN, number,
                     qualifier=("stale %dm" % minutes) if minutes is not None
                     else "stale sample")


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


def load_rows(metrics: dict, cap, window_hours, previous=None) -> list[tuple[str, str]]:
    """The LOAD region's three data rows: now, peak, blocked - always three.

    The order is the order a person asks the questions in: what is happening at
    this instant, how bad it got in this window, and what that cost. Each row
    is (badge, the line it prints), with the badge cell already in the line.

    `previous` is the equal-length window before this one - the scan's own
    extra read. Without it every delta cell is `·`: a first scan has nothing to
    compare against, and the screen says that once, here, rather than printing
    a zero delta that reads as "and nothing changed".
    """
    return [_now_row(metrics, cap),
            _peak_row(metrics, previous, cap, window_hours),
            _blocked_row(metrics, previous)]


# ---- the verdict panel: the CLI's own verdict, never a second one ----------
def verdict_rows(metrics: dict) -> list[tuple[str, str]]:
    """The verdict line, the action, and where the rest of it lives.

    The badge column stays empty: a verdict is a conclusion drawn from the rows
    above it, not a number one source reported. The text is `cli.verdict`'s own
    headline, so the TUI cannot disagree with the CLI about what the window
    means. The status moon leads the headline, read from
    `report.status_moon` - the same helper the pretty renderer asks. The legend
    for that scale is not a row here: `status_legend` returns it, and the screen
    prints it as its own quiet line under the panel, the weight the pretty
    renderer gives it. The one arrow marks the action this screen can take in
    place.

    One sentence, not the whole verdict: the headline is the conclusion, and
    `analyze` prints it with every adjustment in full while `[d]` shows that
    report here. So the panel states the headline and points at the report
    instead of becoming the report. The adjustments are never cut down to fit:
    a trimmed adjustment reads as a smaller finding than the CLI reported. The
    last row is the pointer, and there is only one.
    """
    headline, _adjustments, exit_code = cli.verdict(metrics)
    moon = report.status_moon(exit_code, headline)
    glyph = _GLYPH_STYLE.symbol(moon)
    line = first_sentence(headline)
    rows = [("", f"{glyph} {line}" if glyph else line)]
    rows += [("→", "press [f] for the per-agent fix, or run `luna-doctor fix`"),
             ("", "details: press d for the full analyze report")]
    return rows


def status_legend(metrics: dict) -> str:
    """The one-line legend for the status moons, or "" when no phase is drawn.

    It is the same sentence the pretty renderer closes with, from the same
    table, and the screen prints it as its own muted line: a key to a scale is
    not a conclusion, so it never wears the verdict panel's bold.
    """
    headline, _adjustments, exit_code = cli.verdict(metrics)
    if report.status_moon(exit_code, headline) not in report.MOON_PHASES:
        return ""
    return _GLYPH_STYLE.moon_legend()


def build_verdict_rows(choice_key: str, metrics: dict, cap) -> list[tuple[str, str]]:
    """Every (badge, text) row of the verdict screen, in ranked-trust order."""
    return (account_rows(metrics, cap)
            + local_rows(choice_key, metrics)
            + verdict_rows(metrics))


def panel_text(heading: str, rows, *, bold: bool = False) -> Text:
    """One panel: its heading, then one line per row.

    The badge is what a reader sorts on, so it leads the line. A row with no
    badge is a conclusion (the verdict and its action), and it is not given a
    source it does not have. Measured rows render dimmed, the verdict bold.
    """
    text = Text()
    text.append(heading, style="bold")
    for badge, line in rows:
        text.append("\n")
        row = f"{badge} {line}" if badge else line
        if bold:
            text.append(row, style="bold")
        elif badge == _BADGE_MEASURED:
            text.append(row, style="dim")
        else:
            text.append(row)
    return text


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


def scan_status(metrics: dict, cap, online: bool = False) -> str:
    """The scan's own line: what was read, never a verdict.

    It names the request count and the account's plan and width, and it says
    "not reported" where a number is missing rather than leaving a gap a reader
    would fill with a zero.
    """
    n = _count(metrics.get("requests")) or 0
    bits = [f"✓ {n} request(s) in the last {int(metrics.get('window_hours') or WINDOW_HOURS)}h"]
    if cap is None:
        bits.append("account capacity: not cached (unknown)")
    else:
        plan = cli._cap_unknown(cli._cap_field(cap.config, "plan"))
        width = cli._cap_unknown(cli._cap_field(cap.config, "priority_lanes"))
        bits.append(f"account: plan {plan} · priority concurrency {width}")
    if online:
        bits.append("the cache was refreshed by --online")
    return " · ".join(bits)


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
    """The triage screen: one question, then one badged verdict.

    The scan and the cache-only capacity read run in a thread worker while the
    question is on screen, so the answer is never blocked. The app opens no
    network behavior of its own: run_triage(online=True) refreshes the cache
    once before the screen opens, exactly like `--online` on the CLI.
    """

    TITLE = "luna-doctor triage"

    CSS = """
    Screen { background: $surface; }
    #question { padding: 1 2 0 2; text-style: bold; }
    #choices { margin: 1 2 0 2; height: auto; max-height: 10; border: round $primary; }
    #status { padding: 1 2; color: $text-muted; }
    #verdict { display: none; padding: 0 2; }
    .panel { border: round $primary; padding: 0 1; margin: 1 0 0 0; height: auto; }
    #verdict-line { border: round $accent; }
    #spark { padding: 1 2 0 2; }
    #legend, #moon-legend { padding: 1 2; color: $text-muted; }
    #detail { display: none; border: round $secondary; padding: 0 1; }
    """

    BINDINGS = [
        Binding("c", "show_chart", "chart"),
        Binding("d", "show_detail", "detail"),
        Binding("f", "show_fix", "fix"),
        Binding("q", "quit", "quit"),
        Binding("w", "cycle_window", "window"),
    ]

    def __init__(self, plan: str | None = None, online: bool = False) -> None:
        super().__init__()
        self.plan = plan
        self.online = online
        self.window_hours: int = WINDOW_HOURS
        self.metrics: dict | None = None
        self.previous: dict | None = None
        self.capacity = None
        self.pending_choice: str | None = None
        self.scan_failed: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("What are you seeing?", id="question")
        yield ListView(
            *[ListItem(Label(text), id=key) for text, key in TRIAGE_CHOICES],
            id="choices")
        # markup=False: this line repeats the CLI's words, and those name keys
        # as [f] and [q]. As Rich markup, the brackets and what they hold vanish.
        yield Static("", id="status", markup=False)
        with VerticalScroll(id="verdict"):
            yield Static("", id="account", classes="panel")
            yield Static("", id="local", classes="panel")
            # the picture between the evidence and the conclusion it supports
            yield Static("", id="spark")
            yield Static("", id="verdict-line", classes="panel")
            # its own line, in the muted weight every legend on this screen
            # wears: the verdict panel above it is bold, and a key is not a
            # conclusion
            yield Static("", id="moon-legend")
            yield Static(badges.legend_line(), id="legend")
        with VerticalScroll(id="detail"):
            # markup=False: this panel prints a command's own words, and the
            # fix block may name a tier as [agents].
            yield Static("", id="detail-text", markup=False)

    def on_mount(self) -> None:
        self._set_status(f"scanning local logs… (last {self.window_hours}h)")
        self.query_one("#choices", ListView).focus()
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
        self._set_status(f"the scan failed ({reason}); no verdict is available")

    def _scan_done(self, metrics: dict, previous: dict | None, cap) -> None:
        self.metrics = metrics
        self.previous = previous
        self.capacity = cap
        self._set_status(scan_status(metrics, cap, self.online))
        if self.pending_choice is not None:
            self._show_verdict(self.pending_choice)

    # -- the answer -------------------------------------------------------
    def on_list_view_selected(self, event: ListView.Selected) -> None:
        key = event.item.id
        if key is None:
            return
        if self.scan_failed is not None:
            self._set_status(f"the scan failed ({self.scan_failed}); there is no verdict to show")
            return
        self.pending_choice = key
        if self.metrics is None:
            self._set_status("the scan is still running; the verdict appears when it finishes")
            return
        self._show_verdict(key)

    def _show_verdict(self, key: str) -> None:
        self.query_one("#account", Static).update(
            panel_text(PANEL_ACCOUNT, account_rows(self.metrics, self.capacity)))
        self.query_one("#local", Static).update(
            panel_text(PANEL_LOCAL, local_rows(key, self.metrics)))
        self.query_one("#verdict-line", Static).update(
            panel_text(PANEL_VERDICT, verdict_rows(self.metrics), bold=True))
        legend = status_legend(self.metrics)
        moon_legend = self.query_one("#moon-legend", Static)
        moon_legend.update(legend)
        # no phase drawn means no key: an empty line would be decoration
        moon_legend.display = bool(legend)
        lanes, _source = resolved_lanes(self.metrics, self.capacity)
        spark = spark_text(self.metrics, lanes=lanes)
        spark_line = self.query_one("#spark", Static)
        spark_line.update(spark)
        # no active minute means no picture: a blank line is not a shape
        spark_line.display = bool(spark.plain)
        self.query_one("#question").display = False
        self.query_one("#choices").display = False
        self.query_one("#verdict").display = True
        self._set_status(f"{scan_status(self.metrics, self.capacity, self.online)} · "
                         f"[f] shows the fix, [d] the full report, "
                         f"[c] draws the chart, [q] quits")

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
            self._set_status(f"the {command} command failed: {reason} · [q] quits")
            return
        self.query_one("#detail-text", Static).update(text or f"the {subject} was empty")
        self.query_one("#detail").display = True
        self._set_status(f"the {subject} is on screen · [q] quits")

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
            self._set_status(f"the chart failed: {error} · [q] quits")
            return
        written = png_path or svg_path
        opened = " · the PNG is open" if png_path else ""
        self._set_status(f"chart written: {written}{opened} · [q] quits")


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
