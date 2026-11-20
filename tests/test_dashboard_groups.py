"""The evidence group, the NEXT region and the two key lines, row by row.

Every assertion reads strings the pure builders return, so the suite runs
offline and needs no terminal. The expected lines are copied from the dashboard
mockups, so a row that drifts by one cell fails here.

Three facts decide most of these rows and all three are pinned below: the answer
selects a group of three rows and never gates the screen; a delta needs a
previous window that clears `baseline.FLOOR` in both windows; and the NEXT row
carries only the four imperatives the brief gives, read from the header's own
facts rather than from a second verdict.
"""

import time
import unicodedata
import unittest

import pytest

pytest.importorskip("textual")

from luna_doctor import baseline, sources, tui

# An 80-column screen leaves 2 cells of right margin, so no row may exceed 78.
ROW_BUDGET = 78

_SAMPLED = "2026-11-02T14:31:07.482003114Z"


def _cap(in_use, limit, *, plan="apex", sampled_at=_SAMPLED):
    """The Capacity payload `sources.fetch_capacity` hands the screen."""
    data = {
        "now": {"limit": limit, "in_use": in_use,
                "available": None if in_use is None else limit - in_use,
                "saturated": None, "ledger": "up", "sampled_at": sampled_at},
        "config": {"background_lanes": 12, "overflow_routing": "enabled",
                   "plan": plan, "priority_lanes": limit},
        "history": None,
        "admission": None,
    }
    fields, err = sources._parse_capacity_payload({"success": True, "data": data})
    assert not err, err
    return sources.Capacity(fetched_at=time.time(), **fields)


def _window(requests, peak, *, blocked=0, rejected=0, at_limit=0, background=0,
            stalled=0, starved=0, p50=None, p90=None, samples=12,
            slowest=None, admitted=100.0):
    """One analyse() window, reduced to the keys these rows read.

    The verdict needs the same block `analyse` produces, so the shapes that only
    `cli.verdict` reads are filled here too: this suite must not re-derive a
    status the CLI would decide differently.
    """
    return {
        "window_hours": 1,
        "requests": requests,
        "by_outcome": {"completed": max(0, requests - blocked - rejected - at_limit
                                        - stalled),
                       "admission_timeout": blocked,
                       "rate_limited": rejected,
                       "org_concurrency_limit": at_limit,
                       "client_idle_timeout": stalled},
        "by_tier": {"interactive": requests - background, "background": background},
        "by_harness": {"pi": requests},
        "latency_s": {"interactive": {"n": samples, "p50": p50, "p90": p90,
                                      "max": 900.0},
                      "background": {"n": 0, "p50": None, "p90": None, "max": None}},
        "peak_concurrency": peak,
        "peak_at": None,
        "peak_by_harness": {},
        "admission_conversion": {
            "interactive": {"completed": max(0, requests - blocked),
                            "admission_timeout": blocked,
                            "admitted_pct": admitted},
            "background": {"completed": 0, "admission_timeout": 0,
                           "admitted_pct": None}},
        "saturated": 0,
        "starved_peer_evidence": starved,
        "pressure_windows": 0,
        "longest_pressure_window": None,
        "timeout_storms": [],
        "per_model_deadlines": {},
        "unknown_errors": [],
        "saturation_bands": {},
        "declared_plan": None,
        "scanned_at": None,
        "slowest_request": slowest,
    }


def _cells(text):
    """A string's width in terminal cells: a phase moon is two, the rest one."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


def _lines(rows):
    return [line for _badge, line in rows]


# §2.1, §2.3 and §2.4 draw the same window: 42 requests, peak 17 against 12
# lanes, 3 blocked at admission, 11 refused with a 429.
def _busy():
    return _window(42, 17, blocked=3, rejected=11, at_limit=4, background=7,
                   p50=95.0, p90=240.0, admitted=87.0)


def _busy_before():
    """The window one step back: the mockups' deltas are measured against it."""
    return _window(40, 12, p50=83.0, p90=202.0, samples=10)


