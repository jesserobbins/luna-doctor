#!/usr/bin/env python3
"""Sizing-misconfiguration rules: declared sizes vs the catalog's real ones.

The 2026-10-23 rule family. Six rules judge each routed model's sizing keys
against the live catalog (`GET /v1/models`, served from the snapshot), with
the harness's shipped default standing in when a key is absent:

  sizing-unset-underuses-model        pi: routed catalog model has no
                                      contextWindow/maxTokens while the
                                      catalog value is >= 2x the shipped
                                      default (either dimension)
  sizing-exceeds-model-window         pi: effective contextWindow (declared,
                                      else the V default) above the catalog's
  sizing-output-exceeds-model-max     pi: effective maxTokens above the
                                      catalog's max_output_tokens;
                                      claude-code arm: declared values only,
                                      and the verdict stays informational
                                      (reject vs clamp is unresolved)
  sizing-input-plus-output-over-window
                                      pi: effective maxTokens >= effective
                                      contextWindow - input and output
                                      budgets share the window
  codex-sizing-window                  codex: declared model_context_window
                                      vs the catalog's (V-declared-only)
  claude-sizing-window                 claude-code: CLAUDE_CODE_MAX_CONTEXT_
                                      TOKENS - the key name is V, its default
                                      and semantics U: informational line
                                      only, never a finding

The audit's rules of the audit, wholesale:

  * Only a V-backed comparison may raise a finding. A U-backed line is
    informational and never the sole basis for advice.
  * The routed model id comes from the snapshot's routing join (pi:
    defaultModel + subagents.defaultModel as sources resolves them;
    claude-code: env.CLAUDE_CODE_SUBAGENT_MODEL; codex: model). The catalog
    lookup is per exact id - no sibling resolution, no -background
    inheritance, no version-suffix heuristic; an override resolved to
    `"model": false` is a disabled override and produces no sizing row. A
    model the catalog does not serve is UNCHECKED with the reason "model not
    in catalog"; the routing finding belongs to model-not-lunaroute, which
    sizing never duplicates.
  * An absent config file means the shipped default is in force and is
    judged like any other state. An unreadable one is unchecked, never
    clean. A stale or missing catalog is unchecked for every rule, never
    clean.
  * A deliberate small window or small output is correct: every comparison
    is against the catalog value for that model, never against a magnitude
    heuristic. Declared == catalog and declared < catalog are both clean.
  * Never infer a window, a default or a plan from observed traffic. The
    catalog is the authority or the check is unchecked.

Row statuses: "finding" (V-backed and actionable), "clean" (judged, no
problem), "unchecked" (could not be judged - never clean), "informational"
(a U-backed observation printed so silence stays auditable; never a
finding). Corroboration joins the log half of the run where the design
specifies one: context_length_exceeded requests escalate the window rule's
finding, and an error naming `max_tokens` (pi's V wire field) escalates the
output rule's - never the other way around.

This module is pure: it consumes only what the snapshot already collected
(routing join, configs, catalog, requests), reads no file, touches no
network and writes nothing. Advice copy lives in _fix_block.py.
"""

from __future__ import annotations

from luna_doctor import sources

# ---- the rule-carried V constants --------------------------------------------
# Not a Snapshot provenance state: a shipped default is admissible only with
# a code cite. pi's pair is verified at three call sites in the installed
# bundle - the schema default, request composition and the compaction path -
# so one wrong number corrupts both what pi attempts and when it compacts.
# claude-code's and codex's numeric defaults were never captured, so those
# keys carry no constant here and can never back a finding.

PI_CONTEXT_WINDOW_DEFAULT = 128000
PI_MAX_TOKENS_DEFAULT = 16384
PI_DEFAULT_CITATION = ("@earendil-works/pi-coding-agent dist/core/model-config.js:44, "
                       "dist/core/provider-composer.js:109, "
                       "dist/core/compaction/branch-summarization.js:190")

# The one ratio constant: the underuse fire line, >= 2x on either dimension
# (the owner-owned parameter; the values it compares are always
# catalog-derived). beam sits just above it at 2.05x; a 4x line would drop
# beam and glm-5.3 into the silent band.
UNDERUSE_FIRE_LINE = 2

RULE_UNSET_UNDERUSES = "sizing-unset-underuses-model"
RULE_EXCEEDS_WINDOW = "sizing-exceeds-model-window"
RULE_OUTPUT_EXCEEDS_MAX = "sizing-output-exceeds-model-max"
RULE_INPUT_PLUS_OUTPUT = "sizing-input-plus-output-over-window"
RULE_CODEX_WINDOW = "codex-sizing-window"
RULE_CLAUDE_WINDOW = "claude-sizing-window"

