"""agentsview is optional, and luna-doctor only ever reads it.

The one command that may run is `agentsview projects --json`. A daemon start, a
serve or a sync would write to the index on a diagnostic run, which is not
luna-doctor's business, so the guard here records the argv the fake binary was
called with and fails if it is anything else.

The tests run a fake executable on PATH, so they need no install and no network.
"""

import json
import os
import stat
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from luna_doctor import sources  # noqa: E402

FAKE = """#!/bin/sh
printf '%s\\n' "$*" >> "$ARGV_LOG"
if [ "$1" = "projects" ] && [ "$2" = "--json" ]; then
  cat "$PAYLOAD"
else
  echo "unexpected command: $*" >&2
  exit 9
fi
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="luna-doctor-agentsview-")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.argv_log = os.path.join(self.tmp, "argv.log")
        self.payload = os.path.join(self.tmp, "payload.json")
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def install(self, payload, exit_code=0):
        with open(self.payload, "w", encoding="utf-8") as fh:
            fh.write(payload)
        path = os.path.join(self.bin, "agentsview")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(FAKE)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        # Prepend, so the fake wins but `cat` still resolves for the shell.
        os.environ["PATH"] = self.bin + os.pathsep + self._env.get("PATH", "")
        os.environ["ARGV_LOG"] = self.argv_log
        os.environ["PAYLOAD"] = self.payload
        return exit_code

    def called_argv(self):
        if not os.path.isfile(self.argv_log):
            return []
        with open(self.argv_log, encoding="utf-8") as fh:
            return [line.strip() for line in fh if line.strip()]


class Present(Base):
    def test_counts_sessions_and_projects(self):
        self.install(json.dumps([{"name": "a", "session_count": 7},
                                 {"name": "b", "session_count": 4}]))
        status = sources.agentsview_status()
        self.assertTrue(status["present"])
        self.assertTrue(status["readable"])
        self.assertEqual(status["projects"], 2)
        self.assertEqual(status["sessions"], 11)

    def test_uses_one_read_only_query(self):
        """No sync, no serve, no daemon: one projects query, nothing else."""
        self.install(json.dumps([{"name": "a", "session_count": 1}]))
        sources.agentsview_status()
        self.assertEqual(self.called_argv(), ["projects --json"],
                         "agentsview was called with something other than the one "
                         "read-only query")

    def test_a_failing_query_is_unreadable_not_absent(self):
        self.install("[]", exit_code=0)
        path = os.path.join(self.bin, "agentsview")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 3\n")
        os.chmod(path, 0o755)
        os.environ["PATH"] = self.bin + os.pathsep + self._env.get("PATH", "")
        status = sources.agentsview_status()
        self.assertTrue(status["present"], "the binary is there, so it is not absent")
        self.assertFalse(status["readable"])
        self.assertIn("exited 3", status["reason"])
        self.assertIsNone(status["sessions"])

    def test_output_that_is_not_json_is_unreadable(self):
        self.install("this is not json")
        status = sources.agentsview_status()
        self.assertFalse(status["readable"])
        self.assertIn("not JSON", status["reason"])
        self.assertIsNone(status["sessions"])


class Absent(Base):
    def test_not_installed_says_so_and_needs_no_query(self):
        os.environ["PATH"] = self.bin
        status = sources.agentsview_status()
        self.assertFalse(status["present"])
        self.assertFalse(status["readable"])
        self.assertEqual(status["reason"], "not installed")
        self.assertEqual(self.called_argv(), [])


if __name__ == "__main__":
    unittest.main()
