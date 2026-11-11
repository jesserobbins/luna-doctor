"""The triage screen's data contract: one answer in, three badged panels out.

Every assertion reads the (badge, text) rows the panels are built from, so the
suite runs offline and needs no terminal: what is tested here is the
data-to-widget mapping, not pixels. Textual is an optional extra
(`luna-doctor[tui]`), so this file imports it only when it is installed -
`tui.py` imports Textual at the top by design, and the caller guards that
import.

The order the rows come in is the design's ranked-trust order: what the account
reports (authoritative), then what this machine's logs show (informative, a
lower bound), then the verdict. The verdict rows come from `cli.verdict`, so a
second verdict implementation cannot drift from the CLI's.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

pytest.importorskip("textual")

from luna_doctor import badges, cli, report, sources, tui

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")


# ---- the same canned inputs the capacity tests read -------------------------
def _payload(name):
    with open(os.path.join(FIXTURES, name)) as fh:
        return json.load(fh)


def _cap(payload):
    """The Capacity dataclass fetch_capacity builds from one payload.

    It goes through the same parser the fetch does, so a null stays a null
    here exactly as it does on the disk-cache path."""
    fields, err = sources._parse_capacity_payload(payload)
    assert not err, err
    return sources.Capacity(fetched_at=time.time(), **fields)


def _degraded_payload():
    """The documented degraded shape: config null, every `now` field null with
    ledger 'unknown', a null history tail, and a day nobody waited on. Every
    null must survive as unknown, never as a zero."""
    return {
        "success": True,
        "generated_at": "2026-10-24T00:05:14.458609995Z",
        "data": {
            "now": {"limit": None, "in_use": None, "available": None,
                    "saturated": None, "ledger": "unknown", "sampled_at": None},
            "config": None,
            "history": {"as_of": "2026-10-22",
                        "last_7d": {"avg_lanes": None, "days_active": 5,
                                    "days_covered": 6, "lane_hours": None,
                                    "p95_lanes": None, "peak_lanes": None,
                                    "queued_pct": None},
                        "last_30d": None,
                        "rollup_computed_at": "2026-10-23T15:19:01.958669Z",
                        "tail": None},
            "admission": {"as_of": "2026-10-23",
                          "last_7d": [{"accepted": 10, "avg_queue_wait_ms": None,
                                       "day": "2026-10-22", "deferred": 0,
                                       "denied": 0, "denied_after_wait": 0}],
                          "last_30d": []},
        },
    }


def _metrics(**over):
    """What analyse() produces, reduced to the keys the verdict reads."""
    m = {
        "window_hours": 24,
        "requests": 17,
        "by_outcome": {"completed": 14, "admission_timeout": 3},
        "by_tier": {"interactive": 15, "background": 2},
        "by_harness": {"pi": 17},
        "latency_s": {"interactive": {"n": 12, "p50": 95.0, "p90": 240.0, "max": 300.0},
                      "background": {"n": 2, "p50": 400.0, "p90": 520.0, "max": 600.0}},
        "peak_concurrency": 17,
        "peak_at": "2026-11-02T23:00:00+00:00",
        "peak_by_harness": {"pi": 17},
        "admission_conversion": {
            "interactive": {"completed": 12, "admission_timeout": 3, "admitted_pct": 80.0},
            "background": {"completed": 2, "admission_timeout": 0, "admitted_pct": 100.0}},
        "saturated": 4,
        "starved_peer_evidence": 0,
        "pressure_windows": 1,
        "longest_pressure_window": {"seconds": 900.0, "max_concurrent": 17},
        "timeout_storms": [],
        "per_model_deadlines": {},
        "unknown_errors": [],
        "saturation_bands": {},
        "declared_plan": None,
        "by_minute": [],
    }
    m.update(over)
    return m


_SPARK_START = "2026-11-02T00:00Z"


def _by_minute(pairs, start=_SPARK_START):
    """by_minute entries from (minute offset, max_concurrent) pairs."""
    base = datetime.strptime(start, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    return [{"minute": (base + timedelta(minutes=offset)).strftime("%Y-%m-%dT%H:%MZ"),
             "max_concurrent": value, "started": value, "admission_deaths": 0}
            for offset, value in pairs]


def _scan_of(pairs, **over):
    """Metrics whose 24h window closes at _SPARK_START plus 24h, holding these minutes."""
    base = datetime.strptime(_SPARK_START, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    return _metrics(by_minute=_by_minute(pairs),
                    scanned_at=base.timestamp() + 24 * 3600, **over)


# ---- (a) the six answers steer the lead panel -------------------------------
class TriageChoices(unittest.TestCase):
    """The six on-screen answers, in the order the design draws them."""

    def test_the_six_answers_map_to_their_lead_panels(self):
        self.assertEqual(
            ["failing", "slow", "finish_reason", "concurrency", "stuck", "checking"],
            [key for _text, key in tui.TRIAGE_CHOICES])
        self.assertEqual(
            ["admission", "latency", "stuck", "admission", "stuck", "overview"],
            [tui.pick_lead(key) for _text, key in tui.TRIAGE_CHOICES])

    def test_concurrency_and_stuck_lead_their_own_panels(self):
        self.assertEqual("admission", tui.pick_lead("concurrency"))
        self.assertEqual("stuck", tui.pick_lead("stuck"))

    def test_finish_reason_leads_the_idle_budget_diagnosis(self):
        """The spec binds "stream ended without finish_reason" to its existing
        diagnosis path, the client_idle_timeout class, not to admission."""
        self.assertEqual("stuck", tui.pick_lead("finish_reason"))

    def test_every_answer_has_a_visible_label(self):
        for text, _key in tui.TRIAGE_CHOICES:
            self.assertTrue(text.strip())


# ---- (b) three panels in ranked-trust order --------------------------------
class RankedTrustOrder(unittest.TestCase):
    def setUp(self):
        self.cap = _cap(_payload("capacity-2026-11-02.json"))
        self.metrics = _metrics()

    def test_the_rows_run_account_then_local_then_verdict(self):
        account = tui.account_rows(self.metrics, self.cap)
        local = tui.local_rows("failing", self.metrics)
        verdict = tui.verdict_rows(self.metrics)
        self.assertTrue(account and local and verdict)
        self.assertEqual(account + local + verdict,
                         tui.build_verdict_rows("failing", self.metrics, self.cap))

    def test_the_account_width_is_authoritative(self):
        rows = [row for row in tui.account_rows(self.metrics, self.cap)
                if "priority concurrency" in row[1]]
        self.assertEqual(1, len(rows))
        self.assertEqual(badges.BADGE_ACCOUNT, rows[0][0])
        self.assertIn("12", rows[0][1])

    def test_the_local_peak_is_a_lower_bound(self):
        rows = [row for row in tui.local_rows("concurrency", self.metrics)
                if "peak" in row[1]]
        self.assertEqual(1, len(rows))
        self.assertEqual(badges.BADGE_MEASURED, rows[0][0])
        self.assertIn("lower bound", rows[0][1])

    def test_the_verdict_row_is_the_cli_verdict_not_a_second_one(self):
        head, _adjustments, _code = cli.verdict(_metrics())
        first = tui.verdict_rows(self.metrics)[0]
        self.assertIn(head, first[1], "the verdict line repeats the CLI's own headline")
        self.assertEqual("", first[0],
                         "a verdict is a conclusion, so it wears no source badge")

    def test_the_verdict_row_leads_with_the_status_moon_the_cli_earns(self):
        """One status scale on both surfaces: the moon comes from the same
        helper the pretty renderer asks."""
        head, _adjustments, code = cli.verdict(_metrics())
        rows = tui.verdict_rows(self.metrics)
        glyph = tui._GLYPH_STYLE.symbol(report.status_moon(code, head))
        self.assertEqual(f"{glyph} {head}", rows[0][1])
        self.assertEqual("\U0001F311", glyph,
                         "3 blocked admissions earns the new moon")

    def test_the_legend_is_its_own_line_not_a_row_of_the_bold_panel(self):
        """The panel is bold and the legend is a key, so the screen prints the
        legend at the pretty renderer's quiet weight: its own muted line."""
        legend = tui.status_legend(_metrics())
        for phase in ("\U0001F315", "\U0001F317", "\U0001F311"):
            self.assertIn(phase, legend)
        self.assertNotIn("healthy", " ".join(text for _b, text in
                                             tui.verdict_rows(self.metrics)))

    def test_a_verdict_that_claims_no_status_prints_no_legend(self):
        empty = _metrics(requests=0, by_outcome={}, by_tier={}, by_harness={},
                         pressure_windows=0, longest_pressure_window=None)
        self.assertEqual("", tui.status_legend(empty))


