"""Smoke tests for luna-doctor.

Stdlib only, no fixtures to maintain. Run from the repo root:

    python3 -m unittest discover -s tests -v

Everything here is read-only: it runs the CLI against the local log stores and
checks the contract (exit codes, JSON envelope, version consistency). The live
`probe`/`compare` commands spend credits, so they are not exercised.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "bin", "luna-doctor")
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def run(*args, timeout=180):
    proc = subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True,
                          timeout=timeout, cwd=ROOT)
    return proc.returncode, proc.stdout, proc.stderr


class VersionContract(unittest.TestCase):
    def test_version_is_semver(self):
        code, out, _ = run("--version")
        self.assertEqual(code, 0)
        version = out.strip().split()[-1]
        self.assertRegex(version, SEMVER)

    def test_version_matches_version_file(self):
        _, out, _ = run("--version")
        script_version = out.strip().split()[-1]
        with open(os.path.join(ROOT, "VERSION")) as fh:
            file_version = fh.read().strip()
        self.assertEqual(script_version, file_version,
                         "VERSION and bin/luna-doctor disagree; run scripts/release.sh")

    def test_version_matches_newest_git_tag(self):
        try:
            tags = subprocess.run(["git", "tag", "--list", "v*"], capture_output=True,
                                  text=True, cwd=ROOT, timeout=10)
        except Exception:
            self.skipTest("git unavailable")
        if tags.returncode != 0 or not tags.stdout.strip():
            self.skipTest("no release tags yet")
        newest = sorted(tags.stdout.split(), key=lambda t: [int(p) for p in t.lstrip("v").split(".")])[-1]
        _, out, _ = run("--version")
        self.assertEqual(out.strip().split()[-1], newest.lstrip("v"),
                         "the newest tag and the code disagree; tag the released version")

    def test_changelog_has_the_current_version(self):
        _, out, _ = run("--version")
        version = out.strip().split()[-1]
        with open(os.path.join(ROOT, "CHANGELOG.md")) as fh:
            text = fh.read()
        self.assertIn(f"## [{version}]", text)


class JsonEnvelope(unittest.TestCase):
    def _envelope(self, *args):
        code, out, err = run(*args)
        self.assertIn(code, (0, 1, 2), f"unexpected exit {code}: {err}")
        doc = json.loads(out)          # must be pure JSON on stdout
        self.assertEqual(doc["schema"], 1)
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
        self.assertEqual(doc["schema"], 1)
        self.assertEqual(doc["path"], out)
        self.assertIn("contract", doc)


class Sources(unittest.TestCase):
    def _module(self):
        import importlib.util
        import sys
        from importlib.machinery import SourceFileLoader
        loader = SourceFileLoader("luna_doctor", CLI)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[loader.name] = mod     # dataclass annotations resolve via sys.modules
        loader.exec_module(mod)
        return mod

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
        # minute 2: the admission death lands in the minute it died
        self.assertEqual(series[2]["max_concurrent"], 1)
        self.assertEqual(series[2]["started"], 1)
        self.assertEqual(series[2]["admission_deaths"], 1)
        for row in series:
            self.assertRegex(row["minute"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z$")

    def test_concurrency_by_minute_empty(self):
        m = self._module()
        self.assertEqual(m.concurrency_by_minute([]), [])

    def test_both_files_compile(self):
        for rel in ("bin/luna-doctor", "bin/_fix_block.py"):
            path = os.path.join(ROOT, rel)
            proc = subprocess.run([sys.executable, "-m", "py_compile", path],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"{rel}: {proc.stderr}")

    def test_emitted_apply_blocks_never_write_on_their_own(self):
        """`fix` must print, never apply."""
        code, out, _ = run("fix", "--since", "24h")
        self.assertIn(code, (0, 1))
        self.assertIn("Nothing below runs on its own", out)


if __name__ == "__main__":
    unittest.main()
