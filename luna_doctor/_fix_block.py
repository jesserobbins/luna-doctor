#!/usr/bin/env python3
"""`fix` command for luna-doctor.

Executed inside luna_doctor.cli's namespace, so it may use Request, collect,
analyse, window_hours, ADVISED_CONCURRENCY and friends.

Design rules:
  * Never write. Print only. No shell is emitted.
  * `fix` generates instructions for the user to paste to their agent.
    The agent is the only writer. The tool never does it itself.
  * A config the tool cannot parse is still reported — but instructions say
    to fix the parse error first.
  * Every emitted key must be VERIFIED in its own documentation and code path.
  * Do not re-compress a value the user already set more permissively
    (httpIdleTimeoutMs: 0 means disabled — leave it).
"""

import re

# ---- lunaroute preface ----------------------------------------------------
# What LunaRoute is (short, no links required), how to check it, and the
# only two routing ideas the user needs before touching config. Every
# rendered fix opens with this block unless --json is set.
#
# Detection order the instructions rely on:
#   1. `lunaroute` on PATH (local install / setup tool), else
#   2. `npx @lunaroute/cli …` (works from any shell with node)
# If neither responds, the user can still follow the instructions — the
# diagnosis side is offline.

RAISE_TIMEOUT_MS = 900000       # 15 min: past the ~633s background first token
KNOWN_AGENTS = ("pi", "claude-code", "codex", "swamp")

# Config paths, documented defaults and their citations are defined once in
# the data-collection layer (luna_doctor/sources.py, read_configs) and
# rendered here under the names the instructions have always used. Citations
# point to extension source for the swamp defaults and to ~/.swamp.yaml +
# .env.swamp for live values; knob resolution itself is sources.swamp_knobs.
PI_SETTINGS = sources.PI_SETTINGS
PI_SUBAGENT_CFG = sources.PI_SUBAGENT_CFG
# Where pi's model definitions (and so its declared sizing keys) live. The
# store is read by the collection layer; the fix advice names it so the
# agent knows which file to declare the pair in.
PI_MODEL_STORE = sources.PI_MODEL_STORES[0]
CLAUDE_SETTINGS = sources.CLAUDE_SETTINGS
CODEX_CONFIG = sources.CODEX_CONFIG
SWAMP_CITATION_DEFAULT = sources.SWAMP_CITATION_DEFAULT
SWAMP_DEFAULT_EXTRACT = sources.SWAMP_EXTRACT_CONCURRENCY_DEFAULT   # extensions/models/obsidian_ideas.ts phase extractMany
SWAMP_DEFAULT_LLM = sources.SWAMP_LLM_CONCURRENCY_DEFAULT        # extensions/models/obsidian_ideas.ts runVerifyPass


def _declared(fact) -> object:
    """The value a config file actually declares, or None.

    A documented default reads as unset here (sources.declared_value): the
    from/to strings below must keep distinguishing a declared value from
    "unset (default N)" exactly as they always have.
    """
    return sources.declared_value(fact)


def _swamp_present(window_hours: float = 24.0) -> tuple[bool, str | None]:
    """The swamp advice gate: in use, not merely installed."""
    state = swamp_state(since=time.time() - window_hours * 3600)
    return bool(state["in_use"]), state["repo"]


def _swamp_split(cap: int = INTERACTIVE_BURST_CAP) -> tuple:
    """Recommended split with pi + extract + llm <= `cap`.

    Returns (pi, extract, llm). A `cap` of 3 or more gets one request per phase
    and the remainder to pi, because pi is the interactive work and swamp is the
    background job. A `cap` below 3 cannot give every phase a slot, so the result
    is (cap, 0, 0) with the swamp phases at zero: advise running swamp work
    after the interactive session, never a split that does not fit. Nothing here
    asserts: an earlier version asserted that the fixed 4/3/2 split fitted the
    width, which raised AssertionError out of `fix` for every account narrower
    than nine requests.
    """
    cap = max(1, int(cap or 1))
    if cap < 3:
        return (cap, 0, 0)
    pi_rec = cap - 2
    return (pi_rec, 1, 1)


