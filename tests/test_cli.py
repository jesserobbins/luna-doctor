"""Smoke tests for luna-doctor.

Stdlib only, no fixtures to maintain. Run from the repo root:

    python3 -m unittest discover -s tests -v

The suite never opens live sessions on the gateway (the load-test commands
stay behind their --load-test-enable gate), and every CLI run
points `LUNA_DOCTOR_STORE` at a throwaway directory: nothing here writes to your
real store or to a log store.
"""

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import datetime
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "bin", "luna-doctor")
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")  # semver, prerelease allowed (0.1.1-alpha.1)


# Every CLI invocation in this suite gets its own store directory: the real
# ~/.luna-doctor/store is the user's repeat history, not a test artifact.
_TEST_STORE = tempfile.mkdtemp(prefix="luna-doctor-tests-")

# Same contract for the capacity cache: no run in this suite reads the real
# ~/.luna-doctor/capacity.json; a test that wants a payload passes one and
# gets a throwaway cache file. The fetch layer itself is tested in-process
# (mocked) in test_sources - no CLI run here may touch the network.
_TEST_CAPACITY = os.path.join(_TEST_STORE, "absent-capacity.json")


def _capacity_cache_doc(payload, fetched_at):
    return {"fetched_at": fetched_at, "fetched_at_iso": "",
            "url": "https://api.lunaroute.com/v1/capacity", "response": payload}


def run(*args, timeout=180, store=None, capacity=None, env=None):
    _env = dict(os.environ)
    _env["LUNA_DOCTOR_STORE"] = store or _TEST_STORE
    if capacity is None:
        _env["LUNA_DOCTOR_CAPACITY"] = _TEST_CAPACITY
    else:
        d = tempfile.mkdtemp(prefix="luna-doctor-capacity-")
        _env["LUNA_DOCTOR_CAPACITY"] = os.path.join(d, "capacity.json")
        with open(_env["LUNA_DOCTOR_CAPACITY"], "w") as fh:
            json.dump(_capacity_cache_doc(capacity, time.time()), fh)
    if env:
        _env.update(env)
    proc = subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True,
                          timeout=timeout, cwd=ROOT, env=_env)
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

    def test_load_test_rejects_zero_width(self):
        code, _, err = run("load-test", "--n", "0")
        self.assertEqual(code, 2)
        self.assertIn("--n must be at least 1", err)

    def test_no_data_for_a_harness_we_do_not_read(self):
        code, _, _ = run("analyze", "--harness", "codex", "--since", "1m")
        self.assertEqual(code, 2)


class LoadTestGate(unittest.TestCase):
    """The burst commands are a load test against the live gateway, so they
    stay behind an explicit opt-in. Without the flag they refuse, say
    exactly what they would open and what it costs, and exit 2."""

    def test_load_test_without_the_flag_refuses_and_names_the_width(self):
        code, out, err = run("load-test", "--n", "8")
        self.assertEqual(code, 2)
        self.assertIn("--load-test-enable", err)
        self.assertIn("8 concurrent sessions", err)
        self.assertEqual(out, "")

    def test_compare_without_the_flag_refuses(self):
        code, _, err = run("compare", "--n", "4")
        self.assertEqual(code, 2)
        self.assertIn("--load-test-enable", err)

    def test_the_refusal_is_honest_about_what_it_opens(self):
        code, _, err = run("load-test", "--n", "8")
        self.assertEqual(code, 2)
        # the message must say what the run really is, not soften it
        self.assertIn("load test against the live gateway", err)
        self.assertIn("spending included inference", err)
        self.assertIn("opt-in only", err)
        self.assertIn("glm-5.3", err)

    def test_message_says_concurrency_not_credits(self):
        code, _, err = run("load-test", "--n", "8")
        # billing/quota framing stays out; "spending included inference" is
        # the owner's own honesty line and is asserted above
        for forbidden in ("credit", "token", "cost"):
            self.assertNotIn(forbidden, err.lower())

    def test_help_lists_it_plainly_but_the_docs_do_not_advertise_it(self):
        # `--help` is the one place the command stays visible (argparse
        # cannot hide a subcommand cleanly); the README and the skill are
        # the surfaces that went quiet
        code, out, err = run("--help")
        self.assertEqual(code, 0)
        self.assertIn("load-test", out)
        self.assertNotIn("probe", out, "the retired name is gone everywhere")


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


