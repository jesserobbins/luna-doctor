"""Smoke tests for luna-doctor.

Stdlib only, no fixtures to maintain. Run from the repo root:

    python3 -m unittest discover -s tests -v

The suite never calls `probe`/`compare`, which burn concurrent sessions on the
gateway, and every CLI run
points `LUNA_DOCTOR_STORE` at a throwaway directory: nothing here writes to your
real store or to a log store.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import datetime
import unittest
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "bin", "luna-doctor")
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")  # semver, prerelease allowed (0.1.1-alpha.1)


# Every CLI invocation in this suite gets its own store directory: the real
# ~/.luna-doctor/store is the user's repeat history, not a test artifact.
_TEST_STORE = tempfile.mkdtemp(prefix="luna-doctor-tests-")


def run(*args, timeout=180, store=None):
    env = dict(os.environ)
    env["LUNA_DOCTOR_STORE"] = store or _TEST_STORE
    proc = subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True,
                          timeout=timeout, cwd=ROOT, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def _version_from_output(out: str) -> str:
    """Parse 'luna-doctor 0.1.1-alpha.1 (alpha — …)' -> '0.1.1-alpha.1'."""
    m = re.search(r"luna-doctor\s+(\S+)", out)
    return m.group(1) if m else out.strip().split()[1] if len(out.strip().split()) > 1 else out.strip()

class VersionContract(unittest.TestCase):
    def test_version_is_semver(self):
        code, out, _ = run("--version")
        self.assertEqual(code, 0)
        version = _version_from_output(out)
        self.assertRegex(version, SEMVER)

    def test_version_matches_version_file(self):
        _, out, _ = run("--version")
        script_version = _version_from_output(out)
        with open(os.path.join(ROOT, "VERSION")) as fh:
            file_version = fh.read().strip()
        self.assertEqual(script_version, file_version,
                         "VERSION and luna_doctor/cli.py disagree; run scripts/release.sh")

    def test_version_matches_newest_git_tag(self):
        try:
            tags = subprocess.run(["git", "tag", "--list", "v*"], capture_output=True,
                                  text=True, cwd=ROOT, timeout=10)
        except Exception:
            self.skipTest("git unavailable")
        if tags.returncode != 0 or not tags.stdout.strip():
            self.skipTest("no release tags yet")
        def _semver_key(t):
            core = t.lstrip("v").split("-")[0]
            return [int(p) for p in core.split(".")]
        newest = sorted(tags.stdout.split(), key=_semver_key)[-1]
        _, out, _ = run("--version")
        self.assertEqual(_version_from_output(out), newest.lstrip("v"),
                         "the newest tag and the code disagree; tag the released version")

    def test_changelog_has_the_current_version(self):
        _, out, _ = run("--version")
        version = _version_from_output(out)
        with open(os.path.join(ROOT, "CHANGELOG.md")) as fh:
            text = fh.read()
        self.assertIn(f"## [{version}]", text)


class JsonEnvelope(unittest.TestCase):
    def _envelope(self, *args):
        code, out, err = run(*args)
        self.assertIn(code, (0, 1, 2), f"unexpected exit {code}: {err}")
        doc = json.loads(out)          # must be pure JSON on stdout
        self.assertEqual(doc["schema"], 4)
        self.assertRegex(doc["version"], SEMVER)
        self.assertIn("command", doc)
        return code, doc

    def test_discover_envelope(self):
        _, doc = self._envelope("discover", "--json")
        self.assertEqual(doc["command"], "discover")
        self.assertIn("stores", doc)

    def test_analyze_envelope(self):
        _, doc = self._envelope("analyze", "--since", "24h", "--json")
        self.assertEqual(doc["command"], "analyze")
        for key in ("headline", "adjustments", "metrics", "contract"):
            self.assertIn(key, doc)
        self.assertEqual(doc["contract"]["interactive_burst_cap"], 10)

    def test_fix_envelope(self):
        _, doc = self._envelope("fix", "--since", "24h", "--json")
        self.assertEqual(doc["command"], "fix")
        self.assertIn("agents", doc)
        self.assertIn("findings", doc)


class ExitCodes(unittest.TestCase):
    def test_bad_since_is_usage_error_not_pressure(self):
        code, out, err = run("analyze", "--since", "abc")
        self.assertEqual(code, 2)
        self.assertIn("bad --since", err)
        self.assertNotIn("Traceback", err)

    def test_unknown_agent_is_usage_error(self):
        code, _, err = run("fix", "--agent", "claudecode")
        self.assertEqual(code, 2)
        self.assertIn("unknown agent", err)

    def test_probe_rejects_zero_width(self):
        code, _, err = run("probe", "--n", "0")
        self.assertEqual(code, 2)
        self.assertIn("--n must be at least 1", err)

    def test_no_data_for_a_harness_we_do_not_read(self):
        code, _, _ = run("analyze", "--harness", "codex", "--since", "1m")
        self.assertEqual(code, 2)


class ProbeGate(unittest.TestCase):
    def test_probe_without_the_flag_refuses_and_names_the_width(self):
        code, out, err = run("probe", "--n", "8")
        self.assertEqual(code, 2)
        self.assertIn("--enable-probe", err)
        self.assertIn("8 concurrent sessions", err)
        self.assertEqual(out, "")

    def test_compare_without_the_flag_refuses(self):
        code, _, err = run("compare", "--n", "4")
        self.assertEqual(code, 2)
        self.assertIn("--enable-probe", err)

    def test_message_says_concurrency_not_credits(self):
        code, _, err = run("probe", "--n", "8")
        for forbidden in ("credit", "token", "cost", "spend"):
            self.assertNotIn(forbidden, err.lower())


class Chart(unittest.TestCase):
    def _chart(self, *extra):
        d = tempfile.mkdtemp()
        out = os.path.join(d, "chart.svg")
        code, stdout, err = run("chart", "--since", "24h", "--out", out, *extra)
        self.assertEqual(code, 0, f"chart failed: {err}")
        self.assertTrue(os.path.exists(out), f"no file written: {stdout}")
        with open(out) as fh:
            return fh.read()

    def test_svg_is_self_contained(self):
        svg = self._chart()
        self.assertTrue(svg.startswith("<svg"), svg[:40])
        ET.fromstring(svg)                      # well-formed XML, not just text
        for forbidden in ("<script", "<image", "@font-face", "xlink:href", "REPLACE_ME"):
            self.assertNotIn(forbidden, svg)
        # the only absolute URL may be the SVG namespace
        self.assertEqual(svg.count("http://") + svg.count("https://"), 1)

    def test_svg_carries_the_data(self):
        svg = self._chart()
        self.assertIn("<circle", svg)
        self.assertIn("admission deadline", svg)
        self.assertIn("idle budget", svg)
        self.assertNotIn("nan", svg.lower())
        self.assertNotIn("None", svg)

    def test_chart_json_envelope(self):
        d = tempfile.mkdtemp()
        out = os.path.join(d, "chart.svg")
        code, stdout, _ = run("chart", "--since", "24h", "--out", out, "--json")
        self.assertEqual(code, 0)
        doc = json.loads(stdout)
        self.assertEqual(doc["command"], "chart")
        self.assertEqual(doc["schema"], 4)
        self.assertEqual(doc["path"], out)
        self.assertIn("contract", doc)


class InstallShape(unittest.TestCase):
    """`fix` and `chart` load their code from sibling files, so they only work
    when those files sit beside the CLI.

    Shipping the CLI as a lone script broke exactly that: `pipx install .` put
    one file in the venv's bin/ and both commands answered "support missing"
    (exit 2). Running from a checkout hid it. These tests cover each install
    path the README documents.
    """

    def _env(self, **extra):
        env = dict(os.environ)
        env["LUNA_DOCTOR_STORE"] = _TEST_STORE
        env.pop("PYTHONPATH", None)
        env["SWAMP_ROOT"] = os.path.join(_TEST_STORE, "no-such-swamp")
        env.update(extra)
        return env

    def _assert_loaded(self, proc, what):
        self.assertNotIn("support missing", proc.stderr, f"{what}: {proc.stderr}")
        self.assertNotIn("Traceback", proc.stderr, f"{what}: {proc.stderr}")

    def test_package_import_loads_both_commands_from_disk(self):
        cli = _load_cli()
        for name, path in (("cmd_fix", cli._FIX_PATH), ("cmd_chart", cli._CHART_PATH)):
            fn = getattr(cli, name)
            self.assertEqual(os.path.abspath(fn.__code__.co_filename), os.path.abspath(path),
                             f"{name} came from the missing-support stub, not {path}")

    def test_installed_layout_runs_fix_and_chart(self):
        """A copy of the package with no checkout around it: what pip installs."""
        with tempfile.TemporaryDirectory() as tmp:
            site = os.path.join(tmp, "site-packages")
            os.makedirs(site)
            shutil.copytree(os.path.join(ROOT, "luna_doctor"),
                            os.path.join(site, "luna_doctor"),
                            ignore=shutil.ignore_patterns("__pycache__"))
            env = self._env(PYTHONPATH=site)
            for args in (("fix", "--since", "1h"),
                         ("chart", "--since", "1h", "--out", os.path.join(tmp, "c.svg"))):
                proc = subprocess.run([sys.executable, "-m", "luna_doctor.cli", *args],
                                      capture_output=True, text=True, cwd=tmp, env=env,
                                      timeout=180)
                self.assertIn(proc.returncode, (0, 1, 2), f"{args[0]}: {proc.stderr}")
                self._assert_loaded(proc, args[0])

    def test_symlink_install_runs_fix_and_chart(self):
        """The no-packaging path: a symlink on PATH pointing into a checkout."""
        with tempfile.TemporaryDirectory() as tmp:
            link = os.path.join(tmp, "luna-doctor")
            os.symlink(CLI, link)
            env = self._env()
            for args in (("fix", "--since", "1h"),
                         ("chart", "--since", "1h", "--out", os.path.join(tmp, "c.svg"))):
                proc = subprocess.run([link, *args], capture_output=True, text=True,
                                      cwd=tmp, env=env, timeout=180)
                self.assertIn(proc.returncode, (0, 1, 2), f"{args[0]}: {proc.stderr}")
                self._assert_loaded(proc, args[0])


class Sources(unittest.TestCase):
    def _module(self):
        return _load_cli()

    def test_concurrency_by_minute_bins_and_carries_over(self):
        """Minute series: per-minute max in-flight, boundary-spanning requests counted."""
        m = self._module()
        base = 1_700_000_000.0
        base -= base % 60                  # align to a minute boundary
        R = m.Request
        reqs = [
            R(harness="pi", session="s", model="m", start=base, end=base + 90),
            R(harness="pi", session="s", model="m", start=base + 30, end=base + 40),
            R(harness="pi", session="s", model="m", start=base + 75, end=base + 95),
            R(harness="pi", session="s", model="m", start=base + 150, end=base + 160,
              outcome="admission_timeout"),
        ]
        series = m.concurrency_by_minute(reqs)
        self.assertEqual(len(series), 3)
        # minute 0: two requests overlap -> peak 2
        self.assertEqual(series[0]["max_concurrent"], 2)
        self.assertEqual(series[0]["started"], 2)
        # minute 1: request 1 still in flight across the boundary, not reset to 0
        self.assertEqual(series[1]["max_concurrent"], 2)
        self.assertEqual(series[1]["started"], 1)
        # minute 2: the admission block lands in the minute it ended
        self.assertEqual(series[2]["max_concurrent"], 1)
        self.assertEqual(series[2]["started"], 1)
        self.assertEqual(series[2]["admission_deaths"], 1)
        for row in series:
            self.assertRegex(row["minute"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z$")

    def test_concurrency_by_minute_empty(self):
        m = self._module()
        self.assertEqual(m.concurrency_by_minute([]), [])

    def test_both_files_compile(self):
        for rel in ("luna_doctor/cli.py", "luna_doctor/_fix_block.py",
                    "luna_doctor/_chart_block.py", "bin/luna-doctor"):
            path = os.path.join(ROOT, rel)
            proc = subprocess.run([sys.executable, "-m", "py_compile", path],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"{rel}: {proc.stderr}")

    def test_emitted_apply_blocks_never_write_on_their_own(self):
        """`fix` must print instructions, never shell that writes."""
        code, out, _ = run("fix", "--since", "24h")
        self.assertIn(code, (0, 1))
        self.assertIn("does not edit files itself", out)
        self.assertIn("Tell your agent", out)
        for bad in ('jq \'', ' > "$f.tmp"', 'cp "$f"', "sed -i"):
            self.assertNotIn(bad, out, f"fix emitted shell code: {bad!r}")


def _load_cli():
    """Import the CLI the way the installed console script does: as a package."""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from luna_doctor import cli
    return cli


def _pi_line(ts, model, start_ms, end_ms, err=None, stop="stop", out=100):
    """One pi assistant record, the shape iter_pi reads."""
    rec = {
        "type": "message",
        "id": f"id-{ts}",
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "model": model,
            "provider": "lunaroute",
            "timestamp": start_ms,
            "usage": {"input": 10, "output": out},
            "stopReason": "error" if err else stop,
            "errorMessage": err,
            "content": [{"type": "text", "text": "x"}],
            "responseId": f"r-{ts}",
        },
    }
    return json.dumps(rec)


class OutcomeClasses(unittest.TestCase):
    """Every string below was seen in real traffic; the class decides the advice."""

    def setUp(self):
        self.m = _load_cli()

    def classify(self, err):
        return self.m.classify({"error_message": err, "stop_reason": "error"})

    def test_org_concurrency_limit_is_not_generic_rate_limit(self):
        err = ('lunaroute API error (429): {"code":"CONCURRENT_REQUEST_LIMIT_EXCEEDED",'
               '"message":"Organization has reached its maximum number of concurrent requests."}')
        self.assertEqual(self.classify(err), "org_concurrency_limit")

    def test_model_not_found(self):
        err = ('lunaroute API error (404): {"code":404,"message":"The model '
               "'glm-5.3-vision' does not exist\",\"param\":\"model\"}")
        self.assertEqual(self.classify(err), "model_not_found")

    def test_context_length_exceeded(self):
        err = ('400: {"message":"This model\'s maximum context length is 524288 tokens. '
               'However, you requested 131072 output tokens"}')
        self.assertEqual(self.classify(err), "context_length_exceeded")

    def test_payload_too_large(self):
        err = ('413: {"code":"PAYLOAD_TOO_LARGE","declared_bytes":18970123,'
               '"max_bytes":16777216}')
        self.assertEqual(self.classify(err), "payload_too_large")

    def test_harness_deadline(self):
        err = "request deadline exceeded after 1195s before the response completed"
        self.assertEqual(self.classify(err), "harness_deadline")
        err2 = "provider unhealthy after 4 stream failures (2m18.7s)"
        self.assertEqual(self.classify(err2), "harness_deadline")

    def test_backend_unavailable_folds_into_upstream_unavailable(self):
        self.assertEqual(self.classify('503: {"error":{"message":"backend unavailable"}}'),
                         "upstream_unavailable")

    def test_admission_timeout_still_wins(self):
        self.assertEqual(self.classify("504: scheduler admission timeout"),
                         "admission_timeout")

    def test_unknown_string_is_still_other_error(self):
        self.assertEqual(self.classify("kernel panic in the tensor router"), "other_error")


class RateLimitIsConcurrency(unittest.TestCase):
    """A 429 on LunaRoute is capacity, not a quota: no advice may imply otherwise.

    The gateway has no quota or token-budget dimension. Every 429,
    `rate_limit_error` and "quota"-looking rejection is concurrent requests
    overrunning what the account admits, so the advice has to send the reader
    to fan-out or the -background tier. `message_limit` is the one exception:
    a message-count ceiling, which fan-out genuinely cannot fix - but it is
    still not a quota.
    """

    def setUp(self):
        self.m = _load_cli()

    def _advice(self, *outcomes):
        base = 1_700_000_000.0
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base + i, end=base + i + 2.0, outcome=outcome)
                for i, outcome in enumerate(outcomes)]
        _, adj, _ = self.m.verdict(self.m.analyse(reqs, 1.0))
        return " ".join(adj)

    def test_rate_limited_advice_names_concurrency_as_the_cause(self):
        joined = self._advice("rate_limited")
        self.assertIn("429", joined)
        self.assertIn("concurrent-request capacity", joined)
        self.assertIn("narrow the fan-out", joined)
        self.assertIn("-background", joined)
        # The old advice told the reader that narrowing fan-out would not help.
        self.assertNotIn("will not fix it", joined)
        self.assertNotIn("not concurrency", joined)

    def test_org_lane_total_advice_is_about_the_whole_lane(self):
        joined = self._advice("org_concurrency_limit")
        self.assertIn("concurrent lane", joined)
        self.assertIn("narrow the fan-out", joined)
        self.assertNotIn("not a rate limit", joined)

    def test_message_limit_is_a_message_count_ceiling(self):
        joined = self._advice("message_limit")
        self.assertIn("message-count limit", joined)
        self.assertIn("not concurrency pressure", joined)
        self.assertIn("fan-out will not fix it", joined)

    def test_no_rendered_advice_says_quota(self):
        """Regression: the tool must never tell a reader a quota exists."""
        base = 1_700_000_000.0
        outcomes = ("admission_timeout", "client_idle_timeout", "org_concurrency_limit",
                    "message_limit", "rate_limited", "context_length_exceeded",
                    "payload_too_large", "model_not_found", "harness_deadline")
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base + i, end=base + i + 2.0, outcome=outcome)
                for i, outcome in enumerate(outcomes)]
        metrics = self.m.analyse(reqs, 1.0)
        head, adj, _ = self.m.verdict(metrics)
        rep = self.m.build_report(metrics, head, adj)
        from luna_doctor.report import render_plain
        from luna_doctor.render_pretty import render_pretty
        from luna_doctor.render_share import render_share_text
        from luna_doctor.style import Style
        rendered = {
            "advice": " ".join(adj),
            "plain": render_plain(rep),
            "pretty": render_pretty(rep, Style(False, False, False)),
            "share": render_share_text(rep, metrics),
        }
        self.assertIn("rate_limit", rendered["advice"], "the sample must reach every branch")
        self.assertIn("message-count", rendered["advice"])
        for surface, text in rendered.items():
            self.assertNotIn("quota", text.lower(), f"{surface} still says quota")


class PerModelDeadlines(unittest.TestCase):
    def setUp(self):
        self.m = _load_cli()

    def _dead(self, model, start, dur, outcome="admission_timeout"):
        r = self.m.Request(harness="pi", session="s", model=model,
                           start=start, end=start + dur, outcome=outcome)
        return r

    def test_median_and_spread_per_model(self):
        base = 1_700_000_000.0
        reqs = ([self._dead("glm-5.3", base + i * 500, 120.2 + i * 0.1) for i in range(5)]
                + [self._dead("deepseek-4.1-flash", base + i * 900, 60.3) for i in range(3)])
        d = self.m.per_model_deadlines(reqs)
        self.assertAlmostEqual(d["glm-5.3"]["deadline_s"], 120.4, places=1)
        self.assertEqual(d["glm-5.3"]["n"], 5)
        self.assertAlmostEqual(d["deepseek-4.1-flash"]["deadline_s"], 60.3, places=1)

    def test_unknown_below_three_samples(self):
        """A deadline borrowed from one observation is a guess with a number on it."""
        base = 1_700_000_000.0
        reqs = [self._dead("new-model", base, 90.0), self._dead("new-model", base + 500, 90.1)]
        d = self.m.per_model_deadlines(reqs)
        self.assertIsNone(d["new-model"]["deadline_s"])
        self.assertEqual(d["new-model"]["status"], "unknown")

    def test_completed_requests_are_not_deadline_evidence(self):
        base = 1_700_000_000.0
        reqs = [self._dead("glm-5.3", base + i, 120.2, outcome="completed") for i in range(5)]
        self.assertEqual(self.m.per_model_deadlines(reqs), {})


class TimeoutStorms(unittest.TestCase):
    def setUp(self):
        self.m = _load_cli()

    def _r(self, start, dur, outcome="client_idle_timeout"):
        return self.m.Request(harness="pi", session="s", model="glm-5.3",
                              start=start, end=start + dur, outcome=outcome)

    def test_three_same_duration_within_a_minute_is_a_cohort(self):
        base = 1_700_000_000.0
        reqs = [self._r(base, 300.0), self._r(base + 10, 300.0), self._r(base + 20, 300.0)]
        storms = self.m.timeout_storms(reqs)
        self.assertEqual(len(storms), 1)
        self.assertEqual(storms[0]["cohort"], 3)
        self.assertAlmostEqual(storms[0]["duration_s"], 300.0)

    def test_two_is_not_a_cohort(self):
        base = 1_700_000_000.0
        self.assertEqual(self.m.timeout_storms([self._r(base, 300.0), self._r(base + 5, 300.0)]), [])

    def test_different_durations_do_not_merge(self):
        base = 1_700_000_000.0
        reqs = [self._r(base, 300.0), self._r(base + 5, 120.2, "admission_timeout"),
                self._r(base + 10, 300.0)]
        self.assertEqual(self.m.timeout_storms(reqs), [])

    def test_a_cohort_spread_beyond_the_window_is_split(self):
        base = 1_700_000_000.0
        reqs = ([self._r(base, 300.0), self._r(base + 5, 300.0), self._r(base + 10, 300.0)]
                + [self._r(base + 900, 300.0), self._r(base + 905, 300.0),
                   self._r(base + 910, 300.0)])
        self.assertEqual(len(self.m.timeout_storms(reqs)), 2)


class SaturationBands(unittest.TestCase):
    def setUp(self):
        self.m = _load_cli()

    def _cluster(self, base, n, dur, outcome="completed"):
        """n requests all overlapping, so each sees concurrency n."""
        return [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base, end=base + dur, outcome=outcome)
                for _ in range(n)]

    def test_bands_bucket_by_concurrency_during_the_window(self):
        base = 1_700_000_000.0
        reqs = self._cluster(base, 3, 10.0) + self._cluster(base + 600, 11, 10.0)
        b = self.m.saturation_bands(reqs)
        widths = {row["band"]: row["n"] for row in b["bands"]}
        self.assertEqual(widths["3-5"], 3)
        self.assertEqual(widths["10+"], 11)

    def test_inflation_needs_five_samples_in_the_top_band(self):
        base = 1_700_000_000.0
        slow = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base + i, end=base + i + 60.0, outcome="completed")
                for i in range(11)]           # concurrency 11 -> top band, all 60s
        fast = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base + 5000 + i * 100, end=base + 5000 + i * 100 + 5.0,
                               outcome="completed") for i in range(3)]
        b = self.m.saturation_bands(slow + fast)
        self.assertTrue(b["top_inflation_flagged"])

    def test_thin_top_band_does_not_claim_a_gradient(self):
        """Ten requests overlap, but only two of them completed: the band is real
        and thin, which is exactly the case a min-sample guard exists for."""
        base = 1_700_000_000.0
        cluster = self._cluster(base, 10, 60.0)
        for r in cluster[2:]:
            r.outcome = "aborted"          # still concurrent, not a latency sample
        fast = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base + 5000 + i * 100, end=base + 5000 + i * 100 + 5.0,
                               outcome="completed") for i in range(4)]
        b = self.m.saturation_bands(cluster + fast)
        self.assertEqual(b["bands"][-1]["n"], 2, "the top band must actually hold samples")
        self.assertFalse(b["top_inflation_flagged"], "two samples cannot establish a gradient")
        self.assertIsNotNone(b["note"])


class UnknownErrors(unittest.TestCase):
    def setUp(self):
        self.m = _load_cli()

    def test_signature_normalizes_variable_parts(self):
        a = self.m.error_signature("Provider returned 503 for request 9f8a7b6c at 12:30")
        b = self.m.error_signature("provider returned 500 for request deadbeef at 14:02")
        self.assertEqual(a, b)
        self.assertLessEqual(len(a), 120)

    def test_signature_lowercases(self):
        self.assertEqual(self.m.error_signature("Backend Unavailable"),
                         self.m.error_signature("backend unavailable"))

    def test_unknown_errors_are_surfaced_with_timing(self):
        base = 1_700_000_000.0
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base + i, end=base + i + 5.0, outcome="other_error",
                               error_message=f"weird gateway string variant {i}")
                for i in range(3)]
        u = self.m.unknown_errors(reqs)
        self.assertEqual(len(u), 1)
        self.assertEqual(u[0]["count"], 3)
        self.assertIn("first", u[0])
        self.assertIn("last", u[0])

    def test_known_classes_are_not_unknown(self):
        reqs = [self.m.Request(harness="pi", session="s", model="m", start=1.0, end=2.0,
                               outcome="admission_timeout",
                               error_message="scheduler admission timeout")]
        self.assertEqual(self.m.unknown_errors(reqs), [])


class RootsAndPruning(unittest.TestCase):
    """R1 from the builder check: mirrors keep source mtimes, so mtime pruning
    silently hides months-old data that is the whole point of --pi-root."""

    def _store(self, tmp, mtime_epoch, records):
        proj = os.path.join(tmp, "proj", "--rat-run--")
        os.makedirs(proj, exist_ok=True)
        path = os.path.join(proj, "2026-08-13T06-41-17-867Z_abc.jsonl")
        with open(path, "w") as fh:
            fh.write("\n".join(records) + "\n")
        os.utime(path, (mtime_epoch, mtime_epoch))
        return path

    def test_old_mtime_does_not_hide_recent_records(self):
        m = _load_cli()
        with tempfile.TemporaryDirectory() as tmp:
            now = time.time()
            start_ms = int((now - 900) * 1000)
            ts = datetime.datetime.fromtimestamp(now - 800, datetime.timezone.utc)
            rec = _pi_line(ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "sentinel-model", start_ms, None)
            self._store(tmp, now - 90 * 86400, [rec])   # mtime 3 months old
            reqs = m.collect(24, ["pi"], pi_roots=[tmp])
            self.assertEqual(len(reqs), 1, "record timestamps must decide, not file mtime")
            self.assertEqual(reqs[0].model, "sentinel-model")

    def test_explicit_root_suppresses_the_default_store(self):
        m = _load_cli()
        with tempfile.TemporaryDirectory() as tmp:
            now = time.time()
            start_ms = int((now - 900) * 1000)
            ts = datetime.datetime.fromtimestamp(now - 800, datetime.timezone.utc)
            rec = _pi_line(ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "sentinel-model", start_ms, None)
            self._store(tmp, now, [rec])
            code, out, err = run("analyze", "--since", "1h", "--json",
                                 "--pi-root", tmp, "--harness", "pi")
            self.assertEqual(code, 0, err)
            doc = json.loads(out)
            models = {r["model"] for r in doc["metrics"].get("requests_by_model", [])}
            self.assertEqual(models, {"sentinel-model"},
                             "an explicit --pi-root must not also sweep ~/.pi/agent/sessions")


class ExtractionStore(unittest.TestCase):
    def setUp(self):
        self.m = _load_cli()

    def test_store_dir_env_override(self):
        os.environ["LUNA_DOCTOR_STORE"] = "/tmp/luna-doctor-test-store"
        try:
            self.assertEqual(self.m.store_dir(), "/tmp/luna-doctor-test-store")
        finally:
            del os.environ["LUNA_DOCTOR_STORE"]
        self.assertTrue(self.m.store_dir().endswith(".luna-doctor/store"))

    def test_run_is_persisted_and_findings_appended_once_per_signature(self):
        base = 1_700_000_000.0
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base + i, end=base + i + 300.0,
                               outcome="client_idle_timeout",
                               error_message="Stream ended without finish_reason")
                for i in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            m = self.m.analyse(reqs, 1.0)
            first = self.m.persist_run(m, window_hours=1.0, store=tmp)
            self.assertTrue(os.path.isfile(first["path"]))
            with open(first["path"]) as fh:
                doc = json.load(fh)
            self.assertEqual(doc["schema"], 4)
            with open(first["ledger"]) as fh:
                lines = [json.loads(x) for x in fh if x.strip()]
                events = self.m.ledger_rows(m, "stores:default")
            self.assertEqual(len(lines), len(events),
                             "the ledger holds one row per event; the report holds every finding")
            second = self.m.persist_run(m, window_hours=1.0, store=tmp)
            with open(second["ledger"]) as fh:
                lines2 = [json.loads(x) for x in fh if x.strip()]
            self.assertEqual(len(lines2), len(lines), "the ledger is append-only, not duplicated")


class SchemaTwo(unittest.TestCase):
    def test_analyze_envelope_is_schema_2_and_drops_the_borrowed_deadline(self):
        code, out, err = run("analyze", "--since", "1h", "--json")
        self.assertIn(code, (0, 1, 2), err)
        doc = json.loads(out)
        self.assertEqual(doc["schema"], 4)
        self.assertNotIn("admission_deadline_s", doc["contract"],
                         "per-model deadlines replace the borrowed constant")
        for key in ("pre_header_wait_s", "client_idle_timeout_s", "interactive_burst_cap"):
            self.assertIn(key, doc["contract"])


# The reproduction store is supplied by the environment: the path names a private
# project, and this test file ships in a public repo.
MIRROR = os.environ.get("LUNA_DOCTOR_RAT_MIRROR", "")


@unittest.skipUnless(MIRROR and os.path.isdir(os.path.join(MIRROR, "pi")),
                     "set LUNA_DOCTOR_RAT_MIRROR to a pi+claude mirror to run the reproduction")
class RatRunReproduction(unittest.TestCase):
    """The success test from the design doc, run against the real stores.

    Three August runs, three measurements each. Numbers below were measured by
    this implementation against those stores, and the one that differs from the
    hand sweep is explained rather than smoothed over: pooled-c10 shows 28
    timeout-class wall deaths plus one *completed* request at 298.5s, which a
    looser "it ran about 300s" count folds into 29. The completion did not die
    on a wall, so it is reported separately.
    """

    @classmethod
    def setUpClass(cls):
        code, out, err = run("analyze", "--since", "100y", "--json",
                             "--pi-root", os.path.join(MIRROR, "pi"),
                             "--claude-root", os.path.join(MIRROR, "claude"),
                             "--store-dir", tempfile.mkdtemp(prefix="ld-test-"))
        assert code in (0, 1), err
        cls.doc = json.loads(out)
        cls.m = cls.doc["metrics"]

    def _run(self, needle):
        found = [r for r in self.m["by_run"] if needle in r["run"]]
        self.assertEqual(len(found), 1, f"expected exactly one run matching {needle}")
        return found[0]

    def test_wall_300s_counts_per_run(self):
        expected = {
            "--rat-glm-litellm-background-6arm-n100-c10": 21,
            "--rat-glm-litellm-pooled-6arm-n100-c10": 28,
            "--rat-glm-litellm-pooled-6arm-n100-c14-restart2": 40,
        }
        for needle, n in expected.items():
            r = self._run(needle)
            self.assertEqual(r["wall_counts_s"].get("300.0"), n, r["run"])

    def test_the_29th_request_was_a_completion_not_a_wall_death(self):
        r = self._run("--rat-glm-litellm-pooled-6arm-n100-c10")
        self.assertEqual(r["slow_completions_over_290s"], 1)

    def test_peak_concurrency_is_10_not_higher(self):
        for needle in ("background-6arm-n100-c10", "pooled-6arm-n100-c10",
                       "pooled-6arm-n100-c14-restart2"):
            self.assertEqual(self._run(needle)["peak_concurrency"], 10)

    def test_lockstep_storms_are_ten_packs(self):
        packs = [s for s in self.m["timeout_storms"] if s["cohort"] >= 10]
        self.assertGreaterEqual(len(packs), 3, "the restart produced at least three 10-packs")
        for s in packs:
            self.assertAlmostEqual(s["duration_s"], 300.0, delta=1.0)

    def test_saturation_gradient_inflates_at_the_ceiling(self):
        b = self.m["saturation_bands"]
        self.assertTrue(b["top_inflation_flagged"])
        self.assertGreaterEqual(b["top_inflation"], 2.0)

    def test_message_limit_is_its_own_class(self):
        self.assertGreater(self.m["by_outcome"].get("message_limit", 0), 100)
        self.assertNotIn("message limit", json.dumps(self.m["unknown_errors"]).lower())


class ReviewerRegressions(unittest.TestCase):
    """Each test here closes a specific roborev finding from iteration 1."""

    def setUp(self):
        self.m = _load_cli()

    def test_zero_length_request_does_not_inflate_later_bands(self):
        """codex: a zero-length interval stayed active forever, poisoning the sweep."""
        base = 1_700_000_000.0
        reqs = [self.m.Request(harness="pi", session="s", model="m",
                               start=base, end=base, outcome="completed")]
        reqs += [self.m.Request(harness="pi", session="s", model="m",
                                start=base + 100 + i * 50, end=base + 105 + i * 50,
                                outcome="completed") for i in range(3)]
        peaks = self.m.peak_concurrency_during(reqs)
        self.assertEqual(max(peaks.values()), 1, "a zero-length request overlapped nothing")

    def test_interleaved_duration_does_not_flush_a_cohort(self):
        """codex: three 120s timeouts were split by an interleaved 60s timeout."""
        base = 1_700_000_000.0
        r = lambda start, dur, oc="admission_timeout": self.m.Request(
            harness="pi", session="s", model="glm-5.3", start=start, end=start + dur, outcome=oc)
        reqs = [r(base, 120.0), r(base + 5, 60.0), r(base + 10, 120.0), r(base + 20, 120.0)]
        storms = self.m.timeout_storms(reqs)
        self.assertEqual([s["cohort"] for s in storms], [3])
        self.assertAlmostEqual(storms[0]["duration_s"], 120.0)

    def test_claude_run_attribution(self):
        """codex: iter_claude computed `run` and then dropped it."""
        import datetime as _dt
        with tempfile.TemporaryDirectory() as tmp:
            now = time.time()
            for proj in ("proj-a", "proj-b"):
                d = os.path.join(tmp, proj)
                os.makedirs(d)
                tss = (_dt.datetime.fromtimestamp(now - 60, _dt.timezone.utc)
                       .strftime("%Y-%m-%dT%H:%M:%S.000Z"))
                recs = [
                    json.dumps({"type": "user", "timestamp": tss}),
                    json.dumps({"type": "assistant", "timestamp": tss,
                                "message": {"id": f"m-{proj}", "model": "glm-5.3",
                                            "stop_reason": "end_turn",
                                            "usage": {"input_tokens": 1, "output_tokens": 2}}}),
                ]
                with open(os.path.join(d, "s.jsonl"), "w") as fh:
                    fh.write("\n".join(recs) + "\n")
            reqs = self.m.collect(1, ["claude-code"], claude_roots=[tmp])
            # root-qualified: the store label precedes the project directory
            self.assertEqual({r.run.split("/")[-1] for r in reqs}, {"proj-a", "proj-b"})

    def test_ledger_records_one_row_per_signature_per_window(self):
        """both reviewers: same-signature cohorts duplicated, `seen` never updated."""
        base = 1_700_000_000.0
        reqs = []
        for round_ in range(3):
            start = base + round_ * 3600
            reqs += [self.m.Request(harness="pi", session="s", model="glm-5.3",
                                    start=start + i, end=start + i + 300.0,
                                    outcome="client_idle_timeout",
                                    error_message="Stream ended without finish_reason")
                     for i in range(10)]
        m = self.m.analyse(reqs, 1.0)
        with tempfile.TemporaryDirectory() as tmp:
            out = self.m.persist_run(m, 1.0, store=tmp)
            with open(out["ledger"]) as fh:
                rows = [json.loads(x) for x in fh if x.strip()]
        storm_rows = [r for r in rows if r["kind"] == "timeout_storm"]
        self.assertEqual(len(storm_rows), 3, "a cohort is the event")
        self.assertEqual({r["count"] for r in storm_rows}, {10})
        self.assertEqual(len({r["event_start"] for r in storm_rows}), 3)

    def test_different_sources_do_not_share_a_file_or_a_ledger_key(self):
        """codex: a mirror run and a local run collided on window identity."""
        m = {"requests": 1, "by_outcome": {}, "per_model_deadlines": {},
             "timeout_storms": [{"outcome": "client_idle_timeout", "duration_s": 300.0,
                                 "cohort": 3, "start": "2026-01-01T00:00:00+00:00",
                                 "end": "2026-01-01T00:05:00+00:00", "blocked_s": 300.0,
                                 "models": ["m"]}],
             "saturation_bands": {}, "unknown_errors": [], "by_run": []}
        with tempfile.TemporaryDirectory() as tmp:
            a = self.m.persist_run(m, 1.0, store=tmp, sources="pi:/mirror")
            b = self.m.persist_run(m, 1.0, store=tmp, sources="stores:default")
            self.assertNotEqual(a["path"], b["path"])
            with open(a["ledger"]) as fh:
                rows = [json.loads(x) for x in fh if x.strip()]
        self.assertEqual(len({r["sources"] for r in rows}), 2)

    def test_window_recorded_is_the_window_analysed(self):
        """claude-code: a 24h window was recorded as ending at the previous midnight."""
        m = {"requests": 1, "by_outcome": {}, "per_model_deadlines": {},
             "timeout_storms": [], "saturation_bands": {}, "unknown_errors": [], "by_run": []}
        now = 1_700_000_000.0
        with tempfile.TemporaryDirectory() as tmp:
            out = self.m.persist_run(m, 24.0, store=tmp, since=now - 86400, until=now)
            with open(out["path"]) as fh:
                doc = json.load(fh)
        self.assertEqual(doc["window"]["end"],
                         datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat())
        self.assertEqual(doc["window"]["hours"], 24.0)

    def test_mtime_prune_survives_for_a_live_store(self):
        """claude-code: dropping the prune everywhere made default runs parse everything."""
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "proj")
            os.makedirs(d)
            path = os.path.join(d, "old.jsonl")
            start_ms = int((now - 900) * 1000)
            ts = datetime.datetime.fromtimestamp(now - 800, datetime.timezone.utc)
            with open(path, "w") as fh:
                fh.write(_pi_line(ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "m", start_ms, None) + "\n")
            os.utime(path, (now - 90 * 86400, now - 90 * 86400))
            pruned = self.m.iter_pi([tmp], now - 3600, now, prune_mtime=True)
            kept = self.m.iter_pi([tmp], now - 3600, now, prune_mtime=False)
        self.assertEqual(pruned, [], "a live store may prune on mtime")
        self.assertEqual(len(kept), 1, "a mirror may not")


class SecondReviewRegressions(unittest.TestCase):
    """Iteration 2 findings, one test each."""

    def setUp(self):
        self.m = _load_cli()

    def _r(self, start, dur, oc="client_idle_timeout"):
        return self.m.Request(harness="pi", session="s", model="glm-5.3",
                              start=start, end=start + dur, outcome=oc)

    def test_sliding_window_keeps_the_recent_cohort(self):
        """codex: endings at 0/55/65/70 produced no cohort at all."""
        base = 1_700_000_000.0
        reqs = [self._r(base + off, 300.0) for off in (0, 55, 65, 70)]
        storms = self.m.timeout_storms(reqs)
        self.assertEqual([s["cohort"] for s in storms], [3])

    def test_duration_clustering_ignores_a_rounding_boundary(self):
        """both: 300.49/300.51/300.52 landed in different integer buckets."""
        base = 1_700_000_000.0
        reqs = [self._r(base + i, d) for i, d in enumerate((300.49, 300.51, 300.52))]
        storms = self.m.timeout_storms(reqs)
        self.assertEqual([s["cohort"] for s in storms], [3])

    def test_min_cohort_argument_is_honoured(self):
        """claude-code: _storm_row ignored the caller's min_cohort."""
        base = 1_700_000_000.0
        reqs = [self._r(base + i, 300.0) for i in range(4)]
        self.assertEqual(self.m.timeout_storms(reqs, min_cohort=4)[0]["cohort"], 4)
        self.assertEqual(self.m.timeout_storms(reqs, min_cohort=5), [])

    def test_source_identity_ignores_argument_order(self):
        """codex: reversing root order minted a second identity for one dataset."""
        a = self.m._source_id(["pi"], ["/tmp/a", "/tmp/b"], None)
        b = self.m._source_id(["pi"], ["/tmp/b", "/tmp/a"], None)
        self.assertEqual(a, b)

    def test_overlapping_windows_record_one_occurrence_per_event(self):
        """claude-code: overlapping runs inflated the repeat history."""
        base = 1_700_000_000.0
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base + i, end=base + i + 300.0,
                               outcome="client_idle_timeout",
                               error_message="Stream ended without finish_reason")
                for i in range(3)]
        m = self.m.analyse(reqs, 1.0)
        with tempfile.TemporaryDirectory() as tmp:
            a = self.m.persist_run(m, 1.0, store=tmp, since=base, until=base + 3600)
            b = self.m.persist_run(m, 24.0, store=tmp, since=base - 82800, until=base + 3600)
            self.assertNotEqual(a["path"], b["path"], "each window keeps its own report")
            with open(a["ledger"]) as fh:
                rows = [json.loads(x) for x in fh if x.strip()]
        self.assertEqual(len([r for r in rows if r["kind"] == "timeout_storm"]), 1,
                         "the same storm in two windows is one occurrence")

    def test_a_mistyped_root_is_reported_and_reads_nothing(self):
        """claude-code: a typo'd --pi-root produced a clean, empty diagnosis."""
        code, out, err = run("analyze", "--since", "1h", "--json",
                             "--pi-root", "/nonexistent-luna-doctor-test",
                             "--harness", "pi")
        self.assertIn("is not a directory", err)
        self.assertEqual(json.loads(out)["metrics"]["requests"], 0)

    def test_duplicate_claude_roots_do_not_double_count(self):
        """codex: the response dedupe set reset per file, so repeat roots doubled."""
        import datetime as _dt
        with tempfile.TemporaryDirectory() as tmp:
            now = time.time()
            d = os.path.join(tmp, "proj")
            os.makedirs(d)
            tss = (_dt.datetime.fromtimestamp(now - 60, _dt.timezone.utc)
                   .strftime("%Y-%m-%dT%H:%M:%S.000Z"))
            recs = [json.dumps({"type": "user", "timestamp": tss}),
                    json.dumps({"type": "assistant", "timestamp": tss,
                                "message": {"id": "m-same", "model": "glm-5.3",
                                            "stop_reason": "end_turn",
                                            "usage": {"input_tokens": 1, "output_tokens": 2}}})]
            with open(os.path.join(d, "s.jsonl"), "w") as fh:
                fh.write("\n".join(recs) + "\n")
            once = self.m.collect(1, ["claude-code"], claude_roots=[tmp])
            twice = self.m.collect(1, ["claude-code"], claude_roots=[tmp, tmp])
        self.assertEqual(len(once), 1)
        self.assertEqual(len(twice), 1, "the same response is one request however many roots see it")


