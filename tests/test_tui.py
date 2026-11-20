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

from luna_doctor import badges, baseline, cli, report, sources, tui

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
_HOUR_START = "2026-11-02T14:00Z"


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


def _hour_scan(pairs, **over):
    """Metrics whose one-hour window closes at _HOUR_START plus an hour.

    One column per minute, so a minute offset is a column index and a `█` cell
    is one minute at or over the lane count.
    """
    base = datetime.strptime(_HOUR_START, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    return _metrics(window_hours=1, by_minute=_by_minute(pairs, start=_HOUR_START),
                    scanned_at=base.timestamp() + 3600, **over)


def _busy_hour():
    """An hour with seven minutes at or over 12 lanes, peaking at 14:04Z."""
    pairs = [(offset, 3) for offset in range(0, 4)]
    pairs += [(offset, 12) for offset in range(4, 11)]
    pairs += [(offset, 3) for offset in range(11, 60)]
    return _hour_scan(pairs)


# ---- (a) the six answers select the evidence group -------------------------
class TriageChoices(unittest.TestCase):
    """The six on-screen answers, in the order the design draws them.

    The answer chooses which three evidence rows stand under the picture. It
    never gates the screen and it never changes anything above the picture.
    """

    def test_the_six_answers_select_their_evidence_group(self):
        self.assertEqual(
            ["failing", "slow", "finish_reason", "concurrency", "stuck", "checking"],
            [key for _text, key in tui.TRIAGE_CHOICES])
        self.assertEqual(
            ["stalls", "latency", "stalls", "refusals", "stalls", "latency"],
            [tui.pick_lead(key) for _text, key in tui.TRIAGE_CHOICES])

    def test_the_selection_names_a_region_the_screen_draws(self):
        """One spelling per concept: the selector answers in the same three
        region kinds `region_moon` and the region title use."""
        self.assertEqual(
            set(tui.EVIDENCE_GROUPS),
            {tui.pick_lead(key) for _text, key in tui.TRIAGE_CHOICES})
        for group in tui.EVIDENCE_GROUPS:
            self.assertIn(group, tui.REGION_KINDS)
            self.assertNotEqual(tui.REGION_LOAD, group)

    def test_a_429_answer_selects_the_refusals_and_a_stuck_one_the_stalls(self):
        self.assertEqual(tui.REGION_REFUSALS, tui.pick_lead("concurrency"))
        self.assertEqual(tui.REGION_STALLS, tui.pick_lead("stuck"))

    def test_finish_reason_selects_the_stalls_group(self):
        """The spec binds "stream ended without finish_reason" to the idle
        budget that killed it, which is a stall, not an admission refusal."""
        self.assertEqual(tui.REGION_STALLS, tui.pick_lead("finish_reason"))

    def test_just_checking_selects_latency_rather_than_nothing(self):
        """The dashboard renders before any answer, so "just checking" picks the
        group a quiet screen shows - the same three rows as "slow"."""
        self.assertEqual(tui.REGION_LATENCY, tui.pick_lead("checking"))

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
        self.assertTrue({"f", "q", "w"} <= keys, keys)

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
                mock.patch.object(tui.cli, "analyse", side_effect=lambda reqs, hours: {}), \
                mock.patch.object(tui.cli, "_capacity_metrics",
                                  return_value={}) as metrics_block, \
                mock.patch.object(tui.sources, "fetch_capacity", return_value=None) as fetch:
            metrics, previous, cap = tui.scan(plan="apex")
        fetch.assert_called_once_with(allow_network=False)
        self.assertIs(False, metrics_block.call_args.kwargs.get("allow_network"),
                      "the CLI's capacity block is read cache-only too")
        self.assertEqual("apex", metrics["declared_plan"])
        self.assertEqual({}, previous)
        self.assertIsNone(cap)


# ---- the window: one hour, and the hour before it ------------------------
class TheWindowAndItsPredecessor(unittest.TestCase):
    """The window is one hour, [w] cycles it, and scan() reads the window before
    this one so a delta column has a window to compare against."""

    def test_the_dashboard_scans_one_hour(self):
        self.assertEqual(1, tui.WINDOW_HOURS)

    def test_the_window_cycles_one_six_and_twentyfour(self):
        self.assertEqual(6, tui.window_cycle(1))
        self.assertEqual(24, tui.window_cycle(6))
        self.assertEqual(1, tui.window_cycle(24))

    def test_a_window_outside_the_cycle_falls_back_to_one_hour(self):
        self.assertEqual(1, tui.window_cycle(0.5))
        self.assertEqual(1, tui.window_cycle(None))

    def test_scan_returns_the_window_the_one_before_it_and_the_capacity(self):
        reads, analysed = [], []

        def collect(hours, harnesses, pi_roots, claude_roots, now=None):
            reads.append((hours, now))
            return []

        def analyse(reqs, hours):
            analysed.append(hours)
            return {"window_hours": hours}

        now = 100000.0
        with mock.patch.object(tui.cli, "collect", side_effect=collect), \
                mock.patch.object(tui.cli, "analyse", side_effect=analyse), \
                mock.patch.object(tui.cli, "_capacity_metrics", return_value={}), \
                mock.patch.object(tui.sources, "fetch_capacity", return_value=None), \
                mock.patch.object(tui.time, "time", return_value=now):
            metrics, previous, cap = tui.scan(plan="apex", hours=6)

        _start, end = baseline.previous_window(now, 6)
        self.assertEqual(now - 6 * 3600, end,
                         "the previous window ends where this one starts")
        self.assertEqual([(6, now), (6, end)], reads,
                         "one extra read, of the window immediately before")
        self.assertEqual([6, 6], analysed)
        self.assertEqual(6, metrics["window_hours"])
        self.assertEqual(6, previous["window_hours"])
        self.assertIsNone(cap)

    def test_the_previous_window_is_baselines_own_pairing(self):
        """No second comparison: the span comes from baseline, which tests it."""
        now, hours = 500000.0, 1
        with mock.patch.object(tui.cli, "collect", return_value=[]), \
                mock.patch.object(tui.cli, "analyse", return_value={}) as analyse, \
                mock.patch.object(tui.baseline, "previous_window",
                                  wraps=baseline.previous_window) as pairing:
            tui.previous_window_metrics(now, hours)
        pairing.assert_called_once_with(now, hours)
        self.assertEqual(hours, analyse.call_args.args[1])


class TheWindowKeyOnTheScreen(unittest.TestCase):
    """[w] cycles the window and re-reads the logs: a label never outruns its data."""

    def test_w_cycles_the_window_and_rescans_it(self):
        asyncio.run(self._drive())

    async def _drive(self):
        app = tui.TriageApp()
        seen = []

        def fake_scan(plan=None, hours=tui.WINDOW_HOURS):
            seen.append(hours)
            return _metrics(window_hours=hours), None, None

        with mock.patch.object(tui, "scan", side_effect=fake_scan):
            async with app.run_test(size=(100, 40)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                self.assertEqual([1], seen, "the screen opens on one hour")
                await pilot.press("w")
                await self._settle(pilot, lambda: len(seen) == 2)
                self.assertEqual(6, app.window_hours)
                self.assertEqual(6, app.metrics["window_hours"])
                await pilot.press("w")
                await self._settle(pilot, lambda: len(seen) == 3)
                self.assertEqual(24, app.window_hours)
                self.assertEqual(24, app.metrics["window_hours"])
                await pilot.press("q")

    @staticmethod
    async def _settle(pilot, ready) -> None:
        """Wait for the thread worker's result, bounded, so a hang fails loudly."""
        for _attempt in range(40):
            await pilot.pause(0.05)
            if ready():
                return


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
                               return_value=(_metrics(), None,
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
        with mock.patch.object(tui, "scan", return_value=(empty, None, None)):
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

        def slow_fix(plan=None, hours=tui.WINDOW_HOURS):
            # the worker is inside the fix, and the screen must already say so
            computing.set()
            release.wait(5)
            return "fix output"

        with mock.patch.object(tui, "scan", return_value=(_metrics(), None, _cap(_payload("capacity-2026-11-02.json")))), \
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
        with mock.patch.object(tui, "scan", return_value=(metrics, None, cap)), \
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
        rows = tui.local_rows("concurrency", self._busy())
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

    def _quiet_with_account(self):
        """A quiet window whose account block carries admission days.

        The CLI appends CAPACITY_COUNTS_EXCLUDE and an account-versus-local line
        that opens lowercase, so a panel that reads past the headline draws a
        550-character row out of prose the reader never asked this screen for.
        """
        return _metrics(
            requests=5,
            by_outcome={"completed": 5},
            by_tier={"interactive": 5},
            by_harness={"pi": 5},
            peak_concurrency=2,
            saturated=0,
            pressure_windows=0,
            longest_pressure_window=None,
            latency_s={"interactive": {"n": 5, "p50": 20.0, "p90": 40.0, "max": 60.0},
                       "background": {"n": 0, "p50": None, "p90": None, "max": None}},
            admission_conversion={
                "interactive": {"completed": 5, "admission_timeout": 0,
                                "admitted_pct": 100.0},
                "background": {"completed": 0, "admission_timeout": 0,
                               "admitted_pct": None}},
            capacity={"provenance": "account", "plan": "teams", "priority_lanes": 12,
                      "window_days": [{"day": "2026-11-01", "accepted": 10,
                                       "deferred": 0, "denied": 0,
                                       "denied_after_wait": 0,
                                       "avg_queue_wait_ms": None}]},
        )

    def test_the_verdict_panel_keeps_only_the_headline_of_a_quiet_window(self):
        """The exclusion paragraph is `analyze`'s, not the dashboard's."""
        rows = tui.verdict_rows(self._quiet_with_account())
        for _badge, text in rows:
            self.assertLessEqual(len(text), 120,
                                 f"a row the headline did not bound: {text!r}")
        self.assertIn("none blocked at admission", rows[0][1])
        prose = " ".join(text for _badge, text in rows)
        self.assertNotIn("server counts cover only traffic", prose)

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

    def test_the_caption_says_which_scale_the_row_is_drawn_on(self):
        self.assertEqual("scale: this window · peak at 14:04Z",
                         tui.spark_caption(_busy_hour()))
        self.assertEqual("█ = at or over ●12 lanes · over 7m of 60m · peak at 14:04Z",
                         tui.spark_caption(_busy_hour(), lanes=12))

    def test_an_empty_window_draws_nothing_at_all(self):
        self.assertEqual("", tui.concurrency_sparkline(_metrics()))
        self.assertEqual("", tui.spark_text(_metrics()).plain)

    def test_the_text_is_the_row_then_its_caption(self):
        metrics = _scan_of([(0, 17)])
        row, caption = tui.spark_text(metrics, width=8).plain.split("\n")
        self.assertEqual(8, len(row))
        self.assertEqual(tui.spark_caption(metrics, width=8), caption)


# ---- the picture on the lane scale --------------------------------------
class ThePictureScalesToTheLanes(unittest.TestCase):
    """`█` means at or over the lane count, on every screen and in every window."""

    def test_one_hour_is_one_cell_per_minute(self):
        self.assertEqual(60, tui.SPARK_WIDTH)
        self.assertEqual(60, len(tui.concurrency_sparkline(_busy_hour(), lanes=12)))

    def test_a_minute_at_or_over_the_lane_count_is_the_full_block(self):
        row = tui.concurrency_sparkline(
            _hour_scan([(0, 12), (1, 24), (2, 11)]), lanes=12)
        self.assertEqual("██▇", row[:3])

    def test_below_the_lane_count_the_block_is_the_approach(self):
        row = tui.concurrency_sparkline(_hour_scan([(0, 6), (1, 5), (2, 1)]), lanes=12)
        self.assertEqual("▄▃▁", row[:3])

    def test_the_lanes_set_the_scale_not_this_windows_peak(self):
        """The current behaviour tops out at the window's worst minute, which
        draws the same shape whether the lanes are 12 or 120."""
        row = tui.concurrency_sparkline(_busy_hour(), lanes=100)
        self.assertNotIn("█", row, "nothing here reached 100 lanes")
        self.assertEqual({"▁"}, set(row), "every minute here is far below 100 lanes")

    def test_without_lanes_the_row_still_scales_to_this_windows_peak(self):
        row = tui.concurrency_sparkline(_busy_hour())
        self.assertEqual("█", row[4], "the busiest minute is the full block")
        self.assertNotIn("█", row[:4])

    def test_the_count_of_full_blocks_is_the_minutes_over(self):
        metrics = _busy_hour()
        self.assertEqual(7, tui.concurrency_sparkline(metrics, lanes=12).count("█"))
        self.assertIn("over 7m of 60m", tui.spark_caption(metrics, lanes=12))

    def test_a_window_with_no_minute_over_says_so_in_its_units(self):
        caption = tui.spark_caption(_hour_scan([(0, 3), (1, 4)]), lanes=12)
        self.assertIn("over 0m of 60m", caption)
        self.assertEqual("scale: this window · peak at 14:01Z",
                         tui.spark_caption(_hour_scan([(0, 3), (1, 4)])))

    def test_an_empty_window_has_no_picture_and_no_caption(self):
        self.assertEqual("", tui.concurrency_sparkline(_metrics(), lanes=12))
        self.assertEqual("", tui.spark_caption(_metrics(), lanes=12))
        self.assertEqual("", tui.spark_text(_metrics(), lanes=12).plain)


# ---- the lane count the picture scales to -------------------------------
class TheLanesComeFromTheWidthRow(unittest.TestCase):
    """The header's `●<lanes>` and the caption's denominator are one number."""

    def test_the_accounts_number_wins_and_the_picture_scales_to_it(self):
        cap = _cap(_payload("capacity-2026-11-02.json"))
        self.assertEqual((12, "account"), tui.resolved_lanes(_metrics(), cap))

    def test_a_declaration_is_the_fallback_when_nothing_is_cached(self):
        self.assertEqual((None, "unknown"), tui.resolved_lanes(_metrics(), None))
        value, source = tui.resolved_lanes(_metrics(declared_plan="apex"), None)
        self.assertEqual("plan apex", source)
        self.assertIsInstance(value, int)

    def test_a_declaration_never_overrides_the_account(self):
        cap = _cap(_payload("capacity-2026-11-02.json"))
        self.assertEqual((12, "account"),
                         tui.resolved_lanes(_metrics(declared_plan="pro"), cap))


# ---- the sparkline on the screen ----------------------------------------
class TheSparkOnTheScreen(unittest.TestCase):
    """One quiet line between the evidence panels and the verdict line."""

    def test_the_row_shows_between_the_local_panel_and_the_verdict(self):
        asyncio.run(self._drive())

    async def _drive(self):
        app = tui.TriageApp()
        cap = _cap(_payload("capacity-2026-11-02.json"))
        with mock.patch.object(tui, "scan",
                               return_value=(_scan_of([(0, 17), (600, 4)]), None, cap)):
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
                self.assertIn("█ = at or over ●12 lanes", text)
                self.assertIn("peak at 00:00Z", text)
                self.assertTrue(set(text) & set(tui._SPARK_BLOCKS), text)
                await pilot.press("q")

    def test_a_window_with_no_active_minute_hides_the_line(self):
        asyncio.run(self._drive_quiet())

    async def _drive_quiet(self):
        app = tui.TriageApp()
        with mock.patch.object(tui, "scan", return_value=(_metrics(), None, None)):
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

    def test_the_detail_is_the_read_only_analyze_report(self):
        with mock.patch.object(tui.cli, "main", return_value=0) as main:
            code, text = tui.detail_output("apex", 24)
        argv = main.call_args.args[0]
        self.assertEqual("analyze", argv[0])
        self.assertEqual("24h", argv[argv.index("--since") + 1])
        self.assertIn("--offline", argv)
        # the screen is read-only: a person opening and closing it must not
        # leave a run extraction in the store
        self.assertIn("--no-store", argv)
        self.assertEqual("apex", argv[argv.index("--plan") + 1])
        self.assertEqual(0, code)
        self.assertEqual("", text)

    def test_the_report_asks_for_the_dashboards_window(self):
        with mock.patch.object(tui.cli, "main", return_value=0) as main:
            tui.detail_output()
        argv = main.call_args.args[0]
        self.assertEqual("1h", argv[argv.index("--since") + 1])

    def test_a_failed_analyze_hands_back_its_code_and_its_error(self):
        def refused(argv):
            print("luna-doctor: bad --since value", file=sys.stderr)
            return 2

        with mock.patch.object(tui.cli, "main", side_effect=refused):
            code, text = tui.detail_output("apex", 24)
        self.assertEqual(2, code)
        self.assertIn("bad --since value", text)
        self.assertFalse(tui.carries_report(code, text),
                         "one error line is not a report")

    def test_the_exit_codes_analyze_answers_with_all_carry_a_report(self):
        """0 clean, 1 over capacity, 2 nothing measured: all three are answers."""
        report = "5 requests, none blocked at admission, peak concurrency 2.\n"
        for code in (0, 1, 2):
            self.assertTrue(tui.carries_report(code, report), code)
        self.assertTrue(tui.carries_report(2, "No requests found in the window."),
                        "an empty window is a report that says so")

    def test_a_capture_holding_only_the_tools_own_error_is_no_report(self):
        self.assertFalse(tui.carries_report(2, "luna-doctor: bad --since value: 'nope'"))
        self.assertFalse(tui.carries_report(
            2, "usage: luna-doctor [-h] ...\nluna-doctor: error: unrecognized arguments"))
        self.assertFalse(tui.carries_report(3, "5 requests, none blocked"),
                         "a code analyze never answers with is no report")

    def test_an_empty_capture_is_an_empty_report_not_a_failure(self):
        self.assertTrue(tui.carries_report(0, ""))


# ---- [f]: the fix for the window on screen ------------------------------
class TheFixAsksForThisWindow(unittest.TestCase):
    """The advice is about the rows on screen, so it is read for their window."""

    def test_the_fix_runs_for_the_window_on_screen(self):
        with mock.patch.object(tui.cli, "main", return_value=0) as main:
            tui.fix_output("apex", 6)
        argv = main.call_args.args[0]
        self.assertEqual("fix", argv[0])
        self.assertEqual("6h", argv[argv.index("--since") + 1])
        self.assertIn("--offline", argv)
        self.assertEqual("apex", argv[argv.index("--plan") + 1])

    def test_the_fix_defaults_to_the_dashboards_one_hour(self):
        with mock.patch.object(tui.cli, "main", return_value=0) as main:
            tui.fix_output()
        argv = main.call_args.args[0]
        self.assertEqual("1h", argv[argv.index("--since") + 1])
        self.assertNotIn("--plan", argv)

    def test_d_runs_analyze_and_fills_the_detail_panel(self):
        asyncio.run(self._drive())

    async def _drive(self):
        app = tui.TriageApp(plan="apex")

        def report(argv):
            print("peak concurrency 17")
            return 0

        with mock.patch.object(tui, "scan", return_value=(_metrics(), None, None)), \
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
                self.assertIn("--no-store", argv)
                self.assertEqual("apex", argv[argv.index("--plan") + 1])
                self.assertTrue(app.query_one("#detail").display)
                self.assertIn("the analyze report is on screen",
                              self._text(app, "#status"))
                await pilot.press("q")

    def test_a_failed_analyze_is_announced_as_a_failure(self):
        asyncio.run(self._drive_failure())

    async def _drive_failure(self):
        app = tui.TriageApp()
        failed = (2, "luna-doctor: bad --since value: 'nope'")
        with mock.patch.object(tui, "scan", return_value=(_metrics(), None, None)), \
                mock.patch.object(tui, "detail_output", return_value=failed):
            async with app.run_test(size=(100, 40)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("d")
                await self._settle(
                    pilot, lambda: "analyze command failed" in self._text(app, "#status"))
                status = self._text(app, "#status")
                self.assertIn("the analyze command failed: luna-doctor: bad --since value", status)
                self.assertNotIn("is on screen", status,
                                 "a failed command is not a report on screen")
                self.assertFalse(app.query_one("#detail").display,
                                 "a failure fills no panel")
                await pilot.press("q")

    def test_an_over_capacity_report_is_not_a_failure(self):
        """`analyze` exits 1 when it finds pressure, and that is the report."""
        asyncio.run(self._drive_pressure())

    async def _drive_pressure(self):
        app = tui.TriageApp()
        pressure = (1, "Concurrency pressure across 17 requests: 3 request(s) "
                       "were blocked at admission, peak concurrency 17.\n")
        with mock.patch.object(tui, "scan", return_value=(_metrics(), None, None)), \
                mock.patch.object(tui, "detail_output", return_value=pressure):
            async with app.run_test(size=(100, 40)) as pilot:
                await self._settle(pilot, lambda: app.metrics is not None)
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("d")
                await self._settle(
                    pilot, lambda: "is on screen" in self._text(app, "#status"))
                self.assertIn("the analyze report is on screen",
                              self._text(app, "#status"))
                self.assertTrue(app.query_one("#detail").display)
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

    def test_the_chart_asks_for_the_dashboards_one_hour_by_default(self):
        with mock.patch.object(tui.cli, "main", return_value=0) as main:
            svg, _png, error = tui.chart_output()
        self.addCleanup(shutil.rmtree, os.path.dirname(svg), True)
        argv = main.call_args.args[0]
        self.assertEqual("1h", argv[argv.index("--since") + 1])
        self.assertTrue(error, "nothing was written, so no path is offered")

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
        with mock.patch.object(tui, "scan", return_value=(_metrics(), None, None)), \
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
        with mock.patch.object(tui, "scan", return_value=(_metrics(), None, None)), \
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
