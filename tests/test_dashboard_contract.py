"""The dashboard contract: nineteen rows, twenty with LATENCY, 78 cells.

The dashboard redesign decided a screen, and this file holds the screen to that
decision with properties over the drawn rows instead of over prose: every row
fits 78 cells, the screen is eighteen drawn rows and nineteen when the LATENCY
group's four tier rows are on it, `█` means the account's lane count and nothing
else, no row ends a sentence, no row names a day or a 7-day or 30-day window,
the severity and trend words are closed sets, and the moon on a region title
explains the moon on the header.

The one row latitude is the ruling that made the tiers independent: the LATENCY
group draws a p50 and a p90 for each tier, four rows where the refusals and
stalls groups draw three, and the gateway's fixed wait - a constant with no
trend - left the group so the screen grew by one row rather than two. The
MODELS group is the other variable, and it is its own contents that decide it:
one row per model it measured, one to three, or one row when it measured none.

The rows come from `tui.dashboard_lines(choice_key, metrics, previous, cap)`:
the one pure function that draws the dashboard, in the order the screen draws
it, without the Textual `Footer`. It returns the plain strings the app mounts,
and the app must draw exactly those, so that the screen tested here and the
screen a person opens are one screen. The `Footer` draws under them: it renders
the bindings and adds no number of its own.
Everything else here is measured off those strings, so a row that moves, wraps,
grows a sentence or wears a second moon fails.

The five states are the design's own: over capacity, healthy, no capacity
cached, the refusals group, and a window with no traffic. Each one is built from
the canned capacity payload the other capacity tests read, so no test here opens
the network or reads a real cache.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import unicodedata
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

pytest.importorskip("textual")

from luna_doctor import cli, report, sources, tui
from luna_doctor.style import Style

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")

# The design's terminal target: 80 columns, a 2-cell right margin, so 78 cells
# is the hard cap on any row in any state.
ROW_BUDGET = 78

# The screen is eighteen drawn rows in a state whose evidence group holds three,
# nineteen when LATENCY holds four - two percentiles for each of two tiers - and
# shorter when MODELS measured fewer than three models, which is the one height
# the group's own rows decide. The builders draw those; the Textual Footer draws
# under them and adds no number of its own.
DRAWN_ROWS = 18
GROUP_ROWS = {"LATENCY": 4, "MODELS": 3, "REFUSALS": 3, "STALLS": 3}

# The scan clock the five states are pinned to, and the one-hour window that
# closes at it. The burst in the busy hour peaks at 14:07Z, which is one of the
# facts the caption states.
CLOCK = "2026-11-02T14:32Z"
WINDOW_HOURS = 1

# §4.2: four glyphs, four words, and the word is a 1:1 function of the glyph.
MOON_WORDS = {
    "\U0001F315": "OK",
    "\U0001F317": "PRESSURE",
    "\U0001F311": "OVER CAPACITY",
    "\U0001F319": "NOT ESTABLISHED",
}

# §7 rules 4 and 5: the two closed word sets. Severity is one word per moon,
# capitalised and never a synonym. Trend is `baseline.change_verdict`'s four,
# and every hedge that is not one of the four is banned.
TREND_WORDS = ("better", "worse", "unchanged", "inconclusive")
HEDGES = re.compile(
    r"\b(seems|likely|may be|trending|fine|busy|degraded|bad)\b", re.I)

# The evidence group titles. Region 3 is the only region whose title and rows
# change, and `LOAD` is the only title that never does.
EVIDENCE_GROUPS = ("LATENCY", "MODELS", "REFUSALS", "STALLS")
REGION_TITLES = ("LOAD",) + EVIDENCE_GROUPS

_BLOCKS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"


# ---- measuring one drawn row -----------------------------------------------
def cells(line: str) -> int:
    """The width of one drawn row in terminal cells.

    A phase moon is 2 cells wide and every other glyph in the palette is 1, so
    a code-point count would under-report the row that wears a moon.
    """
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in line)


def moons(line: str) -> list[str]:
    """Every status moon drawn on one row, in order."""
    return [ch for ch in line if ch in MOON_WORDS]


def header_word(lines: list[str]) -> str:
    """The status word on the first row, with its moon taken off."""
    first = re.split(r"\s+·\s+", lines[0].strip(), maxsplit=1)[0]
    return "".join(ch for ch in first if ch not in MOON_WORDS).strip()


def region_titles(lines: list[str]) -> list[tuple[str, str]]:
    """(title, moon token) for each region title row, in reading order."""
    found = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[0] in REGION_TITLES:
            found.append((parts[0], parts[1]))
    return found


def region_of(line: str):
    """The region title a row starts with, or None when it starts with nothing."""
    parts = line.split()
    return parts[0] if parts and parts[0] in REGION_TITLES else None


def rule_rows(lines: list[str]) -> list[int]:
    """The indexes of the rows that are a horizontal rule."""
    return [i for i, line in enumerate(lines) if line.strip() and set(line.strip()) == {"\u2500"}]


def group_height(lines: list[str], group: str) -> int:
    """The data rows one evidence group draws, from its title to the next rule."""
    title = next(i for i, line in enumerate(lines) if line.split()[:1] == [group])
    end = next(i for i in rule_rows(lines) if i > title)
    return end - title - 1


def picture_row(lines: list[str]):
    """The one drawn row of blocks, or None when the window drew no picture."""
    for line in lines:
        if any(ch in _BLOCKS for ch in line):
            return line
    return None


# ---- the five states, as inputs -------------------------------------------
def _payload(now: dict | None = None) -> dict:
    """The canned account payload the capacity tests read, with `now` pinned."""
    with open(os.path.join(FIXTURES, "capacity-2026-11-02.json")) as fh:
        payload = json.load(fh)
    payload["data"]["now"].update(now or {})
    return payload


def _cap(payload: dict):
    """The Capacity dataclass the fetch builds from one payload."""
    fields, err = sources._parse_capacity_payload(payload)
    assert not err, err
    return sources.Capacity(fetched_at=time.time(), **fields)


def _capacity_block(payload: dict, *, fresh: bool = True,
                    window_days=()) -> dict:
    """The metrics.capacity block `cli._capacity_metrics` builds, nulls intact.

    `window_days` is filled in on purpose: the block arrives carrying a complete
    UTC day and its rollups, and the dashboard must print none of them. A
    fixture that withheld them could not catch a calendar row.
    """
    data = payload["data"]
    config = data.get("config") or {}
    return {
        "provenance": sources.PROV_ACCOUNT,
        "source": "GET /v1/capacity (cache)",
        "stale": False,
        "note": "served from cache",
        "plan": config.get("plan"),
        "priority_lanes": config.get("priority_lanes"),
        "background_lanes": config.get("background_lanes"),
        "overflow_routing": config.get("overflow_routing"),
        "now_fresh": fresh,
        "now": dict(data.get("now") or {}),
        "window_days": list(window_days),
    }


def _by_minute(spec, clock: str = CLOCK) -> list[dict]:
    """by_minute entries from (minute offset, max_concurrent) pairs.

    The offsets are minutes after the window opens, so a pair is a minute in the
    hour the screen measured and nothing else.
    """
    end = datetime.strptime(clock, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    start = end - timedelta(hours=WINDOW_HOURS)
    return [{"minute": (start + timedelta(minutes=offset)).strftime("%Y-%m-%dT%H:%MZ"),
             "max_concurrent": value, "started": value}
            for offset, value in spec]


def _clock_epoch(clock: str = CLOCK) -> float:
    return datetime.strptime(clock, "%Y-%m-%dT%H:%MZ").replace(
        tzinfo=timezone.utc).timestamp()


def _metrics(*, requests, peak, minutes, outcomes, tiers, latency,
             capacity, models=()) -> dict:
    """What analyse() produces, reduced to the keys the dashboard reads."""
    dead = outcomes.get("admission_timeout", 0)
    done = outcomes.get("completed", 0)
    admitted = round(100.0 * done / (done + dead), 1) if done + dead else None
    return {
        "window_hours": WINDOW_HOURS,
        "requests": requests,
        "peak_concurrency": peak,
        "peak_concurrency_priority": peak,
        "by_outcome": dict(outcomes),
        "by_tier": dict(tiers),
        "by_harness": {"pi": requests},
        "latency_s": {tier: dict(values) for tier, values in latency.items()},
        "latency_by": {"floor": 5, "suppressed": 0,
                       "by_tier_model": [dict(cell) for cell in models],
                       "by_tier_harness": [], "by_tier_outcome": []},
        "admission_conversion": {
            "interactive": {"completed": done, "admission_timeout": dead,
                            "admitted_pct": admitted},
            "background": {"completed": 0, "admission_timeout": 0,
                           "admitted_pct": None}},
        "pressure_windows": 0,
        "longest_pressure_window": None,
        "saturated": 0,
        "starved_peer_evidence": 0,
        "saturation_bands": {},
        "timeout_storms": [],
        "unknown_errors": [],
        "per_model_deadlines": {},
        "declared_plan": None,
        "capacity": capacity,
        "by_minute": _by_minute(minutes),
        "scanned_at": _clock_epoch(),
    }


# The busy hour: seven minutes at or over the account's 12 lanes, peaking at 17
# on the minute the caption names, inside the sixty the picture draws.
_OVER_MINUTES = (
    (1, 2), (3, 3), (7, 4), (12, 2), (16, 5), (20, 4), (24, 6), (28, 5),
    (30, 6), (31, 9), (32, 12), (33, 14), (34, 16), (35, 17), (36, 15),
    (37, 13), (38, 12), (40, 8), (44, 6), (48, 5), (52, 4), (56, 3), (59, 2),
)
# A quiet hour: nothing reaches the lane count, so the picture draws no `█`.
_OK_MINUTES = (
    (1, 1), (4, 2), (8, 3), (12, 2), (16, 3), (20, 4), (24, 3), (28, 2),
    (32, 3), (36, 2), (40, 2), (44, 3), (48, 2), (52, 1), (56, 2), (59, 1),
)

# The complete UTC day the account block carries, in the two shapes the states
# need. It is a drill-down fact, and the dashboard must never print it: the busy
# shape is the one the calendar lint hunts through.
_DAY = {"day": "2026-11-01", "accepted": 4570, "denied_after_wait": 0,
        "deferred": 0, "avg_queue_wait_ms": 144544.82}
_BUSY_DAY = {"day": "2026-11-01", "accepted": 900, "denied_after_wait": 1213,
             "deferred": 180123, "avg_queue_wait_ms": 144544.82}

# The status moons, through the one gate that owns glyphs.
GLYPHS = Style(color=False, emoji=True, motion=False)

# The per-model half of the busy hour's latency: three models cleared
# `latency_by`'s floor, one of them on the slow tier, so the MODELS group has
# exactly the three rows a `[g]`-reached group draws.
_BUSY_MODELS = (
    {"cell": "interactive / glm-5.3", "n": 20, "p50": 95.0, "p90": 240.0, "max": 300.0},
    {"cell": "background / glm-5.3-background", "n": 7, "p50": 400.0, "p90": 520.0,
     "max": 600.0},
    {"cell": "interactive / claude-opus-lr11", "n": 6, "p50": 130.0, "p90": 300.0,
     "max": 340.0},
)

_PRESSURED_OUTCOMES = {"completed": 39, "admission_timeout": 3, "rate_limited": 11,
                       "org_concurrency_limit": 4}
# The same hour with no admission block in it and no lane count to judge it
# against: pressure this machine cannot explain, which is the quarter moon.
_UNEXPLAINED_OUTCOMES = {"completed": 39, "rate_limited": 11,
                        "org_concurrency_limit": 4, "client_idle_timeout": 2}


class Screen:
    """One of the design's five screens: its inputs, and the rows tui draws."""

    def __init__(self, name: str, choice: str, metrics: dict, previous: dict,
                 cap, minutes, lanes):
        self.name = name
        self.choice = choice
        self.metrics = metrics
        self.previous = previous
        self.cap = cap
        self.minutes = tuple(minutes)
        self.lanes = lanes

    def lines(self) -> list[str]:
        """The rows the dashboard draws, in the screen's order."""
        return list(tui.dashboard_lines(self.choice, self.metrics, self.previous, self.cap))

    def __str__(self) -> str:
        return self.name