class ThirdReviewRegressions(unittest.TestCase):
    """Iteration 3 findings."""

    def setUp(self):
        self.m = _load_cli()

    def _reqs(self, base, n=3, dur=300.0):
        return [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base + i, end=base + i + dur,
                               outcome="client_idle_timeout",
                               error_message="Stream ended without finish_reason")
                for i in range(n)]

    def test_window_aggregates_are_not_occurrences(self):
        """claude-code (twice): an outcome count moves whenever the window edges move."""
        base = 1_700_000_000.0
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base + i, end=base + i + 120.0,
                               outcome="admission_timeout") for i in range(3)]
        m = self.m.analyse(reqs, 24.0)
        with tempfile.TemporaryDirectory() as tmp:
            out = self.m.persist_run(m, 24.0, store=tmp, since=base - 86400, until=base + 60)
            with open(out["ledger"]) as fh:
                rows = [json.loads(x) for x in fh if x.strip()]
            self.assertTrue(out["findings"], "the report still carries every finding")
        self.assertEqual([r for r in rows if r["kind"] == "outcome"], [],
                         "window summaries belong in the report, not the repeat history")

    def test_hourly_cron_over_the_same_window_does_not_stack_outcome_rows(self):
        """claude-code: aggregate findings fell back to a window key that moved hourly."""
        base = 1_700_000_000.0
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base + i, end=base + i + 120.0,
                               outcome="admission_timeout",
                               error_message="scheduler admission timeout") for i in range(4)]
        m = self.m.analyse(reqs, 24.0)
        with tempfile.TemporaryDirectory() as tmp:
            for hour in range(3):
                self.m.persist_run(m, 24.0, store=tmp,
                                   since=base + hour * 3600 - 82800,
                                   until=base + hour * 3600 + 3600)
            with open(os.path.join(tmp, "findings.jsonl")) as fh:
                rows = [json.loads(x) for x in fh if x.strip()]
        storm_rows = [r for r in rows if r["kind"] == "timeout_storm"]
        self.assertLessEqual(len(storm_rows), 1, "unchanged event findings do not stack")

    def test_a_growing_window_records_the_new_observation(self):
        """codex: a later window containing the same storm plus a new one was discarded."""
        base = 1_700_000_000.0
        first = self.m.analyse(self._reqs(base), 1.0)
        later = self.m.analyse(self._reqs(base) + self._reqs(base + 7200), 24.0)
        with tempfile.TemporaryDirectory() as tmp:
            self.m.persist_run(first, 1.0, store=tmp, since=base, until=base + 3600)
            self.m.persist_run(later, 24.0, store=tmp, since=base, until=base + 86400)
            with open(os.path.join(tmp, "findings.jsonl")) as fh:
                rows = [json.loads(x) for x in fh if x.strip()]
        storms = [r for r in rows if r["kind"] == "timeout_storm"]
        self.assertEqual(len(storms), 2, "a second cohort is a new event")
        self.assertEqual(len({r["event_start"] for r in storms}), 2,
                         "each cohort carries its own event identity")

    def test_overlapping_cohorts_from_two_models_are_two_ledger_rows(self):
        """claude-code: class-only signatures plus in-run overlap dedupe collapsed them."""
        base = 1_700_000_000.0
        reqs = ([self.m.Request(harness="pi", session="s", model="glm-5.3",
                                start=base + off, end=base + off + 120.2,
                                outcome="admission_timeout") for off in (0, 5, 10)]
                + [self.m.Request(harness="pi", session="s", model="deepseek-4.1-flash",
                                  start=base + off, end=base + off + 60.3,
                                  outcome="admission_timeout") for off in (15, 20, 25)])
        m = self.m.analyse(reqs, 1.0)
        with tempfile.TemporaryDirectory() as tmp:
            out = self.m.persist_run(m, 1.0, store=tmp, since=base - 3600, until=base + 3600)
            with open(out["ledger"]) as fh:
                rows = [json.loads(x) for x in fh if x.strip()]
        storms = [r for r in rows if r["kind"] == "timeout_storm"]
        self.assertEqual(len(storms), 2, "two models, two cohorts, two events")
        self.assertEqual({tuple(r["detail"]["models"]) for r in storms},
                         {("glm-5.3",), ("deepseek-4.1-flash",)})

    def test_back_to_back_rounds_are_two_ledger_rows(self):
        """claude-code: consecutive lockstep rounds overlap by a fraction of a second."""
        base = 1_700_000_000.0
        reqs = []
        for round_ in range(2):
            offset = round_ * 300.2        # second round starts before the first ends
            reqs += [self.m.Request(harness="pi", session="s", model="glm-5.3",
                                    start=base + offset + i, end=base + offset + i + 300.0,
                                    outcome="client_idle_timeout",
                                    error_message="Stream ended without finish_reason")
                     for i in range(10)]
        m = self.m.analyse(reqs, 1.0)
        self.assertEqual(len(m["timeout_storms"]), 2)
        with tempfile.TemporaryDirectory() as tmp:
            out = self.m.persist_run(m, 1.0, store=tmp, since=base - 3600, until=base + 3600)
            with open(out["ledger"]) as fh:
                rows = [json.loads(x) for x in fh if x.strip()]
        self.assertEqual(len([r for r in rows if r["kind"] == "timeout_storm"]), 2)

    def test_the_same_cohort_seen_twice_is_one_row(self):
        base = 1_700_000_000.0
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base + i, end=base + i + 300.0,
                               outcome="client_idle_timeout",
                               error_message="Stream ended without finish_reason")
                for i in range(10)]
        m = self.m.analyse(reqs, 1.0)
        with tempfile.TemporaryDirectory() as tmp:
            self.m.persist_run(m, 1.0, store=tmp, since=base - 3600, until=base + 3600)
            self.m.persist_run(m, 1.0, store=tmp, since=base - 3600, until=base + 3600)
            with open(os.path.join(tmp, "findings.jsonl")) as fh:
                rows = [json.loads(x) for x in fh if x.strip()]
        self.assertEqual(len([r for r in rows if r["kind"] == "timeout_storm"]), 1)

    def test_a_small_pack_is_not_starved_by_a_larger_one(self):
        """claude-code: the larger cohort won the window and the smaller was dropped."""
        base = 1_700_000_000.0
        small = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                                start=base + off - 10.0, end=base + off,
                                outcome="request_timeout") for off in (0, 1, 2)]
        large = [self.m.Request(harness="pi", session="s", model="deepseek-4.1-flash",
                                start=base + off - 60.0, end=base + off,
                                outcome="request_timeout") for off in (3, 4, 5, 6)]
        storms = self.m.timeout_storms(small + large)
        self.assertEqual(sorted(s["cohort"] for s in storms), [3, 4])

    def test_distinct_cohorts_in_one_run_are_both_recorded(self):
        """codex: in-run fuzzy matching discarded a second, closer cohort."""
        base = 1_700_000_000.0
        reqs = []
        for start_off in (0, 40, 50, 61, 62, 63):
            reqs.append(self.m.Request(harness="pi", session="s", model="glm-5.3",
                                       start=base + start_off, end=base + start_off + 300.0,
                                       outcome="client_idle_timeout",
                                       error_message="Stream ended without finish_reason"))
        m = self.m.analyse(reqs, 1.0)
        self.assertEqual(len(m["timeout_storms"]), 2)
        with tempfile.TemporaryDirectory() as tmp:
            out = self.m.persist_run(m, 1.0, store=tmp, since=base - 3600, until=base + 3600)
            with open(out["ledger"]) as fh:
                rows = [json.loads(x) for x in fh if x.strip()]
        self.assertEqual(len([r for r in rows if r["kind"] == "timeout_storm"]), 2)

    def test_context_error_with_a_model_param_is_not_a_missing_model(self):
        """codex: `param: model` alone classified a context-length 400 as a stale id."""
        err = ('400: {"message":"This model\'s maximum context length is 524288 tokens. '
               'However, you requested 131072 output tokens","param":"model"}')
        self.assertEqual(self.m.classify({"error_message": err, "stop_reason": "error"}),
                         "context_length_exceeded")

    def test_a_cohort_cut_by_a_window_is_recorded_as_seen(self):
        """Documented trade: an exact member set means a cut cohort can be
        recorded twice, but an event is never lost. The report is authoritative."""
        base = 1_700_000_000.0
        full = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base + i, end=base + i + 300.0,
                               outcome="client_idle_timeout",
                               error_message="Stream ended without finish_reason")
                for i in range(10)]
        with tempfile.TemporaryDirectory() as tmp:
            self.m.persist_run(self.m.analyse(full[:5], 1.0), 1.0, store=tmp,
                               since=base - 3600, until=base + 6)
            self.m.persist_run(self.m.analyse(full, 1.0), 1.0, store=tmp,
                               since=base - 3600, until=base + 3600)
            with open(os.path.join(tmp, "findings.jsonl")) as fh:
                rows = [json.loads(x) for x in fh if x.strip()]
        self.assertEqual(len([r for r in rows if r["kind"] == "timeout_storm"]), 2,
                         "a partial view and the full view are both recorded")

    def test_unclassified_errors_are_reported_but_not_ledgered(self):
        """No stable occurrence boundary exists for a stream of sightings."""
        base = 1_700_000_000.0
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base + off, end=base + off + 5.0,
                               outcome="other_error", error_message="backend unavailable")
                for off in (0, 600, 3 * 3600)]
        m = self.m.analyse(reqs, 24.0)
        self.assertEqual(len(m["unknown_error_bursts"]), 2, "bursts are still surfaced")
        with tempfile.TemporaryDirectory() as tmp:
            out = self.m.persist_run(m, 24.0, store=tmp, since=base - 86400, until=base + 86400)
            with open(out["ledger"]) as fh:
                rows = [json.loads(x) for x in fh if x.strip()]
            self.assertTrue(any(f["kind"] == "unknown_error" for f in out["findings"]),
                            "the report still carries the finding")
        self.assertEqual([r for r in rows if r["kind"] == "unknown_error"], [])

    def test_duration_outliers_do_not_veto_a_valid_subset(self):
        """codex: neighbouring outliers widened every anchor past the tolerance."""
        base = 1_700_000_000.0
        durs = (301.125, 301.875, 300.875, 300.0, 302.125, 303.0)
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base + i, end=base + i + d, outcome="request_timeout")
                for i, d in enumerate(durs)]
        storms = self.m.timeout_storms(reqs)
        self.assertEqual([s["cohort"] for s in storms], [3])

    def test_bands_do_not_pool_models_or_tiers(self):
        """codex: a slow background model masqueraded as a concurrency gradient."""
        base = 1_700_000_000.0
        fast = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base + i * 100, end=base + i * 100 + 10.0,
                               outcome="completed", tier="interactive") for i in range(3)]
        slow = [self.m.Request(harness="pi", session="s", model="deepseek-4.1-flash",
                               start=base + 5000, end=base + 5600.0,
                               outcome="completed", tier="background") for _ in range(10)]
        b = self.m.saturation_bands(fast + slow)
        self.assertFalse(b["top_inflation_flagged"],
                         "different models and tiers are not a gradient")
        self.assertEqual(len(b["groups"]), 2)

    def test_the_summary_comes_from_one_group(self):
        """codex: the summary took bands from the busiest group and a ratio from another."""
        base = 1_700_000_000.0
        busy = [self.m.Request(harness="pi", session="s", model="model-a",
                               start=base + i * 50, end=base + i * 50 + 5.0,
                               outcome="completed") for i in range(20)]
        flagged = ([self.m.Request(harness="pi", session="s", model="model-b",
                                   start=base + 9000 + i * 100,
                                   end=base + 9000 + i * 100 + 5.0,
                                   outcome="completed") for i in range(3)]
                   + [self.m.Request(harness="pi", session="s", model="model-b",
                                     start=base + 4000, end=base + 4060.0,
                                     outcome="completed") for _ in range(10)])
        b = self.m.saturation_bands(busy + flagged)
        if b["top_inflation_flagged"]:
            top_n = b["bands"][-1]["n"]
            self.assertGreaterEqual(top_n, 5,
                                    "a flagged summary must have samples in its own top band")
        findings = self.m.run_findings({"timeout_storms": [], "unknown_errors": [],
                                        "saturation_bands": b, "per_model_deadlines": {},
                                        "by_outcome": {}})
        for f in findings:
            if f["kind"] == "saturation_gradient":
                self.assertGreater(f["count"], 0)

    def test_member_identity_prefers_response_ids(self):
        """codex: session filenames made the same cohort hash differently by root."""
        base = 1_700_000_000.0

        def cohort(session):
            return [self.m.Request(harness="pi", session=session, model="glm-5.3",
                                   start=base + i, end=base + i + 300.0,
                                   outcome="client_idle_timeout", response_id=f"r-{i}")
                    for i in range(3)]
        self.assertEqual(self.m._member_id(cohort("mirror-a/proj/x.jsonl")),
                         self.m._member_id(cohort("mirror-b/proj/y.jsonl")))

    def test_two_windows_ending_in_the_same_hour_keep_both_reports(self):
        """pi reviewer: a 24h run at 10:05 and 10:50 overwrote each other."""
        m = {"requests": 0, "by_outcome": {}, "per_model_deadlines": {},
             "timeout_storms": [], "saturation_bands": {}, "unknown_errors": [], "by_run": []}
        base = 1_700_000_000.0
        with tempfile.TemporaryDirectory() as tmp:
            a = self.m.persist_run(m, 24.0, store=tmp, since=base, until=base + 86400)
            b = self.m.persist_run(m, 24.0, store=tmp, since=base + 2700, until=base + 86400 + 2700)
            self.assertNotEqual(a["path"], b["path"])
            self.assertEqual(len([f for f in os.listdir(tmp) if f.endswith(".json")]), 2)

    def test_an_implausible_duration_is_not_a_deadline(self):
        """pi reviewer: a seconds-based archive yields decade-long 'durations'."""
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=0.0, end=90000.0, outcome="admission_timeout")
                for _ in range(3)]
        d = self.m.per_model_deadlines(reqs)
        self.assertEqual(d["glm-5.3"]["status"], "unknown")
        self.assertIsNone(d["glm-5.3"]["deadline_s"])

    def test_message_limit_and_harness_deadline_get_advice(self):
        base = 1_700_000_000.0
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base, end=base + 2.0, outcome="message_limit"),
                self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base, end=base + 1200.0, outcome="harness_deadline")]
        _, adj, _ = self.m.verdict(self.m.analyse(reqs, 1.0))
        joined = " ".join(adj)
        self.assertIn("message-count limit", joined)
        self.assertIn("not concurrency pressure", joined)
        self.assertIn("caller's own deadline", joined)

    def test_filename_carries_the_whole_window(self):
        """codex: two windows sharing a start minute and differing in end date collided."""
        m = {"requests": 0, "by_outcome": {}, "per_model_deadlines": {},
             "timeout_storms": [], "saturation_bands": {}, "unknown_errors": [], "by_run": []}
        base = 1_700_000_000.0
        with tempfile.TemporaryDirectory() as tmp:
            a = self.m.persist_run(m, 24.0, store=tmp, since=base, until=base + 86400)
            b = self.m.persist_run(m, 48.0, store=tmp, since=base, until=base + 2 * 86400)
            c = self.m.persist_run(m, 90.2 / 60, store=tmp, since=base, until=base + 5412)
            d = self.m.persist_run(m, 90.5 / 60, store=tmp, since=base, until=base + 5430)
            self.assertNotEqual(a["path"], b["path"])
            self.assertEqual(len({x["path"] for x in (c, d)}), 2, "close lengths differ too")

    def test_mixed_model_load_is_measured_on_the_lane(self):
        """codex: concurrency per model hid a lane that was actually saturated."""
        base = 1_700_000_000.0
        reqs = []
        for model in ("model-a", "model-b"):
            reqs += [self.m.Request(harness="pi", session="s", model=model,
                                    start=base, end=base + 60.0, outcome="completed")
                     for _ in range(5)]
        b = self.m.saturation_bands(reqs)
        top = [g for g in b["groups"] if g["bands"][-1]["n"]]
        self.assertTrue(top, "ten requests in flight must land in the top band")
        self.assertEqual(top[0]["bands"][-1]["n"], 5)

    def test_flat_experiment_roots_stay_distinct(self):
        """codex: files directly under a root were all '(unattributed)'."""
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            roots = []
            for offset, name in enumerate(("exp-one", "exp-two")):
                d = os.path.join(tmp, name)
                os.makedirs(d)
                start_ms = int((now - 900 - offset) * 1000)
                ts = datetime.datetime.fromtimestamp(now - 800 - offset, datetime.timezone.utc)
                with open(os.path.join(d, "s.jsonl"), "w") as fh:
                    fh.write(_pi_line(ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "m",
                                      start_ms, None) + "\n")
                roots.append(d)
            reqs = self.m.collect(1, ["pi"], pi_roots=roots)
        self.assertEqual(len({r.run for r in reqs}), 2)

    def test_root_order_does_not_change_attribution(self):
        """pi reviewer: the first root listed decided which copy survived."""
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            for offset, store in enumerate(("z-archive", "a-archive")):
                d = os.path.join(tmp, store, "project")
                os.makedirs(d)
                start_ms = int((now - 900 - offset) * 1000)
                ts = datetime.datetime.fromtimestamp(now - 800 - offset, datetime.timezone.utc)
                with open(os.path.join(d, "s.jsonl"), "w") as fh:
                    fh.write(_pi_line(ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "m",
                                      start_ms, None) + "\n")
            roots = [os.path.join(tmp, "z-archive"), os.path.join(tmp, "a-archive")]
            forward = self.m.collect(1, ["pi"], pi_roots=roots)
            backward = self.m.collect(1, ["pi"], pi_roots=list(reversed(roots)))
        self.assertEqual(sorted(r.run for r in forward), sorted(r.run for r in backward))

    def test_unknown_deadline_reports_the_raw_observation_count(self):
        """pi reviewer: five blocks with unusable durations read as 'fewer than 3'."""
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=0.0, end=90000.0, outcome="admission_timeout")
                for _ in range(5)]
        m = self.m.analyse(reqs, 1.0)
        self.assertEqual(m["per_model_deadlines"]["glm-5.3"]["seen_n"], 5)
        _, adj, _ = self.m.verdict(m)
        self.assertIn("5 blocked at admission", " ".join(adj))

    def test_an_implausible_interval_never_reaches_the_minute_bins(self):
        """codex: a seconds-based archive allocated 28 million minute buckets."""
        base = 1_700_000_000.0
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base / 1000.0, end=1.7e9,
                               outcome="admission_timeout") for _ in range(3)]
        m = self.m.analyse(reqs, 1.0)          # must not blow up or hang
        self.assertEqual(m["requests"], 3, "the outcome is still counted")
        self.assertEqual(m["by_minute"], [], "but it is not placed on a timeline")
        self.assertEqual(m["peak_concurrency"], 0)

    def test_incomplete_copy_does_not_beat_the_complete_one(self):
        """codex: an incomplete mirrored copy claimed the response id."""
        import datetime as _dt
        with tempfile.TemporaryDirectory() as tmp:
            now = time.time()
            tss = (_dt.datetime.fromtimestamp(now - 60, _dt.timezone.utc)
                   .strftime("%Y-%m-%dT%H:%M:%S.000Z"))
            late = (_dt.datetime.fromtimestamp(now - 30, _dt.timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%S.000Z"))
            # "a-first" sorts before "z-complete" and holds a partial response
            for name, records in (
                    ("a-first", [{"type": "user", "timestamp": tss},
                                 {"type": "assistant", "timestamp": tss,
                                  "message": {"id": "m-1", "model": "glm-5.3",
                                              "usage": {"input_tokens": 1, "output_tokens": 0}}}]),
                    ("z-complete", [{"type": "user", "timestamp": tss},
                                    {"type": "assistant", "timestamp": late,
                                     "message": {"id": "m-1", "model": "glm-5.3",
                                                 "stop_reason": "end_turn",
                                                 "usage": {"input_tokens": 1,
                                                           "output_tokens": 99}}}])):
                d = os.path.join(tmp, name)
                os.makedirs(d)
                with open(os.path.join(d, "s.jsonl"), "w") as fh:
                    fh.write("\n".join(json.dumps(r) for r in records) + "\n")
            reqs = self.m.collect(1, ["claude-code"], claude_roots=[tmp])
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0].out_tokens, 99, "the complete copy wins")
        self.assertEqual(reqs[0].stop_reason, "end_turn")

    def test_a_single_flat_root_is_named(self):
        """both: one --pi-root holding files directly became '(unattributed)'."""
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            start_ms = int((now - 900) * 1000)
            ts = datetime.datetime.fromtimestamp(now - 800, datetime.timezone.utc)
            with open(os.path.join(tmp, "s.jsonl"), "w") as fh:
                fh.write(_pi_line(ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "m",
                                  start_ms, None) + "\n")
            reqs = self.m.collect(1, ["pi"], pi_roots=[tmp])
            self.assertEqual(len(reqs), 1)
            self.assertEqual(reqs[0].run, f"pi:{os.path.basename(tmp)}",
                             "the run is namespaced by harness and names its root")

    def test_sources_reflect_what_was_read(self):
        """pi reviewer: a typo'd root stayed in the stored identity."""
        code, out, err = run("analyze", "--since", "1h", "--json",
                             "--pi-root", "/nonexistent-luna-doctor-test",
                             "--harness", "pi")
        self.assertIn("is not a directory", err)
        self.assertEqual(json.loads(out)["metrics"]["requests"], 0)

    def test_window_units(self):
        """pi reviewer: `w` and `y` were only covered by the skipped mirror test."""
        self.assertEqual(self.m.window_hours("1w", False), 168.0)
        self.assertEqual(self.m.window_hours("1y", False), 8760.0)
        self.assertEqual(self.m.window_hours("90m", False), 1.5)
        with self.assertRaises(ValueError):
            self.m.window_hours("banana", False)

    def test_per_run_timing_respects_the_sanity_gate(self):
        """both: by_run inflated peaks and wall counts from decade-long intervals."""
        base = 1_700_000_000.0
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3", run="exp-1",
                               start=base / 1000.0, end=1.7e9,
                               outcome="client_idle_timeout") for _ in range(3)]
        m = self.m.analyse(reqs, 1.0)
        run = m["by_run"][0]
        self.assertEqual(run["requests"], 3)
        self.assertEqual(run["peak_concurrency"], 0)
        self.assertEqual(run["wall_counts_s"], {})
        self.assertEqual(run["slow_completions_over_290s"], 0)

    def test_read_nothing_is_not_read_the_default_store(self):
        """both: a rejected --pi-root was labelled as a default-store read."""
        self.assertIn("pi:none", self.m._source_id(["pi"], [], None))
        self.assertIn("pi:default", self.m._source_id(["pi"], None, None))

    def test_runs_do_not_collide_across_harnesses(self):
        """codex: pi/sessions and claude/sessions labelled the same run."""
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            for offset, harness in enumerate(("pi", "claude")):
                d = os.path.join(tmp, harness, "sessions", "project")
                os.makedirs(d)
                start_ms = int((now - 900 - offset) * 1000)
                ts = datetime.datetime.fromtimestamp(now - 800 - offset, datetime.timezone.utc)
                stamp = ts.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                if harness == "pi":
                    body = _pi_line(stamp, "m", start_ms, None)
                else:   # claude-code shape: a user record then an assistant response
                    body = "\n".join([
                        json.dumps({"type": "user", "timestamp": stamp}),
                        json.dumps({"type": "assistant", "timestamp": stamp,
                                    "message": {"id": "m-claude", "model": "glm-5.3",
                                                "stop_reason": "end_turn",
                                                "usage": {"input_tokens": 1, "output_tokens": 2}}})])
                with open(os.path.join(d, "s.jsonl"), "w") as fh:
                    fh.write(body + "\n")
            reqs = self.m.collect(1, ["pi", "claude-code"],
                                  pi_roots=[os.path.join(tmp, "pi", "sessions")],
                                  claude_roots=[os.path.join(tmp, "claude", "sessions")])
        self.assertEqual(len({r.run for r in reqs}), 2)

    def test_cli_respects_no_store_and_store_dir(self):
        """pi reviewer: the persistence gate had no CLI-level coverage."""
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as store:
            start_ms = int((now - 900) * 1000)
            ts = datetime.datetime.fromtimestamp(now - 800, datetime.timezone.utc)
            with open(os.path.join(tmp, "s.jsonl"), "w") as fh:
                fh.write(_pi_line(ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "m",
                                  start_ms, None) + "\n")
            code, _, err = run("analyze", "--since", "1h", "--json", "--pi-root", tmp,
                               "--harness", "pi", "--no-store", "--store-dir", store)
            self.assertIn(code, (0, 1), err)
            self.assertEqual(os.listdir(store), [], "--no-store must write nothing")

            code, _, err = run("analyze", "--since", "1h", "--json", "--pi-root", tmp,
                               "--harness", "pi", "--store-dir", store)
            self.assertIn(code, (0, 1), err)
            names = os.listdir(store)
            self.assertEqual(len([n for n in names if n.endswith(".json")]), 1)
            self.assertIn("findings.jsonl", names)

    def test_zero_length_timeouts_are_not_a_cohort(self):
        """codex: a zero-length inferred interval joined a storm and flipped the exit code."""
        base = 1_700_000_000.0
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base, end=base + 0.5, outcome="request_timeout"),
                self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base + 0.1, end=base + 0.6, outcome="request_timeout"),
                self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base + 0.2, end=base + 0.2, outcome="request_timeout")]
        self.assertEqual(self.m.timeout_storms(reqs), [])

    def test_chart_ignores_implausible_intervals(self):
        """codex: a decade-long interval stretched the chart axis across decades.

        Rendered through the module rather than the CLI: `chart` takes no
        --pi-root, so driving it through subprocess exited 2 and the assertion
        never ran.
        """
        base = 1_700_000_000.0
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base, end=base + 5.0, outcome="completed"),
                self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base / 1000.0, end=1.7e9, outcome="completed")]
        svg = self.m.svg_chart(reqs, self.m.analyse(reqs, 1.0), 1.0)
        self.assertIn("<svg", svg)
        self.assertNotIn("1970", svg[:6000], "the bad interval must not set the axis")

    def test_complementary_copies_merge_into_one_complete_response(self):
        """codex: a start and a terminal record live in different copies."""
        import datetime as _dt
        base = 1_700_000_000.0
        early = _dt.datetime.fromtimestamp(base, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        late = _dt.datetime.fromtimestamp(base + 30, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        with tempfile.TemporaryDirectory() as tmp:
            for name, records in (
                    # user record + fragment: has the start, no terminal data
                    ("a-fragment", [{"type": "user", "timestamp": early},
                                    {"type": "assistant", "timestamp": early,
                                     "message": {"id": "m-1", "model": "glm-5.3",
                                                 "usage": {"input_tokens": 1}}}]),
                    # terminal record only: no preceding record, so no real start
                    ("z-terminal", [{"type": "assistant",
                                     "timestamp": late,
                                     "message": {"id": "m-1", "model": "glm-5.3",
                                                 "stop_reason": "end_turn",
                                                 "usage": {"output_tokens": 99}}}])):
                d = os.path.join(tmp, name)
                os.makedirs(d)
                with open(os.path.join(d, "s.jsonl"), "w") as fh:
                    fh.write("\n".join(json.dumps(r) for r in records) + "\n")
            reqs = self.m.collect(8760 * 100, ["claude-code"], claude_roots=[tmp])
        self.assertEqual(len(reqs), 1)
        r = reqs[0]
        self.assertEqual(r.stop_reason, "end_turn", "terminal metadata survives")
        self.assertEqual(r.out_tokens, 99, "usage survives")
        self.assertIsNotNone(r.start)
        self.assertGreater(r.end - r.start, 0, "the start from the other copy survives")

    def test_a_fragment_before_the_window_still_supplies_the_start(self):
        """both: filtering before merging dropped the only copy with a real start."""
        import datetime as _dt
        base = 1_700_000_000.0
        early = _dt.datetime.fromtimestamp(base, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        frag = _dt.datetime.fromtimestamp(base + 5, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        late = _dt.datetime.fromtimestamp(base + 30, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        with tempfile.TemporaryDirectory() as tmp:
            for name, records in (
                    ("a-fragment", [{"type": "user", "timestamp": early},
                                    {"type": "assistant", "timestamp": frag,
                                     "message": {"id": "m-1", "model": "glm-5.3"}}]),
                    ("z-terminal", [{"type": "assistant", "timestamp": late,
                                     "message": {"id": "m-1", "model": "glm-5.3",
                                                 "stop_reason": "end_turn",
                                                 "usage": {"output_tokens": 5}}}])):
                d = os.path.join(tmp, name)
                os.makedirs(d)
                with open(os.path.join(d, "s.jsonl"), "w") as fh:
                    fh.write("\n".join(json.dumps(r) for r in records) + "\n")
            # window opens after the fragment's last record, before the terminal one
            reqs = self.m.iter_claude([tmp], base + 10, base + 3600)
        self.assertEqual(len(reqs), 1)
        self.assertIsNotNone(reqs[0].start)
        self.assertGreater(reqs[0].end - reqs[0].start, 0, "the pre-window start survives")
        self.assertIn(reqs[0], self.m.timed(reqs), "and the request stays timed")

    def test_the_start_survives_whichever_copy_is_read_first(self):
        """both: applying pre-window starts per file made it order-dependent."""
        import datetime as _dt
        base = 1_700_000_000.0
        early = _dt.datetime.fromtimestamp(base, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        frag = _dt.datetime.fromtimestamp(base + 5, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        late = _dt.datetime.fromtimestamp(base + 30, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        for terminal_name, fragment_name in (("a-terminal", "z-fragment"),
                                             ("z-terminal", "a-fragment")):
            with self.subTest(order=terminal_name):
                with tempfile.TemporaryDirectory() as tmp:
                    for name, records in (
                            (fragment_name, [{"type": "user", "timestamp": early},
                                             {"type": "assistant", "timestamp": frag,
                                              "message": {"id": "m-1", "model": "glm-5.3"}}]),
                            (terminal_name, [{"type": "assistant", "timestamp": late,
                                              "message": {"id": "m-1", "model": "glm-5.3",
                                                          "stop_reason": "end_turn",
                                                          "usage": {"output_tokens": 5}}}])):
                        d = os.path.join(tmp, name)
                        os.makedirs(d)
                        with open(os.path.join(d, "s.jsonl"), "w") as fh:
                            fh.write("\n".join(json.dumps(r) for r in records) + "\n")
                    reqs = self.m.iter_claude([tmp], base + 10, base + 3600)
                self.assertEqual(len(reqs), 1)
                self.assertGreater(reqs[0].end - reqs[0].start, 0,
                                   "the pre-window start must not depend on file order")

    def test_chart_and_fix_read_explicit_roots(self):
        """pi reviewer: the flags existed only on analyze, so a mirror user had no chart."""
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as out:
            start_ms = int((now - 900) * 1000)
            ts = datetime.datetime.fromtimestamp(now - 800, datetime.timezone.utc)
            with open(os.path.join(tmp, "s.jsonl"), "w") as fh:
                fh.write(_pi_line(ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "m",
                                  start_ms, None) + "\n")
            svg = os.path.join(out, "c.svg")
            code, _, err = run("chart", "--since", "1h", "--pi-root", tmp,
                               "--harness", "pi", "--out", svg)
            self.assertIn(code, (0, 1), err)
            self.assertTrue(os.path.exists(svg))
            code, _, err = run("fix", "--since", "1h", "--pi-root", tmp, "--harness", "pi")
            self.assertIn(code, (0, 1), err)

    def test_two_writers_do_not_share_a_temp_path(self):
        """both: concurrent runs of the same window collided on `<path>.tmp`."""
        m = {"requests": 0, "by_outcome": {}, "per_model_deadlines": {},
             "timeout_storms": [], "saturation_bands": {}, "unknown_errors": [], "by_run": []}
        now = 1_700_000_000.0
        with tempfile.TemporaryDirectory() as tmp:
            self.m.persist_run(m, 1.0, store=tmp, since=now - 3600, until=now)
            leftovers = [f for f in os.listdir(tmp) if f.startswith(".tmp-")]
        self.assertEqual(leftovers, [], "no temp file survives a completed write")


class FourthReviewRegressions(unittest.TestCase):
    """Iteration 4 findings."""

    def setUp(self):
        self.m = _load_cli()

    def test_disjoint_windows_with_equal_counts_are_two_occurrences(self):
        """both: event findings that share a count but not a time must both record."""
        day = 86400.0
        base = 1_700_000_000.0

        def window(t0):
            return self.m.analyse([self.m.Request(harness="pi", session="s", model="glm-5.3",
                                                  start=t0 + i, end=t0 + i + 300.0,
                                                  outcome="client_idle_timeout",
                                                  error_message="Stream ended without finish_reason")
                                   for i in range(3)], 24.0)
        a, b = window(base), window(base + day)
        with tempfile.TemporaryDirectory() as tmp:
            self.m.persist_run(a, 24.0, store=tmp, since=1_699_900_000.0, until=1_700_000_120.0)
            self.m.persist_run(b, 24.0, store=tmp,
                               since=1_699_900_000.0 + day, until=1_700_000_120.0 + day)
            with open(os.path.join(tmp, "findings.jsonl")) as fh:
                rows = [json.loads(x) for x in fh if x.strip()]
        # outcome rows are window summaries and stay out of the ledger; the two
        # storms are a day apart and must both be recorded
        self.assertEqual([r for r in rows if r["kind"] == "outcome"], [])
        storms = [r for r in rows if r["kind"] == "timeout_storm"]
        self.assertEqual(len(storms), 2, "two occurrences a day apart, not one")
        self.assertNotEqual(storms[0]["event_start"], storms[1]["event_start"])

    def test_a_bare_404_is_not_a_stale_model(self):
        """both: any 404 got 'refresh your model list' advice and lost its unknown-error sighting."""
        self.assertEqual(self.m.classify({"error_message": "404: Not Found",
                                          "stop_reason": "error"}), "other_error")
        self.assertNotEqual(self.m.classify({"error_message": '404 {"message":"endpoint does not exist"}',
                                             "stop_reason": "error"}), "model_not_found")

    def test_model_404_still_classifies(self):
        patterns = [
            "404: {\"message\":\"The model 'glm-5.3-vision' does not exist\",\"param\":\"model\"}",
            '{"code":"model_not_found","message":"nope"}',
        ]
        for err in patterns:
            with self.subTest(err=err):
                self.assertEqual(self.m.classify({"error_message": err, "stop_reason": "error"}),
                                 "model_not_found")

    def test_glob_metacharacters_in_a_root_are_literal(self):
        """codex: a directory named [backup] was read as a pattern."""
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "[backup]")
            os.makedirs(d)
            start_ms = int((now - 900) * 1000)
            ts = datetime.datetime.fromtimestamp(now - 800, datetime.timezone.utc)
            with open(os.path.join(d, "s.jsonl"), "w") as fh:
                fh.write(_pi_line(ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "m", start_ms, None) + "\n")
            reqs = self.m.collect(1, ["pi"], pi_roots=[d])
        self.assertEqual(len(reqs), 1)


class FifthReviewRegressions(unittest.TestCase):
    """Iteration 5 findings."""

    def setUp(self):
        self.m = _load_cli()

    def test_unknown_error_does_not_claim_concurrency_pressure(self):
        """claude-code: one unclassified string flipped the exit code to 'pressure'."""
        base = 1_700_000_000.0
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base, end=base + 5.0, outcome="other_error",
                               error_message="some new gateway string")]
        head, adj, code = self.m.verdict(self.m.analyse(reqs, 1.0))
        self.assertNotIn("Concurrency pressure", head)
        self.assertTrue(any("matched no known class" in a for a in adj))

    def test_route_404_is_not_a_missing_model(self):
        """claude-code: `/v1/models does not exist` matched the loose word clause."""
        for err in ("404: endpoint /v1/models does not exist",
                    "Route /v1/models not found"):
            with self.subTest(err=err):
                self.assertNotEqual(
                    self.m.classify({"error_message": err, "stop_reason": "error"}),
                    "model_not_found")

    def test_report_filename_distinguishes_close_window_lengths(self):
        """codex: 90.2m and 90.5m collided on one file and one was discarded."""
        m = {"requests": 0, "by_outcome": {}, "per_model_deadlines": {},
             "timeout_storms": [], "saturation_bands": {}, "unknown_errors": [], "by_run": []}
        now = 1_700_000_000.0
        with tempfile.TemporaryDirectory() as tmp:
            a = self.m.persist_run(m, 90.2 / 60, store=tmp, since=now - 5412, until=now)
            b = self.m.persist_run(m, 90.5 / 60, store=tmp, since=now - 5430, until=now)
        self.assertNotEqual(a["path"], b["path"])

    def test_duration_clustering_does_not_chain(self):
        """codex: 60s and 61.2s joined two cohorts through intermediate samples."""
        base = 1_700_000_000.0
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base, end=base + d, outcome="request_timeout")
                for d in (60.0, 60.6, 61.2)]
        self.assertEqual(self.m.timeout_storms(reqs), [],
                         "tolerance is measured against the cohort, not the previous sample")

    def test_runs_are_root_qualified(self):
        """codex: archive-a/project and archive-b/project collapsed into one run."""
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            for offset, store in enumerate(("archive-a", "archive-b")):
                d = os.path.join(tmp, store, "project")
                os.makedirs(d)
                # distinct response ids: the parser dedupes on them across roots
                start_ms = int((now - 900 - offset) * 1000)
                ts = datetime.datetime.fromtimestamp(now - 800 - offset, datetime.timezone.utc)
                with open(os.path.join(d, "s.jsonl"), "w") as fh:
                    fh.write(_pi_line(ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "m",
                                      start_ms, None) + "\n")
            reqs = self.m.collect(1, ["pi"], pi_roots=[os.path.join(tmp, "archive-a"),
                                                      os.path.join(tmp, "archive-b")])
        self.assertEqual(len({r.run for r in reqs}), 2)

    def test_model_without_usable_durations_still_reports_unknown(self):
        """codex: admission failures with no start timestamp vanished entirely."""
        reqs = [self.m.Request(harness="pi", session="s", model="mystery",
                               start=None, end=1_700_000_100.0, outcome="admission_timeout")]
        d = self.m.per_model_deadlines(reqs)
        self.assertEqual(d["mystery"]["status"], "unknown")
        self.assertEqual(d["mystery"]["n"], 0)


class SixthReviewRegressions(unittest.TestCase):
    """Iteration 6 findings."""

    def setUp(self):
        self.m = _load_cli()

    def test_a_stranger_elsewhere_cannot_split_a_pack(self):
        """codex: 299.4s hours earlier partitioned the 300.x pack away from its peers."""
        base = 1_700_000_000.0
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base - 7200, end=base - 7200 + 299.4,
                               outcome="request_timeout"),
                self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base, end=base + 300.2, outcome="request_timeout"),
                self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base + 0.3, end=base + 300.5, outcome="request_timeout"),
                self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base + 0.4, end=base + 300.6, outcome="request_timeout")]
        storms = self.m.timeout_storms(reqs)
        self.assertEqual([s["cohort"] for s in storms], [3])

    def test_serial_timeouts_are_not_a_pack(self):
        """codex: three serial 5s timeouts were called a synchronous cohort."""
        base = 1_700_000_000.0
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base + off, end=base + off + 5.0,
                               outcome="request_timeout")
                for off in (0, 10, 20)]
        self.assertEqual(self.m.timeout_storms(reqs), [],
                         "peak concurrency of one is a slow client, not a fan-out")

    def test_roots_sharing_a_basename_stay_distinct(self):
        """codex: /archive-a/pi and /archive-b/pi both reduced to pi/project."""
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            for offset, store in enumerate(("archive-a", "archive-b")):
                d = os.path.join(tmp, store, "pi", "project")
                os.makedirs(d)
                start_ms = int((now - 900 - offset) * 1000)
                ts = datetime.datetime.fromtimestamp(now - 800 - offset, datetime.timezone.utc)
                with open(os.path.join(d, "s.jsonl"), "w") as fh:
                    fh.write(_pi_line(ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "m",
                                      start_ms, None) + "\n")
            reqs = self.m.collect(1, ["pi"], pi_roots=[
                os.path.join(tmp, "archive-a", "pi"),
                os.path.join(tmp, "archive-b", "pi")])
        self.assertEqual(len({r.run for r in reqs}), 2)


