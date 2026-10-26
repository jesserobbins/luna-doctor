"""End-to-end wiring for the two commands that are new here: `watch` and
`analyze --for-issue`.

The module tests cover the composition; these check the seam - that argparse
accepts the flags, that the loop stops when told to, that the exit code follows
the verdict, and that the issue block is redacted before it reaches stdout.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "bin", "luna-doctor")


def _store(n=6, hours_ago=0.2):
    """A small pi store: one stalled request inside a wide burst."""
    home = tempfile.mkdtemp(prefix="luna-doctor-newcmd-")
    d = os.path.join(home, ".pi", "agent", "sessions", "s")
    os.makedirs(d)
    now = time.time()
    rows = []
    def add(start_off, end_off, outcome, rid):
        start, end = now - start_off, now - end_off
        msg = {"role": "assistant", "model": "glm-5.3", "provider": "lunaroute",
               "usage": {"input": 10, "output": 5}, "stopReason": "stop",
               "responseId": rid, "timestamp": int(start * 1000)}
        if outcome == "client_idle_timeout":
            msg["stopReason"] = "error"
            msg["errorMessage"] = "Stream ended without finish_reason"
        if outcome == "org_concurrency_limit":
            msg["stopReason"] = "error"
            msg["errorMessage"] = '429: {"code":"CONCURRENT_REQUEST_LIMIT_EXCEEDED"}'
        rows.append({"type": "message",
                     "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                                time.gmtime(end)),
                     "message": msg})
    for i in range(n):
        add(200 + i, 60 + i, "completed", "resp-ok-%d" % i)
    add(198, 100, "client_idle_timeout", "resp-stalled")
    add(150, 149.5, "org_concurrency_limit", "resp-refused")
    path = os.path.join(d, "s.jsonl")
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(r) for r in rows) + "\n")
    os.utime(path, (now, now))
    return home


def run(home, *args, timeout=120):
    env = dict(os.environ)
    env["HOME"] = home
    env["LUNA_DOCTOR_STORE"] = os.path.join(home, "store")
    env["LUNA_DOCTOR_CAPACITY"] = os.path.join(home, "no-capacity.json")
    env["NO_COLOR"] = "1"
    proc = subprocess.run([sys.executable, CLI, *args], capture_output=True,
                          text=True, cwd=ROOT, env=env, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


class Watch(unittest.TestCase):
    def test_once_prints_one_sample_and_the_summary(self):
        home = _store()
        code, out, err = run(home, "watch", "--once", "--style", "plain")
        self.assertIn(code, (0, 1), err)
        self.assertTrue(any("(this machine)" in line for line in out.splitlines()), out)
        self.assertIn("1 sample", out)  # the summary, singular for one

    def test_count_stops_the_loop(self):
        home = _store()
        started = time.time()
        code, out, err = run(home, "watch", "--count", "2", "--interval", "1")
        self.assertIn(code, (0, 1), err)
        self.assertLess(time.time() - started, 60, "the loop must stop on --count")
        self.assertIn("2 samples", out)

    def test_piped_output_carries_no_cursor_control(self):
        home = _store()
        code, out, err = run(home, "watch", "--once")
        self.assertNotIn("\x1b[", out, "a pipe must never receive escape sequences")
        self.assertNotIn("\x1b[", err)

    def test_a_wide_burst_reads_as_pressure_in_the_line(self):
        home = _store()
        code, out, err = run(home, "watch", "--once")
        self.assertEqual(1, code, out + err)
        self.assertIn("(this machine)", out)


class ForIssue(unittest.TestCase):
    def test_issue_block_is_datable_and_redacted(self):
        home = _store()
        code, out, err = run(home, "analyze", "--since", "1h", "--style", "plain",
                             "--plan", "apex", "--for-issue")
        self.assertIn(code, (0, 1), err)
        self.assertIn("luna-doctor", out.splitlines()[0])
        self.assertIn("schema", out.splitlines()[0])
        self.assertFalse([l for l in out.splitlines() if home in l],
                         "the issue block must not carry an absolute store path")
        self.assertIn("window:", out)
        self.assertIn("verdict:", out)

    def test_issue_block_replaces_the_report(self):
        home = _store()
        _, out, err = run(home, "analyze", "--since", "1h", "--for-issue")
        self.assertNotIn("TRAFFIC", out.upper(), "one artefact, not the report plus an artefact")
        self.assertIn("(this machine)", out)


if __name__ == "__main__":
    unittest.main()
