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


def _store(n=6, hours_ago=0.2, kind="mixed"):
    """A small pi store.

    `mixed` is one stalled request inside a wide burst. `refusals_only` is a
    quiet machine whose only signal is 429s. `stall_only` is one stalled
    request and nothing else.
    """
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
    if kind == "stall_only":
        add(200, 100, "client_idle_timeout", "resp-stalled")
    elif kind == "refusals_only":
        for i in range(4):
            add(300 - i * 20, 299.5 - i * 20, "org_concurrency_limit",
                "resp-refused-%d" % i)
    else:
        for i in range(n):
            add(200 + i, 60 + i, "completed", "resp-ok-%d" % i)
        add(198, 100, "client_idle_timeout", "resp-stalled")
        add(150, 149.5, "org_concurrency_limit", "resp-refused")
    path = os.path.join(d, "s.jsonl")
    with open(path, "w") as fh:
        fh.write("\n".join(json.dumps(r) for r in rows) + "\n")
    os.utime(path, (now, now))
    return home


def capacity_cache(home, in_use=12, limit=12, sampled_at="2026-10-26T04:00:00Z"):
    """A fresh cache file in the shape sources reads: {fetched_at, response}."""
    doc = {"fetched_at": time.time() - 30,
           "response": {"success": True, "data": {
               "config": {"plan": "apex", "priority_lanes": limit,
                          "background_lanes": 12, "overflow_routing": "enabled"},
               "now": {"in_use": in_use, "limit": limit, "available": limit - in_use,
                       "saturated": in_use >= limit, "ledger": "up",
                       "sampled_at": sampled_at}}}}
    path = os.path.join(home, "capacity.json")
    with open(path, "w") as fh:
        json.dump(doc, fh)
    return path


def run(home, *args, timeout=120, capacity=None):
    env = dict(os.environ)
    env["HOME"] = home
    env["LUNA_DOCTOR_STORE"] = os.path.join(home, "store")
    env["LUNA_DOCTOR_CAPACITY"] = capacity or os.path.join(home, "no-capacity.json")
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


class CapacityWiring(unittest.TestCase):
    """The account half has to reach the consumers, not stop at the cache."""

    def test_the_live_sample_reaches_the_json_block(self):
        home = _store()
        cap = capacity_cache(home)
        code, out, err = run(home, "analyze", "--since", "1h", "--json", capacity=cap)
        self.assertIn(code, (0, 1), err)
        doc = json.loads(out)
        now = doc["metrics"]["capacity"].get("now") or {}
        self.assertEqual(12, now.get("in_use"))
        self.assertEqual("2026-10-26T04:00:00Z", now.get("sampled_at"))

    def test_a_quiet_machine_with_429s_and_a_full_account_reads_your_org(self):
        home = _store(kind="refusals_only")
        cap = capacity_cache(home, in_use=12, limit=12)
        code, out, err = run(home, "analyze", "--since", "1h", "--json", capacity=cap)
        self.assertIn(code, (0, 1), err)
        doc = json.loads(out)
        self.assertEqual("your-org", doc["attribution"]["verdict"], doc["attribution"])


class WatchStalls(unittest.TestCase):
    def test_a_stall_shows_up_in_the_line(self):
        home = _store(kind="stall_only")
        code, out, err = run(home, "watch", "--once")
        self.assertIn(code, (0, 1), err)
        self.assertIn("stall", out.lower(), out)


class RedactionOrdering(unittest.TestCase):
    def test_a_normalized_error_signature_cannot_smuggle_an_email(self):
        """`error_signature()` masks digits with `#`, so an address inside an
        error string arrives as `jesse#@example.com`. The block still must not
        carry it."""
        home = tempfile.mkdtemp(prefix="luna-doctor-norm-")
        d = os.path.join(home, ".pi", "agent", "sessions", "s")
        os.makedirs(d)
        now = time.time()
        msg = {"role": "assistant", "model": "glm-5.3", "provider": "lunaroute",
               "usage": {"input": 5, "output": 5}, "stopReason": "error",
               "errorMessage": "upstream said: contact jesse2@example.com then retry",
               "timestamp": int((now - 30) * 1000)}
        path = os.path.join(d, "s.jsonl")
        with open(path, "w") as fh:
            fh.write(json.dumps({"type": "message",
                                 "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                                            time.gmtime(now - 20)),
                                 "message": msg}) + "\n")
        os.utime(path, (now, now))
        code, out, err = run(home, "analyze", "--since", "1h", "--style", "plain",
                             "--for-issue")
        self.assertIn(code, (0, 1), err)
        self.assertNotIn("example.com", out, out)
        self.assertNotIn("@", out.replace("<redacted:email>", ""), out)


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

    def test_issue_block_redacts_an_explicit_store_root(self):
        """The wiring, not just redact() in isolation: --pi-root puts a real
        absolute path into the stores line, and it must not survive."""
        home = _store()
        root = os.path.join(home, ".pi", "agent", "sessions", "s")
        code, out, err = run(home, "analyze", "--since", "1h", "--style", "plain",
                             "--for-issue", "--pi-root", root)
        self.assertIn(code, (0, 1), err)
        for spelling in (home, os.path.realpath(home)):
            for line in out.splitlines():
                self.assertNotIn(spelling, line,
                                 f"the issue block leaked {spelling}: {line}")

    def test_issue_block_replaces_the_report(self):
        home = _store()
        _, out, err = run(home, "analyze", "--since", "1h", "--for-issue")
        self.assertNotIn("TRAFFIC", out.upper(), "one artefact, not the report plus an artefact")
        self.assertIn("(this machine)", out)


if __name__ == "__main__":
    unittest.main()