def recommendations_swamp(m: dict, agents: list[str], width=None) -> list:
    """Swamp advice, only when agents includes swamp and swamp is in use.

    `width` is the resolved priority concurrency when one is known. Swamp's own
    concurrency is a setting, but the budget it has to fit inside belongs to the
    account, so a split that cannot fit the declared width must not be presented
    as fitting it.
    """
    if "swamp" not in agents:
        return []
    present, repo_dir = _swamp_present(float(m.get("window_hours") or 24.0))
    if not present:
        return []
    knobs = sources.swamp_knobs(repo_dir)
    extract_val = knobs["extractConcurrency"]["value"]
    llm_val = knobs["llmConcurrency"]["value"]
    try:
        extract_n = int(extract_val)
        llm_n = int(llm_val)
    except (TypeError, ValueError):
        extract_n = SWAMP_DEFAULT_EXTRACT
        llm_n = SWAMP_DEFAULT_LLM
    width_known = isinstance(width, int) and not isinstance(width, bool) and width > 0
    # The budget for interactive work: the account's width when it is known, and
    # the tool's starting point when it is not. Swamp's configured concurrency is
    # added only to judge whether the split is small enough, never to state a
    # limit. The two numbers are labelled differently in the copy for that reason.
    pi_budget = width if width_known else ADVISED_CONCURRENCY
    swamp_total = extract_n + llm_n
    combined = pi_budget + swamp_total
    cap = width if width_known else INTERACTIVE_BURST_CAP
    extract_src = knobs["extractConcurrency"]["source"]
    llm_src = knobs["llmConcurrency"]["source"]
    def _citation(source: str) -> str:
        if source == "default":
            return f"{SWAMP_CITATION_DEFAULT} (default)"
        if source == "env":
            return "env var (live)"
        if source == "~/.swamp.yaml":
            return "~/.swamp.yaml (live)"
        if source == ".env.swamp":
            loc = os.path.join(repo_dir, ".env.swamp") if repo_dir else ".env.swamp"
            return f"{loc} (live)"
        return source
    extract_cite = _citation(extract_src)
    llm_cite = _citation(llm_src)
    if combined <= cap:
        return [{
            "agent": "swamp",
            "note": (f"swamp already within budget: pi {pi_budget} + swamp extract {extract_n} + "
                     f"swamp phase-6 {llm_n} = {combined}. No change needed."),
            "changes": [{
                "key": "SWAMP_EXTRACT_CONCURRENCY + SWAMP_LLM_CONCURRENCY",
                "file": os.path.join(repo_dir or "~", ".env.swamp") if repo_dir else "~/.swamp.yaml",
                "from": f"extract={extract_n} ({extract_cite}), llm={llm_n} ({llm_cite})",
                "to": f"extract={extract_n}, llm={llm_n} (within budget)",
                "why": (f"Combined budget pi {pi_budget} + extract {extract_n} + llm {llm_n} = {combined} "
                        f"fits the conservative combined budget. Keep the combined total within it when tuning. "
                        f"Defaults: {SWAMP_CITATION_DEFAULT} extract={SWAMP_DEFAULT_EXTRACT}, llm={SWAMP_DEFAULT_LLM}."),
                "instruction": (f"Your swamp setup already fits the combined budget. No change needed — tell your agent: keep swamp at extract {extract_n} / verify {llm_n}. "
                                f"If you retune, keep pi + swamp ≤ {cap}."),
                "impact": "no change needed; swamp is already within the combined budget.",
            }],
        }]
    pi_rec, extract_rec, llm_rec = _swamp_split(cap)
    combined_rec = pi_rec + extract_rec + llm_rec
    if extract_rec == 0:
        # The width cannot hold an interactive request and a swamp phase at the
        # same time. Say that, with the number, instead of a split that does not
        # fit.
        return [{
            "agent": "swamp",
            "note": (f"Swamp work is in use inside a window whose priority width leaves no room for "
                     f"it: the budget is {cap} concurrent request(s). One interactive request plus "
                     f"either swamp phase exceeds it. Run swamp work when the interactive session is "
                     f"idle, or raise the width of the account. No split is recommended here."),
            "changes": [],
        }]
    return [{
        "agent": "swamp",
        "changes": [
            {
                "key": "SWAMP_EXTRACT_CONCURRENCY",
                "file": os.path.join(repo_dir, ".env.swamp") if repo_dir else "~/.swamp.yaml",
                "from": f"{extract_n} ({extract_cite})",
                "to": str(extract_rec),
                "why": (f"Combined pi {pi_budget} + extract {extract_n} + phase-6 {llm_n} = {combined} "
                        f"is past the conservative combined budget: swamp alone would fill the "
                        f"interactive admission width even before pi traffic. Default {SWAMP_DEFAULT_EXTRACT} "
                        f"from {SWAMP_CITATION_DEFAULT}; live from {extract_cite}."),
                "instruction": (f"Ask your agent: in {os.path.join(repo_dir, '.env.swamp') if repo_dir else '.env.swamp'} set SWAMP_EXTRACT_CONCURRENCY={extract_rec} "
                                f"(was {extract_n} from {extract_cite}). Export it in your shell too if your swamp runner reads env vars. "
                                f"Target combined pi {pi_rec} + extract {extract_rec} + phase-6 {llm_rec} = {combined_rec} ≤ {cap}."),
                "impact": f"keeps combined pi {pi_rec} + extract {extract_rec} + phase-6 {llm_rec} = {combined_rec} within the combined budget.",
            },
            {
                "key": "SWAMP_LLM_CONCURRENCY",
                "file": os.path.join(repo_dir, ".env.swamp") if repo_dir else "~/.swamp.yaml",
                "from": f"{llm_n} ({llm_cite})",
                "to": str(llm_rec),
                "why": (f"Combined pi {pi_budget} + extract {extract_n} + phase-6 {llm_n} = {combined} "
                        f"is past the conservative combined budget. Default {SWAMP_DEFAULT_LLM} from "
                        f"{SWAMP_CITATION_DEFAULT}; live from {llm_cite}. {CAP_COPY[0].upper()}{CAP_COPY[1:]}."),
                "instruction": (f"Ask your agent: set SWAMP_LLM_CONCURRENCY={llm_rec} (was {llm_n} from {llm_cite}). "
                                f"Check it with your swamp runner's env and restart it so the next batch respects the new limit."),
                "impact": f"keeps combined pi {pi_rec} + extract {extract_rec} + phase-6 {llm_rec} = {combined_rec} within the combined budget.",
            },
        ],
        "combined_budget": {
            "pi": pi_budget, "extract": extract_n, "phase6": llm_n,
            "combined": combined, "cap": cap,
            "recommended": {"pi": pi_rec, "extract": extract_rec, "phase6": llm_rec, "combined": combined_rec},
        },
    }]


# The catalog is no longer fetched here by shelling out to `lunaroute models
# --json`: the snapshot built in cmd_fix carries the richer GET /v1/models
# catalog (cached on disk for 24h, fetched only with --online), and
# recommendations() receives its model ids. When the catalog is unknown the
# ids list is empty and pick_background_model falls back to the models the
# logs actually saw - the same degraded answer as before, never a guess.