def _screens() -> list[Screen]:
    """The five states, in the order the design draws them."""
    busy_now = {"in_use": 7, "limit": 12, "available": 5, "ledger": "up",
                "saturated": False, "sampled_at": "2026-11-02T14:20:00Z"}
    calm_now = {"in_use": 3, "limit": 12, "available": 9, "ledger": "up",
                "saturated": False, "sampled_at": "2026-11-02T14:31:00Z"}
    idle_now = {"in_use": 4, "limit": 12, "available": 8, "ledger": "up",
                "saturated": False, "sampled_at": "2026-11-02T14:31:00Z"}

    busy_cap = _cap(_payload(busy_now))
    calm_cap = _cap(_payload(calm_now))
    idle_cap = _cap(_payload(idle_now))

    pressured = _metrics(
        requests=42, peak=17, minutes=_OVER_MINUTES, outcomes=_PRESSURED_OUTCOMES,
        tiers={"interactive": 35, "background": 7},
        latency={"interactive": {"n": 39, "p50": 95.0, "p90": 240.0, "max": 300.0},
                 "background": {"n": 7, "p50": 400.0, "p90": 520.0, "max": 600.0}},
        capacity=_capacity_block(_payload(busy_now), window_days=[_BUSY_DAY]),
        models=_BUSY_MODELS)
    quieter = _metrics(
        requests=35, peak=12, minutes=_OK_MINUTES,
        outcomes={"completed": 35},
        tiers={"interactive": 35},
        latency={"interactive": {"n": 35, "p50": 83.0, "p90": 202.0, "max": 260.0}},
        capacity=_capacity_block(_payload(calm_now), window_days=[_DAY]),
        models=_BUSY_MODELS)
    calm = _metrics(
        requests=9, peak=4, minutes=_OK_MINUTES,
        outcomes={"completed": 9},
        tiers={"interactive": 9},
        latency={"interactive": {"n": 9, "p50": 31.0, "p90": 58.0, "max": 90.0}},
        capacity=_capacity_block(_payload(calm_now), window_days=[_DAY]))
    idle = _metrics(
        requests=0, peak=None, minutes=(), outcomes={}, tiers={}, latency={},
        capacity=_capacity_block(_payload(idle_now), window_days=[_DAY]))
    # No capacity cached: the account block is the CLI's unknown block, and the
    # screen judges the hour against a lane count nobody reported.
    uncached = _metrics(
        requests=42, peak=17, minutes=_OVER_MINUTES, outcomes=_UNEXPLAINED_OUTCOMES,
        tiers={"interactive": 35, "background": 7},
        latency={"interactive": {"n": 39, "p50": 95.0, "p90": 240.0, "max": 300.0}},
        capacity={"provenance": sources.PROV_UNKNOWN,
                  "reason": "no capacity payload is cached"})

    return [
        Screen("over capacity", "slow", pressured, quieter, busy_cap,
               _OVER_MINUTES, 12),
        Screen("healthy", "slow", calm, quieter, calm_cap, _OK_MINUTES, 12),
        Screen("no capacity cached", "slow", uncached, quieter, None,
               _OVER_MINUTES, None),
        Screen("refusals", "concurrency", pressured, quieter, busy_cap,
               _OVER_MINUTES, 12),
        Screen("no traffic", "slow", idle, quieter, idle_cap, (), 12),
    ]


