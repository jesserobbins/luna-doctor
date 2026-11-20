"""The LOAD region's three data rows, measured against the mockups.

Every assertion reads the strings `tui.load_rows` builds, so the suite runs
offline and needs no terminal: what is tested is the row contract, not pixels.
The expected lines are copied from the dashboard mockups, so a row that drifts
by one cell fails here.

Two facts decide most of the rows and both are pinned below: freshness is read
from `metrics["capacity"]["now_fresh"]` (the Capacity payload carries no
freshness field), and a delta needs a comparable previous window that clears
`baseline.FLOOR` in both windows before it may print an arrow.
"""

import time
import unicodedata
import unittest

import pytest

pytest.importorskip("textual")

from luna_doctor import baseline, sources, tui

# An 80-column screen leaves 2 cells of right margin, so no row may exceed 78.
ROW_BUDGET = 78

# The samples the mockups draw, microseconds and all.
_SAMPLED = "2026-11-02T14:20:43.95670792Z"
_DEFAULT_CAP = object()
_HEALTHY_SAMPLED = "2026-11-02T14:31:07.482003114Z"


def _cap(in_use, limit, sampled_at, *, plan="apex"):
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


def _served(cap, *, fresh=True):
    """The `metrics.capacity` block, reduced to what the now row reads."""
    block = {"provenance": sources.PROV_ACCOUNT, "now_fresh": fresh}
    now = None if cap is None else cap.now
    if now is not None:
        block["now"] = {"in_use": now.in_use, "limit": now.limit,
                        "sampled_at": now.sampled_at}
    return block


def _window(requests, peak, blocked=0, rejected=0, *, cap=None, fresh=True,
            scanned_at=_SAMPLED):
    """One analyse() window, reduced to the keys the LOAD rows read."""
    return {
        "window_hours": 1,
        "requests": requests,
        "by_outcome": {"completed": max(0, requests - blocked - rejected),
                       "admission_timeout": blocked,
                       "rate_limited": rejected},
        "peak_concurrency": peak,
        "declared_plan": None,
        "capacity": _served(cap, fresh=fresh),
        "scanned_at": tui._stamp_epoch(scanned_at),
    }


def _cells(text):
    """A string's width in terminal cells: a wide glyph is two cells."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


def _lines(rows):
    return [line for _badge, line in rows]


# ---- §2.1 and §2.4: over capacity, and the refusals group ------------------
class OverCapacity(unittest.TestCase):
    """The owner's case: 42 requests, peak 17 against 12 lanes, 3 blocked."""

    def setUp(self):
        self.cap = _cap(7, 12, _SAMPLED)
        self.metrics = _window(42, 17, blocked=3, cap=self.cap)
        self.previous = _window(40, 12, blocked=0, cap=self.cap)
        self.rows = tui.load_rows(self.metrics, self.cap, 1, self.previous)

    def test_the_block_reads_verbatim(self):
        self.assertEqual(_lines(self.rows), [
            " now          ●     7/12   · sampled 14:20Z",
            " peak 1h      ○       17 ▲ 5 over ●12 by 5",
            " blocked      ○        3 ▲ 3 0 × 429",
        ])

    def test_each_row_carries_its_own_badge(self):
        # ● is the account's number, ○ is a measurement of these logs, and one
        # source supplies both numbers of the pair its badge leads.
        self.assertEqual([badge for badge, _line in self.rows], ["●", "○", "○"])

    def test_the_refusals_group_shares_the_over_capacity_load_block(self):
        # §2.4 differs from §2.1 only in the region under LOAD, so its three
        # rows are asserted once: the `× 429` cross-check is this machine's own
        # count, and the account-side refusals beside it do not move this row.
        self.assertEqual(_lines(self.rows), _lines(tui.load_rows(
            _window(42, 17, blocked=3, cap=self.cap),
            self.cap, 1, self.previous)))