# The existing rule that owns "routed model id not served". Sizing depends on
# it rather than duplicating it: a model absent from the catalog makes every
# sizing rule for that id unchecked, and the routing finding stays where it
# is.
ROUTING_RULE = "model-not-lunaroute"

NOTE = ("checked now, not as-of-window. Only V-backed comparisons raise "
        "findings; U-backed lines are informational and never the sole basis "
        "for advice. A model the catalog does not serve is unchecked here - "
        "the routing finding belongs to " + ROUTING_RULE + ". An absent "
        "config file means the shipped default is in force and is judged; an "
        "unreadable one is unchecked, never clean.")

_SIZING_FILES = (sources.PI_MODEL_STORES[0], sources.PI_MODEL_STORES[1],
                 sources.PI_SETTINGS, sources.CLAUDE_SETTINGS, sources.CODEX_CONFIG)


# ---- small helpers -----------------------------------------------------------

def _num(value):
    """int(value) when value is a real number (or a numeric string), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _fmt(n) -> str:
    return f"{n:,}"


def _pct(part: int, whole: int) -> str:
    return f"{round(100.0 * part / whole)}%"


def _times(ratio: float) -> str:
    """15.625 -> '15.6x', 16.0 -> '16x': the design's multipliers stay readable."""
    if abs(ratio - round(ratio)) < 0.05:
        return f"{round(ratio)}x"
    return f"{ratio:.1f}x"


def _declared_num(fact):
    """The number a config file actually declares for one sizing key, or None.

    A documented default (provenance `configured`) reads as unset here, same
    as sources.declared_value: the caller wants what the file says, and the
    shipped default is this module's own constant, judged explicitly."""
    if _is_declared(fact):
        return _num(fact.value)
    return None


def _is_declared(fact) -> bool:
    return fact is not None and fact.provenance == sources.PROV_DECLARED


def _unreadable_declared(fact) -> bool:
    """The file declares this key but the value is not a number.

    That state is neither unset (a value IS declared) nor judgeable (there
    is no number to compare): unchecked, never clean - never silently
    recategorized as absent."""
    return _is_declared(fact) and _num(fact.value) is None


def _catalog_window(entry):
    """The model's context window: the primary key, else its observed synonym.

    context_window == context_length is an observed invariant, not an
    assumption; a future divergence must degrade to the fallback rather than
    crash - and never silently prefer the synonym while the primary lives."""
    if entry is None:
        return None
    v = _num(entry.context_window)
    if v is not None:
        return v
    return _num(entry.raw.get("context_length"))


def _catalog_max_output(entry):
    if entry is None:
        return None
    v = _num(entry.max_output_tokens)
    if v is not None:
        return v
    return _num(entry.raw.get("max_completion_tokens"))


def _row(rule, harness, model, key, file, status, vu, detail,
         values=None, severity=None, source="", corroborated=False,
         corroboration=""):
    row = {"rule": rule, "harness": harness, "model": model, "key": key,
           "file": file, "status": status, "vu": vu, "detail": detail}
    if values is not None:
        row["values"] = values
    if severity:
        row["severity"] = severity
    if source:
        row["source"] = source
    if corroboration:
        row["corroborated"] = corroborated
        row["corroboration"] = corroboration
    return row


def _routing_ids(snap, harness):
    """The distinct model ids one harness's selection keys resolve to.

    Exact ids, deduped: the same id from defaultModel and a watchdog override
    is one sizing judgement, never two. A `"model": false` override and an
    unset key both resolve to no id here and produce no row."""
    section = (snap.routing or {}).get(harness) or {}
    return sorted({e["model_id"] for e in section.values() if e.get("model_id")})


def _pi_store_unreadable(configs) -> bool:
    """True when any pi model store is present but could not be parsed.

    The declared sizing state is then unreadable - an unparsable store could
    hide a declaration - so every pi sizing row is unchecked, never clean."""
    if configs is None:
        return True
    files = configs.files or {}
    for path in sources.PI_MODEL_STORES:
        state = files.get(path) or {}
        if state.get("present") and not state.get("parsable"):
            return True
    return False


def _file_state(configs, path):
    if configs is None:
        return {}
    return (configs.files or {}).get(path) or {}