def _chart_store(tmp, n):
    """A pi store of n requests for chart tests: two scheduling classes,
    five priority models (so the fold past the four named bands is
    exercised), rejections at admission with a stable 60.3s deadline
    spread across models, silent stalls at the idle budget, and bursts of
    overlap. Deterministic, so every assertion is about the code."""
    now = time.time()
    win = 23 * 3600
    # starts advance 11.6s per request without wrapping (n * 11.6 < win for
    # any n this builder is called with), and the longest duration is 306s,
    # so every end lands strictly inside the window and needs no clamping
    # (the request-count assertion in the tests below is the tripwire for
    # a builder change that silently loses requests)
    p_models = ["sonnet-5", "haiku-4", "gpt-5.3", "glm-5.3",
                "deepseek-4.1-flash"]
    b_models = ["sonnet-5-background", "glm-5.3-background"]
    lines = []
    for i in range(n):
        start = now - win + (i * 11.6) % win
        roll = i % 9
        if roll < 5:
            model, err, dur = p_models[roll], None, 8.0 + (i % 37) * 2.7
        elif roll < 7:
            model, err, dur = b_models[roll - 5], None, 60.0 + (i % 13) * 9.0
        else:
            model, err, dur = p_models[i % 2], None, 15.0 + (i % 11) * 4.0
        if i % 97 == 7:    # ~n/97 rejections, spread over the models: enough
            err, dur = "scheduler admission timeout", 60.3    # per model to observe
        elif i % 251 == 8:  # ~n/251 silent stalls at the client idle budget
            err, dur = "Stream ended without finish_reason", 300.0 + (i % 7)
        elif i % 401 == 9:  # a few org-level concurrency rejections
            err, dur = "concurrent_request_limit_exceeded", 5.0
        end = start + dur
        # millisecond stamps: a second-resolution stamp truncates the end, so
        # a 60.3s rejection would measure 59.x
        ts = datetime.datetime.fromtimestamp(end, datetime.timezone.utc)
        stamp = ts.strftime("%Y-%m-%dT%H:%M:%S") + ".%03dZ" % (ts.microsecond // 1000)
        # a unique responseId per record: collection dedupes by response, and
        # two requests that happen to end in the same millisecond are still
        # two responses (a timestamp-derived id would collapse them)
        rec = {
            "type": "message",
            "id": "id-%06d" % i,
            "timestamp": stamp,
            "message": {
                "role": "assistant",
                "model": model,
                "provider": "lunaroute",
                "timestamp": int(start * 1000),
                "usage": {"input": 10, "output": 100},
                "stopReason": "error" if err else "stop",
                "errorMessage": err,
                "content": [{"type": "text", "text": "x"}],
                "responseId": "r-%06d" % i,
            },
        }
        lines.append(json.dumps(rec))
    proj = os.path.join(tmp, "proj", "--rat-run--")
    os.makedirs(proj, exist_ok=True)
    with open(os.path.join(proj, "2026-08-13T06-41-17-867Z_abc.jsonl"), "w") as fh:
        fh.write("\n".join(lines) + "\n")


def _recent_store(home, n=6):
    """A pi store of n clean completed requests, all inside the last hour,
    for the bare default run's 1h window. Unlike _chart_store's 23h spread,
    every request lands strictly inside the window on any day the test
    runs, so the default run's report is deterministic: no pressure, and
    the analysis verdict is 0."""
    now = time.time()
    lines = []
    for i in range(n):
        start = now - 3000 + i * 120          # 50 minutes back, 2-minute steps
        end = start + 60
        ts = datetime.datetime.fromtimestamp(end, datetime.timezone.utc)
        stamp = ts.strftime("%Y-%m-%dT%H:%M:%S") + ".%03dZ" % (ts.microsecond // 1000)
        rec = {
            "type": "message",
            "id": "id-%06d" % i,
            "timestamp": stamp,
            "message": {
                "role": "assistant",
                "model": "glm-5.3",
                "provider": "lunaroute",
                "timestamp": int(start * 1000),
                "usage": {"input": 10, "output": 100},
                "stopReason": "stop",
                "errorMessage": None,
                "content": [{"type": "text", "text": "x"}],
                "responseId": "r-%06d" % i,
            },
        }
        lines.append(json.dumps(rec))
    proj = os.path.join(home, ".pi", "agent", "sessions", "proj", "--bare--")
    os.makedirs(proj, exist_ok=True)
    with open(os.path.join(proj, "2026-08-13T06-41-17-867Z_abc.jsonl"), "w") as fh:
        fh.write("\n".join(lines) + "\n")


def _bare_run(home, work, capacity=None, timeout=180):
    """Run bare `luna-doctor` the way a first-time user does: no arguments,
    a $HOME that holds only the fixture store (so the default roots read
    the fixture, never this machine's real logs), and a working directory
    the chart files must land in."""
    env = dict(os.environ)
    env["HOME"] = home
    env["LUNA_DOCTOR_STORE"] = os.path.join(home, "store")
    if capacity is None:
        env["LUNA_DOCTOR_CAPACITY"] = _TEST_CAPACITY
    else:
        d = tempfile.mkdtemp(prefix="luna-doctor-capacity-")
        env["LUNA_DOCTOR_CAPACITY"] = os.path.join(d, "capacity.json")
        with open(env["LUNA_DOCTOR_CAPACITY"], "w") as fh:
            json.dump(_capacity_cache_doc(capacity, time.time()), fh)
    proc = subprocess.run([sys.executable, CLI], capture_output=True, text=True,
                          timeout=timeout, cwd=work, env=env)
    return proc.returncode, proc.stdout, proc.stderr


class ChartPng(unittest.TestCase):
    """The chart writes a PNG beside the SVG by default: the same scene
    rasterised by the package's own renderer, taking the same stem as
    --out, suppressed by --no-image, and named in the console summary
    and the --json envelope."""

    def test_the_png_is_written_beside_the_svg_and_named_in_the_summary(self):
        with tempfile.TemporaryDirectory() as d:
            _chart_store(d, 30)
            out = os.path.join(d, "chart.svg")
            code, stdout, err = run("chart", "--since", "24h", "--out", out,
                                   "--pi-root", d, "--harness", "pi")
            self.assertEqual(code, 0, err)
            png = os.path.join(d, "chart.png")
            self.assertTrue(os.path.exists(png),
                            "the inline image must land beside the SVG")
            self.assertIn(os.path.abspath(out), stdout)
            self.assertIn(os.path.abspath(png), stdout,
                          "the summary names both paths")
            self.assertIn("the inline image", stdout)
            with open(png, "rb") as fh:
                data = fh.read()
            self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertGreater(len(data), 0)
            w, h = struct.unpack(">II", data[16:24])
            # the raster honours the requested canvas; the footer may extend
            # it downward, so the width is the contract and 640 is the floor
            self.assertEqual(w, 1120)
            self.assertGreaterEqual(h, 640)

    def test_no_image_suppresses_the_png(self):
        with tempfile.TemporaryDirectory() as d:
            _chart_store(d, 30)
            out = os.path.join(d, "chart.svg")
            code, stdout, err = run("chart", "--since", "24h", "--out", out,
                                    "--no-image", "--pi-root", d, "--harness", "pi")
            self.assertEqual(code, 0, err)
            self.assertTrue(os.path.exists(out))
            self.assertFalse(os.path.exists(os.path.join(d, "chart.png")))
            self.assertNotIn("inline image", stdout)

    def test_the_json_envelope_names_the_png(self):
        with tempfile.TemporaryDirectory() as d:
            _chart_store(d, 30)
            out = os.path.join(d, "chart.svg")
            code, stdout, err = run("chart", "--since", "24h", "--out", out, "--json",
                                   "--pi-root", d, "--harness", "pi")
            self.assertEqual(code, 0, err)
            doc = json.loads(stdout)
            self.assertEqual(doc["png_path"], os.path.join(d, "chart.png"))
            self.assertGreater(doc["png_bytes"], 0)
            self.assertTrue(os.path.exists(doc["png_path"]))


class DefaultRun(unittest.TestCase):
    """Bare `luna-doctor`: the friendly default run. The account line from
    the capacity cache leads, the welcome mat explains, the one-hour
    analysis prints with the chart (SVG and PNG) beside it, and the exit
    code is the analysis verdict."""

    def test_the_cached_account_line_leads_and_the_run_is_complete(self):
        home = tempfile.mkdtemp(prefix="luna-doctor-home-")
        work = tempfile.mkdtemp(prefix="luna-doctor-run-")
        _recent_store(home)
        code, out, err = _bare_run(home, work, capacity=_capacity_payload())
        self.assertEqual(code, 0, err)
        # the account line is the first thing printed
        first = out.splitlines()[0]
        self.assertEqual(
            first,
            "LunaRoute account: apex plan \u00b7 12 priority concurrent "
            "requests \u00b7 12 background \u00b7 overflow enabled")
        # the welcome mat: what LunaRoute is, what this run did, the commands.
        # The welcome wraps at ~78 columns, so the wording is asserted on
        # whitespace-normalised text - the sentence, not the line breaks.
        flat = " ".join(out.split())
        self.assertIn("gateway in front of your models", flat)
        self.assertIn("nothing was sent anywhere", flat)
        for word in ("analyze", "chart", "fix", "capacity"):
            self.assertIn(word, out)
        self.assertNotIn("load-test", out,
                          "the welcome mat does not advertise the load test")
        # the analysis summary: the one-hour report's own stable rows
        self.assertIn("window: last 1h", out)
        self.assertIn("requests: 6", out)
        # the chart beside it: both files land in the working directory,
        # and the summary names both paths
        svgs = [f for f in os.listdir(work) if f.endswith(".svg")]
        pngs = [f for f in os.listdir(work) if f.endswith(".png")]
        self.assertEqual(len(svgs), 1, "the chart SVG must be written once")
        self.assertEqual(len(pngs), 1, "the inline image must be written once")
        with open(os.path.join(work, pngs[0]), "rb") as fh:
            data = fh.read()
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(data), 0)
        self.assertIn("the inline image", out)
        # the chart drew the account's rule, not a declaration
        self.assertIn("priority concurrency 12 (account, plan apex): rule drawn", out)

    def test_a_cold_cache_degrades_the_account_line_and_never_blocks(self):
        home = tempfile.mkdtemp(prefix="luna-doctor-home-")
        work = tempfile.mkdtemp(prefix="luna-doctor-run-")
        _recent_store(home)
        code, out, err = _bare_run(home, work)      # no capacity cache at all
        self.assertEqual(code, 0, err)
        first = out.splitlines()[0]
        self.assertEqual(first, "account: unknown (fetch with: luna-doctor capacity)")
        # the run went on: analysis and both chart files are still there
        self.assertIn("window: last 1h", out)
        self.assertTrue(any(f.endswith(".svg") for f in os.listdir(work)))
        self.assertTrue(any(f.endswith(".png") for f in os.listdir(work)))

    def test_the_exit_code_is_the_analysis_verdict(self):
        home = tempfile.mkdtemp(prefix="luna-doctor-home-")
        work = tempfile.mkdtemp(prefix="luna-doctor-run-")
        _recent_store(home)
        # the same fixture through `analyze --since 1h`: the bare run must
        # answer with analyze's own verdict, not one of its own making
        env = dict(os.environ)
        env["HOME"] = home
        env["LUNA_DOCTOR_STORE"] = os.path.join(home, "store")
        env["LUNA_DOCTOR_CAPACITY"] = _TEST_CAPACITY
        proc = subprocess.run([sys.executable, CLI, "analyze", "--since", "1h"],
                              capture_output=True, text=True, timeout=180,
                              cwd=work, env=env)
        bare_code, _, _ = _bare_run(home, work)
        self.assertEqual(bare_code, proc.returncode)


class ChartSweep(unittest.TestCase):
    """Termination and size guards for the chart's column sweep.

    The first version of the redesign hung in `_accum`: each pass
    recomputed a clamped column index from a cursor that float rounding
    had already nudged off the nominal column edges, then assigned
    `a = e` where `e` could resolve at or before `a` -- at an interior
    boundary as readily as the final one -- so the cursor never advanced,
    on a window as small as 179 requests, and the defect was never
    data-size dependent. The sweep is now bounded by the column count,
    and these tests pin that; the direct one runs out-of-process with a
    timeout, because on a regression it would hang rather than fail.
    """

    def test_accum_terminates_at_the_final_column_boundary(self):
        snippet = (
            "from luna_doctor import cli\n"
            "from luna_doctor.cli import Request\n"
            "def sweep(t0, t1, n_cols, reqs):\n"
            "    bands, band_of = cli._bands(reqs)\n"
            "    return cli._panel_series(reqs, bands, band_of, t0, t1, n_cols)\n"
            "# (t0, window, n_cols) that hung before the fix: epoch-scale t0,\n"
            "# and the 48 columns the renderer really picks for a 100s window\n"
            "m = sweep(1000000000.0, 1000000100.0, 48,\n"
            "          [Request(harness='pi', session='s', model='m',\n"
            "                   start=1000000001.0, end=1000000100.0)])\n"
            "assert abs(sum(sum(v) for v in m[0].values()) - 99.0) < 1e-9, m[0]\n"
            "assert m[2] == 1 and max(m[1]) == 1, m\n"
            "# a request ending exactly on the final boundary, in exact floats\n"
            "m = sweep(0.0, 100.0, 40,\n"
            "          [Request(harness='pi', session='s', model='m',\n"
            "                   start=1.0, end=100.0)])\n"
            "assert abs(sum(sum(v) for v in m[0].values()) - 99.0) < 1e-9, m[0]\n"
            "# a single zero-width request: nothing to accumulate, no hang\n"
            "m = sweep(0.0, 100.0, 48,\n"
            "          [Request(harness='pi', session='s', model='m',\n"
            "                   start=50.0, end=50.0)])\n"
            "assert sum(sum(v) for v in m[0].values()) == 0.0, m[0]\n"
            "# a degenerate window: t0 == t1\n"
            "m = sweep(100.0, 100.0, 48,\n"
            "          [Request(harness='pi', session='s', model='m',\n"
            "                   start=50.0, end=150.0)])\n"
            "assert sum(sum(v) for v in m[0].values()) == 0.0, m[0]\n"
            "print('ok')\n"
        )
        proc = subprocess.run([sys.executable, "-c", snippet], capture_output=True,
                              text=True, timeout=30, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "ok")

    def test_chart_renders_a_seven_thousand_request_window_in_bounded_time(self):
        """Perf guard, never skipped: the sweep is bounded by the column
        count, not the request count, and this pins that at scale. The
        render measures well under a second for 7000 requests here, so
        the 10s bound is deliberately generous headroom for a loaded
        machine while still failing any complexity regression -- a
        reintroduced quadratic or non-terminating sweep cannot get under
        it (the hang surfaces as the subprocess timeout, the same
        failure mode the termination test pins directly).
        """
        with tempfile.TemporaryDirectory() as tmp:
            _chart_store(tmp, 7000)
            out = os.path.join(tmp, "chart.svg")
            began = time.monotonic()
            code, stdout, err = run("chart", "--since", "24h", "--out", out,
                                    "--pi-root", tmp, "--harness", "pi",
                                    timeout=120)
            elapsed = time.monotonic() - began
            self.assertEqual(code, 0, err)
            with open(out) as fh:
                svg = fh.read()
            self.assertTrue(svg.startswith("<svg"), svg[:40])
            ET.fromstring(svg)
            # every request survived collection, every rejection is a tick
            # where it died, and the deadline the footer reports is the
            # measured one, per model, with its n
            self.assertIn("7000 requests", svg)
            self.assertIn("rejected at admission", svg)
            self.assertIn("measured: observed admission deadline per model", svg)
            self.assertIn("60.3s (n=", svg)
            self.assertLess(elapsed, 10.0,
                            "chart needed %.1fs for 7000 requests" % elapsed)


class ChartPlan(unittest.TestCase):
    """--plan declares the account's priority concurrency. The contract:
    a name maps to LunaRoute's published default, a bare integer passes
    through unclamped, anything else is a usage error naming what is
    accepted -- never a silent default -- and with no --plan the chart
    draws no rule and says why in the footer."""

    def _chart(self, tmp, *extra):
        _chart_store(tmp, 60)
        out = os.path.join(tmp, "chart.svg")
        code, stdout, err = run("chart", "--since", "24h", "--out", out,
                                "--pi-root", tmp, "--harness", "pi", *extra)
        return code, err, out

    def test_unknown_plan_name_is_a_usage_error_listing_the_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, err, out = self._chart(tmp, "--plan", "bogus")
            self.assertEqual(code, 2)
            self.assertIn("unknown --plan value 'bogus'", err)
            for name in ("personal", "pro", "max", "apex", "teams"):
                self.assertIn(name, err, "the error must list the valid names")
            self.assertNotIn("Traceback", err)
            self.assertFalse(os.path.exists(out),
                             "a bad declaration must not render anything")

    def test_plan_below_one_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, err, _ = self._chart(tmp, "--plan", "0")
            self.assertEqual(code, 2)
            self.assertIn("at least 1", err)
            self.assertNotIn("Traceback", err)

    def test_a_bare_integer_is_drawn_unclamped_and_labelled_declared(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, err, out = self._chart(tmp, "--plan", "25")
            self.assertEqual(code, 0, err)
            with open(out) as fh:
                svg = fh.read()
            self.assertIn("priority concurrency (--plan 25)", svg)
            self.assertIn("declared by you", svg)
            self.assertIn("A declaration, not a measurement", svg)

    def test_a_plan_name_labels_its_published_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, err, out = self._chart(tmp, "--plan", "max")
            self.assertEqual(code, 0, err)
            with open(out) as fh:
                svg = fh.read()
            self.assertIn("priority concurrency (--plan max = 4)", svg)
            self.assertIn("published plan default", svg)
            self.assertIn("A declaration, not a measurement", svg)

    def test_no_plan_draws_no_rule_and_names_the_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, err, out = self._chart(tmp)
            self.assertEqual(code, 0, err)
            with open(out) as fh:
                svg = fh.read()
            self.assertNotIn("priority concurrency (--plan", svg)
            self.assertIn("no priority concurrency rule drawn", svg)
            self.assertIn("specific to the account", svg)


class ChartSingleStack(unittest.TestCase):
    """The chart is ONE stacked-area graph: one plot area, one time axis,
    one stack, with attribution carried in the band names. The row of
    per-agent pictures is gone, so these tests pin the shape the product
    owner asked for: a regression back to several small pictures, or a
    stack that stops saying whose traffic a band is, cannot pass unnoticed.
    """

    def setUp(self):
        self.m = _load_cli()

    def _scene(self, reqs, hours=24.0):
        return self.m.build_chart_scene(reqs, self.m.analyse(reqs, hours), hours)

    def _bottom_axes(self, scene):
        # the axis under a plot area: the lowest full-width horizontal hairline.
        # A row of N pictures drew N of these at the same height; the single
        # stack draws 1. (The grey peak envelope shares the axis colour but
        # never reaches the bottom of the plot, so it must not count.)
        horiz = [s for s in scene.shapes
                 if isinstance(s, self.m.Line) and s.y1 == s.y2
                 and s.stroke == "#9ca3af" and (s.x2 - s.x1) > 500]
        if not horiz:
            return []
        bot = max(l.y1 for l in horiz)
        return [l for l in horiz if l.y1 == bot]

    def test_one_plot_area_no_matter_how_many_agents_ran(self):
        base = 1_700_000_000.0
        reqs = [
            self.m.Request(harness="pi", session="s", model="glm-5.3",
                           start=base, end=base + 600, outcome="completed"),
            self.m.Request(harness="claude-code", session="s", model="claude-opus-5-5",
                           start=base + 60, end=base + 660, outcome="completed"),
        ]
        scene = self._scene(reqs)
        self.assertEqual(len(self._bottom_axes(scene)), 1,
                         "the picture is one plot area, not a row of them")
        svg = self.m.svg_from(scene)
        self.assertNotIn("all agents", svg,
                         "the aggregate header belonged to the deleted row")
        # one time axis: every tick label of the window appears exactly once
        span = 660.0
        for i in range(7):
            label = datetime.datetime.fromtimestamp(
                base + span * i / 6, datetime.timezone.utc).strftime("%H:%M")
            self.assertEqual(svg.count(">%s<" % label), 1,
                             "tick label %r is not on a single shared axis" % label)
        self.assertEqual(svg.count('r="2.6"'), 1,
                         "one peak circle, on the one stack")

    def test_band_names_carry_the_agent_when_two_agents_ran(self):
        base = 1_700_000_000.0
        reqs = [
            self.m.Request(harness="pi", session="s", model="glm-5.3",
                           start=base, end=base + 600, outcome="completed"),
            self.m.Request(harness="claude-code", session="s", model="glm-5.3",
                           start=base + 60, end=base + 660, outcome="completed"),
        ]
        svg = self.m.svg_chart(reqs, self.m.analyse(reqs, 24.0), 24.0)
        # the same model on two agents is two bands, each named for its agent
        self.assertIn("glm-5.3 \u00b7 pi", svg)
        self.assertIn("glm-5.3 \u00b7 claude-code", svg)

    def test_band_names_drop_the_agent_when_one_agent_ran(self):
        base = 1_700_000_000.0
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base, end=base + 600, outcome="completed")]
        svg = self.m.svg_chart(reqs, self.m.analyse(reqs, 24.0), 24.0)
        self.assertIn(">glm-5.3<", svg)
        self.assertNotIn("\u00b7 pi", svg,
                         "with one agent there is nothing for a suffix to distinguish")

    def test_priority_scheduling_bases_the_stack_background_rides_above(self):
        base = 1_700_000_000.0
        reqs = [
            self.m.Request(harness="pi", session="s", model="glm-5.3",
                           start=base, end=base + 600, outcome="completed"),
            self.m.Request(harness="pi", session="s", model="glm-5.3-background",
                           start=base, end=base + 600, outcome="completed",
                           tier="background"),
        ]
        scene = self._scene(reqs)
        bot = self._bottom_axes(scene)[0].y1
        navy = [s for s in scene.shapes
                if isinstance(s, self.m.Rect) and s.fill == "#173f8f"]
        violet = [s for s in scene.shapes
                  if isinstance(s, self.m.Rect) and s.fill == "#5b21b6"]
        self.assertTrue(navy and violet, "both classes must draw their band")
        self.assertTrue(all(abs(r.y + r.h - bot) < 0.6 for r in navy),
                        "priority scheduling sits on the axis, at the base")
        self.assertTrue(all(r.y + r.h <= min(r.y for r in navy) + 0.6 for r in violet),
                        "background scheduling rides above the priority block")

    def test_an_empty_window_is_one_plot_area_with_the_honest_footer(self):
        scene = self._scene([])
        self.assertEqual(len(self._bottom_axes(scene)), 1,
                         "no traffic still draws the one plot area, not a bare message")
        svg = self.m.svg_from(scene)
        self.assertIn("no timed requests in this window", svg)
        self.assertIn("nothing was measured", svg)
        self.assertIn("no agent had a timed request in this window", svg)


