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
import time
import unittest
from unittest import mock

import pytest

pytest.importorskip("textual")

from luna_doctor import badges, cli, sources, tui

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
    }
    m.update(over)
    return m


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
        self.assertEqual(head, first[1])
        self.assertEqual("", first[0],
                         "a verdict is a conclusion, so it wears no source badge")


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


class TriageAppLiteralText(unittest.TestCase):
    """Bracketed text on this screen is prose, never Rich markup.

    The status line repeats the CLI's words, and those name keys as [f] and
    [q]; the fix panel prints the fix block's own output, which may name a
    tier as [agents]. Parsed as markup, the brackets and what they hold vanish,
    so the screen tells a reader to press nothing.
    """

    FIX = "run `luna-doctor fix --tier [agents]`, then press [f] again"

    def test_bracketed_prose_survives_the_status_line_and_the_fix_panel(self):
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
                await self._settle(pilot, lambda: "[agents]" in self._text(app, "#fix-text"))
                self.assertIn("[agents]", self._text(app, "#fix-text"))
                self.assertIn("--tier [agents]", self._text(app, "#fix-text"))
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