# ---- (c) the degraded payload: every null is unknown ----------------------
class DegradedPayload(unittest.TestCase):
    """config: null and a null `now` block. The account panel says whose
    silence it is, and no field the payload nulled reads as a zero."""

    def setUp(self):
        self.cap = _cap(_degraded_payload())
        self.rows = tui.account_rows(_metrics(), self.cap)

    def test_the_width_says_the_account_did_not_report_it(self):
        width = [text for _badge, text in self.rows if "priority concurrency" in text]
        self.assertTrue(width)
        self.assertIn("not reported by the account", width[0])

    def test_the_occupancy_row_does_not_look_authoritative(self):
        occupancy = [row for row in self.rows
                     if "occupancy" in row[1] or "in use" in row[1]]
        self.assertTrue(occupancy)
        for badge, text in occupancy:
            self.assertNotEqual(badges.BADGE_ACCOUNT, badge,
                                f"a null occupancy wore the account badge: {text!r}")

    def test_no_null_field_renders_as_a_zero_or_a_none(self):
        for _badge, text in self.rows:
            self.assertNotIn("None", text)
            self.assertNotRegex(text, r"(?<![\w.])0(?![\w.])",
                                f"a null became a zero: {text!r}")

    def test_a_missing_payload_says_the_account_did_not_answer(self):
        rows = tui.account_rows(_metrics(), None)
        self.assertEqual(1, len(rows))
        self.assertEqual(badges.badge_for(sources.PROV_UNKNOWN), rows[0][0])
        self.assertIn("the account did not answer", rows[0][1])

    def test_a_declared_plan_still_reaches_a_machine_with_no_payload(self):
        """--plan exists so the ◌ declared width can appear at the front door,
        so a missing capacity payload must not hide the declaration."""
        rows = tui.account_rows({"declared_plan": "max"}, None)
        self.assertIn("the account did not answer", rows[0][1])
        declared = [row for row in rows if row[0] == badges.BADGE_DECLARED]
        self.assertEqual(1, len(declared))
        self.assertIn("priority concurrency 4", declared[0][1])
        self.assertIn("declared", declared[0][1])
        self.assertIn("plan max", declared[0][1])


# ---- (d) an empty window says what is missing ------------------------------
class EmptyWindow(unittest.TestCase):
    def test_the_verdict_says_what_is_missing_rather_than_a_number(self):
        metrics = _metrics(requests=0, by_outcome={}, by_tier={}, by_harness={},
                           saturated=0, pressure_windows=0,
                           longest_pressure_window=None)
        rows = tui.verdict_rows(metrics)
        low = " ".join(text for _badge, text in rows).lower()
        self.assertIn("no request", low)
        self.assertIn("window", low)
        self.assertNotIn("peak", low, "nothing was measured, so no peak is quoted")


