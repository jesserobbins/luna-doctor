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


from luna_doctor import cli, plan, report
from luna_doctor.render_pretty import render_pretty
from luna_doctor.style import Style


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
        # Exit 0 here means "nothing here measured pressure", not "healthy":
        # the account's own claim is the pressure, so the status moon says so
        # and the surface prints the legend for the scale it just used.
        self.assertEqual("moon_quarter", report.status_moon(code, head))
        style = Style(color=False, emoji=True, motion=False)
        out = render_pretty(report.Report(headline=head, sections=[],
                                          adjustments=list(adj), exit_code=code),
                            style)
        self.assertTrue(out.startswith("\U0001F317"), out.splitlines()[0])
        self.assertEqual(1, out.count(style.moon_legend()))

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
        self.assertLessEqual(len(section[0].rows), 4,
                             "the report carries a bounded summary, not a matrix")
        self.assertTrue(any("luna-doctor latency" in r.value for r in section[0].rows),
                        "the summary points at the table it cannot fit")
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


class TheChartIsDesignedForDiagnosis(unittest.TestCase):
    """The chart follows observability practice, not dashboard habit.

    A percentile line cannot tell "everything got 3x slower" from "1% started
    taking 20 minutes"; a duration heatmap can. Refusals never enter the
    duration distribution, and the account's day-granular counters read as a
    sentence instead of asserting a step function on an hourly axis. Sources:
    the private brief 2026-11-03-observability-viz-brief.md.
    """

    def _scene(self, reqs, hours=24.0, capacity=None):
        m = cli.analyse(reqs, hours)
        return cli.build_chart_scene(reqs, m, hours, capacity=capacity)

    def _text(self, scene):
        return " ".join(getattr(shape, "s", "") for shape in scene.shapes)

    def _req(self, start, duration, outcome="completed", tier="interactive"):
        return cli.Request(harness="pi", session="s", model="glm-5.3", tier=tier,
                           start=start, end=start + duration, outcome=outcome)

    def test_the_heatmap_states_its_scale_and_axes(self):
        now = 1_700_000_000.0
        scene = self._scene([self._req(now + i, 10.0) for i in range(20)])
        text = self._text(scene)
        self.assertIn("request duration, log scale", text)
        self.assertIn("completion(s) per cell", text)
        self.assertIn("completions per column", text)

    def test_octave_labels_are_drawn_on_edges(self):
        now = 1_700_000_000.0
        scene = self._scene([self._req(now + i, 10.0) for i in range(20)])
        text = self._text(scene)
        for label in ("1s", "4s", "16s", "1m"):
            self.assertIn(label, text, f"the duration axis is missing its {label} edge")

    def test_a_refusal_never_enters_the_duration_distribution(self):
        """AWS: do not pollute latency with failed-request latency."""
        now = 1_700_000_000.0
        served = [self._req(now + i, 10.0) for i in range(10)]
        refused = [self._req(now + i, 120.0, outcome="admission_timeout") for i in range(10)]
        scene = self._scene(served + refused)
        text = self._text(scene)
        self.assertIn("10 completed request(s)", text,
                      "the refusals were counted as completed traffic")

    def test_the_admission_fates_are_counted_per_column(self):
        now = 1_700_000_000.0
        reqs = ([self._req(now + i * 60, 5.0) for i in range(12)]
                + [self._req(now + i * 60, 120.0, outcome="admission_timeout") for i in range(6)])
        text = self._text(self._scene(reqs))
        for kind in ("served", "demoted", "refused: admission", "refused: limit"):
            self.assertIn(kind, text)

    def test_the_picture_says_the_rule_and_one_reading_in_two_lines(self):
        """The footer carries the rule's provenance and one reading, no more.

        The account's day counters, the measured deadlines and the heatmap's
        reading guide left this band: they are sentences, and `luna-doctor
        analyze` reports the deadlines and the counters, while `explain` tells
        the story of one request."""
        now = 1_700_000_000.0
        text = self._text(self._scene([self._req(now + i * 60, 5.0) for i in range(10)]))
        self.assertIn("rule: not established", text)
        self.assertIn("reading: no request was rejected at admission", text)
        self.assertNotIn("complete UTC days only", text)
        self.assertNotIn("measured: observed admission deadline", text)
        self.assertNotIn("reading the heatmap", text)

    def test_no_completions_says_so_rather_than_drawing_an_empty_field(self):
        now = 1_700_000_000.0
        text = self._text(self._scene([self._req(now, 120.0, outcome="admission_timeout")]))
        self.assertIn("fewer than 5 requests per column", text)