def _healthy():
    return _window(9, 4, p50=31.0, p90=58.0, samples=9)


def _healthy_before():
    return _window(11, 4, p50=35.0, p90=67.0, samples=11)


def _empty():
    return _window(0, 0, samples=0, admitted=None)


# ---- the five mockups, verbatim -------------------------------------------
class LatencyGroup(unittest.TestCase):
    """§2.1, §2.2, §2.3 and §2.5: the same three rows, and all that changes."""

    def test_the_busy_latency_group_is_the_mockups_rows(self):
        self.assertEqual(_lines(tui.group_rows(
            tui.REGION_LATENCY, _busy(), _busy_before())), [
            " p50          ○      95s ▲ 12",
            " p90          ○     240s ▲ 38",
            " gateway      ○      25s   · fixed wait before headers",
        ])

    def test_the_healthy_latency_group_is_the_mockups_rows(self):
        self.assertEqual(_lines(tui.group_rows(
            tui.REGION_LATENCY, _healthy(), _healthy_before())), [
            " p50          ○      31s ▾ 4",
            " p90          ○      58s ▾ 9",
            " gateway      ○      25s   · fixed wait before headers",
        ])

    def test_a_window_with_no_lane_count_shows_the_same_latency_rows(self):
        """§2.3 differs from §2.1 above this region and nowhere inside it."""
        self.assertEqual(_lines(tui.group_rows(tui.REGION_LATENCY, _busy(), _busy_before())),
                         _lines(tui.group_rows(tui.REGION_LATENCY, _busy(), _busy_before())))

    def test_a_window_with_nothing_measured_is_the_mockups_rows(self):
        self.assertEqual(_lines(tui.group_rows(tui.REGION_LATENCY, _empty(), None)), [
            " p50          ?            ·",
            " p90          ?            ·",
            " gateway      ○      25s   · fixed wait before headers",
        ])

    def test_a_suppressed_percentile_prints_no_denominator(self):
        rows = tui.group_rows(tui.REGION_LATENCY, _empty(), None)
        for line in _lines(rows[:2]):
            self.assertNotIn("n=", line)
            self.assertEqual(tui._BADGE_UNKNOWN, rows[0][0])


class RefusalsGroup(unittest.TestCase):
    """§2.4: the group the 429 and concurrency answers select."""

    def test_the_refusals_group_is_the_mockups_rows(self):
        self.assertEqual(_lines(tui.group_rows(
            tui.REGION_REFUSALS, _busy(), _busy_before())), [
            " 429          ○       11 ▲ 11 the account's rate limit",
            " at limit     ○        4 ▲  4 account pool full",
            " background   ○        7 ▲  7 demoted to the slow tier",
        ])

    def test_the_badges_are_this_machines_own_counts(self):
        rows = tui.group_rows(tui.REGION_REFUSALS, _busy(), _busy_before())
        self.assertEqual([badges for badges, _line in rows],
                         [tui.badges.BADGE_MEASURED] * 3)

    def test_a_window_that_refused_nothing_reads_zero_not_unknown(self):
        """A counter's absent class is a zero: nothing was refused, so nothing
        was unread."""
        rows = tui.group_rows(tui.REGION_REFUSALS, _healthy(), None)
        for _badge, line in rows:
            self.assertIn("        0", line)


