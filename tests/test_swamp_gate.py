"""Swamp is opt-in: one named repo, and its own run tracker decides use.

Swamp keeps run history per repository, and nothing on the machine lists the
repositories a person uses swamp in. So the tool never looks for swamp: the
caller names a repository with `--swamp-root`, and without that flag no swamp
appears in any report. Within a named repository, installed is still not in
use: swamp's own `run_tracker.db` says whether a run overlapped the window, and
that replaces the file-time guess an earlier version made.

Also held here: the arithmetic rule that a measured peak and a configured
concurrency never render as one sum, and the scratch key a zero-request window
must not ship to `--json`.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from luna_doctor import cli
from tests import swamp_fixtures

CLI = os.path.join(ROOT, "bin", "luna-doctor")


def iso(epoch: float) -> str:
    """A stamp in the shape swamp writes: 2026-10-23T17:12:59.642Z."""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class SwampIsOptIn(unittest.TestCase):
    def test_without_the_flag_swamp_never_enters_the_report(self):
        """A swamp repo in the working directory must not appear unasked.

        This is the whole point of the flag: the tool used to walk up from the
        cwd, found the repo it was sitting in, and put swamp's configured
        concurrency into pi's advice. Same layout here, and the report must
        still say nothing about swamp.
        """
        with tempfile.TemporaryDirectory() as d:
            repo = swamp_fixtures.make_swamp_repo(
                d, runs=[{"started_at": iso(datetime.now(timezone.utc).timestamp())}])
            old = os.getcwd()
            os.chdir(repo)
            try:
                stores = cli.discover()
            finally:
                os.chdir(old)
            self.assertFalse(stores["swamp"]["present"])
            self.assertIsNone(stores["swamp"]["repoDir"])
            self.assertEqual(stores["swamp"]["evidence"], "not requested")

    def test_a_named_dir_without_the_marker_is_no_marker(self):
        with tempfile.TemporaryDirectory() as d:
            state = cli.swamp_state(d)
            self.assertFalse(state["installed"])
            self.assertFalse(state["in_use"])
            self.assertEqual(state["evidence"], "no marker")

    def test_installed_without_a_tracker_is_not_in_use(self):
        """Older swamp, or a store whose tracker is gone: say so, claim nothing.

        The history counts still report the size of the store, because that is
        a fact about the repository and not a claim about the window.
        """
        with tempfile.TemporaryDirectory() as d:
            repo = swamp_fixtures.make_swamp_repo(d, behaviors=[{}], workflow_runs=[{}],
                                                  tracker=False)
            state = cli.swamp_state(repo, since=0)
            self.assertTrue(state["installed"])
            self.assertFalse(state["in_use"])
            self.assertEqual(state["evidence"], "installed, no run tracker")
            self.assertEqual(state["behaviors"], 1)
            self.assertEqual(state["workflow_runs"], 1)

    def test_a_run_inside_the_window_is_in_use(self):
        with tempfile.TemporaryDirectory() as d:
            now = datetime.now(timezone.utc).timestamp()
            repo = swamp_fixtures.make_swamp_repo(d, runs=[
                {"started_at": iso(now - 600), "completed_at": iso(now - 60),
                 "status": "succeeded", "workflow_name": "catchup"}])
            state = cli.swamp_state(repo, since=now - 3600, until=now)
            self.assertTrue(state["in_use"])
            self.assertEqual(state["runs"]["overlapping"], 1)
            self.assertEqual(state["evidence"], "1 run(s) in window")

    def test_old_runs_do_not_make_a_full_store_busy(self):
        """The retired heuristic: hundreds of old files said "in use" forever."""
        with tempfile.TemporaryDirectory() as d:
            old = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
            repo = swamp_fixtures.make_swamp_repo(
                d, behaviors=[{} for _ in range(3)], workflow_runs=[{} for _ in range(2)],
                runs=[{"started_at": iso(old), "completed_at": iso(old + 60),
                       "status": "succeeded"}])
            now = datetime.now(timezone.utc).timestamp()
            state = cli.swamp_state(repo, since=now - 3600, until=now)
            self.assertFalse(state["in_use"])
            self.assertEqual(state["evidence"], "installed, no runs in window")
            self.assertEqual(state["behaviors"], 3)

    def test_a_run_still_open_with_a_running_status_is_in_flight(self):
        with tempfile.TemporaryDirectory() as d:
            now = datetime.now(timezone.utc).timestamp()
            repo = swamp_fixtures.make_swamp_repo(d, runs=[
                {"started_at": iso(now - 120), "status": "running"}])
            state = cli.swamp_state(repo, since=now - 3600, until=now)
            self.assertTrue(state["in_use"])
            self.assertEqual(state["runs"]["in_flight"], 1)
            self.assertIn("in flight now", state["evidence"])

    def test_a_queued_run_counts_as_use(self):
        with tempfile.TemporaryDirectory() as d:
            now = datetime.now(timezone.utc).timestamp()
            repo = swamp_fixtures.make_swamp_repo(d, pending=[
                {"created_at": iso(now - 30), "workflow_id_or_name": "catchup"}])
            state = cli.swamp_state(repo, since=now - 3600, until=now)
            self.assertTrue(state["in_use"])
            self.assertEqual(state["runs"]["pending"], 1)

    def test_an_unreadable_tracker_reads_as_no_tracker(self):
        """A corrupt file is not evidence, and it must not raise."""
        with tempfile.TemporaryDirectory() as d:
            repo = swamp_fixtures.make_swamp_repo(d)
            path = os.path.join(repo, ".swamp", "run_tracker.db")
            with open(path, "wb") as fh:
                fh.write(b"not a database at all")
            state = cli.swamp_state(repo, since=0)
            self.assertFalse(state["in_use"])
            self.assertEqual(state["evidence"], "installed, no run tracker")
            self.assertFalse(state["runs"]["available"])
            self.assertIn("unreadable", state["runs"]["reason"])

    def test_discover_omits_idle_swamp(self):
        with tempfile.TemporaryDirectory() as d:
            repo = swamp_fixtures.make_swamp_repo(d)
            rep = cli.build_discover_report(cli.discover(swamp_root=repo))
            labels = [r.label for s in rep.sections for r in s.rows]
            self.assertNotIn("swamp", labels)

    def test_the_advice_gate_needs_the_named_repo(self):
        """`fix` reads the gate from the metrics, not from the filesystem."""
        self.assertFalse(cli._swamp_present({})[0])
        self.assertFalse(cli._swamp_present({"swamp": {"present": False}})[0])
        idle = {"swamp": {"repoDir": "/tmp/x", "runTracker": {"available": True,
                                                              "overlapping": 0,
                                                              "pending": 0}}}
        self.assertFalse(cli._swamp_present(idle)[0])
        busy = {"swamp": {"repoDir": "/tmp/x", "runTracker": {"available": True,
                                                              "overlapping": 3,
                                                              "pending": 0}}}
        present, repo = cli._swamp_present(busy)
        self.assertTrue(present)
        self.assertEqual(repo, "/tmp/x")


class SwampNumbers(unittest.TestCase):
    def test_measured_peak_is_never_summed_with_configured_swamp(self):
        """6 measured requests + a 20-wide swamp config is not 26 of anything."""
        reqs = [cli.Request(harness="pi", session="s", model="glm-5.3",
                            start=1_700_000_000.0, end=1_700_000_020.0, outcome="completed")
                for _ in range(6)]
        metrics = cli.analyse(reqs, window_hours=24)
        metrics["_swamp_configured_concurrency"] = 20
        head, adj, _ = cli.verdict(metrics)
        self.assertIn("6", head)
        self.assertTrue(any("configured" in line.lower() for line in [head] + adj),
                        "swamp's configured concurrency must be described as a setting, "
                        "not measured traffic")
        # This test once checked only the headline, so a sum re-added to the
        # adjustments passed. No line may carry the bare sum of the measured
        # peak (6) and the configured concurrency (20), in any voice.
        for line in [head] + adj:
            self.assertIsNone(re.search(r"\b26\b", line),
                              f"measured + configured must not be summed: {line}")

    def test_json_envelope_on_an_empty_window_ships_no_scratch_keys(self):
        """A zero-request window returns from verdict() before the scratch key
        is consumed; `analyze --json` must still not ship it."""
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as empty:
            now = datetime.now(timezone.utc).timestamp()
            repo = swamp_fixtures.make_swamp_repo(
                d, runs=[{"started_at": iso(now - 600), "completed_at": iso(now - 60),
                          "status": "succeeded"}])
            proc = subprocess.run(
                [sys.executable, CLI, "analyze", "--json", "--since", "24h",
                 "--pi-root", empty, "--claude-root", empty, "--no-store",
                 "--swamp-root", repo],
                capture_output=True, text=True, timeout=180, cwd=ROOT)
            self.assertEqual(proc.returncode, 2, "an empty window is no data, not pressure")
            doc = json.loads(proc.stdout)
            self.assertTrue((doc["metrics"].get("swamp") or {}).get("present"),
                            "the leak path needs swamp in use to be exercised")
            self.assertTrue(doc["metrics"]["swamp"]["runTracker"]["available"],
                            "the tracker is what put swamp in the envelope")

            def keys(node):
                if isinstance(node, dict):
                    for k, v in node.items():
                        yield k
                        yield from keys(v)
                elif isinstance(node, list):
                    for v in node:
                        yield from keys(v)

            scratch = [k for k in keys(doc) if k.startswith("_")]
            self.assertEqual(scratch, [], "no internal key may ride the JSON envelope")


if __name__ == "__main__":
    unittest.main()