# ---- §2.2: healthy ---------------------------------------------------------
class Healthy(unittest.TestCase):
    """9 requests, peak 4 under 12 lanes, nothing blocked."""

    def setUp(self):
        self.cap = _cap(3, 12, _HEALTHY_SAMPLED)
        self.metrics = _window(9, 4, cap=self.cap, scanned_at=_HEALTHY_SAMPLED)
        self.previous = _window(9, 3, cap=self.cap)
        self.rows = tui.load_rows(self.metrics, self.cap, 1, self.previous)

    def test_the_block_reads_verbatim(self):
        self.assertEqual(_lines(self.rows), [
            " now          ●     3/12   · sampled 14:31Z",
            " peak 1h      ○        4 ▲ 1 never over",
            # Zero blocks in both windows is no change to report, so the delta
            # is `·`; `▬` is reserved for a count that both windows carried and
            # that did not move.
            " blocked      ○        0   · 0 × 429",
        ])

    def test_a_clean_window_is_stated_positively(self):
        # No negative-first construction: a reader should not have to negate a
        # noun pile to learn good news.
        self.assertIn("never over", self.rows[1][1])


# ---- §2.3: no capacity cached ---------------------------------------------
class NoCapacityCached(unittest.TestCase):
    """The honest degradation: the logs answered, the account did not."""

    def setUp(self):
        self.metrics = _window(42, 17, blocked=3)
        self.previous = _window(40, 12, blocked=0)
        self.rows = tui.load_rows(self.metrics, None, 1, self.previous)

    def test_the_block_reads_verbatim(self):
        self.assertEqual(_lines(self.rows), [
            " now          ?            · no capacity cached",
            " peak 1h      ○       17 ▲ 5 lane count unknown",
            " blocked      ○        3 ▲ 3 0 × 429",
        ])

    def test_a_missing_lane_count_never_becomes_a_denominator(self):
        # No "over ●0 by 17", and no "never over": the row says what it lacks.
        self.assertNotIn("●0", self.rows[1][1])
        self.assertNotIn("never over", self.rows[1][1])


# ---- §2.5: no traffic in the window ---------------------------------------
class NoTraffic(unittest.TestCase):
    """0 requests: the account still answers, the logs measured nothing."""

    def setUp(self):
        self.cap = _cap(4, 12, _HEALTHY_SAMPLED)
        self.metrics = _window(0, 0, cap=self.cap, scanned_at=_HEALTHY_SAMPLED)
        self.previous = _window(0, 0, cap=self.cap)
        self.rows = tui.load_rows(self.metrics, self.cap, 1, self.previous)

    def test_the_block_reads_verbatim(self):
        self.assertEqual(_lines(self.rows), [
            " now          ●     4/12   · sampled 14:31Z",
            " peak 1h      ?            · nothing measured here",
            " blocked      ?            ·",
        ])

    def test_a_row_with_nothing_to_qualify_ends_at_the_delta_column(self):
        # A trailing space would be a cell a reader counts and finds empty.
        self.assertEqual(self.rows[2][1], self.rows[2][1].rstrip())


# ---- a window with no rejection of that kind -------------------------------
class AnUnrecordedOutcome(unittest.TestCase):
    """analyse() drops an outcome key nobody hit; a window this scan read is not unknown."""

    def setUp(self):
        self.cap = _cap(3, 12, _HEALTHY_SAMPLED)
        self.quiet = {"window_hours": 1, "requests": 9, "peak_concurrency": 2,
                      "by_outcome": {"completed": 9}, "declared_plan": None,
                      "capacity": _served(self.cap),
                      "scanned_at": tui._stamp_epoch(_HEALTHY_SAMPLED)}

    def test_a_scanned_window_with_no_admission_block_reads_zero(self):
        # `○ 0`, not `?`: the window was read, nothing of that kind happened,
        # and only the 429 cross-check follows the number.
        badge, line = tui.load_rows(self.quiet, self.cap, 1)[2]
        self.assertEqual((badge, line), ("○", " blocked      ○        0   · 0 × 429"))

    def test_the_delta_counts_an_absent_outcome_as_zero(self):
        # The previous window blocked two, this one none: a measured fall.
        previous = {"window_hours": 1, "requests": 9, "peak_concurrency": 2,
                    "by_outcome": {"completed": 7, "admission_timeout": 2}}
        rows = tui.load_rows(self.quiet, self.cap, 1, previous)
        self.assertEqual(rows[2][1][25:28].strip(), "▾ 2")

    def test_a_window_nothing_was_measured_in_stays_unknown(self):
        # No traffic is not a clean window: there was nothing to be clean about.
        badge, line = tui.load_rows({"window_hours": 1, "requests": 0,
                                     "by_outcome": {}, "peak_concurrency": 0},
                                    self.cap, 1)[2]
        self.assertEqual((badge, line), ("?", " blocked      ?            ·"))


