"""Tests for luna_doctor/watch.py - the live view of one machine's pressure.

The module is pure functions over two dicts and a timestamp, so these tests
are too: no log store, no capacity cache, no terminal, no clock. What is
asserted here is what a watcher can read off a screen mid-incident, so the
checks are mostly about honesty rather than arithmetic:

- the one-line surface is one line: ASCII, no ANSI, no newline, <= 100
  characters, because it is printed to a terminal and appended to a pipe;
- every number says whose it is. This machine's peak and refusal counts are
  not the account's occupancy, the two are never added, and a number the tool
  does not have is unknown rather than zero;
- the frame fits the width it is drawn in and never goes blank;
- the summary's duration is the two timestamps the caller passed, not a
  clock read anywhere in the module.
"""

import re
import unittest

from luna_doctor import watch

# Words the product does not use: concurrency is billed, so nothing here
# counts tokens, credits or spend, and the load-test surfaces are not part of
# a watch. "fan-out" and "lane" are the retired capacity vocabulary the rest
# of the suite already forbids.
FORBIDDEN = ("token", "credit", "spend", "cost", "dollar", "price",
             "fan-out", "fanout", "fan out", "lane", "probe", "load-test",
             "compare")

ANSI = re.compile(r"\x1b\[")


# ---- fixtures ---------------------------------------------------------------
def local_store(by_outcome=None, peak=11, windows=6):
    """The shape analyse() returns, reduced to the keys watch reads."""
    return {
        "requests": 40,
        "by_outcome": dict(by_outcome or {}),
        "peak_concurrency": peak,
        "pressure_windows": windows,
    }


def account(in_use=11, limit=12, saturated=False,
            sampled_at="2026-10-24T17:44:03.510815919Z"):
    """The capacity payload's live sample: the account's own occupancy."""
    # cli._capacity_metrics decides freshness against the window; these fixtures
    # describe a sample that covers it.
    return {"provenance": "account", "plan": "apex", "priority_lanes": 12,
            "now_fresh": True,
            "now": {"in_use": in_use, "limit": limit, "available": 1,
                    "saturated": saturated, "sampled_at": sampled_at}}


def all_surfaces():
    """One of every string this module can hand a caller, by surface.

    Two shapes per surface: a pressured sample with every refusal class
    populated, and a cold one with no account payload at all - the branch
    that must say unknown instead of borrowing a zero.
    """
    hot_local = local_store({"admission_timeout": 4, "rate_limited": 3,
                             "org_concurrency_limit": 2}, peak=11, windows=6)
    cold_local = {}
    hot_prev = local_store({"admission_timeout": 0}, peak=0, windows=0)
    surfaces = {
        "sample_line": [
            watch.sample_line(hot_local, account(), "2026-10-24T17:42:03Z",
                              previous=hot_prev),
            watch.sample_line(cold_local, None, "2026-10-24T17:42:03Z"),
        ],
        "frame": watch.frame(hot_local, account(), "2026-10-24T17:42:03Z",
                             ["17:42:03Z pressure: none"], width=72)
        + watch.frame(cold_local, None, "2026-10-24T17:42:03Z", [], width=72),
        "summary": watch.summary(hot_local, account(), 17,
                                 "2026-10-24T17:40:05Z", "2026-10-24T17:44:17Z")
        + watch.summary(cold_local, None, 0,
                        "2026-10-24T17:40:05Z", "2026-10-24T17:44:17Z"),
    }
    return surfaces


def clauses(line):
    """A sample line split at its clause separators."""
    return line.split(watch.PIPE_SEPARATOR)


