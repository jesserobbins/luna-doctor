"""Tests for luna_doctor/sizing.py - the sizing-misconfiguration family.

Offline by construction: the catalog comes from a temp cache file (never the
real one), the configs from a fake $HOME, and sources._urlopen is patched to
explode so no test can reach the network seam. The cases are the design's
success-test table (2026-10-23-sizing-misconfiguration-rules.md): the
expected findings against the recorded state, the *nothing* rows - the
important half, because a lint that flags a correct config is worse than no
lint - and every near-miss guard.

The catalog fixture is the verified live table from the design, so the
arithmetic these tests assert (12% of the window, 6% of the output budget,
15.6x, 3.9x) is falsifiable against the design's own numbers regardless of
what any real config file says today.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from luna_doctor import cli              # noqa: E402
from luna_doctor import sizing           # noqa: E402
from luna_doctor import sources          # noqa: E402

CLI_BIN = os.path.join(ROOT, "bin", "luna-doctor")


# ---- fixtures -----------------------------------------------------------------

def catalog_payload():
    """The verified live /v1/models table from the sizing design.

    clef-flash and djev carry no client_compat block at all - the two
    smallest-window models in the catalog - so any rule that gated on
    client_compat would leave them unaudited; these tests would catch that.
    context_length / max_completion_tokens ride along as the observed
    synonyms of the primary keys.
    """
    def model(mid, cw, mo, compat=True):
        entry = {"id": mid, "object": "model", "owned_by": "lunaroute",
                 "context_window": cw, "context_length": cw,
                 "max_input_tokens": cw, "max_output_tokens": mo,
                 "max_completion_tokens": mo}
        if compat:
            entry["client_compat"] = {"pi": {"maxTokensField": "max_tokens"}}
        return entry
    return {"object": "list", "data": [
        model("clef-flash", 8192, 2048, compat=False),
        model("djev", 32768, 1024, compat=False),
        model("beam", 262144, 65536),
        model("beam-background", 262144, 65536),
        model("glm-5.3", 524288, 131072),
        model("glm-5.3-background", 524288, 131072),
        model("glm-5.3-flash", 524288, 163840),
        model("glm-5.3-flash-background", 524288, 163840),
        model("deepseek-4.1-flash", 1048576, 262144),
        model("deepseek-4.1-flash-background", 1048576, 262144),
    ]}


def pi_settings(default="deepseek-4.1-flash", subagents=None):
    doc = {"defaultProvider": "lunaroute"}
    if default is not None:
        doc["defaultModel"] = default
    if subagents is not None:
        doc["subagents"] = {"defaultModel": subagents}
    return json.dumps(doc)


def pi_store(models):
    """models-store.json content; each model is (id, contextWindow, maxTokens),
    None for an unset key (an entry that declares any value is a decision)."""
    entries = []
    for mid, cw, mo in models:
        entry = {"id": mid}
        if cw is not None:
            entry["contextWindow"] = cw
        if mo is not None:
            entry["maxTokens"] = mo
        entries.append(entry)
    return json.dumps({"lunaroute": {"models": entries}})


def claude_settings(env=None, extra=None):
    doc = {}
    if env is not None:
        doc["env"] = env
    doc.update(extra or {})
    return json.dumps(doc)


def codex_config(model="gpt-6.1-sol", window=None):
    lines = []
    if model is not None:
        lines.append(f'model = "{model}"')
    if window is not None:
        lines.append(f"model_context_window = {window}")
    return "\n".join(lines) + "\n"


def _boom(req, timeout=None):
    raise AssertionError("a sizing test reached the network seam unpatched")


def _snapshot(files, requests=(), stale=False, with_catalog=True):
    """One snapshot from a fake $HOME, a fixture catalog and no network."""
    tmp = tempfile.mkdtemp(prefix="luna-doctor-sizing-")
    home = os.path.join(tmp, "home")
    for rel, content in (files or {}).items():
        path = os.path.join(home, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)
    os.makedirs(home, exist_ok=True)
    cache = os.path.join(tmp, "catalog.json")
    fetched = time.time() - (sources.CATALOG_TTL_S + 10) if stale else time.time() - 60
    if with_catalog:
        sources._write_catalog_cache(cache, catalog_payload(), fetched)
    env = {"HOME": home, "LUNAROUTE_API_KEY": "",
           "SWAMP_ROOT": os.path.join(tmp, "no-such-swamp")}
    with mock.patch.dict(os.environ, env):
        with mock.patch.object(sources, "_urlopen", side_effect=_boom):
            configs = sources.read_configs(swamp_repo_dir=os.path.join(tmp, "no-such"))
            snap = sources.build_snapshot(
                list(requests), since=time.time() - 3600, until=time.time(),
                allow_network=False, include_codex=False, configs=configs,
                cache_path=cache)
    return snap


def rows_of(snap):
    return sizing.evaluate(snap)["rows"]


def find(rows, rule, model=None, status=None, key=None):
    return [r for r in rows
            if r["rule"] == rule
            and (model is None or r["model"] == model)
            and (status is None or r["status"] == status)
            and (key is None or r["key"] == key)]


def findings(rows):
    return [r for r in rows if r["status"] == "finding"]


def req(model, outcome=None, error=None):
    now = time.time()
    return cli.Request(harness="pi", session="s", model=model,
                       start=now - 30, end=now - 10, outcome=outcome,
                       error_message=error)


# ---- the pi engine: expected findings and nothing-rows ---------------------------

class PiEngine(unittest.TestCase):
    """The design's success-test rows for pi, against the design's catalog."""

    def test_recorded_state_unset_deepseek_fires_underuse(self):
        """The 2026-10-21 recorded state: both deepseek twins routed, no
        sizing keys anywhere (the store file is absent, so the default is in
        force and is judged) - 12% of the window, 6% of the output budget."""
        snap = _snapshot({".pi/agent/settings.json": pi_settings(
            subagents="lunaroute/deepseek-4.1-flash-background")})
        rows = rows_of(snap)
        found = find(rows, sizing.RULE_UNSET_UNDERUSES, status="finding")
        self.assertEqual(len(found), 4, "both models x both dimensions")
        for mid in ("deepseek-4.1-flash", "deepseek-4.1-flash-background"):
            win = find(found, sizing.RULE_UNSET_UNDERUSES, model=mid,
                       key="contextWindow")
            self.assertEqual(len(win), 1)
            self.assertIn("unset -> default 128,000; catalog context_window "
                          "1,048,576 - running at 12% of the window",
                          win[0]["detail"])
            out = find(found, sizing.RULE_UNSET_UNDERUSES, model=mid,
                       key="maxTokens")
            self.assertEqual(len(out), 1)
            self.assertIn("unset -> default 16,384; catalog max_output_tokens "
                          "262,144 - running at 6% of the output budget",
                          out[0]["detail"])
        self.assertEqual([r["rule"] for r in findings(rows)],
                         [sizing.RULE_UNSET_UNDERUSES] * 4,
                         "nothing else fires on an unset deepseek")

    def test_unset_small_model_overpromises_both_dimensions(self):
        """clef-flash unset: the identical file state that underuses deepseek
        is the opposite verdict here - only the catalog join tells them
        apart. (clef-flash carries no client_compat; the rules must not.)"""
        snap = _snapshot({".pi/agent/settings.json": pi_settings(default="clef-flash")})
        rows = rows_of(snap)
        win = find(rows, sizing.RULE_EXCEEDS_WINDOW, "clef-flash", "finding")
        self.assertEqual(len(win), 1)
        self.assertIn("15.6x the real window", win[0]["detail"])
        self.assertIn("will not compact until after the model starts refusing",
                      win[0]["detail"])
        out = find(rows, sizing.RULE_OUTPUT_EXCEEDS_MAX, "clef-flash", "finding")
        self.assertEqual(len(out), 1)
        self.assertIn("8x the model's max output", out[0]["detail"])
        self.assertEqual(len(findings(rows)), 2)
        self.assertNotIn(sizing.RULE_UNSET_UNDERUSES,
                         {r["rule"] for r in rows},
                         "underuse cannot fire when the default over-promises")

    def test_unset_djev_overpromises_window(self):
        snap = _snapshot({".pi/agent/settings.json": pi_settings(default="djev")})
        rows = rows_of(snap)
        win = find(rows, sizing.RULE_EXCEEDS_WINDOW, "djev", "finding")
        self.assertEqual(len(win), 1)
        self.assertIn("3.9x the real window", win[0]["detail"])

    def test_declared_at_catalog_is_clean(self):
        """The *nothing* row: clef-flash declared at 8,192/2,048 is exactly
        right - a deliberately small model at its real size. A finding here
        means the deliberate-small guard is inverted."""
        snap = _snapshot({
            ".pi/agent/settings.json": pi_settings(default="clef-flash"),
            ".pi/agent/models-store.json": pi_store(
                [("clef-flash", 8192, 2048)]),
        })
        rows = rows_of(snap)
        self.assertEqual(findings(rows), [])
        clean = find(rows, sizing.RULE_EXCEEDS_WINDOW, "clef-flash", "clean")
        self.assertEqual(len(clean), 1)
        self.assertIn("exact match, correct", clean[0]["detail"])
        out = find(rows, sizing.RULE_OUTPUT_EXCEEDS_MAX, "clef-flash", "clean")
        self.assertEqual(len(out), 1)
        self.assertIn("exact match, correct", out[0]["detail"])

    def test_deliberate_cap_is_clean(self):
        """The *nothing* row: glm-5.3 declared at 65536/8192 is a decision,
        not an error - and the double-book check must not fire either."""
        snap = _snapshot({
            ".pi/agent/settings.json": pi_settings(default="glm-5.3"),
            ".pi/agent/models-store.json": pi_store(
                [("glm-5.3", 65536, 8192)]),
        })
        rows = rows_of(snap)
        self.assertEqual(findings(rows), [])
        self.assertEqual(find(rows, sizing.RULE_INPUT_PLUS_OUTPUT), [])
        clean = find(rows, sizing.RULE_EXCEEDS_WINDOW, "glm-5.3", "clean")
        self.assertIn("deliberate cap, clean", clean[0]["detail"])

    def test_declared_window_above_catalog_fires(self):
        snap = _snapshot({
            ".pi/agent/settings.json": pi_settings(default="glm-5.3"),
            ".pi/agent/models-store.json": pi_store(
                [("glm-5.3", 600000, None)]),
        })
        rows = rows_of(snap)
        win = find(rows, sizing.RULE_EXCEEDS_WINDOW, "glm-5.3", "finding")
        self.assertEqual(len(win), 1)
        self.assertIn("declared 600,000 above catalog context_window 524,288",
                      win[0]["detail"])
        self.assertEqual(win[0]["severity"], "high")
        # the unset output dimension is judged independently: underuse fires
        self.assertEqual(len(find(rows, sizing.RULE_UNSET_UNDERUSES,
                                  status="finding")), 1)

    def test_declared_output_above_catalog_fires(self):
        snap = _snapshot({
            ".pi/agent/settings.json": pi_settings(default="glm-5.3"),
            ".pi/agent/models-store.json": pi_store(
                [("glm-5.3", None, 200000)]),
        })
        rows = rows_of(snap)
        out = find(rows, sizing.RULE_OUTPUT_EXCEEDS_MAX, "glm-5.3", "finding")
        self.assertEqual(len(out), 1)
        self.assertIn("declared 200,000 above catalog max_output_tokens 131,072",
                      out[0]["detail"])
        self.assertEqual(out[0]["severity"], "medium")

    def test_output_ge_window_fires_the_double_book_rule(self):
        """The design's typical trigger: the catalog's max_output copied into
        maxTokens while contextWindow stays at the default - a mixed
        declared/default pair the default pair never produces."""
        snap = _snapshot({
            ".pi/agent/settings.json": pi_settings(default="glm-5.3"),
            ".pi/agent/models-store.json": pi_store(
                [("glm-5.3", None, 262144)]),
        })
        rows = rows_of(snap)
        dbl = find(rows, sizing.RULE_INPUT_PLUS_OUTPUT, "glm-5.3", "finding")
        self.assertEqual(len(dbl), 1)
        self.assertEqual(dbl[0]["values"], {"window": 128000, "output": 262144})
        self.assertIn("input and output budgets share the window",
                      dbl[0]["detail"])
        # and the same session is over the output max as well
        self.assertEqual(len(find(rows, sizing.RULE_OUTPUT_EXCEEDS_MAX,
                                  status="finding")), 1)

    def test_output_equal_to_window_fires_the_double_book_rule(self):
        """The boundary is inclusive: an output reservation the size of the
        window leaves zero input, so output == window fires."""
        snap = _snapshot({
            ".pi/agent/settings.json": pi_settings(default="glm-5.3"),
            ".pi/agent/models-store.json": pi_store(
                [("glm-5.3", 65536, 65536)]),
        })
        rows = rows_of(snap)
        dbl = find(rows, sizing.RULE_INPUT_PLUS_OUTPUT, "glm-5.3", "finding")
        self.assertEqual(len(dbl), 1)
        self.assertEqual(dbl[0]["values"], {"window": 65536, "output": 65536})

    def test_output_below_window_never_double_books(self):
        snap = _snapshot({
            ".pi/agent/settings.json": pi_settings(default="glm-5.3"),
            ".pi/agent/models-store.json": pi_store(
                [("glm-5.3", 65536, 32768)]),
        })
        rows = rows_of(snap)
        self.assertEqual(find(rows, sizing.RULE_INPUT_PLUS_OUTPUT,
                              status="finding"), [])

    def test_background_twin_does_not_inherit_declared_sizing(self):
        """Catalog lookup and declared state are per exact id: sizing for
        glm-5.3 says nothing about glm-5.3-background."""
        snap = _snapshot({
            ".pi/agent/settings.json": pi_settings(
                default="glm-5.3",
                subagents="glm-5.3-background"),
            ".pi/agent/models-store.json": pi_store(
                [("glm-5.3", 524288, 131072)]),
        })
        rows = rows_of(snap)
        self.assertEqual(findings(find(rows, sizing.RULE_EXCEEDS_WINDOW,
                                       "glm-5.3")), [])
        # the twin is unset: the shipped default is in force and underuses it
        self.assertEqual(len(find(rows, sizing.RULE_UNSET_UNDERUSES,
                                  "glm-5.3-background", "finding")), 2)

    def test_no_fuzzy_catalog_resolution(self):
        """glm-5.3-x must never resolve to glm-5.3: exact id or unchecked."""
        snap = _snapshot({
            ".pi/agent/settings.json": pi_settings(default="glm-5.3-x"),
            ".pi/agent/models-store.json": pi_store(
                [("glm-5.3", 524288, 131072)]),
        })
        rows = rows_of(snap)
        self.assertEqual(findings(rows), [])
        self.assertEqual({r["model"] for r in rows}, {"glm-5.3-x"})
        self.assertTrue(all(r["status"] == "unchecked" for r in rows))
        self.assertTrue(all("model not in catalog" in r["detail"] for r in rows))

    def test_non_catalog_model_is_unchecked_and_defers(self):
        """A model the catalog does not serve is unchecked, not wrong, and
        the routing finding belongs to model-not-lunaroute."""
        snap = _snapshot({".pi/agent/settings.json": pi_settings(
            default="gpt-6.1-sol")})
        rows = rows_of(snap)
        self.assertEqual(findings(rows), [])
        unchecked = [r for r in rows if r["status"] == "unchecked"]
        self.assertTrue(unchecked)
        for r in unchecked:
            self.assertIn("model not in catalog", r["detail"])
            self.assertIn(sizing.ROUTING_RULE, r["detail"])


# ---- pi: absent vs unreadable, catalog states ------------------------------------

class UncheckedStates(unittest.TestCase):
    """Absent file != unreadable file; no catalog, no verdict."""

    def test_absent_store_is_judged_not_unchecked(self):
        # no models-store.json at all: the default is in force and fires
        snap = _snapshot({".pi/agent/settings.json": pi_settings(
            default="deepseek-4.1-flash")})
        self.assertEqual(len(findings(rows_of(snap))), 2)

    def test_unparsable_store_is_unchecked_never_clean(self):
        snap = _snapshot({
            ".pi/agent/settings.json": pi_settings(default="glm-5.3"),
            ".pi/agent/models-store.json": "{ this is not json",
        })
        rows = rows_of(snap)
        self.assertEqual(findings(rows), [])
        unchecked = [r for r in rows if r["status"] == "unchecked"]
        self.assertTrue(unchecked, "an unparsable store could hide a declaration")
        self.assertIn("unparsable", unchecked[0]["detail"])

    def test_unparsable_settings_leave_sizing_unchecked(self):
        snap = _snapshot({".pi/agent/settings.json": "{ this is not json"})
        rows = rows_of(snap)
        self.assertEqual(findings(rows), [])
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("unparsable" in r["detail"] for r in rows))

    def test_stale_catalog_is_unchecked_never_clean(self):
        snap = _snapshot({".pi/agent/settings.json": pi_settings(
            default="deepseek-4.1-flash")}, stale=True)
        rows = rows_of(snap)
        self.assertEqual(findings(rows), [])
        self.assertTrue(all(r["status"] == "unchecked" for r in rows))
        self.assertIn("stale", sizing.evaluate(snap)["catalog"]["reason"])

    def test_missing_catalog_is_unchecked_never_clean(self):
        snap = _snapshot({".pi/agent/settings.json": pi_settings(
            default="deepseek-4.1-flash")}, with_catalog=False)
        rows = rows_of(snap)
        self.assertEqual(findings(rows), [])
        self.assertTrue(all(r["status"] == "unchecked" for r in rows))
        block = sizing.evaluate(snap)
        self.assertEqual(block["catalog"]["provenance"], sources.PROV_UNKNOWN)

    def test_no_snapshot_is_explicitly_unchecked(self):
        block = sizing.evaluate(None)
        self.assertEqual(block["rows"], [])
        self.assertEqual(block["catalog"]["provenance"], sources.PROV_UNKNOWN)
        self.assertIn("never clean", block["catalog"]["reason"])

    def test_unset_within_catalog_prints_the_clean_arithmetic(self):
        """beam sits just above the 2x fire line, so it fires; a catalog value
        between the default and 2x it would print clean instead."""
        snap = _snapshot({".pi/agent/settings.json": pi_settings(default="beam")})
        rows = rows_of(snap)
        self.assertEqual(len(find(rows, sizing.RULE_UNSET_UNDERUSES,
                                  "beam", "finding")), 2,
                         "262,144 is 2.05x the default: both dimensions fire")
        snap = _snapshot({".pi/agent/settings.json": pi_settings(
            default="deepseek-4.1-flash"),
            ".pi/agent/models-store.json": pi_store(
                [("deepseek-4.1-flash", 200000, 20000)])})
        rows = rows_of(snap)
        self.assertEqual(findings(rows), [])
        clean = [r for r in rows if r["status"] == "clean"]
        self.assertEqual(len(clean), 2)


# ---- claude-code: V key, U default ------------------------------------------------

class ClaudeCodeRules(unittest.TestCase):

    def test_unset_output_key_is_informational_never_finding(self):
        """The success-test row: the key unset and the numeric default U
        renders 'unset; shipped default unverified', not a finding."""
        snap = _snapshot({".claude/settings.json": claude_settings(env={})})
        rows = rows_of(snap)
        out = find(rows, sizing.RULE_OUTPUT_EXCEEDS_MAX, status="informational")
        self.assertEqual(len(out), 1)
        self.assertIn("CLAUDE_CODE_MAX_OUTPUT_TOKENS unset; the shipped default "
                      "is unverified", out[0]["detail"])
        self.assertEqual(findings(rows), [])

    def test_declared_output_above_max_stays_informational(self):
        """The clamp fork: the key is V, but reject-vs-clamp is not, so even
        a declared over-max stays informational."""
        snap = _snapshot({".claude/settings.json": claude_settings(env={
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "200000",
            "CLAUDE_CODE_SUBAGENT_MODEL": "glm-5.3-flash",
        })})
        rows = rows_of(snap)
        out = find(rows, sizing.RULE_OUTPUT_EXCEEDS_MAX, "glm-5.3-flash")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["status"], "informational")
        self.assertIn("declared 200,000 above catalog max_output_tokens 163,840",
                      out[0]["detail"])
        self.assertIn("reject vs clamp is unverified", out[0]["detail"])
        self.assertEqual(findings(rows), [])

    def test_declared_output_within_max_is_clean(self):
        snap = _snapshot({".claude/settings.json": claude_settings(env={
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "100000",
            "CLAUDE_CODE_SUBAGENT_MODEL": "glm-5.3-flash",
        })})
        rows = rows_of(snap)
        out = find(rows, sizing.RULE_OUTPUT_EXCEEDS_MAX, "glm-5.3-flash", "clean")
        self.assertEqual(len(out), 1)

    def test_window_key_is_informational_in_both_directions(self):
        """claude-sizing-window: U throughout - set, unset, over, under; the
        line exists, the finding never does."""
        for env, expect in (
                ({}, "unset"),
                ({"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "600000",
                  "CLAUDE_CODE_SUBAGENT_MODEL": "glm-5.3"},
                 "declared 600,000 vs catalog context_window 524,288"),
        ):
            snap = _snapshot({".claude/settings.json": claude_settings(env=env)})
            rows = rows_of(snap)
            win = find(rows, sizing.RULE_CLAUDE_WINDOW)
            self.assertEqual(len(win), 1)
            self.assertEqual(win[0]["status"], "informational")
            self.assertEqual(win[0]["vu"], "U")
            self.assertIn(expect, win[0]["detail"])
            self.assertIn("never a finding", win[0]["detail"])
            self.assertEqual(findings(rows), [])

    def test_declared_but_unreadable_value_is_unchecked(self):
        snap = _snapshot({".claude/settings.json": claude_settings(env={
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "auto",
        })})
        rows = rows_of(snap)
        self.assertEqual(findings(rows), [])
        unchecked = [r for r in rows if r["status"] == "unchecked"]
        self.assertEqual(len(unchecked), 1)
        self.assertIn("not a number", unchecked[0]["detail"])

    def test_declared_output_without_a_model_is_unchecked(self):
        snap = _snapshot({".claude/settings.json": claude_settings(env={
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "200000",
        })})
        rows = rows_of(snap)
        out = find(rows, sizing.RULE_OUTPUT_EXCEEDS_MAX, None, "unchecked")
        self.assertEqual(len(out), 1)
        self.assertIn("no claude-code model is declared", out[0]["detail"])

    def test_absent_claude_config_is_silent(self):
        snap = _snapshot({})
        rows = rows_of(snap)
        self.assertEqual([r for r in rows if r["harness"] == "claude-code"], [])


# ---- codex: V-declared-only --------------------------------------------------------

class CodexRules(unittest.TestCase):

    def test_declared_window_above_catalog_fires(self):
        snap = _snapshot({".codex/config.toml": codex_config(
            model="glm-5.3", window=600000)})
        rows = rows_of(snap)
        win = find(rows, sizing.RULE_CODEX_WINDOW, "glm-5.3", "finding")
        self.assertEqual(len(win), 1)
        self.assertIn("declared 600,000 above catalog context_window 524,288",
                      win[0]["detail"])

    def test_declared_window_within_catalog_is_clean(self):
        snap = _snapshot({".codex/config.toml": codex_config(
            model="glm-5.3", window=262144)})
        rows = rows_of(snap)
        self.assertEqual(findings(rows), [])
        win = find(rows, sizing.RULE_CODEX_WINDOW, "glm-5.3", "clean")
        self.assertEqual(len(win), 1)

    def test_non_catalog_model_is_unchecked_not_wrong(self):
        """The success-test row: this machine's recorded codex state - a
        declared window, a routed model the catalog does not serve."""
        snap = _snapshot({".codex/config.toml": codex_config(
            model="gpt-6.1-sol", window=262144)})
        rows = rows_of(snap)
        self.assertEqual(findings(rows), [])
        win = find(rows, sizing.RULE_CODEX_WINDOW, "gpt-6.1-sol", "unchecked")
        self.assertEqual(len(win), 1)
        self.assertIn("model not in catalog", win[0]["detail"])
        self.assertIn(sizing.ROUTING_RULE, win[0]["detail"])
        # the observed 262,144 is one machine's declared value, never a default
        self.assertNotIn("262,144", win[0]["detail"])

    def test_unset_window_is_informational(self):
        snap = _snapshot({".codex/config.toml": codex_config(model="glm-5.3")})
        rows = rows_of(snap)
        win = find(rows, sizing.RULE_CODEX_WINDOW, "glm-5.3", "informational")
        self.assertEqual(len(win), 1)
        self.assertIn("model_context_window unset; the shipped default is "
                      "unverified", win[0]["detail"])

    def test_absent_codex_config_is_silent(self):
        snap = _snapshot({})
        self.assertEqual([r for r in rows_of(snap) if r["harness"] == "codex"], [])


# ---- corroboration: the log half of the run -----------------------------------------

class Corroboration(unittest.TestCase):

    def test_context_overruns_corroborate_the_window_rule(self):
        snap = _snapshot({".pi/agent/settings.json": pi_settings(default="clef-flash")},
                         requests=[req("clef-flash", outcome="context_length_exceeded"),
                                   req("clef-flash", outcome="context_length_exceeded")])
        rows = rows_of(snap)
        win = find(rows, sizing.RULE_EXCEEDS_WINDOW, "clef-flash", "finding")[0]
        self.assertTrue(win["corroborated"])
        self.assertIn("2 context_length_exceeded request(s)", win["corroboration"])

    def test_max_tokens_rejections_corroborate_the_output_rule(self):
        snap = _snapshot({
            ".pi/agent/settings.json": pi_settings(default="glm-5.3"),
            ".pi/agent/models-store.json": pi_store([("glm-5.3", None, 200000)]),
        }, requests=[req("glm-5.3", outcome="other_error",
                         error="'max_tokens' is too large: 200000 > 131072")])
        rows = rows_of(snap)
        out = find(rows, sizing.RULE_OUTPUT_EXCEEDS_MAX, "glm-5.3", "finding")[0]
        self.assertTrue(out["corroborated"])
        self.assertIn("naming max_tokens", out["corroboration"])

    def test_context_length_errors_never_corroborate_the_output_rule(self):
        """The standard over-window error breaks its token count down by
        max_tokens too; a naive join would misfile it."""
        snap = _snapshot({
            ".pi/agent/settings.json": pi_settings(default="glm-5.3"),
            ".pi/agent/models-store.json": pi_store([("glm-5.3", None, 200000)]),
        }, requests=[req("glm-5.3", outcome="context_length_exceeded",
                         error="maximum context length is 524288 tokens, "
                               "however you requested 600000 (400000 in "
                               "messages, 200000 in max_tokens)")])
        rows = rows_of(snap)
        out = find(rows, sizing.RULE_OUTPUT_EXCEEDS_MAX, "glm-5.3", "finding")[0]
        self.assertNotIn("corroborated", out)

    def test_clean_rows_are_never_corroborated(self):
        snap = _snapshot({
            ".pi/agent/settings.json": pi_settings(default="glm-5.3"),
            ".pi/agent/models-store.json": pi_store([("glm-5.3", 524288, 131072)]),
        }, requests=[req("glm-5.3", outcome="context_length_exceeded")])
        rows = rows_of(snap)
        self.assertEqual(findings(rows), [],
                         "a declared-at-catalog entry is clean even when the "
                         "log shows the class; the backwards join is the "
                         "catalog-drift finding, not this family's")


# ---- the V/U discipline, as a property ----------------------------------------------

class VUDiscipline(unittest.TestCase):

    CASES = [
        {".pi/agent/settings.json": pi_settings(
            subagents="lunaroute/deepseek-4.1-flash-background")},
        {".pi/agent/settings.json": pi_settings(default="clef-flash")},
        {".pi/agent/settings.json": pi_settings(default="glm-5.3"),
         ".pi/agent/models-store.json": pi_store([("glm-5.3", 65536, 262144)])},
        {".pi/agent/settings.json": pi_settings(default="gpt-6.1-sol")},
        {".claude/settings.json": claude_settings(env={
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "200000",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "600000",
            "CLAUDE_CODE_SUBAGENT_MODEL": "glm-5.3"})},
        {".codex/config.toml": codex_config(model="glm-5.3", window=600000)},
        {".codex/config.toml": codex_config(model="gpt-6.1-sol", window=262144)},
    ]

    def test_only_v_backed_rules_raise_findings(self):
        for files in self.CASES:
            with self.subTest(files=sorted(files)):
                rows = rows_of(_snapshot(files))
                for r in rows:
                    if r["status"] == "finding":
                        self.assertIn("V", r["vu"],
                                      f"a U-backed row raised a finding: {r}")
                    if r["rule"] == sizing.RULE_CLAUDE_WINDOW:
                        self.assertNotEqual(r["status"], "finding",
                                            "claude-sizing-window is U: "
                                            "informational line only, ever")
                    if (r["harness"] == "claude-code"
                            and r["rule"] == sizing.RULE_OUTPUT_EXCEEDS_MAX):
                        self.assertIn(r["status"], ("informational", "clean",
                                                    "unchecked"),
                                      "the claude-code arm never raises a "
                                      "finding until reject-vs-clamp is V")

    def test_every_row_is_self_describing(self):
        for files in self.CASES:
            with self.subTest(files=sorted(files)):
                block = sizing.evaluate(_snapshot(files))
                for r in block["rows"]:
                    self.assertIn(r["rule"], (sizing.RULE_UNSET_UNDERUSES,
                                              sizing.RULE_EXCEEDS_WINDOW,
                                              sizing.RULE_OUTPUT_EXCEEDS_MAX,
                                              sizing.RULE_INPUT_PLUS_OUTPUT,
                                              sizing.RULE_CODEX_WINDOW,
                                              sizing.RULE_CLAUDE_WINDOW))
                    self.assertIn(r["status"], ("finding", "clean",
                                               "unchecked", "informational"))
                    self.assertTrue(r["detail"])
                    self.assertTrue(r["file"])
                self.assertIn("checked now", block["note"])
                self.assertIn("underuse_fire_line",
                              block["defaults"])
                self.assertIn(sizing.PI_DEFAULT_CITATION,
                              block["defaults"]["pi"]["citation"])


# ---- the wiring: fix output and analyze --json ---------------------------------------

def _run_cli(env_extra, *args):
    env = dict(os.environ)
    env["LUNA_DOCTOR_STORE"] = tempfile.mkdtemp(prefix="luna-doctor-sizing-cli-")
    env.update(env_extra)
    proc = subprocess.run([sys.executable, CLI_BIN, *args],
                          capture_output=True, text=True, timeout=180,
                          cwd=ROOT, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def _fake_machine(files):
    """A fake $HOME plus its own catalog cache, hermetic for the CLI."""
    tmp = tempfile.mkdtemp(prefix="luna-doctor-sizing-home-")
    home = os.path.join(tmp, "home")
    for rel, content in files.items():
        path = os.path.join(home, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)
    cache = os.path.join(tmp, "catalog.json")
    sources._write_catalog_cache(cache, catalog_payload(), time.time())
    env = {"HOME": home, "LUNAROUTE_API_KEY": "",
           "SWAMP_ROOT": os.path.join(tmp, "no-such-swamp"),
           "LUNAROUTE_KEY": "",
           "LUNA_DOCTOR_CATALOG": cache}
    return env


class FixAndAnalyzeWiring(unittest.TestCase):

    def test_analyze_json_carries_the_sizing_block(self):
        env = _fake_machine({".pi/agent/settings.json": pi_settings(
            subagents="lunaroute/deepseek-4.1-flash-background")})
        code, out, err = _run_cli(env, "analyze", "--since", "1h", "--json")
        self.assertIn(code, (0, 1, 2), err)
        doc = json.loads(out)
        self.assertEqual(doc["schema"], 4, "the sizing block is additive")
        block = doc["metrics"]["sizing"]
        rules = {r["rule"] for r in block["rows"]}
        self.assertIn(sizing.RULE_UNSET_UNDERUSES, rules)
        self.assertIn("12% of the window",
                      " ".join(r["detail"] for r in block["rows"]))

    def test_fix_renders_sizing_instructions_and_sets_exit_1(self):
        env = _fake_machine({".pi/agent/settings.json": pi_settings(
            subagents="lunaroute/deepseek-4.1-flash-background")})
        code, out, err = _run_cli(env, "fix", "--since", "24h")
        self.assertEqual(code, 1, err)
        self.assertIn("does not edit files itself", out)
        self.assertIn("models[deepseek-4.1-flash].contextWindow", out)
        self.assertIn("unset (default 128,000)", out)
        self.assertIn("at 12% of the window", out)
        self.assertIn("at 6% of the output budget", out)
        self.assertIn("restart the harness", out)
        for bad in ('jq \'', ' > "$f.tmp"', 'cp "$f"', "sed -i"):
            self.assertNotIn(bad, out, "fix emitted shell code")

    def test_fix_informational_lines_are_recorded_not_actionable(self):
        """U-backed lines ride as from == to changes: present in the output,
        silent in the exit code."""
        env = _fake_machine({".claude/settings.json": claude_settings(env={})})
        code, out, err = _run_cli(env, "fix", "--since", "24h", "--agent",
                                  "claude-code", "--json")
        self.assertIn(code, (0, 1), err)
        doc = json.loads(out)
        changes = [ch for g in doc["agents"] for ch in g["changes"]
                   if ch.get("key") in ("env.CLAUDE_CODE_MAX_OUTPUT_TOKENS",
                                        "env.CLAUDE_CODE_MAX_CONTEXT_TOKENS")]
        self.assertEqual(len(changes), 2,
                         "both U lines are printed, not swallowed")
        for ch in changes:
            self.assertEqual(ch["from"], ch["to"])

    def test_fix_says_so_when_the_catalog_is_unavailable(self):
        """Routed models + no catalog: sizing is unchecked, never clean, and
        fix must not let a silent sizing section read as checked-and-clean."""
        tmp = tempfile.mkdtemp(prefix="luna-doctor-sizing-nocat-")
        home = os.path.join(tmp, "home")
        os.makedirs(os.path.join(home, ".pi/agent"), exist_ok=True)
        with open(os.path.join(home, ".pi/agent/settings.json"), "w") as fh:
            fh.write(pi_settings(default="deepseek-4.1-flash"))
        env = {"HOME": home, "LUNAROUTE_API_KEY": "",
               "LUNAROUTE_KEY": "",
               "SWAMP_ROOT": os.path.join(tmp, "no-such-swamp"),
               "LUNA_DOCTOR_CATALOG": os.path.join(tmp, "absent-catalog.json")}
        code, out, err = _run_cli(env, "fix", "--since", "24h", "--agent", "pi",
                                  "--json")
        self.assertIn(code, (0, 1), err)
        doc = json.loads(out)
        keys = [str(ch.get("key")) for g in doc["agents"]
                for ch in g["changes"]]
        self.assertIn("model sizing (catalog join)", keys)
        for g in doc["agents"]:
            for ch in g["changes"]:
                if ch["key"] == "model sizing (catalog join)":
                    self.assertEqual(ch["from"], ch["to"], "never actionable")
                    self.assertIn("unchecked", ch["why"])

    def test_recommendations_merge_sizing_into_the_owning_agent(self):
        snap = _snapshot({
            ".pi/agent/settings.json": pi_settings(
                subagents="lunaroute/deepseek-4.1-flash-background"),
            ".codex/config.toml": codex_config(model="glm-5.3", window=600000),
        })
        ev = sizing.evaluate(snap)
        m = {"peak_concurrency": 0, "by_outcome": {}, "by_tier": {},
             "window_hours": 24.0}
        recs = cli.recommendations(m, [], ["pi", "codex"], configs=snap.configs,
                                   catalog_ids=[], sizing_eval=ev)
        by_agent = {g["agent"]: g for g in recs}
        self.assertIn("pi", by_agent)
        keys = [ch["key"] for ch in by_agent["pi"]["changes"]]
        self.assertIn("models[deepseek-4.1-flash].contextWindow", keys)
        self.assertIn("models[deepseek-4.1-flash-background].maxTokens", keys)
        self.assertIn("codex", by_agent)
        self.assertIn("model_context_window",
                      [ch["key"] for ch in by_agent["codex"]["changes"]])

    def test_recommendations_without_sizing_eval_say_nothing(self):
        m = {"peak_concurrency": 0, "by_outcome": {}, "by_tier": {},
             "window_hours": 24.0}
        recs = cli.recommendations(m, [], ["pi", "claude-code", "codex"],
                                   sizing_eval=None)
        keys = [ch["key"] for g in recs for ch in g["changes"]]
        self.assertNotIn("model sizing (catalog join)", keys)


class NewConfigKeys(unittest.TestCase):
    """The three keys the family added to the collection layer."""

    def test_claude_sizing_keys_carry_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
            with open(os.path.join(home, ".claude/settings.json"), "w") as fh:
                fh.write(claude_settings(env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "8000"}))
            env = {"HOME": home, "LUNAROUTE_API_KEY": "",
                   "SWAMP_ROOT": os.path.join(tmp, "no-such")}
            with mock.patch.dict(os.environ, env):
                cfg = sources.read_configs(swamp_repo_dir=os.path.join(tmp, "no-such"))
            fact = cfg.claude_code["env.CLAUDE_CODE_MAX_OUTPUT_TOKENS"]
            self.assertEqual((fact.value, fact.provenance),
                             ("8000", sources.PROV_DECLARED))
            fact = cfg.claude_code["env.CLAUDE_CODE_MAX_CONTEXT_TOKENS"]
            self.assertEqual(fact.provenance, sources.PROV_UNKNOWN)
            self.assertIn("unverified", fact.reason)

    def test_codex_window_key_is_declared_or_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(os.path.join(home, ".codex"), exist_ok=True)
            with open(os.path.join(home, ".codex/config.toml"), "w") as fh:
                fh.write(codex_config(window=262144))
            env = {"HOME": home, "LUNAROUTE_API_KEY": "",
                   "SWAMP_ROOT": os.path.join(tmp, "no-such")}
            with mock.patch.dict(os.environ, env):
                cfg = sources.read_configs(swamp_repo_dir=os.path.join(tmp, "no-such"))
            fact = cfg.codex["model_context_window"]
            self.assertEqual((fact.value, fact.provenance),
                             ("262144", sources.PROV_DECLARED))


if __name__ == "__main__":
    unittest.main()