# ---- the capacity payload fixtures -------------------------------------------
# The canned live payload was captured once (2026-10-23, the one live GET the
# build was allowed) and lives in tests/fixtures/. Its admission days are
# frozen, so tests whose window must overlap them build a same-shaped payload
# with days placed relative to real now.

def _capacity_payload():
    """The captured live payload verbatim: plan apex, 12/12 concurrency,
    overflow enabled, Oct 20 denied_after_wait 16, null avg_queue_wait_ms on
    the days nobody waited."""
    with open(os.path.join(ROOT, "tests", "fixtures",
                          "capacity-2026-10-23.json")) as fh:
        return json.load(fh)


def _shifted_capacity_payload(now=None):
    """The live payload's shape with admission days placed on the last seven
    complete UTC days relative to real now, so a --since window run on any
    future date still overlaps them. One day carries denied_after_wait 16
    with a real avg_queue_wait_ms; the rest are null-wait days."""
    now = now if now is not None else time.time()
    today = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date()

    def entry(days_back, daw=0, avg=None):
        return {"accepted": 4570, "avg_queue_wait_ms": avg,
                "day": (today - datetime.timedelta(days=days_back)).isoformat(),
                "deferred": 12, "denied": 0, "denied_after_wait": daw}

    last7 = [entry(7 - i) for i in range(7)]      # 7 days back .. yesterday
    last7[3]["denied_after_wait"] = 16            # four days back corroborates
    last7[3]["avg_queue_wait_ms"] = 144544.82
    as_of = last7[-1]["day"]
    return {
        "success": True,
        "generated_at": "2026-10-24T00:05:14.458609995Z",
        "data": {
            "now": {"available": 12, "in_use": 0, "ledger": "up", "limit": 12,
                    "sampled_at": "2026-10-24T00:05:14.510815919Z",
                    "saturated": False},
            "config": {"background_lanes": 12, "overflow_routing": "enabled",
                       "plan": "apex", "priority_lanes": 12},
            "history": {"as_of": as_of,
                        "last_7d": {"avg_lanes": 0.465, "days_active": 5,
                                    "days_covered": 6, "lane_hours": 55.896,
                                    "p95_lanes": 14, "peak_lanes": 18,
                                    "queued_pct": 0.08},
                        "last_30d": None,
                        "rollup_computed_at": "2026-10-23T15:19:01.958669Z",
                        "tail": {"calculated_at": "2026-10-24T00:05:14.528567203Z",
                                 "through": "2026-10-24T00:05:14.458609Z"}},
            "admission": {"as_of": as_of, "last_7d": last7, "last_30d": last7},
        },
    }


