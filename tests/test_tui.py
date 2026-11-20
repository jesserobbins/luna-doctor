"""The triage screen's data contract: one answer in, the designed rows out.

Every assertion reads the rows `tui.dashboard_lines` builds or the widgets the
app mounts, so the suite runs offline and needs no terminal. Textual is an
optional extra (`luna-doctor[tui]`), so this file imports it only when it is
installed - `tui.py` imports Textual at the top by design, and the caller guards
that import.

The order is the design's own (§3.1 of the dashboard brief): the status header
and its rule, LOAD's six rows, the evidence group the answer selects, NEXT, and
the two key lines. The header's word and moon are `cli.verdict`'s and
`report.status_moon`'s, so the screen cannot hold a second verdict about the
window - and it never prints the CLI's headline sentence.

The rows this file does *not* find matter as much as the ones it does. The
30-day admission record, the overflow pool, the ledger sentence, the saturation
flag, the API field names and the hand-written key hints are drill-down facts:
the kill list (§6) keeps them off the dashboard, and `Footer` renders the keys.
"""

import asyncio
import json
import os
import shutil
import sys
import threading
import time
import unicodedata
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

pytest.importorskip("textual")

from luna_doctor import badges, baseline, cli, report, sources, tui

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")


# ---- the same canned inputs the capacity tests read -------------------------
def _payload(name):
    with open(os.path.join(FIXTURES, name)) as fh:
        return json.load(fh)


def _cap(payload):
    """The Capacity dataclass fetch_capacity builds from one payload.

    It goes through the same parser the fetch does, so a null stays a null
    here exactly as it does on the disk-cache path."""
    fields, err = sources._parse_capacity_payload(payload)
    assert not err, err
    return sources.Capacity(fetched_at=time.time(), **fields)


def _degraded_payload():
    """The documented degraded shape: config null, every `now` field null with
    ledger 'unknown', a null history tail, and a day nobody waited on. Every
    null must survive as unknown, never as a zero."""
    return {
        "success": True,
        "generated_at": "2026-10-24T00:05:14.458609995Z",
        "data": {
            "now": {"limit": None, "in_use": None, "available": None,
                    "saturated": None, "ledger": "unknown", "sampled_at": None},
            "config": None,
            "history": {"as_of": "2026-10-22",
                        "last_7d": {"avg_lanes": None, "days_active": 5,
                                    "days_covered": 6, "lane_hours": None,
                                    "p95_lanes": None, "peak_lanes": None,
                                    "queued_pct": None},
                        "last_30d": None,
                        "rollup_computed_at": "2026-10-23T15:19:01.958669Z",
                        "tail": None},
            "admission": {"as_of": "2026-10-23",
                          "last_7d": [{"accepted": 10, "avg_queue_wait_ms": None,
                                       "day": "2026-10-22", "deferred": 0,
                                       "denied": 0, "denied_after_wait": 0}],
                          "last_30d": []},
        },
    }


def _metrics(**over):
    """What analyse() produces, reduced to the keys the screen reads."""
    m = {
        "window_hours": 24,
        "requests": 17,
        "by_outcome": {"completed": 14, "admission_timeout": 3},
        "by_tier": {"interactive": 15, "background": 2},
        "by_harness": {"pi": 17},
        "latency_s": {"interactive": {"n": 12, "p50": 95.0, "p90": 240.0, "max": 300.0},
                      "background": {"n": 2, "p50": 400.0, "p90": 520.0, "max": 600.0}},
        "peak_concurrency": 17,
        "peak_at": "2026-11-02T23:00:00+00:00",
        "peak_by_harness": {"pi": 17},
        "admission_conversion": {
            "interactive": {"completed": 12, "admission_timeout": 3, "admitted_pct": 80.0},
            "background": {"completed": 2, "admission_timeout": 0, "admitted_pct": 100.0}},
        "saturated": 4,
        "starved_peer_evidence": 0,
        "pressure_windows": 1,
        "longest_pressure_window": {"seconds": 900.0, "max_concurrent": 17},
        "timeout_storms": [],
        "per_model_deadlines": {},
        "unknown_errors": [],
        "saturation_bands": {},
        "declared_plan": None,
        "by_minute": [],
    }
    m.update(over)
    return m


_SPARK_START = "2026-11-02T00:00Z"
_HOUR_START = "2026-11-02T14:00Z"

# The mockups' clock: 14:32Z, when the one-hour window closes.
_CLOCK = datetime(2026, 11, 2, 14, 32, tzinfo=timezone.utc).timestamp()

# The account's live sample, microseconds and all: the `now` row prints the
# minute and nothing finer.
_SAMPLED = "2026-11-02T14:20:43.95670792Z"

# The complete UTC day the account block carries. It is a drill-down fact, and
# the dashboard must never print it (§6.3).
_DAY = {"day": "2026-11-01", "accepted": 900, "denied_after_wait": 1213,
        "deferred": 180123, "avg_queue_wait_ms": 144544.82}


def _served(*, fresh=True, now=None):
    """The `metrics.capacity` block: fresh by default, calendar attached."""
    return {"provenance": sources.PROV_ACCOUNT, "plan": "apex",
            "priority_lanes": 12, "now_fresh": fresh,
            "now": dict(now if now is not None else
                        {"in_use": 7, "limit": 12, "sampled_at": _SAMPLED}),
            "window_days": [dict(_DAY)]}