def pick_background_model(reqs, cat):
    """The background model to recommend, as one model id (or None).

    One scalar, never a list: every caller writes this value into a config key,
    and a list reached the pi config as `lunaroute/['glm-5.3-background']`. That
    is a config file that fails to load, which is worse than no advice.
    """
    counts = {}
    for r in reqs:
        if r.tier == "interactive" and r.model:
            counts[r.model] = counts.get(r.model, 0) + 1
    known = set(cat) | {r.model for r in reqs}
    if counts:
        top = max(counts, key=counts.get)
        twin = top if top.endswith("-background") else top + "-background"
        if twin in known:
            return twin
    for cand in sorted(known):
        if cand.endswith("-background"):
            return cand
    return None


def _why(*bits: str) -> str:
    return " ".join(b for b in bits if b)


# ---- sizing (the 2026-10-23 family) ------------------------------------------
# sizing.evaluate() (luna_doctor/sizing.py) is the audit: pure rows, the
# verdict table, the V/U discipline. This block is the advice: findings turn
# into the same instruction-only changes every other rule here emits - key,
# file, value found, value judged against, one-line why, restart-scoped - and
# U-backed rows turn into from == to lines that record what was not checked
# and why, never advice. A U line that presents itself as a finding is the
# failure mode the marking exists to prevent.

def _sizing_finding_change(row) -> dict:
    v = row.get("values") or {}
    model = row.get("model") or ""
    rule = row["rule"]
    detail = row["detail"]
    corr = row.get("corroboration") or ""
    if rule == sizing.RULE_UNSET_UNDERUSES:
        default = v.get("default")
        catalog = v.get("catalog")
        is_window = row["key"] == "contextWindow"
        noun = "context window" if is_window else "max output tokens"
        why_extra = (" pi's compaction path reads the same default "
                     "(branch-summarization.js:190), so compaction also fires "
                     "at the default's boundary instead of the model's real one."
                     if is_window else
                     " The harness runs the model at the shipped default, not "
                     "at the model.")
        return {
            "key": f"models[{model}].{row['key']}",
            "file": PI_MODEL_STORE,
            "from": f"unset (default {default:,})",
            "to": f"your declared value (the model's real {noun}: {catalog:,})",
            "why": _why(detail, why_extra),
            "instruction": (f"Ask your agent: in {PI_MODEL_STORE} declare {row['key']} for "
                            f"lunaroute/{model} (the providers.lunaroute.models entry). It is "
                            f"unset, so pi runs the model at the shipped default {default:,} "
                            f"while the catalog serves {catalog:,} {noun} - the model's real "
                            f"limit, not a requirement: set the value you actually want, then "
                            f"restart the harness so the new value is read."),
            "impact": ("the model runs at the size you chose instead of the shipped "
                       "default; compaction and output budgets follow the real limits."),
        }
    if rule == sizing.RULE_EXCEEDS_WINDOW:
        declared = v.get("declared")
        catalog = v.get("catalog")
        frm = f"{declared:,} (declared)" if declared is not None \
            else f"unset (default {v.get('default'):,})"
        return {
            "key": f"models[{model}].contextWindow",
            "file": PI_MODEL_STORE,
            "from": frm,
            "to": f"at or below {catalog:,} (the model's real window)",
            "why": _why(detail, corr),
            "instruction": (f"Ask your agent: in {PI_MODEL_STORE} set contextWindow for "
                            f"lunaroute/{model} to at most {catalog:,} (was {frm}). The catalog "
                            f"serves a {catalog:,}-token window for this model; a larger value "
                            f"promises sessions the model will refuse with "
                            f"context_length_exceeded, and compaction will not fire in time "
                            f"to save them. Restart the harness after the change."),
            "impact": ("compaction fires at the model's real boundary; sessions stop "
                       "being promised a window the model refuses."),
        }
    if rule == sizing.RULE_OUTPUT_EXCEEDS_MAX:
        declared = v.get("declared")
        catalog = v.get("catalog")
        frm = f"{declared:,} (declared)" if declared is not None \
            else f"unset (default {v.get('default'):,})"
        return {
            "key": f"models[{model}].maxTokens",
            "file": PI_MODEL_STORE,
            "from": frm,
            "to": f"at or below {catalog:,} (the model's max output)",
            "why": _why(detail, corr),
            "instruction": (f"Ask your agent: in {PI_MODEL_STORE} set maxTokens for "
                            f"lunaroute/{model} to at most {catalog:,} (was {frm}). The model "
                            f"produces at most {catalog:,} output tokens; a larger budget is "
                            f"either rejected or silently clamped. Restart the harness after "
                            f"the change."),
            "impact": "stops requesting an output budget the model will not produce.",
        }
    if rule == sizing.RULE_INPUT_PLUS_OUTPUT:
        window = v.get("window")
        output = v.get("output")
        return {
            "key": f"models[{model}].contextWindow + models[{model}].maxTokens",
            "file": PI_MODEL_STORE,
            "from": f"window {window:,}, output {output:,}",
            "to": "output below the window",
            "why": _why(detail),
            "instruction": (f"Ask your agent: in {PI_MODEL_STORE} bring maxTokens for "
                            f"lunaroute/{model} below its contextWindow ({output:,} >= "
                            f"{window:,} today) - input and output budgets share the window, "
                            f"so an output reservation the size of the window leaves zero "
                            f"input. Restart the harness after the change."),
            "impact": ("the request is buildable again: the output reservation leaves "
                       "room for input inside the window."),
        }
    if rule == sizing.RULE_CODEX_WINDOW:
        declared = v.get("declared")
        catalog = v.get("catalog")
        return {
            "key": "model_context_window",
            "file": CODEX_CONFIG,
            "from": f"{declared:,} (declared)",
            "to": f"at or below {catalog:,} (the model's real window)",
            "why": _why(detail, "The key is V by observation; this finding judges only "
                               "the declared value - the shipped default is unverified and "
                               "is not advice."),
            "instruction": (f"Ask your agent: in {CODEX_CONFIG} set model_context_window to "
                            f"at most {catalog:,} (was {declared:,}). Existing keys in that "
                            f"file must be preserved. Restart codex after the change."),
            "impact": "codex stops promising a window the model refuses.",
        }
    return {
        "key": row.get("key") or rule,
        "file": row.get("file") or "",
        "from": "finding",
        "to": "finding",
        "why": detail,
        "instruction": f"Ask your agent to review this sizing finding: {detail}",
        "impact": "sizing finding.",
    }