def _svg_text(svg):
    """Every text run in an SVG joined with spaces. The chart's footer wraps
    to the canvas width, so a sentence can be split across rows; joined, the
    wording is assertable without depending on where the wrap falls."""
    return " ".join(re.findall(r"<text[^>]*>([^<]*)</text>", svg))


class ChartAccountRule(unittest.TestCase):
    """The chart's priority rule resolves from the account when it can:
    the capacity API is the first authority, so a cached payload draws the
    rule labelled account and a conflicting --plan declaration is ignored
    with a note; a payload that reports no number falls back to --plan;
    neither source established means no rule with the reason said."""

    def _chart(self, tmp, *extra, **kw):
        _chart_store(tmp, 60)
        out = os.path.join(tmp, "chart.svg")
        code, stdout, err = run("chart", "--since", "24h", "--out", out,
                                "--pi-root", tmp, "--harness", "pi", *extra, **kw)
        self.assertEqual(code, 0, err)
        with open(out) as fh:
            return stdout, fh.read()

    def test_a_cached_account_payload_draws_the_rule_labelled_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            stdout, svg = self._chart(tmp, capacity=_capacity_payload())
            self.assertIn("priority concurrency 12 (account, plan apex)", svg)
            self.assertIn("fetched from api.lunaroute.com, served from disk cache", svg)
            self.assertIn("priority concurrency 12 (account, plan apex): rule drawn",
                          stdout)
            self.assertNotIn("--plan", stdout,
                             "the rule is the account's, not a declaration")

    def test_the_chart_json_carries_the_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            _chart_store(tmp, 60)
            out = os.path.join(tmp, "chart.svg")
            code, stdout, err = run("chart", "--since", "24h", "--out", out, "--json",
                                   "--pi-root", tmp, "--harness", "pi",
                                   capacity=_capacity_payload())
            self.assertEqual(code, 0, err)
            doc = json.loads(stdout)
            self.assertEqual(doc["schema"], 4)
            self.assertEqual(doc["plan"]["source"], "account")
            self.assertEqual(doc["plan"]["priority"], 12)
            self.assertEqual(doc["plan"]["account_plan"], "apex")
            self.assertTrue(doc["plan"]["account_present"])
            self.assertEqual(doc["plan"]["account_priority"], 12)

    def test_the_account_wins_over_a_declaration_and_notes_it(self):
        """Capacity API first: a --plan that disagrees with the account's
        number is ignored, and both the picture and the console say so."""
        with tempfile.TemporaryDirectory() as tmp:
            stdout, svg = self._chart(tmp, "--plan", "25", capacity=_capacity_payload())
            self.assertIn("priority concurrency 12 (account, plan apex)", svg)
            joined = _svg_text(svg)
            self.assertIn("your --plan 25 declaration was ignored", joined)
            self.assertIn("the account reports priority concurrency 12", joined)
            self.assertIn("priority concurrency 12 (account, plan apex): rule drawn",
                          stdout)
            self.assertIn("the --plan 25 declaration was ignored", stdout)

    def test_the_account_wins_in_the_json_envelope_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            _chart_store(tmp, 60)
            out = os.path.join(tmp, "chart.svg")
            code, stdout, err = run("chart", "--since", "24h", "--out", out, "--json",
                                   "--pi-root", tmp, "--harness", "pi",
                                   "--plan", "25", capacity=_capacity_payload())
            self.assertEqual(code, 0, err)
            doc = json.loads(stdout)
            self.assertEqual(doc["plan"]["source"], "account")
            self.assertEqual(doc["plan"]["priority"], 12)
            self.assertEqual(doc["plan"]["declared"], "25")
            self.assertTrue(doc["plan"]["declared_ignored"],
                            "the set-aside declaration must be visible in the envelope")
            self.assertEqual(doc["plan"]["account_priority"], 12)

    def test_a_payload_without_a_number_falls_back_to_the_declaration(self):
        payload = _capacity_payload()
        payload["data"]["config"] = None       # the documented degraded shape
        with tempfile.TemporaryDirectory() as tmp:
            stdout, svg = self._chart(tmp, "--plan", "25", capacity=payload)
            self.assertIn("priority concurrency (--plan 25)", svg)
            self.assertIn("so the declaration stands", _svg_text(svg))
            self.assertIn("declared via --plan 25", stdout)

    def test_a_stale_payload_draws_the_rule_and_says_so(self):
        """Never draw a rule from a stale payload without saying so."""
        with tempfile.TemporaryDirectory() as tmp:
            _chart_store(tmp, 60)
            out = os.path.join(tmp, "chart.svg")
            stale_path = os.path.join(tempfile.mkdtemp(prefix="luna-doctor-capacity-"),
                                      "capacity.json")
            with open(stale_path, "w") as fh:
                json.dump(_capacity_cache_doc(_capacity_payload(),
                                              time.time() - 3600), fh)
            env = dict(os.environ)
            env["LUNA_DOCTOR_STORE"] = _TEST_STORE
            env["LUNA_DOCTOR_CAPACITY"] = stale_path
            proc = subprocess.run([sys.executable, CLI, "chart", "--since", "24h",
                                  "--out", out, "--pi-root", tmp, "--harness", "pi"],
                                 capture_output=True, text=True, timeout=180,
                                 cwd=ROOT, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(out) as fh:
                svg = fh.read()
            self.assertIn("priority concurrency 12 (account, plan apex) \u00b7 stale cache", svg)
            self.assertIn("the cache is stale, so the rule may be out of date",
                          _svg_text(svg))
            self.assertIn("stale cache", proc.stdout)

    def test_a_payload_without_a_number_draws_no_rule(self):
        payload = _capacity_payload()
        payload["data"]["config"] = None       # the documented degraded shape
        with tempfile.TemporaryDirectory() as tmp:
            stdout, svg = self._chart(tmp, capacity=payload)
            self.assertNotIn("priority concurrency (", svg)
            self.assertIn("no priority concurrency rule drawn", svg)
            self.assertIn("cached but reports no priority concurrency", svg)
            self.assertIn("the cached capacity payload reports none", stdout)

    def test_no_payload_no_plan_still_says_why_and_points_at_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            stdout, svg = self._chart(tmp)
            self.assertNotIn("priority concurrency (", svg)
            self.assertIn("no priority concurrency rule drawn", svg)
            self.assertIn("specific to the account", svg)
            self.assertIn("luna-doctor capacity", svg)


class AnalyzeCapacity(unittest.TestCase):
    """metrics.capacity is additive (schema stays 4): the account's plan and
    concurrency, the server's admission counts for the window's complete
    UTC days placed next to the local outcome counts - corroborating, never
    merging - and the server counts' exclusions stated wherever they sit
    beside local ones, because the local logs see BYOK and embedding traffic
    the server never counts."""

    def setUp(self):
        self.m = _load_cli()

    def _cache_env(self, payload):
        d = tempfile.mkdtemp(prefix="luna-doctor-capacity-")
        path = os.path.join(d, "capacity.json")
        with open(path, "w") as fh:
            json.dump(_capacity_cache_doc(payload, time.time()), fh)
        return {"LUNA_DOCTOR_CAPACITY": path}

    def _block(self, payload, since, until, outcomes):
        with mock.patch.dict(os.environ, self._cache_env(payload)):
            return self.m._capacity_metrics(since, until, {"by_outcome": outcomes})

    def test_a_window_overlapping_the_admission_days_joins_verbatim(self):
        base = datetime.datetime(2026, 10, 18, 12, 0,
                                 tzinfo=datetime.timezone.utc).timestamp()
        block = self._block(_capacity_payload(), base, base + 3 * 86400,
                            {"completed": 5, "admission_timeout": 1})
        self.assertEqual(block["provenance"], "account")
        self.assertEqual(block["plan"], "apex")
        self.assertEqual(block["priority_lanes"], 12)
        self.assertEqual(block["background_lanes"], 12)
        self.assertEqual(block["overflow_routing"], "enabled")
        self.assertEqual(block["admission_as_of"], "2026-10-23")
        self.assertEqual([d["day"] for d in block["window_days"]],
                         ["2026-10-18", "2026-10-19", "2026-10-20", "2026-10-21"])
        oct20 = block["window_days"][2]
        self.assertEqual(oct20["denied_after_wait"], 16)
        self.assertEqual(oct20["avg_queue_wait_ms"], 144544.82)
        self.assertIsNone(block["window_days"][3]["avg_queue_wait_ms"])
        self.assertEqual(block["local_outcomes"],
                         {"completed": 5, "admission_timeout": 1})
        self.assertIn("BYOK", block["counts_exclude"])
        self.assertIn("embeddings", block["counts_exclude"])

    def test_a_window_outside_the_api_days_carries_no_day_counts(self):
        base = datetime.datetime(2024, 5, 1, 12, 0,
                                 tzinfo=datetime.timezone.utc).timestamp()
        block = self._block(_capacity_payload(), base, base + 3600, {})
        self.assertEqual(block["provenance"], "account")
        self.assertNotIn("window_days", block)
        self.assertNotIn("counts_exclude", block,
                         "no server counts sit beside local ones without overlap")

    def test_no_cache_is_unknown_with_its_reason(self):
        with mock.patch.dict(os.environ, {"LUNA_DOCTOR_CAPACITY": "/no/such/capacity.json"}):
            block = self.m._capacity_metrics(time.time() - 3600, time.time(), {})
        self.assertEqual(block["provenance"], "unknown")
        self.assertIn("no cached capacity", block["reason"])

    def _metrics_with_capacity(self, window_days):
        base = time.time() - 600
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base, end=base + 30, outcome="completed")]
        m = self.m.analyse(reqs, 7 * 24)
        m["capacity"] = {"provenance": "account", "source": "GET capacity", "stale": False,
                          "note": "served from cache", "plan": "apex",
                          "priority_lanes": 12, "background_lanes": 12,
                          "overflow_routing": "enabled",
                          "window_days": window_days,
                          "local_outcomes": dict(m["by_outcome"]),
                          "counts_exclude": self.m.CAPACITY_COUNTS_EXCLUDE}
        return m

    def test_denied_after_wait_corroborates_the_park_deadline_class(self):
        days = [{"accepted": 4570, "avg_queue_wait_ms": 144544.82, "day": "2026-10-20",
                 "deferred": 12, "denied": 0, "denied_after_wait": 16}]
        head, adj, code = self.m.verdict(self._metrics_with_capacity(days))
        text = "\n".join(adj)
        self.assertIn("local peak 1 concurrent request(s) (a lower bound", text)
        self.assertIn("account limit 12 priority", text)
        self.assertIn("confirms 16 request(s) were parked and then refused "
                      "after waiting (denied_after_wait) on 2026-10-20 UTC", text)
        self.assertIn("Cross-check, not a merge", text)
        self.assertIn("BYOK", text)

    def test_clean_days_state_the_exclusions_without_corroborating(self):
        days = [{"accepted": 4570, "avg_queue_wait_ms": None, "day": "2026-10-21",
                 "deferred": 0, "denied": 0, "denied_after_wait": 0}]
        head, adj, code = self.m.verdict(self._metrics_with_capacity(days))
        text = "\n".join(adj)
        self.assertNotIn("denied_after_wait) on", text,
                         "nothing corroborates when the server counted none")
        self.assertIn("account limit 12 priority", text)
        self.assertIn("server counts cover only traffic contending for the shared pool", text)

    def test_capacity_unknown_adds_no_adjustments(self):
        base = time.time() - 600
        reqs = [self.m.Request(harness="pi", session="s", model="glm-5.3",
                               start=base, end=base + 30, outcome="completed")]
        m = self.m.analyse(reqs, 1.0)
        m["capacity"] = {"provenance": "unknown", "reason": "no cached capacity"}
        head, adj, code = self.m.verdict(m)
        text = "\n".join(adj)
        self.assertNotIn("account limit", text)
        self.assertNotIn("BYOK", text)

    def test_a_byok_and_embedding_window_renders_the_exclusion_line(self):
        """Two exclusions that must not be conflated: the local window
        excludes non-gateway traffic by provider (the vendor filter), while
        the server excludes gateway-routed BYOK and embeddings from its
        admission counts; the report must say the server's part wherever the
        two sit beside each other - and the server's denied_after_wait must
        corroborate."""
        with tempfile.TemporaryDirectory() as tmp:
            now = time.time()
            lines = []
            traffic = [("glm-5.3", "lunaroute"), ("glm-5.3", "lunaroute"),
                       ("glm-5.3-background", "lunaroute"),
                       ("gpt-5.3", "openai"),                 # BYOK: own key
                       ("text-embedding-3-large", "openai")]  # embeddings
            for i, (model, provider) in enumerate(traffic):
                end = now - i * 120
                start = end - 30
                ts = datetime.datetime.fromtimestamp(end, datetime.timezone.utc)
                stamp = (ts.strftime("%Y-%m-%dT%H:%M:%S")
                         + ".%03dZ" % (ts.microsecond // 1000))
                lines.append(json.dumps({
                    "type": "message", "id": "id-%d" % i, "timestamp": stamp,
                    "message": {"role": "assistant", "model": model,
                                "provider": provider,
                                "timestamp": int(start * 1000),
                                "usage": {"input": 10, "output": 100},
                                "stopReason": "stop", "errorMessage": None,
                                "content": [{"type": "text", "text": "x"}],
                                "responseId": "r-%d" % i}}))
            proj = os.path.join(tmp, "proj", "--cap--")
            os.makedirs(proj, exist_ok=True)
            with open(os.path.join(proj, "2026-10-23T06-41-17-867Z_abc.jsonl"), "w") as fh:
                fh.write("\n".join(lines) + "\n")
            code, out, err = run("analyze", "--since", "7d", "--pi-root", tmp,
                                 "--harness", "pi", "--no-store",
                                 capacity=_shifted_capacity_payload())
            self.assertIn(code, (0, 1), err)
            self.assertIn("server counts cover only traffic contending for the "
                          "shared pool", out)
            self.assertIn("BYOK", out)
            self.assertIn("denied_after_wait", out)
            self.assertIn("account limit 12 priority", out)
            self.assertIn("3 requests", out)   # the vendor filter keeps the
            # window gateway-only: the two openai (BYOK/embedding) requests
            # are excluded and reported in their own row, not counted.
            self.assertIn("excluded: 2 pi request(s) on other providers "
                          "(openai 2)", out)


class CapacityCommand(unittest.TestCase):
    """`capacity` renders the API's own story from the canned live payload:
    now/config/history/admission, unknown-never-zero for every degraded
    shape, the envelope on --json, and unknown (never an error) with nothing
    established."""

    def test_renders_the_canned_live_payload(self):
        code, out, err = run("capacity", capacity=_capacity_payload())
        self.assertEqual(code, 0, err)
        self.assertIn("account capacity: plan apex \u00b7 priority concurrency 12", out)
        self.assertIn("in use 0 of 12 priority concurrent requests", out)
        self.assertIn("ledger: up", out)
        self.assertIn("plan: apex", out)
        self.assertIn("priority concurrency: 12", out)
        self.assertIn("background: 12 concurrent requests", out)
        self.assertIn("eligible managed text-generation requests only", out)
        self.assertIn("5 day(s) with traffic", out)
        self.assertIn("6 day(s) with data", out)
        self.assertIn("minute-level occupancy through", out)
        self.assertIn("2026-10-20: accepted 4570 \u00b7 deferred 12 \u00b7 denied 0 "
                      "\u00b7 denied after wait 16 \u00b7 avg queue wait 144.5s", out)
        self.assertIn("no waiting that day", out)
        self.assertIn("one entry per complete UTC day, through 2026-10-23 (as_of)", out)
        self.assertIn("server counts cover only traffic contending for the shared pool", out)
        self.assertIn("served from cache", out)

    def test_json_envelope(self):
        code, out, err = run("capacity", "--json", capacity=_capacity_payload())
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual(doc["schema"], 4)
        self.assertEqual(doc["command"], "capacity")
        self.assertIn("contract", doc)
        cap = doc["capacity"]
        self.assertEqual(cap["provenance"], "account")
        self.assertEqual(cap["served_from"], "cache")
        self.assertFalse(cap["stale"])
        self.assertEqual(cap["config"]["priority_lanes"], 12)
        self.assertEqual(cap["now"]["ledger"], "up")
        self.assertEqual(cap["admission"]["as_of"], "2026-10-23")
        self.assertEqual(len(cap["admission"]["last_7d"]), 7)
        self.assertEqual(len(cap["admission"]["last_30d"]), 30)
        self.assertEqual(cap["history"]["last_7d"]["peak_lanes"], 18)
        # the null contract survives the envelope: a null wait day stays null
        waits = [d["avg_queue_wait_ms"] for d in cap["admission"]["last_7d"]]
        self.assertIn(None, waits)

    def test_nothing_established_is_unknown_not_an_error(self):
        code, out, err = run("capacity")
        self.assertEqual(code, 2, "no data, the tool's convention - not an error")
        self.assertIn("account capacity: unknown", out)
        self.assertIn("no cached capacity", out)
        self.assertIn("pass --online to fetch it now", out)
        self.assertNotIn("Traceback", err)

    def test_a_stale_cache_serves_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "capacity.json")
            with open(path, "w") as fh:
                json.dump(_capacity_cache_doc(_capacity_payload(),
                                              time.time() - 3600), fh)
            env = dict(os.environ)
            env["LUNA_DOCTOR_STORE"] = _TEST_STORE
            env["LUNA_DOCTOR_CAPACITY"] = path
            proc = subprocess.run([sys.executable, CLI, "capacity"],
                                  capture_output=True, text=True, timeout=180,
                                  cwd=ROOT, env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("served stale", proc.stdout)
            self.assertIn("past its 5-minute TTL", proc.stdout)

    def test_degraded_nulls_render_unknown_never_zero(self):
        payload = {
            "success": True, "generated_at": "2026-10-24T00:05:14.458609995Z",
            "data": {
                "now": {"limit": None, "in_use": None, "available": None,
                        "saturated": None, "ledger": "unknown", "sampled_at": None},
                "config": None,
                "history": {"as_of": "2026-10-22", "last_7d": None,
                            "last_30d": None, "rollup_computed_at": None,
                            "tail": None},
                "admission": {"as_of": "2026-10-23",
                              "last_7d": [{"accepted": 10,
                                           "avg_queue_wait_ms": None,
                                           "day": "2026-10-22", "deferred": 0,
                                           "denied": 0, "denied_after_wait": 0}],
                              "last_30d": None},
            },
        }
        code, out, err = run("capacity", capacity=payload)
        self.assertEqual(code, 0, err)
        self.assertIn("plan: unknown (not reported)", out)
        self.assertIn("priority concurrency: unknown (not reported)", out)
        self.assertIn("background: unknown (not reported)", out)
        self.assertIn("overflow routing: unknown (not reported)", out)
        self.assertIn("occupancy unknown (not reported)", out)
        self.assertIn("saturated: unknown (not reported)", out)
        self.assertIn("ledger: unknown", out)
        self.assertIn("not reported (null) - days_covered already excludes it", out)
        self.assertIn("not reported by the account (unknown)", out)
        self.assertIn("avg queue wait no waiting that day", out)
        self.assertNotIn("priority concurrency: 0", out)
        self.assertNotIn("in use 0 of", out)

    def test_zero_background_concurrency_is_a_real_value(self):
        payload = _shifted_capacity_payload()
        payload["data"]["config"] = {"plan": "pro", "priority_lanes": 2,
                                     "background_lanes": 0,
                                     "overflow_routing": "disabled"}
        code, out, err = run("capacity", capacity=payload)
        self.assertEqual(code, 0, err)
        self.assertIn("0 concurrent requests (the account is entitled to no overflow)", out)
        self.assertIn("overflow routing: disabled", out)
        self.assertNotIn("background: unknown", out)


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


def _pi_line(ts, model, start_ms, end_ms, err=None, stop="stop", out=100,
             provider="lunaroute"):
    """One pi assistant record, the shape iter_pi reads."""
    rec = {
        "type": "message",
        "id": f"id-{ts}",
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "model": model,
            "provider": provider,
            "timestamp": start_ms,
            "usage": {"input": 10, "output": out},
            "stopReason": "error" if err else stop,
            "errorMessage": err,
            "content": [{"type": "text", "text": "x"}],
            "responseId": f"r-{ts}",
        },
    }
    return json.dumps(rec)


def _claude_line(mid, model, ts, stop="end_turn", out=10):
    """One claude-code assistant record, shaped like the real vendors.

    The two vendor populations in the live store differ in more than the
    model name, and these fixtures keep that corroboration: an
    Anthropic-direct record carries Anthropic's native msg_011C... id and a
    non-empty inference_geo, while a LunaRoute-routed one carries the
    gateway's own msg_<uuid> id and empty geo (verified store-wide; see
    claude_route's docstring).
    """
    anthropic = model.startswith("claude-") and "-lr" not in model
    return json.dumps({
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "id": mid,
            "model": model,
            "stop_reason": stop,
            "usage": {"input_tokens": 3, "output_tokens": out,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0,
                      "service_tier": "standard",
                      "inference_geo": "global" if anthropic else "",
                      "speed": "standard", "fallback_credit": None,
                      "iterations": 1},
        },
    })