class SeventhReviewRegressions(unittest.TestCase):
    """Iteration 7 findings, all in the cohort scanner."""

    def setUp(self):
        self.m = _load_cli()

    def _r(self, end_off, dur, model="glm-5.3", oc="admission_timeout"):
        base = 1_700_000_000.0
        return self.m.Request(harness="pi", session="s", model=model,
                              start=base + end_off - dur, end=base + end_off, outcome=oc)

    def test_interleaved_packs_are_both_reported(self):
        """both: emitting one cohort consumed members of the other, interleaved one."""
        slow = [self._r(off, 120.2, "glm-5.3") for off in (300, 310, 320)]
        fast = [self._r(off, 60.3, "deepseek-4.1-flash") for off in (305, 315, 325)]
        storms = self.m.timeout_storms(slow + fast)
        self.assertEqual(sorted(s["cohort"] for s in storms), [3, 3])
        self.assertEqual(len({s["duration_s"] for s in storms}), 2)

    def test_equal_valued_requests_terminate(self):
        """both: list.index on equal Requests could stall the scan forever."""
        base = 1_700_000_000.0
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base, end=base + 300.0, outcome="request_timeout")
                for _ in range(4)]
        storms = self.m.timeout_storms(reqs)      # must return, not spin
        self.assertEqual(len(storms), 1)
        self.assertEqual(storms[0]["cohort"], 4)

    def test_a_concurrent_pack_is_not_shadowed_by_a_larger_serial_cluster(self):
        """codex: the biggest cluster won on size, then failed the overlap guard."""
        concurrent = [self._r(off, 10.0, oc="request_timeout") for off in (0, 1, 2)]
        serial = [self._r(off, 5.0, oc="request_timeout") for off in (20, 30, 40, 50)]
        storms = self.m.timeout_storms(concurrent + serial)
        self.assertEqual([s["cohort"] for s in storms], [3])
        self.assertAlmostEqual(storms[0]["duration_s"], 10.0)