# ---- the one-line surface ---------------------------------------------------
class SampleLine(unittest.TestCase):
    def test_one_plain_ascii_line_inside_the_cap(self):
        line = watch.sample_line(
            local_store({"admission_timeout": 4}), account(),
            "2026-10-24T17:42:03Z", previous=local_store(peak=0))
        self.assertNotIn("\n", line)
        self.assertEqual(line, line.strip(), "a sample line carries no padding")
        self.assertIsNone(ANSI.search(line), "the parent owns cursor control")
        self.assertTrue(all(ord(ch) < 128 for ch in line),
                        "a piped line must not need a UTF-8 locale: %r" % line)
        self.assertLessEqual(len(line), 100, line)

    def test_shape_is_a_clock_then_pressure_then_owners(self):
        line = watch.sample_line(
            local_store({"admission_timeout": 4}, peak=11), account(),
            "2026-10-24T17:42:03Z")
        self.assertTrue(line.startswith("17:42:03Z "), line)
        self.assertIn("pressure: 4 blocked at admission", line)
        self.assertIn("peak 11 (this machine)", line)
        self.assertIn("11/12 in use (account)", line)

    def test_a_count_that_moved_says_what_it_was(self):
        line = watch.sample_line(
            local_store({"admission_timeout": 4}, peak=9), account(),
            "2026-10-24T17:42:03Z",
            previous=local_store({"admission_timeout": 0}, peak=9))
        self.assertIn("4 blocked at admission (was 0)", line)

    def test_a_count_that_did_not_move_carries_no_was_note(self):
        """A line printed because the account's occupancy moved must not
        decorate an unchanged local count with a delta it does not have."""
        line = watch.sample_line(
            local_store({"admission_timeout": 4}, peak=9), account(in_use=12),
            "2026-10-24T17:44:03Z",
            previous=local_store({"admission_timeout": 4}, peak=9))
        self.assertIn("4 blocked at admission", line)
        self.assertNotIn("(was", line)

    def test_the_cap_holds_when_every_count_is_absurd(self):
        """The 100-character promise is a promise about any input, not just
        the tidy two-digit one: five-digit counts still fit or get cut."""
        huge = local_store({"admission_timeout": 99999, "rate_limited": 99998,
                            "org_concurrency_limit": 99997}, peak=99999)
        line = watch.sample_line(huge, account(in_use=99999, limit=99999),
                                 "2026-10-24T17:42:03Z", previous=huge)
        self.assertLessEqual(len(line), 100, line)
        self.assertIn("blocked at admission", line)
        self.assertTrue(all(ord(ch) < 128 for ch in line))

    def test_an_unreadable_timestamp_drops_the_clock_rather_than_inventing_one(self):
        line = watch.sample_line(local_store(), account(), "not a timestamp")
        self.assertTrue(line.startswith("pressure: "), line)

    def test_the_same_input_renders_the_same_line(self):
        first = watch.sample_line(local_store({"rate_limited": 2}), account(),
                                  "2026-10-24T17:42:03Z")
        second = watch.sample_line(local_store({"rate_limited": 2}), account(),
                                   "2026-10-24T17:42:03Z")
        self.assertEqual(first, second)


# ---- whose number is whose --------------------------------------------------
class OwnerLabels(unittest.TestCase):
    def test_every_number_sits_in_a_clause_that_names_its_owner(self):
        lines = (
            watch.sample_line(local_store({"admission_timeout": 4}), account(),
                              "2026-10-24T17:42:03Z",
                              previous=local_store(peak=0)),
            watch.sample_line(local_store({"rate_limited": 3}), None,
                              "2026-10-24T17:42:03Z"),
            watch.sample_line({}, account(), "2026-10-24T17:42:03Z"),
        )
        for line in lines:
            parts = clauses(line)
            account_side = parts[-1]
            local_side = " ".join(parts[:-1])
            self.assertIn("(account)", account_side,
                          "the account's number must say it is the account's: %r" % line)
            self.assertNotIn("(account)", local_side, line)
            if re.search(r"\d", local_side):
                self.assertIn("(this machine)", local_side,
                              "a local number with no owner: %r" % line)
            self.assertNotIn("(this machine)", account_side, line)

    def test_the_two_halves_are_never_added(self):
        """A local peak of 11 inside an account holding 11 of 12 must not
        produce 22 anywhere: the peak is this machine's, the occupancy the
        account's, and the line says each on its own."""
        line = watch.sample_line(local_store({}, peak=11), account(in_use=11, limit=12),
                                 "2026-10-24T17:42:03Z")
        self.assertNotIn("22", line)
        self.assertIn("peak 11 (this machine)", line)
        self.assertIn("11/12 in use (account)", line)

    def test_a_saturated_pool_is_the_accounts_word_not_ours(self):
        line = watch.sample_line(local_store({}), account(in_use=12, limit=12),
                                 "2026-10-24T17:42:03Z")
        self.assertIn("12/12 in use (account)", line)

    def test_an_absent_value_is_unknown_never_zero(self):
        lines = [watch.sample_line({}, None, "2026-10-24T17:42:03Z"),
                 watch.sample_line({}, {}, "2026-10-24T17:42:03Z")]
        for line in lines:
            self.assertIn("peak unknown (this machine)", line)
            self.assertIn("in use unknown (account)", line)
            self.assertNotIn("0/", line)
            self.assertNotIn("of 0", line)
            self.assertNotIn("nan", line.lower())