class OneLayoutAtEveryWindow(unittest.TestCase):
    """Jesse, 2026-11-11: the per-request timeline was not a useful thing.

    Up to two hours the picture spent its top on one bar per request -- a
    burndown of the window, one row each -- and drew a different page from the
    one a 24h window got. The rows are gone: every window now draws the fate
    panel, the latency field with its completion bars, and the concurrency
    stack, in that order, so a fifteen-minute picture and a month-long one are
    the same page read at different scales.
    """

    NOW = 1_700_000_000.0
    # The two colours the retired rows were drawn in: priority navy and
    # background violet, one bar per request. The fate panel uses neither (its
    # served and demoted fills are the pale steps of the same hues), so their
    # absence above the field is the rows' absence, and their presence below
    # it is the stack.
    BAND_INK = ("#173f8f", "#5b21b6")

    def _reqs(self, count, hours):
        """`count` completed requests, evenly spread over the window."""
        step = hours * 3600.0 / count
        return [cli.Request(harness="pi", session="s", model="glm-5.3",
                            start=self.NOW + i * step,
                            end=self.NOW + i * step + min(90.0, step),
                            outcome="completed")
                for i in range(count)]

    def _scene(self, hours, count=60):
        reqs = self._reqs(count, hours)
        return cli.build_chart_scene(reqs, cli.analyse(reqs, hours), hours)

    def _text(self, scene):
        return " ".join(getattr(shape, "s", "") for shape in scene.shapes)

    def _field_title_y(self, scene):
        """The y of the latency field's own heading: the page's middle seam."""
        titles = [s for s in scene.shapes
                  if isinstance(s, cli.Text) and s.s.startswith("request duration")]
        self.assertEqual(1, len(titles), "the latency field states its scale once")
        return titles[0].y

    def _panels(self, scene):
        """The panels the page draws, named by the line each one states."""
        text = self._text(scene)
        return {
            "fate": "refused: admission" in text,
            "latency field": "request duration, log scale" in text,
            "completion bars": "completions per column" in text,
        }

    def _panel_baseline(self, scene):
        """The y of the completion bars' baseline: the page's upper seam.

        The page draws two full-width axis hairlines in the axis grey: one
        under the completion bars, one under the stack. Nothing else in that
        colour spans the plot, because the grey peak envelope rides inside the
        stack and never reaches either seam. The upper of the two is the seam
        between the marks above the stack and the stack itself.
        """
        seams = [s for s in scene.shapes
                 if isinstance(s, cli.Line) and s.y1 == s.y2
                 and s.stroke == "#9ca3af" and (s.x2 - s.x1) > 500]
        self.assertTrue(seams, "the page draws no panel baseline")
        return min(seam.y1 for seam in seams)

    def _stack_rects(self, scene):
        """The band rects the concurrency stack draws, below that seam.

        A fill alone cannot prove the stack drew: the latency field's busiest
        cell is `#173f8f`, the same navy as the top band, because the field is
        one hue linear in count. A fill-only guard therefore passes on the
        field's own ink. The stack owns everything below the completion bars'
        baseline.
        """
        baseline = self._panel_baseline(scene)
        return [s for s in scene.shapes
                if isinstance(s, cli.Rect) and s.fill in self.BAND_INK
                and s.y > baseline]

    def test_the_per_request_timeline_is_retired(self):
        """Gone, not switched off: no rows, and no function that drew them."""
        self.assertFalse(hasattr(cli, "_timeline_panel"),
                         "the per-request timeline is retired, not hidden")
        for hours in (0.25, 1.0, 24.0):
            text = self._text(self._scene(hours))
            self.assertNotIn("one bar per request", text,
                             "a %gh window still draws request-by-request rows" % hours)

    def test_a_short_window_draws_the_same_three_panels_as_a_long_one(self):
        self.assertEqual(self._panels(self._scene(1.0)),
                         self._panels(self._scene(24.0)),
                         "the 1h and 24h pages must be the same page")
        for hours in (0.25, 1.0, 24.0):
            self.assertEqual({"fate": True, "latency field": True,
                              "completion bars": True},
                             self._panels(self._scene(hours)),
                             "the %gh page is missing a panel" % hours)

    def test_no_request_row_is_drawn_outside_the_main_plot(self):
        """The only per-request mark left is the flare tick, in the stack."""
        for hours in (0.25, 1.0, 24.0):
            scene = self._scene(hours)
            seam = self._field_title_y(scene)
            rows = [s for s in scene.shapes
                    if isinstance(s, cli.Rect) and s.fill in self.BAND_INK
                    and s.y < seam]
            self.assertEqual([], rows,
                             "a band-coloured bar above the field at %gh is a "
                             "per-request row" % hours)
            self.assertTrue(self._stack_rects(scene),
                            "the stack draws below the field at %gh" % hours)


