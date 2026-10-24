"""Tests for luna_doctor/sources.py — the one data-collection layer.

The suite runs offline by construction: every catalog/limits/capacity test
patches luna_doctor.sources._urlopen (the only network seam in the package),
and a meta-test pins that the offline default can never reach it. Config
reading is tested against a fake $HOME so no test depends on this machine's
real harness configs, and the caches live in temp files via
LUNA_DOCTOR_CATALOG / LUNA_DOCTOR_CAPACITY or an explicit cache_path so no
test reads a real cache. The capacity fixtures are the one canned live
payload, captured once on 2026-10-23 (tests/fixtures/), and every other
shape is synthetic.
"""

import datetime
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from luna_doctor import sources          # noqa: E402
from luna_doctor import cli              # noqa: E402


# ---- helpers -----------------------------------------------------------------

def _models_payload():
    """The verified /v1/models shape, trimmed to two models."""
    def model(mid, **extra):
        entry = {
            "id": mid, "object": "model", "owned_by": "lunaroute",
            "context_window": 262144, "max_input_tokens": 262144,
            "max_output_tokens": 65536,
            "capabilities": {"tools": True, "reasoning": True},
            "client_compat": {"pi": {"maxTokensField": "max_tokens",
                                     "thinkingFormat": "deepseek"}},
        }
        entry.update(extra)
        return entry
    return {"object": "list", "data": [model("glm-5.3"),
                                       model("glm-5.3-background"),
                                       model("deepseek-4.1-flash")]}


class _FakeResponse:
    def __init__(self, body, status=200):
        # dict/list bodies are payloads, and the real endpoint answers JSON
        # text: serialize them so read() returns bytes like a true response.
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen_returning(body, status=200):
    def opener(req, timeout=None):
        if isinstance(body, Exception):
            raise body
        return _FakeResponse(body, status)
    return opener


def _http_error(code):
    return urllib.error.HTTPError("https://gw.lunaroute.com/v1/x", code,
                                   "boom", None, None)


class _NoNetwork:
    """Patches the one network seam so any call fails the test loudly."""

    def setUp(self):
        super().setUp()
        def boom(req, timeout=None):
            raise AssertionError("a test reached the network seam unpatched")
        self._patcher = mock.patch.object(sources, "_urlopen", side_effect=boom)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)


def _write_cache(path, payload, fetched_at):
    sources._write_catalog_cache(path, payload, fetched_at)


def _capacity_payload():
    """The canned live capacity payload, captured 2026-10-23 with the one
    live GET this suite is ever allowed: plan apex, 12/12 concurrency,
    overflow enabled, Oct 20 denied_after_wait 16, null avg_queue_wait_ms on
    the days nobody waited, nanosecond timestamps, admission through the
    last complete UTC day."""
    with open(os.path.join(ROOT, "tests", "fixtures",
                          "capacity-2026-10-23.json")) as fh:
        return json.load(fh)


def _write_cap_cache(path, payload, fetched_at):
    sources._write_catalog_cache(path, payload, fetched_at,
                                  url=sources.CAPACITY_URL)


