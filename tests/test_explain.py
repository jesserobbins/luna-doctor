"""Tests for the per-request explain view.

Stdlib only, no fixtures to maintain, nothing written anywhere: every test
builds Request objects in memory, the way the analysis layer would.
"""

import re
import unittest

from luna_doctor import explain
from luna_doctor.cli import Request

T0 = 1_700_000_000.0


def req(harness="pi", session="s1", model="glm-5.3", start=T0, end=T0 + 10,
        outcome="completed", error_message=None, response_id=None, run="",
        tier="interactive", provider="lunaroute", thinking_chars=0, text_chars=0):
    return Request(harness=harness, session=session, model=model, run=run,
                   provider=provider, start=start, end=end, outcome=outcome,
                   error_message=error_message, response_id=response_id, tier=tier,
                   thinking_chars=thinking_chars, text_chars=text_chars)


def wide_burst(n=10, span=120.0):
    """A burst that all opens at once - the shape that produces the symptom."""
    return [req(model="glm-5.3", start=T0, end=T0 + span) for _ in range(n)]


class Selection(unittest.TestCase):
    def test_worst_prefers_the_request_that_went_wrong(self):
        slow_but_fine = req(start=T0, end=T0 + 300)
        stalled = req(start=T0, end=T0 + 95, outcome="client_idle_timeout")
        picked = explain.worst([slow_but_fine, stalled])
        self.assertEqual(1, len(picked))
        self.assertEqual("client_idle_timeout", picked[0].outcome)

    def test_worst_falls_back_to_duration_when_nothing_went_wrong(self):
        short = req(start=T0, end=T0 + 5)
        long = req(start=T0, end=T0 + 200)
        self.assertIs(long, explain.worst([short, long])[0])

    def test_blocked_is_a_refusal_not_a_stall(self):
        """A stall is the default view; --blocked means the gateway said no."""
        stalled = req(start=T0, end=T0 + 95, outcome="client_idle_timeout")
        refused = req(start=T0 + 50, end=T0 + 50.4, outcome="org_concurrency_limit")
        picked = explain.blocked([stalled, refused])
        self.assertEqual(1, len(picked))
        self.assertEqual("org_concurrency_limit", picked[0].outcome)

    def test_blocked_on_a_clean_window_is_empty(self):
        self.assertEqual([], explain.blocked([req()]))

    def test_slowest_is_strictly_duration(self):
        slow_but_fine = req(start=T0, end=T0 + 300)
        stalled = req(start=T0, end=T0 + 95, outcome="client_idle_timeout")
        self.assertIs(slow_but_fine, explain.slowest([slow_but_fine, stalled])[0])

    def test_last_n_returns_n(self):
        reqs = [req(start=T0, end=T0 + 10 + i) for i in range(5)]
        self.assertEqual(3, len(explain.slowest(reqs, 3)))

    def test_by_id_matches_response_id_session_and_run(self):
        r = req(session="sess-abc", run="run-1", response_id="resp-9")
        self.assertEqual([r], explain.by_id([r], "resp-9"))
        self.assertEqual([r], explain.by_id([r], "sess-abc"))
        self.assertEqual([r], explain.by_id([r], "run-1"))
        self.assertEqual([r], explain.by_id([r], "resp-"))
        self.assertEqual([], explain.by_id([r], "nope"))

    def test_by_id_refuses_to_guess_between_candidates(self):
        a = req(session="other", response_id="refix-a")
        b = req(session="another", response_id="refix-b")
        self.assertEqual([], explain.by_id([a, b], "refix-"))