SCREENS = _screens()
LANE_SCREENS = [screen for screen in SCREENS if screen.lanes is not None]


# ---- the geometry -----------------------------------------------------------
class RowGeometry(unittest.TestCase):
    def test_every_row_fits_the_seventy_eight_cell_budget(self):
        """The hard cap holds in all five states, moons counted as 2 cells."""
        for screen in SCREENS:
            for line in screen.lines():
                self.assertLessEqual(
                    cells(line), ROW_BUDGET,
                    f"the {screen} screen draws a {cells(line)}-cell row: {line!r}")

    def test_the_screen_is_the_designed_height_in_every_state(self):
        """Eighteen drawn rows plus the Footer, and one more when LATENCY is up.

        The count is the contract, not the content: a region that grows or drops
        a row moves every row under it, which is the scan pattern the design
        forbids. The one group that is allowed to be shorter is MODELS, whose
        height is the models it measured, so this test draws it for every state
        and counts the rows it draws. The window with no traffic keeps its
        picture's two rows, empty, rather than handing their place to the
        regions below.
        """
        for screen in SCREENS:
            lines = screen.lines()
            group = tui.pick_lead(screen.choice).upper()
            expected = DRAWN_ROWS + GROUP_ROWS[group] - 3
            self.assertEqual(
                expected, len(lines),
                f"the {screen} screen draws {len(lines)} rows, not {expected}: "
                + repr(lines))
            self.assertEqual(
                GROUP_ROWS[group], group_height(lines, group),
                f"the {screen} screen's {group} group is {group_height(lines, group)} rows")

    def test_the_models_group_is_as_tall_as_the_models_it_measured(self):
        """§3.1: one row per measured model, three at most, and one when none.

        MODELS is the one group whose height follows its own rows, so the height
        contract is checked over the models each state measured: the three-model
        states draw the eighteen-row shape, the idle window measured none and
        draws its single no-model row two rows shorter.
        """
        for screen in SCREENS:
            models = (screen.metrics.get("latency_by") or {}).get("by_tier_model") or []
            rows = min(GROUP_ROWS["MODELS"], len(models)) or 1
            lines = tui.dashboard_lines(tui.REGION_MODELS, screen.metrics,
                                        screen.previous, screen.cap)
            self.assertEqual(DRAWN_ROWS + rows - 3, len(lines),
                             f"the {screen} models screen draws {len(lines)} rows")
            self.assertEqual(rows, group_height(lines, "MODELS"),
                             f"the {screen} models group is "
                             f"{group_height(lines, 'MODELS')} rows")

    def test_the_two_ends_of_the_screen_are_chrome(self):
        """The header is the first row and the two key lines are the last two.

        A region title is not a key and a key is not a region: the design puts
        the reading keys last, and the Footer under them.
        """
        for screen in SCREENS:
            lines = screen.lines()
            self.assertIsNone(region_of(lines[0]),
                             f"the {screen} screen starts with a region title")
            self.assertEqual(2, len([line for line in lines[-2:]
                                     if region_of(line) is None]),
                             f"the {screen} screen does not end on two key lines")

    def test_the_answer_picks_the_evidence_group(self):
        """LOAD is fixed. The group under it names the trouble the answer named.

        The refusals group is the 429 answer's, and the latency group is the slow
        answer's: the same three rows in the same places, holding the evidence
        for the question the person picked.
        """
        chosen = {}
        for screen in SCREENS:
            titles = [title for title, _moon in region_titles(screen.lines())]
            self.assertEqual("LOAD", titles[0], f"the {screen} screen starts on LOAD")
            self.assertEqual(1, titles.count("LOAD"), f"{screen}: LOAD appears twice")
            groups = [title for title in titles if title in EVIDENCE_GROUPS]
            self.assertEqual(1, len(groups),
                             f"the {screen} screen holds {len(groups)} evidence groups")
            chosen[screen.name] = groups[0]
        self.assertEqual("REFUSALS", chosen["refusals"])
        self.assertEqual("LATENCY", chosen["over capacity"])

    def test_the_models_group_draws_one_row_per_model_on_the_same_grid(self):
        """MODELS is reached with `[g]`, and it draws its own measured models.

        One row per measured model, three at most, on the same 78-cell grid: the
        region that `[g]` adds is a group, not a taller screen, so the Footer is
        still the row under the key lines. This window measured three models, so
        it draws the eighteen-row shape.
        """
        screen = SCREENS[0]
        lines = tui.dashboard_lines("models", screen.metrics, screen.previous,
                                    screen.cap)
        self.assertEqual(DRAWN_ROWS, len(lines))
        self.assertTrue(lines[9].startswith("MODELS "), lines[9])
        self.assertEqual(3, len(tui.group_rows(tui.REGION_MODELS, screen.metrics,
                                               screen.previous)))
        for line in lines:
            self.assertLessEqual(cells(line), ROW_BUDGET,
                                 f"the models screen draws a wide row: {line!r}")
            self.assertTrue(tui.dashboard_ok_row(line), line)

    def test_the_load_region_keeps_its_six_rows(self):
        """Title, three numbers, the picture's two rows: always six.

        The empty-picture rule hides the drawing, not the rows: the window with
        no traffic still holds the six, so the regions under it do not move.
        """
        for screen in SCREENS:
            lines = screen.lines()
            start = next(i for i, line in enumerate(lines) if region_of(line) == "LOAD")
            end = next(i for i in rule_rows(lines) if i > start)
            self.assertEqual(6, end - start,
                             f"the {screen} LOAD region holds {end - start} rows")