def _by_minute(pairs, start=_SPARK_START):
    """by_minute entries from (minute offset, max_concurrent) pairs."""
    base = datetime.strptime(start, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    return [{"minute": (base + timedelta(minutes=offset)).strftime("%Y-%m-%dT%H:%MZ"),
             "max_concurrent": value, "started": value, "admission_deaths": 0}
            for offset, value in pairs]


def _scan_of(pairs, **over):
    """Metrics whose 24h window closes at _SPARK_START plus 24h, holding these minutes."""
    base = datetime.strptime(_SPARK_START, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    return _metrics(by_minute=_by_minute(pairs),
                    scanned_at=base.timestamp() + 24 * 3600, **over)


def _hour_scan(pairs, **over):
    """Metrics whose one-hour window closes at _HOUR_START plus an hour.

    One column per minute, so a minute offset is a column index and a `█` cell
    is one minute at or over the lane count.
    """
    base = datetime.strptime(_HOUR_START, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    return _metrics(window_hours=1, by_minute=_by_minute(pairs, start=_HOUR_START),
                    scanned_at=base.timestamp() + 3600, **over)


def _busy_hour():
    """An hour with seven minutes at or over 12 lanes, peaking at 14:04Z."""
    pairs = [(offset, 3) for offset in range(0, 4)]
    pairs += [(offset, 12) for offset in range(4, 11)]
    pairs += [(offset, 3) for offset in range(11, 60)]
    return _hour_scan(pairs)


# ---- the dashboard's windows, as the mockups draw them ---------------------
# Seven minutes at or over the account's 12 lanes, peaking at 17 on 14:35Z.
_OVER_MINUTES = ((1, 2), (7, 4), (20, 4), (31, 9), (32, 12), (33, 14), (34, 16),
                 (35, 17), (36, 15), (37, 13), (38, 12), (44, 6), (52, 4), (59, 2))
# A quiet hour: nothing reaches the lane count, so the picture draws no `█`.
_OK_MINUTES = ((1, 1), (8, 3), (20, 4), (32, 3), (44, 3), (52, 1), (59, 1))


def _dash(**over):
    """One dashboard window: the hour that closed at 14:32Z."""
    metrics = _metrics(window_hours=1, scanned_at=_CLOCK, capacity=_served(),
                       by_minute=_by_minute(_OVER_MINUTES, start=_HOUR_START))
    metrics.update(over)
    return metrics


def _models(busy=True):
    """The per-model cells `cli.latency_by` fills, most samples first."""
    cells = (("interactive / glm-5.3", 20, 95.0, 240.0),
             ("background / glm-5.3-background", 7, 400.0, 520.0),
             ("interactive / claude-opus-lr11", 6, 130.0, 300.0))
    return {"floor": 5, "suppressed": 0, "by_tier_harness": [], "by_tier_outcome": [],
            "by_tier_model": [{"cell": cell, "n": n, "p50": p50, "p90": p90, "max": p90}
                              for cell, n, p50, p90 in cells]}


def _busy(**over):
    """The owner's window: 42 requests, peak 17 against 12 lanes, 3 blocked."""
    metrics = _dash(
        requests=42,
        by_outcome={"completed": 28, "admission_timeout": 3, "rate_limited": 11},
        by_tier={"interactive": 35, "background": 7},
        peak_concurrency=17,
        latency_s={"interactive": {"n": 39, "p50": 95.0, "p90": 240.0, "max": 300.0},
                   "background": {"n": 7, "p50": 400.0, "p90": 520.0, "max": 600.0}},
        latency_by=_models())
    metrics.update(over)
    return metrics


def _busy_before():
    """The window before it: quieter, and wide enough for a delta."""
    return _dash(
        requests=40, by_outcome={"completed": 40}, by_tier={"interactive": 40},
        peak_concurrency=12, latency_s={"interactive": {"n": 35, "p50": 83.0,
                                                        "p90": 202.0, "max": 260.0}},
        latency_by=_models(),
        by_minute=_by_minute(((1, 3), (20, 5), (40, 4)), start=_HOUR_START))


def _healthy():
    """A clean hour: 9 requests, peak 4 under 12 lanes, nothing blocked."""
    return _dash(
        requests=9, by_outcome={"completed": 9}, by_tier={"interactive": 9},
        peak_concurrency=4, saturated=0,
        latency_s={"interactive": {"n": 9, "p50": 31.0, "p90": 58.0, "max": 90.0}},
        by_minute=_by_minute(_OK_MINUTES, start=_HOUR_START))


def _empty():
    """An hour with no traffic at all: nothing was measured here."""
    return _dash(requests=0, by_outcome={}, by_tier={}, peak_concurrency=None,
                 saturated=0, latency_s={}, by_minute=[])


# The rows the screen draws for each answer: every group holds three rows except
# LATENCY, which holds one row per tier and percentile. The Footer draws under
# them and is not one of these.
ROWS_FOR_ANSWER = {"slow": 19, "checking": 19, "concurrency": 18, "stuck": 18,
                   "failing": 18, "finish_reason": 18}

# The height of the group a region title names, read off the answer that selects
# it: `[g]` can put any of the three on screen, and LATENCY is the taller one.
ROWS_FOR_GROUP = {"latency": ROWS_FOR_ANSWER["slow"],
                  "models": ROWS_FOR_ANSWER["slow"] - 1,
                  "refusals": ROWS_FOR_ANSWER["concurrency"],
                  "stalls": ROWS_FOR_ANSWER["stuck"]}


def _painted(app) -> list[str]:
    """The rows the screen paints, as text: the frame a person is looking at."""
    return [strip.text.rstrip() for strip in app.screen._compositor.render_strips()]


def _lines(choice_key="slow", metrics=None, previous=None, cap=None):
    return tui.dashboard_lines(choice_key,
                               _busy() if metrics is None else metrics,
                               _busy_before() if previous is None else previous,
                               cap)


def _cells(text):
    """A string's width in terminal cells: a wide glyph is two cells."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


def _local(stamp):
    """`HH:MM±HH:MM` for one stamp, in the timezone this suite happens to run in.

    The screen prints local time with its offset (a dashboard is read on the
    machine it measures), so an expected value is derived the same way rather than
    pinned to one developer's zone. The format itself is pinned in
    `TheLocalClock` below, where the zone is set for the test and put back.
    """
    return tui.local_hhmm(tui._minute_epoch(stamp))


def _cap_with_background(block):
    """The fixture's Capacity with its live background pool replaced.

    `block` of None removes the pool altogether, which is a payload captured
    before the account reported one - unknown, never an idle pool.
    """
    payload = _payload("capacity-2026-11-02.json")
    if block is None:
        payload["data"]["now"].pop("background", None)
    else:
        payload["data"]["now"]["background"] = block
    return _cap(payload)


# ---- (a) the six answers select the evidence group -------------------------
class TriageChoices(unittest.TestCase):
    """The six on-screen answers, in the order the design draws them.

    The answer chooses which three evidence rows stand under the picture. It
    never gates the screen and it never changes anything above the picture.
    """

    def test_the_six_answers_select_their_evidence_group(self):
        self.assertEqual(
            ["failing", "slow", "finish_reason", "concurrency", "stuck", "checking"],
            [key for _text, key in tui.TRIAGE_CHOICES])
        self.assertEqual(
            ["stalls", "latency", "stalls", "refusals", "stalls", "latency"],
            [tui.pick_lead(key) for _text, key in tui.TRIAGE_CHOICES])

    def test_the_selection_names_a_region_the_screen_draws(self):
        """One spelling per concept: the selector answers in the same region
        kinds `region_moon` and the region title use.

        The six answers reach three of the four groups; MODELS is the per-model
        reading of the same latency, so no answer on the list means it and `[g]`
        is how a person gets there.
        """
        selected = {tui.pick_lead(key) for _text, key in tui.TRIAGE_CHOICES}
        self.assertEqual({tui.REGION_LATENCY, tui.REGION_REFUSALS,
                          tui.REGION_STALLS}, selected)
        self.assertEqual(selected | {tui.REGION_MODELS},
                         set(tui.EVIDENCE_GROUPS))
        for group in tui.EVIDENCE_GROUPS:
            self.assertIn(group, tui.REGION_KINDS)
            self.assertNotEqual(tui.REGION_LOAD, group)

    def test_a_429_answer_selects_the_refusals_and_a_stuck_one_the_stalls(self):
        self.assertEqual(tui.REGION_REFUSALS, tui.pick_lead("concurrency"))
        self.assertEqual(tui.REGION_STALLS, tui.pick_lead("stuck"))

    def test_finish_reason_selects_the_stalls_group(self):
        """The spec binds "stream ended without finish_reason" to the idle
        budget that killed it, which is a stall, not an admission refusal."""
        self.assertEqual(tui.REGION_STALLS, tui.pick_lead("finish_reason"))

    def test_just_checking_selects_latency_rather_than_nothing(self):
        """The dashboard renders before any answer, so "just checking" picks the
        group a quiet screen shows - the same three rows as "slow"."""
        self.assertEqual(tui.REGION_LATENCY, tui.pick_lead("checking"))

    def test_the_screen_opens_on_the_answer_a_quiet_screen_shows(self):
        self.assertEqual("checking", tui.DEFAULT_CHOICE)
        self.assertEqual(tui.REGION_LATENCY, tui.pick_lead(tui.DEFAULT_CHOICE))

    def test_every_answer_has_a_visible_label(self):
        for text, _key in tui.TRIAGE_CHOICES:
            self.assertTrue(text.strip())


# ---- (b) the order: header, LOAD, evidence, NEXT, keys ---------------------
class TheDashboardsOrder(unittest.TestCase):
    """§3.1: the reading order is the decision order, in every state."""

    def setUp(self):
        self.cap = _cap(_payload("capacity-2026-11-02.json"))

    def test_the_rows_run_header_load_evidence_next(self):
        # The default answer is "just checking", so the group on screen is LATENCY
        # and it draws its four tier rows.
        lines = _lines(cap=self.cap)
        self.assertEqual(19, len(lines))
        self.assertTrue(lines[0].startswith(tui.moon_glyph(tui.header_moon(_busy()))))
        self.assertEqual(tui.RULE, lines[1])
        self.assertTrue(lines[2].startswith("LOAD "))
        self.assertTrue(lines[6].startswith(" "), "the picture is the sixth row")
        self.assertTrue(lines[7].startswith(" "), "its caption sits under it")
        self.assertEqual(tui.RULE, lines[8])
        self.assertTrue(lines[9].startswith("LATENCY "))
        self.assertEqual(tui.RULE, lines[14])
        self.assertTrue(lines[15].startswith("NEXT  "))
        self.assertTrue(lines[16].startswith("→ luna-doctor "))
        self.assertEqual(tui.key_lines(_busy(), self.cap), lines[17:])

    def test_the_answer_swaps_the_evidence_group_and_nothing_above_it(self):
        slow = _lines("slow", cap=self.cap)
        refusals = _lines("concurrency", cap=self.cap)
        self.assertEqual(slow[:9], refusals[:9], "a region above the group moved")
        self.assertTrue(refusals[9].startswith("REFUSALS "))
        self.assertNotEqual(slow[10:14], refusals[10:13])

    def test_the_header_word_and_moon_come_from_the_cli_verdict(self):
        """One verdict: the header asks `cli.verdict` and `report.status_moon`,
        the same pair the CLI prints, and reads the word off that glyph."""
        headline, _adjustments, code = cli.verdict(_busy())
        name = report.status_moon(code, headline)
        first = _lines(cap=self.cap)[0]
        self.assertTrue(first.startswith(tui.moon_glyph(name) + " "), first)
        self.assertIn(tui.status_word(name), first)

    def test_the_screen_never_prints_the_headline_sentence(self):
        """§6.1 item 1: the prose left, and the header states its facts."""
        headline, adjustments, _code = cli.verdict(_busy())
        drew = "\n".join(_lines(cap=self.cap))
        self.assertNotIn(headline, drew)
        for adjustment in adjustments:
            self.assertNotIn(adjustment, drew)

    def test_the_lower_bound_caveat_lives_in_the_key_not_the_peak_row(self):
        """§6.1 item 3: one caveat, stated once, in the `○` key line."""
        lines = _lines(cap=self.cap)
        peak = [line for line in lines if line.strip().startswith("peak")]
        self.assertEqual(1, len(peak))
        self.assertNotIn("lower bound", peak[0])
        self.assertIn("(a lower bound)", lines[17])

    def test_the_two_key_lines_close_the_screen(self):
        """§6.2 item 10: provenance on one line, the moons on the other."""
        lines = _lines(cap=self.cap)
        self.assertIn("account", lines[17])
        self.assertIn(tui.moon_glyph(report.MOON_NEW), lines[18])
        for line in (lines[17], lines[18]):
            self.assertLessEqual(_cells(line), tui.HEADER_CELLS)

    def test_a_window_that_claims_no_status_keys_the_plain_moon(self):
        """A key to a glyph nobody drew is decoration; a glyph on screen is not."""
        lines = _lines("slow", _empty(), None, self.cap)
        self.assertEqual(tui.moon_glyph("moon"), lines[0][0])
        self.assertTrue(lines[18].startswith(tui.moon_glyph("moon")))

    def test_the_region_titles_name_what_the_delta_compares(self):
        lines = _lines(cap=self.cap)
        self.assertTrue(lines[2].endswith("vs previous 1h"), lines[2])
        self.assertTrue(lines[9].endswith("vs previous 1h"), lines[9])
        self.assertEqual(tui.HEADER_CELLS, _cells(lines[2]))
        self.assertEqual(tui.HEADER_CELLS, _cells(lines[9]))

    def test_the_empty_picture_row_explains_the_missing_picture(self):
        """§2.5: the drawing goes, the rows and a one-clause caption stay.

        A blank row under a blank picture reads as a rendering fault, so the row
        says which fact is missing and every region under it keeps its place.
        """
        lines = _lines("slow", _empty(), None, self.cap)
        self.assertEqual("", lines[6], "an empty picture is not a shape")
        self.assertEqual(" " + tui.NO_PICTURE_CAPTION, lines[7])
        self.assertTrue(tui.dashboard_ok_row(lines[7]), lines[7])
        self.assertEqual(tui.RULE, lines[8])

    def test_an_unknown_answer_is_a_caller_bug(self):
        with self.assertRaises(KeyError):
            _lines("prose", cap=self.cap)


# ---- (c) the kill list: the rows that left the dashboard -------------------
class TheKilledRows(unittest.TestCase):
    """§6: every fact below survives in `analyze`, `capacity` or `explain`.

    The dashboard is nineteen rows, so a fact that cannot change between two
    glances - a 30-day total, a rollup, a ledger's health - is in the report and
    not here. Each assertion names the row that used to carry it.
    """

    # The three answers that select a group, so every group's rows are read.
    ANSWERS = ("slow", "concurrency", "stuck")

    # What the account block and the metrics carry, and the screen must not say.
    BANNED = ("30 complete", "deferred", "denied after waiting", "overflow",
              "saturated", "available", "ledger", "request(s)", "n=",
              "queue wait", "details:", "press d", "press [f]",
              "last 30", "last 7d", "30d", "7d")

    def setUp(self):
        self.cap = _cap(_payload("capacity-2026-11-02.json"))
        self.metrics = _busy(slowest_request={
            "duration": "312.4s", "outcome": "client_idle_timeout",
            "headline": "The stream stopped without a finish_reason."})

    def test_no_row_carries_a_calendar_or_a_rollup(self):
        for answer in self.ANSWERS:
            for line in _lines(answer, self.metrics, _busy_before(), self.cap):
                for word in self.BANNED:
                    self.assertNotIn(word, line, f"{answer}: {line!r}")

    def test_no_row_names_an_api_field_or_a_backtick(self):
        banned = ("rate_limit_error", "scheduler admission timeout",
                  "client_idle_timeout", "org_concurrency_limit",
                  "admission_timeout", "starved_peer_evidence", "`")
        for answer in self.ANSWERS:
            for line in _lines(answer, self.metrics, _busy_before(), self.cap):
                for word in banned:
                    self.assertNotIn(word, line, f"{answer}: {line!r}")

    def test_each_latency_tier_keeps_its_own_rows(self):
        """The tier split was one number's worth of information; the latency group
        carries a p50 and a p90 for each tier, and the labels say which is which."""
        lines = _lines("slow", self.metrics, _busy_before(), self.cap)
        latency = lines[10:14]
        self.assertEqual(["p50 pri", "p90 pri", "p50 bg", "p90 bg"],
                         [line[1:13].strip() for line in latency])
        self.assertIn("400s", latency[2], "the background tier's own p50")
        self.assertIn("520s", latency[3], "the background tier's own p90")

    def test_the_slow_tier_appears_only_where_a_refusal_explains_it(self):
        refusals = _lines("concurrency", self.metrics, _busy_before(), self.cap)
        self.assertIn("background", refusals[12])
        self.assertIn("demoted to the slow tier", refusals[12])

    def test_no_row_repeats_a_key_that_the_footer_renders(self):
        """§6.2 item 11: the Footer renders the bindings, so no row lists them."""
        drew = "\n".join(_lines("slow", self.metrics, _busy_before(), self.cap))
        for hint in ("[d]", "[c]", "[q]", "shows the fix", "the full report"):
            self.assertNotIn(hint, drew)

    def test_no_row_is_a_sentence(self):
        for answer in self.ANSWERS:
            for line in _lines(answer, self.metrics, _busy_before(), self.cap):
                self.assertFalse(line.rstrip().endswith((".", "!", "?")), line)

    def test_every_row_fits_the_budget(self):
        for answer in self.ANSWERS:
            for line in _lines(answer, self.metrics, _busy_before(), self.cap):
                self.assertLessEqual(_cells(line), tui.ROW_CELLS, line)
                self.assertTrue(tui.dashboard_ok_row(line), line)


# ---- (d) every null is unknown, never a zero ------------------------------
class EveryNullIsUnknown(unittest.TestCase):
    """config: null and a null `now` block. The rows say what is missing, and no
    field the payload nulled reads as a zero (§7 rule 8)."""

    def test_the_now_row_says_why_there_is_no_sample(self):
        lines = _lines("slow", _busy(), _busy_before(),
                       _cap(_degraded_payload()))
        now = [line for line in lines if line.strip().startswith("now")]
        self.assertEqual(1, len(now))
        self.assertIn("?", now[0])
        self.assertIn("no live sample", now[0])
        self.assertNotRegex(now[0], r"\d+/\d+")

    def test_no_row_renders_none_or_a_bare_zero(self):
        for line in _lines("slow", _busy(), _busy_before(),
                           _cap(_degraded_payload())):
            self.assertNotIn("None", line)
            self.assertNotRegex(line, r"(?<![\w.])0(?![\w.])",
                                f"a null became a zero: {line!r}")

    def test_a_missing_payload_says_the_lane_count_is_unknown(self):
        """§2.3: the honest degradation - the hour is measured, the wall is not."""
        lines = _lines("slow", _busy(), _busy_before(), None)
        self.assertIn("? lanes", lines[0])
        self.assertIn("lane count unknown", lines[4])
        self.assertIn("scale: this window", lines[7])

    def test_a_declared_plan_reaches_a_machine_with_no_payload(self):
        """--plan exists so the ◌ declared width can appear at the front door."""
        metrics = _busy(declared_plan="max")
        lines = _lines("slow", metrics, _busy_before(), None)
        self.assertIn("max", lines[0], "the header names the declared plan")
        self.assertIn("%s4 lanes" % badges.BADGE_DECLARED, lines[0])
        self.assertIn("Hold 4 in flight", lines[15], "the advice reads the declaration")
        self.assertTrue(lines[17].endswith("%s declared" % badges.BADGE_DECLARED),
                        lines[17])


# ---- the age of the dashboard's data, live --------------------------------
class TheAgeOfTheData(unittest.TestCase):
    """Row one dates itself, and keeps counting while the screen is open.

    A dashboard is a screenshot the moment it stops saying how old it is: the
    reader who looks up twenty minutes after a scan cannot tell the current
    number from a stale one without subtracting a clock. So the header's right
    cell is the age, and it is a function of two numbers the screen already
    holds - the scan's clock and the wall clock - so the tick that keeps it
    honest re-reads nothing.
    """

    def setUp(self):
        self.cap = _cap(_payload("capacity-2026-11-02.json"))

    def test_seconds_under_a_minute_then_whole_minutes(self):
        self.assertEqual("age 47s", tui.age_cell(1000.0, 1047.0))
        self.assertEqual("age 2m", tui.age_cell(1000.0, 1120.0))
        self.assertEqual("age 62m stale", tui.age_cell(1000.0, 4720.0),
                         "past an hour the unit stays minutes")

    def test_the_marker_arrives_one_second_past_the_freshness_window(self):
        """The window is the capacity freshness window, so the age and the `now`
        row's `?` badge turn together rather than disagreeing about freshness."""
        self.assertEqual(cli.CAPACITY_FRESH_S, tui.AGE_STALE_AFTER_S)
        window = cli.CAPACITY_FRESH_S
        self.assertEqual("age 15m", tui.age_cell(1000.0, 1000.0 + window))
        self.assertEqual("age 15m stale",
                         tui.age_cell(1000.0, 1001.0 + window))

    def test_a_scan_with_no_clock_has_no_age_to_print(self):
        self.assertEqual("", tui.age_cell(None, 2000.0))
        self.assertEqual("", tui.age_cell(True, 2000.0), "a bool is not a clock")

    def test_a_wall_clock_behind_the_scan_clamps_to_zero(self):
        self.assertEqual("age 0s", tui.age_cell(2000.0, 1990.0))

    def test_the_header_carries_the_age_in_its_spare_cells(self):
        lines = tui.dashboard_lines("slow", _busy(), _busy_before(), self.cap,
                                    _CLOCK + 120)
        self.assertTrue(lines[0].endswith("age 2m"), lines[0])
        self.assertEqual(19, len(lines), "the age lives in a row, it does not add one")

    def test_a_quiet_call_dates_the_scan_it_was_given(self):
        """No wall clock names no moment: the data is as old as its own scan."""
        lines = _lines("slow", cap=self.cap)
        self.assertTrue(lines[0].endswith("age 0s"), lines[0])

    def test_the_screen_never_outgrows_its_budget_while_it_ages(self):
        for age in (0, 47, 120, 3600):
            lines = tui.dashboard_lines("slow", _busy(), _busy_before(),
                                        self.cap, _CLOCK + age)
            self.assertLessEqual(_cells(lines[0]), tui.ROW_CELLS, lines[0])

    def test_the_now_row_loses_its_badge_once_the_sample_ages_out(self):
        """One threshold, two clocks: past it both cells move, and the header's
        marker arrives on the scan's own age."""
        payload = _payload("capacity-2026-11-02.json")
        payload["data"]["now"]["sampled_at"] = "2026-11-02T14:31:43.9Z"
        cap = _cap(payload)
        settled = tui.dashboard_lines("slow", _busy(), _busy_before(), cap,
                                      _CLOCK)
        stale = tui.dashboard_lines("slow", _busy(), _busy_before(), cap,
                                    _CLOCK + tui.AGE_STALE_AFTER_S + 60)
        self.assertIn(badges.BADGE_ACCOUNT, settled[3])
        self.assertNotIn(badges.BADGE_ACCOUNT, stale[3])
        self.assertIn("stale", stale[3])
        self.assertIn("stale", stale[0])

    def test_the_sample_goes_stale_before_the_scan_does(self):
        """A cached sample is older than the scan that read it, so it crosses the
        one threshold first: the row refuses the badge while the header still
        reads `age 3m` with no marker. The two cells never contradict each other,
        because they are not ageing the same number."""
        payload = _payload("capacity-2026-11-02.json")
        # Twelve minutes before the scan closes, as the real cache is: 14:20:43Z.
        payload["data"]["now"]["sampled_at"] = "2026-11-02T14:20:43.9Z"
        cap = _cap(payload)
        lines = tui.dashboard_lines("slow", _busy(), _busy_before(), cap,
                                    _CLOCK + 231)
        self.assertTrue(lines[0].endswith("age 3m"), lines[0])
        self.assertNotIn("stale", lines[0],
                         "the scan is three minutes old, not fifteen")
        self.assertNotIn(badges.BADGE_ACCOUNT, lines[3])
        self.assertIn(tui._BADGE_UNKNOWN, lines[3])
        self.assertIn("7/12", lines[3], "the numbers stay when the badge goes")
        self.assertIn("stale 15m", lines[3])


class TheAgeTicksOnTheScreen(unittest.TestCase):
    """The one cell that moves: one second, one repaint, no re-scan."""

    def test_a_tick_ages_the_header_and_leaves_every_other_row_alone(self):
        asyncio.run(self._drive())

    async def _drive(self):
        app = tui.TriageApp()
        cap = _cap(_payload("capacity-2026-11-02.json"))
        # The screen dates the frame it draws, so the wall clock is pinned for the
        # whole drive: a landing is `age 0s` only against the clock it landed on.
        with mock.patch.object(tui, "scan",
                               return_value=(_busy(), _busy_before(), cap)) as scan, \
                mock.patch.object(tui.time, "time", return_value=_CLOCK):
            async with app.run_test(size=(80, 24)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                opened = list(app.drawn)
                self.assertTrue(opened[0].endswith("age 0s"), opened[0])
                with mock.patch.object(tui.time, "time",
                                       return_value=_CLOCK + 125):
                    app._age_tick()
                    await pilot.pause()
                aged = list(app.drawn)
                self.assertEqual(1, scan.call_count, "a tick re-scanned the logs")
                self.assertTrue(aged[0].endswith("age 2m"), aged[0])
                self.assertEqual(opened[1:], aged[1:],
                                 "the age moved a row that has no age in it")
                self.assertIn("age 2m", str(app.query_one("#dashboard").render()))
                await pilot.press("q")

    @staticmethod
    async def _settle(pilot, ready) -> None:
        """Wait for the thread worker's result, bounded, so a hang fails loudly."""
        for _attempt in range(40):
            await pilot.pause(0.05)
            if ready():
                return


# ---- both pools on the now row --------------------------------------------
class BothPoolsOnTheNowRow(unittest.TestCase):
    """Priority and background are two pools, and the row shows both.

    The flat `now` fields count priority requests only: a request demoted to the
    background tier appears in the payload's own `background` block instead. One
    row answers "right now" for both, on one sample and one badge, and the null
    contract decides the three shapes - a measured cap, a closed pool, an unread
    cap - so a closed pool never reads as a saturated one and an unread cap never
    reads as zero.
    """

    def _now(self, cap):
        return tui.dashboard_lines("slow", _busy(), _busy_before(), cap)[3]

    def test_the_number_column_is_priority_and_the_clause_is_background(self):
        row = self._now(_cap(_payload("capacity-2026-11-02.json")))
        self.assertIn("7/12", row, "the priority pool keeps the number column")
        self.assertIn("background 0/12", row, "the fixture's pool is open and idle")
        self.assertRegex(row, r"sampled \d\d:\d\d[+-]\d\d:\d\d")

    def test_a_closed_pool_says_closed_and_never_saturated(self):
        row = self._now(_cap_with_background(
            {"in_use": 0, "limit": 0, "available": 0, "saturated": False}))
        self.assertIn("background 0 closed", row)
        self.assertNotIn("saturated", row)
        self.assertNotIn("0/0", row, "a closed pool is not a ratio")

    def test_an_unread_cap_is_unknown_and_not_a_closed_pool(self):
        row = self._now(_cap_with_background(
            {"in_use": 4, "limit": None, "available": None, "saturated": None}))
        self.assertIn("background 4 cap unknown", row)
        self.assertNotIn("closed", row)

    def test_a_pool_the_account_never_reported_is_unknown(self):
        row = self._now(_cap_with_background(None))
        self.assertIn("background ? not reported", row)
        self.assertNotIn("closed", row)

    def test_a_count_nobody_read_is_a_question_mark_not_a_zero(self):
        row = self._now(_cap_with_background(
            {"in_use": None, "limit": 12, "available": None, "saturated": None}))
        self.assertIn("background ?/12", row)

    def test_one_sample_wears_one_badge(self):
        """§4.1: both pools come from the same sample, so one `●` leads the row."""
        row = self._now(_cap(_payload("capacity-2026-11-02.json")))
        self.assertEqual(1, row.count(badges.BADGE_ACCOUNT), row)

    def test_the_two_null_shapes_stay_apart_on_the_row(self):
        closed = self._now(_cap_with_background({"in_use": 0, "limit": 0}))
        unread = self._now(_cap_with_background({"in_use": 0, "limit": None}))
        self.assertNotEqual(closed, unread)


# ---- every row on the fixed grid, in every state, with an aged scan -------
class TheGridHoldsInEveryState(unittest.TestCase):
    """The designed height, 78 cells, five states - and the age breaks neither.

    The age is a new cell in an existing row, so the contract that has to survive
    it is the old one: the geometry does not move, and no row outgrows the
    budget, however long the screen has been open. The height is the group's:
    LATENCY holds four rows and the other two hold three, which is the one place
    the screen is taller than the eighteen rows the mockups draw.
    """

    def _states(self):
        healthy_cap = _cap(_payload("capacity-2026-11-02.json"))
        return [
            ("over capacity", "slow", _busy(), _busy_before(), healthy_cap),
            ("healthy", "slow", _healthy(), _busy_before(), healthy_cap),
            ("refusals", "concurrency", _busy(), _busy_before(), healthy_cap),
            ("no traffic", "slow", _empty(), None, healthy_cap),
            ("no capacity", "slow", _busy(), _busy_before(), None),
            ("degraded", "slow", _busy(), _busy_before(),
             _cap(_degraded_payload())),
        ]

    def test_every_state_is_the_designed_height_inside_the_budget(self):
        for name, answer, metrics, previous, cap in self._states():
            for wall_now in (None, (metrics.get("scanned_at") or 0) + 3600):
                lines = tui.dashboard_lines(answer, metrics, previous, cap,
                                            wall_now)
                self.assertEqual(ROWS_FOR_ANSWER[answer], len(lines),
                                 f"{name}: {answer}")
                for line in lines:
                    self.assertLessEqual(_cells(line), tui.ROW_CELLS,
                                         f"{name}: {line!r}")
                    self.assertTrue(tui.dashboard_ok_row(line), f"{name}: {line!r}")

    def test_the_age_is_a_cell_and_not_a_row(self):
        lines = tui.dashboard_lines("slow", _busy(), _busy_before(), None)
        self.assertEqual(19, len(lines))
        self.assertEqual(1, sum(1 for line in lines if "age " in line))


# ---- the clock the screen speaks: this machine's timezone -----------------
class TheLocalClock(unittest.TestCase):
    """Timestamps print local time with one offset, never a bare UTC minute.

    The logs and the account API both speak UTC, so the offset is the fact that
    lets a reader compare this screen to them; a `Z` suffix silently claims the
    machine is on UTC and is wrong for most of them.
    """

    def test_the_format_is_minutes_plus_the_offset(self):
        epoch = tui._minute_epoch("2026-11-02T14:04Z")
        self.assertRegex(tui.local_hhmm(epoch), r"^\d\d:\d\d[+-]\d\d:\d\d$")

    def test_a_known_zone_prints_its_own_offset(self):
        previous = os.environ.get("TZ")
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        self.addCleanup(self._restore_zone, previous)
        self.assertEqual("06:04-08:00",
                         tui.local_hhmm(tui._minute_epoch("2026-11-02T14:04Z")))

    @staticmethod
    def _restore_zone(previous):
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()

    def test_no_row_claims_utc(self):
        lines = tui.dashboard_lines("slow", _busy(), _busy_before(),
                                    _cap(_payload("capacity-2026-11-02.json")))
        for line in lines:
            self.assertNotRegex(line, r"\d\d:\d\dZ", f"a UTC minute survived: {line!r}")

    def test_a_stamp_nobody_can_read_prints_nothing(self):
        self.assertEqual("", tui.local_hhmm(None))
        self.assertEqual("", tui.local_hhmm("2026-11-02T14:04Z"))
        self.assertEqual("", tui._minute_of_day(None))


# ---- (e) an empty window says what is missing -----------------------------
class TheEmptyWindow(unittest.TestCase):
    """No traffic in the window: the rows say `?` with a reason, never a number."""

    def setUp(self):
        self.cap = _cap(_payload("capacity-2026-11-02.json"))
        self.lines = _lines("slow", _empty(), None, self.cap)

    def test_the_peak_and_blocked_rows_print_the_unknown_glyph(self):
        peak, blocked = self.lines[4], self.lines[5]
        self.assertTrue(peak.startswith(" peak 1h"), peak)
        self.assertIn("%s " % tui._BADGE_UNKNOWN, peak)
        self.assertIn("nothing measured here", peak)
        self.assertIn(tui._BADGE_UNKNOWN, blocked)
        self.assertNotRegex(peak, r"\d+$", "a peak nobody measured quotes no number")

    def test_the_latency_rows_say_what_is_missing(self):
        tier_rows = self.lines[10:14]
        self.assertEqual(["p50 pri", "p90 pri", "p50 bg", "p90 bg"],
                         [line[1:13].strip() for line in tier_rows])
        for line in tier_rows:
            self.assertIn(tui._BADGE_UNKNOWN, line)
            self.assertNotRegex(line, r"\d+s", "no percentile was measured here")

    def test_the_next_row_widens_the_window_instead_of_naming_a_number(self):
        self.assertIn("Nothing measured in the last hour", self.lines[15])
        self.assertIn("--since 6h", self.lines[16])


# ---- (f) the stalls group carries the slowest digest ----------------------
class TheSlowestDigest(unittest.TestCase):
    """The stuck answer reaches the explain --slowest panel.

    One row of the stalls group carries that request's own why - not the whole
    panel, which stays in `luna-doctor explain`.
    """

    def test_the_digest_uses_the_explain_machinery(self):
        """One digest, from explain's own selectors: nothing here re-derives a
        reason from the metrics."""
        doc = {"headline": "The stream stopped without a finish_reason.",
               "rows": [("request", "pi/abc"), ("duration", "312.4s")],
               "actions": [], "outcome": "client_idle_timeout", "room": True}
        with mock.patch.object(tui.explain, "slowest", return_value=["req"]) as slow, \
                mock.patch.object(tui.explain, "explain", return_value=doc):
            digest = tui.slowest_request_digest(["req"], _metrics())
        self.assertEqual({"duration": "312.4s", "outcome": "client_idle_timeout",
                          "headline": "The stream stopped without a finish_reason."},
                         digest)
        slow.assert_called_once_with(["req"], 1)

    def test_the_digest_is_none_without_requests_or_on_failure(self):
        self.assertIsNone(tui.slowest_request_digest([], _metrics()))
        with mock.patch.object(tui.explain, "slowest", side_effect=RuntimeError("boom")):
            self.assertIsNone(tui.slowest_request_digest(["req"], _metrics()))

    def test_the_stalls_group_carries_the_duration_and_what_happened(self):
        metrics = _busy(slowest_request={
            "duration": "312.4s", "outcome": "client_idle_timeout",
            "headline": "The stream stopped without a finish_reason."})
        slowest = tui.dashboard_lines("stuck", metrics, _busy_before(), None)[11]
        self.assertIn("312.4s", slowest)
        self.assertIn("the idle budget killed it", slowest)

    def test_an_unranked_row_says_why_rather_than_a_bare_question_mark(self):
        """A digest that is None may be an explain that raised: the row states
        that nothing was ranked, never why."""
        slowest = tui.dashboard_lines("stuck", _busy(), _busy_before(), None)[11]
        self.assertIn("nothing ranked", slowest)
        self.assertIn("?", slowest)


# ---- the app shell: no terminal needed to check the contract -------------
class TriageAppSurface(unittest.TestCase):
    """The app shell, and the bindings the Footer renders."""

    def test_the_app_takes_a_plan_and_an_online_flag(self):
        app = tui.TriageApp(plan="apex", online=False)
        self.assertEqual("apex", app.plan)
        self.assertFalse(app.online)


# ---- the help overlay -----------------------------------------------------
class TheHelpOverlay(unittest.TestCase):
    """`[?]` opens one overlay: every key, every mark, and the account.

    The dashboard carries one imperative and two gloss lines on purpose (§7 rule
    2), so the rest of the vocabulary lives here. It is a modal screen, which is
    what lets the rows underneath stay exactly where they were.
    """

    # Textual's own key name for the glyph on the cap.
    DISPLAY = {"question_mark": "?"}

    def setUp(self):
        self.cap = _cap(_payload("capacity-2026-11-02.json"))
        self.lines = tui.help_lines(_busy(), self.cap)
        self.text = "\n".join(self.lines)

    def test_every_binding_is_named_with_its_own_line(self):
        keys = [line.strip().split()[0] for line in self.lines
                if line.startswith("  ") and not line.startswith("   ")]
        for binding in tui.TriageApp.BINDINGS:
            key = self.DISPLAY.get(binding.key, binding.key)
            self.assertIn(key, keys[:len(tui.HELP_KEYS)],
                          f"{key} is not in the help: {self.text!r}")
        self.assertEqual([key for key, _what in tui.HELP_KEYS],
                         [self.DISPLAY.get(b.key, b.key) for b in tui.TriageApp.BINDINGS])

    def test_every_row_fits_the_dashboards_budget(self):
        for line in self.lines:
            self.assertLessEqual(_cells(line), tui.ROW_CELLS, line)

    def test_the_marks_are_spelled_out_further_than_the_gloss_line(self):
        self.assertIn("the account reported it", self.text)
        self.assertIn("measured on this machine", self.text)
        self.assertIn("not established", self.text,
                      "the plain moon is explained even when it is not on screen")
        for name in (report.MOON_FULL, report.MOON_QUARTER, report.MOON_NEW):
            self.assertIn(tui.moon_glyph(name), self.text)

    def test_one_row_says_what_the_drill_downs_run(self):
        drill = [line for line in self.lines if line.strip().startswith("d runs")]
        self.assertEqual(1, len(drill), self.text)
        for command in ("analyze", "chart", "fix"):
            self.assertIn(command, drill[0])

    def test_the_account_section_is_the_accounts_own_facts(self):
        account = self.lines[self.lines.index(" ACCOUNT") + 1:]
        # One grid, like a data row: two cells of indent, a 16-cell label, a value.
        facts = {line[2:18].strip(): line[19:].strip() for line in account}
        self.assertEqual("apex", facts["plan"])
        self.assertEqual("%s12" % badges.BADGE_ACCOUNT, facts["priority lanes"])
        self.assertEqual("12", facts["background lanes"])
        self.assertEqual("enabled", facts["overflow routing"])
        self.assertTrue(facts["cache age"], "the cache's age is the account's too")

    def test_an_account_nobody_cached_says_so_on_every_row(self):
        account = tui.help_lines({}, None)
        section = account[account.index(" ACCOUNT") + 1:]
        for line in section:
            self.assertIn("?", line, f"a fact nobody reported is not a number: {line!r}")

    def test_the_section_labels_are_the_dashboards_own_regions(self):
        labels = [line.strip() for line in self.lines if line.strip() in tui.HELP_SECTIONS]
        self.assertEqual(list(tui.HELP_SECTIONS), labels)

    def test_the_question_mark_both_opens_and_closes_it(self):
        asyncio.run(self._drive(toggle=True))

    def test_escape_closes_it_and_gives_the_same_screen_back(self):
        asyncio.run(self._drive(toggle=False))

    async def _drive(self, *, toggle):
        app = tui.TriageApp()
        cap = _cap(_payload("capacity-2026-11-02.json"))
        with mock.patch.object(tui, "scan",
                               return_value=(_busy(), _busy_before(), cap)) as scan, \
                mock.patch.object(tui.time, "time", return_value=_CLOCK):
            async with app.run_test(size=(80, 24)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                before = self._dashboard(app)
                await pilot.press("question_mark")
                await self._settle(pilot, lambda: self._help_text(app))
                self.assertIsInstance(app.screen, tui.HelpScreen)
                self.assertIn("KEYS", self._help_text(app))
                self.assertIn("ACCOUNT", self._help_text(app))
                await pilot.press("question_mark" if toggle else "escape")
                await self._settle(pilot,
                                   lambda: not isinstance(app.screen, tui.HelpScreen))
                self.assertEqual(before, self._dashboard(app),
                                 "the overlay did not hand the same screen back")
                self.assertEqual(1, scan.call_count, "closing the help re-scanned")
                self.assertIn("help", self._footer(app))
                await pilot.press("q")

    @staticmethod
    def _help_text(app) -> str:
        """The overlay's own words, "" until the screen has composed."""
        drawn = app.screen.query("#help-text")
        return str(drawn.first().render()) if drawn else ""

    @staticmethod
    def _dashboard(app) -> list:
        return str(app.query_one("#dashboard").render()).splitlines()

    @staticmethod
    def _footer(app) -> str:
        return "\n".join(row for row in _painted(app) if "quit" in row)

    @staticmethod
    async def _settle(pilot, ready) -> None:
        """Wait for the thread worker's result, bounded, so a hang fails loudly."""
        for _attempt in range(40):
            await pilot.pause(0.05)
            if ready():
                return


# ---- colour: severity, and nothing else -----------------------------------
class TheSeverityColours(unittest.TestCase):
    """The moons carry their terminal colour, and the plain text does not move.

    Colour is redundancy here, never meaning: every coloured mark is a glyph that
    already says the same thing, the width contract is measured on the uncoloured
    rows, and Textual's own Mono filter answers NO_COLOR.
    """

    def setUp(self):
        self.cap = _cap(_payload("capacity-2026-11-02.json"))
        self.lines = tui.dashboard_lines("slow", _busy(), _busy_before(), self.cap)

    def test_the_three_moons_are_the_three_colours(self):
        self.assertEqual({report.MOON_FULL, report.MOON_QUARTER, report.MOON_NEW},
                         set(tui.SEVERITY_COLORS))

    def test_the_styled_screen_is_the_plain_screen_character_for_character(self):
        styled = tui.styled_lines(self.lines)
        self.assertEqual("\n".join(self.lines), str(styled))
        self.assertEqual(_cells("\n".join(self.lines)), _cells(str(styled)))
        self.assertEqual(19, len(str(styled).splitlines()))

    def test_the_status_word_wears_its_moons_colour(self):
        name = tui.header_moon(_busy())
        word = tui.status_word(name)
        start = self.lines[0].index(word)
        self.assertIn((start, start + len(word), tui._severity_style(name)),
                      tui._line_spans(self.lines[0]))

    def test_each_region_title_wears_its_own_regions_colour(self):
        for index in (2, 9):
            line = self.lines[index]
            name = tui._glyph_name_at(line, len(line.split()[0]) + 1)
            self.assertTrue(name, line)
            self.assertIn((0, len(line.split()[0]) + 1 + len(tui.moon_glyph(name)),
                           tui._severity_style(name)), tui._line_spans(line))

    def test_the_at_or_over_run_is_the_one_coloured_mark_in_the_picture(self):
        spans = tui._line_spans(self.lines[6])
        coloured = {index for start, _end, _style in spans for index in [start]
                    if self.lines[6][start] == "█"}
        self.assertTrue(coloured, "the picture drew no at-or-over cell")
        self.assertEqual({index for index, char in enumerate(self.lines[6])
                          if char == "█"}, coloured)

    def test_no_colour_ever_lands_on_a_data_row_number(self):
        """The rows stay neutral: severity is a region fact, not a row fact."""
        for index in (3, 4, 5, 10, 11, 12):
            self.assertEqual([], tui._line_spans(self.lines[index]), self.lines[index])

    def test_a_mono_terminal_loses_the_colour_and_nothing_else(self):
        asyncio.run(self._drive())

    async def _drive(self):
        previous = os.environ.get("NO_COLOR")
        os.environ["NO_COLOR"] = "1"
        self.addCleanup(self._restore_no_color, previous)
        app = tui.TriageApp()
        with mock.patch.object(tui, "scan",
                               return_value=(_busy(), _busy_before(), self.cap)), \
                mock.patch.object(tui.time, "time", return_value=_CLOCK):
            async with app.run_test(size=(80, 24)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                self.assertTrue(app.no_color, "Textual did not take the NO_COLOR filter")
                # The Footer is composed a frame after the rows land, so wait for
                # it rather than counting whatever the first frame happened to hold.
                await self._settle(pilot, lambda: any("quit" in row for row in _painted(app)))
                painted = [row for row in _painted(app) if row.strip()]
                self.assertEqual(20, len(painted), "the mono screen lost a row")
                self.assertEqual(self.lines, self._dashboard(app))
                await pilot.press("q")

    @staticmethod
    def _restore_no_color(previous):
        if previous is None:
            os.environ.pop("NO_COLOR", None)
        else:
            os.environ["NO_COLOR"] = previous

    @staticmethod
    def _dashboard(app) -> list:
        return str(app.query_one("#dashboard").render()).splitlines()

    @staticmethod
    async def _settle(pilot, ready) -> None:
        for _attempt in range(40):
            await pilot.pause(0.05)
            if ready():
                return


class TriageAppKeysOnTheFooter(unittest.TestCase):
    """The footer renders `BINDINGS`: the keys are the screen's whole vocabulary."""

    def test_the_footer_renders_every_key_with_a_description(self):
        """§8 item 5: the Footer renders `BINDINGS`, and every binding has a word."""
        bindings = [(binding.key, binding.description)
                    for binding in tui.TriageApp.BINDINGS]
        self.assertEqual(["f", "d", "c", "g", "w", "question_mark", "q"],
                         [key for key, _word in bindings])
        for key, word in bindings:
            self.assertTrue(word.strip(), key)

    def test_run_triage_runs_the_app_and_returns_zero(self):
        with mock.patch.object(tui.TriageApp, "run", return_value=None) as run:
            self.assertEqual(0, tui.run_triage(plan=None, online=False))
        self.assertEqual(1, run.call_count)

    def test_online_refreshes_the_cache_once_before_the_screen_opens(self):
        with mock.patch.object(tui.sources, "fetch_capacity", return_value=None) as fetch, \
                mock.patch.object(tui.TriageApp, "run", return_value=None):
            tui.run_triage(plan="apex", online=True)
        fetch.assert_called_once_with(allow_network=True)

    def test_the_scan_reads_the_capacity_cache_only(self):
        with mock.patch.object(tui.cli, "collect", return_value=[]), \
                mock.patch.object(tui.cli, "analyse", side_effect=lambda reqs, hours: {}), \
                mock.patch.object(tui.cli, "_capacity_metrics",
                                  return_value={}) as metrics_block, \
                mock.patch.object(tui.sources, "fetch_capacity", return_value=None) as fetch:
            metrics, previous, cap = tui.scan(plan="apex")
        fetch.assert_called_once_with(allow_network=False)
        self.assertIs(False, metrics_block.call_args.kwargs.get("allow_network"),
                      "the CLI's capacity block is read cache-only too")
        self.assertEqual("apex", metrics["declared_plan"])
        self.assertEqual({}, previous)
        self.assertIsNone(cap)


# ---- the window: one hour, and the hour before it ------------------------
class TheWindowAndItsPredecessor(unittest.TestCase):
    """The window is one hour, [w] cycles it, and scan() reads the window before
    this one so a delta column has a window to compare against."""

    def test_the_dashboard_scans_one_hour(self):
        self.assertEqual(1, tui.WINDOW_HOURS)

    def test_the_window_cycles_one_six_and_twentyfour(self):
        self.assertEqual(6, tui.window_cycle(1))
        self.assertEqual(24, tui.window_cycle(6))
        self.assertEqual(1, tui.window_cycle(24))

    def test_a_window_outside_the_cycle_falls_back_to_one_hour(self):
        self.assertEqual(1, tui.window_cycle(0.5))
        self.assertEqual(1, tui.window_cycle(None))

    def test_scan_returns_the_window_the_one_before_it_and_the_capacity(self):
        reads, analysed = [], []

        def collect(hours, harnesses, pi_roots, claude_roots, now=None):
            reads.append((hours, now))
            return []

        def analyse(reqs, hours):
            analysed.append(hours)
            return {"window_hours": hours}

        now = 100000.0
        with mock.patch.object(tui.cli, "collect", side_effect=collect), \
                mock.patch.object(tui.cli, "analyse", side_effect=analyse), \
                mock.patch.object(tui.cli, "_capacity_metrics", return_value={}), \
                mock.patch.object(tui.sources, "fetch_capacity", return_value=None), \
                mock.patch.object(tui.time, "time", return_value=now):
            metrics, previous, cap = tui.scan(plan="apex", hours=6)

        _start, end = baseline.previous_window(now, 6)
        self.assertEqual(now - 6 * 3600, end,
                         "the previous window ends where this one starts")
        self.assertEqual([(6, now), (6, end)], reads,
                         "one extra read, of the window immediately before")
        self.assertEqual([6, 6], analysed)
        self.assertEqual(6, metrics["window_hours"])
        self.assertEqual(6, previous["window_hours"])
        self.assertIsNone(cap)

    def test_the_previous_window_is_baselines_own_pairing(self):
        """No second comparison: the span comes from baseline, which tests it."""
        now, hours = 500000.0, 1
        with mock.patch.object(tui.cli, "collect", return_value=[]), \
                mock.patch.object(tui.cli, "analyse", return_value={}) as analyse, \
                mock.patch.object(tui.baseline, "previous_window",
                                  wraps=baseline.previous_window) as pairing:
            tui.previous_window_metrics(now, hours)
        pairing.assert_called_once_with(now, hours)
        self.assertEqual(hours, analyse.call_args.args[1])


class TheWindowKeyOnTheScreen(unittest.TestCase):
    """[w] cycles the window and re-reads the logs: a label never outruns its data."""

    def test_w_cycles_the_window_and_rescans_it(self):
        asyncio.run(self._drive())

    async def _drive(self):
        app = tui.TriageApp()
        seen = []

        def fake_scan(plan=None, hours=tui.WINDOW_HOURS):
            seen.append(hours)
            return _metrics(window_hours=hours), None, None

        with mock.patch.object(tui, "scan", side_effect=fake_scan):
            async with app.run_test(size=(100, 40)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                self.assertEqual([1], seen, "the screen opens on one hour")
                await pilot.press("w")
                await self._settle(pilot, lambda: len(seen) == 2)
                self.assertEqual(6, app.window_hours)
                self.assertEqual(6, app.metrics["window_hours"])
                self.assertIn("peak 6h", self._dashboard(app))
                await pilot.press("w")
                await self._settle(pilot, lambda: len(seen) == 3)
                self.assertEqual(24, app.window_hours)
                self.assertEqual(24, app.metrics["window_hours"])
                self.assertIn("peak 24h", self._dashboard(app))
                await pilot.press("q")

    @staticmethod
    def _dashboard(app) -> str:
        return str(app.query_one("#dashboard").render())

    @staticmethod
    async def _settle(pilot, ready) -> None:
        """Wait for the thread worker's result, bounded, so a hang fails loudly."""
        for _attempt in range(40):
            await pilot.pause(0.05)
            if ready():
                return


class TheGroupKeyOnTheScreen(unittest.TestCase):
    """[g] steps through the groups: latency -> models -> refusals -> stalls.

    The answer list is only up while the scan runs, so this is how a person asks
    for another group afterwards, and it is the only way to the per-model
    breakdown. The groups are a ring: every row above the group keeps its place,
    and the screen's height is the height of the group on screen - four tier rows
    for LATENCY, three for the other three.
    """

    def test_next_group_rings_the_evidence_groups(self):
        seen, group = [], tui.EVIDENCE_GROUPS[0]
        for _step in range(len(tui.EVIDENCE_GROUPS)):
            seen.append(group)
            group = tui.next_group(group)
        self.assertEqual(list(tui.EVIDENCE_GROUPS), seen, "the ring is the ranked order")
        self.assertEqual(tui.EVIDENCE_GROUPS[0], group, "the ring closes")
        self.assertEqual(tui.EVIDENCE_GROUPS[0], tui.next_group("prose"),
                         "a group the dashboard does not offer lands on the first")

    def test_g_cycles_the_group_and_each_group_keeps_its_height(self):
        asyncio.run(self._drive())

    async def _drive(self):
        app = tui.TriageApp()
        cap = _cap(_payload("capacity-2026-11-02.json"))
        # The rows above the group are dated against the wall clock each time they
        # are drawn, so the clock is pinned here: without it `[g]` pressed twenty
        # minutes in would re-age the header and this comparison would be about
        # the clock rather than about the group.
        with mock.patch.object(tui, "scan",
                               return_value=(_busy(), _busy_before(), cap)), \
                mock.patch.object(tui.time, "time", return_value=_CLOCK):
            async with app.run_test(size=(80, 24)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                opened = self._dashboard(app)
                self.assertTrue(opened[9].startswith("LATENCY "), opened[9])
                self.assertEqual(ROWS_FOR_GROUP["latency"], len(opened))
                titles = []
                for _press in range(4):
                    await pilot.press("g")
                    await pilot.pause()
                    drawn = self._dashboard(app)
                    title = drawn[9].split()[0].lower()
                    self.assertEqual(ROWS_FOR_GROUP[title], len(drawn),
                                     "the group on screen decides the height")
                    self.assertEqual(opened[:9], drawn[:9],
                                     "a region above the group moved")
                    if title == "models":
                        # The group `[g]` exists for: one row per model, the
                        # model's name in the label cell and its p90 in the clause.
                        self.assertIn(" glm-5.3", drawn[10], drawn[10])
                        self.assertIn("p90 240s", drawn[10], drawn[10])
                        self.assertIn("400s", drawn[11], drawn[11])
                    titles.append(title.upper())
                self.assertEqual(["MODELS", "REFUSALS", "STALLS", "LATENCY"],
                                 titles)
                painted = [row for row in _painted(app) if row.strip()]
                self.assertEqual(ROWS_FOR_GROUP["latency"] + 1, len(painted),
                                 "the drawn rows and the Footer under them")
                await pilot.press("q")

    @staticmethod
    def _dashboard(app) -> list:
        return str(app.query_one("#dashboard").render()).splitlines()

    @staticmethod
    async def _settle(pilot, ready) -> None:
        """Wait for the thread worker's result, bounded, so a hang fails loudly."""
        for _attempt in range(40):
            await pilot.pause(0.05)
            if ready():
                return


class TriageAppFailure(unittest.TestCase):
    """A failed scan must not be reported as a scan still in progress."""

    def test_a_failed_scan_is_never_reported_as_still_running(self):
        asyncio.run(self._drive())

    async def _drive(self):
        app = tui.TriageApp()
        with mock.patch.object(tui, "scan", side_effect=RuntimeError("boom")):
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause()
                await pilot.pause()
                self.assertEqual("RuntimeError: boom", app.scan_failed)
                self.assertIn("the scan failed", self._status(app))
                await pilot.press("enter")
                await pilot.pause()
                self.assertIn("the scan failed", self._status(app))
                self.assertNotIn("still running", self._status(app))
                await pilot.press("f")
                await pilot.pause()
                self.assertIn("the scan failed", self._status(app))
                self.assertNotIn("still running", self._status(app))
                await pilot.press("q")

    @staticmethod
    def _status(app) -> str:
        return str(app.query_one("#status").render())


class TheDashboardOnTheScreen(unittest.TestCase):
    """The scan lands as the dashboard, and the answer list stands down.

    §8 item 6: the header, LOAD and the default evidence group are on screen the
    moment the scan finishes. The six answers choose a group; they do not gate
    the numbers, and they do not keep a list of their own on a nineteen-row
    screen.
    """

    def test_the_scan_lands_as_the_designed_rows_and_the_footer(self):
        asyncio.run(self._drive())

    async def _drive(self):
        app = tui.TriageApp()
        cap = _cap(_payload("capacity-2026-11-02.json"))
        with mock.patch.object(tui, "scan",
                               return_value=(_busy(), _busy_before(), cap)):
            async with app.run_test(size=(80, 24)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                drawn = self._dashboard(app)
                self.assertEqual(ROWS_FOR_ANSWER[tui.DEFAULT_CHOICE],
                                 len(drawn.splitlines()), drawn)
                self.assertTrue(drawn.splitlines()[0].startswith(
                    tui.moon_glyph(tui.header_moon(_busy()))))
                self.assertIn("LATENCY", drawn)
                self.assertEqual(0, app.query_one("#ask").size.height,
                                 "the answer list kept a column of its own")
                footer = app.query_one("Footer")
                self.assertEqual(1, footer.size.height)
                await pilot.press("q")

    def test_a_window_with_no_active_minute_draws_no_picture(self):
        asyncio.run(self._drive_quiet())

    async def _drive_quiet(self):
        app = tui.TriageApp()
        with mock.patch.object(tui, "scan", return_value=(_empty(), None, None)):
            async with app.run_test(size=(80, 24)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                lines = self._dashboard(app).splitlines()
                self.assertEqual("", lines[6], "an empty picture is not a shape")
                self.assertIn("no picture", lines[7],
                              "the empty picture's row says what is missing")
                self.assertIn("NOT ESTABLISHED", lines[0])
                await pilot.press("q")

    @staticmethod
    def _dashboard(app) -> str:
        return str(app.query_one("#dashboard").render())

    @staticmethod
    async def _settle(pilot, ready) -> None:
        """Wait for the thread worker's result, bounded, so a hang fails loudly."""
        for _attempt in range(40):
            await pilot.pause(0.05)
            if ready():
                return


class EverySlowStepSaysItIsRunning(unittest.TestCase):
    """The owner's rule: every surface that can sit for a second or more shows
    activity first. The scan says so on mount, the fix says so before it runs,
    and the one capacity fetch that happens before the screen exists says so on
    the terminal it is about to replace."""

    def test_the_fix_sets_the_status_before_it_starts_working(self):
        asyncio.run(self._drive())

    async def _drive(self):
        app = tui.TriageApp()
        computing, release = threading.Event(), threading.Event()

        def slow_fix(plan=None, hours=tui.WINDOW_HOURS):
            # the worker is inside the fix, and the screen must already say so
            computing.set()
            release.wait(5)
            return "fix output"

        with mock.patch.object(tui, "scan", return_value=(_busy(), _busy_before(), _cap(_payload("capacity-2026-11-02.json")))), \
                mock.patch.object(tui, "fix_output", side_effect=slow_fix):
            async with app.run_test(size=(100, 40)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                await pilot.press("f")
                for _attempt in range(40):
                    await pilot.pause(0.05)
                    if computing.is_set():
                        break
                self.assertTrue(computing.is_set(), "the fix never started")
                status = str(app.query_one("#status").render())
                self.assertIn("rendering the fix output", status)
                self.assertNotIn("is on screen", status,
                                 "the screen still offered the previous action, not the work")
                release.set()
                await self._settle(pilot, lambda: "fix output" in str(app.query_one("#detail-text").render()))
                await pilot.press("q")

    def test_the_capacity_refresh_before_the_screen_says_so_first(self):
        events = []
        with mock.patch.object(tui.cli, "notice",
                               side_effect=lambda message: events.append(("notice", message))), \
                mock.patch.object(tui.sources, "fetch_capacity",
                                  side_effect=lambda **kw: events.append(("fetch", kw))), \
                mock.patch.object(tui.TriageApp, "run", return_value=None):
            tui.run_triage(plan=None, online=True)
        self.assertEqual("notice", events[0][0],
                         "the refresh began with no word on the terminal")
        self.assertIn("capacity", events[0][1])
        self.assertEqual("fetch", events[1][0])

    def test_a_cache_only_screen_refresh_prints_nothing(self):
        events = []
        with mock.patch.object(tui.cli, "notice",
                               side_effect=lambda message: events.append(message)), \
                mock.patch.object(tui.TriageApp, "run", return_value=None):
            tui.run_triage(plan=None, online=False)
        self.assertEqual([], events, "an offline run has no wait to excuse")

    @staticmethod
    async def _settle(pilot, ready) -> None:
        """Wait for the thread worker's result, bounded, so a hang fails loudly."""
        for _attempt in range(40):
            await pilot.pause(0.05)
            if ready():
                return


class TriageAppLiteralText(unittest.TestCase):
    """Bracketed text on this screen is prose, never Rich markup.

    The detail panel prints a command's own output, the fix block may name a
    tier as [agents], and a failure quotes the CLI's own last line. Parsed as
    markup, the brackets and what they hold vanish, so the screen tells a reader
    to press nothing.
    """

    FIX = "run `luna-doctor fix --tier [agents]`, then press [f] again"

    def test_bracketed_prose_survives_the_detail_panel_and_the_status_line(self):
        asyncio.run(self._drive())

    async def _drive(self):
        app = tui.TriageApp()
        cap = _cap(_payload("capacity-2026-11-02.json"))
        failed = (2, "luna-doctor: bad --since value: 'nope' [agents]")
        with mock.patch.object(tui, "scan", return_value=(_busy(), _busy_before(), cap)), \
                mock.patch.object(tui, "fix_output", return_value=self.FIX), \
                mock.patch.object(tui, "detail_output", return_value=failed):
            async with app.run_test(size=(100, 40)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                await pilot.press("f")
                await self._settle(pilot, lambda: "[agents]" in self._text(app, "#detail-text"))
                self.assertIn("[agents]", self._text(app, "#detail-text"))
                self.assertIn("--tier [agents]", self._text(app, "#detail-text"))
                await pilot.press("d")
                await self._settle(pilot, lambda: "command failed" in self._text(app, "#status"))
                self.assertIn("[agents]", self._text(app, "#status"))
                await pilot.press("q")

    @staticmethod
    async def _settle(pilot, ready) -> None:
        """Wait for the thread worker's result, bounded, so a hang fails loudly."""
        for _attempt in range(40):
            await pilot.pause(0.05)
            if ready():
                return

    @staticmethod
    def _text(app, selector) -> str:
        return str(app.query_one(selector).render())


# ---- the picture: the window's shape, re-binned -------------------------
class ConcurrencySparkline(unittest.TestCase):
    """by_minute is sparse, so the row is re-binned onto uniform columns."""

    def test_sparse_minutes_are_rebinned_onto_uniform_columns(self):
        spark = tui.concurrency_sparkline(_scan_of([(0, 2), (720, 9)]), width=4)
        self.assertEqual(4, len(spark))
        self.assertNotEqual(" ", spark[0])
        self.assertEqual(" ", spark[1], "an empty column is a space, not a low bar")
        self.assertEqual("\u2588", spark[2], "12h later lands in the third column")
        self.assertEqual(" ", spark[3])

    def test_a_minute_before_the_window_is_not_clamped_into_column_zero(self):
        spark = tui.concurrency_sparkline(_scan_of([(-120, 17), (600, 4)]), width=4)
        self.assertEqual(" ", spark[0],
                         "a minute outside the window is not this window's traffic")
        self.assertNotEqual(" ", spark[1])

    def test_the_busiest_minute_inside_a_column_is_the_column(self):
        spark = tui.concurrency_sparkline(_scan_of([(0, 2), (10, 9)]), width=2)
        self.assertEqual("\u2588 ", spark)

    def test_a_quiet_window_draws_blanks_not_low_bars(self):
        pairs = [(minute, 17) for minute in range(1400, 1440, 5)]
        spark = tui.concurrency_sparkline(_scan_of(pairs), width=12)
        self.assertEqual(" " * 11, spark[:11], "nothing ran there, so nothing is drawn")
        self.assertNotEqual(" ", spark[11])

    def test_a_dense_window_fills_the_ramp(self):
        pairs = [(minute, level) for level, minute in
                 enumerate(range(0, 1440, 120), start=1)]
        spark = tui.concurrency_sparkline(_scan_of(pairs), width=12)
        self.assertNotIn(" ", spark)
        self.assertEqual("\u2581", spark[0])
        self.assertEqual("\u2588", spark[-1])
        levels = [tui._SPARK_BLOCKS.index(char) for char in spark]
        self.assertEqual(sorted(levels), levels, "the ramp only goes up here")
        self.assertGreaterEqual(len(set(spark)), 4, "the ramp is not two heights")

    def test_the_window_closes_at_the_scans_clock(self):
        """Activity from 22h ago stays at the left, where it happened. Closing
        the window on the last active minute instead would draw it at the
        right edge."""
        spark = tui.concurrency_sparkline(_scan_of([(0, 5), (20, 17)]), width=12)
        self.assertEqual(" " * 11, spark[1:])
        self.assertNotEqual(" ", spark[0])

    def test_without_a_scan_clock_the_window_ends_at_the_last_active_minute(self):
        spark = tui.concurrency_sparkline(
            _metrics(by_minute=_by_minute([(0, 5)])), width=4)
        self.assertEqual(4, len(spark))
        self.assertEqual(1, sum(char != " " for char in spark))
        self.assertNotEqual(" ", spark[-1])

    def test_the_caption_says_which_scale_the_row_is_drawn_on(self):
        self.assertEqual("scale: this window · peak at %s" % _local("2026-11-02T14:04Z"),
                         tui.spark_caption(_busy_hour()))
        self.assertEqual("█ = at or over ●12 lanes · over 7m of 60m · peak at %s"
                         % _local("2026-11-02T14:04Z"),
                         tui.spark_caption(_busy_hour(), lanes=12))

    def test_the_caption_badges_the_wall_with_the_source_its_lanes_came_from(self):
        """◌ is a declared width, so the caption says whose number the wall is."""
        caption = tui.spark_caption(_busy_hour(), lanes=6, source="plan apex")
        self.assertIn("%s6 lanes" % badges.BADGE_DECLARED, caption)
        self.assertNotIn(badges.BADGE_ACCOUNT, caption)

    def test_an_empty_window_draws_nothing_at_all(self):
        self.assertEqual("", tui.concurrency_sparkline(_metrics()))
        self.assertEqual("", tui.spark_caption(_metrics()))


# ---- the picture on the lane scale --------------------------------------
class ThePictureScalesToTheLanes(unittest.TestCase):
    """`█` means at or over the lane count, on every screen and in every window."""

    def test_one_hour_is_one_cell_per_minute(self):
        self.assertEqual(60, tui.SPARK_WIDTH)
        self.assertEqual(60, len(tui.concurrency_sparkline(_busy_hour(), lanes=12)))

    def test_a_minute_at_or_over_the_lane_count_is_the_full_block(self):
        row = tui.concurrency_sparkline(
            _hour_scan([(0, 12), (1, 24), (2, 11)]), lanes=12)
        self.assertEqual("██▇", row[:3])

    def test_below_the_lane_count_the_block_is_the_approach(self):
        row = tui.concurrency_sparkline(_hour_scan([(0, 6), (1, 5), (2, 1)]), lanes=12)
        self.assertEqual("▄▃▁", row[:3])

    def test_the_lanes_set_the_scale_not_this_windows_peak(self):
        """The current behaviour tops out at the window's worst minute, which
        draws the same shape whether the lanes are 12 or 120."""
        row = tui.concurrency_sparkline(_busy_hour(), lanes=100)
        self.assertNotIn("█", row, "nothing here reached 100 lanes")
        self.assertEqual({"▁"}, set(row), "every minute here is far below 100 lanes")

    def test_without_lanes_the_row_still_scales_to_this_windows_peak(self):
        row = tui.concurrency_sparkline(_busy_hour())
        self.assertEqual("█", row[4], "the busiest minute is the full block")
        self.assertNotIn("█", row[:4])

    def test_the_count_of_full_blocks_is_the_minutes_over(self):
        metrics = _busy_hour()
        self.assertEqual(7, tui.concurrency_sparkline(metrics, lanes=12).count("█"))
        self.assertIn("over 7m of 60m", tui.spark_caption(metrics, lanes=12))

    def test_a_window_with_no_minute_over_never_went_over(self):
        """A zero minutes-over is the clean case, and the caption says so in
        words rather than printing `over 0m of 60m`."""
        caption = tui.spark_caption(_hour_scan([(0, 3), (1, 4)]), lanes=12)
        self.assertIn("never over", caption)
        self.assertNotIn("over 0m", caption)
        self.assertEqual("scale: this window · peak at %s" % _local("2026-11-02T14:01Z"),
                         tui.spark_caption(_hour_scan([(0, 3), (1, 4)])))

    def test_an_empty_window_has_no_picture_and_no_caption(self):
        self.assertEqual("", tui.concurrency_sparkline(_metrics(), lanes=12))
        self.assertEqual("", tui.spark_caption(_metrics(), lanes=12))


# ---- the lane count the picture scales to -------------------------------
class TheLanesComeFromTheWidthRow(unittest.TestCase):
    """The header's `●<lanes>` and the caption's denominator are one number."""

    def test_the_accounts_number_wins_and_the_picture_scales_to_it(self):
        cap = _cap(_payload("capacity-2026-11-02.json"))
        self.assertEqual((12, "account"), tui.resolved_lanes(_metrics(), cap))

    def test_a_declaration_is_the_fallback_when_nothing_is_cached(self):
        self.assertEqual((None, "unknown"), tui.resolved_lanes(_metrics(), None))
        value, source = tui.resolved_lanes(_metrics(declared_plan="apex"), None)
        self.assertEqual("plan apex", source)
        self.assertIsInstance(value, int)

    def test_a_declaration_never_overrides_the_account(self):
        cap = _cap(_payload("capacity-2026-11-02.json"))
        self.assertEqual((12, "account"),
                         tui.resolved_lanes(_metrics(declared_plan="pro"), cap))


# ---- [d]: the report the dashboard was cut from -------------------------
class TheDetailOnAKeypress(unittest.TestCase):
    """`[d]` fills the detail panel with the `analyze` report, cache only."""

    def test_the_detail_is_the_read_only_analyze_report(self):
        with mock.patch.object(tui.cli, "main", return_value=0) as main:
            code, text = tui.detail_output("apex", 24)
        argv = main.call_args.args[0]
        self.assertEqual("analyze", argv[0])
        self.assertEqual("24h", argv[argv.index("--since") + 1])
        self.assertIn("--offline", argv)
        # the screen is read-only: a person opening and closing it must not
        # leave a run extraction in the store
        self.assertIn("--no-store", argv)
        self.assertEqual("apex", argv[argv.index("--plan") + 1])
        self.assertEqual(0, code)
        self.assertEqual("", text)

    def test_the_report_asks_for_the_dashboards_window(self):
        with mock.patch.object(tui.cli, "main", return_value=0) as main:
            tui.detail_output()
        argv = main.call_args.args[0]
        self.assertEqual("1h", argv[argv.index("--since") + 1])

    def test_a_failed_analyze_hands_back_its_code_and_its_error(self):
        def refused(argv):
            print("luna-doctor: bad --since value", file=sys.stderr)
            return 2

        with mock.patch.object(tui.cli, "main", side_effect=refused):
            code, text = tui.detail_output("apex", 24)
        self.assertEqual(2, code)
        self.assertIn("bad --since value", text)
        self.assertFalse(tui.carries_report(code, text),
                         "one error line is not a report")

    def test_the_exit_codes_analyze_answers_with_all_carry_a_report(self):
        """0 clean, 1 over capacity, 2 nothing measured: all three are answers."""
        report_text = "5 requests, none blocked at admission, peak concurrency 2.\n"
        for code in (0, 1, 2):
            self.assertTrue(tui.carries_report(code, report_text), code)
        self.assertTrue(tui.carries_report(2, "No requests found in the window."),
                        "an empty window is a report that says so")

    def test_a_capture_holding_only_the_tools_own_error_is_no_report(self):
        self.assertFalse(tui.carries_report(2, "luna-doctor: bad --since value: 'nope'"))
        self.assertFalse(tui.carries_report(
            2, "usage: luna-doctor [-h] ...\nluna-doctor: error: unrecognized arguments"))
        self.assertFalse(tui.carries_report(3, "5 requests, none blocked"),
                         "a code analyze never answers with is no report")

    def test_an_empty_capture_is_an_empty_report_not_a_failure(self):
        self.assertTrue(tui.carries_report(0, ""))


# ---- [f]: the fix for the window on screen ------------------------------
class TheFixAsksForThisWindow(unittest.TestCase):
    """The advice is about the rows on screen, so it is read for their window."""

    def test_the_fix_runs_for_the_window_on_screen(self):
        with mock.patch.object(tui.cli, "main", return_value=0) as main:
            tui.fix_output("apex", 6)
        argv = main.call_args.args[0]
        self.assertEqual("fix", argv[0])
        self.assertEqual("6h", argv[argv.index("--since") + 1])
        self.assertIn("--offline", argv)
        self.assertEqual("apex", argv[argv.index("--plan") + 1])

    def test_the_fix_defaults_to_the_dashboards_one_hour(self):
        with mock.patch.object(tui.cli, "main", return_value=0) as main:
            tui.fix_output()
        argv = main.call_args.args[0]
        self.assertEqual("1h", argv[argv.index("--since") + 1])
        self.assertNotIn("--plan", argv)

    def test_d_runs_analyze_and_fills_the_detail_panel(self):
        asyncio.run(self._drive())

    async def _drive(self):
        app = tui.TriageApp(plan="apex")

        def report(argv):
            print("peak concurrency 17")
            return 0

        with mock.patch.object(tui, "scan", return_value=(_busy(), None, None)), \
                mock.patch.object(tui.cli, "main", side_effect=report) as main:
            async with app.run_test(size=(100, 40)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                await pilot.press("d")
                await self._settle(
                    pilot, lambda: "peak concurrency 17" in self._text(app, "#detail-text"))
                argv = main.call_args.args[0]
                self.assertEqual("analyze", argv[0])
                self.assertIn("--offline", argv)
                self.assertIn("--no-store", argv)
                self.assertEqual("apex", argv[argv.index("--plan") + 1])
                self.assertTrue(app.query_one("#detail").display)
                self.assertIn("the analyze report is on screen",
                              self._text(app, "#status"))
                await pilot.press("q")

    def test_a_failed_analyze_is_announced_as_a_failure(self):
        asyncio.run(self._drive_failure())

    async def _drive_failure(self):
        app = tui.TriageApp()
        failed = (2, "luna-doctor: bad --since value: 'nope'")
        with mock.patch.object(tui, "scan", return_value=(_busy(), None, None)), \
                mock.patch.object(tui, "detail_output", return_value=failed):
            async with app.run_test(size=(100, 40)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                await pilot.press("d")
                await self._settle(
                    pilot, lambda: "analyze command failed" in self._text(app, "#status"))
                status = self._text(app, "#status")
                self.assertIn("the analyze command failed: luna-doctor: bad --since value", status)
                self.assertNotIn("is on screen", status,
                                 "a failed command is not a report on screen")
                self.assertFalse(app.query_one("#detail").display,
                                 "a failure fills no panel")
                await pilot.press("q")

    def test_an_over_capacity_report_is_not_a_failure(self):
        """`analyze` exits 1 when it finds pressure, and that is the report."""
        asyncio.run(self._drive_pressure())

    async def _drive_pressure(self):
        app = tui.TriageApp()
        pressure = (1, "Concurrency pressure across 17 requests: 3 request(s) "
                       "were blocked at admission, peak concurrency 17.\n")
        with mock.patch.object(tui, "scan", return_value=(_busy(), None, None)), \
                mock.patch.object(tui, "detail_output", return_value=pressure):
            async with app.run_test(size=(100, 40)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                await pilot.press("d")
                await self._settle(
                    pilot, lambda: "is on screen" in self._text(app, "#status"))
                self.assertIn("the analyze report is on screen",
                              self._text(app, "#status"))
                self.assertTrue(app.query_one("#detail").display)
                await pilot.press("q")

    @staticmethod
    async def _settle(pilot, ready) -> None:
        """Wait for the thread worker's result, bounded, so a hang fails loudly."""
        for _attempt in range(40):
            await pilot.pause(0.05)
            if ready():
                return

    @staticmethod
    def _text(app, selector) -> str:
        return str(app.query_one(selector).render())


# ---- [c]: the chart, drawn by the command that owns it -------------------
class TheChartOnAKeypress(unittest.TestCase):
    """The rendered picture stays one keypress away, drawn for this window."""

    def test_the_chart_asks_the_command_for_this_window_offline(self):
        def drawn(argv):
            out = argv[argv.index("--out") + 1]
            with open(out, "w", encoding="utf-8") as fh:
                fh.write("<svg/>")
            with open(os.path.splitext(out)[0] + ".png", "wb") as fh:
                fh.write(b"png")
            return 0

        with mock.patch.object(tui.cli, "main", side_effect=drawn) as main:
            svg, png, error = tui.chart_output("apex", 24)
        self.addCleanup(shutil.rmtree, os.path.dirname(svg), True)
        argv = main.call_args.args[0]
        self.assertEqual("chart", argv[0])
        self.assertEqual("24h", argv[argv.index("--since") + 1])
        self.assertIn("--offline", argv)
        self.assertEqual("apex", argv[argv.index("--plan") + 1])
        self.assertEqual(svg, argv[argv.index("--out") + 1])
        self.assertTrue(svg.endswith("chart.svg"), svg)
        self.assertEqual("", error)
        self.assertTrue(png.endswith("chart.png"), "the PNG the command wrote is offered")

    def test_a_failed_chart_reports_the_error_and_offers_no_file(self):
        def refused(argv):
            print("luna-doctor: cannot write the chart: no space left", file=sys.stderr)
            return 2

        with mock.patch.object(tui.cli, "main", side_effect=refused):
            svg, png, error = tui.chart_output("apex", 24)
        self.addCleanup(shutil.rmtree, os.path.dirname(svg), True)
        self.assertIsNone(png, "a chart nobody wrote is not offered as opened")
        self.assertIn("no space left", error, "the command's own last line comes back")

    def test_the_chart_asks_for_the_dashboards_one_hour_by_default(self):
        with mock.patch.object(tui.cli, "main", return_value=0) as main:
            svg, _png, error = tui.chart_output()
        self.addCleanup(shutil.rmtree, os.path.dirname(svg), True)
        argv = main.call_args.args[0]
        self.assertEqual("1h", argv[argv.index("--since") + 1])
        self.assertTrue(error, "nothing was written, so no path is offered")

    def test_a_chart_that_wrote_no_file_is_a_failure_even_at_exit_zero(self):
        with mock.patch.object(tui.cli, "main", return_value=0):
            svg, _png, error = tui.chart_output("apex", 24)
        self.addCleanup(shutil.rmtree, os.path.dirname(svg), True)
        self.assertFalse(os.path.exists(svg))
        self.assertTrue(error, "a path asked for is not a file that exists")

    def test_c_confirms_the_path_it_wrote(self):
        asyncio.run(self._drive())

    async def _drive(self):
        app = tui.TriageApp()
        drawn = ("/tmp/luna-doctor-chart/chart.svg",
                 "/tmp/luna-doctor-chart/chart.png", "")
        with mock.patch.object(tui, "scan", return_value=(_busy(), None, None)), \
                mock.patch.object(tui, "chart_output", return_value=drawn) as chart:
            async with app.run_test(size=(100, 40)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                await pilot.press("c")
                await self._settle(
                    pilot, lambda: "chart written" in self._status(app))
                status = self._status(app)
                self.assertEqual(1, chart.call_count)
                self.assertIn("chart written: /tmp/luna-doctor-chart/chart.png", status)
                await pilot.press("q")

    def test_a_failed_chart_names_the_error_instead_of_a_path(self):
        asyncio.run(self._drive_failure())

    async def _drive_failure(self):
        app = tui.TriageApp()
        failed = ("/tmp/luna-doctor-chart/chart.svg", None,
                  "luna-doctor: cannot write the chart: no space left")
        with mock.patch.object(tui, "scan", return_value=(_busy(), None, None)), \
                mock.patch.object(tui, "chart_output", return_value=failed):
            async with app.run_test(size=(100, 40)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                await pilot.press("c")
                await self._settle(
                    pilot, lambda: "the chart failed" in self._status(app))
                status = self._status(app)
                self.assertIn("cannot write the chart", status)
                self.assertNotIn("chart written", status,
                                 "a status must not name a file nobody wrote")
                await pilot.press("q")

    @staticmethod
    async def _settle(pilot, ready) -> None:
        """Wait for the thread worker's result, bounded, so a hang fails loudly."""
        for _attempt in range(40):
            await pilot.pause(0.05)
            if ready():
                return

    @staticmethod
    def _status(app) -> str:
        return str(app.query_one("#status").render())
