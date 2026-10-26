"""Tests for luna_doctor/baseline.py -- the window before this one.

The module exists to stop one specific lie: a two-sample "win" printed as an
improvement. So most of what follows is about what the comparison *refuses*
to say -- the floor that makes a thin window `inconclusive`, the tie that
falls through to the next pressure signal, the missing denominator that
reads `-- (no requests)` instead of 0%, and the copy that may not claim
causation or bill the wrong quantity.

Everything is pure: the windows are built here, the clock never is.
"""

import re
import sys
import unittest

from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from luna_doctor import baseline        # noqa: E402

# Words no emitted string may carry: the billing vocabulary (LunaRoute bills
# concurrent sessions, never credits or tokens), the names of the surfaces
# this comparison is not, and the causal verbs a before/after may not use.
# Scanned as substrings, case-insensitively, so a prefix cannot slip past.
FORBIDDEN = ("credit", "spend", "token", "probe", "load-test", "compare",
             "because", "caused", "proves", "fixed", "since")

COUNT_CELL = re.compile(r"^\d+ -> \d+$")
PERCENT_SIDE = r"(?:\d+\.\d%|-- \(no requests\)|unknown)"
CONVERSION_CELL = re.compile(r"^%s -> %s$" % (PERCENT_SIDE, PERCENT_SIDE))

NOW = 1_700_000_000.0


def window(requests=10, admission_timeout=0, client_idle_timeout=0,
           rate_limited=0, peak_concurrency=1, admitted_pct=100.0):
    """One analyse() dict carrying the keys the comparison reads.

    The outcome keys that decide the verdict are arguments; the rest are the
    zeros analyse() would have produced for a quiet window.
    """
    return {
        "requests": requests,
        "by_outcome": {
            "admission_timeout": admission_timeout,
            "client_idle_timeout": client_idle_timeout,
            "rate_limited": rate_limited,
            "completed": requests,
            "upstream_unavailable": 0,
            "upstream_error": 0,
        },
        "peak_concurrency": peak_concurrency,
        "admission_conversion": {"interactive": {"admitted_pct": admitted_pct}},
        "starved_peer_evidence": 0,
        "window_hours": 24.0,
    }


def forbidden_hits(text):
    low = text.lower()
    return [word for word in FORBIDDEN if word in low]


def rows_by_label(current, previous, floor=baseline.FLOOR):
    return dict(baseline.change_rows(current, previous, floor))


def emitted_strings():
    """Every string the module can hand a user, across a matrix of windows.

    The matrix forces each branch: zero windows on either side, a null
    conversion, a large peak beside a small one, every pressure moving, and
    a floor of zero so the judged path always renders too.
    """
    quiet = window(requests=40)
    thin = window(requests=3, admission_timeout=1)
    empty = window(requests=0, peak_concurrency=0, admitted_pct=None)
    background_only = window(requests=12, admitted_pct=None)
    blocked = window(requests=50, admission_timeout=6, peak_concurrency=2,
                     admitted_pct=64.0)
    reopened = window(requests=50, peak_concurrency=9, admitted_pct=100.0)
    stalled = window(requests=30, client_idle_timeout=4)
    throttled = window(requests=30, rate_limited=7)
    pairs = [
        (quiet, quiet),
        (reopened, blocked),
        (blocked, reopened),
        (stalled, quiet),
        (throttled, quiet),
        (thin, quiet),
        (empty, quiet),
        (quiet, empty),
        (empty, empty),
        (background_only, quiet),
    ]
    out = []
    for current, previous in pairs:
        for floor in (0, baseline.FLOOR, 100):
            for label, value in baseline.change_rows(current, previous, floor):
                out.extend([label, value])
            word, reason = baseline.change_verdict(current, previous, floor)
            out.extend([word, reason])
    return out