class ReportModel(unittest.TestCase):
    def test_render_is_built_from_a_report_object(self):
        m = _load_cli()
        reqs = [m.Request(harness="pi", session="s", model="glm-5.3",
                          start=1_700_000_000.0, end=1_700_000_030.0, outcome="completed")]
        metrics = m.analyse(reqs, window_hours=24)
        rep = m.build_report(metrics, "No pressure in this window.", ["Narrow to 8."])
        from luna_doctor.report import Report
        self.assertIsInstance(rep, Report)
        self.assertEqual(rep.headline, "No pressure in this window.")
        labels = [row.label for s in rep.sections for row in s.rows]
        self.assertIn("requests", labels)
        self.assertIn("peak concurrency", labels)

    def test_zero_requests_yields_no_nan_or_none(self):
        m = _load_cli()
        metrics = m.analyse([], window_hours=24)
        rep = m.build_report(metrics, "No data.", [])
        from luna_doctor.report import render_plain
        out = render_plain(rep)
        self.assertNotIn("nan", out.lower())
        self.assertNotIn("None", out)


class OtherSurfacesUseReports(unittest.TestCase):
    def test_discover_report_has_a_row_per_store(self):
        m = _load_cli()
        rep = m.build_discover_report({"stores": [{"name": "pi", "path": "/x", "exists": True,
                                                   "files": 3, "bytes": 10, "newest": None}]})
        self.assertEqual(len(rep.sections), 1)
        self.assertTrue(any(r.label == "pi" for r in rep.sections[0].rows))

    def test_probe_report_keeps_the_admission_line(self):
        m = _load_cli()
        # A run_probe() result: wait_headers/ttfb are the p50 floats pct() returns.
        r = {"n": 8, "tiers": {"interactive": 8, "background": 0}, "demoted": 0,
             "streamed": 8, "silent": 0, "admission_deaths": 0, "errors": 0,
             "keepalives": 0, "wait_headers": 25.2, "ttfb": 30.0, "cut": 0, "budget_s": 170}
        rep = m.build_probe_report(r, 8, "glm-5.3")
        from luna_doctor.report import render_plain
        out = render_plain(rep)
        self.assertIn("glm-5.3", out)
        self.assertIn("blocked at admission 0", out)