# ---- (e) "agent seems stuck" names the idle-timeout class ------------------
class StuckAnswer(unittest.TestCase):
    def test_the_stuck_answer_names_the_idle_timeout_outcome(self):
        metrics = _metrics(by_outcome={"completed": 12, "client_idle_timeout": 3},
                           requests=15)
        rows = tui.local_rows("stuck", metrics)
        named = [text for _badge, text in rows if "client_idle_timeout" in text]
        self.assertTrue(named, "the stuck answer must name the class it reads")
        self.assertIn("3", named[0])

    def test_a_window_without_a_stall_says_so(self):
        rows = tui.local_rows("stuck", _metrics(by_outcome={"completed": 17}))
        named = [text for _badge, text in rows if "client_idle_timeout" in text]
        self.assertTrue(named)
        self.assertIn("no request", named[0])

    def test_the_stuck_answer_adds_one_slowest_request_row(self):
        """The builder-check decision binds "agent seems stuck" to the
        explain --slowest panel as well, so one bounded row carries that
        request's own why - not the whole panel."""
        metrics = _metrics(slowest_request={
            "duration": "312.4s", "outcome": "client_idle_timeout",
            "headline": "The stream stopped without a finish_reason."})
        rows = [row for row in tui.local_rows("stuck", metrics)
                if "slowest request" in row[1]]
        self.assertEqual(1, len(rows))
        self.assertEqual(badges.BADGE_MEASURED, rows[0][0])
        self.assertIn("312.4s", rows[0][1])
        self.assertIn("client_idle_timeout", rows[0][1])
        self.assertIn("finish_reason", rows[0][1])

    def test_a_window_with_nothing_to_rank_says_so(self):
        rows = [row for row in tui.local_rows("stuck", _metrics())
                if "slowest" in row[1]]
        self.assertEqual(1, len(rows))
        self.assertIn("not established (unknown)", rows[0][1])

    def test_an_unranked_row_claims_no_cause_it_did_not_establish(self):
        """A digest that is None may be an explain that raised, not an empty
        window: the row states that nothing was ranked, never why."""
        with mock.patch.object(tui.explain, "slowest", side_effect=RuntimeError("boom")):
            digest = tui.slowest_request_digest(["req"], _metrics())
        self.assertIsNone(digest)
        rows = [row for row in tui.local_rows("stuck", _metrics(slowest_request=digest))
                if "slowest" in row[1]]
        self.assertEqual(1, len(rows))
        self.assertNotIn("usable interval", rows[0][1])
        self.assertNotIn("no request", rows[0][1])
        self.assertIn("not established (unknown)", rows[0][1])

    def test_the_slowest_digest_uses_the_explain_machinery(self):
        """One digest, from explain's own selectors: nothing here re-derives a
        reason from the metrics."""
        doc = {"headline": "The stream stopped without a finish_reason.",
               "rows": [("request", "pi/abc"), ("duration", "312.4s")],
               "actions": [], "outcome": "client_idle_timeout", "room": True}
        with mock.patch.object(tui.explain, "slowest", return_value=["req"]) as slow, \
                mock.patch.object(tui.explain, "explain", return_value=doc):
            digest = tui.slowest_request_digest(["req"], _metrics())
        self.assertEqual({"duration": "312.4s", "outcome": "client_idle_timeout",
                          "headline": "The stream stopped without a finish_reason."},
                         digest)
        slow.assert_called_once_with(["req"], 1)

    def test_the_slowest_digest_is_none_without_requests_or_on_failure(self):
        self.assertIsNone(tui.slowest_request_digest([], _metrics()))
        with mock.patch.object(tui.explain, "slowest", side_effect=RuntimeError("boom")):
            self.assertIsNone(tui.slowest_request_digest(["req"], _metrics()))


# ---- (e2) "things are slow" reports the account's queue wait ----------------
class SlowAnswer(unittest.TestCase):
    def _rows(self, metrics):
        return [row for row in tui.local_rows("slow", metrics)
                if "queue wait" in row[1]]

    def test_the_latest_complete_day_carries_the_queue_wait(self):
        metrics = _metrics(capacity={"provenance": "account", "window_days": [
            {"day": "2026-10-31", "avg_queue_wait_ms": 4200.0},
            {"day": "2026-11-01", "avg_queue_wait_ms": 181478.12}]})
        rows = self._rows(metrics)
        self.assertEqual(1, len(rows))
        self.assertEqual(badges.BADGE_ACCOUNT, rows[0][0],
                         "the queue wait is the account's own measure")
        self.assertIn("2026-11-01", rows[0][1])
        self.assertIn("181.5s", rows[0][1])

    def test_a_day_nobody_waited_says_so_rather_than_a_zero(self):
        metrics = _metrics(capacity={"provenance": "account", "window_days": [
            {"day": "2026-11-01", "avg_queue_wait_ms": None}]})
        rows = self._rows(metrics)
        self.assertIn("no waiting that day", rows[0][1])
        self.assertNotIn("0.0s", rows[0][1])

    def test_without_an_account_day_the_queue_wait_is_unknown(self):
        rows = self._rows(_metrics())
        self.assertEqual(1, len(rows))
        self.assertEqual(badges.badge_for(sources.PROV_UNKNOWN), rows[0][0])
        self.assertIn("unknown", rows[0][1])

    def test_the_latency_rows_still_carry_the_local_percentiles(self):
        rows = tui.local_rows("slow", _metrics())
        self.assertTrue(any("interactive latency" in text for _badge, text in rows))


# ---- (f) the occupancy line is qualified by the ledger --------------------
class LedgerQualifiedOccupancy(unittest.TestCase):
    def test_a_ledger_that_is_not_up_makes_the_count_advisory(self):
        payload = _payload("capacity-2026-11-02.json")
        payload["data"]["now"]["ledger"] = "degraded"
        rows = tui.account_rows(_metrics(), _cap(payload))
        occupancy = [row for row in rows if "priority pool" in row[1]]
        self.assertEqual(1, len(occupancy))
        badge, text = occupancy[0]
        self.assertIn("degraded", text)
        self.assertIn("may be stale", text)
        self.assertNotEqual(badges.BADGE_ACCOUNT, badge,
                            "an advisory count must not wear the authoritative badge")

    def test_an_unread_sample_time_downgrades_the_pool_row(self):
        """The row prints "sampled at unknown", so it is not the account's word."""
        payload = _payload("capacity-2026-11-02.json")
        payload["data"]["now"]["sampled_at"] = None
        rows = [row for row in tui.account_rows(_metrics(), _cap(payload))
                if "priority pool" in row[1]]
        self.assertEqual(1, len(rows))
        self.assertIn("sampled at unknown", rows[0][1])
        self.assertNotEqual(badges.BADGE_ACCOUNT, rows[0][0],
                            "a row with an unread sample time wore the account badge")

    def test_a_healthy_ledger_keeps_the_authoritative_row(self):
        rows = tui.account_rows(_metrics(), _cap(_payload("capacity-2026-11-02.json")))
        occupancy = [row for row in rows if "priority pool" in row[1]]
        self.assertEqual(1, len(occupancy))
        self.assertEqual(badges.BADGE_ACCOUNT, occupancy[0][0])
        self.assertNotIn("may be stale", occupancy[0][1])