def _sizing_informational_change(row) -> dict:
    """A U-backed row as a from == to line: recorded, never advice.

    The httpIdleTimeoutMs: 0 precedent - the verdict is not silent, and the
    exit code stays untouched because from == to."""
    v = row.get("values") or {}
    declared = v.get("declared")
    state = "unset" if declared is None else f"declared {declared:,}"
    return {
        "key": row["key"],
        "file": row["file"],
        "from": state,
        "to": state,
        "why": _why(row["detail"],
                    "Informational (U): never a finding, never the sole basis for "
                    "advice - only verification of the shipped default can promote "
                    "this line to a rule."),
        "instruction": (f"No change prescribed from an unverified default - recorded so "
                        f"the sizing verdict is not silent: {row['detail']}"),
        "impact": "recorded, not actionable; the line says what was not checked and why.",
    }


def _sizing_changes(sizing_eval, agents) -> dict:
    """Sizing rows -> the per-agent change lists fix renders.

    Findings (V-backed) arrive as changes with from != to - actionable, the
    same exit-code contract as every other finding. Informational rows arrive
    as from == to. Unchecked rows are the audit ledger's business (analyze's
    metrics.sizing), except one: when the pi models are routed but the whole
    catalog could not be established, fix says so - a silent sizing section
    would read as "checked and clean" and it is neither."""
    out = {"pi": [], "claude-code": [], "codex": []}
    if not sizing_eval:
        return out
    rows = sizing_eval.get("rows") or []
    for row in rows:
        harness = row.get("harness")
        if harness not in out or harness not in agents:
            continue
        if row.get("status") == "finding":
            out[harness].append(_sizing_finding_change(row))
        elif row.get("status") == "informational":
            out[harness].append(_sizing_informational_change(row))
    cat = sizing_eval.get("catalog") or {}
    if (cat.get("provenance") != sources.PROV_CATALOG
            and any(r.get("harness") == "pi" for r in rows)
            and "pi" in agents):
        out["pi"].append({
            "key": "model sizing (catalog join)",
            "file": "the LunaRoute model catalog (GET /v1/models)",
            "from": "unchecked",
            "to": "unchecked",
            "why": _why(f"sizing is unchecked, never clean: {cat.get('reason') or 'the catalog could not be established'}.",
                        "The declared sizing keys can only be judged against the live catalog."),
            "instruction": ("No change is prescribed from an unchecked state. To make the "
                            "sizing checks runnable, fetch the catalog once with --online "
                            "(one authenticated GET, cached on disk for 24h); the checks then "
                            "run offline from the cache."),
            "impact": ("the sizing rules say nothing rather than guessing; with the "
                       "catalog cached, the same run judges them."),
        })
    return out


def _impact(peak: int, width=None, known=True) -> str:
    """What this change does at the peak this window measured.

    When the width is known, the sentence compares the peak with that number.
    When it is not, the comparison is against a starting point, and the sentence
    says so: a claim about "this limit" made from a constant is the failure this
    whole branch set out to remove.
    """
    limit = width if isinstance(width, int) and not isinstance(width, bool) else ADVISED_CONCURRENCY
    if not known:
        if peak <= limit:
            return (f"no effect at your current peak of {peak} concurrent requests, which is "
                    f"already inside this starting point. Set the real number once "
                    f"`luna-doctor capacity` reports it.")
        return (f"your peak of {peak} concurrent requests is above this starting point, so the "
                f"change bites. Make sure that the number is the account's before you apply it.")
    if peak <= limit:
        return (f"no effect at your current peak of {peak} concurrent requests - this is headroom so "
                f"a later burst cannot overflow into the background tier.")
    return f"your peak of {peak} concurrent requests is already past this limit, so this is the change that bites."


def demotion_note(reason: str, model: str) -> str:
    if reason == "no_entitlement":
        return (f"`{model}` is being served on priority concurrency: this account has no background "
                f"entitlement, so its `-background` requests are downgraded into the priority pool "
                f"and count against interactive headroom.")
    return (f"your `-background` traffic may be running on priority concurrency: if this account has "
            f"no background entitlement, `{model}` requests are downgraded into the priority pool, "
            f"which is why a background model can still be squeezed. Only a load test can see the "
            f"served tier.")


# ---- lunaroute explainer (instructions-only fix) --------------------------
# Rendered once at the top of every non-JSON fix report so the per-agent
# instructions make sense even to a first-time user. Short by design:
# what LunaRoute is, what plan/tier means here, how concurrency fits, and how to
# talk to the `lunaroute` command (with the `npx` fallback when it is not on
# PATH).

LUNAROUTE_PREFACE = (
    "LunaRoute is a gateway that sits between your harness and the model. "
    "It multiplexes many concurrent sessions over one account. "
    "Your plan determines how many concurrent requests the gateway will admit "
    "and which tier they ride on."
)

LUNAROUTE_ROUTING_PREFACE = (
    "Routing: by default, non-interactive work (subagents, background tasks) "
    "should ride the background tier — it is slower but admits more concurrent requests. "
    "Interactive work (the session you are watching) rides the priority tier. "
    "Every non-interactive call should use the background variant of your model (e.g. "
    "`glm-5.3-background` rather than `glm-5.3`). If your account has no background "
    "entitlement, background requests are downgraded into the priority pool and still "
    "count against the same budget — a load test shows the served tier either way."
)