class PreviousWindowArithmetic(unittest.TestCase):
    """The predecessor window: same length, ends where the current starts."""

    def test_previous_window_ends_where_the_current_one_starts(self):
        start, end = baseline.previous_window(NOW, 24)
        self.assertEqual(NOW - 86400.0, end)
        self.assertEqual(NOW - 172800.0, start)

    def test_previous_window_is_the_same_length_as_the_current_one(self):
        for hours in (24, 1, 0.5, 1.5, 6.25):
            start, end = baseline.previous_window(NOW, hours)
            span = hours * 3600.0
            self.assertAlmostEqual(span, end - start, places=6,
                                   msg="hours=%r" % hours)
            self.assertAlmostEqual(span, NOW - end, places=6,
                                   msg="hours=%r" % hours)

    def test_previous_window_handles_less_than_one_hour(self):
        start, end = baseline.previous_window(NOW, 0.5)
        self.assertEqual(NOW - 1800.0, end)
        self.assertEqual(NOW - 3600.0, start)

    def test_previous_window_handles_non_integer_hours(self):
        start, end = baseline.previous_window(NOW, 1.5)
        self.assertEqual(NOW - 5400.0, end)
        self.assertEqual(NOW - 10800.0, start)

    def test_the_two_windows_abut_with_no_gap_and_no_overlap(self):
        for hours in (24, 0.25, 3, 1.5):
            start, end = baseline.previous_window(NOW, hours)
            self.assertEqual(NOW - hours * 3600.0, end,
                             "the predecessor must end at the current start")
            self.assertLess(start, end)
            # chaining: the window before the predecessor ends at its start
            older_start, older_end = baseline.previous_window(end, hours)
            self.assertEqual(start, older_end)
            self.assertEqual(older_start, start - (end - start))

    def test_previous_window_is_pure_arithmetic_of_its_arguments(self):
        self.assertEqual(baseline.previous_window(NOW, 2),
                         baseline.previous_window(NOW, 2))
        start, end = baseline.previous_window(NOW + 10, 2)
        self.assertEqual((start - 10, end - 10), baseline.previous_window(NOW, 2))


class AccountLimitSignal(unittest.TestCase):
    def test_a_window_that_gained_named_refusals_is_worse(self):
        """classify() assigns org_concurrency_limit before rate_limited, so a
        comparison that ignores it calls ten new refusals "unchanged"."""
        previous = window(requests=30, rate_limited=0)
        current = window(requests=30)
        current["by_outcome"]["org_concurrency_limit"] = 10
        word, reason = baseline.change_verdict(current, previous)
        self.assertEqual("worse", word, reason)
        self.assertIn("refusals at the account limit", reason)


