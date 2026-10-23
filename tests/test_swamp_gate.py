"""Swamp is conditional: installed is not in use.

Five gates, one per state an install can be in: no marker, installed but
idle, active inside the window, idle in `discover`, and the arithmetic rule
that a measured peak and a configured fan-out are different kinds of number
and never render as one sum.
"""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from luna_doctor import cli


def _load_cli():
    """Import the CLI the way the installed console script does: as a package."""
    return cli


class SwampGate(unittest.TestCase):
    def setUp(self):
        self._old_root = os.environ.get("SWAMP_ROOT")
        os.environ.pop("SWAMP_ROOT", None)

    def tearDown(self):
        if self._old_root is None:
            os.environ.pop("SWAMP_ROOT", None)
        else:
            os.environ["SWAMP_ROOT"] = self._old_root

    def test_no_marker_means_not_installed_and_no_subprocess(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["SWAMP_ROOT"] = d
            state = cli.swamp_state()
            self.assertFalse(state["installed"])
            self.assertFalse(state["in_use"])
            self.assertEqual(state["evidence"], "no marker")

    def test_installed_but_idle_is_not_in_use(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, ".swamp.yaml"), "w").close()
            os.environ["SWAMP_ROOT"] = d
            state = cli.swamp_state()
            self.assertTrue(state["installed"])
            self.assertFalse(state["in_use"])
            self.assertEqual(state["evidence"], "installed, no activity")

    def test_activity_in_window_means_in_use(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, ".swamp.yaml"), "w").close()
            runs = os.path.join(d, ".swamp", "workflow-runs", "r1")
            os.makedirs(runs)
            open(os.path.join(runs, "run.yaml"), "w").write("ok: true\n")
            os.environ["SWAMP_ROOT"] = d
            state = cli.swamp_state(since=0)
            self.assertTrue(state["in_use"])
            self.assertIn("workflow run", state["evidence"])

    def test_discover_omits_idle_swamp(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, ".swamp.yaml"), "w").close()
            os.environ["SWAMP_ROOT"] = d
            rep = cli.build_discover_report(cli.discover())
            labels = [r.label for s in rep.sections for r in s.rows]
            self.assertNotIn("swamp", labels)

    def test_measured_peak_is_never_summed_with_configured_swamp(self):
        """6 measured requests + a 20-wide swamp config is not 26 of anything."""
        m = _load_cli()
        reqs = [m.Request(harness="pi", session="s", model="glm-5.3",
                          start=1_700_000_000.0, end=1_700_000_020.0, outcome="completed")
                for _ in range(6)]
        metrics = m.analyse(reqs, window_hours=24)
        metrics["_swamp_configured_fanout"] = 20
        head, adj, _ = m.verdict(metrics)
        self.assertNotIn("= 26", head)
        self.assertIn("6", head)
        self.assertTrue(any("configured" in line.lower() for line in [head] + adj),
                        "swamp fan-out must be described as configuration, not measured traffic")


if __name__ == "__main__":
    unittest.main()