# ---- the picture ------------------------------------------------------------
class ThePicture(unittest.TestCase):
    def test_the_block_means_the_lane_count(self):
        """`█` appears exactly when a minute reached the account's lanes.

        The glyph has one meaning on every screen: at or over the lane count.
        A picture that draws `█` on a quiet minute, or withholds it from the
        minute that crossed the wall, is the decoration this design replaced.
        """
        for screen in LANE_SCREENS:
            if not screen.minutes:
                continue        # a window with no minute draws no picture at all
            picture = picture_row(screen.lines())
            self.assertIsNotNone(picture, f"the {screen} screen drew no picture")
            reached = any(value >= screen.lanes for _offset, value in screen.minutes)
            self.assertEqual(
                reached, "\u2588" in picture,
                f"the {screen} screen: a minute reached {screen.lanes} lanes = {reached}, "
                f"and the picture reads {picture!r}")

    def test_an_unknown_lane_count_says_which_scale_it_drew(self):
        """With no denominator the ramp self-scales, and the caption says so.

        `█` cannot mean the lane count on a screen that has none, so the caption
        names the scale instead of leaving the reader to guess it.
        """
        uncached = [screen for screen in SCREENS if screen.lanes is None]
        self.assertTrue(uncached, "the five states must include one with no lane count")
        for screen in uncached:
            self.assertIsNotNone(picture_row(screen.lines()),
                                 f"the {screen} screen measured the hour but drew no picture")
            self.assertTrue(
                any("scale: this window" in line for line in screen.lines()),
                f"the {screen} screen does not name its scale")

    def test_a_window_with_no_minute_draws_no_picture(self):
        """No active minute is no picture: an empty picture is not a shape."""
        quiet = [screen for screen in SCREENS if not screen.minutes]
        self.assertTrue(quiet, "the five states must include one with no traffic")
        for screen in quiet:
            self.assertIsNone(picture_row(screen.lines()),
                              f"the {screen} screen drew a picture from no minute")