LUNAROUTE_COMMAND_PREFACE = (
    "To inspect your LunaRoute setup, use the `lunaroute` command:\n"
    "  lunaroute whoami          # who is signed in and which org (no plan number here)\n"
    "  lunaroute models --json   # what models are available on this account\n"
    "  luna-doctor capacity      # the account's own priority concurrency\n"
    "If `lunaroute` is not on your PATH, run it via npx instead:\n"
    "  npx @lunaroute/cli whoami\n"
    "  npx @lunaroute/cli models --json\n"
    "`lunaroute whoami` does not report a plan, and no `lunaroute` subcommand reports a "
    "concurrency number: read those from `luna-doctor capacity`, which asks the account.\n"
    "The `fix` instructions below name the exact config key and desired value. "
    "Do not paste generated code — paste the instruction to your agent and let "
    "the agent apply it."
)


def _lunaroute_preface_text() -> str:
    return "\n\n".join([LUNAROUTE_PREFACE, LUNAROUTE_ROUTING_PREFACE, LUNAROUTE_COMMAND_PREFACE])


def recommendations(m, reqs, agents, configs=None, catalog_ids=None,
                    sizing_eval=None) -> list:
    """Per-agent instructions from the evidence.

    `configs` is the snapshot's Configs (sources.read_configs() when not
    given): every value comes as a Fact with provenance, and _declared() keeps
    the from/to strings distinguishing a declared value from "unset (default
    N)" exactly as the old direct file reads did. `catalog_ids` are the
    snapshot catalog's model ids; empty means the catalog is unknown.
    `sizing_eval` is sizing.evaluate(snapshot) - the sizing family's rows:
    findings join the owning agent's changes after the concurrency advice,
    U-backed rows arrive as from == to lines, and None means sizing says
    nothing here (analyze's metrics.sizing carries the full ledger).
    """
    out = []
    if configs is None:
        configs = sources.read_configs()
    cat = list(catalog_ids or [])
    bg = pick_background_model(reqs, cat)
    # The width the advice aims at: the account's own priority concurrency, a
    # declared plan's, or - when neither is known - the old conservative
    # starting point, labelled as advice rather than as a gateway fact.
    resolved, resolved_src = _priority_for_advice(m)
    target = resolved or ADVISED_CONCURRENCY
    # When swamp is in use and its split is reduced to fit, pi's own budget is the
    # pi slice of that split, not the whole width: emitting both a width target and
    # a split that add up past the width is advice that cannot be followed.
    swamp_pi = None
    if "swamp" in agents:
        try:
            _present, _repo = _swamp_present(float(m.get("window_hours") or 24.0))
            if _present:
                _cap = resolved if isinstance(resolved, int) and not isinstance(resolved, bool) \
                    and resolved > 0 else INTERACTIVE_BURST_CAP
                _pi_rec, _ex_rec, _llm_rec = _swamp_split(_cap)
                if _ex_rec and _pi_rec < target:
                    swamp_pi = _pi_rec
        except Exception:
            swamp_pi = None
    if swamp_pi is not None:
        target = swamp_pi
    # When the width is unknown, the number in the instruction is a starting
    # point, never the account's. The sentence below every such instruction says
    # so, because a config value a reader cannot check is worse than no value.
    width_known = isinstance(resolved, int) and not isinstance(resolved, bool)
    start_note = ""
    if not width_known:
        start_note = (" This value is a starting point. The priority concurrency of your "
                      "account is unknown here. Run `luna-doctor capacity --online`, or declare "
                      "`--plan <name or number>`, and use that number instead.")
    # The peak the width is set against: the priority tier only. The all-tier peak
    # is what `analyze` reports as total traffic, and comparing it with a priority
    # allowance calls ten entitled background requests a breach.
    peak = m.get("peak_concurrency_priority")
    if not isinstance(peak, int) or isinstance(peak, bool):
        peak = m["peak_concurrency"]
    dead = m["by_outcome"].get("admission_timeout", 0)
    idle_stalls = m["by_outcome"].get("client_idle_timeout", 0)
    bgcount = m["by_tier"].get("background", 0)
    wide = peak > target or dead > 0
    evidence = _why(
        f"peak concurrency {peak},",
        f"{dead} request(s) blocked at admission," if dead else "",
        f"{idle_stalls} silent stall(s)," if idle_stalls else "",
        f"{bgcount} request(s) already on the background tier." if bgcount else "",
    )
    demotion = next((r.reason for r in reqs if r.reason), "")
    bg_note = demotion_note(demotion, bg) if bg else ""

    # ---------------- pi ----------------
    if "pi" in agents:
        pi_cfg = configs.pi
        limit = _declared(pi_cfg.get("globalConcurrencyLimit"))
        idle_ms = _declared(pi_cfg.get("httpIdleTimeoutMs"))
        changes = []

        if isinstance(limit, int) and limit > target:
            changes.append({
                "key": "globalConcurrencyLimit",
                "file": PI_SUBAGENT_CFG,
                "from": limit,
                "to": target,
                "why": _why(evidence,
                            f"This machine caps children at {limit}, wider than "
                            + ("the priority concurrency of the account" if width_known
                               else "the starting point this tool uses while your account width "
                                    "is unknown")
                            + f" - {CAP_COPY}. Past that width, the excess concurrent requests "
                            f"park or overflow into background scheduling, where the first token "
                            f"takes about 600s."),
                "instruction": (f"Ask your agent: in {PI_SUBAGENT_CFG} set globalConcurrencyLimit to {target} "
                                f"(was {limit}). This caps simultaneous child agents so your concurrent requests stay "
                                f"within the interactive admission width instead of overflowing into background "
                                f"scheduling. Restart the harness after the change." + start_note),
                "impact": _impact(peak, target, width_known),
            })
        elif limit is None:
            changes.append({
                "key": "globalConcurrencyLimit",
                "file": PI_SUBAGENT_CFG,
                "from": "unset (default 20)",
                "to": target,
                "why": _why(evidence,
                            "The default lets one run hold 20 concurrent requests, well past the interactive "
                            f"admission width - {CAP_COPY}."),
                "instruction": (f"Ask your agent: in {PI_SUBAGENT_CFG} set globalConcurrencyLimit to {target}. "
                                f"The current default (20) will keep overflowing the interactive tier under load; lower it. "
                                f"Have the agent read the existing file first, preserve other keys, then apply the change." + start_note),
                "impact": _impact(peak, target, width_known),
            })

        if isinstance(idle_ms, int) and 0 < idle_ms < 600000:
            changes.append({
                "key": "httpIdleTimeoutMs",
                "file": PI_SETTINGS,
                "from": idle_ms,
                "to": RAISE_TIMEOUT_MS,
                "why": _why(evidence,
                            f"pi's idle budget is {idle_ms}ms. A demoted background stream can stay silent"
                            f" for about 600s while emitting keep-alives, so a shorter budget converts a"
                            f" slow success into `Stream ended without finish_reason`."),
                "instruction": (f"Ask your agent: in {PI_SETTINGS} raise httpIdleTimeoutMs to {RAISE_TIMEOUT_MS} (from {idle_ms}). "
                                f"Anything shorter kills a slow background request before it answers. If your harness reads its "
                                f"timeout from env instead (e.g. HTTP_IDLE_TIMEOUT_MS), update that instead."),
                "impact": "prevents a background request from being killed by the client before it speaks.",
            })
        elif idle_ms == 0:
            changes.append({
                "key": "httpIdleTimeoutMs",
                "file": PI_SETTINGS,
                "from": 0,
                "to": 0,
                "why": _why(evidence,
                            "Already `0`, which means the idle budget is disabled - as permissive as this"
                            " setting gets. No change needed."),
                "instruction": "No change needed — httpIdleTimeoutMs is already disabled (0), which is the correct posture for background-heavy traffic.",
                "impact": "nothing to do; recorded so the verdict is not silent about it.",
            })

        if bg and wide:
            # `bg_model`, never `target`: `target` above is the numeric width the
            # advice aims at, and reusing the name here put a model id where a
            # concurrency number belongs. The claude and codex branches compare
            # against that number, so the reuse crashed them on a string compare.
            bg_model = bg if "/" in bg else f"lunaroute/{bg}"
            cur = _declared(pi_cfg.get("subagents.defaultModel"))
            if cur != bg_model:
                changes.append({
                    "key": "subagents.defaultModel",
                    "file": PI_SETTINGS,
                    "from": cur,
                    "to": bg_model,
                    "why": _why(evidence,
                                f"Children currently inherit the interactive model. `{bg}` is the tier with"
                                f" the larger concurrency allowance; the trade is a much slower SLA.",
                                bg_note),
                    "instruction": (f"Ask your agent: in {PI_SETTINGS} set your default background model to {bg_model} "
                                    f"so child agents ride the background tier by default (was {cur!r}). Ask it to verify the model "
                                    f"exists via: lunaroute models --json   (or: npx @lunaroute/cli models --json)."),
                    "impact": _impact(peak, target, width_known),
                })
        if changes:
            out.append({"agent": "pi", "changes": changes})

    # ---------------- claude-code ----------------
    if "claude-code" in agents:
        cc_cfg = configs.claude_code
        changes, note = [], None
        if not _declared(cc_cfg.get("env.ANTHROPIC_BASE_URL")):
            note = ("This settings file has no ANTHROPIC_BASE_URL, so claude-code may not be reaching"
                    " LunaRoute at all. Run `lunaroute setup claude-code` (or `npx @lunaroute/cli setup claude-code`) "
                    "first; the tier advice below only matters once traffic is routed.")

        subs = _declared(cc_cfg.get("env.CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"))

        def too_big(v, cap):
            try:
                return v is None or int(str(v)) > cap
            except ValueError:
                return True

        if too_big(subs, target):
            changes.append({
                "key": "env.CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS",
                "file": CLAUDE_SETTINGS,
                "from": subs if subs is not None else "unset (default 20)",
                "to": str(target),
                "why": _why(evidence,
                            f"The default is 20 concurrent subagents, well past the interactive "
                            f"admission width - {CAP_COPY}."),
                "instruction": (f"Ask your agent: in {CLAUDE_SETTINGS} set CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS to {target}. "
                                f"The default 20 keeps overflowing interactive headroom." + start_note),
                "impact": _impact(peak, target, width_known),
            })

        idle_env = _declared(cc_cfg.get("env.CLAUDE_STREAM_IDLE_TIMEOUT_MS"))
        api_env = _declared(cc_cfg.get("env.API_TIMEOUT_MS"))
        # Only raise a budget that is too short. A value already longer than the
        # target is permissive, and telling a user to lower it would make a
        # background request fail earlier than it does today. `too_big` was the
        # wrong direction for a timeout and shipped that advice.
        def _too_short(v, floor):
            try:
                return v is None or int(str(v)) < floor
            except ValueError:
                return True

        # Each timeout is judged on its own. One knob was inspected and both were
        # rewritten, so a user with a permissive API timeout was told to lower it
        # and a user with a short one was told nothing.
        short_timeouts = []
        if _too_short(idle_env, RAISE_TIMEOUT_MS):
            short_timeouts.append("CLAUDE_STREAM_IDLE_TIMEOUT_MS")
        if _too_short(api_env, RAISE_TIMEOUT_MS):
            short_timeouts.append("API_TIMEOUT_MS")
        if short_timeouts:
            changes.append({
                "key": "env." + " + env.".join(short_timeouts),
                "file": CLAUDE_SETTINGS,
                "from": ", ".join("%s %s" % (name, _declared(cc_cfg.get("env." + name))
                                             or "unset (floor 300000)")
                                  for name in short_timeouts),
                "to": str(RAISE_TIMEOUT_MS),
                "why": _why(evidence,
                            "The background tier can stay silent for longer than this budget, so the"
                            " client stops a request the gateway is still working on. Both timeout keys"
                            " belong in the `env` block, because the top-level settings keys are ignored"
                            " behind a custom base URL, which LunaRoute is.",
                            "Short here: " + ", ".join(short_timeouts) + "."),
                "instruction": (f"Ask your agent to raise {', '.join(short_timeouts)} to {RAISE_TIMEOUT_MS} "
                                f"in {CLAUDE_SETTINGS} (env block). Leave any value that is already longer alone. "
                                f"This prevents a background request from being killed on the client side before "
                                f"the gateway answers. Top-level timeout keys do not take effect when LunaRoute is "
                                f"the base URL."),
                "impact": "prevents a demoted request from being killed before it speaks.",
            })

        if bg and wide:
            sub_model = _declared(cc_cfg.get("env.CLAUDE_CODE_SUBAGENT_MODEL"))
            if sub_model != bg:
                changes.append({
                    "key": "env.CLAUDE_CODE_SUBAGENT_MODEL",
                    "file": CLAUDE_SETTINGS,
                    "from": sub_model,
                    "to": bg,
                    "why": _why(evidence,
                                f"Children inherit the parent model unless this is set; `{bg}` carries the"
                                f" larger concurrency allowance.",
                                bg_note),
                    "instruction": (f"Ask your agent: in {CLAUDE_SETTINGS} set CLAUDE_CODE_SUBAGENT_MODEL to {bg} "
                                    f"so spawned subagents use the background model. Verify with: lunaroute models --json | grep {bg} "
                                    f"(or npx @lunaroute/cli models --json)."),
                    "impact": _impact(peak, target, width_known),
                })
        if changes:
            out.append({"agent": "claude-code", "changes": changes, "note": note})

    # ---------------- swamp ----------------
    if "swamp" in agents:
        # The unresolved value, not `target`: `target` falls back to the starting
        # constant, and passing it made the unknown-width path unreachable, so the
        # swamp block claimed a budget the report itself called unknown.
        swamp_recs = recommendations_swamp(m, agents, width=resolved)
        for grp in swamp_recs:
            out.append(grp)

    # ---------------- codex ----------------
    if "codex" in agents:
        cx_cfg = configs.codex
        keys = {}
        if _declared(cx_cfg.get("agents.max_concurrent_threads_per_session")) is None:
            keys["max_concurrent_threads_per_session"] = str(target)
        if bg and wide and _declared(cx_cfg.get("agents.default_subagent_model")) is None:
            keys["default_subagent_model"] = json.dumps(bg)
        if keys:
            pretty_keys = ", ".join(f"agents.{k}" for k in keys)
            desired = ", ".join(str(v).strip('"') for v in keys.values())
            out.append({
                "agent": "codex",
                "changes": [{
                    "key": pretty_keys,
                    "file": CODEX_CONFIG,
                    "from": "unset",
                    "to": desired,
                    "why": _why(evidence,
                                "Codex has no verified request-timeout knob, so it cannot defend itself"
                                " against a 600s first token; keep concurrent requests under the"
                                " advised width instead.",
                                bg_note),
                    "instruction": (f"Ask your agent: in {CODEX_CONFIG} set {pretty_keys} to {desired}. "
                                    f"Existing keys in that file must be preserved. No shell is needed — let the agent edit the TOML directly. "
                                    f"Remember codex reads [agents] from config.toml, but a LunaRoute profile in "
                                    f"~/.codex/lunaroute.config.toml can override tier — keep tier there, limits here."),
                    "impact": _impact(peak, target, width_known),
                }],
                "note": ("Codex reads `[agents]` from config.toml, but a LunaRoute profile lives in"
                         " ~/.codex/lunaroute.config.toml and wins for the keys it sets. Keep tier changes"
                         " in the profile file, limits in config.toml."),
            })

    # ---------------- sizing (the 2026-10-23 family) ----------------
    # Findings join the owning agent's group after the concurrency advice -
    # the report has always led with concurrency, and a sizing finding is a
    # second family, not a new headline. U-backed lines ride as from == to.
    for agent, changes in _sizing_changes(sizing_eval, agents).items():
        if not changes:
            continue
        group = next((g for g in out if g.get("agent") == agent), None)
        if group is None:
            out.append({"agent": agent, "changes": list(changes)})
        else:
            group["changes"].extend(changes)
    return out


