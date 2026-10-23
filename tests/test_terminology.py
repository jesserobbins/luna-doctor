"""The retired capacity vocabulary must never reach a user.

"Fan-out" was the owner's personal shorthand for capacity, never a LunaRoute
term, and "lane" is an internal API field name, not product language. This
file is the one place in the suite where those words may appear: inside
FORBIDDEN below, as the strings the scan hunts for.

The scan is over rendered surfaces, not source text, because what a user
sees is the contract: the plain and pretty renderers, the WhatsApp share
message and its card, the fix block and its --json envelope, the chart SVG,
the probe and compare reports, and the README and skill a reader is pointed
at. The in-process surfaces are driven with synthetic data that forces every
advice branch to render; the fix and chart surfaces run the real commands the
way a user would.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "bin", "luna-doctor")

# The retired terms. Lowercase: the scan is case-insensitive so "Fan-out"
# at the start of a rendered line cannot slip through.
FORBIDDEN = ("fan-out", "fanout", "fan out", "lane")

_STORE = tempfile.mkdtemp(prefix="luna-doctor-terms-")


def run(*args, timeout=180):
    env = dict(os.environ)
    env["LUNA_DOCTOR_STORE"] = _STORE
    proc = subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True,
                          timeout=timeout, cwd=ROOT, env=env)
    return proc.returncode, proc.stdout, proc.stderr


class NoRetiredVocabulary(unittest.TestCase):
    def _assert_clean(self, name, text):
        low = text.lower()
        for word in FORBIDDEN:
            self.assertNotIn(word, low, f"{name} contains the retired term {word!r}")

    # ---- in-process surfaces, driven to render every advice branch ----

    def _pressured_metrics(self, m):
        """Patch analyse() output so verdict() renders every branch it has."""
        m["pressure_windows"] = 2
        m["longest_pressure_window"] = {"seconds": 42.0, "max_concurrent": 17}
        m["timeout_storms"] = [{"cohort": 10, "outcome": "client_idle_timeout",
                                "duration_s": 300.0, "blocked_s": 3000.0}]
        m["saturation_bands"] = {"top_inflation_flagged": True, "top_inflation": 9.5,
                                 "mid_p90_baseline_s": 12.0,
                                 "bands": [{"band": "10+", "n": 7, "p50": 30.0, "p90": 110.0}]}
        m["unknown_errors"] = [{"signature": "weird gateway error xyz", "count": 3,
                                "first": "2026-10-23", "last": "2026-10-23"}]
        m["starved_peer_evidence"] = 1
        m["latency_s"]["interactive"]["p50"] = 75.0
        m["swamp"] = {"present": True, "configuredConcurrency": 20, "knobs": {}}
        return m

    def _rendered(self):
        """Every string the analyze path can hand a user, from one metrics dict."""
        from luna_doctor import cli
        from luna_doctor.render_pretty import render_pretty
        from luna_doctor.render_share import render_share_text
        from luna_doctor.report import render_plain
        from luna_doctor.style import Style

        base = 1_700_000_000.0
        outcomes = ("admission_timeout", "client_idle_timeout", "org_concurrency_limit",
                    "message_limit", "rate_limited", "context_length_exceeded",
                    "payload_too_large", "model_not_found", "harness_deadline",
                    "request_timeout", "aborted", "upstream_unavailable")
        reqs = [cli.Request(harness="pi", session="s", model="glm-5.3",
                            start=base + i, end=base + i + 300.0, outcome=outcome)
                for i, outcome in enumerate(outcomes)]
        reqs.append(cli.Request(harness="pi", session="s", model="glm-5.3-background",
                                tier="background", start=base + 100, end=base + 400.0,
                                outcome="completed"))
        metrics = self._pressured_metrics(cli.analyse(reqs, 1.0))
        head, adj, _ = cli.verdict(metrics)
        rep = cli.build_report(metrics, head, adj)
        surfaces = {
            "headline": head,
            "advice": " ".join(adj),
            "plain report": render_plain(rep),
            "pretty report": render_pretty(rep, Style(False, False, False)),
            "share text": render_share_text(rep, metrics),
        }

        # The WhatsApp card scene: the second share surface.
        from luna_doctor.scene import build_card_scene, svg_from
        surfaces["share card svg"] = svg_from(build_card_scene(metrics, head))

        # probe and compare: pure report builders, no network, all branches.
        probe_run = {"direct": [{"reason": "overflow"}, {"reason": "no_entitlement"}],
                     "demoted": 2, "tiers": {"interactive": 6, "background": 2},
                     "streamed": 6, "silent": 1, "admission_deaths": 1, "errors": 0,
                     "cut": 0, "wait_headers": 25.2, "ttfb": 41.0, "keepalives": 40,
                     "budget_s": 600.0, "n": 8}
        surfaces["probe report"] = render_plain(
            cli.build_probe_report(probe_run, 8, "glm-5.3"))
        rows = [{"model": "glm-5.3", "n": 8, "tiers": {"interactive": 8}, "streamed": 8,
                 "silent": 0, "admission_deaths": 0, "cut": 0, "ttfb": 41.0},
                {"model": "glm-5.3-background", "n": 8, "tiers": {"background": 8},
                 "streamed": 7, "silent": 0, "admission_deaths": 0, "cut": 1,
                 "ttfb": 590.0}]
        surfaces["compare report"] = render_plain(cli.build_compare_report(rows))
        return surfaces

    def test_rendered_reports_are_clean(self):
        for name, text in self._rendered().items():
            self._assert_clean(name, text)

    def test_rendered_reports_reach_every_branch(self):
        """A scan that renders nothing proves nothing: the branches must fire."""
        text = self._rendered()
        self.assertIn("park or overflow into background scheduling", text["advice"])
        self.assertIn("priority concurrency", text["advice"])
        self.assertIn("swamp is configured for 20 concurrent requests",
                      text["advice"])
        self.assertIn("configured concurrency", text["plain report"])

    # ---- command surfaces, run the way a user runs them ----

    def test_fix_text_and_json_are_clean(self):
        for extra in ([], ["--json"]):
            code, out, err = run("fix", "--since", "24h", *extra)
            self.assertIn(code, (0, 1, 2), f"unexpected exit {code}: {err}")
            if extra:
                json.loads(out)            # still a well-formed envelope
            self._assert_clean(f"fix {' '.join(extra)}", out)

    def test_chart_svg_and_console_are_clean(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "chart.svg")
            code, stdout, err = run("chart", "--since", "24h", "--out", out)
            self.assertEqual(code, 0, f"chart failed: {err}")
            with open(out, encoding="utf-8") as fh:
                svg = fh.read()
        # The redesign draws no advisory rule: the old "advised" number was
        # headroom under a cap that turned out to be one plan's default, so
        # advice that depends on the plan is re-derived elsewhere, not drawn.
        # Without --plan the footer says why no rule is drawn, in the
        # capacity vocabulary (the declared-rule paths live in ChartPlan).
        self.assertNotIn("advised concurrency", svg,
                         "a plan-dependent advisory rule must not be drawn")
        self.assertIn("specific to the account", svg,
                      "the no-plan footer must say why no rule is drawn")
        self._assert_clean("chart svg", svg)
        self._assert_clean("chart console", stdout)

    def test_readme_and_skill_are_clean(self):
        for rel in ("README.md", os.path.join("skills", "luna-doctor", "SKILL.md")):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                self._assert_clean(rel, fh.read())


if __name__ == "__main__":
    unittest.main()
