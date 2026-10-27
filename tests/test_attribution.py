"""Tests for luna_doctor/attribution.py - whose concurrency a refusal belongs to.

The module is pure functions over two dicts, so these tests are too: no log
store, no capacity cache, no network. Each verdict gets a case that reaches it
and a case that shows the precedence that kept it away, because "you" and
"your-org" are the pair a reader most needs the tool to get right: one blames
the machine in front of the user, the other says the account's other sessions
were the width.

The copy assertions are the product's honesty checks, not style: never a
unit of money, never a number the tool does not have, and never a bare digit
that does not say whose number it is.
"""

import copy
import unittest

from luna_doctor.attribution import (
    VERDICT_NOT_CONCURRENCY,
    VERDICT_THE_GATEWAY,
    VERDICT_UNKNOWN,
    VERDICT_YOU,
    VERDICT_YOUR_ORG,
    VERDICTS,
    Attribution,
    attribute,
    resolve_priority_concurrency,
)


# ---- fixtures ---------------------------------------------------------------
def local_store(requests=0, by_outcome=None, peak=0, admitted_pct=None,
                starved=0, hours=24.0, deadlines=None):
    """The shape analyse() returns, reduced to the keys attribution reads."""
    outcomes = dict(by_outcome or {})
    return {
        "window_hours": hours,
        "requests": requests,
        "by_outcome": outcomes,
        "peak_concurrency": peak,
        "admission_conversion": {"interactive": {"admitted_pct": admitted_pct},
                                 "background": {"admitted_pct": None}},
        "starved_peer_evidence": starved,
        "per_model_deadlines": dict(deadlines or {}),
    }


def account(priority_lanes=12, plan="apex", now=None, days=None, stale=False):
    """The metrics.capacity dict the CLI builds, plus an optional live sample."""
    cap = {
        "provenance": "account",
        "source": "GET /v1/capacity (cache)",
        "stale": stale,
        "note": "served from cache",
        "plan": plan,
        "priority_lanes": priority_lanes,
        "background_lanes": 12,
        "overflow_routing": "enabled",
    }
    if now is not None:
        cap["now"] = now
        # cli._capacity_metrics decides freshness against the window it will be
        # read against; these fixtures are describing a sample that covers it.
        cap.setdefault("now_fresh", True)
    if days is not None:
        cap["window_days"] = days
    return cap


def busy_now(in_use=12, limit=12, saturated=True):
    return {"in_use": in_use, "limit": limit, "available": limit - in_use,
            "saturated": saturated, "sampled_at": "2026-10-23T10:00:00Z"}


def quiet_now(in_use=1, limit=12):
    return {"in_use": in_use, "limit": limit, "available": limit - in_use,
            "saturated": False, "sampled_at": "2026-10-23T10:00:00Z"}


def denied_day(day="2026-10-20", denied=0, denied_after_wait=16):
    return {"day": day, "accepted": 4570, "deferred": 12, "denied": denied,
            "denied_after_wait": denied_after_wait, "avg_queue_wait_ms": 144544.82}


def strings_of(attribution):
    """Every string one Attribution would put in front of a user."""
    out = [attribution.headline]
    out.extend(attribution.actions)
    for label, value in attribution.rows:
        out.extend([label, value])
    return out