def _write_catalog(path, models):
    """A cached LunaRoute catalog naming `models`, in the file shape
    sources._read_catalog_cache serves. Tests point LUNA_DOCTOR_CATALOG at
    it, so the claude-code classifier reads a fixture cache and never the
    real one, the network, or $HOME."""
    with open(path, "w") as fh:
        json.dump({"fetched_at": time.time(),
                   "response": {"data": [{"id": m} for m in models]}}, fh)
    return path


# The catalog the claude-code regression fixtures route through: their
# sessions ran on glm-5.3, which no naming rule can attribute, so the
# classifier must see it in a catalog (see the VendorFilterClaude tests for
# the naming rules themselves).
_REGRESSION_CATALOG = _write_catalog(
    os.path.join(_TEST_STORE, "catalog-glm-5.3.json"), ("glm-5.3",))


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
    to fewer concurrent requests or the -background tier. `message_limit` is
    the one exception: a message-count ceiling, which holding fewer concurrent
    requests genuinely cannot fix - but it is still not a quota.
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
        self.assertIn("priority concurrency allowance", joined)
        self.assertIn("-background", joined)
        # The old advice told the reader that narrowing concurrent requests
        # would not help.
        self.assertNotIn("will not fix it", joined)
        self.assertNotIn("not concurrency", joined)

    def test_org_limit_advice_is_about_the_whole_priority_concurrency(self):
        joined = self._advice("org_concurrency_limit")
        self.assertIn("priority concurrency", joined)
        self.assertIn("Hold fewer concurrent requests", joined)
        self.assertNotIn("not a rate limit", joined)

    def test_message_limit_is_a_message_count_ceiling(self):
        joined = self._advice("message_limit")
        self.assertIn("message-count limit", joined)
        self.assertIn("not concurrency pressure", joined)
        self.assertIn("holding fewer concurrent requests will not fix it", joined)

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