class StyleAndJson(unittest.TestCase):
    def test_json_wins_over_style(self):
        code, out, _ = run("analyze", "--since", "1h", "--json", "--style", "pretty")
        self.assertIn(code, (0, 1, 2))
        self.assertNotIn("\033", out)
        json.loads(out) if out.strip() else None

    def test_plain_style_has_no_escapes(self):
        code, out, _ = run("analyze", "--since", "1h", "--style", "plain")
        self.assertIn(code, (0, 1, 2))
        self.assertNotIn("\033", out)
        self.assertNotIn("🌙", out)

    def test_no_color_leaves_no_escapes_even_when_pretty_is_explicit(self):
        env = dict(os.environ)
        env["NO_COLOR"] = "1"
        proc = subprocess.run(
            [sys.executable, CLI, "analyze", "--since", "1h", "--style", "pretty"],
            capture_output=True, text=True, timeout=180, cwd=ROOT,
            env={**env, "LUNA_DOCTOR_STORE": _TEST_STORE})
        self.assertIn(proc.returncode, (0, 1, 2))
        self.assertNotIn("\033", proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)

    def test_share_degrades_to_plain_with_a_note_on_stderr(self):
        code, out, err = run("analyze", "--since", "1h", "--style", "share")
        self.assertIn(code, (0, 1, 2))
        self.assertEqual(len([l for l in err.splitlines() if "share" in l]), 1)
        self.assertNotIn("\033", out)
        self.assertNotIn("🌙", out)

    def test_non_utf8_stdout_degrades_instead_of_raising(self):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "cp1252"
        proc = subprocess.run(
            [sys.executable, CLI, "analyze", "--since", "1h", "--style", "pretty"],
            capture_output=True, timeout=180, cwd=ROOT,
            env={**env, "LUNA_DOCTOR_STORE": _TEST_STORE})
        self.assertIn(proc.returncode, (0, 1, 2))
        self.assertNotIn(b"Traceback", proc.stderr)
        proc.stdout.decode("cp1252")            # must not raise
        self.assertNotIn("🌙".encode("utf-8"), proc.stdout)

    def test_ascii_stdout_still_prints_a_report(self):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "ascii"
        proc = subprocess.run(
            [sys.executable, CLI, "discover"],
            capture_output=True, timeout=60, cwd=ROOT,
            env={**env, "LUNA_DOCTOR_STORE": _TEST_STORE})
        self.assertIn(proc.returncode, (0, 2))
        self.assertNotIn(b"Traceback", proc.stderr)
        proc.stdout.decode("ascii")              # must not raise