# ---- change detection -------------------------------------------------------
class StateChanged(unittest.TestCase):
    def test_the_first_sample_always_changed(self):
        self.assertTrue(watch.state_changed(None, local_store()))

    def test_nothing_moved(self):
        self.assertFalse(watch.state_changed(
            local_store({"admission_timeout": 4}, peak=11), local_store({"admission_timeout": 4}, peak=11),
            capacity_previous=account(), capacity_current=account()))

    def test_a_blocked_count_moved(self):
        self.assertTrue(watch.state_changed(
            local_store({"admission_timeout": 0}, peak=11),
            local_store({"admission_timeout": 4}, peak=11),
            capacity_previous=account(), capacity_current=account()))

    def test_only_the_accounts_in_use_moved(self):
        """The owner of a refusal can move without this machine moving at all,
        and that is the whole reason the account half is on the line."""
        self.assertTrue(watch.state_changed(
            local_store({"admission_timeout": 4}, peak=11),
            local_store({"admission_timeout": 4}, peak=11),
            capacity_previous=account(in_use=4), capacity_current=account(in_use=12)))

    def test_the_account_payload_appearing_is_a_change(self):
        self.assertTrue(watch.state_changed(
            local_store({}), local_store({}),
            capacity_previous=None, capacity_current=account()))


# ---- the frame --------------------------------------------------------------
class Frame(unittest.TestCase):
    def test_every_line_fits_the_width_and_none_is_empty(self):
        history = [watch.sample_line(local_store({"admission_timeout": n}),
                                     account(), "2026-10-24T17:42:03Z")
                   for n in (0, 4, 7)]
        for width in (40, 72, 100):
            lines = watch.frame(local_store({"admission_timeout": 7}), account(),
                                "2026-10-24T17:44:03Z", history, width=width)
            self.assertTrue(lines, "a frame with no lines is not a frame")
            for line in lines:
                self.assertLessEqual(len(line), width, line)
                self.assertNotEqual(line.strip(), "", "a blank row moves the view")

    def test_the_header_carries_the_current_numbers_with_their_owners(self):
        lines = watch.frame(local_store({"admission_timeout": 4}), account(),
                            "2026-10-24T17:44:03Z", [])
        head = " ".join(lines)
        self.assertIn("luna-doctor watch 17:44:03Z", head)
        self.assertIn("pressure: 4 blocked at admission", head)
        self.assertIn("peak 11 (this machine)", head)
        self.assertIn("11 of 12 in use (account)", head)

    def test_the_history_tail_is_last_and_in_the_order_given(self):
        history = ["17:40:03Z pressure: none | peak 1 (this machine) | 3/12 in use (account)",
                   "17:41:03Z pressure: 2 blocked at admission | peak 4 (this machine) | 5/12 in use (account)"]
        lines = watch.frame(local_store(), account(), "2026-10-24T17:42:03Z",
                            history, width=100)
        self.assertEqual(history, lines[-2:])

    def test_a_tight_width_keeps_the_account_clause(self):
        """Narrow enough that the peak has to go: the account's occupancy is
        the clause that must survive, because it says whose pool was full."""
        line = watch.sample_line(local_store({"admission_timeout": 4}, peak=11),
                                 account(), "2026-10-24T17:42:03Z")
        lines = watch.frame(local_store({"admission_timeout": 4}), account(),
                            "2026-10-24T17:42:03Z", [line], width=72)
        self.assertIn("11/12 in use (account)", lines[-1])
        self.assertLessEqual(len(lines[-1]), 72)

    def test_a_header_full_of_refusal_classes_is_shortened_by_class_not_cut(self):
        """A header row carries no owner label of its own, so a pressure clause
        cut mid-list would leave counts with nothing saying whose they are."""
        hot = local_store({"admission_timeout": 4, "rate_limited": 3,
                           "org_concurrency_limit": 2})
        lines = watch.frame(hot, account(), "2026-10-24T17:42:03Z", [], width=72)
        pressure = [line for line in lines if line.startswith("pressure: ")]
        self.assertEqual(1, len(pressure), lines)
        self.assertIn("blocked at admission", pressure[0])
        self.assertFalse(pressure[0].endswith("..."), pressure[0])
        for line in lines:
            self.assertLessEqual(len(line), 72)

    def test_a_cold_payload_still_renders(self):
        lines = watch.frame({}, None, "2026-10-24T17:42:03Z", [])
        self.assertTrue(lines)
        head = " ".join(lines)
        self.assertIn("peak unknown (this machine)", head)
        self.assertIn("in use unknown (account)", head)
        self.assertIn("luna-doctor capacity --online", head,
                      "the frame names the command that fetches the missing half")

    def test_the_same_input_renders_the_same_frame(self):
        history = ["17:42:03Z pressure: none | peak 0 (this machine) | 1/12 in use (account)"]
        first = watch.frame(local_store(), account(), "2026-10-24T17:44:03Z", history)
        second = watch.frame(local_store(), account(), "2026-10-24T17:44:03Z", history)
        self.assertEqual(first, second)