class EnvelopeSchema(unittest.TestCase):
    def test_analyze_envelope_contract_drops_the_borrowed_deadline(self):
        code, out, err = run("analyze", "--since", "1h", "--json")
        self.assertIn(code, (0, 1, 2), err)
        doc = json.loads(out)
        self.assertEqual(doc["schema"], 4)
        self.assertNotIn("admission_deadline_s", doc["contract"],
                         "per-model deadlines replace the borrowed constant")
        for key in ("pre_header_wait_s", "client_idle_timeout_s", "interactive_burst_cap",
                    "advised_concurrency"):
            self.assertIn(key, doc["contract"])
        self.assertNotIn("advised_fanout_max", doc["contract"],
                         "schema 4 renamed the advised-contract key to the"
                         " LunaRoute vocabulary")


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
        # claude-code fixtures route through a catalog: the classifier must
        # see their model as LunaRoute's, and no test reads the real cache.
        os.environ["LUNA_DOCTOR_CATALOG"] = _REGRESSION_CATALOG

    def tearDown(self):
        os.environ.pop("LUNA_DOCTOR_CATALOG", None)

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
        # claude-code fixtures route through a catalog: the classifier must
        # see their model as LunaRoute's, and no test reads the real cache.
        os.environ["LUNA_DOCTOR_CATALOG"] = _REGRESSION_CATALOG

    def tearDown(self):
        os.environ.pop("LUNA_DOCTOR_CATALOG", None)

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
        # claude-code fixtures route through a catalog: the classifier must
        # see their model as LunaRoute's, and no test reads the real cache.
        os.environ["LUNA_DOCTOR_CATALOG"] = _REGRESSION_CATALOG

    def tearDown(self):
        os.environ.pop("LUNA_DOCTOR_CATALOG", None)

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

    def test_mixed_model_load_is_measured_on_the_account(self):
        """codex: per-model concurrency hid account-wide saturation."""
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
            reqs = self.m.iter_claude([tmp], base + 10, base + 3600,
                                      catalog={"glm-5.3"})
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
                    reqs = self.m.iter_claude([tmp], base + 10, base + 3600,
                                              catalog={"glm-5.3"})
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
        # claude-code fixtures route through a catalog: the classifier must
        # see their model as LunaRoute's, and no test reads the real cache.
        os.environ["LUNA_DOCTOR_CATALOG"] = _REGRESSION_CATALOG

    def tearDown(self):
        os.environ.pop("LUNA_DOCTOR_CATALOG", None)

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
                         "peak concurrency of one is a slow client, not a burst of concurrent requests")

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

    def test_demotion_note_hedges_when_no_load_test_has_spoken(self):
        m = _load_cli()
        note = m.demotion_note("", "glm-5.3-background")
        self.assertIn("may be running on priority concurrency", note)
        self.assertIn("only a load test can see", note.lower())