class CopyToClipboard(unittest.TestCase):
    """`--copy` puts the printed artifact on the clipboard via the first tool
    on PATH (pbcopy, wl-copy, xclip, xsel), as plain text, with stdout
    unchanged - so a machine with no clipboard tool loses nothing."""

    FALLBACK = "no clipboard tool found (pbcopy, wl-copy, xclip, xsel); printed instead"
    COPIED = re.compile(r"^copied (\d+) bytes to the clipboard via pbcopy$", re.M)

    def _fake_clipboard(self):
        """A PATH with a fake pbcopy first; it writes stdin to a file we read back."""
        clip = os.path.join(tempfile.mkdtemp(prefix="luna-doctor-clip-"), "clipboard.txt")
        bindir = tempfile.mkdtemp(prefix="luna-doctor-bin-")
        tool = os.path.join(bindir, "pbcopy")
        with open(tool, "w") as fh:
            fh.write("#!/usr/bin/env python3\n"
                     "import os, sys\n"
                     "open(os.environ['LUNA_FAKE_CLIPBOARD'], 'w').write(sys.stdin.read())\n")
        os.chmod(tool, 0o755)
        env = dict(os.environ)
        env["LUNA_DOCTOR_STORE"] = _TEST_STORE
        env["LUNA_FAKE_CLIPBOARD"] = clip
        # Prepended, so the fake wins over any real pbcopy further down PATH.
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
        return env, clip

    def _run(self, env, *args):
        return subprocess.run([sys.executable, CLI, *args], capture_output=True,
                              text=True, timeout=180, cwd=ROOT, env=env)

    def test_analyze_plain_copy_equals_stdout(self):
        env, clip = self._fake_clipboard()
        proc = self._run(env, "analyze", "--since", "24h", "--style", "plain", "--copy")
        self.assertIn(proc.returncode, (0, 1, 2))
        with open(clip) as fh:
            self.assertEqual(fh.read(), proc.stdout)
        m = self.COPIED.search(proc.stderr)
        self.assertIsNotNone(m, proc.stderr)
        self.assertEqual(int(m.group(1)), len(proc.stdout.encode("utf-8")))

    def test_fix_copy_copies_what_it_prints(self):
        env, clip = self._fake_clipboard()
        proc = self._run(env, "fix", "--copy")
        self.assertIn(proc.returncode, (0, 1))
        with open(clip) as fh:
            copied = fh.read()
        self.assertEqual(copied, proc.stdout)
        if copied:
            # verbatim, with nothing added at either end
            self.assertFalse(copied[:1].isspace())
        if "apply (copy these lines exactly):" in proc.stdout:
            # the apply lines ride along untouched, including the jq tail
            self.assertIn("apply (copy these lines exactly):", copied)
            self.assertIn("jq -e . \"$f\" >/dev/null && echo ok", copied)

    def test_copy_stays_plain_even_with_style_pretty(self):
        env, clip = self._fake_clipboard()
        proc = self._run(env, "analyze", "--since", "24h", "--style", "pretty", "--copy")
        self.assertIn(proc.returncode, (0, 1, 2))
        with open(clip) as fh:
            copied = fh.read()
        self.assertNotIn("\033[", copied)
        self.assertEqual(copied, proc.stdout)   # the copy is exactly what printed

    def test_no_clipboard_tool_prints_fallback_and_keeps_the_answer(self):
        # A PATH-like env with a python3 but no clipboard tool on it.
        bindir = tempfile.mkdtemp(prefix="luna-doctor-empty-")
        os.symlink(sys.executable, os.path.join(bindir, "python3"))
        env = {"PATH": bindir, "HOME": os.environ.get("HOME", ""),
               "LUNA_DOCTOR_STORE": _TEST_STORE,
               "NO_COLOR": "1"}
        proc = self._run(env, "analyze", "--since", "24h", "--style", "plain", "--copy")
        self.assertIn(proc.returncode, (0, 1, 2))
        self.assertIn(self.FALLBACK, proc.stderr)
        self.assertTrue(proc.stdout)          # a failed copy is never a lost answer
        self.assertNotIn("copied ", proc.stderr)

    def test_json_copy_is_the_exact_document(self):
        env, clip = self._fake_clipboard()
        proc = self._run(env, "analyze", "--since", "24h", "--json", "--copy")
        self.assertIn(proc.returncode, (0, 1, 2))
        with open(clip) as fh:
            copied = fh.read()
        self.assertEqual(copied, proc.stdout)
        doc = json.loads(copied)               # the whole document, nothing more
        self.assertEqual(doc["command"], "analyze")
        self.assertEqual(doc["schema"], 4)


