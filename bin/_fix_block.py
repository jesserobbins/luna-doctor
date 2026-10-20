#!/usr/bin/env python3
"""`fix` command for luna-doctor.

Executed inside bin/luna-doctor's namespace, so it may use Request, collect,
analyse, window_hours, ADVISE_FANOUT_MAX and friends.

Design rules, learned from verification:
  * Never write. Print only.
  * A config the tool cannot parse is a config the tool refuses to touch.
  * Every emitted key must be VERIFIED in
    luna-doctor-private/research/2026-10-21-agent-concurrency-knobs.md.
  * Say plainly when a recommendation cannot change current behaviour.
  * Do not re-compress a value the user already set more permissively
    (`httpIdleTimeoutMs: 0` means disabled).
"""

RAISE_TIMEOUT_MS = 900000       # 15 min: past the ~633s background first token
KNOWN_AGENTS = ("pi", "claude-code", "codex")

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


def pick_background_model(reqs, cat) -> str | None:
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


def _jq_apply(path: str, filter_: str, label: str) -> str:
    """Back up, validate, merge into a temp file, then move it into place.

    The earlier version piped jq straight into the live file, which truncated
    the config to zero bytes whenever jq failed or was missing.
    """
    return "\n".join([
        f"f={path}",
        'mkdir -p "$(dirname "$f")"',
        '[ -f "$f" ] || echo \'{}\' > "$f"',
        'command -v jq >/dev/null || { echo "refusing: jq is not installed (brew install jq)"; exit 1; }',
        'jq -e . "$f" >/dev/null || { echo "refusing: $f is not valid JSON, fix it first"; exit 1; }',
        'cp "$f" "$f.bak.$(date +%Y%m%d%H%M%S)"',
        f"jq '{filter_}' \"$f\" > \"$f.tmp\" && mv \"$f.tmp\" \"$f\"   # {label}",
        'jq -e . "$f" >/dev/null && echo ok',
    ])


def _toml_agents_apply(path: str, keys: dict) -> str:
    """Merge keys into the existing [agents] table, or create it.

    Appending a second `[agents]` header is a TOML error, and a heredoc whose
    terminator gets indented never terminates - so the merge is done by python
    reading the file, and the emitted block is never indented by the renderer.
    """
    keys_lit = "{" + ", ".join(f"{k!r}: {v!r}" for k, v in keys.items()) + "}"
    body = "\n".join([
        "import os, re, tomllib",
        'p = os.environ["F"]',
        f"keys = {keys_lit}",
        'src = open(p).read().splitlines() if os.path.exists(p) else []',
        "out = list(src)",
        "idx = next((i for i, l in enumerate(out) if re.match(r'^\\[agents\\]\\s*(#.*)?$', l.strip())), None)",
        "if idx is None:",
        "    if out and out[-1].strip():",
        '        out.append("")',
        '    out.append("[agents]")',
        "    out += [f'{k} = {v}' for k, v in keys.items()]",
        "else:",
        "    end = len(out)",
        "    for j in range(idx + 1, len(out)):",
        "        if out[j].startswith('['):",
        "            end = j",
        "            break",
        "    block = out[idx + 1:end]",
        "    left = dict(keys)",
        "    for i, l in enumerate(block):",
        "        m = re.match(r'\\s*([A-Za-z0-9_]+)\\s*=', l)",
        "        if m and m.group(1) in left:",
        "            block[i] = f'{m.group(1)} = {left.pop(m.group(1))}'",
        "    block += [f'{k} = {v}' for k, v in left.items()]",
        "    if end < len(out) and block and block[-1].strip():",
        '        block.append("")',
        "    out[idx + 1:end] = block",
        "text = '\\n'.join(out) + '\\n'",
        "try:",
        "    doc = tomllib.loads(text)",
        "except Exception as e:",
        '    print("FAILED: merged TOML would not parse:", e)',
        "    raise SystemExit(1)",
        "got = doc.get('agents') or {}",
        "missing = [k for k in keys if k not in got]",
        "if missing:",
        '    print("FAILED: keys missing after merge:", missing)',
        "    raise SystemExit(1)",
        "import shutil, time",
        'shutil.copy2(p, p + ".bak." + time.strftime("%Y%m%d%H%M%S"))',
        'open(p, "w").write(text)',
        "print('ok:', {k: got[k] for k in keys})",
    ])
    head = "\n".join([
        f"f={path}",
        'mkdir -p "$(dirname "$f")"',
        '[ -f "$f" ] || : > "$f"',
        'F="$f" python3 - <<\'PY\'',
    ])
    return head + "\n" + body + "\n" + "PY"