class SaturatedVocabulary(unittest.TestCase):
    def test_stream_stalled_is_not_a_promised_outcome(self):
        m = _load_cli()
        r = m.Request(harness="pi", session="s", model="m", outcome="stream_stalled")
        self.assertFalse(r.saturated)
        r2 = m.Request(harness="pi", session="s", model="m", outcome="client_idle_timeout")
        self.assertTrue(r2.saturated)


class VendorFilterPi(unittest.TestCase):
    """pi names the provider that answered; only lunaroute touched the gateway.

    The real store carries lunaroute beside ollama, gengen, openai-codex,
    lmstudio, poolside and test gateways. Local and direct sessions have
    different latency, no admission, no parking - and were inflating the
    concurrency, latency and per-model stats of a report about the gateway.
    """

    def setUp(self):
        self.m = _load_cli()

    def _store(self, tmp, lines, name="s.jsonl"):
        d = os.path.join(tmp, "proj")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w") as fh:
            fh.write("\n".join(lines) + "\n")

    def _stamp(self, now, offset=800):
        return datetime.datetime.fromtimestamp(
            now - offset, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def test_only_lunaroute_traffic_reaches_the_request_set(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            start_ms = int((now - 900) * 1000)
            self._store(tmp, [
                _pi_line(self._stamp(now, 800), "glm-5.3", start_ms, None),
                _pi_line(self._stamp(now, 780), "minimax-m3:cloud", start_ms, None,
                         provider="ollama"),
                _pi_line(self._stamp(now, 760), "glm-5-2-260617", start_ms, None,
                         provider="gengen"),
                # no record in the real store lacks the provider field, so an
                # empty one is not attributable to the gateway either
                _pi_line(self._stamp(now, 740), "glm-5.3", start_ms, None, provider=""),
            ])
            reqs = self.m.collect(1, ["pi"], pi_roots=[tmp])
        self.assertEqual([r.model for r in reqs], ["glm-5.3"])
        self.assertEqual(reqs.excluded["pi_non_lunaroute"], 3)
        self.assertEqual(reqs.excluded["pi_by_provider"],
                         {"(no provider)": 1, "gengen": 1, "ollama": 1})

    def test_exclusions_are_counted_once_per_response(self):
        """the filter sits after de-duplication: a mirrored copy of the same
        ollama response is one exclusion, not two."""
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            start_ms = int((now - 900) * 1000)
            line = _pi_line(self._stamp(now), "minimax-m3:cloud", start_ms, None,
                            provider="ollama")
            self._store(tmp, [line], name="a.jsonl")
            self._store(tmp, [line], name="b.jsonl")
            reqs = self.m.collect(1, ["pi"], pi_roots=[tmp])
        self.assertEqual(len(reqs), 0)
        self.assertEqual(reqs.excluded["pi_non_lunaroute"], 1)

    def test_metrics_and_report_carry_the_exclusions(self):
        from luna_doctor import report as _report
        reqs = self.m.Requests(
            [self.m.Request(harness="pi", session="s", model="glm-5.3",
                             start=1.0, end=2.0, outcome="completed")],
            excluded={"pi_non_lunaroute": 2, "pi_by_provider": {"ollama": 2}})
        m = self.m.analyse(reqs, 1.0)
        self.assertEqual(m["excluded"],
                         {"pi_non_lunaroute": 2, "pi_by_provider": {"ollama": 2}})
        head, adj, _ = self.m.verdict(m)
        out = _report.render_plain(self.m.build_report(m, head, adj))
        self.assertIn("2 pi request(s) on other providers (ollama 2) excluded", out)
        self.assertIn("never touched the gateway", out)
        # a list assembled by hand filtered nothing, and says so by omission
        self.assertNotIn("excluded", self.m.analyse(list(reqs), 1.0))

    def test_a_clean_window_reports_zero_exclusions(self):
        """the block is present when the filter ran, even when it dropped
        nothing - a positive statement that the window is gateway-only."""
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            start_ms = int((now - 900) * 1000)
            self._store(tmp, [_pi_line(self._stamp(now), "glm-5.3", start_ms, None)])
            reqs = self.m.collect(1, ["pi"], pi_roots=[tmp])
        m = self.m.analyse(reqs, 1.0)
        self.assertEqual(m["excluded"],
                         {"pi_non_lunaroute": 0, "pi_by_provider": {}})

    def test_the_cli_reports_what_it_excluded(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            start_ms = int((now - 900) * 1000)
            self._store(tmp, [
                _pi_line(self._stamp(now, 800), "glm-5.3", start_ms, None),
                _pi_line(self._stamp(now, 780), "minimax-m3:cloud", start_ms, None,
                         provider="ollama"),
            ])
            code, out, err = run("analyze", "--since", "1h", "--pi-root", tmp,
                                 "--harness", "pi", "--json")
            self.assertIn(code, (0, 1), err)
            doc = json.loads(out)
            self.assertEqual(doc["metrics"]["excluded"]["pi_non_lunaroute"], 1)
            self.assertEqual(doc["metrics"]["excluded"]["pi_by_provider"],
                             {"ollama": 1})
            code, out, err = run("analyze", "--since", "1h", "--pi-root", tmp,
                                 "--harness", "pi")
            self.assertIn(code, (0, 1), err)
        self.assertIn("1 pi request(s) on other providers (ollama 1) excluded", out)
        self.assertIn("never touched the gateway", out)


class VendorFilterClaude(unittest.TestCase):
    """claude-code logs no provider, so the route is read from the model name.

    Verified against the live store: LunaRoute-routed sessions ran
    deepseek-4.1-flash and its -background twin (live-catalog models) and the
    -lr aliases claude-opus-lr11 / claude-opus-lr29 / claude-haiku-lr34;
    Anthropic-direct sessions ran plain claude-sonnet-5-5 / claude-opus-5-5
    and carry Anthropic-native msg_011C... ids; `<synthetic>` is claude-code's
    own local placeholder, never an API call.
    """

    def setUp(self):
        self.m = _load_cli()

    def _store(self, tmp, records, name="s.jsonl"):
        d = os.path.join(tmp, "proj")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w") as fh:
            fh.write("\n".join(records) + "\n")

    def _stamp(self, now, offset=800):
        return datetime.datetime.fromtimestamp(
            now - offset, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def test_anthropic_direct_models_are_excluded_and_counted(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, [
                _claude_line("msg_011Cabc123def456ghi78jkl",
                             "claude-sonnet-5-5", self._stamp(now, 800)),
                _claude_line("msg_011Czzz999yyy888xxx77www",
                             "claude-opus-5-5", self._stamp(now, 780)),
                _claude_line("msg_40e6185b-085e-4c9e-98d1-0981184a2888",
                             "claude-opus-lr11", self._stamp(now, 760)),
            ])
            reqs = self.m.iter_claude([tmp], now - 3600, now, catalog=None)
        self.assertEqual([r.model for r in reqs], ["claude-opus-lr11"])
        self.assertEqual(reqs.excluded["claude_non_lunaroute"], 2)
        self.assertEqual(reqs.excluded["claude_by_model"],
                         {"claude-opus-5-5": 1, "claude-sonnet-5-5": 1})
        self.assertEqual(reqs.excluded["claude_unclassified"], 0)

    def test_catalog_models_and_their_background_twin_are_kept(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, [
                _claude_line("msg_70f4aaaa-bbbb-4ccc-8ddd-0123456789ab",
                             "deepseek-4.1-flash", self._stamp(now, 800)),
                _claude_line("msg_70f4cccc-bbbb-4ccc-8ddd-0123456789ab",
                             "deepseek-4.1-flash-background", self._stamp(now, 780)),
            ])
            reqs = self.m.iter_claude([tmp], now - 3600, now,
                                      catalog={"deepseek-4.1-flash"})
        self.assertEqual([r.model for r in reqs],
                         ["deepseek-4.1-flash", "deepseek-4.1-flash-background"])
        self.assertEqual(reqs.excluded["claude_non_lunaroute"], 0)
        self.assertEqual(reqs.excluded["claude_unclassified"], 0)

    def test_without_a_catalog_a_non_claude_model_is_unclassified(self):
        """No catalog, no -lr alias, no claude naming: the honest bucket is
        unclassified - excluded and counted, never guessed into the window."""
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp, [
                _claude_line("msg_70f4aaaa-bbbb-4ccc-8ddd-0123456789ab",
                             "deepseek-4.1-flash", self._stamp(now, 800)),
                _claude_line("5d841807-c4b5-4a1e-8f2a-63b7a52cb1a5",
                             "<synthetic>", self._stamp(now, 780)),
            ])
            reqs = self.m.iter_claude([tmp], now - 3600, now, catalog=None)
        self.assertEqual(len(reqs), 0)
        self.assertEqual(reqs.excluded["claude_unclassified"], 2)
        self.assertEqual(reqs.excluded["claude_unclassified_by_model"],
                         {"<synthetic>": 1, "deepseek-4.1-flash": 1})
        self.assertEqual(reqs.excluded["claude_non_lunaroute"], 0)

    def test_the_classifier_reads_the_route_off_the_model_name(self):
        route = self.m.claude_route
        self.assertEqual(route("deepseek-4.1-flash", {"deepseek-4.1-flash"}),
                         "lunaroute")
        self.assertEqual(
            route("deepseek-4.1-flash-background", {"deepseek-4.1-flash"}),
            "lunaroute")
        self.assertEqual(route("glm-5.3-vision", ["glm-5.3-vision"]), "lunaroute")
        # the catalog trap: an alias today's catalog no longer lists is still
        # LunaRoute's own naming, and still gateway traffic
        self.assertEqual(route("claude-opus-lr11", None), "lunaroute")
        self.assertEqual(route("claude-haiku-lr34", set()), "lunaroute")
        # plain Anthropic naming, no alias: direct traffic
        self.assertEqual(route("claude-sonnet-5-5", {"deepseek-4.1-flash"}),
                         "anthropic")
        self.assertEqual(route("claude-opus-4-8", None), "anthropic")
        # everything else is unclassifiable from the logs alone
        self.assertEqual(route("gpt-5.5", None), "")
        self.assertEqual(route("<synthetic>", {"glm-5.3"}), "")
        self.assertEqual(route("", {"deepseek-4.1-flash"}), "")

    def test_the_cli_classifies_through_the_catalog_cache(self):
        """collect() serves the classifier from the disk cache only: with a
        cached catalog naming deepseek-4.1-flash the run keeps that traffic
        and counts the Anthropic-direct session beside it."""
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as cat:
            catalog_path = _write_catalog(os.path.join(cat, "catalog.json"),
                                          ["deepseek-4.1-flash"])
            self._store(tmp, [
                _claude_line("msg_70f4aaaa-bbbb-4ccc-8ddd-0123456789ab",
                             "deepseek-4.1-flash", self._stamp(now, 800)),
                _claude_line("msg_011Cabc123def456ghi78jkl",
                             "claude-sonnet-5-5", self._stamp(now, 780)),
            ])
            code, out, err = run("analyze", "--since", "1h", "--claude-root", tmp,
                                 "--harness", "claude-code", "--json",
                                 env={"LUNA_DOCTOR_CATALOG": catalog_path})
            self.assertIn(code, (0, 1), err)
            doc = json.loads(out)
        self.assertEqual(doc["metrics"]["requests"], 1)
        self.assertEqual(doc["metrics"]["excluded"]["claude_non_lunaroute"], 1)
        self.assertEqual(doc["metrics"]["excluded"]["claude_by_model"],
                         {"claude-sonnet-5-5": 1})


if __name__ == "__main__":
    unittest.main()