class DemotionReason(unittest.TestCase):
    def test_no_background_entitlement_is_priority_demotion(self):
        m = _load_cli()
        tier, demoted, reason = m.tier_of("glm-5.3-background", "no-background-entitlement")
        self.assertEqual((tier, demoted, reason), ("interactive", True, "no_entitlement"))

    def test_overflow_still_reads_as_background(self):
        m = _load_cli()
        self.assertEqual(m.tier_of("glm-5.3", "overflowed-to-background"),
                         ("background", True, "overflow"))

    def test_plain_background_model_is_unremarkable(self):
        m = _load_cli()
        self.assertEqual(m.tier_of("glm-5.3-background"), ("background", False, ""))

    def test_analyze_does_not_claim_to_know_the_served_tier(self):
        code, out, _ = run("analyze", "--since", "24h")
        self.assertIn("served tier is not in these logs", out)

    def test_probe_report_splits_demoted_by_reason(self):
        m = _load_cli()
        r = {"n": 2, "tiers": {"interactive": 0, "background": 2}, "demoted": 2,
             "streamed": 2, "silent": 0, "admission_deaths": 0, "errors": 0,
             "keepalives": 0, "wait_headers": 1.0, "ttfb": 2.0, "cut": 0, "budget_s": 170,
             "direct": [{"reason": "overflow"}, {"reason": "no_entitlement"}]}
        rep = m.build_probe_report(r, 2, "glm-5.3-background")
        from luna_doctor.report import render_plain
        out = render_plain(rep)
        self.assertIn("demoted 2", out)
        self.assertIn("overflow 1", out)
        self.assertIn("no entitlement 1", out)
        self.assertIn("no-background-entitlement", out)
        self.assertIn("priority concurrency", out)

    def test_probe_report_without_reasons_keeps_the_old_row(self):
        m = _load_cli()
        r = {"n": 8, "tiers": {"interactive": 8, "background": 0}, "demoted": 0,
             "streamed": 8, "silent": 0, "admission_deaths": 0, "errors": 0,
             "keepalives": 0, "wait_headers": 25.2, "ttfb": 30.0, "cut": 0, "budget_s": 170}
        from luna_doctor.report import render_plain
        out = render_plain(m.build_probe_report(r, 8, "glm-5.3"))
        self.assertIn("demoted 0", out)
        self.assertNotIn("overflow 1", out)
        self.assertNotIn("no entitlement", out)

    def test_demotion_note_names_the_priority_pool_when_observed(self):
        m = _load_cli()
        note = m.demotion_note("no_entitlement", "glm-5.3-background")
        self.assertIn("glm-5.3-background", note)
        self.assertIn("priority concurrency", note)
        self.assertIn("no background entitlement", note)

    def test_demotion_note_hedges_when_no_probe_has_spoken(self):
        m = _load_cli()
        note = m.demotion_note("", "glm-5.3-background")
        self.assertIn("may be running on priority concurrency", note)
        self.assertIn("only a probe can see", note.lower())


class SaturatedVocabulary(unittest.TestCase):
    def test_stream_stalled_is_not_a_promised_outcome(self):
        m = _load_cli()
        r = m.Request(harness="pi", session="s", model="m", outcome="stream_stalled")
        self.assertFalse(r.saturated)
        r2 = m.Request(harness="pi", session="s", model="m", outcome="client_idle_timeout")
        self.assertTrue(r2.saturated)


if __name__ == "__main__":
    unittest.main()