class Why(unittest.TestCase):
    def test_a_stall_inside_a_wide_burst_blames_this_machine(self):
        victim = req(start=T0, end=T0 + 95, outcome="client_idle_timeout")
        doc = explain.explain(victim, wide_burst() + [victim], None, (6, "plan apex"))
        self.assertIn("this machine", doc["headline"].lower())
        self.assertIn("held 11 concurrent requests", doc["headline"])
        self.assertIn("declared plan apex", doc["headline"])
        self.assertTrue(doc["actions"])

    def test_a_stall_outside_the_width_report_reads_as_unknown_not_as_blame(self):
        """No width, no account half: the owner cannot be named, and the copy says so."""
        victim = req(start=T0, end=T0 + 95, outcome="client_idle_timeout")
        doc = explain.explain(victim, [req(start=T0, end=T0 + 95), victim], None, (None, "unknown"))
        self.assertIn("no width on this machine explains", doc["headline"].lower())
        self.assertIn("capacity --online", doc["actions"][0])

    def test_a_named_org_limit_refusal_needs_no_capacity_payload(self):
        """The gateway named the pool; saying less would hide the answer."""
        refused = req(start=T0, end=T0 + 0.4, outcome="org_concurrency_limit",
                      error_message='429: {"code":"CONCURRENT_REQUEST_LIMIT_EXCEEDED"}')
        doc = explain.explain(refused, wide_burst() + [refused], None, (6, "plan apex"))
        self.assertIn("CONCURRENT_REQUEST_LIMIT_EXCEEDED", doc["headline"])
        self.assertIn("11 concurrent requests", doc["headline"])
        self.assertIn("not free in the pool of the account", doc["headline"])

    def test_a_served_request_beside_a_wide_burst_is_explained_as_queueing(self):
        slow = req(start=T0, end=T0 + 145)
        doc = explain.explain(slow, wide_burst(12, 200) + [slow], None, (6, "plan apex"))
        self.assertIn("not from the latency of the model", doc["headline"])

    def test_a_fast_served_request_is_not_dressed_up_as_a_problem(self):
        doc = explain.explain(req(start=T0, end=T0 + 3), [req(start=T0, end=T0 + 3)],
                              None, (6, "plan apex"))
        self.assertIn("latency of the model itself", doc["headline"])

    def test_an_upstream_failure_names_the_gateway(self):
        failed = req(start=T0, end=T0 + 2, outcome="upstream_unavailable")
        doc = explain.explain(failed, [failed], None, (6, "plan apex"))
        self.assertIn("gateway or the transport", doc["headline"])

    def test_a_context_overflow_points_at_the_window_not_at_concurrency(self):
        bad = req(outcome="context_length_exceeded")
        doc = explain.explain(bad, [bad], None, (6, "plan apex"))
        self.assertIn("context window", doc["headline"])
        self.assertTrue(any("maxTokens" in a for a in doc["actions"]))


class Rows(unittest.TestCase):
    def test_rows_carry_owner_labels_and_never_a_bare_number(self):
        victim = req(start=T0, end=T0 + 95, outcome="client_idle_timeout",
                     response_id="resp-1", thinking_chars=2400, text_chars=0)
        doc = explain.explain(victim, wide_burst() + [victim], None, (6, "plan apex"))
        rows = dict(doc["rows"])
        self.assertIn("(this machine)", rows["peak during its span"])
        self.assertIn("(this machine)", rows["in flight when it started"])
        self.assertIn("resp-1", rows["response id"])
        self.assertIn("client_idle_timeout", rows["outcome"])
        self.assertIn("character(s) of thinking", rows["produced"])

    def test_account_occupancy_is_labelled_account_when_present(self):
        now = {"in_use": 12, "limit": 12, "sampled_at": "2026-10-24T00:05:14Z"}
        capacity = {"provenance": "account", "priority_lanes": 12, "now": now}
        doc = explain.explain(req(), [req()], capacity, (12, "account"))
        self.assertIn("12 of 12 in use", dict(doc["rows"])["account occupancy"])
        self.assertIn("(account)", dict(doc["rows"])["account occupancy"])

    def test_a_long_error_message_is_clipped(self):
        long = req(outcome="upstream_error", error_message="x" * 400)
        doc = explain.explain(long, [long], None, (6, "plan apex"))
        text = dict(doc["rows"])["error text"]
        self.assertLessEqual(len(text), 221)
        self.assertTrue(text.endswith("…"))

    def test_unknown_values_render_unknown_never_zero(self):
        doc = explain.explain(req(start=None, end=None), [req(start=None, end=None)],
                              None, (None, "unknown"))
        rows = dict(doc["rows"])
        self.assertEqual("unknown", rows["duration"])
        self.assertEqual("unknown", rows["when"])

    def test_no_error_message_means_no_row(self):
        self.assertNotIn("error text", dict(explain.explain(req(), [req()])["rows"]))


class Copy(unittest.TestCase):
    # word-boundary matched: `maxTokens` is a config key, not token framing.
    FORBIDDEN = re.compile(r"\b(credits?|spend|spends|tokens?|probe|load-test|compare)\b")

    def _all_text(self, doc):
        parts = [doc["headline"]] + [v for _, v in doc["rows"]] + list(doc["actions"])
        return "\n".join(parts).lower()

    def test_no_forbidden_framing_in_any_outcome(self):
        outcomes = list(explain.OUTCOME_COPY) + ["something_new"]
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                r = req(outcome=outcome)
                text = self._all_text(explain.explain(r, wide_burst() + [r], None,
                                                      (6, "plan apex")))
                hit = self.FORBIDDEN.search(text)
                self.assertIsNone(hit, f"{outcome}: {hit.group(0) if hit else ''}")

    def test_a_wide_burst_headline_never_calls_the_width_a_gateway_fact(self):
        victim = req(start=T0, end=T0 + 95, outcome="client_idle_timeout")
        doc = explain.explain(victim, wide_burst() + [victim], None, (6, "plan apex"))
        self.assertNotIn("gateway admits", doc["headline"].lower())
        self.assertIn("declared plan apex", doc["headline"])

    def test_outcome_copy_says_blocked_not_dead(self):
        for text in explain.OUTCOME_COPY.values():
            self.assertNotIn("died", text.lower())
            self.assertNotIn("death", text.lower())


if __name__ == "__main__":
    unittest.main()