class StallsGroup(unittest.TestCase):
    """The group "failing", "stuck" and "stream ended" select."""

    def _busy_with_digest(self):
        window = _window(42, 17, blocked=3, stalled=2, starved=1,
                         slowest={"duration": "312.4s", "outcome": "client_idle_timeout",
                                  "headline": "The stream stopped without a finish_reason."})
        return window

    def test_the_three_rows_are_stalled_slowest_and_starved(self):
        rows = tui.group_rows(tui.REGION_STALLS, self._busy_with_digest(), None)
        self.assertEqual([line[1:13].strip() for _badge, line in rows],
                         ["stalled", "slowest", "starved"])

    def test_the_digest_row_carries_the_duration_and_what_happened(self):
        rows = tui.group_rows(tui.REGION_STALLS, self._busy_with_digest(), None)
        self.assertIn("312.4s", rows[1][1])
        self.assertIn("the idle budget killed it", rows[1][1])

    def test_an_unranked_digest_says_why_rather_than_a_bare_question_mark(self):
        rows = tui.group_rows(tui.REGION_STALLS, _busy(), None)
        self.assertEqual(tui._BADGE_UNKNOWN, rows[1][0])
        self.assertIn("nothing ranked", rows[1][1])

    def test_no_digest_has_a_comparable_previous_window(self):
        rows = tui.group_rows(tui.REGION_STALLS, self._busy_with_digest(),
                              self._busy_with_digest())
        self.assertIn(tui.DELTA_NONE, rows[1][1][24:28])


# ---- the grid every evidence row is drawn on -----------------------------
class TheEvidenceGrid(unittest.TestCase):
    """§3.2: one grid for every data row, and the copy rules on top of it."""

    def setUp(self):
        self.busy = _busy()
        self.before = _busy_before()

    def test_every_group_holds_three_rows_under_the_budget(self):
        for key in tui.EVIDENCE_GROUPS:
            rows = tui.group_rows(key, self.busy, self.before)
            self.assertEqual(3, len(rows), key)
            for badge, line in rows:
                self.assertLessEqual(_cells(line), ROW_BUDGET, line)
                self.assertEqual(badge, line[14], "the badge left its column")
                self.assertEqual(" ", line[0], "the indent left the grid")
                self.assertEqual(" ", line[24], "the delta left its column")
                self.assertTrue(tui.dashboard_ok_row(line), line)

    def test_each_group_earns_the_moon_its_own_rows_show(self):
        """The region title wears `region_moon`, so the labels and clauses above
        have to be shapes it can read: the darkest moon for a refusal, pressure
        for a latency that moved, a clean moon in a healthy window, and the plain
        moon when nothing was measured."""
        new = tui.moon_glyph(tui.report.MOON_NEW)
        quarter = tui.moon_glyph(tui.report.MOON_QUARTER)
        full = tui.moon_glyph(tui.report.MOON_FULL)
        plain = tui.moon_glyph("moon")
        stressed = _window(42, 17, blocked=3, rejected=11, at_limit=4, background=7,
                           stalled=2, starved=1, p50=95.0, p90=240.0)
        expected = {tui.REGION_REFUSALS: new, tui.REGION_LATENCY: quarter,
                    tui.REGION_STALLS: quarter}
        for key in tui.EVIDENCE_GROUPS:
            busy = tui.group_rows(key, stressed, self.before)
            healthy = tui.group_rows(key, _healthy(), _healthy_before())
            self.assertEqual(expected[key], tui.region_moon(busy, key), key)
            self.assertEqual(full, tui.region_moon(healthy, key), key)
            self.assertEqual(plain,
                             tui.region_moon(tui.group_rows(key, _empty(), None), key), key)

    def test_the_refusals_title_can_read_over_capacity(self):
        """999 429s is a gateway turning work away, and the region says so.

        §4.2 forbids the old reading: the header can be dark because of the
        refusals alone, and a region that answered only to `blocked` could never
        explain it. The three rows carry the count, so the count decides.
        """
        refused = _window(999, 17, rejected=999, p50=95.0, p90=240.0)
        lines = tui.dashboard_lines("concurrency", refused, self.before, _cap(7, 12))
        self.assertEqual(tui.moon_glyph(tui.report.MOON_NEW), lines[9].split()[1])
        self.assertEqual(tui.moon_glyph(tui.report.MOON_NEW),
                         tui.region_moon(tui.group_rows(tui.REGION_REFUSALS, refused,
                                                        self.before),
                                         tui.REGION_REFUSALS))

    def test_the_moons_still_tie_to_the_header(self):
        """The two-level invariant survives the refusal rule, in both directions."""
        stressed = _window(42, 17, blocked=3, rejected=11, at_limit=4, background=7,
                           stalled=2, starved=1, p50=95.0, p90=240.0)
        for metrics in (stressed, _window(999, 17, rejected=999),
                        _healthy()):
            header = tui.moon_glyph(tui.header_moon(metrics))
            regions = [tui.region_moon(tui.load_rows(metrics, _cap(7, 12), 1, self.before),
                                       tui.REGION_LOAD)]
            for key in tui.EVIDENCE_GROUPS:
                regions.append(tui.region_moon(
                    tui.group_rows(key, metrics, self.before), key))
            self.assertTrue(tui.regions_satisfy_invariant(header, regions),
                            "%s: %s" % (header, regions))

    def test_a_qualifier_names_no_api_field(self):
        banned = ("rate_limit_error", "scheduler admission timeout",
                  "client_idle_timeout", "org_concurrency_limit",
                  "admission_timeout", "starved_peer_evidence", "finish_reason", "`")
        busy = _window(42, 17, blocked=3, rejected=11, at_limit=4, background=7,
                       stalled=2, starved=1, p50=95.0, p90=240.0,
                       slowest={"duration": "312.4s", "outcome": "client_idle_timeout",
                                "headline": "The stream stopped without a finish_reason."})
        for key in tui.EVIDENCE_GROUPS:
            for _badge, line in tui.group_rows(key, busy, self.before):
                for word in banned:
                    self.assertNotIn(word, line, key)

    def test_a_window_under_the_floor_shows_no_delta(self):
        thin = _window(baseline.FLOOR - 1, 12, p50=83.0, p90=202.0)
        for _badge, line in tui.group_rows(tui.REGION_LATENCY, self.busy, thin):
            self.assertIn(tui.DELTA_NONE, line[24:28], line)

    def test_two_equal_windows_read_flat_rather_than_missing(self):
        for _badge, line in tui.group_rows(tui.REGION_REFUSALS, self.busy, self.busy):
            self.assertIn(tui.DELTA_LEVEL, line[24:28], line)

    def test_an_unknown_group_is_a_caller_bug(self):
        with self.assertRaises(KeyError):
            tui.group_rows("prose", self.busy, self.before)