def _degraded_capacity_payload():
    """The documented degraded shapes in one payload: config null (so every
    concurrency number is unknown), now fields null with ledger 'unknown',
    history tail null, and an admission day with null avg_queue_wait_ms.
    Every null must survive as None, never as a zero."""
    return {
        "success": True,
        "generated_at": "2026-10-24T00:05:14.458609995Z",
        "data": {
            "now": {"limit": None, "in_use": None, "available": None,
                    "saturated": None, "ledger": "unknown",
                    "sampled_at": None},
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


def _home_env(tmp, **extra):
    """A fake $HOME (empty unless the test writes into it) + no swamp repo."""
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    env = {"HOME": home, "SWAMP_ROOT": os.path.join(home, "no-such-swamp")}
    env.update(extra)
    return env


def _iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")


# ---- catalog: cache hit / miss / TTL / degrade -------------------------------

class CatalogCacheAndTTL(_NoNetwork, unittest.TestCase):

    def test_offline_miss_is_none_with_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "catalog.json")
            self.assertIsNone(sources.fetch_catalog(False, cache_path=path))
            _, note = sources._catalog_with_note(False, cache_path=path)
            self.assertEqual(note, "no cached catalog")

    def test_offline_serves_a_fresh_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "catalog.json")
            now = time.time()
            _write_cache(path, _models_payload(), now - 100)
            cat = sources.fetch_catalog(False, cache_path=path, now=now)
            self.assertIsNotNone(cat)
            self.assertEqual(cat.served_from, "cache")
            self.assertFalse(cat.stale)
            self.assertEqual(cat.ids(), ["deepseek-4.1-flash", "glm-5.3",
                                         "glm-5.3-background"])
            self.assertIn("cache", cat.note)

    def test_offline_serves_an_expired_cache_marked_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "catalog.json")
            now = time.time()
            _write_cache(path, _models_payload(), now - sources.CATALOG_TTL_S - 10)
            cat = sources.fetch_catalog(False, cache_path=path, now=now)
            self.assertIsNotNone(cat, "a stale cache still serves when offline")
            self.assertTrue(cat.stale)
            self.assertIn("served stale", cat.note)

    def test_online_refreshes_a_stale_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "catalog.json")
            now = time.time()
            _write_cache(path, _models_payload(), now - sources.CATALOG_TTL_S - 10)
            with mock.patch.object(sources, "_urlopen",
                                   _urlopen_returning(_models_payload())):
                cat = sources.fetch_catalog(True, cache_path=path, now=now, key="k")
            self.assertEqual(cat.served_from, "network")
            self.assertFalse(cat.stale)
            with open(path) as fh:
                doc = json.load(fh)
            self.assertGreaterEqual(doc["fetched_at"], now - 5,
                                    "the refresh must rewrite the cache")

    def test_online_uses_a_fresh_cache_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "catalog.json")
            now = time.time()
            _write_cache(path, _models_payload(), now - 60)
            with mock.patch.object(sources, "_urlopen",
                                   _urlopen_returning(AssertionError("network!"))):
                cat = sources.fetch_catalog(True, cache_path=path, now=now, key="k")
            self.assertEqual(cat.served_from, "cache",
                             "a fresh cache serves; the network is only for stale or missing")

    def test_401_degrades_to_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "catalog.json")
            with mock.patch.object(sources, "_urlopen",
                                   _urlopen_returning(_http_error(401))):
                cat = sources.fetch_catalog(True, cache_path=path, key="k")
                self.assertIsNone(cat)
                _, note = sources._catalog_with_note(True, cache_path=path, key="k")
            self.assertIn("401", note)

    def test_no_key_degrades_to_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "catalog.json")
            with mock.patch.object(sources, "catalog_key", return_value=None):
                cat = sources.fetch_catalog(True, cache_path=path)
            self.assertIsNone(cat)
            self.assertFalse(os.path.exists(path), "no cache may be written on failure")

    def test_malformed_json_degrades_to_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "catalog.json")
            with mock.patch.object(sources, "_urlopen",
                                   _urlopen_returning("{not json")):
                cat = sources.fetch_catalog(True, cache_path=path, key="k")
            self.assertIsNone(cat)

    def test_a_stale_cache_survives_a_failed_online_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "catalog.json")
            now = time.time()
            _write_cache(path, _models_payload(), now - sources.CATALOG_TTL_S - 10)
            with mock.patch.object(sources, "_urlopen",
                                   _urlopen_returning(_http_error(503))):
                cat = sources.fetch_catalog(True, cache_path=path, now=now, key="k")
            self.assertIsNotNone(cat, "a network failure never turns a report into an error")
            self.assertTrue(cat.stale)
            self.assertIn("served stale", cat.note)

    def test_offline_default_never_reaches_the_network(self):
        """The meta-test: no cache anywhere, offline, and the seam must stay cold."""
        calls = []
        def recorder(req, timeout=None):
            calls.append(req)
            return _FakeResponse(json.dumps(_models_payload()))
        with mock.patch.object(sources, "_urlopen", recorder):
            with tempfile.TemporaryDirectory() as tmp:
                sources.fetch_catalog(False, cache_path=os.path.join(tmp, "c.json"))
                sources.fetch_limits(False, key="k")
                sources.fetch_capacity(False, cache_path=os.path.join(tmp, "cap.json"))
                snap = sources.build_snapshot([], since=time.time() - 60,
                                              include_codex=False,
                                              cache_path=os.path.join(tmp, "c2.json"))
        self.assertEqual(calls, [], "the offline default must not touch the network")
        self.assertIsNone(snap.catalog)
        self.assertIsNone(snap.limits)

    def test_cache_path_env_override(self):
        with mock.patch.dict(os.environ, {"LUNA_DOCTOR_CATALOG": "/tmp/x/catalog.json"}):
            self.assertEqual(sources.catalog_cache_path(), "/tmp/x/catalog.json")


class KeyResolution(unittest.TestCase):

    def test_env_var_wins(self):
        with mock.patch.dict(os.environ, {"LUNAROUTE_API_KEY": "sk-from-env"}):
            self.assertEqual(sources.catalog_key(), "sk-from-env")

    def test_auth_json_key_then_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = os.path.join(tmp, "auth.json")
            # the env var wins when set (test_env_var_wins); blank it so this
            # machine's real LUNAROUTE_API_KEY cannot mask the auth.json path
            with mock.patch.dict(os.environ, {"LUNAROUTE_API_KEY": ""}):
                with open(auth, "w") as fh:
                    json.dump({"lunaroute": {"key": "lr-key", "access": "lr-access"}}, fh)
                self.assertEqual(sources.catalog_key(auth), "lr-key")
                with open(auth, "w") as fh:
                    json.dump({"lunaroute": {"access": "lr-access"}}, fh)
                self.assertEqual(sources.catalog_key(auth), "lr-access")

    def test_missing_or_barren_auth_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"LUNAROUTE_API_KEY": ""}):
                self.assertIsNone(sources.catalog_key(os.path.join(tmp, "absent.json")))
                auth = os.path.join(tmp, "auth.json")
                with open(auth, "w") as fh:
                    json.dump({"other": {"key": "x"}}, fh)
                self.assertIsNone(sources.catalog_key(auth), "no lunaroute stanza is no key")