# ---- the grid --------------------------------------------------------------
class TheFixedGrid(unittest.TestCase):
    """§3.2: label 12 cells, badge one cell, number 8, delta 3, then one fact."""

    def _every_block(self):
        cap = _cap(7, 12, _SAMPLED)
        healthy = _cap(3, 12, _HEALTHY_SAMPLED)
        return {
            "over capacity": tui.load_rows(
                _window(42, 17, blocked=3, cap=cap), cap, 1,
                _window(40, 12, cap=cap)),
            "healthy": tui.load_rows(
                _window(9, 4, cap=healthy, scanned_at=_HEALTHY_SAMPLED), healthy, 1,
                _window(9, 3, cap=healthy)),
            "no capacity": tui.load_rows(
                _window(42, 17, blocked=3), None, 1, _window(40, 12)),
            "no traffic": tui.load_rows(
                _window(0, 0, cap=healthy, scanned_at=_HEALTHY_SAMPLED), healthy, 1,
                _window(0, 0, cap=healthy)),
        }

    def test_every_row_fits_the_seventy_eight_cell_budget(self):
        for name, rows in self._every_block().items():
            for line in _lines(rows):
                with self.subTest(block=name, row=line):
                    self.assertLessEqual(_cells(line), ROW_BUDGET)

    def test_the_region_is_three_rows_in_every_state(self):
        for name, rows in self._every_block().items():
            with self.subTest(block=name):
                self.assertEqual(len(rows), 3)

    def test_the_rows_are_now_peak_blocked_in_that_order(self):
        for name, rows in self._every_block().items():
            with self.subTest(block=name):
                self.assertEqual([line[1:13].strip() for line in _lines(rows)],
                                 ["now", "peak 1h", "blocked"])

    def test_the_columns_land_in_the_same_cells_in_every_state(self):
        # One cell of indent, the label cell to 14, the badge at 15, the number
        # closed at 25, the delta at 26-28 and the qualifier from 30: that is
        # the geometry a reader's eye compares row against row.
        for name, rows in self._every_block().items():
            for line in _lines(rows):
                with self.subTest(block=name, row=line):
                    self.assertEqual(line[0], " ")
                    self.assertEqual(line[13], " ")
                    self.assertEqual(_cells(line[14]), 1)
                    self.assertEqual(line[15], " ")
                    self.assertTrue(line[16:24].isascii())   # a wide digit would shift the grid
                    if len(line) > 29:
                        self.assertEqual(line[28], " ")
                        self.assertNotEqual(line[29], " ")

    def test_every_label_is_lowercase_and_fits_twelve_cells(self):
        for label in ("now", "peak 1h", "blocked"):
            self.assertEqual(label, label.lower())
            self.assertLessEqual(_cells(label), tui.LOAD_LABEL_CELLS)

    def test_the_qualifier_is_the_last_column_and_never_pushes_the_row_past_it(self):
        cap = _cap(7, 12, _SAMPLED)
        rows = tui.load_rows(_window(42, 17, blocked=3, cap=cap), cap, 1,
                             _window(40, 12, cap=cap))
        for line in _lines(rows):
            self.assertEqual(line[28], " ")
            self.assertLessEqual(_cells(line[29:]), 48)