# ---- the summary ------------------------------------------------------------
class Summary(unittest.TestCase):
    def test_arithmetic_comes_from_the_two_timestamps_and_the_local_half(self):
        lines = watch.summary(local_store({"admission_timeout": 4}, peak=11, windows=6),
                              account(), 17,
                              "2026-10-24T17:40:05Z", "2026-10-24T17:44:17Z")
        self.assertEqual(3, len(lines))
        self.assertIn("watched 4m12s", lines[0])
        self.assertIn("17 samples", lines[0])
        self.assertIn("6 pressure windows (this machine)", lines[0])
        self.assertIn("peak 11 (this machine)", lines[1])
        self.assertIn("4 blocked at admission (this machine)", lines[1])
        self.assertIn("11 of 12 in use at 17:44Z (account)", lines[2])

    def test_a_watch_that_crosses_midnight_still_measures(self):
        lines = watch.summary({}, None, 2,
                              "2026-10-24T23:59:00Z", "2026-10-25T00:01:30Z")
        self.assertIn("watched 2m30s", lines[0])

    def test_a_short_watch_reads_in_seconds_and_one_sample_is_singular(self):
        lines = watch.summary({}, None, 1,
                              "2026-10-24T17:40:05Z", "2026-10-24T17:40:13Z")
        self.assertIn("watched 8s", lines[0])
        self.assertIn("1 sample ", lines[0])

    def test_an_unreadable_timestamp_is_unknown_not_zero(self):
        lines = watch.summary({}, None, 3,
                              "2026-10-24T17:40:05Z", "the end")
        self.assertIn("watched unknown", lines[0])
        self.assertNotIn("0s", lines[0])

    def test_the_sample_count_can_be_the_history_itself(self):
        history = ["17:40:03Z pressure: none | peak 0 (this machine) | 1/12 in use (account)",
                   "17:41:03Z pressure: none | peak 0 (this machine) | 1/12 in use (account)"]
        lines = watch.summary({}, None, history,
                              "2026-10-24T17:40:05Z", "2026-10-24T17:44:17Z")
        self.assertIn("2 samples", lines[0])

    def test_a_cold_payload_ends_with_unknown_and_the_command(self):
        lines = watch.summary({}, None, 5,
                              "2026-10-24T17:40:05Z", "2026-10-24T17:44:17Z")
        self.assertIn("peak unknown (this machine)", lines[1])
        self.assertIn("in use unknown (account)", lines[2])
        self.assertIn("luna-doctor capacity --online", lines[2])
        self.assertIn("in use unknown (account). Run ", lines[2],
                      "the command is a second sentence, not a clause")