class Verdict(unittest.TestCase):
    """The four words, and how precedence and the floor reach them."""

    def test_fewer_blocked_at_admission_is_better(self):
        word, reason = baseline.change_verdict(
            window(admission_timeout=1), window(admission_timeout=5))
        self.assertEqual("better", word)
        self.assertIn("blocked at admission", reason)
        self.assertIn("5 of 10 requests", reason)
        self.assertIn("1 of 10 requests", reason)

    def test_more_blocked_at_admission_is_worse(self):
        word, reason = baseline.change_verdict(
            window(admission_timeout=5), window(admission_timeout=1))
        self.assertEqual("worse", word)
        self.assertIn("1 of 10 requests", reason)
        self.assertIn("5 of 10 requests", reason)

    def test_a_smaller_window_with_a_worse_rate_is_not_an_improvement(self):
        """5 blocked of 40 is a 12.5% rate. 2 blocked of 9 is 22%. A count-only
        comparison calls that an improvement and gets the direction wrong."""
        previous = window(requests=40, admission_timeout=5)
        current = window(requests=9, admission_timeout=2)
        word, reason = baseline.change_verdict(current, previous)
        self.assertEqual("worse", word, reason)
        self.assertIn("12.5%", reason)
        self.assertIn("22.2%", reason)

    def test_equal_windows_are_unchanged(self):
        quiet = window(requests=40, peak_concurrency=4, admitted_pct=97.5)
        word, reason = baseline.change_verdict(quiet, dict(quiet))
        self.assertEqual("unchanged", word)
        self.assertIn("same rate", reason)

    def test_the_floor_counts_the_smaller_window(self):
        big, small = window(requests=400), window(requests=7)
        self.assertEqual("inconclusive", baseline.change_verdict(big, small)[0])
        self.assertEqual("inconclusive", baseline.change_verdict(small, big)[0])

    def test_a_window_one_below_the_floor_is_inconclusive(self):
        below = window(requests=baseline.FLOOR - 1, admission_timeout=9)
        quiet = window(requests=50)
        word, reason = baseline.change_verdict(below, quiet)
        self.assertEqual("inconclusive", word)
        self.assertIn("below the floor of %d" % baseline.FLOOR, reason)

    def test_a_window_exactly_at_the_floor_is_judged(self):
        at_floor = window(requests=baseline.FLOOR)
        quiet = window(requests=50, admission_timeout=3)
        word, reason = baseline.change_verdict(at_floor, quiet)
        self.assertEqual("better", word)
        self.assertNotIn("floor", reason)

    def test_the_inconclusive_reason_names_the_small_count(self):
        word, reason = baseline.change_verdict(window(requests=2),
                                               window(requests=90))
        self.assertEqual("inconclusive", word)
        self.assertIn("2 requests", reason)
        self.assertIn("floor of %d" % baseline.FLOOR, reason)

    def test_no_requests_either_side_is_inconclusive_not_a_crash(self):
        empty = window(requests=0, peak_concurrency=0, admitted_pct=None)
        word, reason = baseline.change_verdict(empty, window(requests=0,
                                                            peak_concurrency=0,
                                                            admitted_pct=None))
        self.assertEqual("inconclusive", word)
        self.assertIn("0 requests", reason)

    def test_blocked_falling_while_429s_rise_is_better(self):
        """Precedence, not arithmetic: admission deaths outrank 429s."""
        current = window(requests=50, admission_timeout=1, rate_limited=9)
        previous = window(requests=50, admission_timeout=6, rate_limited=0)
        word, reason = baseline.change_verdict(current, previous)
        self.assertEqual("better", word)
        self.assertIn("blocked at admission", reason)
        self.assertNotIn("429", reason)

    def test_blocked_rising_while_429s_fall_is_worse(self):
        current = window(requests=50, admission_timeout=6, rate_limited=0)
        previous = window(requests=50, admission_timeout=1, rate_limited=9)
        word, reason = baseline.change_verdict(current, previous)
        self.assertEqual("worse", word)
        self.assertIn("blocked at admission", reason)

    def test_silent_stalls_decide_when_admission_is_tied(self):
        current = window(requests=50, client_idle_timeout=1)
        previous = window(requests=50, client_idle_timeout=4)
        word, reason = baseline.change_verdict(current, previous)
        self.assertEqual("better", word)
        self.assertIn("silent stalls", reason)

    def test_429s_decide_when_admission_and_stalls_are_tied(self):
        current = window(requests=50, rate_limited=8)
        previous = window(requests=50, rate_limited=2)
        word, reason = baseline.change_verdict(current, previous)
        self.assertEqual("worse", word)
        self.assertIn("concurrency rejections (429)", reason)

    def test_peak_concurrency_and_conversion_never_decide_alone(self):
        current = window(requests=50, peak_concurrency=11, admitted_pct=41.0)
        previous = window(requests=50, peak_concurrency=2, admitted_pct=100.0)
        self.assertEqual("unchanged", baseline.change_verdict(current, previous)[0])

    def test_the_floor_is_the_callers_to_set(self):
        current = window(requests=3, admission_timeout=0)
        previous = window(requests=3, admission_timeout=2)
        self.assertEqual("better", baseline.change_verdict(current, previous, 3)[0])
        self.assertEqual("inconclusive",
                         baseline.change_verdict(current, previous, 4)[0])

    def test_the_verdict_word_is_always_one_of_four(self):
        pairs = [(window(admission_timeout=a), window(admission_timeout=b))
                 for a in range(0, 3) for b in range(0, 3)]
        pairs.append((window(requests=0), window(requests=0)))
        pairs.append((window(requests=2), window(requests=60)))
        for current, previous in pairs:
            word, reason = baseline.change_verdict(current, previous)
            self.assertIn(word, ("better", "worse", "unchanged", "inconclusive"))
            self.assertTrue(reason.startswith(word + ": "),
                            "reason must open with its own word: %r" % reason)

    def test_a_missing_outcome_key_counts_as_zero(self):
        sparse = {"requests": 40, "by_outcome": {}}
        word, reason = baseline.change_verdict(sparse, window(requests=40))
        self.assertEqual("unchanged", word)
        rows = rows_by_label(sparse, sparse)
        self.assertEqual("40 -> 40", rows["requests"])
        self.assertEqual("0 -> 0", rows["blocked at admission"])
        self.assertEqual("unchanged", rows["change"])