# ---- resolve_priority_concurrency ------------------------------------------
class ResolvePriorityConcurrency(unittest.TestCase):
    def test_account_number_is_the_first_authority(self):
        self.assertEqual((12, "account"),
                         resolve_priority_concurrency(account(12), None))

    def test_account_number_beats_a_declared_plan(self):
        """Both sources present: the account wins, whatever --plan said."""
        self.assertEqual((9, "account"),
                         resolve_priority_concurrency(account(9), "apex"))

    def test_account_number_beats_a_declared_number(self):
        self.assertEqual((11, "account"),
                         resolve_priority_concurrency(account(11), "4"))

    def test_plan_name_resolves_to_its_published_default(self):
        value, source = resolve_priority_concurrency(None, "Apex")
        self.assertEqual("plan apex", source)
        self.assertEqual(6, value)

    def test_plain_number_is_a_declared_width(self):
        self.assertEqual((12, "declared"),
                         resolve_priority_concurrency(None, " 12 "))

    def test_null_priority_lanes_falls_back_to_the_declaration(self):
        cap = account(priority_lanes=None, plan=None)
        self.assertEqual((2, "plan pro"), resolve_priority_concurrency(cap, "pro"))

    def test_no_capacity_and_no_plan_is_unknown_not_a_number(self):
        for cap in (None, {}, {"provenance": "unknown", "reason": "no cached capacity"}):
            self.assertEqual((None, "unknown"), resolve_priority_concurrency(cap, None))

    def test_null_priority_lanes_alone_is_unknown(self):
        cap = account(priority_lanes=None, plan="apex")
        self.assertEqual((None, "unknown"), resolve_priority_concurrency(cap, None))

    def test_a_bool_or_float_is_not_a_width(self):
        for bad in (True, 4.0, "4"):
            self.assertEqual((None, "unknown"),
                             resolve_priority_concurrency({"priority_lanes": bad}, None))

    def test_an_unusable_declaration_is_unknown_never_a_guess(self):
        for bad in ("enterprise", "0", "-3", "4.5", ""):
            self.assertEqual((None, "unknown"),
                             resolve_priority_concurrency(None, bad))

    def test_source_vocabulary_is_closed(self):
        seen = {
            resolve_priority_concurrency(account(3), "apex")[1],
            resolve_priority_concurrency(None, "apex")[1],
            resolve_priority_concurrency(None, "7")[1],
            resolve_priority_concurrency(None, None)[1],
        }
        self.assertEqual({"account", "plan apex", "declared", "unknown"}, seen)


# ---- the verdicts -----------------------------------------------------------
class UnknownVerdict(unittest.TestCase):
    def test_no_capacity_and_no_plan_is_unknown(self):
        att = attribute(local_store(requests=10, peak=4), None, (None, "unknown"))
        self.assertEqual(VERDICT_UNKNOWN, att.verdict)

    def test_a_busy_machine_with_no_capacity_is_unknown_never_you(self):
        """The whole point: a local peak is not a width, so it cannot be 'you'."""
        att = attribute(local_store(requests=40, peak=9,
                                    by_outcome={"completed": 37,
                                                "admission_timeout": 3}),
                        None, (None, "unknown"))
        self.assertEqual(VERDICT_UNKNOWN, att.verdict)
        self.assertIn("unknown", att.headline)

    def test_capacity_with_a_null_config_is_unknown_too(self):
        cap = account(priority_lanes=None, plan=None, now=None)
        att = attribute(local_store(requests=10, peak=4), cap, (None, "unknown"))
        self.assertEqual(VERDICT_UNKNOWN, att.verdict)

    def test_headline_names_both_halves_and_the_command_that_fetches_one(self):
        att = attribute(local_store(requests=8, peak=3), None, (None, "unknown"))
        self.assertIn("local half", att.headline)
        self.assertIn("account half is unknown", att.headline)
        self.assertIn("luna-doctor capacity --online", att.headline)

    def test_unknown_never_renders_a_zero_width(self):
        att = attribute(local_store(requests=8, peak=3), None, (None, "unknown"))
        width = dict(att.rows)["priority concurrency"]
        self.assertIn("unknown", width)
        self.assertFalse(any(ch.isdigit() for ch in width))
        self.assertIn("luna-doctor capacity --online", " ".join(att.actions))


class YouVerdict(unittest.TestCase):
    def test_local_peak_at_the_account_width_is_you(self):
        att = attribute(local_store(requests=30, peak=12,
                                    by_outcome={"completed": 27,
                                                "admission_timeout": 3}),
                        account(12), (12, "account"))
        self.assertEqual(VERDICT_YOU, att.verdict)
        self.assertIn("peak on the priority tier was 12 concurrent requests (this machine)",
                      att.headline)
        self.assertIn("12 priority concurrent requests (account)", att.headline)

    def test_local_peak_above_the_width_is_you(self):
        att = attribute(local_store(requests=30, peak=14), account(12), (12, "account"))
        self.assertEqual(VERDICT_YOU, att.verdict)

    def test_local_peak_at_a_declared_plan_width_is_you(self):
        att = attribute(local_store(requests=30, peak=6), None, (6, "plan apex"))
        self.assertEqual(VERDICT_YOU, att.verdict)
        self.assertIn("declared plan apex", att.headline)
        self.assertNotIn("account's", att.headline)

    def test_you_without_refusals_says_there_is_no_headroom(self):
        att = attribute(local_store(requests=30, peak=12), account(12), (12, "account"))
        self.assertEqual(VERDICT_YOU, att.verdict)
        self.assertIn("no headroom", att.headline)


