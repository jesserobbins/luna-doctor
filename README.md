# luna-doctor — `0.1.1-alpha.1` · prerelease

> **Alpha.** The CLI, JSON schema, and measured gateway behaviour may evolve without a major bump until `1.0.0`. Pin to an exact tag if you embed this in automation.

Diagnose what your LunaRoute agent traffic is doing to concurrency — and what to change.

LunaRoute bills **concurrency**, not tokens. When you exceed it the gateway does not return a 429. It waits, or demotes the request to a background tier and holds the stream open with keep-alives while producing nothing. The harness eventually kills the stall with an error string of its own making — which is why grepping logs for 429 finds nothing.

`luna-doctor` reads the session logs the machine already has, reconstructs what was asked for at what tier and what came back, and tells you what to adjust. Thresholds are measured on your machine (via `probe`), not assumed from ours.

## Install

**Requirements:** Python 3.9+ and `lunaroute login` (for `probe`/`compare`; `discover` and `analyze` are offline and need no credentials).

```bash
git clone https://github.com/jesserobbins/luna-doctor.git
cd luna-doctor
pipx install .        # installs the `luna-doctor` command
luna-doctor --version # should print 0.1.1-alpha.1
```

Or without packaging — just put it on your `PATH`:

```bash
ln -s "$PWD/bin/luna-doctor" ~/.local/bin/luna-doctor
luna-doctor --version
```

## Quick start

```bash
luna-doctor discover          # what log stores exist, how big, how fresh
luna-doctor analyze           # last 24h: what happened and what to adjust
luna-doctor analyze --now     # last 15 minutes (alias for --since 15m)
luna-doctor analyze --since 7d --json   # machine-readable, for cron/CI
luna-doctor fix               # per-agent copy-paste config, from the evidence
luna-doctor fix --agent pi    # …or one agent at a time (pi | claude-code | codex | swamp)
luna-doctor chart --since 24h # SVG: fan-out over time + every request's latency
luna-doctor chart --since 24h --open  # …and open it in your browser
```

Exit codes: `0` no pressure, `1` pressure found, `2` no data — usable from a shell prompt, a skill, or cron.

To measure the gateway itself (this is a live burst — it burns concurrent
sessions on your account, and needs `lunaroute login`):

```bash
luna-doctor probe --n 8 --model <model>     # live burst measurement at width 8
luna-doctor compare --n 8 --model <model>   # model vs its -background twin
```

## How it works

### `analyze` — what your machine has been doing

Reconstructs every model request from the local log stores (pi, claude-code) and classifies each outcome against measured gateway behaviour — admission timeouts, silent background demotions, upstream errors, rate limits, and more. Thresholds (admission deadlines, idle budgets) are measured per model via `probe`, not hardcoded — and the number of interactive requests the gateway admits at once is per account, not a constant: measure yours with `luna-doctor probe --enable-probe`. `analyze` reports what it observed for each model in the window.

### `fix` — what to change, with copy-paste you can audit

Prints one block per agent: the exact file, the snippet, and a command that backs the file up, validates it, merges the key, and prints `ok`. It never writes anything itself. Every key it emits was verified against the installed harness bundles or their shipped docs; anything unverifiable is not emitted.

### `chart` — one picture

A standalone SVG (no fonts, scripts, or CDN — opens anywhere). Top panel: requests in flight over time, split by tier, with the gateway's rough admission level and the advised fan-out as dashed rules. Bottom panel: every request end-to-end on a log axis, with reference walls for admission and idle budgets — failures sit on the wall that killed them. A burst in the top panel at the same x as a cluster of failures below is the whole diagnosis.

### `probe` / `compare` — what the gateway will actually admit

Sends controlled bursts and captures the response headers that say which tier served the request and whether it was demoted. Use this to measure your admission deadlines and the background tier's behaviour on your models, then feed the result back into `analyze`.

## What it reads

Read-only and offline for `discover` / `analyze` / `fix` / `chart`:

| harness | store |
|---|---|
| pi | `~/.pi/agent/sessions/**/*.jsonl` |
| claude-code | `~/.claude/projects/**/*.jsonl` |
| codex | detected and reported, not analysed (zstd rollouts, usually not LunaRoute traffic) |
| swamp | `~/.swamp` / sibling `swamp-tracker` repo, when present |

`probe` and `compare` are the only commands that touch the network, and they use the routing key already stored by `lunaroute login`.

## Limits worth knowing

- Log records capture when a request started and when its record was written. They do **not** record time to first token, so `analyze` measures end-to-end latency and classifies outcomes; only `probe` measures the wait itself.
- Error records zero their token usage, so input size is unavailable on failures.
- Concurrency reconstructed from one machine is a lower bound; other machines and other tenants are invisible.

## Repo layout

| path | what |
|---|---|
| `luna_doctor/cli.py` | the CLI, the log readers and the analyser |
| `luna_doctor/_fix_block.py` | the `fix` command, kept apart so its snippets can be read and verified on their own |
| `luna_doctor/_chart_block.py` | the `chart` command (SVG) |
| `bin/luna-doctor` | launcher for the no-packaging path; resolves its own symlink and imports the package |

The three package files ship together. `cli` loads the other two off disk from its own directory, so a partial install is the one thing that breaks `fix` and `chart`.

## How this project is built

This project is developed using a **Greenroom** — this public repo beside a private one that holds the design docs, drafts and review notes, so work can be shaped before it takes the stage. If you want to read the private greenroom repo, reach out and ask.

Greenroom is a skill you can use on your own projects:

- how it works: [Announcing Greenroom](https://jesserobbins.com/blog/greenroom/)
- the skill: [github.com/jesserobbins/greenroom](https://github.com/jesserobbins/greenroom) — `npx skills add jesserobbins/greenroom`

## Versioning

Semantic versioning with prerelease identifiers (`0.1.1-alpha.1`, `0.1.1-beta.1`, …), single source of truth:

| what | where |
|---|---|
| version number | `VERSION`, mirrored in `luna_doctor/cli.py` |
| history | `CHANGELOG.md` (Keep a Changelog) |
| releases | annotated git tags, `vX.Y.Z` and `vX.Y.Z-prerelease` (e.g. `v0.1.1-alpha.1`) |
| JSON document shape | `"schema": 3` in every `--json` envelope — bumped independently when the shape changes |
| measured gateway facts | `"contract": {...}` in every `--json` envelope |

Every `--json` document carries `schema`, `version`, and `command`. The reporting commands (`discover`, `analyze`, `fix`, `probe`, `compare`) also carry `contract`, so a saved report stays readable after the tool moves on. Usage errors print to stderr and exit `2` with no JSON document.

Bumping a version updates the number in both places, opens a dated changelog section, and prints the tag and push commands. It never pushes:

```bash
scripts/release.sh patch       # 0.1.1 -> 0.1.2
scripts/release.sh 0.1.1-beta.1 # explicit
```

Tests are stdlib-only and read-only (they never call `probe`, which burns
concurrent sessions on the gateway):

```bash
python3 -m unittest discover -s tests -v
```