# ---- the words --------------------------------------------------------------
class TheWords(unittest.TestCase):
    def test_no_row_ends_a_sentence(self):
        """A data row is a label, a badge, a number, a delta and one fact."""
        for screen in SCREENS:
            for line in screen.lines():
                self.assertFalse(
                    line.rstrip().endswith((".", "!", "?")),
                    f"the {screen} screen ends a row as a sentence: {line!r}")

    def test_no_row_names_a_day_or_a_week_or_month_window(self):
        """No calendar on the dashboard, in any window.

        The fixture hands the region builders a complete UTC day and a 7-day and
        30-day rollup. None of them has a place on a nineteen-row screen.
        """
        calendar = re.compile(
            r"\b(?:7d|30d|\d+d|days?|weeks?|months?|yesterday|today|utc day"
            r"|mon|tue|wed|thu|fri|sat|sun"
            r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
            r"|january|february|march|april|june|july|august|september"
            r"|october|november|december"
            r"|last \d+ (?:days?|weeks?|months?))\b", re.I)
        for screen in SCREENS:
            for line in screen.lines():
                found = calendar.search(line)
                self.assertIsNone(
                    found, f"the {screen} screen names a calendar day: {found and found.group(0)!r}")

    def test_no_row_hedges(self):
        """§7 rule 5 bans every hedge that is not one of the four trend words."""
        for screen in SCREENS:
            for line in screen.lines():
                found = HEDGES.search(line)
                self.assertIsNone(
                    found, f"the {screen} screen hedges: {found and found.group(0)!r}")

    def test_the_status_word_is_one_of_four(self):
        """One word per moon, capitalised, and never a synonym for it."""
        for screen in SCREENS:
            lines = screen.lines()
            worn = moons(lines[0])
            self.assertEqual(1, len(worn), f"the {screen} header wears {worn!r}")
            word = header_word(lines)
            self.assertIn(word, MOON_WORDS.values(),
                          f"the {screen} screen says {word!r} on its first row")
            self.assertEqual(
                MOON_WORDS[worn[0]], word,
                f"the {screen} screen pairs {worn!r} with {word!r}")

    def test_a_delta_carries_one_of_the_four_trend_words(self):
        """§7 rule 5: the trend slot is `baseline`'s four words and nothing else.

        The arrow says direction and the word says the verdict; a hedge beside
        an arrow is a fifth word for a four-word scale.
        """
        slot = re.compile(r"·\s+([A-Za-z]+)\s+[\u25B2\u25BE\u25AC]")
        seen = 0
        for screen in SCREENS:
            for match in slot.finditer(screen.lines()[0]):
                seen += 1
                self.assertIn(match.group(1).lower(), TREND_WORDS,
                              f"the {screen} screen trends with {match.group(1)!r}")
        self.assertTrue(seen, "no state drew a trend word, so nothing was checked")