class YourOrgVerdict(unittest.TestCase):
    def test_a_429_only_store_with_a_quiet_machine_and_a_busy_account(self):
        """The product case: two jobs in flight, a 429, and the account is full."""
        att = attribute(
            local_store(requests=4, peak=2,
                        by_outcome={"completed": 2, "rate_limited": 2}),
            account(12, now=busy_now()), (12, "account"))
        self.assertEqual(VERDICT_YOUR_ORG, att.verdict)
        self.assertIn("This machine was not the width, the account was",
                      att.headline)
        self.assertIn("2 concurrent requests (this machine)", att.headline)
        self.assertIn("in use 12 of 12 priority concurrent requests (account)",
                      att.headline)

    def test_admission_refusals_inside_the_width_with_a_saturated_account(self):
        att = attribute(
            local_store(requests=20, peak=3,
                        by_outcome={"completed": 17, "admission_timeout": 3}),
            account(12, now=busy_now(in_use=12, limit=12)), (12, "account"))
        self.assertEqual(VERDICT_YOUR_ORG, att.verdict)

    def test_a_saturated_flag_without_numbers_still_corroborates(self):
        now = {"saturated": True, "sampled_at": "2026-10-23T10:00:00Z"}
        att = attribute(local_store(requests=20, peak=3,
                                    by_outcome={"rate_limited": 1}),
                        account(12, now=now), (12, "account"))
        self.assertEqual(VERDICT_YOUR_ORG, att.verdict)

    def test_the_accounts_admission_days_corroborate_without_a_now_block(self):
        att = attribute(
            local_store(requests=20, peak=3,
                        by_outcome={"client_idle_timeout": 2}),
            account(12, days=[denied_day()]), (12, "account"))
        self.assertEqual(VERDICT_YOUR_ORG, att.verdict)
        self.assertIn("denied_after_wait", dict(att.rows)["account admission log"])

    def test_admission_days_without_denials_are_not_occupancy_evidence(self):
        """A day of accepted traffic says nothing about a refusal. The refusal
        itself is still the account's own evidence, so the verdict is the
        account's, not a claim that the day corroborated it."""
        day = {"day": "2026-10-20", "accepted": 4570, "deferred": 12,
               "denied": 0, "denied_after_wait": 0, "avg_queue_wait_ms": None}
        att = attribute(local_store(requests=20, peak=3,
                                    by_outcome={"client_idle_timeout": 1}),
                        account(12, days=[day]), (12, "account"))
        self.assertEqual(VERDICT_UNKNOWN, att.verdict,
                         "a stall with no denial day and no full pool stays unresolved")
        self.assertIn("owner is not established", att.headline)

    def test_a_named_org_limit_rejection_corroborates_without_any_payload(self):
        """The real complaint: background work filled the pool and the next
        foreground request came back CONCURRENT_REQUEST_LIMIT_EXCEEDED with no
        capacity payload on the machine. The gateway named the account's pool in
        the response, so that rejection is account-level evidence by itself - it
        used to be read as "nothing was refused" and attributed to
        not-concurrency while the headline above it reported the rejections."""
        att = attribute(
            local_store(requests=4, peak=0,
                        by_outcome={"completed": 1, "org_concurrency_limit": 3}),
            None, (6, "plan apex"))
        self.assertEqual(VERDICT_YOUR_ORG, att.verdict)
        self.assertIn("This machine was not the width, the account was",
                      att.headline)
        self.assertIn("refused with CONCURRENT_REQUEST_LIMIT_EXCEEDED",
                      dict(att.rows)["refused at the account limit"])

    def test_an_unknown_width_does_not_block_the_org_verdict(self):
        """No width to exceed, an occupied account, and a local refusal."""
        att = attribute(local_store(requests=20, peak=3,
                                    by_outcome={"rate_limited": 1}),
                        {"provenance": "account", "priority_lanes": None,
                         "now": busy_now()},
                        (None, "unknown"))
        self.assertEqual(VERDICT_YOUR_ORG, att.verdict)
        self.assertIn("width unknown", att.headline)