def _context_overruns(snap, model_id) -> int:
    """Requests in the window that died on the window: the class the window
    rule predicts, so observing it corroborates that rule's finding."""
    return sum(1 for r in snap.requests
               if r.model == model_id and r.outcome == "context_length_exceeded")


def _output_rejections(snap, model_id) -> int:
    """Errors naming `max_tokens` (pi's V wire field), excluding the
    context-length class: the standard over-window error also breaks its
    token count down by max_tokens, so a naive match would misjoin it to the
    output rule."""
    n = 0
    for r in snap.requests:
        if r.model != model_id or r.outcome == "context_length_exceeded":
            continue
        if "max_tokens" in (r.error_message or "").lower():
            n += 1
    return n


# ---- pi ----------------------------------------------------------------------

def _pi_rows(snap, usable, why_unusable, rows):
    ids = _routing_ids(snap, "pi")
    if not ids:
        # The settings file that carries the routing keys may itself be
        # unreadable: that is unchecked, never silent and never clean.
        settings = _file_state(snap.configs, sources.PI_SETTINGS)
        if settings.get("present") and not settings.get("parsable"):
            for key in ("contextWindow", "maxTokens"):
                rows.append(_row(
                    RULE_EXCEEDS_WINDOW, "pi", None, key, sources.PI_MODEL_STORES[0],
                    "unchecked", "V",
                    "~/.pi/agent/settings.json is present but unparsable, so no "
                    "routed model id could be resolved; sizing unchecked, not clean"))
        return
    unreadable = _pi_store_unreadable(snap.configs)
    catalog = snap.catalog
    for mid in ids:
        entry = (snap.configs.pi_models.get(mid) or {}) if snap.configs is not None else {}
        where = entry.get("source") or ""
        ctx_declared = _declared_num(entry.get("contextWindow"))
        out_declared = _declared_num(entry.get("maxTokens"))
        ctx = ctx_declared if ctx_declared is not None else PI_CONTEXT_WINDOW_DEFAULT
        out = out_declared if out_declared is not None else PI_MAX_TOKENS_DEFAULT
        cat = catalog.models.get(mid) if (usable and catalog is not None) else None
        if cat is None:
            reason = why_unusable or "model not in catalog"
            detail = reason
            if reason == "model not in catalog":
                detail += ("; the routing finding belongs to " + ROUTING_RULE)
            keys = [k for k, d in (("contextWindow", ctx_declared),
                                   ("maxTokens", out_declared)) if d is None]
            for rule, key in ((RULE_EXCEEDS_WINDOW, "contextWindow"),
                              (RULE_OUTPUT_EXCEEDS_MAX, "maxTokens"),
                              (RULE_INPUT_PLUS_OUTPUT, "contextWindow + maxTokens")):
                rows.append(_row(rule, "pi", mid, key, sources.PI_MODEL_STORES[0],
                                 "unchecked", "V", detail))
            for key in keys:
                rows.append(_row(RULE_UNSET_UNDERUSES, "pi", mid, key,
                                 sources.PI_MODEL_STORES[0], "unchecked", "V", detail))
            continue
        cw = _catalog_window(cat)
        mo = _catalog_max_output(cat)
        if unreadable:
            for rule, key in ((RULE_EXCEEDS_WINDOW, "contextWindow"),
                              (RULE_OUTPUT_EXCEEDS_MAX, "maxTokens"),
                              (RULE_INPUT_PLUS_OUTPUT, "contextWindow + maxTokens")):
                rows.append(_row(rule, "pi", mid, key, sources.PI_MODEL_STORES[0],
                                 "unchecked", "V",
                                 "a pi model store is present but unparsable; the "
                                 "declared sizing state is unreadable, so sizing is "
                                 "unchecked, not clean"))
            continue
        overruns = _context_overruns(snap, mid)
        rejections = _output_rejections(snap, mid)

        # ---- the window dimension ----
        if ctx_declared is None:
            # The key is absent: the shipped default is in force, and the
            # catalog join decides which wrong it is - or that it is none.
            if cw is not None and cw >= UNDERUSE_FIRE_LINE * PI_CONTEXT_WINDOW_DEFAULT:
                rows.append(_row(
                    RULE_UNSET_UNDERUSES, "pi", mid, "contextWindow",
                    sources.PI_MODEL_STORES[0], "finding", "V",
                    f"unset -> default {_fmt(PI_CONTEXT_WINDOW_DEFAULT)}; catalog "
                    f"context_window {_fmt(cw)} - running at "
                    f"{_pct(PI_CONTEXT_WINDOW_DEFAULT, cw)} of the window",
                    values={"declared": None, "effective": PI_CONTEXT_WINDOW_DEFAULT,
                            "catalog": cw, "default": PI_CONTEXT_WINDOW_DEFAULT},
                    severity="medium", source=where))
            elif cw is not None and ctx > cw:
                rows.append(_row(
                    RULE_EXCEEDS_WINDOW, "pi", mid, "contextWindow",
                    sources.PI_MODEL_STORES[0], "finding", "V",
                    f"unset -> default {_fmt(PI_CONTEXT_WINDOW_DEFAULT)} above "
                    f"catalog context_window {_fmt(cw)} - {_times(PI_CONTEXT_WINDOW_DEFAULT / cw)} "
                    f"the real window; the harness will not compact until after the "
                    f"model starts refusing",
                    values={"declared": None, "effective": PI_CONTEXT_WINDOW_DEFAULT,
                            "catalog": cw, "default": PI_CONTEXT_WINDOW_DEFAULT},
                    severity="high", source=where,
                    corroborated=overruns > 0,
                    corroboration=(f"{overruns} context_length_exceeded request(s) in "
                                   f"the window name this model - the failure this "
                                   f"rule predicts was observed") if overruns else ""))
            elif cw is not None:
                rows.append(_row(
                    RULE_UNSET_UNDERUSES, "pi", mid, "contextWindow",
                    sources.PI_MODEL_STORES[0], "clean", "V",
                    f"unset -> default {_fmt(PI_CONTEXT_WINDOW_DEFAULT)} = "
                    f"{_pct(PI_CONTEXT_WINDOW_DEFAULT, cw)} of catalog context_window "
                    f"{_fmt(cw)} - the default is within the model's window",
                    values={"declared": None, "effective": PI_CONTEXT_WINDOW_DEFAULT,
                            "catalog": cw, "default": PI_CONTEXT_WINDOW_DEFAULT}))
            else:
                rows.append(_row(
                    RULE_EXCEEDS_WINDOW, "pi", mid, "contextWindow",
                    sources.PI_MODEL_STORES[0], "unchecked", "V",
                    "the catalog entry carries no usable context_window; "
                    "unchecked, never clean"))
        else:
            if cw is None:
                rows.append(_row(
                    RULE_EXCEEDS_WINDOW, "pi", mid, "contextWindow",
                    sources.PI_MODEL_STORES[0], "unchecked", "V",
                    "the catalog entry carries no usable context_window; "
                    "unchecked, never clean",
                    values={"declared": ctx_declared, "effective": ctx,
                            "catalog": None, "default": PI_CONTEXT_WINDOW_DEFAULT},
                    source=where))
            elif ctx > cw:
                rows.append(_row(
                    RULE_EXCEEDS_WINDOW, "pi", mid, "contextWindow",
                    sources.PI_MODEL_STORES[0], "finding", "V",
                    f"declared {_fmt(ctx)} above catalog context_window {_fmt(cw)} "
                    f"({_times(ctx / cw)}); the harness will not compact until "
                    f"after the model starts refusing",
                    values={"declared": ctx_declared, "effective": ctx,
                            "catalog": cw, "default": PI_CONTEXT_WINDOW_DEFAULT},
                    severity="high", source=where,
                    corroborated=overruns > 0,
                    corroboration=(f"{overruns} context_length_exceeded request(s) in "
                                   f"the window name this model - the failure this "
                                   f"rule predicts was observed") if overruns else ""))
            elif ctx == cw:
                rows.append(_row(
                    RULE_EXCEEDS_WINDOW, "pi", mid, "contextWindow",
                    sources.PI_MODEL_STORES[0], "clean", "V",
                    f"declared {_fmt(ctx)} = catalog context_window {_fmt(cw)} - "
                    f"exact match, correct",
                    values={"declared": ctx_declared, "effective": ctx,
                            "catalog": cw, "default": PI_CONTEXT_WINDOW_DEFAULT},
                    source=where))
            else:
                rows.append(_row(
                    RULE_EXCEEDS_WINDOW, "pi", mid, "contextWindow",
                    sources.PI_MODEL_STORES[0], "clean", "V",
                    f"declared {_fmt(ctx)} = {_pct(ctx, cw)} of catalog context_window "
                    f"{_fmt(cw)} - a deliberate cap, clean",
                    values={"declared": ctx_declared, "effective": ctx,
                            "catalog": cw, "default": PI_CONTEXT_WINDOW_DEFAULT},
                    source=where))

        # ---- the output dimension ----
        if out_declared is None:
            if mo is not None and mo >= UNDERUSE_FIRE_LINE * PI_MAX_TOKENS_DEFAULT:
                rows.append(_row(
                    RULE_UNSET_UNDERUSES, "pi", mid, "maxTokens",
                    sources.PI_MODEL_STORES[0], "finding", "V",
                    f"unset -> default {_fmt(PI_MAX_TOKENS_DEFAULT)}; catalog "
                    f"max_output_tokens {_fmt(mo)} - running at "
                    f"{_pct(PI_MAX_TOKENS_DEFAULT, mo)} of the output budget",
                    values={"declared": None, "effective": PI_MAX_TOKENS_DEFAULT,
                            "catalog": mo, "default": PI_MAX_TOKENS_DEFAULT},
                    severity="medium", source=where))
            elif mo is not None and out > mo:
                rows.append(_row(
                    RULE_OUTPUT_EXCEEDS_MAX, "pi", mid, "maxTokens",
                    sources.PI_MODEL_STORES[0], "finding", "V",
                    f"unset -> default {_fmt(PI_MAX_TOKENS_DEFAULT)} above catalog "
                    f"max_output_tokens {_fmt(mo)} - {_times(PI_MAX_TOKENS_DEFAULT / mo)} "
                    f"the model's max output",
                    values={"declared": None, "effective": PI_MAX_TOKENS_DEFAULT,
                            "catalog": mo, "default": PI_MAX_TOKENS_DEFAULT},
                    severity="medium", source=where,
                    corroborated=rejections > 0,
                    corroboration=(f"{rejections} rejection(s) naming max_tokens in the "
                                   f"window - the gateway rejects rather than clamps")
                    if rejections else ""))
            elif mo is not None:
                rows.append(_row(
                    RULE_UNSET_UNDERUSES, "pi", mid, "maxTokens",
                    sources.PI_MODEL_STORES[0], "clean", "V",
                    f"unset -> default {_fmt(PI_MAX_TOKENS_DEFAULT)} = "
                    f"{_pct(PI_MAX_TOKENS_DEFAULT, mo)} of catalog max_output_tokens "
                    f"{_fmt(mo)} - the default is within the model's output budget",
                    values={"declared": None, "effective": PI_MAX_TOKENS_DEFAULT,
                            "catalog": mo, "default": PI_MAX_TOKENS_DEFAULT}))
            else:
                rows.append(_row(
                    RULE_OUTPUT_EXCEEDS_MAX, "pi", mid, "maxTokens",
                    sources.PI_MODEL_STORES[0], "unchecked", "V",
                    "the catalog entry carries no usable max_output_tokens; "
                    "unchecked, never clean"))
        else:
            if mo is None:
                rows.append(_row(
                    RULE_OUTPUT_EXCEEDS_MAX, "pi", mid, "maxTokens",
                    sources.PI_MODEL_STORES[0], "unchecked", "V",
                    "the catalog entry carries no usable max_output_tokens; "
                    "unchecked, never clean",
                    values={"declared": out_declared, "effective": out,
                            "catalog": None, "default": PI_MAX_TOKENS_DEFAULT},
                    source=where))
            elif out > mo:
                rows.append(_row(
                    RULE_OUTPUT_EXCEEDS_MAX, "pi", mid, "maxTokens",
                    sources.PI_MODEL_STORES[0], "finding", "V",
                    f"declared {_fmt(out)} above catalog max_output_tokens {_fmt(mo)} "
                    f"({_times(out / mo)}) - either rejected or silently clamped",
                    values={"declared": out_declared, "effective": out,
                            "catalog": mo, "default": PI_MAX_TOKENS_DEFAULT},
                    severity="medium", source=where,
                    corroborated=rejections > 0,
                    corroboration=(f"{rejections} rejection(s) naming max_tokens in the "
                                   f"window - the gateway rejects rather than clamps")
                    if rejections else ""))
            elif out == mo:
                rows.append(_row(
                    RULE_OUTPUT_EXCEEDS_MAX, "pi", mid, "maxTokens",
                    sources.PI_MODEL_STORES[0], "clean", "V",
                    f"declared {_fmt(out)} = catalog max_output_tokens {_fmt(mo)} - "
                    f"exact match, correct",
                    values={"declared": out_declared, "effective": out,
                            "catalog": mo, "default": PI_MAX_TOKENS_DEFAULT},
                    source=where))
            else:
                rows.append(_row(
                    RULE_OUTPUT_EXCEEDS_MAX, "pi", mid, "maxTokens",
                    sources.PI_MODEL_STORES[0], "clean", "V",
                    f"declared {_fmt(out)} = {_pct(out, mo)} of catalog "
                    f"max_output_tokens {_fmt(mo)} - a deliberate output cap, clean",
                    values={"declared": out_declared, "effective": out,
                            "catalog": mo, "default": PI_MAX_TOKENS_DEFAULT},
                    source=where))

        # ---- the double-book check ----
        # output >= window leaves zero input: the request is over-window
        # before it is built. The deliberately narrow form - not the stronger
        # catalog-max_input version, which fires on every deliberate small
        # window.
        if out >= ctx:
            rows.append(_row(
                RULE_INPUT_PLUS_OUTPUT, "pi", mid, "contextWindow + maxTokens",
                sources.PI_MODEL_STORES[0], "finding", "V",
                f"effective output {_fmt(out)} >= effective window {_fmt(ctx)} - "
                f"input and output budgets share the window, so the request is "
                f"over-window before it is built",
                values={"window": ctx, "output": out},
                severity="high", source=where))