# ---- (g) the overflow pool's two null shapes stay apart -------------------
class OverflowPoolShapes(unittest.TestCase):
    def _overflow(self, background):
        payload = _payload("capacity-2026-11-02.json")
        payload["data"]["now"]["background"] = background
        rows = [row for row in tui.account_rows(_metrics(), _cap(payload))
                if "overflow" in row[1]]
        self.assertTrue(rows, "the account panel must render the overflow pool")
        return rows[0]

    def test_an_unread_cap_is_unknown_not_a_closed_pool(self):
        _badge, text = self._overflow({"limit": None, "in_use": None,
                                       "available": None, "saturated": None})
        self.assertIn("cap unknown", text)
        self.assertNotIn("entitled to no overflow", text)

    def test_a_zero_cap_is_no_entitlement_and_never_saturated(self):
        badge, text = self._overflow({"limit": 0, "in_use": 0,
                                      "available": 0, "saturated": False})
        self.assertIn("entitled to no overflow", text)
        self.assertIn("never saturated", text)
        self.assertNotIn("cap unknown", text)
        self.assertEqual(badges.BADGE_ACCOUNT, badge,
                         "a closed pool rests on its cap, so it stays authoritative")

    def test_the_two_null_shapes_are_two_different_strings(self):
        unknown = self._overflow({"limit": None, "in_use": None,
                                  "available": None, "saturated": None})[1]
        closed = self._overflow({"limit": 0, "in_use": 0,
                                 "available": 0, "saturated": False})[1]
        self.assertNotEqual(unknown, closed)

    def test_a_real_cap_with_an_unread_count_keeps_the_cap(self):
        badge, text = self._overflow({"limit": 12, "in_use": None,
                                      "available": None, "saturated": None})
        self.assertIn("unknown (not reported)", text)
        self.assertIn("12", text)
        self.assertEqual(badges.badge_for(sources.PROV_UNKNOWN), badge,
                         "a cap beside a count nobody could read is not authoritative")

    def test_an_unread_count_is_not_authoritative_in_either_pool(self):
        """One rule for both pools: the same fact shape reads the same way."""
        payload = _payload("capacity-2026-11-02.json")
        payload["data"]["now"].update({
            "in_use": None, "available": None, "saturated": None,
            "background": {"limit": 12, "in_use": None,
                           "available": None, "saturated": None}})
        rows = tui.account_rows(_metrics(), _cap(payload))
        priority = [row for row in rows if "priority pool" in row[1]]
        overflow = [row for row in rows if "overflow" in row[1]]
        self.assertEqual(1, len(priority))
        self.assertEqual(1, len(overflow))
        self.assertEqual(badges.badge_for(sources.PROV_UNKNOWN), overflow[0][0])
        self.assertEqual(priority[0][0], overflow[0][0])

    def test_a_reported_pool_shows_its_count_and_its_flag(self):
        _badge, text = self._overflow({"limit": 12, "in_use": 0,
                                       "available": 12, "saturated": False})
        self.assertIn("in use 0 of 12 concurrent requests", text)
        self.assertIn("saturated: no", text)

    def test_an_absent_block_uses_the_capacity_reports_own_words(self):
        """A payload captured before the account reported the pool has no such
        block: the row is the capacity report's sentence, not a second one."""
        payload = _payload("capacity-2026-10-23.json")
        rows = [row for row in tui.account_rows(_metrics(), _cap(payload))
                if "overflow" in row[1]]
        self.assertEqual(1, len(rows))
        self.assertEqual(badges.badge_for(sources.PROV_UNKNOWN), rows[0][0])
        self.assertEqual("overflow pool: " + cli._cap_pool_now(None), rows[0][1])


# ---- the priority pool's own saturated flag is an observation -------------
class PriorityPoolObservation(unittest.TestCase):
    def test_a_saturated_sample_keeps_its_flag_when_the_count_is_unread(self):
        payload = _payload("capacity-2026-11-02.json")
        payload["data"]["now"].update({"in_use": None, "available": None,
                                        "saturated": True})
        rows = [row for row in tui.account_rows(_metrics(), _cap(payload))
                if "priority pool" in row[1]]
        self.assertEqual(1, len(rows))
        badge, text = rows[0]
        self.assertIn("in use unknown (not reported)", text)
        self.assertIn("saturated: yes", text)
        self.assertNotIn("denied", text.lower(), "an observation, never a refusal")
        self.assertNotIn("refus", text.lower())
        self.assertNotEqual(badges.BADGE_ACCOUNT, badge,
                            "an unread count is not authoritative")

    def test_a_healthy_sample_reports_the_flag_as_no(self):
        rows = [row for row in tui.account_rows(
                    _metrics(), _cap(_payload("capacity-2026-11-02.json")))
                if "priority pool" in row[1]]
        self.assertIn("saturated: no", rows[0][1])


# ---- (h) refusal words come only from admission counts -------------------
class RefusalWordsComeFromAdmissionCounts(unittest.TestCase):
    def test_a_full_pool_alone_is_never_a_refusal(self):
        payload = _payload("capacity-2026-11-02.json")
        payload["data"]["now"].update({
            "in_use": 12, "available": 0, "saturated": True,
            "background": {"limit": 12, "in_use": 12, "available": 0,
                           "saturated": True}})
        payload["data"]["admission"] = None
        rows = tui.account_rows(_metrics(), _cap(payload))
        for _badge, text in rows:
            low = text.lower()
            self.assertNotIn("denied", low, text)
            self.assertNotIn("refus", low, text)

    def test_the_admission_counts_are_where_the_refusal_wording_comes_from(self):
        rows = [row for row in tui.account_rows(
                    _metrics(), _cap(_payload("capacity-2026-11-02.json")))
                if "admission" in row[1]]
        self.assertTrue(rows)
        self.assertTrue(any("denied after waiting" in text for _badge, text in rows))