class PriorityTier(unittest.TestCase):
    def test_background_traffic_does_not_count_against_the_priority_width(self):
        """Ten background requests beside one interactive request are not ten
        requests against a priority width of two."""
        att = attribute(
            {"requests": 11, "by_outcome": {"completed": 11}, "peak_concurrency": 11,
             "peak_concurrency_priority": 1,
             "admission_conversion": {"interactive": {"admitted_pct": 100.0}}},
            account(2), (2, "account"))
        self.assertEqual(VERDICT_NOT_CONCURRENCY, att.verdict)

    def test_the_priority_peak_still_reaches_the_width(self):
        att = attribute(
            {"requests": 4, "by_outcome": {"completed": 4}, "peak_concurrency": 12,
             "peak_concurrency_priority": 3,
             "admission_conversion": {"interactive": {"admitted_pct": 100.0}}},
            account(2), (2, "account"))
        self.assertEqual(VERDICT_YOU, att.verdict)
        self.assertIn("priority tier", att.headline)


class PeakRowsMatchTheWidth(unittest.TestCase):
    def test_a_background_heavy_window_never_says_the_peak_sat_inside_the_width(self):
        """15 requests on both tiers, 1 on the priority tier, width 8. The
        headline used to place the all-tier peak against the width and claim 15
        sat inside 8."""
        att = attribute(
            {"requests": 16, "by_outcome": {"rate_limited": 1, "completed": 15},
             "peak_concurrency": 15, "peak_concurrency_priority": 1,
             "admission_conversion": {"interactive": {"admitted_pct": 100.0}}},
            account(8, now=busy_now()), (8, "account"))
        self.assertNotIn("15", att.headline)
        self.assertIn("1 concurrent request", att.headline)
        rows = dict(att.rows)
        self.assertIn("all tiers", rows["peak concurrency"])
        self.assertIn("set against", rows["peak on the priority tier"])


class GatewayVerdict(unittest.TestCase):
    def test_upstream_failures_without_a_refusal_or_occupancy(self):
        att = attribute(
            local_store(requests=20, peak=2,
                        by_outcome={"completed": 16,
                                    "upstream_unavailable": 4}),
            account(12, now=quiet_now()), (12, "account"))
        self.assertEqual(VERDICT_THE_GATEWAY, att.verdict)
        self.assertIn("gateway's, not this machine's concurrency", att.headline)
        self.assertIn("upstream error", " ".join(att.actions))

    def test_upstream_error_counts_with_upstream_unavailable(self):
        att = attribute(local_store(requests=9, peak=1,
                                    by_outcome={"upstream_error": 1,
                                                "upstream_unavailable": 1}),
                        None, (4, "declared"))
        self.assertEqual(VERDICT_THE_GATEWAY, att.verdict)
        self.assertIn("2 requests failed upstream", att.headline)

    def test_a_refusal_at_admission_keeps_the_gateway_verdict_away(self):
        """An upstream error is not the whole story when a request was also
        refused at admission: the refusal needs an owner, so this cannot be
        the gateway's verdict."""
        att = attribute(local_store(requests=20, peak=2,
                                    by_outcome={"admission_timeout": 1,
                                                "upstream_error": 3}),
                        account(12, now=quiet_now()), (12, "account"))
        self.assertEqual(VERDICT_UNKNOWN, att.verdict)
        self.assertIn("owner is not established", att.headline)


