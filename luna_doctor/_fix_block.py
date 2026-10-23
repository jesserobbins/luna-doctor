#!/usr/bin/env python3
"""`fix` command for luna-doctor.

Executed inside luna_doctor.cli's namespace, so it may use Request, collect,
analyse, window_hours, ADVISE_FANOUT_MAX and friends.

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

# Swamp env paths — citations point to extension source for defaults and
# ~/.swamp.yaml + .env.swamp for live values.
SWAMP_ENV_FILE = "~/.swamp.yaml"
SWAMP_ENV_SWAMP = ".env.swamp"  # repo-local variant
SWAMP_ENV_GLOBAL_SOURCES = "~/.swamp-sources.yaml"
SWAMP_CITATION_DEFAULT = "extensions/models/obsidian_ideas.ts"
SWAMP_DEFAULT_EXTRACT = 12   # extensions/models/obsidian_ideas.ts phase extractMany
SWAMP_DEFAULT_LLM = 8        # extensions/models/obsidian_ideas.ts runVerifyPass

# Verified paths (research/2026-10-21-agent-concurrency-knobs.md).
PI_SETTINGS = "~/.pi/agent/settings.json"
PI_SUBAGENT_CFG = "~/.pi/agent/extensions/subagent/config.json"
CLAUDE_SETTINGS = "~/.claude/settings.json"
CODEX_CONFIG = "~/.codex/config.toml"


def _read_json(path: str) -> dict:
    try:
        with open(os.path.expanduser(path)) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _read_text(path: str) -> str:
    try:
        with open(os.path.expanduser(path)) as fh:
            return fh.read()
    except Exception:
        return ""


def _swamp_present(window_hours: float = 24.0) -> tuple[bool, str | None]:
    """The swamp advice gate: in use, not merely installed."""
    state = swamp_state(since=time.time() - window_hours * 3600)
    return bool(state["in_use"]), state["repo"]


def resolve_swamp_knobs(repo_dir: str | None = None) -> dict:
    """Effective swamp concurrency knobs with provenance."""
    if repo_dir is None:
        _, repo_dir = _swamp_present()
    env_swamp_values: dict[str, str] = {}
    if repo_dir:
        env_path = os.path.join(repo_dir, ".env.swamp")
        if os.path.isfile(env_path):
            try:
                with open(env_path) as fh:
                    for line in fh:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if "=" in line:
                            k, v = line.split("=", 1)
                            env_swamp_values[k.strip()] = v.strip().strip('"\'')
            except Exception:
                pass
    swamp_yaml_values: dict[str, str] = {}
    for yaml_path in (os.path.expanduser(SWAMP_ENV_FILE),
                      os.path.expanduser(SWAMP_ENV_GLOBAL_SOURCES)):
        if os.path.isfile(yaml_path):
            try:
                with open(yaml_path) as fh:
                    for line in fh:
                        line = line.strip()
                        if not line or line.startswith("#") or ":" not in line:
                            continue
                        k, v = line.split(":", 1)
                        k, v = k.strip(), v.strip().strip('"\'')
                        if k in ("SWAMP_EXTRACT_CONCURRENCY", "SWAMP_LLM_CONCURRENCY",
                                  "SWAMP_LLM_MODEL", "SWAMP_LLM_PROVIDER"):
                            swamp_yaml_values[k] = v
            except Exception:
                pass

    def _resolve(key: str, default: int | str) -> dict:
        if key in os.environ:
            raw = os.environ[key].strip()
            try:
                return {"value": int(raw) if isinstance(default, int) else raw, "source": "env", "raw": raw}
            except ValueError:
                return {"value": raw, "source": "env", "raw": raw}
        if key in swamp_yaml_values:
            raw = swamp_yaml_values[key]
            try:
                return {"value": int(raw) if isinstance(default, int) else raw, "source": "~/.swamp.yaml", "raw": raw}
            except ValueError:
                return {"value": raw, "source": "~/.swamp.yaml", "raw": raw}
        if key in env_swamp_values:
            raw = env_swamp_values[key]
            try:
                return {"value": int(raw) if isinstance(default, int) else raw, "source": ".env.swamp", "raw": raw}
            except ValueError:
                return {"value": raw, "source": ".env.swamp", "raw": raw}
        return {"value": default, "source": "default", "raw": str(default)}

    return {
        "extractConcurrency": _resolve("SWAMP_EXTRACT_CONCURRENCY", SWAMP_DEFAULT_EXTRACT),
        "llmConcurrency": _resolve("SWAMP_LLM_CONCURRENCY", SWAMP_DEFAULT_LLM),
        "model": _resolve("SWAMP_LLM_MODEL", "deepseek-4.1-flash-background"),
        "provider": _resolve("SWAMP_LLM_PROVIDER", "lunaroute"),
    }


def _swamp_split() -> tuple[int, int, int]:
    """Recommended split with pi+extract+llm <= INTERACTIVE_BURST_CAP."""
    cap = INTERACTIVE_BURST_CAP
    pi_rec, sw_extract, sw_llm = 4, 3, 2
    total = pi_rec + sw_extract + sw_llm
    if total > cap:
        over = total - cap
        take_extract = min(over, max(0, sw_extract - 1))
        sw_extract -= take_extract
        over -= take_extract
        if over > 0:
            take_llm = min(over, max(0, sw_llm - 1))
            sw_llm -= take_llm
            over -= take_llm
        if over > 0:
            pi_rec = max(2, pi_rec - over)
    return pi_rec, sw_extract, sw_llm


def recommendations_swamp(m: dict, agents: list[str]) -> list:
    """Swamp advice — only when agents includes swamp and swamp is in use."""
    if "swamp" not in agents:
        return []
    present, repo_dir = _swamp_present(float(m.get("window_hours") or 24.0))
    if not present:
        return []
    knobs = resolve_swamp_knobs(repo_dir)
    extract_val = knobs["extractConcurrency"]["value"]
    llm_val = knobs["llmConcurrency"]["value"]
    try:
        extract_n = int(extract_val)
        llm_n = int(llm_val)
    except (TypeError, ValueError):
        extract_n = SWAMP_DEFAULT_EXTRACT
        llm_n = SWAMP_DEFAULT_LLM
    pi_budget = ADVISE_FANOUT_MAX
    swamp_total = extract_n + llm_n
    combined = pi_budget + swamp_total
    cap = INTERACTIVE_BURST_CAP
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
    pi_rec, extract_rec, llm_rec = _swamp_split()
    assert pi_rec + extract_rec + llm_rec <= cap, f"split {pi_rec}+{extract_rec}+{llm_rec} exceeds cap {cap}"
    combined_rec = pi_rec + extract_rec + llm_rec
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
                                f"Check it with your swamp runner's env and restart it so the next batch respects the new fan-out."),
                "impact": f"keeps combined pi {pi_rec} + extract {extract_rec} + phase-6 {llm_rec} = {combined_rec} within the combined budget.",
            },
        ],
        "combined_budget": {
            "pi": pi_budget, "extract": extract_n, "phase6": llm_n,
            "combined": combined, "cap": cap,
            "recommended": {"pi": pi_rec, "extract": extract_rec, "phase6": llm_rec, "combined": combined_rec},
        },
    }]


def catalog() -> list:
    """Model ids from the lunaroute CLI; empty when unavailable."""
    try:
        import subprocess
        out = subprocess.run(["lunaroute", "models", "--json"], capture_output=True,
                             text=True, timeout=8)
        if out.returncode == 0:
            data = json.loads(out.stdout)
            if isinstance(data, list):
                return [str(m) for m in data]
    except Exception:
        pass
    return []


def pick_background_model(reqs, cat) -> list | None:
    counts = {}
    for r in reqs:
        if r.tier == "interactive" and r.model:
            counts[r.model] = counts.get(r.model, 0) + 1
    known = set(cat) | {r.model for r in reqs}
    if counts:
        top = max(counts, key=counts.get)
        twin = top if top.endswith("-background") else top + "-background"
        if twin in known:
            return [twin]
    for cand in sorted(known):
        if cand.endswith("-background"):
            return [cand]
    return None


def _why(*bits: str) -> str:
    return " ".join(b for b in bits if b)


def _impact(peak: int) -> str:
    if peak <= ADVISE_FANOUT_MAX:
        return (f"no effect at your current peak of {peak} concurrent requests - this is headroom so "
                f"the next wide fan-out cannot overflow into the background tier.")
    return f"your peak of {peak} concurrent requests is already past this limit, so this is the change that bites."


def demotion_note(reason: str, model: str) -> str:
    if reason == "no_entitlement":
        return (f"`{model}` is being served on priority concurrency: this account has no background "
                f"entitlement, so its `-background` requests are downgraded into the priority pool "
                f"and count against interactive headroom.")
    return (f"your `-background` traffic may be running on priority concurrency: if this account has "
            f"no background entitlement, `{model}` requests are downgraded into the priority pool, "
            f"which is why a background model can still be squeezed. Only a probe can see the "
            f"served tier.")


# ---- lunaroute explainer (instructions-only fix) --------------------------
# Rendered once at the top of every non-JSON fix report so the per-agent
# instructions make sense even to a first-time user. Short by design:
# what LunaRoute is, what plan/tier means here, how fan-out fits, and how to
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
    "should ride the background tier — it is slower but admits wider fan-out. "
    "Interactive work (the session you are watching) rides the priority tier. "
    "Every non-interactive call should use the background variant of your model (e.g. "
    "`glm-5.3-background` rather than `glm-5.3`). If your account has no background "
    "entitlement, background requests are downgraded into the priority pool and still "
    "count against the same budget — a probe shows the served tier either way."
)

LUNAROUTE_COMMAND_PREFACE = (
    "To inspect your LunaRoute setup, use the `lunaroute` command:\n"
    "  lunaroute whoami          # who is signed in and on what plan\n"
    "  lunaroute models --json   # what models are available on this account\n"
    "  lunaroute usage --json    # remaining budget / concurrency headroom\n"
    "If `lunaroute` is not on your PATH, run it via npx instead:\n"
    "  npx @lunaroute/cli whoami\n"
    "  npx @lunaroute/cli models --json\n"
    "The `fix` instructions below name the exact config key and desired value. "
    "Do not paste generated code — paste the instruction to your agent and let "
    "the agent apply it."
)


def _lunaroute_preface_text() -> str:
    return "\n\n".join([LUNAROUTE_PREFACE, LUNAROUTE_ROUTING_PREFACE, LUNAROUTE_COMMAND_PREFACE])


def recommendations(m, reqs, agents) -> list:
    out = []
    cat = catalog()
    bg = pick_background_model(reqs, cat)
    peak = m["peak_concurrency"]
    dead = m["by_outcome"].get("admission_timeout", 0)
    idle_stalls = m["by_outcome"].get("client_idle_timeout", 0)
    bgcount = m["by_tier"].get("background", 0)
    wide = peak > ADVISE_FANOUT_MAX or dead > 0
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
        settings = _read_json(PI_SETTINGS)
        subcfg = _read_json(PI_SUBAGENT_CFG)
        limit = subcfg.get("globalConcurrencyLimit")
        idle_ms = settings.get("httpIdleTimeoutMs")
        changes = []

        if isinstance(limit, int) and limit > ADVISE_FANOUT_MAX:
            changes.append({
                "key": "globalConcurrencyLimit",
                "file": PI_SUBAGENT_CFG,
                "from": limit,
                "to": ADVISE_FANOUT_MAX,
                "why": _why(evidence,
                            f"This machine caps children at {limit}, wider than the interactive "
                            f"admission width - {CAP_COPY}. Past it, requests are demoted to the "
                            f"background tier, where the first token takes about 600s."),
                "instruction": (f"Ask your agent: in {PI_SUBAGENT_CFG} set globalConcurrencyLimit to {ADVISE_FANOUT_MAX} "
                                f"(was {limit}). This caps simultaneous child agents so your burst no longer overflows "
                                f"the interactive lane and gets demoted to background. Restart the harness after the change."),
                "impact": _impact(peak),
            })
        elif limit is None:
            changes.append({
                "key": "globalConcurrencyLimit",
                "file": PI_SUBAGENT_CFG,
                "from": "unset (default 20)",
                "to": ADVISE_FANOUT_MAX,
                "why": _why(evidence,
                            "The default lets one run fan out 20 children, well past the interactive "
                            f"admission width - {CAP_COPY}."),
                "instruction": (f"Ask your agent: in {PI_SUBAGENT_CFG} set globalConcurrencyLimit to {ADVISE_FANOUT_MAX}. "
                                f"The current default (20) will keep overflowing the interactive tier under load; lower it. "
                                f"Have the agent read the existing file first, preserve other keys, then apply the change."),
                "impact": _impact(peak),
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
            target = bg if "/" in bg else f"lunaroute/{bg}"
            cur = (settings.get("subagents") or {}).get("defaultModel")
            if cur != target:
                changes.append({
                    "key": "subagents.defaultModel",
                    "file": PI_SETTINGS,
                    "from": cur,
                    "to": target,
                    "why": _why(evidence,
                                f"Children currently inherit the interactive model. `{bg}` is the tier with"
                                f" the larger concurrency allowance; the trade is a much slower SLA.",
                                bg_note),
                    "instruction": (f"Ask your agent: in {PI_SETTINGS} set your default background model to {target} "
                                    f"so child agents ride the background tier by default (was {cur!r}). Ask it to verify the model "
                                    f"exists via: lunaroute models --json   (or: npx @lunaroute/cli models --json)."),
                    "impact": _impact(peak),
                })
        if changes:
            out.append({"agent": "pi", "changes": changes})

    # ---------------- claude-code ----------------
    if "claude-code" in agents:
        settings = _read_json(CLAUDE_SETTINGS)
        env = settings.get("env") or {}
        changes, note = [], None
        if not settings.get("env", {}).get("ANTHROPIC_BASE_URL"):
            note = ("This settings file has no ANTHROPIC_BASE_URL, so claude-code may not be reaching"
                    " LunaRoute at all. Run `lunaroute setup claude-code` (or `npx @lunaroute/cli setup claude-code`) "
                    "first; the tier advice below only matters once traffic is routed.")

        subs = env.get("CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS")

        def too_big(v, cap):
            try:
                return v is None or int(str(v)) > cap
            except ValueError:
                return True

        if too_big(subs, ADVISE_FANOUT_MAX):
            changes.append({
                "key": "env.CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS",
                "file": CLAUDE_SETTINGS,
                "from": subs if subs is not None else "unset (default 20)",
                "to": str(ADVISE_FANOUT_MAX),
                "why": _why(evidence,
                            f"The default is 20 concurrent subagents, well past the interactive "
                            f"admission width - {CAP_COPY}."),
                "instruction": (f"Ask your agent: in {CLAUDE_SETTINGS} set CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS to {ADVISE_FANOUT_MAX}. "
                                f"The default 20 keeps overflowing interactive headroom."),
                "impact": _impact(peak),
            })

        idle_env = env.get("CLAUDE_STREAM_IDLE_TIMEOUT_MS")
        if too_big(idle_env, 300000) or idle_env is None:
            changes.append({
                "key": "env.CLAUDE_STREAM_IDLE_TIMEOUT_MS + env.API_TIMEOUT_MS",
                "file": CLAUDE_SETTINGS,
                "from": idle_env if idle_env is not None else "unset (floor 300000)",
                "to": str(RAISE_TIMEOUT_MS),
                "why": _why(evidence,
                            "The stream idle floor is 300s and the API timeout is shorter than the background"
                            " tier's time to first token. Both belong in the `env` block: the top-level"
                            " settings keys are ignored behind a custom base URL, which LunaRoute is."),
                "instruction": (f"Ask your agent to raise CLAUDE_STREAM_IDLE_TIMEOUT_MS and API_TIMEOUT_MS to {RAISE_TIMEOUT_MS} "
                                f"in {CLAUDE_SETTINGS} (env block). This is what prevents a background request from being killed on the "
                                f"client side before the gateway answers. Top-level timeout keys do not take effect when LunaRoute is the base URL."),
                "impact": "prevents a demoted request from being killed before it speaks.",
            })

        if bg and wide:
            sub_model = env.get("CLAUDE_CODE_SUBAGENT_MODEL")
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
                    "impact": _impact(peak),
                })
        if changes:
            out.append({"agent": "claude-code", "changes": changes, "note": note})

    # ---------------- swamp ----------------
    if "swamp" in agents:
        swamp_recs = recommendations_swamp(m, agents)
        for grp in swamp_recs:
            out.append(grp)

    # ---------------- codex ----------------
    if "codex" in agents:
        text = _read_text(CODEX_CONFIG)
        keys = {}
        if "max_concurrent_threads_per_session" not in text:
            keys["max_concurrent_threads_per_session"] = str(ADVISE_FANOUT_MAX)
        if bg and wide and "default_subagent_model" not in text:
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
                                " against a 600s first token; keep the fan-out under the advised width instead.",
                                bg_note),
                    "instruction": (f"Ask your agent: in {CODEX_CONFIG} set {pretty_keys} to {desired}. "
                                    f"Existing keys in that file must be preserved. No shell is needed — let the agent edit the TOML directly. "
                                    f"Remember codex reads [agents] from config.toml, but a LunaRoute profile in "
                                    f"~/.codex/lunaroute.config.toml can override tier — keep tier there, limits here."),
                    "impact": _impact(peak),
                }],
                "note": ("Codex reads `[agents]` from config.toml, but a LunaRoute profile lives in"
                         " ~/.codex/lunaroute.config.toml and wins for the keys it sets. Keep tier changes"
                         " in the profile file, limits in config.toml."),
            })
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
                "fanout_limit": ADVISE_FANOUT_MAX,
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
    print(f"Fan-out limit for the advice below: {ADVISE_FANOUT_MAX} "
          f"({CAP_COPY}).")
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
    reqs = collect(hours, harnesses, getattr(args, "pi_root", None),
                   getattr(args, "claude_root", None))
    m = analyse(reqs, hours)
    recs = recommendations(m, reqs, agents)
    render_fix(recs, m, args.json)
    actionable = any(ch["from"] != ch["to"] for g in recs for ch in g["changes"])
    return 1 if actionable else 0