# ---- the moons --------------------------------------------------------------
class TheMoons(unittest.TestCase):
    def test_the_header_moon_is_the_cli_verdict(self):
        """§4.2: the screen and the CLI cannot disagree about the window.

        The header moon is `report.status_moon(*cli.verdict(metrics))`, so the
        five states have to earn the moons they draw before the region invariant
        means anything. Between them they draw all four words, which is what
        makes the invariant's clauses reachable.
        """
        drawn = []
        for screen in SCREENS:
            headline, _adjustments, code = cli.verdict(screen.metrics)
            earned = GLYPHS.symbol(report.status_moon(code, headline))
            worn = moons(screen.lines()[0])
            self.assertEqual(1, len(worn), f"the {screen} header wears {worn!r}")
            drawn.append(MOON_WORDS[worn[0]])
            self.assertEqual(earned, worn[0],
                             f"the {screen} screen wears {worn[0]!r}, and the CLI earned {earned!r}")
        self.assertEqual(set(MOON_WORDS.values()), set(drawn),
                         f"the five states draw {drawn!r}, not all four words")

    def test_a_region_moon_explains_the_header_moon(self):
        """§4.2's invariant, which is the whole reason a region wears a moon.

        A darker header needs a darker region under it, because the regions are
        the causes and the header is the conclusion. A clean header admits no
        region that contradicts it.
        """
        for screen in SCREENS:
            lines = screen.lines()
            header = moons(lines[0])[0]
            titles = region_titles(lines)
            self.assertTrue(titles, f"the {screen} screen has no region title")
            region_moons = [worn for _title, worn in titles]
            for worn in region_moons:
                self.assertIn(worn, MOON_WORDS,
                              f"the {screen} screen wears {worn!r} as a region moon")
            if header == "\U0001F311":          # OVER CAPACITY: a cause must show
                self.assertIn(
                    "\U0001F311", region_moons,
                    f"the {screen} screen is over capacity and no region says so")
            if header == "\U0001F315":          # OK: no region may contradict it
                self.assertEqual(
                    ["\U0001F315"] * len(region_moons), region_moons,
                    f"the {screen} screen is OK and a region disagrees")

    def test_no_screen_wears_more_than_three_moons(self):
        """The header and at most two region titles. A fourth is a design error."""
        for screen in SCREENS:
            drawn = screen.lines()[:-2]         # the key lines are not status
            worn = [moon for line in drawn for moon in moons(line)]
            self.assertLessEqual(
                len(worn), 3, f"the {screen} screen wears {len(worn)} moons: {worn!r}")
            self.assertLessEqual(
                len(region_titles(screen.lines())), 2,
                f"the {screen} screen wears two region moons and no more")

    def test_the_key_lines_name_the_palette_once(self):
        """Two key lines: one for provenance, one for the moons, and no third."""
        for screen in SCREENS:
            keys = screen.lines()[-2:]
            self.assertTrue(
                any("account" in line for line in keys),
                f"the {screen} screen does not key the provenance badges")
            self.assertTrue(
                any(moons(line) for line in keys),
                f"the {screen} screen does not key the moons")