def _val(v) -> str:
    return "unset" if v is None else str(v)


def render_fix(recs, m, as_json: bool) -> None:
    if as_json:
        print(json.dumps({
            "schema": SCHEMA_VERSION,
            "version": VERSION,
            "command": "fix",
            "contract": CONTRACT,
            "findings": {
                "peak_concurrency": m["peak_concurrency"],
                "admission_deaths": m["by_outcome"].get("admission_timeout", 0),
                "silent_stalls": m["by_outcome"].get("client_idle_timeout", 0),
                "background_requests": m["by_tier"].get("background", 0),
                # The account's priority concurrency allowance, in concurrent
                # requests. Resolved account-first, then a --plan declaration;
                # still null when neither is known: never 0, never a guess.
                "priority_concurrency": _priority_for_advice(m)[0],
                "priority_concurrency_source": _priority_for_advice(m)[1],
            },
            "agents": recs,
        }, indent=2))
        return
    if not recs:
        print("Nothing to change: no agent is configured in a way that can overflow the gateway.")
        return
    print("These are instructions for your agent. luna-doctor does not edit files itself —"
          " copy the relevant block and paste it to the agent that owns the harness.")
    print()
    for line in _wrap(_lunaroute_preface_text(), 72):
        print(line)
    print()
    width, source = _priority_for_advice(m)
    if isinstance(width, int):
        where = ("account" if source == "account"
                 else f"declared plan {source[len('plan '):]}" if source.startswith("plan ")
                 else source)
        print(f"Advice: keep concurrent requests within {width} ({where}) - the interactive "
              f"width this account admits, not a gateway-wide constant.")
    else:
        print("Advice: keep concurrent requests within your priority concurrency allowance. This "
              "machine cannot read that number from its logs: get it from the account with "
              "`luna-doctor capacity --online`, or declare one with `--plan <name or number>`.")
    if not m.get("requests"):
        print()
        print("Nothing was measured in this window: no agent log held a request, so everything "
              "below is the harness default being audited, not a measurement of your traffic. "
              "Run `luna-doctor discover` to find a store, then re-run before treating any of "
              "this as urgent.")
    print()
    for group in recs:
        n = len(group["changes"])
        print("=" * 72)
        print(f"{group['agent']}   {n} change{'s' if n > 1 else ''}")
        print("=" * 72)
        if group.get("note"):
            print()
            for line in _wrap(group["note"], 70):
                print("  ! " + line)
        for ch in group["changes"]:
            print()
            print(f"  {ch['key']}")
            print(f"  {_val(ch['from'])} -> {_val(ch['to'])}")
            print(f"  file: {ch['file']}")
            print()
            for line in _wrap(ch["why"], 70):
                print("    " + line)
            print()
            for line in _wrap("impact: " + ch["impact"], 70):
                print("    " + line)
            print()
            print("  Tell your agent (copy-paste):")
            print()
            for line in _wrap(ch["instruction"], 68):
                print("    " + line)
        print()
    # The loop does not end at advice: a change nobody verified is a change
    # whose effect nobody knows. The window before the change is already in
    # the same logs, so the check costs nothing and needs no network.
    print("=" * 72)
    print("verify")
    print("=" * 72)
    print()
    for line in _wrap(
            "After restarting the harness, run: luna-doctor analyze --baseline prev", 70):
        print("  " + line)
    for line in _wrap(
            "Success looks like `better`: a lower rate of blocked-at-admission requests than the "
            "window before it, at the same time as no new silent stalls and no new 429s. The "
            "comparison says `inconclusive` when either window holds too few requests to judge, "
            "and that is not a failure.", 70):
        print("  " + line)
    print()