# ---- the copy rules --------------------------------------------------------
class TheCopyRules(unittest.TestCase):
    """§7: no sentences, no `request(s)`, no field names, no parentheticals."""

    def setUp(self):
        self.cap = _cap(7, 12, _SAMPLED)
        self.lines = _lines(tui.load_rows(
            _window(42, 17, blocked=3, cap=self.cap), self.cap, 1,
            _window(40, 12, cap=self.cap)))

    def test_no_row_is_a_sentence(self):
        for line in self.lines:
            with self.subTest(row=line):
                self.assertNotIn(".", line)

    def test_no_row_carries_a_parenthetical_plural_or_a_field_name(self):
        for line in self.lines:
            with self.subTest(row=line):
                self.assertNotIn("request(s)", line)
                self.assertNotIn("`", line)
                self.assertNotIn("admission_timeout", line)
                self.assertNotIn("(", line)

    def test_no_row_carries_a_calendar_or_a_queue_wait(self):
        # The 30d/7d admission record, the overflow pool's per-day rollups, the
        # queue wait on the account's last complete UTC day and the saturation
        # bands are drill-down facts: the LOAD region never carries them.
        for line in self.lines:
            with self.subTest(row=line):
                for killed in ("day", "7d", "30d", "queue", "saturation", "band"):
                    self.assertNotIn(killed, line.lower())

    def test_the_window_sits_in_the_label_not_the_number(self):
        self.assertEqual(self.lines[1][1:13].strip(), "peak 1h")
        self.assertNotIn("hour", self.lines[1])


# ---- the delta column ------------------------------------------------------
class TheDeltaColumn(unittest.TestCase):
    """Gated by `baseline.FLOOR` on both windows, and never mooned."""

    def _rows(self, previous):
        cap = _cap(7, 12, _SAMPLED)
        return tui.load_rows(_window(42, 17, blocked=3, cap=cap), cap, 1, previous)

    def _delta(self, rows, index):
        return rows[index][1][25:28].strip()

    def test_a_comparable_window_shows_the_move(self):
        rows = self._rows(_window(40, 12, blocked=0))
        self.assertEqual(self._delta(rows, 1), "▲ 5")
        self.assertEqual(self._delta(rows, 2), "▲ 3")

    def test_a_window_under_the_floor_shows_no_delta_at_all(self):
        rows = self._rows(_window(baseline.FLOOR - 1, 12, blocked=1))
        self.assertEqual(self._delta(rows, 1), "·")
        self.assertEqual(self._delta(rows, 2), "·")

    def test_the_floor_is_inclusive_on_the_previous_window(self):
        at_floor = self._rows(_window(baseline.FLOOR, 12, blocked=0))
        self.assertEqual(self._delta(at_floor, 1), "▲ 5")

    def test_the_floor_is_inclusive_on_the_current_window(self):
        cap = _cap(7, 12, _SAMPLED)
        thin = tui.load_rows(_window(baseline.FLOOR - 1, 17, blocked=3, cap=cap),
                             cap, 1, _window(40, 12))
        self.assertEqual(self._delta(thin, 1), "·")

    def test_no_previous_window_is_a_placeholder_not_a_zero(self):
        rows = self._rows(None)
        self.assertEqual(self._delta(rows, 1), "·")
        self.assertEqual(self._delta(rows, 2), "·")

    def test_a_move_down_gets_the_falling_arrow(self):
        cap = _cap(7, 12, _SAMPLED)
        rows = tui.load_rows(_window(42, 9, blocked=1, cap=cap), cap, 1,
                             _window(40, 17, blocked=4))
        self.assertEqual(self._delta(rows, 1), "▾ 8")
        self.assertEqual(self._delta(rows, 2), "▾ 3")

    def test_a_count_that_was_zero_in_both_windows_has_no_change_to_report(self):
        # The §2.2 case: nothing was blocked in either window, so there is no
        # move to point at and the cell is the same `·` a missing window gets.
        cap = _cap(7, 12, _SAMPLED)
        rows = tui.load_rows(_window(9, 4, blocked=0, cap=cap), cap, 1,
                             _window(9, 3, blocked=0))
        self.assertEqual(self._delta(rows, 1), "▲ 1")
        self.assertEqual(self._delta(rows, 2), "·")

    def test_a_comparable_window_that_did_not_move_reads_as_unchanged(self):
        # §3.2 gives `▬` for no movement and `·` for the absence of a
        # comparable window: two different facts, two different cells.
        cap = _cap(7, 12, _SAMPLED)
        rows = tui.load_rows(_window(42, 17, blocked=3, cap=cap), cap, 1,
                             _window(40, 17, blocked=3))
        self.assertEqual(self._delta(rows, 1), "▬")
        self.assertEqual(self._delta(rows, 2), "▬")

    def test_a_ten_request_move_still_fits_three_cells(self):
        cell = tui.delta_cells({"requests": 40, "peak_concurrency": 27},
                               {"requests": 40, "peak_concurrency": 17},
                               "peak_concurrency")
        self.assertEqual(cell, "▲10")
        self.assertLessEqual(_cells(cell), tui.LOAD_DELTA_CELLS)

    def test_no_delta_cell_ever_wears_a_moon(self):
        # A row cannot know whether its own move crossed a wall, so severity
        # stays with the region: the arrows say direction and nothing else.
        moons = set("\U0001F311\U0001F312\U0001F313\U0001F314"
                    "\U0001F315\U0001F316\U0001F317\U0001F318")
        for current, before in ((27, 17), (5, 12), (12, 12)):
            cell = tui.delta_cells({"requests": 40, "peak_concurrency": current},
                                   {"requests": 40, "peak_concurrency": before},
                                   "peak_concurrency")
            self.assertFalse(moons & set(cell), cell)
            self.assertLessEqual(_cells(cell), tui.LOAD_DELTA_CELLS)