# ---- claude-code ----------------------------------------------------------------

def _claude_rows(snap, usable, why_unusable, rows):
    configs = snap.configs
    if configs is None:
        return
    state = _file_state(configs, sources.CLAUDE_SETTINGS)
    ids = _routing_ids(snap, "claude-code")
    routed = ids[0] if ids else None
    out_fact = configs.claude_code.get("env.CLAUDE_CODE_MAX_OUTPUT_TOKENS")
    win_fact = configs.claude_code.get("env.CLAUDE_CODE_MAX_CONTEXT_TOKENS")
    declared_out = _declared_num(out_fact)
    declared_win = _declared_num(win_fact)
    if not (state.get("present") or routed or _is_declared(out_fact)
            or _is_declared(win_fact)):
        # claude-code is not configured here at all; the files block says so.
        return
    if state.get("present") and not state.get("parsable"):
        for rule, key in ((RULE_OUTPUT_EXCEEDS_MAX, "env.CLAUDE_CODE_MAX_OUTPUT_TOKENS"),
                          (RULE_CLAUDE_WINDOW, "env.CLAUDE_CODE_MAX_CONTEXT_TOKENS")):
            rows.append(_row(rule, "claude-code", routed, key, sources.CLAUDE_SETTINGS,
                             "unchecked", "U",
                             "settings present but unparsable; reported unchecked, "
                             "not clean"))
        return
    if _unreadable_declared(out_fact) or _unreadable_declared(win_fact):
        # a declared value that is not a number is neither unset nor judgeable
        if _unreadable_declared(out_fact):
            rows.append(_row(RULE_OUTPUT_EXCEEDS_MAX, "claude-code", routed,
                             "env.CLAUDE_CODE_MAX_OUTPUT_TOKENS",
                             sources.CLAUDE_SETTINGS, "unchecked",
                             "V key / U default",
                             f"declared {out_fact.value!r} is not a number; "
                             f"unchecked, not clean"))
        if _unreadable_declared(win_fact):
            rows.append(_row(RULE_CLAUDE_WINDOW, "claude-code", routed,
                             "env.CLAUDE_CODE_MAX_CONTEXT_TOKENS",
                             sources.CLAUDE_SETTINGS, "unchecked", "U",
                             f"declared {win_fact.value!r} is not a number; "
                             f"unchecked, not clean"))
        return
    catalog = snap.catalog
    cat = catalog.models.get(routed) if (usable and routed and catalog is not None) else None

    # ---- the output key: V key, U default - declared values only, and even
    # a declared over-max stays informational until reject-vs-clamp is V.
    if declared_out is None:
        rows.append(_row(
            RULE_OUTPUT_EXCEEDS_MAX, "claude-code", routed,
            "env.CLAUDE_CODE_MAX_OUTPUT_TOKENS", sources.CLAUDE_SETTINGS,
            "informational", "V key / U default",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS unset; the shipped default is "
            "unverified - informational, never a finding",
            values={"declared": None}))
    elif routed is None:
        rows.append(_row(
            RULE_OUTPUT_EXCEEDS_MAX, "claude-code", None,
            "env.CLAUDE_CODE_MAX_OUTPUT_TOKENS", sources.CLAUDE_SETTINGS,
            "unchecked", "V key / U default",
            f"declared {_fmt(declared_out)}; no claude-code model is declared to "
            f"compare against - unchecked, not clean",
            values={"declared": declared_out}))
    elif cat is None:
        reason = why_unusable or "model not in catalog"
        detail = reason
        if reason == "model not in catalog":
            detail += ("; the routing finding belongs to " + ROUTING_RULE)
        rows.append(_row(
            RULE_OUTPUT_EXCEEDS_MAX, "claude-code", routed,
            "env.CLAUDE_CODE_MAX_OUTPUT_TOKENS", sources.CLAUDE_SETTINGS,
            "unchecked", "V key / U default", detail,
            values={"declared": declared_out}))
    else:
        mo = _catalog_max_output(cat)
        if mo is None:
            rows.append(_row(
                RULE_OUTPUT_EXCEEDS_MAX, "claude-code", routed,
                "env.CLAUDE_CODE_MAX_OUTPUT_TOKENS", sources.CLAUDE_SETTINGS,
                "unchecked", "V key / U default",
                "the catalog entry carries no usable max_output_tokens; "
                "unchecked, never clean",
                values={"declared": declared_out}))
        elif declared_out > mo:
            rows.append(_row(
                RULE_OUTPUT_EXCEEDS_MAX, "claude-code", routed,
                "env.CLAUDE_CODE_MAX_OUTPUT_TOKENS", sources.CLAUDE_SETTINGS,
                "informational", "V key / U default",
                f"declared {_fmt(declared_out)} above catalog max_output_tokens "
                f"{_fmt(mo)} ({_times(declared_out / mo)}) - reject vs clamp is "
                f"unverified (U): informational only, never a finding",
                values={"declared": declared_out, "catalog": mo}))
        else:
            rows.append(_row(
                RULE_OUTPUT_EXCEEDS_MAX, "claude-code", routed,
                "env.CLAUDE_CODE_MAX_OUTPUT_TOKENS", sources.CLAUDE_SETTINGS,
                "clean", "V key / U default",
                f"declared {_fmt(declared_out)} at or below catalog "
                f"max_output_tokens {_fmt(mo)} - clean",
                values={"declared": declared_out, "catalog": mo}))

    # ---- the window key: U throughout - the line exists so the report can
    # say what was not checked and why; only verification promotes it.
    detail = ("CLAUDE_CODE_MAX_CONTEXT_TOKENS "
              + ("unset" if declared_win is None else f"declared {_fmt(declared_win)}"))
    if cat is not None:
        cw = _catalog_window(cat)
        if cw is not None:
            detail += f" vs catalog context_window {_fmt(cw)} for {routed}"
    detail += ("; the key's default and semantics are unverified (U) - "
               "informational only, never a finding")
    rows.append(_row(
        RULE_CLAUDE_WINDOW, "claude-code", routed,
        "env.CLAUDE_CODE_MAX_CONTEXT_TOKENS", sources.CLAUDE_SETTINGS,
        "informational", "U", detail,
        values={"declared": declared_win}))