def _wrap(text: str, width: int) -> list:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def cmd_fix(args) -> int:
    agents = [a.strip() for a in (args.agent.split(",") if args.agent else KNOWN_AGENTS) if a.strip()]
    unknown = [a for a in agents if a not in KNOWN_AGENTS]
    if unknown:
        print(f"luna-doctor: unknown agent(s): {', '.join(unknown)}. Choose from {', '.join(KNOWN_AGENTS)}.",
              file=sys.stderr)
        return 2
    try:
        hours = window_hours(args.since, args.now)
    except ValueError as e:
        print(f"luna-doctor: {e}", file=sys.stderr)
        return 2
    harnesses = [h.strip() for h in args.harness.split(",") if h.strip()]
    now = time.time()
    reqs = collect(hours, harnesses, getattr(args, "pi_root", None),
                   getattr(args, "claude_root", None), now=now)
    m = analyse(reqs, hours)
    # The same priority-tier peak `analyze` computes. Without it every width
    # comparison on this path silently fell back to total traffic, and the advice
    # told users to lower settings because of entitled background work.
    try:
        m["peak_concurrency_priority"] = max_concurrency(
            [r for r in timed(reqs) if getattr(r, "tier", "") == "interactive"])[0]
    except Exception:
        m["peak_concurrency_priority"] = m.get("peak_concurrency")
    m["declared_plan"] = getattr(args, "plan", None)
    try:
        m["capacity"] = _capacity_metrics(now - hours * 3600, now, m)
    except Exception:
        m["capacity"] = {"provenance": sources.PROV_UNKNOWN}
    # One combined snapshot feeds the advice: the harness configs with
    # provenance, and the catalog (cached by default; fetched only with
    # --online). A collection failure degrades to unset values and an empty
    # catalog - the advice still renders from the measured logs, and a
    # network failure is never an error. The same snapshot feeds the sizing
    # rules (sizing.evaluate): a snapshot that could not be built leaves them
    # explicitly unchecked, never silently clean.
    snap = None
    try:
        snap = sources.build_snapshot(reqs, since=now - hours * 3600, until=now,
                                      allow_network=bool(getattr(args, "online", False)))
        configs = snap.configs
        catalog_ids = snap.catalog.ids() if snap.catalog is not None else []
        if bool(getattr(args, "online", False)) and snap.catalog is None:
            print(f"luna-doctor: catalog unavailable ({snap.catalog_note}); "
                  f"the advice below falls back to the models seen in the logs",
                  file=sys.stderr)
    except Exception as e:
        print(f"luna-doctor: snapshot collection failed ({type(e).__name__}: {e}); "
              f"config values read as unset", file=sys.stderr)
        configs, catalog_ids = None, []
    sizing_eval = sizing.evaluate(snap)
    recs = recommendations(m, reqs, agents, configs=configs, catalog_ids=catalog_ids,
                           sizing_eval=sizing_eval)
    render_fix(recs, m, args.json)
    actionable = any(ch["from"] != ch["to"] for g in recs for ch in g["changes"])
    return 1 if actionable else 0