# ---- the now row's freshness ----------------------------------------------
class TheNowRowFreshness(unittest.TestCase):
    """§5.4: `now_fresh` decides the badge, and the dataclass has no such field."""

    def setUp(self):
        self.cap = _cap(7, 12, _SAMPLED)

    def _now(self, metrics, cap=_DEFAULT_CAP):
        return tui.load_rows(metrics, self.cap if cap is _DEFAULT_CAP else cap, 1)[0]

    def test_a_fresh_sample_wears_the_account_badge_and_its_sample_time(self):
        badge, line = self._now(_window(42, 17, cap=self.cap))
        self.assertEqual(badge, "●")
        self.assertIn("sampled 14:20Z", line)
        self.assertNotIn(".956", line)   # microseconds are not for a screen

    def test_a_stale_sample_keeps_the_numbers_and_loses_the_badge(self):
        # The scan closed 47 minutes after the account's sample.
        badge, line = self._now(_window(42, 17, cap=self.cap, fresh=False,
                                        scanned_at="2026-11-02T15:07:43Z"))
        self.assertEqual(badge, "?")
        self.assertEqual(line, " now          ?     7/12   · stale 47m")

    def test_a_sample_with_no_readable_age_still_loses_the_badge(self):
        # No scan clock to measure the sample against: the row refuses the
        # authority without inventing an age for it.
        badge, line = self._now({"window_hours": 1, "requests": 42,
                                 "by_outcome": {}, "peak_concurrency": 17,
                                 "capacity": {"now_fresh": False}})
        self.assertEqual(badge, "?")
        self.assertEqual(line, " now          ?     7/12   · stale sample")

    def test_no_cached_payload_prints_no_ratio_and_says_why(self):
        badge, line = self._now(_window(42, 17), cap=None)
        self.assertEqual(badge, "?")
        self.assertNotIn("/", line)
        self.assertIn("no capacity cached", line)

    def test_a_cached_payload_with_no_live_sample_is_a_different_absence(self):
        # The cache answered, the live block did not: the row says the sample is
        # missing rather than claiming there is no cache.
        quiet = _cap(None, 12, None)
        badge, line = self._now(_window(42, 17, cap=quiet), cap=quiet)
        self.assertEqual(badge, "?")
        self.assertNotIn("/", line)
        self.assertIn("no live sample", line)
        self.assertNotIn("no capacity cached", line)

    def test_the_freshness_flag_is_what_moves_the_badge(self):
        # The Capacity payload is identical in both calls: only the flag differs.
        fresh = self._now(_window(42, 17, cap=self.cap, fresh=True))
        stale = self._now(_window(42, 17, cap=self.cap, fresh=False))
        self.assertEqual((fresh[0], stale[0]), ("●", "?"))
        self.assertNotEqual(fresh[1], stale[1])


if __name__ == "__main__":
    unittest.main()