# ---- codex -----------------------------------------------------------------------

def _codex_rows(snap, usable, why_unusable, rows):
    configs = snap.configs
    if configs is None:
        return
    state = _file_state(configs, sources.CODEX_CONFIG)
    ids = _routing_ids(snap, "codex")
    routed = ids[0] if ids else None
    fact = configs.codex.get("model_context_window")
    declared = _declared_num(fact)
    if routed is None and not _is_declared(fact):
        return
    if _unreadable_declared(fact):
        rows.append(_row(
            RULE_CODEX_WINDOW, "codex", routed, "model_context_window",
            sources.CODEX_CONFIG, "unchecked", "V-declared-only",
            f"declared {fact.value!r} is not a number; unchecked, not clean"))
        return
    if state.get("present") and not state.get("parsable"):
        rows.append(_row(
            RULE_CODEX_WINDOW, "codex", routed, "model_context_window",
            sources.CODEX_CONFIG, "unchecked", "V-declared-only",
            "config present but unreadable; reported unchecked, not clean"))
        return
    if routed is None:
        rows.append(_row(
            RULE_CODEX_WINDOW, "codex", None, "model_context_window",
            sources.CODEX_CONFIG, "unchecked", "V-declared-only",
            f"model_context_window declared ({_fmt(declared)}); no codex model is "
            f"declared to compare against - unchecked, not clean",
            values={"declared": declared}))
        return
    catalog = snap.catalog
    cat = catalog.models.get(routed) if (usable and catalog is not None) else None
    if cat is None:
        reason = why_unusable or "model not in catalog"
        detail = reason
        if reason == "model not in catalog":
            detail += ("; the routing finding belongs to " + ROUTING_RULE)
        rows.append(_row(
            RULE_CODEX_WINDOW, "codex", routed, "model_context_window",
            sources.CODEX_CONFIG, "unchecked", "V-declared-only", detail,
            values={"declared": declared}))
        return
    if declared is None:
        rows.append(_row(
            RULE_CODEX_WINDOW, "codex", routed, "model_context_window",
            sources.CODEX_CONFIG, "informational", "V-declared-only",
            "model_context_window unset; the shipped default is unverified (U) - "
            "informational, never a finding",
            values={"declared": None}))
        return
    cw = _catalog_window(cat)
    if cw is None:
        rows.append(_row(
            RULE_CODEX_WINDOW, "codex", routed, "model_context_window",
            sources.CODEX_CONFIG, "unchecked", "V-declared-only",
            "the catalog entry carries no usable context_window; "
            "unchecked, never clean",
            values={"declared": declared}))
    elif declared > cw:
        rows.append(_row(
            RULE_CODEX_WINDOW, "codex", routed, "model_context_window",
            sources.CODEX_CONFIG, "finding", "V-declared-only",
            f"declared {_fmt(declared)} above catalog context_window {_fmt(cw)} "
            f"({_times(declared / cw)}) - the key is V by observation; this "
            f"comparison judges only the declared value",
            values={"declared": declared, "catalog": cw},
            severity="medium"))
    else:
        rows.append(_row(
            RULE_CODEX_WINDOW, "codex", routed, "model_context_window",
            sources.CODEX_CONFIG, "clean", "V-declared-only",
            f"declared {_fmt(declared)} at or below catalog context_window "
            f"{_fmt(cw)} - clean",
            values={"declared": declared, "catalog": cw}))