class TheRuleMarkCarriesItsAuthority(unittest.TestCase):
    """Authority is a property of the mark, not only of the label.

    The design decision (2026-11-10): the capacity rule on every chart is drawn
    according to its provenance - the account's own number is a thick, solid
    line, a --plan declaration visibly thinner and dashed - so a reader can
    tell which one they are looking at before reading a word.
    """

    NOW = 1_700_000_000.0

    def _scene(self, capacity):
        reqs = [cli.Request(harness="pi", session="s", model="glm-5.3",
                            start=self.NOW + i * 60, end=self.NOW + i * 60 + 600,
                            outcome="completed") for i in range(4)]
        return cli.build_chart_scene(reqs, cli.analyse(reqs, 24.0), 24.0,
                                     capacity=capacity)

    def _rule(self, scene):
        """The one full-width horizontal line that is not an axis or a grid."""
        rules = [s for s in scene.shapes
                 if isinstance(s, cli.Line) and s.y1 == s.y2
                 and (s.x2 - s.x1) > 500
                 and s.stroke not in ("#9ca3af", "#e5e7eb")]
        self.assertEqual(1, len(rules), "one priority rule, against the one stack")
        return rules[0]

    def _account(self):
        return plan.Capacity(priority=12, source="account", declared="",
                             account_present=True, account_priority=12,
                             account_plan="apex")

    def test_an_account_rule_is_thick_and_solid(self):
        rule = self._rule(self._scene(self._account()))
        self.assertEqual((), rule.dash, "an authoritative rule is never dashed")
        self.assertGreaterEqual(rule.stroke_width, 3.0)
        self.assertEqual("#111827", rule.stroke, "the rule wears the strongest ink")

    def test_a_declared_rule_is_thin_and_dashed(self):
        rule = self._rule(self._scene(plan.Capacity(priority=4, source="plan",
                                                    declared="max")))
        self.assertTrue(rule.dash, "a declared rule is dashed")
        self.assertLessEqual(rule.stroke_width, 1.5)
        self.assertEqual("#374151", rule.stroke)

    def test_the_svg_carries_the_authority_too(self):
        declared = cli.svg_from(self._scene(
            plan.Capacity(priority=4, source="plan", declared="max")))
        self.assertIn('stroke="#374151" stroke-dasharray="5 4"', declared)
        account = cli.svg_from(self._scene(self._account()))
        self.assertIn('stroke="#111827" stroke-width="3.5"', account)
        self.assertNotIn("stroke-dasharray", account)


