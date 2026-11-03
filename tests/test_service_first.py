"""Service first: the account's numbers lead, the machine's explain them.

The account is what the tool exists to explain, so its section comes before
anything this machine measured, and every number in it says whose it is. The
local sections keep their own labels, and nothing here is ever added to them.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from unittest import mock  # noqa: E402

from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")


from luna_doctor import cli


def metrics(**capacity):
    """A small analyse() metrics dict with an account block attached."""
    m = cli.analyse([], window_hours=1)
    if capacity:
        m["capacity"] = capacity
    return m


ACCOUNT = {
    "provenance": "account",
    "plan": "apex",
    "priority_lanes": 12,
    "background_lanes": 12,
    "overflow_routing": "enabled",
    "now_fresh": True,
    "now": {
        "in_use": 8, "limit": 12, "available": 4, "saturated": False,
        "ledger": "up", "sampled_at": "2026-11-03T22:14:42.219872605Z",
        "background": {"limit": 12, "in_use": 9, "available": 3, "saturated": False},
    },
    "admission_as_of": "2026-11-02",
    "window_days": [
        {"day": "2026-11-01", "accepted": 40, "deferred": 2, "denied": 1,
         "denied_after_wait": 6},
        {"day": "2026-11-02", "accepted": 30, "deferred": 0, "denied": 0,
         "denied_after_wait": 0},
    ],
}


class TheServiceSectionLeads(unittest.TestCase):
    def test_it_is_the_first_section(self):
        report = cli.build_report(metrics(**ACCOUNT), "headline", [])
        self.assertEqual(report.sections[0].title, "service")

    def test_every_account_number_says_whose_it_is(self):
        report = cli.build_report(metrics(**ACCOUNT), "headline", [])
        rows = report.sections[0].rows
        self.assertTrue(rows)
        unlabelled = [r.label for r in rows
                      if "(account)" not in r.value and "stale" not in r.value]
        self.assertEqual(unlabelled, [],
                         "an account number without (account) reads as this machine's")

    def test_the_live_pool_carries_its_sample_time(self):
        report = cli.build_report(metrics(**ACCOUNT), "headline", [])
        live = [r for r in report.sections[0].rows if r.label == "live priority pool"]
        self.assertTrue(live, "the live occupancy belongs in the service section")
        self.assertIn("in use 8 of 12 priority concurrent requests", live[0].value)
        self.assertIn("2026-11-03T22:14:42.219872605Z", live[0].value,
                      "a reading without its moment is a fact about no moment")

    def test_the_overflow_pool_is_its_own_row(self):
        report = cli.build_report(metrics(**ACCOUNT), "headline", [])
        labels = [r.label for r in report.sections[0].rows]
        self.assertIn("overflow pool", labels)
        self.assertIn("live overflow pool", labels)

    def test_the_admission_record_names_the_blocked_days(self):
        report = cli.build_report(metrics(**ACCOUNT), "headline", [])
        row = [r for r in report.sections[0].rows if r.label == "admission record"][0]
        self.assertIn("2 complete UTC day(s)", row.value)
        self.assertIn("6 denied after waiting", row.value)
        self.assertIn("most on 2026-11-01, at 6", row.value)

    def test_no_payload_means_no_section(self):
        """A machine with no account payload still gets the local report."""
        self.assertEqual(cli._service_rows(metrics()), [])
        unknown = cli._service_rows(metrics(provenance="unknown",
                                            reason="no cached capacity"))
        self.assertEqual(unknown, [])

    def test_a_stale_payload_says_the_reading_is_of_another_moment(self):
        stale = dict(ACCOUNT, stale=True)
        rows = cli._service_rows(metrics(**stale))
        labels = [r.label for r in rows]
        self.assertIn("account sample", labels)
        text = " ".join(r.value for r in rows if r.label == "account sample")
        self.assertIn("another moment", text)

    def test_a_zero_overflow_entitlement_is_not_a_width(self):
        zero = dict(ACCOUNT, background_lanes=0)
        rows = cli._service_rows(metrics(**zero))
        overflow = [r for r in rows if r.label == "overflow pool"][0]
        self.assertIn("no overflow entitlement", overflow.value)
        self.assertNotIn("0 concurrent requests", overflow.value)


if __name__ == "__main__":
    unittest.main()


class TheInsufficientEvidenceOutcome(unittest.TestCase):
    """The account can be conclusive while these logs are not.

    Jesse's shape, 2026-11-03: "you appear to be going over capacity, however
    I can't see on this machine's logs or in agentsview what is wrong, so I
    have no suggestions to give here. You can run me elsewhere, or you can ask
    your agent to collect logs". Three parts, in order, and no invented advice.
    """

    def _pressed(self, **over):
        cap = dict(ACCOUNT)
        cap["now"] = dict(ACCOUNT["now"], saturated=True, in_use=12)
        cap.update(over)
        return metrics(**cap)

    def test_the_three_parts_in_order(self):
        head, adj, code = cli.verdict(self._pressed())
        self.assertIn("The account reports pressure this machine cannot explain", head)
        self.assertIn("priority pool was full at the sample", head)
        self.assertIn("(account)", head)
        self.assertTrue(adj[0].startswith("what is missing here:"), adj[0])
        self.assertTrue(adj[1].startswith("what to do:"), adj[1])
        self.assertIn("run luna-doctor on the machine that serves this traffic", adj[1])
        self.assertIn("collect its logs", adj[1])

    def test_an_empty_window_still_carries_the_service_answer(self):
        """No data here is exit 2, and stdout still answers for the account."""
        head, adj, code = cli.verdict(self._pressed())
        self.assertEqual(code, 2, "nothing measured here keeps the no-data code")
        self.assertIn("(account)", head)

    def test_a_thin_window_names_the_floor(self):
        m = self._pressed()
        m["requests"] = 3
        m["by_outcome"] = {"completed": 3}
        m["latency_s"] = {"interactive": {"n": 3, "p50": 1.0, "p90": 1.0, "max": 1.0},
                          "background": {"n": 0, "p50": None, "p90": None, "max": None}}
        head, adj, code = cli.verdict(m)
        self.assertIn("EVIDENCE" if "EVIDENCE" in adj[0] else str(cli.EVIDENCE_MIN_REQUESTS), adj[0])
        self.assertIn("fewer than", adj[0])
        self.assertEqual(code, 0, "measured, but not enough to explain")

    def test_no_store_says_so_and_names_the_missing_thing(self):
        with unittest.mock.patch.object(cli, "_any_default_store", return_value=False):
            state, why = cli.local_evidence(metrics())
        self.assertEqual(state, "none")
        self.assertIn("no pi, claude-code or codex log store", why)

    def test_an_empty_store_asks_for_the_logs_instead(self):
        with unittest.mock.patch.object(cli, "_any_default_store", return_value=True):
            state, why = cli.local_evidence(metrics())
        self.assertEqual(state, "none")
        self.assertIn("hold no request inside the window", why)

    def test_local_pressure_keeps_its_own_verdict(self):
        """When this machine did see pressure, the account does not take over."""
        m = self._pressed()
        m["by_outcome"] = {"admission_timeout": 2, "completed": 40}
        m["requests"] = 42
        head, adj, code = cli.verdict(m)
        self.assertNotIn("cannot explain", head)
        self.assertEqual(code, 1)

    def test_a_background_only_window_is_its_own_state(self):
        m = self._pressed()
        m["requests"] = 40
        m["peak_concurrency_priority"] = 0
        state, why = cli.local_evidence(m, "priority")
        self.assertEqual(state, "other-tier")
        self.assertIn("background tier", why)

    def test_fix_suggests_nothing_without_local_evidence(self):
        with unittest.mock.patch.object(cli, "_any_default_store", return_value=False):
            import subprocess
            import tempfile
            with tempfile.TemporaryDirectory() as home:
                cache = os.path.join(home, "capacity.json")
                import json
                with open(cache, "w") as fh:
                    json.dump({"fetched_at": __import__("time").time(),
                               "response": {"success": True,
                                            "generated_at": _now_iso(),
                                            "data": {"now": {"limit": 12, "in_use": 12,
                                                             "available": 0, "saturated": True,
                                                             "ledger": "up",
                                                             "sampled_at": _now_iso()},
                                                     "config": {"plan": "apex",
                                                                "priority_lanes": 12,
                                                                "background_lanes": 12,
                                                                "overflow_routing": "enabled"}}}},
                              fh)
                env = dict(os.environ, HOME=home, LUNA_DOCTOR_CAPACITY=cache,
                           LUNA_DOCTOR_STORE=os.path.join(home, "store"), NO_COLOR="1")
                empty = os.path.join(home, "empty")
                os.makedirs(empty, exist_ok=True)
                proc = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "luna-doctor"),
                                       "fix", "--since", "1h", "--offline",
                                       "--pi-root", empty, "--claude-root", empty],
                                      capture_output=True, text=True, timeout=180,
                                      cwd=ROOT, env=env)
        self.assertIn("no concurrency change to suggest", proc.stdout)
        self.assertIn("what is missing here", proc.stdout)
        self.assertIn("Run luna-doctor on the machine that serves this traffic", proc.stdout)
        # The config audit that follows is about declared settings, never about
        # the account's pressure, and it keeps its own findings.
        self.assertNotIn("concurrency allowance", proc.stdout.lower().replace(
            "priority concurrent", ""))


class LatencyPerCategory(unittest.TestCase):
    """Latency is measured here, because the account carries none.

    The API has no first-token time, no duration and no percentile for any
    request, so every number in this section is this machine's, and a cell
    below the sample floor is counted rather than printed.
    """

    def _reqs(self, n, model="glm-5.3", tier="interactive", duration=10.0,
              harness="pi", outcome="completed"):
        return [cli.Request(harness=harness, session="s", model=model,
                            tier=tier,
                            start=1_700_000_000.0 + i * 60,
                            end=1_700_000_000.0 + i * 60 + duration,
                            outcome=outcome)
                for i in range(n)]

    def test_cells_by_tier_model_harness_and_outcome(self):
        m = cli.analyse(self._reqs(6) + self._reqs(6, model="x-background", tier="background", duration=20.0),
                        window_hours=24)
        lat = m["latency_by"]
        self.assertEqual(lat["floor"], cli.BAND_MIN_TOP_SAMPLES)
        tier_model = {c["cell"]: c for c in lat["by_tier_model"]}
        self.assertIn("interactive / glm-5.3", tier_model)
        self.assertIn("background / x-background", tier_model)
        self.assertEqual(tier_model["interactive / glm-5.3"]["n"], 6)
        self.assertEqual(lat["by_tier_harness"][0]["n"], 6)
        self.assertTrue(lat["by_tier_outcome"])

    def test_a_cell_below_the_floor_is_counted_not_printed(self):
        m = cli.analyse(self._reqs(6) + self._reqs(2, model="rare"), window_hours=24)
        lat = m["latency_by"]
        cells = [c["cell"] for c in lat["by_tier_model"]]
        self.assertNotIn("interactive / rare", cells,
                         "two samples are a coincidence with a number attached")
        self.assertGreaterEqual(lat["suppressed"], 1)

    def test_the_report_shows_three_lines_and_no_matrix(self):
        reqs = self._reqs(6) + self._reqs(6, model="x-background", tier="background", duration=20.0)
        m = cli.analyse(reqs, window_hours=24)
        report = cli.build_report(m, "headline", [])
        section = [s for s in report.sections if s.title == "latency"]
        self.assertTrue(section, "the latency section belongs in the report")
        self.assertLessEqual(len(section[0].rows), 3,
                             "the report carries a bounded summary, not a matrix")
        self.assertTrue(all("(this machine)" in r.value for r in section[0].rows))

    def test_a_zero_request_window_has_no_latency_section(self):
        m = cli.analyse([], window_hours=1)
        report = cli.build_report(m, "headline", [])
        self.assertEqual([s for s in report.sections if s.title == "latency"], [])

    def test_blocked_durations_never_enter_a_completed_cell(self):
        m = cli.analyse(self._reqs(6) + self._reqs(6, outcome="admission_timeout",
                                                   duration=60.3),
                        window_hours=24)
        cells = {c["cell"]: c for c in m["latency_by"]["by_tier_model"]}
        self.assertEqual(cells["interactive / glm-5.3"]["n"], 6)
        self.assertLess(cells["interactive / glm-5.3"]["p90"], 60.0)


class TheChartHasTwoPanels(unittest.TestCase):
    """Two panels, one time axis: the account's blocking above, this
    machine's concurrency below, each with its own unit and scale."""

    def _scene_text(self, days):
        from datetime import datetime, timedelta, timezone
        from luna_doctor import sources
        now = datetime.now(timezone.utc)
        # Spread across days, because the account reports per UTC day: a window
        # inside one day has exactly one daily point to draw.
        step = 4 * 86400 / 8
        reqs = [cli.Request(harness="pi", session="s", model="glm-5.3", tier="interactive",
                            start=now.timestamp() - 4 * 86400 + i * step,
                            end=now.timestamp() - 4 * 86400 + i * step + 20,
                            outcome="completed")
                for i in range(8)]
        m = cli.analyse(reqs, window_hours=7)
        entries = [{"day": (now - timedelta(days=i + 1)).strftime("%Y-%m-%d"),
                    "accepted": 10, "deferred": 0, "denied": 0,
                    "denied_after_wait": daw} for i, daw in enumerate(days)]
        cap = sources.Capacity(admission=sources.CapacityAdmission(last_7d=entries))
        scene = cli.build_chart_scene(reqs, m, 24 * 7, capacity=None, account=cap)
        return " ".join(getattr(shape, "s", "") for shape in scene.shapes)

    def test_the_account_panel_names_what_it_draws(self):
        text = self._scene_text([0, 6, 0, 12])
        self.assertIn("blocked requests, per complete UTC day", text)
        self.assertIn("denied after waiting (account)", text)

    def test_a_quiet_week_says_so_instead_of_drawing_a_flat_line(self):
        text = self._scene_text([0, 0, 0])
        self.assertIn("no request denied after waiting", text)

    def test_no_admission_days_says_the_account_has_none(self):
        from luna_doctor import sources
        reqs = [cli.Request(harness="pi", session="s", model="glm-5.3", tier="interactive",
                            start=1_700_000_000.0, end=1_700_000_020.0,
                            outcome="completed")]
        m = cli.analyse(reqs, window_hours=7)
        scene = cli.build_chart_scene(reqs, m, 168, capacity=None,
                                      account=sources.Capacity())
        text = " ".join(getattr(shape, "s", "") for shape in scene.shapes)
        self.assertIn("no admission days", text)
