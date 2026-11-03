"""Service first: the account's numbers lead, the machine's explain them.

The account is what the tool exists to explain, so its section comes before
anything this machine measured, and every number in it says whose it is. The
local sections keep their own labels, and nothing here is ever added to them.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from luna_doctor import cli


def metrics(**capacity):
    """A small analyse() metrics dict with an account block attached."""
    m = cli.analyse([], window_hours=1)
    if capacity:
        m["capacity"] = capacity
    return m


ACCOUNT = {
    "provenance": "account",
    "plan": "apex",
    "priority_lanes": 12,
    "background_lanes": 12,
    "overflow_routing": "enabled",
    "now_fresh": True,
    "now": {
        "in_use": 8, "limit": 12, "available": 4, "saturated": False,
        "ledger": "up", "sampled_at": "2026-11-03T22:14:42.219872605Z",
        "background": {"limit": 12, "in_use": 9, "available": 3, "saturated": False},
    },
    "admission_as_of": "2026-11-02",
    "window_days": [
        {"day": "2026-11-01", "accepted": 40, "deferred": 2, "denied": 1,
         "denied_after_wait": 6},
        {"day": "2026-11-02", "accepted": 30, "deferred": 0, "denied": 0,
         "denied_after_wait": 0},
    ],
}


class TheServiceSectionLeads(unittest.TestCase):
    def test_it_is_the_first_section(self):
        report = cli.build_report(metrics(**ACCOUNT), "headline", [])
        self.assertEqual(report.sections[0].title, "service")

    def test_every_account_number_says_whose_it_is(self):
        report = cli.build_report(metrics(**ACCOUNT), "headline", [])
        rows = report.sections[0].rows
        self.assertTrue(rows)
        unlabelled = [r.label for r in rows
                      if "(account)" not in r.value and "stale" not in r.value]
        self.assertEqual(unlabelled, [],
                         "an account number without (account) reads as this machine's")

    def test_the_live_pool_carries_its_sample_time(self):
        report = cli.build_report(metrics(**ACCOUNT), "headline", [])
        live = [r for r in report.sections[0].rows if r.label == "live priority pool"]
        self.assertTrue(live, "the live occupancy belongs in the service section")
        self.assertIn("in use 8 of 12 priority concurrent requests", live[0].value)
        self.assertIn("2026-11-03T22:14:42.219872605Z", live[0].value,
                      "a reading without its moment is a fact about no moment")

    def test_the_overflow_pool_is_its_own_row(self):
        report = cli.build_report(metrics(**ACCOUNT), "headline", [])
        labels = [r.label for r in report.sections[0].rows]
        self.assertIn("overflow pool", labels)
        self.assertIn("live overflow pool", labels)

    def test_the_admission_record_names_the_blocked_days(self):
        report = cli.build_report(metrics(**ACCOUNT), "headline", [])
        row = [r for r in report.sections[0].rows if r.label == "admission record"][0]
        self.assertIn("2 complete UTC day(s)", row.value)
        self.assertIn("6 denied after waiting", row.value)
        self.assertIn("most on 2026-11-01, at 6", row.value)

    def test_no_payload_means_no_section(self):
        """A machine with no account payload still gets the local report."""
        self.assertEqual(cli._service_rows(metrics()), [])
        unknown = cli._service_rows(metrics(provenance="unknown",
                                            reason="no cached capacity"))
        self.assertEqual(unknown, [])

    def test_a_stale_payload_says_the_reading_is_of_another_moment(self):
        stale = dict(ACCOUNT, stale=True)
        rows = cli._service_rows(metrics(**stale))
        labels = [r.label for r in rows]
        self.assertIn("account sample", labels)
        text = " ".join(r.value for r in rows if r.label == "account sample")
        self.assertIn("another moment", text)

    def test_a_zero_overflow_entitlement_is_not_a_width(self):
        zero = dict(ACCOUNT, background_lanes=0)
        rows = cli._service_rows(metrics(**zero))
        overflow = [r for r in rows if r.label == "overflow pool"][0]
        self.assertIn("no overflow entitlement", overflow.value)
        self.assertNotIn("0 concurrent requests", overflow.value)


if __name__ == "__main__":
    unittest.main()