class TheChartSaysWhatItIsDoing(unittest.TestCase):
    """Drawing the chart takes seconds, and it used to say nothing at all.

    The owner's words: "chart says nothing". Two lines, both through the one
    `notice` helper every slow command uses: one before the render starts, one
    after it that names the file and what it took. Neither is written under
    --json, where the envelope is the whole answer.
    """

    class _Terminal:
        """A stream that says it is a terminal and keeps what it was given."""

        encoding = "utf-8"

        def __init__(self):
            self.text = ""

        def isatty(self):
            return True

        def write(self, text):
            self.text += text
            return len(text)

        def flush(self):
            pass

    def _chart(self, tmp, *extra):
        """Run `chart` in-process with the picture faked and the streams kept."""
        stdout, stderr = self._Terminal(), self._Terminal()
        at_render = {}

        def fake_scene(*args, **kwargs):
            at_render["stderr"] = stderr.text
        out = os.path.join(tmp, "chart.svg")
        env = {k: v for k, v in os.environ.items() if k != "LUNA_DOCTOR_QUIET"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(cli, "collect", return_value=[]), \
                mock.patch.object(cli, "analyse",
                                  return_value=cli.analyse([], 1.0)), \
                mock.patch.object(cli.sources, "fetch_capacity", return_value=None), \
                mock.patch.object(cli, "build_chart_scene", side_effect=fake_scene), \
                mock.patch.object(cli, "svg_from", return_value="<svg/>"), \
                mock.patch.object(sys, "stdout", stdout), \
                mock.patch.object(sys, "stderr", stderr):
            code = cli.main(["chart", "--since", "1h", "--offline", "--no-image",
                             "--out", out, *extra])
        self.assertEqual(0, code)
        return at_render.get("stderr", ""), stderr.text, out

    def test_a_notice_precedes_the_render_and_one_names_the_file_after(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            before, stderr, out = self._chart(tmp)
        self.assertIn("drawing the chart", before,
                      "nothing was said before the picture was drawn")
        self.assertNotIn("chart written", before)
        self.assertIn("chart written: %s" % os.path.abspath(out), stderr)
        self.assertRegex(stderr, r"chart written: .*\(\d+ KB, \d+\.\d s\)")

    def test_json_gets_no_activity_lines(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            _before, stderr, _out = self._chart(tmp, "--json")
        self.assertNotIn("drawing the chart", stderr)
        self.assertNotIn("chart written", stderr)


class TheChartFooterStaysTwoLines(unittest.TestCase):
    """Jesse, 2026-11-10: drop the wall of text, and date the picture up top.

    The marks carry the story. The band under the picture carries the rule's
    provenance and the one reading that matters, and nothing else: a third
    line is already more prose than a picture can support. The generation
    timestamp moves to the title block, where a reader looks first.
    """

    NOW = 1_700_000_000.0

    def _req(self, i):
        return cli.Request(harness="pi", session="s", model="glm-5.3",
                           start=self.NOW + i * 60, end=self.NOW + i * 60 + 5,
                           outcome="completed")

    def _scene(self, reqs, capacity=None):
        return cli.build_chart_scene(reqs, cli.analyse(reqs, 24.0), 24.0,
                                     capacity=capacity)

    def _footer_rows(self, scene):
        """Every text run in the bottom tenth of the canvas: the footer band."""
        return [s for s in scene.shapes
                if isinstance(s, cli.Text) and s.y >= scene.height * 0.9]

    def test_the_footer_is_at_most_two_lines(self):
        reqs = [self._req(i) for i in range(20)]
        for capacity in (None, plan.Capacity(priority=4, source="plan",
                                             declared="max")):
            rows = self._footer_rows(self._scene(reqs, capacity))
            self.assertLessEqual(len(rows), 2, [r.s for r in rows])
            self.assertTrue(rows[0].s.startswith("rule: "), rows[0].s)
            self.assertTrue(rows[-1].s.startswith("reading: "), rows[-1].s)

    def test_the_prose_that_left_the_picture_did_not_come_back(self):
        reqs = [self._req(i) for i in range(20)]
        text = " ".join(s.s for s in self._footer_rows(self._scene(reqs)))
        for gone in ("bands:", "measured:", "complete UTC days only",
                     "reading the heatmap", "too few completed requests",
                     "luna-doctor v"):
            self.assertNotIn(gone, text)

    def test_the_header_carries_the_time_the_picture_was_drawn(self):
        scene = self._scene([])
        header = [s for s in scene.shapes
                  if isinstance(s, cli.Text) and s.s.startswith("generated ")]
        self.assertEqual(1, len(header), "the picture dates itself once")
        self.assertLessEqual(header[0].y, 40, "the date belongs in the title block")
        self.assertRegex(header[0].s, r"^generated \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC$")


class LatencyTablePlaceholder(unittest.TestCase):
    pass


class Ergonomics(unittest.TestCase):
    """Fewer keystrokes, and a sign of life while the account answers."""

    def test_a_lone_duration_is_the_command(self):
        self.assertEqual(cli._bare_duration(["15m"]), ["analyze", "--since", "15m"])
        self.assertEqual(cli._bare_duration(["1h", "--offline"]),
                         ["analyze", "--since", "1h", "--offline"])
        self.assertEqual(cli._bare_duration([]), [])
        self.assertEqual(cli._bare_duration(["chart"]), ["chart"])

    def test_the_notice_stays_off_a_pipe(self):
        import contextlib
        import io as _io
        buf = _io.StringIO()
        with contextlib.redirect_stderr(buf):
            cli.notice("reading the logs")
        self.assertEqual(buf.getvalue(), "", "a piped run must not print a notice")

    def test_the_sparkline_shows_the_shape_in_one_line(self):
        now = 1_700_000_000.0
        reqs = []
        for i in range(60):
            dur = 10.0 if i < 30 else 40.0
            reqs.append(cli.Request(harness="pi", session="s", model="glm-5.3",
                                    tier="interactive", start=now + i * 60,
                                    end=now + i * 60 + dur, outcome="completed"))
        m = cli.analyse(reqs, 2)
        spark = cli.duration_sparkline(m, reqs)
        self.assertTrue(spark, "a window with enough completions earns a sparkline")
        self.assertIn("p90", spark)
        self.assertNotIn("None", spark)

    def test_no_sparkline_without_enough_evidence(self):
        now = 1_700_000_000.0
        reqs = [cli.Request(harness="pi", session="s", model="glm-5.3",
                            tier="interactive", start=now, end=now + 5,
                            outcome="completed")]
        self.assertEqual(cli.duration_sparkline(cli.analyse(reqs, 1), reqs), "")

    def test_the_bare_run_acknowledges_before_it_can_block(self):
        """The first line says what the run is doing, before any wait."""
        import subprocess
        import tempfile
        home = tempfile.mkdtemp(prefix="luna-doctor-ack-")
        env = dict(os.environ, HOME=home, NO_COLOR="1",
                   LUNA_DOCTOR_STORE=os.path.join(home, "store"),
                   LUNA_DOCTOR_CAPACITY=os.path.join(home, "cap.json"))
        # a pty is what notice() requires; script(1) gives us one
        proc = subprocess.run(["script", "-q", "/dev/null",
                               os.path.join(ROOT, "bin", "luna-doctor"), "--offline"],
                              capture_output=True, text=True, timeout=180,
                              cwd=home, env=env)
        self.assertIn("luna-doctor: starting:", proc.stdout)
        first_notice = proc.stdout.index("luna-doctor: starting:")
        self.assertLess(first_notice, proc.stdout.index("account:"),
                        "the acknowledgement comes before the line that may wait")

class TheArchiveIsRead(unittest.TestCase):
    """agentsview supplies closed hours from every agent it archives.

    It is history, not the live view: it ends at the last closed UTC hour, which
    is why the current hour stays this tool's own reading of the logs.
    """

    def test_a_missing_binary_is_unavailable_not_zero(self):
        from unittest import mock
        from luna_doctor import sources
        with mock.patch.object(sources.shutil, "which", return_value=None):
            out = sources.agentsview_window(0, 3600)
        self.assertFalse(out["present"])
        self.assertFalse(out["readable"])
        self.assertEqual(out["reason"], "not installed")
        self.assertIsNone(out["peak_agents"])
        self.assertIsNone(out["cost_usd"])

    def test_it_reads_closed_hours_and_peaks(self):
        from unittest import mock
        from luna_doctor import sources
        hours = {
            "2026-11-03-21": {"activity": {"buckets": []}, "usage": {"by_model": []},
                              "joint": {"cells": []}},
            "2026-11-03-22": {"activity": {"buckets": [
                {"start": "2026-11-03T22:00:00Z", "max_agents": 2,
                 "max_interactive_agents": 2, "max_subagent_agents": 0,
                 "output_tokens": 100, "cost": {"microdollars": 500000}}]},
                "usage": {"by_model": [{"key": "glm-5.3", "output_tokens": 100,
                                        "input_tokens": 10,
                                        "cost": {"microdollars": 500000}}]},
                "joint": {"cells": [{"pricing": {"unpriced_rows": 3}}]}},
            "2026-11-03-23": {"activity": {"buckets": [
                {"start": "2026-11-03T23:00:00Z", "max_agents": 7,
                 "max_interactive_agents": 5, "max_subagent_agents": 2,
                 "output_tokens": 200, "cost": {"microdollars": 1000000}}]},
                "usage": {"by_model": [{"key": "glm-5.3", "output_tokens": 50,
                                        "input_tokens": 5,
                                        "cost": {"microdollars": 250000}}]},
                "joint": {"cells": []}},
        }

        def fake_run(args, timeout):
            if args[:2] == ["export", "range"]:
                return {"closed_through": "2026-11-04T00:00:00Z",
                        "earliest_date": "2025-11-20"}
            stamp = args[2]
            return hours.get(stamp)

        with mock.patch.object(sources.shutil, "which", return_value="/bin/agentsview"), \
             mock.patch.object(sources, "_agentsview_run", side_effect=fake_run):
            out = sources.agentsview_window(
                __import__("datetime").datetime(2026, 11, 3, 21, 0,
                                                tzinfo=__import__("datetime").timezone.utc).timestamp(),
                __import__("datetime").datetime(2026, 11, 4, 0, 0,
                                                tzinfo=__import__("datetime").timezone.utc).timestamp())
        self.assertTrue(out["readable"])
        self.assertEqual(out["peak_agents"], 7)
        self.assertEqual(out["cost_usd"], 1.5)
        self.assertEqual(out["unpriced_rows"], 3)
        self.assertEqual(len(out["hours"]), 3)
        self.assertEqual(out["by_model"][0]["model"], "glm-5.3")
        self.assertEqual(out["by_model"][0]["output_tokens"], 150,
                         "the same model across hours is one row, summed")

    def test_no_closed_hour_inside_the_window_says_so(self):
        from unittest import mock
        from luna_doctor import sources
        with mock.patch.object(sources.shutil, "which", return_value="/bin/agentsview"), \
             mock.patch.object(sources, "_agentsview_run",
                               return_value={"closed_through": "2026-11-04T00:00:00Z"}):
            out = sources.agentsview_window(0, 600)
        self.assertTrue(out["readable"])
        self.assertEqual(out["reason"], "no closed hour inside the window")
        self.assertEqual(out["buckets"], [])


class TheTrendSurvivesOddWindows(unittest.TestCase):
    """A request that started before the window's first completion.

    A long request can begin well before the earliest completion in the window,
    so its span begins left of column zero. Clamping only the right end put a
    negative index into the in-flight list and the report died with an
    IndexError. Jesse hit this on a real run.
    """

    def test_a_request_starting_before_the_window_does_not_crash(self):
        reqs = [cli.Request(harness="pi", session="s", model="glm-5.3",
                            tier="interactive", start=1_700_000_000.0,
                            end=1_700_009_000.0, outcome="completed")]
        for i in range(12):
            reqs.append(cli.Request(harness="pi", session="s", model="glm-5.3",
                                    tier="interactive",
                                    start=1_700_008_000.0 + i * 5,
                                    end=1_700_008_100.0 + i * 5,
                                    outcome="completed"))
        m = cli.analyse(reqs, 1)
        text, worst = cli.trend_summary(m, reqs)      # must not raise
        self.assertTrue(text)
        report = cli.build_report(m, "headline", [], reqs=reqs)
        self.assertTrue([s for s in report.sections if s.title == "trend"])

    def test_a_broken_trend_degrades_and_never_kills_the_report(self):
        from unittest import mock
        reqs = [cli.Request(harness="pi", session="s", model="glm-5.3",
                            tier="interactive", start=1_700_000_000.0,
                            end=1_700_000_060.0, outcome="completed")]
        m = cli.analyse(reqs, 1)
        with mock.patch.object(cli, "trend_summary", side_effect=RuntimeError("boom")):
            report = cli.build_report(m, "headline", [], reqs=reqs)
        trend = [s for s in report.sections if s.title == "trend"][0]
        self.assertIn("the trend could not be read in this window", trend.rows[0].value)
        self.assertTrue([s for s in report.sections if s.title == "traffic"],
                        "the rest of the report must still be there")