# ---- the assembled screen ---------------------------------------------------
class TheAssembledScreen(unittest.TestCase):
    """The real app at the design's terminal size, drawn by Textual itself.

    The row tests above measure the rows the builders return. This one measures
    what a person sees: a widget's padding, a border or a blank spacer row is a
    row on the screen even when no builder produced it.
    """

    def test_the_rendered_screen_is_the_designed_height(self):
        """Twenty rows with LATENCY on it, and the Footer is the last of them."""
        screen = SCREENS[0]
        rows, scanned = _rendered(screen)
        self.assertTrue(scanned, "the scan never reached the screen, so nothing was drawn")
        drawn = [row for row in rows if row.strip()]
        self.assertEqual(
            DRAWN_ROWS + 1 + GROUP_ROWS["LATENCY"] - 3, len(drawn),
            "the assembled screen paints these rows at 80x24:\n" + "\n".join(drawn))
        for row in drawn:
            self.assertLessEqual(cells(row), ROW_BUDGET,
                                 f"the assembled screen paints a wide row: {row!r}")


def _painted(app) -> list[str]:
    """The rows the screen paints, as text, once a frame has been composed.

    `Screen.render_lines` paints the screen widget alone and cannot see the
    widgets mounted on it, so the compositor is the reader that holds the frame
    a person sees. Textual is pinned to major 8, and a border, a padding row or
    a scrolled row appears here as it appears on the terminal.
    """
    return [strip.text.rstrip() for strip in app.screen._compositor.render_strips()]


def _rendered(screen: Screen, size=(80, 24)) -> tuple:
    """The rows Textual paints for one state, and whether the scan landed."""
    app = tui.TriageApp()
    scan_result = (screen.metrics, screen.previous, screen.cap)

    async def drive():
        with mock.patch.object(tui, "scan", return_value=scan_result):
            async with app.run_test(size=size) as pilot:
                for _attempt in range(200):
                    await pilot.pause(0.02)
                    # The Footer is composed a frame after the rows land, so the
                    # frame a person sees is the one that holds all of them.
                    if app.metrics is not None and any(
                            "quit" in row for row in _painted(app)):
                        break
                rows = _painted(app)
                await pilot.press("q")
                return rows, app.metrics is not None

    return asyncio.run(drive())


if __name__ == "__main__":
    unittest.main()