class LimitsSeam(_NoNetwork, unittest.TestCase):

    def test_404_is_not_deployed_never_fabricated(self):
        with mock.patch.object(sources, "_urlopen", _urlopen_returning(_http_error(404))):
            self.assertIsNone(sources.fetch_limits(True, key="k"))
            _, note = sources._limits_with_note(True, key="k")
            self.assertIn("not deployed", note)

    def test_offline_is_none(self):
        _, note = sources._limits_with_note(False)
        self.assertIn("offline", note)

    def test_deployed_shape_is_kept_as_is(self):
        payload = {"priority": 10, "background": 40, "embedding": 4}
        with mock.patch.object(sources, "_urlopen", _urlopen_returning(payload)):
            limits = sources.fetch_limits(True, key="k")
        self.assertEqual(limits.raw, payload)


# ---- capacity: cache hit / miss / TTL / the null contract -------------------


class CapacityFetch(_NoNetwork, unittest.TestCase):
    """fetch_capacity mirrors fetch_catalog: cache-first, 5-minute TTL,
    degrade to None - and the null contract is preserved end to end."""

    def _cache(self, tmp, payload=None, age_s=0.0):
        path = os.path.join(tmp, "capacity.json")
        _write_cap_cache(path, payload if payload is not None else _capacity_payload(),
                          time.time() - age_s)
        return path

    def test_offline_miss_is_none_with_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "capacity.json")
            self.assertIsNone(sources.fetch_capacity(False, cache_path=path))
            _, note = sources._capacity_with_note(False, cache_path=path)
            self.assertEqual(note, "no cached capacity")

    def test_offline_serves_a_fresh_cache_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            cap = sources.fetch_capacity(False, cache_path=self._cache(tmp))
            self.assertIsNotNone(cap)
            self.assertEqual(cap.served_from, "cache")
            self.assertFalse(cap.stale)
            self.assertIn("cache", cap.note)
            # fields verbatim from the canned live payload, including the
            # nanosecond timestamp string - never parsed, never reformatted
            self.assertEqual(cap.config.plan, "apex")
            self.assertEqual(cap.config.priority_lanes, 12)
            self.assertEqual(cap.config.background_lanes, 12)
            self.assertEqual(cap.config.overflow_routing, "enabled")
            self.assertEqual(cap.now.in_use, 0)
            self.assertEqual(cap.now.limit, 12)
            self.assertEqual(cap.now.available, 12)
            self.assertIs(cap.now.saturated, False)
            self.assertEqual(cap.now.ledger, "up")
            self.assertEqual(cap.now.sampled_at, "2026-10-24T00:05:14.510815919Z")
            self.assertEqual(cap.history.last_7d["peak_lanes"], 18)
            self.assertEqual(cap.history.last_7d["days_active"], 5)
            self.assertEqual(cap.history.last_7d["days_covered"], 6)
            self.assertIsInstance(cap.history.tail, dict)
            self.assertEqual(cap.admission.as_of, "2026-10-23")
            days = dict(sources.capacity_admission_days(cap))
            self.assertEqual(len(days), 30, "7d and 30d windows union to one per day")
            oct20 = days["2026-10-20"]
            self.assertEqual(oct20["denied_after_wait"], 16)
            self.assertEqual(oct20["avg_queue_wait_ms"], 144544.82)
            # the null path is common and must survive as None, never as 0
            self.assertIsNone(days["2026-10-21"]["avg_queue_wait_ms"])

    def test_offline_serves_an_expired_cache_marked_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            cap = sources.fetch_capacity(
                False, cache_path=self._cache(tmp, age_s=sources.CAPACITY_TTL_S + 10))
            self.assertIsNotNone(cap, "a stale cache still serves when offline")
            self.assertTrue(cap.stale)
            self.assertIn("served stale", cap.note)

    def test_online_refreshes_a_stale_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._cache(tmp, age_s=sources.CAPACITY_TTL_S + 10)
            now = time.time()
            with mock.patch.object(sources, "_urlopen",
                                   _urlopen_returning(_capacity_payload())):
                cap = sources.fetch_capacity(True, cache_path=path, now=now, key="k")
            self.assertEqual(cap.served_from, "network")
            self.assertFalse(cap.stale)
            with open(path) as fh:
                doc = json.load(fh)
            self.assertGreaterEqual(doc["fetched_at"], now - 5,
                                    "the refresh must rewrite the cache")
            self.assertEqual(doc["url"], sources.CAPACITY_URL)

    def test_online_uses_a_fresh_cache_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(sources, "_urlopen",
                                   _urlopen_returning(AssertionError("network!"))):
                cap = sources.fetch_capacity(True, cache_path=self._cache(tmp), key="k")
            self.assertEqual(cap.served_from, "cache",
                             "a fresh cache serves; the network is only for stale or missing")

    def test_the_documented_header_and_the_longer_timeout(self):
        """The capacity GET carries the documented LUNAROUTE-API-KEY header
        (not Bearer) and the 30s timeout the 4-17s endpoint needs; a timeout
        still degrades, never blocks."""
        seen = {}

        def recorder(req, timeout=None):
            seen["url"] = req.full_url
            seen["timeout"] = timeout
            seen["key"] = req.get_header("Lunaroute-api-key")   # urllib capitalizes
            seen["auth"] = req.get_header("Authorization")
            return _FakeResponse(json.dumps(_capacity_payload()))

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(sources, "_urlopen", recorder):
                cap = sources.fetch_capacity(
                    True, cache_path=os.path.join(tmp, "c.json"), key="lr_x")
        self.assertEqual(seen["url"], sources.CAPACITY_URL)
        self.assertEqual(seen["timeout"], sources.CAPACITY_TIMEOUT_S)
        self.assertEqual(seen["key"], "lr_x")
        self.assertIsNone(seen["auth"], "the documented form, not Bearer")
        self.assertIsNotNone(cap)

    def test_no_key_degrades_to_none_and_writes_no_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "capacity.json")
            with mock.patch.object(sources, "catalog_key", return_value=None):
                cap = sources.fetch_capacity(True, cache_path=path)
            self.assertIsNone(cap)
            self.assertFalse(os.path.exists(path), "no cache may be written on failure")

    def test_401_and_timeout_degrade_to_none_with_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "capacity.json")
            with mock.patch.object(sources, "_urlopen",
                                   _urlopen_returning(_http_error(401))):
                self.assertIsNone(sources.fetch_capacity(True, cache_path=path, key="k"))
                _, note = sources._capacity_with_note(True, cache_path=path, key="k")
            self.assertIn("401", note)
            with mock.patch.object(sources, "_urlopen",
                                   _urlopen_returning(TimeoutError())):
                self.assertIsNone(sources.fetch_capacity(True, cache_path=path, key="k"))

    def test_a_stale_cache_survives_a_failed_online_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._cache(tmp, age_s=sources.CAPACITY_TTL_S + 10)
            with mock.patch.object(sources, "_urlopen",
                                   _urlopen_returning(_http_error(503))):
                cap = sources.fetch_capacity(True, cache_path=path, key="k")
            self.assertIsNotNone(cap, "a network failure never turns a report into an error")
            self.assertTrue(cap.stale)
            self.assertIn("served stale", cap.note)

    def test_degraded_null_blocks_are_preserved_never_zeroed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cap = sources.fetch_capacity(
                False, cache_path=self._cache(tmp, _degraded_capacity_payload()))
            self.assertIsNone(cap.config, "config: null is None, not a defaulted block")
            self.assertIsNone(cap.now.limit)
            self.assertIsNone(cap.now.in_use)
            self.assertIsNone(cap.now.saturated)
            self.assertEqual(cap.now.ledger, "unknown")
            self.assertIsNone(cap.history.tail)
            self.assertIsNone(cap.history.last_7d["peak_lanes"])
            days = sources.capacity_admission_days(cap)
            self.assertEqual(days, [("2026-10-22",
                                     {"accepted": 10, "avg_queue_wait_ms": None,
                                      "day": "2026-10-22", "deferred": 0,
                                      "denied": 0, "denied_after_wait": 0})])
            doc = cap.to_dict()
            self.assertIsNone(doc["config"])
            # a null round-trips through JSON as null - the contract a
            # consumer of the envelope relies on
            self.assertIsNone(json.loads(json.dumps(doc))["config"])

    def test_background_lanes_zero_is_a_real_value(self):
        payload = _capacity_payload()
        payload["data"]["config"] = {"plan": "pro", "priority_lanes": 2,
                                     "background_lanes": 0,
                                     "overflow_routing": "disabled"}
        with tempfile.TemporaryDirectory() as tmp:
            cap = sources.fetch_capacity(False, cache_path=self._cache(tmp, payload))
            self.assertEqual(cap.config.background_lanes, 0,
                             "0 is sent as a value: entitled to no overflow")
            self.assertEqual(cap.config.priority_lanes, 2)
            self.assertEqual(cap.to_dict()["config"]["background_lanes"], 0)

    def test_unexpected_payload_shapes_degrade_to_none(self):
        for bad in ({}, {"data": None}, {"data": "nope"}, ["not", "a", "dict"]):
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "capacity.json")
                _write_cap_cache(path, bad, time.time())
                self.assertIsNone(sources.fetch_capacity(False, cache_path=path), bad)

    def test_capacity_cache_path_env_override(self):
        with mock.patch.dict(os.environ, {"LUNA_DOCTOR_CAPACITY": "/tmp/x/capacity.json"}):
            self.assertEqual(sources.capacity_cache_path(), "/tmp/x/capacity.json")

    def test_a_null_config_block_yields_unknown_not_zero(self):
        """The chart and analyze consume config through getattr: a null
        config must read as unknown (None), never as a zero."""
        with tempfile.TemporaryDirectory() as tmp:
            cap = sources.fetch_capacity(
                False, cache_path=self._cache(tmp, _degraded_capacity_payload()))
            self.assertIsNone(getattr(cap.config, "priority_lanes", None)
                              if cap.config is not None else None)