class NotConcurrencyVerdict(unittest.TestCase):
    def test_a_clean_window_inside_the_width(self):
        att = attribute(local_store(requests=20, peak=2,
                                    by_outcome={"completed": 20}),
                        account(12, now=quiet_now()), (12, "account"))
        self.assertEqual(VERDICT_NOT_CONCURRENCY, att.verdict)
        self.assertIn("Concurrency is not what is wrong here", att.headline)

    def test_a_429_is_never_ruled_out_of_concurrency(self):
        """A 429 is the account's capacity being exceeded, so a quiet sample
        cannot turn it into "concurrency is not what is wrong here"."""
        att = attribute(local_store(requests=20, peak=2,
                                    by_outcome={"rate_limited": 2,
                                                "completed": 18}),
                        account(12, now=quiet_now()), (12, "account"))
        self.assertEqual(VERDICT_YOUR_ORG, att.verdict)

    def test_a_stall_with_no_account_agreement_stays_unresolved(self):
        """A stall is a concurrency symptom. With no half able to name its owner
        the answer is "unresolved", not a verdict of "not concurrency"."""
        att = attribute(local_store(requests=20, peak=2,
                                    by_outcome={"client_idle_timeout": 2,
                                                "completed": 18}),
                        account(12, now=quiet_now()), (12, "account"))
        self.assertEqual(VERDICT_UNKNOWN, att.verdict)
        self.assertIn("owner is not established", att.headline)

    def test_a_declared_width_and_no_account_half_claims_nothing_about_it(self):
        att = attribute(local_store(requests=5, peak=1), None, (4, "declared"))
        self.assertEqual(VERDICT_NOT_CONCURRENCY, att.verdict)
        self.assertIn("inside the declared width of 4 priority concurrent requests "
                      "(declared by --plan)", att.headline)
        self.assertNotIn("the account", att.headline)


# ---- precedence -------------------------------------------------------------
class Precedence(unittest.TestCase):
    def test_you_beats_your_org(self):
        att = attribute(
            local_store(requests=30, peak=12,
                        by_outcome={"rate_limited": 4, "completed": 26}),
            account(12, now=busy_now()), (12, "account"))
        self.assertEqual(VERDICT_YOU, att.verdict)

    def test_your_org_beats_the_gateway(self):
        att = attribute(
            local_store(requests=30, peak=2,
                        by_outcome={"rate_limited": 2, "upstream_error": 3}),
            account(12, now=busy_now()), (12, "account"))
        self.assertEqual(VERDICT_YOUR_ORG, att.verdict)

    def test_unknown_beats_every_local_evidence(self):
        att = attribute(local_store(requests=30, peak=1,
                                    by_outcome={"rate_limited": 3,
                                                "upstream_error": 5}),
                        None, (None, "unknown"))
        self.assertEqual(VERDICT_UNKNOWN, att.verdict)

    def test_unknown_beats_you_when_there_is_no_width_to_reach(self):
        """A peak of 6 cannot be "at" a width nobody has reported."""
        att = attribute(local_store(requests=30, peak=6), None, (None, "unknown"))
        self.assertEqual(VERDICT_UNKNOWN, att.verdict)

    def test_every_verdict_is_reachable(self):
        seen = {
            attribute(local_store(requests=9, peak=9), None, (None, "unknown")).verdict,
            attribute(local_store(requests=30, peak=12), account(12), (12, "account")).verdict,
            attribute(local_store(requests=30, peak=2, by_outcome={"rate_limited": 1}),
                      account(12, now=busy_now()), (12, "account")).verdict,
            attribute(local_store(requests=30, peak=2, by_outcome={"upstream_error": 1}),
                      account(12, now=quiet_now()), (12, "account")).verdict,
            attribute(local_store(requests=5, peak=1), account(12), (12, "account")).verdict,
        }
        self.assertEqual(set(VERDICTS), seen)


