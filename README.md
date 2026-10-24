# luna-doctor — `0.2.0-alpha.1` · prerelease

> **Alpha.** The CLI, JSON schema, and measured gateway behaviour may evolve without a major bump until `1.0.0`. Pin to an exact tag if you embed this in automation.

[**LunaRoute**](https://lunaroute.com) runs your coding agents on open models for a fixed monthly price — capacity is measured in concurrent requests, not tokens. This tool explains what your LunaRoute agent traffic is doing to that concurrency — and what to change.

LunaRoute bills **concurrency**, not tokens. There is no quota or token-budget dimension to it: the account admits a fixed number of concurrent requests at once, and every symptom below is that number being exceeded. The gateway waits, or demotes the request to a background tier and holds the stream open with keep-alives while producing nothing — and part of that same overflow surfaces directly as a 429 (`rate_limit_error`, `CONCURRENT_REQUEST_LIMIT_EXCEEDED`). A 429 in your logs is therefore a concurrency symptom, not a quota you can wait out. Often the harness kills a stall with an error string of its own making instead, which is why grepping logs for 429 finds only part of the picture.

`luna-doctor` reads the session logs the machine already has, reconstructs what was asked for at what tier and what came back, and tells you what to adjust. Thresholds are measured on your machine, not assumed from ours.

## Install

**Requirements:** Python 3.9+. Everything below — `discover`, `analyze`, `fix`, `chart`, and the bare default run — is offline and needs no credentials.

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
luna-doctor                  # the default run: the last hour analyzed, plus the chart
luna-doctor discover          # what log stores exist, how big, how fresh
luna-doctor analyze           # last 24h: what happened and what to adjust
luna-doctor analyze --now     # last 15 minutes (alias for --since 15m)
luna-doctor analyze --since 7d --json   # machine-readable, for cron/CI
luna-doctor analyze --style plain       # undecorated text, for a paste or a pipe
luna-doctor fix               # per-agent copy-paste config, from the evidence
luna-doctor fix --agent pi    # …or one agent at a time (pi | claude-code | codex | swamp)
luna-doctor chart --since 24h # SVG: concurrent requests over time + every request's latency
luna-doctor chart --since 24h --open  # …and open it in your browser
luna-doctor analyze --share          # WhatsApp text + PNG card, for pasting into a chat
```

`--share` renders the same report twice for a chat: a WhatsApp-ready message on
stdout — a quoted headline, at most five `*label:* value` lines, at most three
bullets, one provenance line — and a 1080x1350 PNG card beside it, with the
card's path on stderr so stdout alone is the thing to paste:

```bash
luna-doctor analyze --share                # message on stdout, card path on stderr
luna-doctor analyze --share --out card.png # …and the card written where you say
```

Exit codes: `0` no pressure, `1` pressure found, `2` no data — usable from a shell prompt, a skill, or cron.

## How it works

### One report, two renderers

`discover`, `analyze` and `capacity` all build the same `Report` — a
headline, sections of `label: value` rows, adjustments and a footnote — and then
hand it to one of two renderers. `plain` is undecorated text: no ANSI, no emoji,
and no column padding, so a value longer than its column cannot break the layout.
That is why output survives a narrow terminal, a pipe, or a chat app that wraps
at 40 characters. `pretty` draws the same rows at a terminal, with color, a rule
under each section and a moon.

`--style` is on `discover`, `analyze`, `capacity` and `chart`. `auto` — the
default — picks `pretty` when stdout is a terminal and `NO_COLOR` is unset, and
`plain` otherwise. `--style pretty` asks for decoration even down a pipe;
`NO_COLOR` still wins over it. `CLICOLOR_FORCE` colors a pipe that would otherwise
stay plain. `--json` is never styled: no `Style` is constructed on that path at
all. `chart` takes the flag for consistency, but its console output is two plain
lines — the picture is the SVG.

`--copy` is on `discover`, `analyze`, `capacity` and `fix`: it puts the
printed text on the clipboard too — via the first of `pbcopy`, `wl-copy`,
`xclip` or `xsel` found on PATH — as plain text, while stdout stays exactly as
it would have been, so a machine with no clipboard tool loses nothing (stderr
says so, never as an error). `analyze --share --copy` copies the chat message,
not the card's path.

### `analyze` — what your machine has been doing

Reconstructs every model request from the local log stores (pi, claude-code) and classifies each outcome against measured gateway behaviour — admission timeouts, silent background demotions, upstream errors, concurrency rejections that surface as 429s, and more. Thresholds (admission deadlines, idle budgets) are measured per model, not hardcoded — and the number of interactive requests the gateway admits at once is specific to the account and cannot be read from these logs. `analyze` reports what it observed for each model in the window.

### `fix` — what to change, with copy-paste you can audit

Prints one block per agent: the exact file, the snippet, and a command that backs the file up, validates it, merges the key, and prints `ok`. It never writes anything itself. Every key it emits was verified against the installed harness bundles or their shipped docs; anything unverifiable is not emitted.

### `chart` — one picture

A standalone SVG (no fonts, scripts, or CDN — opens anywhere), with a PNG written beside it under the same stem — the inline image, pasteable into a chat, a note or a doc; `--no-image` suppresses it. One stacked-area graph: a single time axis and a single stack whose height is the account's total concurrent inference requests, layered by band — one model's traffic from one agent in one scheduling class (blues for priority, violets for background), each band named for the agent it belongs to ("glm-5.3 · pi"). A grey outline carries the busiest moment per column, an open circle marks the exact peak, red flare ticks hang where the gateway rejected a request, and the account's priority concurrency is one dashed rule drawn from the capacity API's cached account value (or declared with `--plan` when the API is unreachable). The footer carries the provenance: what was measured, what was declared.

## What it reads

Read-only and offline for `discover` / `analyze` / `fix` / `chart`:

| harness | store |
|---|---|
| pi | `~/.pi/agent/sessions/**/*.jsonl` |
| claude-code | `~/.claude/projects/**/*.jsonl` |
| codex | detected and reported, not analysed (zstd rollouts, usually not LunaRoute traffic) |
| swamp | `~/.swamp` / sibling `swamp-tracker` repo — reported only while its activity falls inside the window |

Swamp is counted only when it is in use: files under `.swamp/data` or
`.swamp/workflow-runs` modified inside the window. A populated but idle swamp
store is installed, not running, and `analyze` says nothing about it — installed
is not traffic. When swamp does appear, its concurrency is described as
configured (its extract and verify settings, and where they came from), never
as measured concurrency.

`luna-doctor capacity` serves a disk cache of the account's plan and priority concurrency by default; only `capacity --online` touches the network, using the routing key already stored by `lunaroute login`.

## Limits worth knowing

- Log records capture when a request started and when its record was written. They do **not** record time to first token, so `analyze` measures end-to-end latency and classifies outcomes; the wait before headers is not in these logs.
- Error records zero their token usage, so input size is unavailable on failures.
- Concurrency reconstructed from one machine is a lower bound; other machines and other tenants are invisible.

## Repo layout

| path | what |
|---|---|
| `luna_doctor/cli.py` | the CLI, the log readers and the analyser |
| `luna_doctor/_fix_block.py` | the `fix` command, kept apart so its snippets can be read and verified on their own |
| `luna_doctor/_chart_block.py` | the `chart` command |
| `luna_doctor/report.py` | the report model and its `plain` renderer |
| `luna_doctor/render_pretty.py` | the decorated renderer over the same report |
| `luna_doctor/style.py` | the single decoration gate: color, emoji, motion |
| `luna_doctor/scene.py` | the chart's shape list, and the SVG renderer over it |
| `luna_doctor/png.py` | the raster renderer over the same scene |
| `luna_doctor/font.py` | the hand-drawn 5x7 bitmap font and its ASCII folding |
| `bin/luna-doctor` | launcher for the no-packaging path; resolves its own symlink and imports the package |

These files ship together as the package. `cli` loads `_fix_block` and
`_chart_block` off disk from its own directory, so a partial install is the one
thing that breaks `fix` and `chart`.

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
| JSON document shape | `"schema": 4` in every `--json` envelope — bumped independently when the shape changes |
| measured gateway facts | `"contract": {...}` in every `--json` envelope |

Every `--json` document carries `schema`, `version`, and `command`. The reporting commands (`discover`, `analyze`, `fix`, `capacity`) also carry `contract`, so a saved report stays readable after the tool moves on. While the project is prerelease, `schema` is bumped freely — the envelope has no stable consumers to protect yet. Usage errors print to stderr and exit `2` with no JSON document.

Bumping a version updates the number in both places, opens a dated changelog section, and prints the tag and push commands. It never pushes:

```bash
scripts/release.sh patch       # 0.1.1 -> 0.1.2
scripts/release.sh 0.1.1-beta.1 # explicit
```

Tests are stdlib-only and read-only (they never open live sessions on the
gateway):

```bash
python3 -m unittest discover -s tests -v
```