# ---- the NEXT region ------------------------------------------------------
class TheNextRegion(unittest.TestCase):
    """One imperative and one command: the brief's four, and no fifth."""

    def setUp(self):
        self.cap = _cap(7, 12)

    def test_over_capacity_is_the_mockups_two_rows(self):
        self.assertEqual(_lines(tui.next_rows(_busy(), self.cap, tui.REGION_LATENCY)), [
            "NEXT  Hold 12 in flight · send the rest to -background",
            "→ luna-doctor fix --since 1h                                              [f]",
        ])

    def test_a_healthy_window_is_the_mockups_two_rows(self):
        self.assertEqual(_lines(tui.next_rows(_healthy(), self.cap, tui.REGION_LATENCY)), [
            "NEXT  Nothing to change · ●12 lanes hold this load",
            "→ luna-doctor watch --since 1h",
        ])

    def test_a_window_with_no_lane_count_holds_flat_and_fetches_it(self):
        self.assertEqual(_lines(tui.next_rows(_busy(), None, tui.REGION_REFUSALS)), [
            "NEXT  Hold concurrency flat until the lane count is known",
            "→ luna-doctor capacity --online",
        ])

    def test_nothing_measured_widens_the_window(self):
        self.assertEqual(_lines(tui.next_rows(_empty(), self.cap, tui.REGION_LATENCY)), [
            "NEXT  Nothing measured in the last hour · widen the window",
            "→ luna-doctor analyze --since 6h                                          [w]",
        ])

    def test_the_imperative_is_one_short_verb_led_clause(self):
        cases = ((_busy(), self.cap), (_healthy(), self.cap), (_busy(), None),
                 (_empty(), self.cap))
        for metrics, cap in cases:
            line = tui.next_rows(metrics, cap, tui.REGION_STALLS)[0][1]
            clause = line[len("NEXT  "):]
            self.assertLessEqual(_cells(clause), tui.NEXT_CELLS, clause)
            self.assertIn(clause.split()[0], ("Hold", "Nothing"), clause)
            self.assertFalse(clause.endswith("."), "a data row has no sentence stop")

    def test_the_command_row_ends_with_the_key_that_runs_it(self):
        for metrics, cap, key in ((_busy(), self.cap, "f"),
                                  (_empty(), self.cap, "w"),
                                  (_healthy(), self.cap, None),
                                  (_busy(), None, None)):
            line = tui.next_rows(metrics, cap, tui.REGION_LATENCY)[1][1]
            self.assertTrue(line.startswith("→ luna-doctor "), line)
            self.assertLessEqual(_cells(line), tui.HEADER_CELLS, line)
            if key is None:
                self.assertNotIn("[", line)
            else:
                self.assertTrue(line.endswith("[%s]" % key), line)

    def test_the_group_on_screen_does_not_change_the_advice(self):
        """§2.4 draws the same imperative beside the refusals group that §2.1
        draws beside the latency group: the state decides, not the selection."""
        for key in tui.EVIDENCE_GROUPS:
            self.assertEqual(tui.next_rows(_busy(), self.cap, tui.REGION_LATENCY),
                             tui.next_rows(_busy(), self.cap, key))

    def test_the_row_reads_the_headers_own_moon(self):
        """The NEXT row and the header cannot disagree: both ask header_moon."""
        for metrics in (_busy(), _healthy(), _empty()):
            moon = tui.header_moon(metrics)
            line = tui.next_rows(metrics, self.cap, tui.REGION_LATENCY)[0][1]
            if moon == "moon":
                self.assertIn("Nothing measured", line)
            elif moon in (tui.report.MOON_NEW, tui.report.MOON_QUARTER):
                self.assertIn("Hold 12 in flight", line)
            else:
                self.assertIn("Nothing to change", line)

    def test_a_declared_lane_count_is_badged_as_declared(self):
        declared = dict(_healthy(), declared_plan="apex")
        line = tui.next_rows(declared, None, tui.REGION_LATENCY)[0][1]
        self.assertIn("%s6 lanes hold this load" % tui.badges.BADGE_DECLARED, line)

    def test_an_unknown_group_is_a_caller_bug(self):
        with self.assertRaises(KeyError):
            tui.next_rows(_healthy(), self.cap, "prose")