# ---- what the rows and the copy say ----------------------------------------
class Rows(unittest.TestCase):
    def _attributions(self):
        return [
            attribute(local_store(requests=9, peak=9), None, (None, "unknown")),
            attribute(local_store(requests=30, peak=12, admitted_pct=80.0,
                                  by_outcome={"admission_timeout": 3},
                                  deadlines={"glm-5.3": {"n": 4, "deadline_s": 60.3,
                                                         "status": "observed"}}),
                      account(12, now=busy_now()), (12, "account")),
            attribute(local_store(requests=10, peak=2, admitted_pct=50.0,
                                  starved=1,
                                  by_outcome={"rate_limited": 1,
                                              "client_idle_timeout": 1}),
                      account(12, now=busy_now(), days=[denied_day()], stale=True),
                      (12, "account")),
            attribute(local_store(requests=10, peak=2,
                                  by_outcome={"upstream_error": 2}),
                      {"provenance": "account", "priority_lanes": None,
                       "now": {"in_use": 3, "limit": None}},
                      (None, "unknown")),
            attribute(local_store(requests=10, peak=1,
                                  by_outcome={"rate_limited": 1}),
                      account(6, plan="max", now=quiet_now()), (6, "account")),
            attribute(local_store(requests=10, peak=1), account(12), (12, "account")),
        ]

    def test_rows_are_label_value_string_pairs(self):
        for att in self._attributions():
            for row in att.rows:
                self.assertEqual(2, len(row))
                label, value = row
                self.assertIsInstance(label, str)
                self.assertIsInstance(value, str)
                self.assertTrue(label.strip())
                self.assertTrue(value.strip())

    def test_no_row_value_is_a_bare_number_without_a_label(self):
        whose = ("(this machine", "(account", "(declared plan", "(declared by",
                 "unknown")
        for att in self._attributions():
            for label, value in att.rows:
                if not any(ch.isdigit() for ch in value):
                    continue
                self.assertTrue(any(ch.isalpha() for ch in value),
                                "bare number in row %r: %r" % (label, value))
                self.assertTrue(any(marker in label + value for marker in whose),
                                "unattributed number in row %r: %r" % (label, value))

    def test_local_and_account_numbers_are_never_summed(self):
        att = attribute(local_store(requests=40, peak=3), account(12), (12, "account"))
        text = " ".join(strings_of(att))
        self.assertNotIn("15", text, "3 + 12 is not a number this tool measured")

    def test_the_width_row_says_whose_number_it_is(self):
        account_att = attribute(local_store(peak=1), account(12), (12, "account"))
        declared_att = attribute(local_store(peak=1), None, (6, "plan apex"))
        number_att = attribute(local_store(peak=1), None, (8, "declared"))
        self.assertEqual("12 priority concurrent requests (account)",
                         dict(account_att.rows)["priority concurrency"])
        self.assertEqual("6 priority concurrent requests (declared plan apex)",
                         dict(declared_att.rows)["priority concurrency"])
        self.assertEqual("8 priority concurrent requests (declared by --plan)",
                         dict(number_att.rows)["priority concurrency"])

    def test_the_rows_carry_the_local_evidence_with_counts(self):
        att = attribute(local_store(requests=10, peak=2, admitted_pct=60.0,
                                    starved=1,
                                    by_outcome={"admission_timeout": 2,
                                                "rate_limited": 1,
                                                "client_idle_timeout": 3,
                                                "upstream_error": 1},
                                    deadlines={"glm-5.3": {"n": 4, "deadline_s": 60.3,
                                                           "status": "observed"}}),
                        account(12, now=busy_now()), (12, "account"))
        rows = dict(att.rows)
        self.assertIn("2 requests waited for room", rows["blocked at admission"])
        self.assertIn("1 request was told", rows["rate limited"])
        self.assertIn("3 requests stalled", rows["silent stalls"])
        self.assertIn("1 request failed upstream", rows["upstream failures"])
        self.assertIn("60.3s median over 4 requests refused",
                      rows["park deadline observed"])
        self.assertIn("60.0%", rows["interactive admitted"])

    def test_a_stale_account_sample_says_so_and_names_the_refresh(self):
        att = attribute(local_store(requests=10, peak=1), account(12, stale=True),
                        (12, "account"))
        self.assertIn("luna-doctor capacity --online",
                      dict(att.rows)["account sample age"])

    def test_rows_never_use_a_retired_or_money_vocabulary(self):
        forbidden = ("fan-out", "fanout", "lane", "credit", "spend", "token",
                     "probe", "load-test", "compar", "dollar")
        for att in self._attributions():
            for label, value in att.rows:
                for word in forbidden:
                    self.assertNotIn(word, (label + " " + value).lower())