# ---- the app shell: no terminal needed to check the contract -------------
class TriageAppSurface(unittest.TestCase):
    def test_the_app_takes_a_plan_and_an_online_flag(self):
        app = tui.TriageApp(plan="apex", online=False)
        self.assertEqual("apex", app.plan)
        self.assertFalse(app.online)

    def test_fix_and_quit_are_bound(self):
        keys = set()
        for binding in tui.TriageApp.BINDINGS:
            key = getattr(binding, "key", None)
            if key is None and isinstance(binding, (tuple, list)):
                key = binding[0]
            keys.add(key)
        self.assertTrue({"f", "q"} <= keys, keys)

    def test_run_triage_runs_the_app_and_returns_zero(self):
        with mock.patch.object(tui.TriageApp, "run", return_value=None) as run:
            self.assertEqual(0, tui.run_triage(plan=None, online=False))
        self.assertEqual(1, run.call_count)

    def test_online_refreshes_the_cache_once_before_the_screen_opens(self):
        with mock.patch.object(tui.sources, "fetch_capacity", return_value=None) as fetch, \
                mock.patch.object(tui.TriageApp, "run", return_value=None):
            tui.run_triage(plan="apex", online=True)
        fetch.assert_called_once_with(allow_network=True)

    def test_the_scan_reads_the_capacity_cache_only(self):
        with mock.patch.object(tui.cli, "collect", return_value=[]), \
                mock.patch.object(tui.cli, "analyse", return_value={}), \
                mock.patch.object(tui.cli, "_capacity_metrics",
                                  return_value={}) as metrics_block, \
                mock.patch.object(tui.sources, "fetch_capacity", return_value=None) as fetch:
            metrics, cap = tui.scan(plan="apex")
        fetch.assert_called_once_with(allow_network=False)
        self.assertIs(False, metrics_block.call_args.kwargs.get("allow_network"),
                      "the CLI's capacity block is read cache-only too")
        self.assertEqual("apex", metrics["declared_plan"])
        self.assertIsNone(cap)


class TriageAppFailure(unittest.TestCase):
    """A failed scan must not be reported as a scan still in progress."""

    def test_a_failed_scan_is_never_reported_as_still_running(self):
        asyncio.run(self._drive())

    async def _drive(self):
        app = tui.TriageApp()
        with mock.patch.object(tui, "scan", side_effect=RuntimeError("boom")):
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause()
                await pilot.pause()
                self.assertEqual("RuntimeError: boom", app.scan_failed)
                self.assertIn("the scan failed", self._status(app))
                await pilot.press("enter")
                await pilot.pause()
                self.assertIn("the scan failed", self._status(app))
                self.assertNotIn("still running", self._status(app))
                await pilot.press("f")
                await pilot.pause()
                self.assertIn("the scan failed", self._status(app))
                self.assertNotIn("still running", self._status(app))
                await pilot.press("q")

    @staticmethod
    def _status(app) -> str:
        return str(app.query_one("#status").render())


class TheStatusMoonOnTheScreen(unittest.TestCase):
    """The moon and its legend reach the screen, and the legend stays quiet.

    The verdict panel renders bold; the legend is a key to a scale, not a
    conclusion, so it renders as its own muted line under that panel - the
    weight the pretty renderer paints it in.
    """

    def test_the_legend_renders_below_the_panel_not_inside_it(self):
        asyncio.run(self._drive())

    async def _drive(self):
        app = tui.TriageApp()
        with mock.patch.object(tui, "scan",
                               return_value=(_metrics(),
                                             _cap(_payload("capacity-2026-11-02.json")))):
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause()
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                legend = str(app.query_one("#moon-legend").render())
                panel = str(app.query_one("#verdict-line").render())
                self.assertIn("healthy", legend)
                self.assertIn("\U0001F311", legend)
                self.assertNotIn("healthy", panel,
                                 "the key was drawn in the panel's bold weight")
                self.assertTrue(app.query_one("#moon-legend").display)
                await pilot.press("q")

    def test_a_verdict_with_no_status_hides_the_legend_line(self):
        asyncio.run(self._drive_no_status())

    async def _drive_no_status(self):
        empty = _metrics(requests=0, by_outcome={}, by_tier={}, by_harness={},
                         pressure_windows=0, longest_pressure_window=None)
        app = tui.TriageApp()
        with mock.patch.object(tui, "scan", return_value=(empty, None)):
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause()
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                self.assertFalse(app.query_one("#moon-legend").display,
                                 "nothing measured, so no scale was used")
                await pilot.press("q")


