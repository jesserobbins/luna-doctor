"""The triage screen's data contract: one answer in, eighteen rows out.

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


def _busy(**over):
    """The owner's window: 42 requests, peak 17 against 12 lanes, 3 blocked."""
    metrics = _dash(
        requests=42,
        by_outcome={"completed": 28, "admission_timeout": 3, "rate_limited": 11},
        by_tier={"interactive": 35, "background": 7},
        peak_concurrency=17,
        latency_s={"interactive": {"n": 39, "p50": 95.0, "p90": 240.0, "max": 300.0},
                   "background": {"n": 7, "p50": 400.0, "p90": 520.0, "max": 600.0}})
    metrics.update(over)
    return metrics


def _busy_before():
    """The window before it: quieter, and wide enough for a delta."""
    return _dash(
        requests=40, by_outcome={"completed": 40}, by_tier={"interactive": 40},
        peak_concurrency=12, latency_s={"interactive": {"n": 35, "p50": 83.0,
                                                        "p90": 202.0, "max": 260.0}},
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


def _lines(choice_key="slow", metrics=None, previous=None, cap=None):
    return tui.dashboard_lines(choice_key,
                               _busy() if metrics is None else metrics,
                               _busy_before() if previous is None else previous,
                               cap)


def _cells(text):
    """A string's width in terminal cells: a wide glyph is two cells."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


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
        """One spelling per concept: the selector answers in the same three
        region kinds `region_moon` and the region title use."""
        self.assertEqual(
            set(tui.EVIDENCE_GROUPS),
            {tui.pick_lead(key) for _text, key in tui.TRIAGE_CHOICES})
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
        lines = _lines(cap=self.cap)
        self.assertEqual(18, len(lines))
        self.assertTrue(lines[0].startswith(tui.moon_glyph(tui.header_moon(_busy()))))
        self.assertEqual(tui.RULE, lines[1])
        self.assertTrue(lines[2].startswith("LOAD "))
        self.assertTrue(lines[6].startswith(" "), "the picture is the sixth row")
        self.assertTrue(lines[7].startswith(" "), "its caption sits under it")
        self.assertEqual(tui.RULE, lines[8])
        self.assertTrue(lines[9].startswith("LATENCY "))
        self.assertEqual(tui.RULE, lines[13])
        self.assertTrue(lines[14].startswith("NEXT  "))
        self.assertTrue(lines[15].startswith("→ luna-doctor "))
        self.assertEqual(tui.key_lines(_busy(), self.cap), lines[16:])

    def test_the_answer_swaps_the_evidence_group_and_nothing_above_it(self):
        slow = _lines("slow", cap=self.cap)
        refusals = _lines("concurrency", cap=self.cap)
        self.assertEqual(slow[:9], refusals[:9], "a region above the group moved")
        self.assertTrue(refusals[9].startswith("REFUSALS "))
        self.assertNotEqual(slow[10:13], refusals[10:13])

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
        self.assertIn("(a lower bound)", lines[16])

    def test_the_two_key_lines_close_the_screen(self):
        """§6.2 item 10: provenance on one line, the moons on the other."""
        lines = _lines(cap=self.cap)
        self.assertIn("account", lines[16])
        self.assertIn(tui.moon_glyph(report.MOON_NEW), lines[17])
        for line in (lines[16], lines[17]):
            self.assertLessEqual(_cells(line), tui.HEADER_CELLS)

    def test_a_window_that_claims_no_status_keys_the_plain_moon(self):
        """A key to a glyph nobody drew is decoration; a glyph on screen is not."""
        lines = _lines("slow", _empty(), None, self.cap)
        self.assertEqual(tui.moon_glyph("moon"), lines[0][0])
        self.assertTrue(lines[17].startswith(tui.moon_glyph("moon")))

    def test_the_region_titles_name_what_the_delta_compares(self):
        lines = _lines(cap=self.cap)
        self.assertTrue(lines[2].endswith("vs previous 1h"), lines[2])
        self.assertTrue(lines[9].endswith("vs previous 1h"), lines[9])
        self.assertEqual(tui.HEADER_CELLS, _cells(lines[2]))
        self.assertEqual(tui.HEADER_CELLS, _cells(lines[9]))

    def test_the_picture_rows_stay_when_the_window_drew_no_picture(self):
        """§2.5: the empty-picture rule hides the drawing, not the positions."""
        lines = _lines("slow", _empty(), None, self.cap)
        self.assertEqual(["", ""], lines[6:8])
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

    def test_the_two_latency_tiers_never_share_a_row_again(self):
        """The tier split was one number's worth of information; the latency
        group carries the interactive p50, p90 and the gateway's fixed wait."""
        lines = _lines("slow", self.metrics, _busy_before(), self.cap)
        latency = lines[10:13]
        self.assertIn("p50", latency[0])
        self.assertIn("p90", latency[1])
        self.assertIn("gateway", latency[2])
        for line in latency:
            self.assertNotIn("background", line)

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
        self.assertIn("Hold 4 in flight", lines[14], "the advice reads the declaration")
        self.assertTrue(lines[16].endswith("%s declared" % badges.BADGE_DECLARED),
                        lines[16])


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
        p50, p90, gateway = self.lines[10:13]
        self.assertIn("p50", p50)
        self.assertIn("p90", p90)
        self.assertIn("gateway", gateway)
        for line in (p50, p90):
            self.assertIn(tui._BADGE_UNKNOWN, line)
            self.assertNotRegex(line, r"\d+s", "no percentile was measured here")

    def test_the_next_row_widens_the_window_instead_of_naming_a_number(self):
        self.assertIn("Nothing measured in the last hour", self.lines[14])
        self.assertIn("--since 6h", self.lines[15])


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
    def test_the_app_takes_a_plan_and_an_online_flag(self):
        app = tui.TriageApp(plan="apex", online=False)
        self.assertEqual("apex", app.plan)
        self.assertFalse(app.online)

    def test_the_footer_renders_five_keys_with_descriptions(self):
        """§8 item 5: the Footer renders `BINDINGS`, and every binding has a word."""
        bindings = [(binding.key, binding.description)
                    for binding in tui.TriageApp.BINDINGS]
        self.assertEqual(["f", "d", "c", "w", "q"], [key for key, _word in bindings])
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

    def test_the_scan_lands_as_the_eighteen_rows_and_the_footer(self):
        asyncio.run(self._drive())

    async def _drive(self):
        app = tui.TriageApp()
        cap = _cap(_payload("capacity-2026-11-02.json"))
        with mock.patch.object(tui, "scan",
                               return_value=(_busy(), _busy_before(), cap)):
            async with app.run_test(size=(80, 24)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                drawn = self._dashboard(app)
                self.assertEqual(18, len(drawn.splitlines()), drawn)
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
                self.assertEqual(["", ""], lines[6:8],
                                 "an empty picture is not a shape")
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
        self.assertEqual("scale: this window · peak at 14:04Z",
                         tui.spark_caption(_busy_hour()))
        self.assertEqual("█ = at or over ●12 lanes · over 7m of 60m · peak at 14:04Z",
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
        self.assertEqual("scale: this window · peak at 14:01Z",
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