# ---- configs: provenance, defaults, unchecked ---------------------------------

class ConfigsProvenance(unittest.TestCase):

    def _read(self, tmp, files, **env_extra):
        home = os.path.join(tmp, "home")
        for rel, content in files.items():
            path = os.path.join(home, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(content)
        env = _home_env(tmp, **env_extra)
        with mock.patch.dict(os.environ, env):
            return sources.read_configs()

    def test_declared_fact_names_file_and_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._read(tmp, {
                ".pi/agent/settings.json": json.dumps(
                    {"defaultProvider": "lunaroute", "defaultModel": "deepseek-4.1-flash"}),
            })
            fact = cfg.pi["defaultModel"]
            self.assertEqual(fact.value, "deepseek-4.1-flash")
            self.assertEqual(fact.provenance, sources.PROV_DECLARED)
            self.assertEqual(fact.source,
                             "file ~/.pi/agent/settings.json key defaultModel")

    def test_absent_key_with_documented_default_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._read(tmp, {})      # no settings file at all
            fact = cfg.claude_code["env.CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"]
            self.assertEqual(fact.value, 20)
            self.assertEqual(fact.provenance, sources.PROV_CONFIGURED)
            self.assertIn("absent → default 20", fact.source)

    def test_absent_key_without_default_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._read(tmp, {})
            fact = cfg.pi["defaultModel"]
            self.assertEqual(fact.provenance, sources.PROV_UNKNOWN)
            self.assertEqual(fact.reason, "config file absent")

    def test_unparsable_file_is_reported_unchecked(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._read(tmp, {
                ".pi/agent/settings.json": "{ this is not json",
                ".claude/settings.json": "{\"env\": oops}",
            })
            for section, key in ((cfg.pi, "httpIdleTimeoutMs"),
                                 (cfg.pi, "subagents.defaultModel"),
                                 (cfg.claude_code, "env.CLAUDE_CODE_SUBAGENT_MODEL")):
                fact = section[key]
                self.assertEqual(fact.provenance, sources.PROV_UNKNOWN, key)
                self.assertIn("unchecked", fact.reason, key)
                self.assertIsNone(fact.value, key)
            state = cfg.files["~/.pi/agent/settings.json"]
            self.assertTrue(state["present"])
            self.assertFalse(state["parsable"])
            self.assertIn("unchecked", state["note"])

    def test_codex_toml_values_are_declared_with_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._read(tmp, {
                ".codex/config.toml": ("model = \"gpt-6.1-sol\"\n"
                                       "model_reasoning_effort = \"medium\"\n"
                                       "\n[agents]\n"
                                       "max_concurrent_threads_per_session = 4\n"),
            })
            self.assertEqual(cfg.codex["model"].value, "gpt-6.1-sol")
            self.assertEqual(cfg.codex["model"].provenance, sources.PROV_DECLARED)
            self.assertEqual(
                cfg.codex["agents.max_concurrent_threads_per_session"].value, "4")
            absent = cfg.codex["agents.default_subagent_model"]
            self.assertEqual(absent.provenance, sources.PROV_UNKNOWN)

    def test_pi_model_stores_declare_sizing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._read(tmp, {
                # models-store.json: providers as a top-level map (live shape)
                ".pi/agent/models-store.json": json.dumps({
                    "lunaroute": {"models": [
                        {"id": "glm-5.3", "contextWindow": 131072, "maxTokens": 32768},
                        {"id": "no-sizes"},
                    ]}}),
                # models.json: providers under a `providers` key (local shape)
                ".pi/agent/models.json": json.dumps({
                    "providers": {"afm-mlx": {"models": [
                        {"id": "local-7b", "contextWindow": 8192}]}}}),
            })
            entry = cfg.pi_models["glm-5.3"]
            self.assertEqual(entry["contextWindow"].value, 131072)
            self.assertEqual(entry["maxTokens"].value, 32768)
            self.assertEqual(entry["contextWindow"].provenance, sources.PROV_DECLARED)
            self.assertEqual(cfg.pi_models["local-7b"]["contextWindow"].value, 8192)
            self.assertEqual(cfg.pi_models["no-sizes"]["contextWindow"].provenance,
                             sources.PROV_UNKNOWN)
            self.assertEqual(cfg.pi_models["no-sizes"]["contextWindow"].reason,
                             "not declared")

    def test_swamp_knobs_precedence_and_provenance(self):
        for knob in ("SWAMP_EXTRACT_CONCURRENCY", "SWAMP_LLM_CONCURRENCY",
                     "SWAMP_LLM_MODEL", "SWAMP_LLM_PROVIDER"):
            os.environ.pop(knob, None)
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "swamp")
            os.makedirs(repo)
            with open(os.path.join(repo, ".env.swamp"), "w") as fh:
                fh.write("SWAMP_EXTRACT_CONCURRENCY=6\n")
            knobs = sources.swamp_knobs(repo)
            self.assertEqual(knobs["extractConcurrency"],
                             {"value": 6, "source": ".env.swamp", "raw": "6"})
            with mock.patch.dict(os.environ, {"SWAMP_EXTRACT_CONCURRENCY": "3"}):
                knobs = sources.swamp_knobs(repo)
                self.assertEqual(knobs["extractConcurrency"]["source"], "env")
                self.assertEqual(knobs["extractConcurrency"]["value"], 3)
            knobs = sources.swamp_knobs(repo)
            self.assertEqual(knobs["llmConcurrency"],
                             {"value": 8, "source": "default", "raw": "8"})
            cfg = sources.read_configs(swamp_repo_dir=repo)
            fact = cfg.swamp["SWAMP_LLM_CONCURRENCY"]
            self.assertEqual(fact.provenance, sources.PROV_CONFIGURED)
            self.assertIn("absent → default 8", fact.source)
            self.assertIn("obsidian_ideas.ts", fact.source)
            fact = cfg.swamp["SWAMP_EXTRACT_CONCURRENCY"]
            self.assertEqual(fact.provenance, sources.PROV_DECLARED)
            self.assertEqual(fact.value, 6)
            self.assertIn(".env.swamp", fact.source)

    def test_swamp_yaml_rung_beats_repo_dotenv(self):
        for knob in ("SWAMP_EXTRACT_CONCURRENCY", "SWAMP_LLM_CONCURRENCY",
                     "SWAMP_LLM_MODEL", "SWAMP_LLM_PROVIDER"):
            os.environ.pop(knob, None)
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "swamp")
            os.makedirs(repo)
            with open(os.path.join(repo, ".env.swamp"), "w") as fh:
                fh.write("SWAMP_LLM_CONCURRENCY=6\n")
            env = _home_env(tmp, SWAMP_ROOT=os.path.join(tmp, "no-such-swamp"))
            home = os.path.join(tmp, "home")
            with open(os.path.join(home, ".swamp.yaml"), "w") as fh:
                fh.write("SWAMP_LLM_CONCURRENCY: 9\n")
            # the fake $HOME must be active for the read, or the real
            # ~/.swamp.yaml is what the rung consults
            with mock.patch.dict(os.environ, env):
                cfg = sources.read_configs(swamp_repo_dir=repo)
            fact = cfg.swamp["SWAMP_LLM_CONCURRENCY"]
            self.assertEqual((fact.value, fact.provenance), (9, sources.PROV_DECLARED))
            self.assertIn("~/.swamp.yaml", fact.source)