# ---- the two key lines ----------------------------------------------------
class TheKeyLines(unittest.TestCase):
    """Provenance on one line, the moons on the other, and no third line."""

    def setUp(self):
        self.cap = _cap(7, 12)

    def test_the_two_key_lines_are_the_mockups_lines(self):
        self.assertEqual(tui.key_lines(), [
            "● account · ○ this machine (a lower bound) · ? unknown",
            "🌕 ok · 🌗 pressure · 🌑 blocked at admission",
        ])

    def test_the_moon_key_names_the_plain_moon_only_when_the_screen_drew_it(self):
        self.assertEqual(tui.key_lines(_empty(), self.cap)[1],
                         "🌙 not established · 🌕 ok · 🌗 pressure · 🌑 blocked at admission")
        self.assertEqual(tui.key_lines(_healthy(), self.cap)[1],
                         "🌕 ok · 🌗 pressure · 🌑 blocked at admission")

    def test_the_declared_entry_appears_only_beside_a_declared_number(self):
        self.assertNotIn("◌", tui.key_lines(_healthy(), self.cap)[0])
        self.assertEqual(tui.key_lines(dict(_healthy(), declared_plan="apex"), None)[0],
                         "● account · ○ this machine (a lower bound) · ? unknown"
                         " · ◌ declared")

    def test_each_key_line_fits_the_headers_budget(self):
        for metrics in (None, _empty(), _healthy(), _busy(),
                        dict(_healthy(), declared_plan="apex")):
            lines = tui.key_lines(metrics, self.cap)
            self.assertEqual(2, len(lines))
            for line in lines:
                self.assertLessEqual(_cells(line), tui.HEADER_CELLS, line)