class EverySlowStepSaysItIsRunning(unittest.TestCase):
    """The owner's rule: every surface that can sit for a second or more shows
    activity first. The scan says so on mount, the fix says so before it runs,
    and the one capacity fetch that happens before the screen exists says so on
    the terminal it is about to replace."""

    def test_the_fix_sets_the_status_before_it_starts_working(self):
        asyncio.run(self._drive())

    async def _drive(self):
        app = tui.TriageApp()
        computing, release = threading.Event(), threading.Event()

        def slow_fix(plan=None):
            # the worker is inside the fix, and the screen must already say so
            computing.set()
            release.wait(5)
            return "fix output"

        with mock.patch.object(tui, "scan", return_value=(_metrics(), _cap(_payload("capacity-2026-11-02.json")))), \
                mock.patch.object(tui, "fix_output", side_effect=slow_fix):
            async with app.run_test(size=(100, 40)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("f")
                for _attempt in range(40):
                    await pilot.pause(0.05)
                    if computing.is_set():
                        break
                self.assertTrue(computing.is_set(), "the fix never started")
                status = str(app.query_one("#status").render())
                self.assertIn("rendering the fix output", status)
                self.assertNotIn("[q] quits", status,
                                 "the screen still offered the previous action, not the work")
                release.set()
                await self._settle(pilot, lambda: "fix output" in str(app.query_one("#detail-text").render()))
                await pilot.press("q")

    def test_the_capacity_refresh_before_the_screen_says_so_first(self):
        events = []
        with mock.patch.object(tui.cli, "notice",
                               side_effect=lambda message: events.append(("notice", message))), \
                mock.patch.object(tui.sources, "fetch_capacity",
                                  side_effect=lambda **kw: events.append(("fetch", kw))), \
                mock.patch.object(tui.TriageApp, "run", return_value=None):
            tui.run_triage(plan=None, online=True)
        self.assertEqual("notice", events[0][0],
                         "the refresh began with no word on the terminal")
        self.assertIn("capacity", events[0][1])
        self.assertEqual("fetch", events[1][0])

    def test_a_cache_only_screen_refresh_prints_nothing(self):
        events = []
        with mock.patch.object(tui.cli, "notice",
                               side_effect=lambda message: events.append(message)), \
                mock.patch.object(tui.TriageApp, "run", return_value=None):
            tui.run_triage(plan=None, online=False)
        self.assertEqual([], events, "an offline run has no wait to excuse")

    @staticmethod
    async def _settle(pilot, ready) -> None:
        """Wait for the thread worker's result, bounded, so a hang fails loudly."""
        for _attempt in range(40):
            await pilot.pause(0.05)
            if ready():
                return


class TriageAppLiteralText(unittest.TestCase):
    """Bracketed text on this screen is prose, never Rich markup.

    The status line repeats the CLI's words, and those name keys as [f] and
    [q]; the detail panel prints a command's own output, and the fix block
    may name a tier as [agents]. Parsed as markup, the brackets and what they
    hold vanish, so the screen tells a reader to press nothing.
    """

    FIX = "run `luna-doctor fix --tier [agents]`, then press [f] again"

    def test_bracketed_prose_survives_the_status_line_and_the_detail_panel(self):
        asyncio.run(self._drive())

    async def _drive(self):
        app = tui.TriageApp()
        metrics = _metrics()
        cap = _cap(_payload("capacity-2026-11-02.json"))
        with mock.patch.object(tui, "scan", return_value=(metrics, cap)), \
                mock.patch.object(tui, "fix_output", return_value=self.FIX):
            async with app.run_test(size=(100, 40)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                await pilot.press("enter")
                await pilot.pause()
                status = self._text(app, "#status")
                self.assertIn("[f] shows the fix", status)
                self.assertIn("[q] quits", status)
                await pilot.press("f")
                await self._settle(pilot, lambda: "[agents]" in self._text(app, "#detail-text"))
                self.assertIn("[agents]", self._text(app, "#detail-text"))
                self.assertIn("--tier [agents]", self._text(app, "#detail-text"))
                self.assertIn("[q] quits", self._text(app, "#status"))
                await pilot.press("q")

    @staticmethod
    async def _settle(pilot, ready) -> None:
        """Wait for the thread worker's result, bounded, so a hang fails loudly."""
        for _attempt in range(40):
            await pilot.pause(0.05)
            if ready():
                return

    @staticmethod
    def _text(app, selector) -> str:
        return str(app.query_one(selector).render())


# ---- the dashboard's density: four rows a panel, and no more --------------
class MediumDensityPanels(unittest.TestCase):
    """The screen orients. `analyze`, `explain` and `capacity` enumerate."""

    def setUp(self):
        self.cap = _cap(_payload("capacity-2026-11-02.json"))

    def _busy(self):
        """The busiest window this fixture can make: every lead panel full."""
        return _metrics(
            requests=12,
            by_outcome={"completed": 4, "admission_timeout": 3, "rate_limited": 2,
                        "org_concurrency_limit": 1, "client_idle_timeout": 2},
            by_tier={"interactive": 10, "background": 2},
            saturated=4,
            capacity={"provenance": "account", "window_days": [
                {"day": "2026-11-01", "avg_queue_wait_ms": 4200.0}]},
            saturation_bands={"bands": [{"band": "8-12", "n": 3, "p50": 1.0,
                                         "p90": 300.0}],
                              "mid_p90_baseline_s": 60.0, "top_inflation": 5.0,
                              "top_inflation_flagged": True},
            slowest_request={"duration": "312.4s", "outcome": "client_idle_timeout",
                             "headline": "The stream stopped without a finish_reason."},
        )

    def test_no_panel_holds_more_than_four_rows(self):
        for _text, key in tui.TRIAGE_CHOICES:
            with self.subTest(answer=key):
                self.assertLessEqual(len(tui.account_rows(self._busy(), self.cap)), 4)
                self.assertLessEqual(len(tui.local_rows(key, self._busy())), 4)
        self.assertLessEqual(len(tui.verdict_rows(self._busy())), 4)

    def test_the_account_panel_folds_the_ledger_into_the_pool_row(self):
        rows = tui.account_rows(_metrics(), self.cap)
        self.assertEqual(4, len(rows), "width, pool, overflow, admission")
        pool = [text for _badge, text in rows if "priority pool" in text]
        self.assertEqual(1, len(pool))
        self.assertIn("sampled at", pool[0], "the sample time kept a row of its own")

    def test_both_latency_tiers_share_one_row(self):
        rows = tui.local_rows("slow", _metrics())
        tiers = [text for _badge, text in rows if "latency" in text]
        self.assertEqual(1, len(tiers))
        self.assertIn("interactive latency", tiers[0])
        self.assertIn("background latency", tiers[0])

    def test_the_pressure_classes_share_one_row(self):
        rows = tui.local_rows("failing", self._busy())
        named = [text for _badge, text in rows if "rate_limit_error" in text]
        self.assertEqual(1, len(named))
        self.assertIn("scheduler admission timeout", named[0])

    def test_the_verdict_panel_holds_three_lines_on_a_busy_window(self):
        """One sentence, the action and one pointer: the report is `[d]`.

        This fixture carries three adjustments, about a thousand characters of
        the CLI's prose that the panel used to fold into one sprawling row."""
        rows = tui.verdict_rows(_metrics())
        self.assertLessEqual(len(rows), 3, rows)
        for _badge, text in rows:
            self.assertLessEqual(len(text), 120, f"a row that wraps into a wall: {text!r}")
        prose = " ".join(text for _badge, text in rows)
        self.assertNotIn("the right home for", prose,
                         "the adjustments are the report's job, not the panel's")

    def test_the_verdict_panel_states_one_sentence_of_the_cli_verdict(self):
        metrics = _metrics()
        headline, adjustments, code = cli.verdict(metrics)
        self.assertTrue(adjustments, "this fixture carries prose beyond the headline")
        glyph = tui._GLYPH_STYLE.symbol(report.status_moon(code, headline))
        self.assertEqual(f"{glyph} {headline}", tui.verdict_rows(metrics)[0][1],
                         "the panel states the headline and none of the adjustments")

    def test_the_verdict_panel_ends_with_one_pointer_to_the_report(self):
        rows = tui.verdict_rows(self._busy())
        self.assertEqual("", rows[-1][0], "a pointer to the report is no one's number")
        self.assertEqual("details: press d for the full analyze report", rows[-1][1])
        self.assertEqual(1, sum("press d" in text for _badge, text in rows),
                         "one pointer, not two")


# ---- the sparkline: the window's shape, re-binned ------------------------
class ConcurrencySparkline(unittest.TestCase):
    """by_minute is sparse, so the row is re-binned onto uniform columns."""

    def test_sparse_minutes_are_rebinned_onto_uniform_columns(self):
        spark = tui.concurrency_sparkline(_scan_of([(0, 2), (720, 9)]), width=4)
        self.assertEqual(4, len(spark))
        self.assertNotEqual(" ", spark[0])
        self.assertEqual(" ", spark[1], "an empty column is a space, not a low bar")
        self.assertEqual("\u2588", spark[2], "12h later lands in the third column")
        self.assertEqual(" ", spark[3])

    def test_a_minute_before_the_window_is_not_clamped_into_column_zero(self):
        spark = tui.concurrency_sparkline(_scan_of([(-120, 17), (600, 4)]), width=4)
        self.assertEqual(" ", spark[0],
                         "a minute outside the window is not this window's traffic")
        self.assertNotEqual(" ", spark[1])

    def test_the_busiest_minute_inside_a_column_is_the_column(self):
        spark = tui.concurrency_sparkline(_scan_of([(0, 2), (10, 9)]), width=2)
        self.assertEqual("\u2588 ", spark)

    def test_a_quiet_window_draws_blanks_not_low_bars(self):
        pairs = [(minute, 17) for minute in range(1400, 1440, 5)]
        spark = tui.concurrency_sparkline(_scan_of(pairs), width=12)
        self.assertEqual(" " * 11, spark[:11], "nothing ran there, so nothing is drawn")
        self.assertNotEqual(" ", spark[11])

    def test_a_dense_window_fills_the_ramp(self):
        pairs = [(minute, level) for level, minute in
                 enumerate(range(0, 1440, 120), start=1)]
        spark = tui.concurrency_sparkline(_scan_of(pairs), width=12)
        self.assertNotIn(" ", spark)
        self.assertEqual("\u2581", spark[0])
        self.assertEqual("\u2588", spark[-1])
        levels = [tui._SPARK_BLOCKS.index(char) for char in spark]
        self.assertEqual(sorted(levels), levels, "the ramp only goes up here")
        self.assertGreaterEqual(len(set(spark)), 4, "the ramp is not two heights")

    def test_the_window_closes_at_the_scans_clock(self):
        """Activity from 22h ago stays at the left, where it happened. Closing
        the window on the last active minute instead would draw it at the
        right edge."""
        spark = tui.concurrency_sparkline(_scan_of([(0, 5), (20, 17)]), width=12)
        self.assertEqual(" " * 11, spark[1:])
        self.assertNotEqual(" ", spark[0])

    def test_without_a_scan_clock_the_window_ends_at_the_last_active_minute(self):
        spark = tui.concurrency_sparkline(
            _metrics(by_minute=_by_minute([(0, 5)])), width=4)
        self.assertEqual(4, len(spark))
        self.assertEqual(1, sum(char != " " for char in spark))
        self.assertNotEqual(" ", spark[-1])

    def test_the_caption_names_the_peak_the_row_scales_to(self):
        self.assertEqual("concurrency across the window · peak 17",
                         tui.spark_caption(_metrics(peak_concurrency=17)))
        self.assertEqual("concurrency across the window · peak unknown",
                         tui.spark_caption(_metrics(peak_concurrency=None)))

    def test_an_empty_window_draws_nothing_at_all(self):
        self.assertEqual("", tui.concurrency_sparkline(_metrics()))
        self.assertEqual("", tui.spark_text(_metrics()).plain)

    def test_the_text_is_the_row_then_its_caption(self):
        metrics = _scan_of([(0, 17)])
        row, caption = tui.spark_text(metrics, width=8).plain.split("\n")
        self.assertEqual(8, len(row))
        self.assertEqual(tui.spark_caption(metrics), caption)


# ---- the sparkline on the screen ----------------------------------------
class TheSparkOnTheScreen(unittest.TestCase):
    """One quiet line between the evidence panels and the verdict line."""

    def test_the_row_shows_between_the_local_panel_and_the_verdict(self):
        asyncio.run(self._drive())

    async def _drive(self):
        app = tui.TriageApp()
        cap = _cap(_payload("capacity-2026-11-02.json"))
        with mock.patch.object(tui, "scan",
                               return_value=(_scan_of([(0, 17), (600, 4)]), cap)):
            async with app.run_test(size=(100, 40)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                await pilot.press("enter")
                await pilot.pause()
                ids = [child.id for child in app.query_one("#verdict").children]
                self.assertEqual(["account", "local", "spark", "verdict-line",
                                  "moon-legend", "legend"], ids)
                spark = app.query_one("#spark")
                self.assertTrue(spark.display)
                text = str(spark.render())
                self.assertIn("concurrency across the window · peak 17", text)
                self.assertTrue(set(text) & set(tui._SPARK_BLOCKS), text)
                await pilot.press("q")

    def test_a_window_with_no_active_minute_hides_the_line(self):
        asyncio.run(self._drive_quiet())

    async def _drive_quiet(self):
        app = tui.TriageApp()
        with mock.patch.object(tui, "scan", return_value=(_metrics(), None)):
            async with app.run_test(size=(100, 40)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                await pilot.press("enter")
                await pilot.pause()
                self.assertFalse(app.query_one("#spark").display,
                                 "an empty picture is not a shape")
                await pilot.press("q")

    @staticmethod
    async def _settle(pilot, ready) -> None:
        """Wait for the thread worker's result, bounded, so a hang fails loudly."""
        for _attempt in range(40):
            await pilot.pause(0.05)
            if ready():
                return

    @staticmethod
    def _status(app) -> str:
        return str(app.query_one("#status").render())


# ---- [d]: the report the verdict was cut from ---------------------------
class TheDetailOnAKeypress(unittest.TestCase):
    """`[d]` fills the detail panel with the `analyze` report, cache only."""

    def test_the_detail_is_the_analyze_report_offline(self):
        with mock.patch.object(tui.cli, "main", return_value=0) as main:
            text = tui.detail_output("apex", 24)
        argv = main.call_args.args[0]
        self.assertEqual("analyze", argv[0])
        self.assertEqual("24h", argv[argv.index("--since") + 1])
        self.assertIn("--offline", argv)
        self.assertEqual("apex", argv[argv.index("--plan") + 1])
        self.assertEqual("", text)

    def test_d_runs_analyze_and_fills_the_detail_panel(self):
        asyncio.run(self._drive())

    async def _drive(self):
        app = tui.TriageApp(plan="apex")

        def report(argv):
            print("peak concurrency 17")
            return 0

        with mock.patch.object(tui, "scan", return_value=(_metrics(), None)), \
                mock.patch.object(tui.cli, "main", side_effect=report) as main:
            async with app.run_test(size=(100, 40)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("d")
                await self._settle(
                    pilot, lambda: "peak concurrency 17" in self._text(app, "#detail-text"))
                argv = main.call_args.args[0]
                self.assertEqual("analyze", argv[0])
                self.assertIn("--offline", argv)
                self.assertEqual("apex", argv[argv.index("--plan") + 1])
                self.assertTrue(app.query_one("#detail").display)
                self.assertIn("the analyze report is on screen",
                              self._text(app, "#status"))
                await pilot.press("q")

    @staticmethod
    async def _settle(pilot, ready) -> None:
        """Wait for the thread worker's result, bounded, so a hang fails loudly."""
        for _attempt in range(40):
            await pilot.pause(0.05)
            if ready():
                return

    @staticmethod
    def _text(app, selector) -> str:
        return str(app.query_one(selector).render())


# ---- one sentence of prose, then the report ------------------------------
class TheFirstSentence(unittest.TestCase):
    def test_it_keeps_one_sentence_and_drops_the_rest(self):
        self.assertEqual("Blocked at admission.",
                         tui.first_sentence("Blocked at admission. Run `fix` next."))
        self.assertEqual("2 requests peaked at 17.",
                         tui.first_sentence("2 requests peaked at 17. 3 ran on -background."))

    def test_a_decimal_point_does_not_end_a_sentence(self):
        self.assertEqual("The p90 of 240.0s held.",
                         tui.first_sentence("The p90 of 240.0s held. The next one is longer."))

    def test_text_without_a_full_stop_comes_back_whole(self):
        self.assertEqual("no full stop here", tui.first_sentence("  no full stop here  "))
        self.assertEqual("", tui.first_sentence(""))


# ---- [c]: the chart, drawn by the command that owns it -------------------
class TheChartOnAKeypress(unittest.TestCase):
    """The rendered picture stays one keypress away, drawn for this window."""

    def test_the_chart_asks_the_command_for_this_window_offline(self):
        def drawn(argv):
            out = argv[argv.index("--out") + 1]
            with open(out, "w", encoding="utf-8") as fh:
                fh.write("<svg/>")
            with open(os.path.splitext(out)[0] + ".png", "wb") as fh:
                fh.write(b"png")
            return 0

        with mock.patch.object(tui.cli, "main", side_effect=drawn) as main:
            svg, png, error = tui.chart_output("apex", 24)
        self.addCleanup(shutil.rmtree, os.path.dirname(svg), True)
        argv = main.call_args.args[0]
        self.assertEqual("chart", argv[0])
        self.assertEqual("24h", argv[argv.index("--since") + 1])
        self.assertIn("--offline", argv)
        self.assertEqual("apex", argv[argv.index("--plan") + 1])
        self.assertEqual(svg, argv[argv.index("--out") + 1])
        self.assertTrue(svg.endswith("chart.svg"), svg)
        self.assertEqual("", error)
        self.assertTrue(png.endswith("chart.png"), "the PNG the command wrote is offered")

    def test_a_failed_chart_reports_the_error_and_offers_no_file(self):
        def refused(argv):
            print("luna-doctor: cannot write the chart: no space left", file=sys.stderr)
            return 2

        with mock.patch.object(tui.cli, "main", side_effect=refused):
            svg, png, error = tui.chart_output("apex", 24)
        self.addCleanup(shutil.rmtree, os.path.dirname(svg), True)
        self.assertIsNone(png, "a chart nobody wrote is not offered as opened")
        self.assertIn("no space left", error, "the command's own last line comes back")

    def test_a_chart_that_wrote_no_file_is_a_failure_even_at_exit_zero(self):
        with mock.patch.object(tui.cli, "main", return_value=0):
            svg, _png, error = tui.chart_output("apex", 24)
        self.addCleanup(shutil.rmtree, os.path.dirname(svg), True)
        self.assertFalse(os.path.exists(svg))
        self.assertTrue(error, "a path asked for is not a file that exists")

    def test_c_confirms_the_path_it_wrote(self):
        asyncio.run(self._drive())

    async def _drive(self):
        app = tui.TriageApp()
        drawn = ("/tmp/luna-doctor-chart/chart.svg",
                 "/tmp/luna-doctor-chart/chart.png", "")
        with mock.patch.object(tui, "scan", return_value=(_metrics(), None)), \
                mock.patch.object(tui, "chart_output", return_value=drawn) as chart:
            async with app.run_test(size=(100, 40)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("c")
                await self._settle(
                    pilot, lambda: "chart written" in self._status(app))
                status = self._status(app)
                self.assertEqual(1, chart.call_count)
                self.assertIn("chart written: /tmp/luna-doctor-chart/chart.png", status)
                self.assertIn("[q] quits", status)
                await pilot.press("q")

    def test_a_failed_chart_names_the_error_instead_of_a_path(self):
        asyncio.run(self._drive_failure())

    async def _drive_failure(self):
        app = tui.TriageApp()
        failed = ("/tmp/luna-doctor-chart/chart.svg", None,
                  "luna-doctor: cannot write the chart: no space left")
        with mock.patch.object(tui, "scan", return_value=(_metrics(), None)), \
                mock.patch.object(tui, "chart_output", return_value=failed):
            async with app.run_test(size=(100, 40)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("c")
                await self._settle(
                    pilot, lambda: "the chart failed" in self._status(app))
                status = self._status(app)
                self.assertIn("cannot write the chart", status)
                self.assertNotIn("chart written", status,
                                 "a status must not name a file nobody wrote")
                await pilot.press("q")

    @staticmethod
    async def _settle(pilot, ready) -> None:
        """Wait for the thread worker's result, bounded, so a hang fails loudly."""
        for _attempt in range(40):
            await pilot.pause(0.05)
            if ready():
                return

    @staticmethod
    def _status(app) -> str:
        return str(app.query_one("#status").render())