def _why(*bits: str) -> str:
    return " ".join(b for b in bits if b)


def _impact(peak: int) -> str:
    if peak <= ADVISE_FANOUT_MAX:
        return (f"no effect at your current peak of {peak} concurrent requests - this is headroom so "
                f"the next wide fan-out cannot overflow into the background tier.")
    return f"your peak of {peak} concurrent requests is already past this limit, so this is the change that bites."


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
        f"{dead} admission death(s)," if dead else "",
        f"{idle_stalls} silent stall(s)," if idle_stalls else "",
        f"{bgcount} request(s) already on the background tier." if bgcount else "",
    )

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
                            f"This machine caps children at {limit}, above the gateway's interactive"
                            f" burst cap of {INTERACTIVE_BURST_CAP}: anything past the cap is demoted to the"
                            f" background tier, where the first token takes about 600s."),
                "impact": _impact(peak),
                "apply": _jq_apply(PI_SUBAGENT_CFG,
                                   f".globalConcurrencyLimit = {ADVISE_FANOUT_MAX}",
                                   "cap simultaneous children"),
                "snippet": json.dumps({"globalConcurrencyLimit": ADVISE_FANOUT_MAX}),
            })
        elif limit is None:
            changes.append({
                "key": "globalConcurrencyLimit",
                "file": PI_SUBAGENT_CFG,
                "from": "unset (default 20)",
                "to": ADVISE_FANOUT_MAX,
                "why": _why(evidence,
                            "The default lets one run fan out 20 children, double the gateway's interactive"
                            " burst cap."),
                "impact": _impact(peak),
                "apply": _jq_apply(PI_SUBAGENT_CFG,
                                   f".globalConcurrencyLimit = {ADVISE_FANOUT_MAX}",
                                   "cap simultaneous children"),
                "snippet": json.dumps({"globalConcurrencyLimit": ADVISE_FANOUT_MAX}),
            })

        # 0 means disabled, which is already more permissive than our value.
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
                "impact": "prevents a background request from being killed by the client before it speaks.",
                "apply": _jq_apply(PI_SETTINGS,
                                   f".httpIdleTimeoutMs = {RAISE_TIMEOUT_MS}",
                                   "outlast the background tier"),
                "snippet": json.dumps({"httpIdleTimeoutMs": RAISE_TIMEOUT_MS}),
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
                "impact": "nothing to do; recorded so the verdict is not silent about it.",
                "apply": "true   # nothing to apply",
                "snippet": json.dumps({"httpIdleTimeoutMs": 0}),
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
                                f" the larger concurrency allowance; the trade is a much slower SLA."),
                    "impact": _impact(peak),
                    "apply": _jq_apply(PI_SETTINGS,
                                       f'.subagents.defaultModel = "{target}"',
                                       "send children to the background tier"),
                    "snippet": json.dumps({"subagents": {"defaultModel": target}}),
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
                    " LunaRoute at all. Run `lunaroute setup claude-code` first; the tier advice below"
                    " only matters once traffic is routed.")

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
                            f"The default is 20 concurrent subagents, double the gateway's interactive cap"
                            f" of {INTERACTIVE_BURST_CAP}."),
                "impact": _impact(peak),
                "apply": _jq_apply(CLAUDE_SETTINGS,
                                   f'.env.CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS = "{ADVISE_FANOUT_MAX}"',
                                   "cap concurrent subagents"),
                "snippet": json.dumps({"env": {"CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": str(ADVISE_FANOUT_MAX)}}),
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
                "impact": "prevents a demoted request from being killed before it speaks.",
                "apply": _jq_apply(CLAUDE_SETTINGS,
                                   f'.env.CLAUDE_STREAM_IDLE_TIMEOUT_MS = "{RAISE_TIMEOUT_MS}"\n'
                                   f'   | .env.API_TIMEOUT_MS = "{RAISE_TIMEOUT_MS}"',
                                   "outlast the background tier (two keys)"),
                "snippet": json.dumps({"env": {"CLAUDE_STREAM_IDLE_TIMEOUT_MS": str(RAISE_TIMEOUT_MS),
                                               "API_TIMEOUT_MS": str(RAISE_TIMEOUT_MS)}}),
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
                                f" larger concurrency allowance."),
                    "impact": _impact(peak),
                    "apply": _jq_apply(CLAUDE_SETTINGS,
                                       f'.env.CLAUDE_CODE_SUBAGENT_MODEL = "{bg}"',
                                       "send children to the background tier"),
                    "snippet": json.dumps({"env": {"CLAUDE_CODE_SUBAGENT_MODEL": bg}}),
                })
        if changes:
            out.append({"agent": "claude-code", "changes": changes, "note": note})

    # ---------------- codex ----------------
    if "codex" in agents:
        text = _read_text(CODEX_CONFIG)
        keys = {}
        if "max_concurrent_threads_per_session" not in text:
            keys["max_concurrent_threads_per_session"] = str(ADVISE_FANOUT_MAX)
        if bg and wide and "default_subagent_model" not in text:
            keys["default_subagent_model"] = json.dumps(bg)
        if keys:
            out.append({
                "agent": "codex",
                "changes": [{
                    "key": ", ".join(f"agents.{k}" for k in keys),
                    "file": CODEX_CONFIG,
                    "from": "unset",
                    "to": ", ".join(str(v).strip('"') for v in keys.values()),
                    "why": _why(evidence,
                                "Codex has no verified request-timeout knob, so it cannot defend itself"
                                " against a 600s first token; keep the fan-out under the cap instead."),
                    "impact": _impact(peak),
                    "apply": _toml_agents_apply(CODEX_CONFIG, keys),
                    "snippet": "[agents]\n" + "\n".join(f"{k} = {v}" for k, v in keys.items()),
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
    print("Copy-paste fixes. Nothing below runs on its own; each block backs the file up first.")
    print(f"Fan-out limit used throughout: {ADVISE_FANOUT_MAX} (gateway interactive cap is "
          f"{INTERACTIVE_BURST_CAP}).")
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
            print("  apply (copy these lines exactly):")
            print()
            for line in ch["apply"].splitlines():
                print(line)
            print()
            where = ("inside the existing [agents] table" if ch["file"].endswith(".toml")
                     else "inside the existing top-level object")
            print(f"  or edit by hand: ADD these keys {where}. Do not replace the file.")
            for line in ch["snippet"].splitlines():
                print("    " + line)
        print()
    print("=" * 72)
    print("Then confirm the shape of your traffic, not just the config:")
    print(f"  luna-doctor probe   --n {ADVISE_FANOUT_MAX} --model <interactive model> --max-seconds 200")
    print(f"  luna-doctor compare --n {ADVISE_FANOUT_MAX} --model <interactive model> --max-seconds 200")


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
    reqs = collect(hours, harnesses)
    m = analyse(reqs, hours)
    recs = recommendations(m, reqs, agents)
    render_fix(recs, m, args.json)
    actionable = any(ch["from"] != ch["to"] for g in recs for ch in g["changes"])
    return 1 if actionable else 0