# ---- a 429-only window ------------------------------------------------------
class RejectionsAreReported(unittest.TestCase):
    def test_a_429_only_window_reports_its_rejections_not_zero_blocked(self):
        """A window whose only refusals are bare 429s is not a quiet window:
        "0 blocked" would read as "nothing happened"."""
        local = local_store({"rate_limited": 3}, peak=3)
        line = watch.sample_line(local, account(), "2026-10-24T17:42:03Z")
        self.assertIn("3 rate limited", line)
        self.assertNotIn("0 blocked", line)

        lines = watch.summary(local, account(), 4,
                              "2026-10-24T17:40:05Z", "2026-10-24T17:44:17Z")
        self.assertIn("3 rate limited (this machine)", lines[1])

    def test_a_named_account_limit_rejection_is_reported_under_its_own_name(self):
        line = watch.sample_line(local_store({"org_concurrency_limit": 2}),
                                 account(), "2026-10-24T17:42:03Z")
        self.assertIn("2 refused at the account limit", line)


# ---- the surfaces a user reads ----------------------------------------------
class EveryEmittedString(unittest.TestCase):
    def test_nothing_emits_an_ansi_escape(self):
        for surface, lines in all_surfaces().items():
            for line in lines:
                self.assertIsNone(ANSI.search(line), "%s: %r" % (surface, line))

    def test_nothing_names_a_unit_of_money_load_or_a_retired_term(self):
        for surface, lines in all_surfaces().items():
            text = " ".join(lines).lower()
            for word in FORBIDDEN:
                self.assertNotIn(word, text,
                                 "%s contains %r" % (surface, word))

    def test_sample_line_and_summary_are_ascii_because_they_cross_a_pipe(self):
        surfaces = all_surfaces()
        for surface in ("sample_line", "summary"):
            for line in surfaces[surface]:
                self.assertTrue(all(ord(ch) < 128 for ch in line),
                                "%s: %r" % (surface, line))

    def test_the_frame_separates_with_the_one_dot_and_nothing_else(self):
        for line in all_surfaces()["frame"]:
            outside_ascii = {ch for ch in line if ord(ch) > 127}
            self.assertLessEqual(outside_ascii, {"·"}, line)


class TheNewEventSurvivesShortening(unittest.TestCase):
    def test_a_new_429_is_named_ahead_of_unchanged_admission_refusals(self):
        """The line prints because three 429s arrived. Shortening must not cut
        them off in favour of a class that did not move."""
        def local(rate, adm):
            return {"requests": 20, "peak_concurrency": 11, "window_hours": 0.25,
                    "by_outcome": {"rate_limited": rate, "admission_timeout": adm,
                                   "completed": 4}}
        before = local(1, 4)
        now = local(4, 4)
        cap = account(in_use=11, limit=12)
        line = watch.sample_line(now, cap, "2026-10-26T04:00:01Z", before)
        self.assertIn("rate limited", line)
        self.assertLessEqual(len(line), 100, line)


class StaleSampleIsNotCurrent(unittest.TestCase):
    def test_a_sample_that_is_no_longer_fresh_reads_as_unknown(self):
        """Yesterday's saturation beside today's timestamp would be a fact the
        tool invented about right now."""
        stale = {"provenance": "account", "priority_lanes": 12, "now_fresh": False,
                 "now": {"in_use": 12, "limit": 12, "saturated": True,
                         "sampled_at": "2026-10-25T00:00:00Z"}}
        local = {"requests": 4, "by_outcome": {"completed": 4}, "peak_concurrency": 1,
                 "window_hours": 0.25}
        line = watch.sample_line(local, stale, "2026-10-26T04:00:00Z")
        self.assertIn("unknown", line)
        self.assertNotIn("12/12", line)


class OwnerLabelSurvivesTheCap(unittest.TestCase):
    def test_a_saturated_absurd_sample_keeps_the_account_label(self):
        """40 blocked and a full pool is the widest line this can build, and the
        owner label must still be on it."""
        local = {"requests": 90,
                 "by_outcome": {"admission_timeout": 40, "client_idle_timeout": 12},
                 "peak_concurrency": 40, "window_hours": 1.0}
        cap = {"provenance": "account", "now": {"in_use": 12, "limit": 12,
                                                "saturated": True,
                                                "sampled_at": "2026-10-26T04:00:00Z"}}
        line = watch.sample_line(local, cap, "2026-10-26T04:00:01Z")
        self.assertLessEqual(len(line), 100, line)
        self.assertIn("(account)", line)
        self.assertIn("(this machine)", line)


if __name__ == "__main__":
    unittest.main()