class Rows(unittest.TestCase):
    """The table the caller renders: fixed order, one cell shape."""

    def test_rows_come_in_their_fixed_order(self):
        labels = [label for label, _ in
                  baseline.change_rows(window(), window())]
        self.assertEqual(["requests", "blocked at admission", "silent stalls",
                          "concurrency rejections (429)",
                          "refusals at the account limit", "peak concurrency",
                          "admission conversion", "change"], labels)

    def test_every_count_cell_reads_previous_then_current(self):
        current = window(requests=12, admission_timeout=3,
                         client_idle_timeout=1, rate_limited=4,
                         peak_concurrency=7)
        previous = window(requests=40, admission_timeout=9,
                          client_idle_timeout=0, rate_limited=2,
                          peak_concurrency=5)
        rows = rows_by_label(current, previous)
        self.assertEqual("40 -> 12", rows["requests"])
        self.assertEqual("9 -> 3", rows["blocked at admission"])
        self.assertEqual("0 -> 1", rows["silent stalls"])
        self.assertEqual("2 -> 4", rows["concurrency rejections (429)"])
        self.assertEqual("5 -> 7", rows["peak concurrency"])
        for label in ("requests", "blocked at admission", "silent stalls",
                      "concurrency rejections (429)", "peak concurrency"):
            self.assertRegex(rows[label], COUNT_CELL)

    def test_admission_conversion_reads_previous_then_current(self):
        rows = rows_by_label(window(admitted_pct=88.0), window(admitted_pct=64.5))
        self.assertEqual("64.5% -> 88.0%",
                         rows["admission conversion"])
        self.assertRegex(rows["admission conversion"], CONVERSION_CELL)

    def test_a_null_admitted_pct_is_unknown_never_zero(self):
        rows = rows_by_label(window(requests=12, admitted_pct=None),
                             window(requests=12, admitted_pct=91.0))
        self.assertEqual("91.0% -> unknown", rows["admission conversion"])
        sides = rows["admission conversion"].split(" -> ")
        self.assertNotIn("0%", sides)

    def test_a_window_with_no_requests_has_no_percentage(self):
        empty = window(requests=0, peak_concurrency=0, admitted_pct=None)
        rows = rows_by_label(empty, empty)
        self.assertEqual("-- (no requests) -> -- (no requests)",
                         rows["admission conversion"])
        self.assertEqual("0 -> 0", rows["requests"])

    def test_no_requests_on_the_previous_side(self):
        current = window(requests=12, admitted_pct=75.0)
        empty = window(requests=0, peak_concurrency=0, admitted_pct=None)
        rows = rows_by_label(current, empty)
        self.assertEqual("0 -> 12", rows["requests"])
        self.assertEqual("-- (no requests) -> 75.0%", rows["admission conversion"])

    def test_no_requests_on_the_current_side(self):
        previous = window(requests=12, admitted_pct=75.0)
        empty = window(requests=0, peak_concurrency=0, admitted_pct=None)
        rows = rows_by_label(empty, previous)
        self.assertEqual("12 -> 0", rows["requests"])
        self.assertEqual("75.0% -> -- (no requests)", rows["admission conversion"])

    def test_the_change_row_carries_the_verdict_word(self):
        for current, previous in ((window(admission_timeout=0),
                                   window(admission_timeout=4)),
                                  (window(requests=4), window(requests=90))):
            word, _ = baseline.change_verdict(current, previous)
            self.assertEqual(word, rows_by_label(current, previous)["change"])

    def test_the_floor_reaches_the_change_row(self):
        current = window(requests=3, admission_timeout=0)
        previous = window(requests=3, admission_timeout=2)
        self.assertEqual("better", rows_by_label(current, previous, 3)["change"])
        self.assertEqual("inconclusive",
                         rows_by_label(current, previous, 9)["change"])

    def test_no_cell_renders_nan_none_or_infinity(self):
        for text in emitted_strings():
            for bad in ("nan", "none", "inf"):
                self.assertNotIn(bad, text.lower(),
                                 "a missing number must never render as %r: %r"
                                 % (bad, text))


class Copy(unittest.TestCase):
    """What no emitted string may say, whatever the windows look like."""

    def test_forbidden_vocabulary_is_absent_from_every_emitted_string(self):
        for text in emitted_strings():
            self.assertEqual([], forbidden_hits(text),
                             "%r carries forbidden copy: %r" % (text, text))

    def test_the_forbidden_word_scan_fires_on_a_planted_sentence(self):
        planted = ("fixed because caused proves since: a probe, a load-test, "
                   "a compare, credits to spend, tokens used")
        self.assertEqual(sorted(FORBIDDEN), sorted(forbidden_hits(planted)),
                         "the scan must catch every forbidden word it claims")

    def test_text_is_ascii(self):
        for text in emitted_strings():
            self.assertTrue(text.isascii(),
                            "non-ASCII in an emitted string: %r" % text)
            self.assertNotIn("\u2192", text, "arrow glyph in %r" % text)

    def test_no_ansi_and_no_emoji(self):
        for text in emitted_strings():
            self.assertNotIn("\033", text)
            self.assertTrue(all(ord(ch) < 0x2100 for ch in text),
                            "emoji or symbol in %r" % text)

    def test_the_change_row_word_is_copy_too(self):
        rows = rows_by_label(window(requests=0, admitted_pct=None),
                             window(requests=0, admitted_pct=None))
        self.assertEqual("inconclusive", rows["change"])


if __name__ == "__main__":
    unittest.main()
