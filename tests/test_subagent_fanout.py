"""Async subagent runs are invisible to the request parser, and measurable.

pi writes each async subagent's transcript beside the session that launched it,
at `<store>/<session>/subagent-artifacts/<runId>_<agent>_transcript.jsonl`. No
record carries `type`, so `iter_pi` reads none of them: they are not requests to
the gateway. They do answer a different question from measurement - how many
subagents this machine ran at the same time - which is what a person asking "is
it safe to fan out eight" wants to know.

Held here: the parser leaves those files alone, a run's span is its first and
last record, the window decides which runs count, the peak is the largest
number open at one instant, the parent session groups one launch, and a missing
meta file reads unknown rather than zero.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from luna_doctor import cli

CLI = os.path.join(ROOT, "bin", "luna-doctor")


def epoch(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


def make_run(store: str, run_id: str, agent: str, start: float, end: float,
             parent: str | None = "parent-1", exit_code: int | None = 0,
             records: int = 3) -> str:
    """Write one subagent run: a transcript plus the meta file beside it."""
    session = os.path.join(store, "--project--")
    folder = os.path.join(session, "subagent-artifacts")
    os.makedirs(folder, exist_ok=True)
    stem = os.path.join(folder, f"{run_id}_{agent}")
    span = [start + (end - start) * i / max(1, records - 1) for i in range(records)]
    with open(stem + "_transcript.jsonl", "w", encoding="utf-8") as fh:
        for i, ts in enumerate(span):
            fh.write(json.dumps({
                "version": 1, "recordType": "message", "source": "async",
                "runId": run_id, "agent": agent, "childIndex": 0,
                "ts": int(ts * 1000),
                "timestamp": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                "sourceEventType": "message_end" if i else "initial_prompt",
                "role": "user" if i == 0 else "assistant",
            }) + "\n")
    if parent is not None or exit_code is not None:
        meta = {"runId": run_id, "agent": agent}
        if parent is not None:
            meta["parentSessionId"] = parent
        if exit_code is not None:
            meta["exitCode"] = exit_code
        with open(stem + "_meta.json", "w", encoding="utf-8") as fh:
            json.dump(meta, fh)
    return stem


class SubagentRunsAreNotRequests(unittest.TestCase):
    def test_the_request_parser_reads_none_of_them(self):
        """The blind spot was real: these records are not `type: message`."""
        with tempfile.TemporaryDirectory() as d:
            make_run(d, "run-a", "worker", epoch(2026, 3, 1, 10, 0), epoch(2026, 3, 1, 10, 5))
            reqs = cli.iter_pi([d], since=epoch(2026, 3, 1), prune_mtime=False)
            self.assertEqual(reqs, [], "a subagent transcript is not gateway traffic")


class SpanAndWindow(unittest.TestCase):
    def test_the_span_is_the_first_and_last_record(self):
        with tempfile.TemporaryDirectory() as d:
            make_run(d, "run-a", "worker", epoch(2026, 3, 1, 10, 0),
                     epoch(2026, 3, 1, 10, 10), records=5)
            runs = cli.iter_subagent_runs([d], since=epoch(2026, 3, 1), until=epoch(2026, 3, 2))
            self.assertEqual(len(runs), 1)
            self.assertAlmostEqual(runs[0]["start"], epoch(2026, 3, 1, 10, 0), places=3)
            self.assertAlmostEqual(runs[0]["end"], epoch(2026, 3, 1, 10, 10), places=3)
            self.assertEqual(runs[0]["agent"], "worker")
            self.assertEqual(runs[0]["parent"], "parent-1")
            self.assertEqual(runs[0]["exitCode"], 0)

    def test_a_run_that_ended_before_the_window_is_out(self):
        with tempfile.TemporaryDirectory() as d:
            make_run(d, "old", "worker", epoch(2026, 3, 1, 9, 0), epoch(2026, 3, 1, 9, 5))
            runs = cli.iter_subagent_runs([d], since=epoch(2026, 3, 1, 10, 0),
                                          until=epoch(2026, 3, 1, 11, 0))
            self.assertEqual(runs, [])

    def test_a_run_that_started_after_the_window_is_out(self):
        with tempfile.TemporaryDirectory() as d:
            make_run(d, "later", "worker", epoch(2026, 3, 1, 12, 0), epoch(2026, 3, 1, 12, 5))
            runs = cli.iter_subagent_runs([d], since=epoch(2026, 3, 1, 10, 0),
                                          until=epoch(2026, 3, 1, 11, 0))
            self.assertEqual(runs, [])

    def test_a_missing_meta_file_is_unknown_not_zero(self):
        with tempfile.TemporaryDirectory() as d:
            make_run(d, "run-a", "worker", epoch(2026, 3, 1, 10, 0),
                     epoch(2026, 3, 1, 10, 5), parent=None, exit_code=None)
            runs = cli.iter_subagent_runs([d], since=epoch(2026, 3, 1), until=epoch(2026, 3, 2))
            self.assertIsNone(runs[0]["parent"], "an unknown parent is not a parent")
            self.assertIsNone(runs[0]["exitCode"], "an unknown exit code is not success")


class PeakFanOut(unittest.TestCase):
    def test_overlapping_runs_peak_at_their_count(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(8):
                make_run(d, f"run-{i}", "worker", epoch(2026, 3, 1, 10, 0),
                         epoch(2026, 3, 1, 10, 10))
            runs = cli.iter_subagent_runs([d], since=epoch(2026, 3, 1), until=epoch(2026, 3, 2))
            fan = cli.subagent_fanout(runs, since=epoch(2026, 3, 1), until=epoch(2026, 3, 2))
            self.assertEqual(fan["runs"], 8)
            self.assertEqual(fan["peakInFlight"], 8)
            self.assertEqual(fan["peakAt"], "2026-03-01T10:00:00Z")

    def test_sequential_runs_peak_at_one(self):
        with tempfile.TemporaryDirectory() as d:
            make_run(d, "first", "worker", epoch(2026, 3, 1, 10, 0), epoch(2026, 3, 1, 10, 4))
            make_run(d, "second", "worker", epoch(2026, 3, 1, 10, 5), epoch(2026, 3, 1, 10, 9))
            runs = cli.iter_subagent_runs([d], since=epoch(2026, 3, 1), until=epoch(2026, 3, 2))
            fan = cli.subagent_fanout(runs, since=epoch(2026, 3, 1), until=epoch(2026, 3, 2))
            self.assertEqual(fan["peakInFlight"], 1,
                             "two runs that never overlapped are not a fan-out of two")

    def test_the_parent_session_groups_one_launch(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(3):
                make_run(d, f"a-{i}", "worker", epoch(2026, 3, 1, 10, 0),
                         epoch(2026, 3, 1, 10, 10), parent="parent-a")
            for i in range(5):
                make_run(d, f"b-{i}", "reviewer", epoch(2026, 3, 1, 12, 0),
                         epoch(2026, 3, 1, 12, 10), parent="parent-b")
            runs = cli.iter_subagent_runs([d], since=epoch(2026, 3, 1), until=epoch(2026, 3, 2))
            fan = cli.subagent_fanout(runs, since=epoch(2026, 3, 1), until=epoch(2026, 3, 2))
            self.assertEqual(fan["peakInFlight"], 5, "the two launches did not overlap")
            self.assertEqual(fan["byParent"]["parent-b"], 5)
            self.assertEqual(fan["byParent"]["parent-a"], 3)
            self.assertEqual(fan["byAgent"], {"reviewer": 5, "worker": 3})

    def test_a_non_zero_exit_is_counted_and_a_zero_is_not(self):
        with tempfile.TemporaryDirectory() as d:
            make_run(d, "ok", "worker", epoch(2026, 3, 1, 10, 0), epoch(2026, 3, 1, 10, 5))
            make_run(d, "bad", "worker", epoch(2026, 3, 1, 10, 0), epoch(2026, 3, 1, 10, 5),
                     exit_code=2)
            runs = cli.iter_subagent_runs([d], since=epoch(2026, 3, 1), until=epoch(2026, 3, 2))
            fan = cli.subagent_fanout(runs, since=epoch(2026, 3, 1), until=epoch(2026, 3, 2))
            self.assertEqual(fan["failed"], 1)

    def test_the_fan_out_never_enters_the_request_numbers(self):
        """A run is not a request: the peak concurrency is the logs' own."""
        reqs = [cli.Request(harness="pi", session="s", model="glm-5.3",
                            start=epoch(2026, 3, 1, 10, 0),
                            end=epoch(2026, 3, 1, 10, 1), outcome="completed")]
        m = cli.analyse(reqs, window_hours=24)
        before = m["peak_concurrency"]
        m["subagents"] = {"runs": 8, "peakInFlight": 8, "peakAt": "2026-03-01T10:00:00Z",
                          "byAgent": {"worker": 8}, "byParent": {"p": 8}, "failed": 0}
        head, adj, _ = cli.verdict(m)
        self.assertEqual(m["peak_concurrency"], before)
        for line in [head] + adj:
            self.assertNotIn("16", line, "measured requests + measured runs is not a number")
        report = cli.build_report(m, head, adj)
        row = [r for s in report.sections for r in s.rows if r.label == "subagents"]
        self.assertTrue(row, "the fan-out belongs in the report as its own row")
        self.assertIn("peak 8 in flight", row[0].value)


class EnvelopeCarriesIt(unittest.TestCase):
    def test_analyze_json_carries_the_measurement(self):
        now = datetime.now(timezone.utc).timestamp()
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as empty:
            make_run(d, "run-a", "worker", now - 600, now - 60, exit_code=0)
            proc = subprocess.run(
                [sys.executable, CLI, "analyze", "--json", "--since", "24h",
                 "--pi-root", d, "--harness", "pi", "--no-store",
                 "--claude-root", empty],
                capture_output=True, text=True, timeout=180, cwd=ROOT)
            self.assertIn(proc.returncode, (0, 1, 2), proc.stderr)
            doc = json.loads(proc.stdout)
            sub = doc["metrics"].get("subagents") or {}
            self.assertEqual(sub.get("runs"), 1)
            self.assertEqual(sub.get("peakInFlight"), 1)


if __name__ == "__main__":
    unittest.main()