class Copy(unittest.TestCase):
    FORBIDDEN = ("credit", "spend", "token", "probe", "load-test",
                 "compare", "compar", "dollar", "fan-out", "lane")

    def _attributions(self):
        return Rows()._attributions()

    def test_no_emitted_string_uses_a_forbidden_word(self):
        for att in self._attributions():
            for text in strings_of(att):
                low = text.lower()
                for word in self.FORBIDDEN:
                    self.assertNotIn(word, low,
                                     "%r appears in %r" % (word, text))

    def test_headlines_are_one_plain_sentence(self):
        for att in self._attributions():
            self.assertNotIn("\n", att.headline)
            self.assertNotIn("`", att.headline)
            self.assertNotIn("*", att.headline)
            self.assertTrue(att.headline.endswith("."))

    def test_actions_are_plain_text(self):
        for att in self._attributions():
            for action in att.actions:
                self.assertNotIn("`", action)
                self.assertNotIn("\n", action)

    def test_a_single_request_reads_as_one_request(self):
        """The counts are read one request at a time, so the copy must be."""
        one = attribute(local_store(requests=2, peak=1,
                                    by_outcome={"rate_limited": 1}),
                        account(1), (1, "account"))
        self.assertEqual(VERDICT_YOU, one.verdict)
        self.assertIn("1 request that was refused or stalled", one.headline)
        self.assertIn("1 request was told", dict(one.rows)["rate limited"])
        self.assertIn("1 concurrent request (this machine)",
                      dict(one.rows)["peak concurrency"])

    def test_no_word_is_printed_twice_in_a_row(self):
        for att in self._attributions() + self._singular_attributions():
            for text in strings_of(att):
                words = text.split()
                for i in range(len(words) - 1):
                    self.assertNotEqual(words[i], words[i + 1],
                                        "repeated word in %r" % text)

    def _singular_attributions(self):
        return [
            attribute(local_store(requests=2, peak=1,
                                  by_outcome={"rate_limited": 1}),
                      account(1), (1, "account")),
            attribute(local_store(requests=2, peak=1,
                                  by_outcome={"upstream_error": 1}),
                      account(4, now=quiet_now()), (4, "account")),
            attribute(local_store(requests=2, peak=1,
                                  by_outcome={"admission_timeout": 1,
                                              "client_idle_timeout": 1}),
                      account(4, days=[denied_day(denied=1)]), (4, "account")),
        ]

    def test_the_accounts_command_is_never_offered_as_a_measurement(self):
        """Copy may point at a width; it never promises a measurement."""
        for att in self._attributions():
            for text in strings_of(att):
                self.assertNotIn("the gateway admits", text.lower())


# ---- the shape the parent codes against ------------------------------------
class Contract(unittest.TestCase):
    def test_attribute_returns_an_attribution(self):
        att = attribute(local_store(peak=1), None, (None, "unknown"))
        self.assertIsInstance(att, Attribution)
        self.assertEqual(["verdict", "headline", "rows", "actions"], list(att._fields))
        self.assertIsInstance(att.rows, list)
        self.assertIsInstance(att.actions, list)

    def test_verdict_is_always_in_the_vocabulary(self):
        for att in Rows()._attributions():
            self.assertIn(att.verdict, VERDICTS)

    def test_attribute_renders_the_priority_it_is_handed(self):
        """No re-resolution: the caller's resolution is the one rendered."""
        att = attribute(local_store(requests=5, peak=6), account(20),
                        (3, "account"))
        self.assertEqual("3 priority concurrent requests (account)",
                         dict(att.rows)["priority concurrency"])
        self.assertEqual(VERDICT_YOU, att.verdict)

    def test_inputs_are_not_mutated(self):
        store = local_store(requests=10, peak=2,
                            by_outcome={"rate_limited": 1},
                            deadlines={"glm-5.3": {"n": 4, "deadline_s": 60.3,
                                                   "status": "observed"}})
        cap = account(12, now=busy_now(), days=[denied_day()])
        store_before = copy.deepcopy(store)
        cap_before = copy.deepcopy(cap)
        attribute(store, cap, (12, "account"))
        self.assertEqual(store_before, store)
        self.assertEqual(cap_before, cap)

    def test_missing_local_keys_do_not_raise(self):
        for store in ({}, {"requests": None, "by_outcome": None,
                           "peak_concurrency": None,
                           "admission_conversion": None,
                           "per_model_deadlines": None}):
            att = attribute(store, None, (None, "unknown"))
            self.assertEqual(VERDICT_UNKNOWN, att.verdict)
        # An empty window carries no verdict: with traffic it would be
        # not-concurrency, but "no requests" is not evidence of anything.
        att = attribute({}, account(12), (12, "account"))
        self.assertEqual(VERDICT_UNKNOWN, att.verdict)
        self.assertIn("no verdict", att.headline.lower())
        att = attribute({"requests": 5, "by_outcome": {}, "peak_concurrency": 1},
                        account(12), (12, "account"))
        self.assertEqual(VERDICT_NOT_CONCURRENCY, att.verdict)


if __name__ == "__main__":
    unittest.main()