# ---- codex rollouts -----------------------------------------------------------

def _rollout_lines(now, model="gpt-6.1-sol"):
    """One turn with two progressive usage records for one response."""
    ctx = {"timestamp": _iso(now - 60), "type": "turn_context",
           "payload": {"turn_id": "t1", "model": model}}
    u1 = {"timestamp": _iso(now - 40), "type": "token_usage_record",
          "payload": {"turn_id": "t1", "response_id": "resp_a",
                      "usage": {"input_tokens": 100, "cached_input_tokens": 60,
                                "output_tokens": 5, "total_tokens": 105}}}
    u2 = {"timestamp": _iso(now - 10), "type": "token_usage_record",
          "payload": {"turn_id": "t1", "response_id": "resp_a",
                      "usage": {"input_tokens": 120, "cached_input_tokens": 60,
                                "cache_write_input_tokens": 0,
                                "reasoning_output_tokens": 20,
                                "output_tokens": 15, "total_tokens": 135}}}
    return [json.dumps(x) for x in (ctx, u1, u2)]


class CodexRollouts(unittest.TestCase):

    def _store(self, tmp, lines, name="rollout-2026-10-23T01-00-00-x.jsonl"):
        d = os.path.join(tmp, "sessions", "2026", "10", "23")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        os.utime(path, (time.time(), time.time()))
        return os.path.join(tmp, "sessions")

    def test_last_usage_record_per_response_wins(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            root = self._store(tmp, _rollout_lines(now))
            reqs = sources.iter_codex([root], now - 3600, now, prune_mtime=False)
            self.assertEqual(len(reqs), 1, "progressive updates are one response")
            req = reqs[0]
            self.assertEqual(req.response_id, "resp_a")
            self.assertEqual(req.out_tokens, 15)
            self.assertEqual(req.in_tokens, 120)
            self.assertEqual(req.usage_raw["cached_input_tokens"], 60)
            self.assertEqual(req.usage_raw["reasoning_output_tokens"], 20)
            self.assertEqual(req.model, "gpt-6.1-sol")
            self.assertAlmostEqual(req.start, now - 60, delta=2,
                                   msg="the turn_context timestamp starts the request")
            self.assertAlmostEqual(req.end, now - 10, delta=2)
            self.assertEqual(req.outcome, "completed")
            self.assertEqual(req.harness, "codex")

    def test_aborted_turn_is_classified_aborted(self):
        now = time.time()
        lines = _rollout_lines(now)
        abort = {"timestamp": _iso(now - 5), "type": "event_msg",
                 "payload": {"type": "turn_aborted", "turn_id": "t1",
                             "reason": "interrupted"}}
        lines.append(json.dumps(abort))
        with tempfile.TemporaryDirectory() as tmp:
            root = self._store(tmp, lines)
            reqs = sources.iter_codex([root], now - 3600, now, prune_mtime=False)
            self.assertEqual(reqs[0].stop_reason, "aborted")
            self.assertEqual(reqs[0].outcome, "aborted")

    def test_window_and_store_edges(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            root = self._store(tmp, _rollout_lines(now))
            # outside the window: nothing
            self.assertEqual(sources.iter_codex([root], now - 5, now, prune_mtime=False), [])
            # mtime prune hides an old-mtime file, like iter_pi on a live store
            stale = os.path.join(root, "2026", "10", "23")
            for name in os.listdir(stale):
                os.utime(os.path.join(stale, name), (now - 90 * 86400,) * 2)
            self.assertEqual(sources.iter_codex([root], now - 3600, now), [])
            self.assertEqual(len(sources.iter_codex([root], now - 3600, now,
                                                    prune_mtime=False)), 1)
            # zstd rollouts are not .jsonl and are never read
            self._store(tmp, ["{}"], name="rollout-old.jsonl.zst")
            reqs = sources.iter_codex([root], now - 3600, now, prune_mtime=False)
            self.assertEqual(len(reqs), 1)

    def test_two_turns_keep_their_own_models(self):
        now = time.time()
        lines = [
            json.dumps({"timestamp": _iso(now - 120), "type": "turn_context",
                        "payload": {"turn_id": "t1", "model": "gpt-6.1-sol"}}),
            json.dumps({"timestamp": _iso(now - 110), "type": "token_usage_record",
                        "payload": {"turn_id": "t1", "response_id": "r1",
                                    "usage": {"input_tokens": 10, "output_tokens": 1}}}),
            json.dumps({"timestamp": _iso(now - 60), "type": "turn_context",
                        "payload": {"turn_id": "t2", "model": "gpt-6.1-sol-background"}}),
            json.dumps({"timestamp": _iso(now - 50), "type": "token_usage_record",
                        "payload": {"turn_id": "t2", "response_id": "r2",
                                    "usage": {"input_tokens": 20, "output_tokens": 2}}}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = self._store(tmp, lines)
            reqs = sources.iter_codex([root], now - 3600, now, prune_mtime=False)
            by_id = {r.response_id: r for r in reqs}
            self.assertEqual(by_id["r1"].model, "gpt-6.1-sol")
            self.assertEqual(by_id["r2"].model, "gpt-6.1-sol-background")
            self.assertEqual(by_id["r2"].tier, "background")


# ---- the combined snapshot ------------------------------------------------------

def _configs_for_snapshot(default_model="lunaroute/glm-5.3"):
    return sources.Configs(
        pi={
            "defaultModel": sources.Fact(value=default_model, provenance=sources.PROV_DECLARED,
                                          source="file ~/.pi/agent/settings.json key defaultModel"),
            "subagents.defaultModel": sources.Fact(provenance=sources.PROV_UNKNOWN,
                                                   source="file ~/.pi/agent/settings.json key subagents.defaultModel",
                                                   reason="absent; children inherit defaultModel"),
            "httpIdleTimeoutMs": sources.Fact(value=900000, provenance=sources.PROV_DECLARED,
                                               source="file ~/.pi/agent/settings.json key httpIdleTimeoutMs"),
        },
        pi_models={
            "glm-5.3": {"provider": "lunaroute",
                        "source": "file ~/.pi/agent/models-store.json providers.lunaroute.models",
                        "contextWindow": sources.Fact(value=131072, provenance=sources.PROV_DECLARED,
                                                      source="models-store contextWindow"),
                        "maxTokens": sources.Fact(value=32768, provenance=sources.PROV_DECLARED,
                                                   source="models-store maxTokens")},
        },
        claude_code={},
        codex={"model": sources.Fact(value="gpt-6.1-sol", provenance=sources.PROV_DECLARED,
                                     source="file ~/.codex/config.toml key model")},
        swamp={},
    )


class SnapshotJoin(_NoNetwork, unittest.TestCase):

    def _snapshot(self, requests, tmp, configs=None, with_catalog=True, **kw):
        cache = os.path.join(tmp, "catalog.json")
        if with_catalog:
            _write_cache(cache, _models_payload(), time.time())
        # include_codex defaults to False so no test reads the real codex
        # store; a test that wants codex passes its own roots.
        kw.setdefault("include_codex", False)
        return sources.build_snapshot(requests, since=time.time() - 3600,
                                      cache_path=cache,
                                      configs=configs if configs is not None
                                      else _configs_for_snapshot(), **kw)

    def test_model_absent_from_the_catalog_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            req = cli.Request(harness="codex", session="s", model="gpt-6.1-sol",
                              start=time.time() - 30, end=time.time() - 10)
            snap = self._snapshot([req], tmp)
            doc = snap.to_dict()
            flagged = [f for f in doc["findings"] if f["kind"] == "model_not_in_catalog"]
            self.assertEqual(len(flagged), 1)
            self.assertEqual(flagged[0]["model"], "gpt-6.1-sol")
            self.assertEqual(flagged[0]["requests"], 1)
            row = doc["models"]["gpt-6.1-sol"]
            self.assertFalse(row["in_catalog"])
            self.assertEqual(row["catalog"]["provenance"], sources.PROV_UNKNOWN)
            self.assertIn("not in the live catalog", row["catalog"]["reason"])

    def test_routed_model_absent_is_flagged_with_where(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap = self._snapshot([], tmp)
            flagged = [f for f in snap.to_dict()["findings"]
                       if f["kind"] == "model_not_in_catalog"]
            self.assertEqual(flagged[0]["model"], "gpt-6.1-sol")
            self.assertIn("codex model", flagged[0]["declared_in"])

    def test_measured_model_joins_its_real_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = time.time()
            req = cli.Request(harness="pi", session="s", model="glm-5.3",
                              start=now - 30, end=now - 10,
                              usage_raw={"input": 100, "cacheRead": 50, "output": 10,
                                         "totalTokens": 160})
            snap = self._snapshot([req], tmp)
            doc = snap.to_dict()
            row = doc["models"]["glm-5.3"]
            self.assertTrue(row["in_catalog"])
            self.assertEqual(row["requests"],
                             {"value": 1, "provenance": sources.PROV_MEASURED,
                              "source": "log records in the window"})
            self.assertEqual(row["catalog"]["value"]["context_window"], 262144)
            self.assertEqual(row["catalog"]["value"]["max_output_tokens"], 65536)
            self.assertEqual(row["catalog"]["provenance"], sources.PROV_CATALOG)
            # configured-vs-real is computable: declared 131072 vs real 262144
            self.assertEqual(row["declared"]["contextWindow"]["value"], 131072)

    def test_routing_join_strips_the_provider_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap = self._snapshot([], tmp)
            routing = snap.to_dict()["routing"]
            entry = routing["pi"]["defaultModel"]
            self.assertEqual(entry["value"], "lunaroute/glm-5.3")
            self.assertEqual(entry["model_id"], "glm-5.3")
            self.assertEqual(entry["catalog"]["value"]["context_window"], 262144)
            # subagents.defaultModel absent: children inherit the declared default
            sub = routing["pi"]["subagents.defaultModel"]
            self.assertEqual(sub["model_id"], "glm-5.3")
            self.assertIn("inherit the default", sub["source"])

    def test_per_harness_token_fields_are_never_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = time.time()
            pi = cli.Request(harness="pi", session="s", model="glm-5.3",
                             start=now - 30, end=now - 10,
                             usage_raw={"input": 100, "cacheRead": 50, "cacheWrite": 5,
                                        "output": 10, "totalTokens": 160})
            codex = cli.Request(harness="codex", session="s", model="gpt-6.1-sol",
                                start=now - 25, end=now - 5,
                                usage_raw={"input_tokens": 120, "cached_input_tokens": 60,
                                           "output_tokens": 15, "total_tokens": 135})
            snap = self._snapshot([pi, codex], tmp)
            tokens = snap.to_dict()["tokens"]
            self.assertEqual(sorted(tokens), ["codex", "pi"])
            self.assertEqual(tokens["pi"],
                             {"input": 100, "cacheRead": 50, "cacheWrite": 5,
                              "output": 10, "totalTokens": 160})
            self.assertEqual(tokens["codex"],
                             {"input_tokens": 120, "cached_input_tokens": 60,
                              "output_tokens": 15, "total_tokens": 135})
            # codex's input_tokens includes its cached tokens; pi's input does
            # not include cacheRead - so no merged "input" number may exist
            self.assertNotIn("input", tokens["codex"])
            self.assertNotIn("input_tokens", tokens["pi"])
            self.assertNotIn("cacheRead", tokens["codex"])
            self.assertNotIn("cached_input_tokens", tokens["pi"])

    def test_catalog_unknown_is_unknown_everywhere_never_a_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            req = cli.Request(harness="pi", session="s", model="glm-5.3",
                              start=time.time() - 30, end=time.time() - 10)
            snap = self._snapshot([req], tmp, with_catalog=False)
            doc = snap.to_dict()
            self.assertEqual(doc["catalog"]["provenance"], sources.PROV_UNKNOWN)
            self.assertIn(doc["findings"][0]["kind"], ("catalog_unknown",))
            row = doc["models"]["glm-5.3"]
            self.assertEqual(row["catalog"]["provenance"], sources.PROV_UNKNOWN)
            self.assertIsNone(row["catalog"]["value"])
            self.assertEqual(doc["limits"]["provenance"], sources.PROV_UNKNOWN)

    def test_codex_store_joins_behind_the_existing_discovery(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            codex_root = os.path.join(tmp, "codex-store")
            d = os.path.join(codex_root, "2026")
            os.makedirs(d)
            with open(os.path.join(d, "r.jsonl"), "w") as fh:
                fh.write("\n".join(_rollout_lines(now)) + "\n")
            os.utime(os.path.join(d, "r.jsonl"), (now, now))
            snap = self._snapshot([], tmp, codex_roots=[codex_root],
                                  include_codex=True)
            doc = snap.to_dict()
            self.assertEqual(doc["tokens"].get("codex", {}).get("input_tokens"), 120)
            self.assertEqual(doc["models"]["gpt-6.1-sol"]["requests"]["value"], 1)


# ---- the CLI flags (subprocess; offline) ------------------------------------------

def _run_cli(*args):
    import subprocess
    store = tempfile.mkdtemp(prefix="luna-doctor-sources-")
    env = dict(os.environ)
    env["LUNA_DOCTOR_STORE"] = store
    env["LUNA_DOCTOR_CATALOG"] = os.path.join(store, "catalog.json")
    # hermetic: no test in this suite may read this machine's real capacity
    # cache (the fetch layer itself is tested in-process, mocked)
    env["LUNA_DOCTOR_CAPACITY"] = os.path.join(store, "absent-capacity.json")
    proc = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "luna-doctor"), *args],
                          capture_output=True, text=True, timeout=180, cwd=ROOT, env=env)
    return proc.returncode, proc.stdout, proc.stderr


class NetworkFlags(unittest.TestCase):

    def test_offline_analyze_reports_an_unknown_catalog(self):
        code, out, err = _run_cli("analyze", "--since", "1h", "--json", "--offline")
        self.assertIn(code, (0, 1, 2), err)
        doc = json.loads(out)
        self.assertEqual(doc["schema"], 4)
        snap = doc["metrics"]["snapshot"]
        self.assertEqual(snap["catalog"]["provenance"], "unknown",
                         "with no cache and no network the catalog is unknown, not defaulted")
        self.assertEqual(snap["limits"]["provenance"], "unknown")

    def test_online_and_offline_are_mutually_exclusive(self):
        code, out, err = _run_cli("analyze", "--online", "--offline")
        self.assertEqual(code, 2)
        self.assertIn("not allowed with", err)

    def test_offline_fix_still_renders_instructions(self):
        code, out, err = _run_cli("fix", "--since", "1h", "--offline")
        self.assertIn(code, (0, 1), err)
        self.assertIn("does not edit files itself", out)


if __name__ == "__main__":
    unittest.main()