# ---- the entry point -------------------------------------------------------------

def _defaults_block() -> dict:
    return {
        "pi": {"contextWindow": PI_CONTEXT_WINDOW_DEFAULT,
               "maxTokens": PI_MAX_TOKENS_DEFAULT,
               "vu": "V", "citation": PI_DEFAULT_CITATION},
        "claude-code": {"vu": "U",
                        "reason": "numeric defaults not captured; these keys "
                                  "can never back a finding"},
        "codex": {"vu": "U",
                  "reason": "no default observed; declared values only"},
        "underuse_fire_line": UNDERUSE_FIRE_LINE,
    }


def evaluate(snap) -> dict:
    """The sizing rows for one snapshot: the six rules over the routing join.

    Pure - no IO beyond what the snapshot already collected. A snapshot that
    could not be built yields an empty, explicitly-unchecked block: sizing
    never guesses a default of its own and never turns absence into clean.
    """
    if snap is None:
        return {
            "note": NOTE,
            "defaults": _defaults_block(),
            "catalog": {"provenance": sources.PROV_UNKNOWN,
                        "reason": "snapshot unavailable; sizing unchecked, "
                                  "never clean"},
            "files": {},
            "rows": [],
        }
    catalog = snap.catalog
    if catalog is None:
        cat_block = {"provenance": sources.PROV_UNKNOWN,
                     "reason": snap.catalog_note or "catalog unknown"}
        usable = False
        why = snap.catalog_note or "catalog unknown"
    elif catalog.stale:
        # No catalog, no verdict: a stale catalog is unchecked for every
        # rule, never clean - the gateway's window may have moved under us.
        # (Relaxing this is the design's open question 1; until the owner
        # settles it, the verdict table's "stale -> unchecked" holds.)
        cat_block = {"provenance": sources.PROV_CATALOG,
                     "fetched_at": sources._iso(catalog.fetched_at),
                     "served_from": catalog.served_from,
                     "stale": True,
                     "reason": f"catalog stale (fetched "
                               f"{sources._age_str(catalog.age_s)} ago)"}
        usable = False
        why = cat_block["reason"]
    else:
        cat_block = {"provenance": sources.PROV_CATALOG,
                     "fetched_at": sources._iso(catalog.fetched_at),
                     "served_from": catalog.served_from,
                     "stale": False}
        usable = True
        why = ""
    rows: list = []
    _pi_rows(snap, usable, why, rows)
    _claude_rows(snap, usable, why, rows)
    _codex_rows(snap, usable, why, rows)
    files = {}
    if snap.configs is not None:
        files = {p: snap.configs.files[p] for p in _SIZING_FILES
                 if p in (snap.configs.files or {})}
    return {
        "note": NOTE,
        "defaults": _defaults_block(),
        "catalog": cat_block,
        "files": files,
        "rows": rows,
    }
