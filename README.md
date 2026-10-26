# luna-doctor

> **Alpha.** The CLI, JSON schema, and measured gateway behaviour may evolve without a major bump until `1.0.0`. Pin to an exact tag if you embed this in automation.

[**LunaRoute**](https://lunaroute.com) runs your coding agents on open models for a fixed monthly price — capacity is measured in concurrent requests, not tokens. This tool explains what your LunaRoute agent traffic is doing to that concurrency — and what to change.

LunaRoute bills **concurrency**, not tokens. There is no quota or token-budget dimension to it: the account admits a fixed number of concurrent requests at once, and every symptom below is that number being exceeded. The gateway waits, or demotes the request to a background tier and holds the stream open with keep-alives while producing nothing — and part of that same overflow surfaces directly as a 429 (`rate_limit_error`, `CONCURRENT_REQUEST_LIMIT_EXCEEDED`). A 429 in your logs is therefore a concurrency symptom, not a quota you can wait out. Often the harness kills a stall with an error string of its own making instead, which is why grepping logs for 429 finds only part of the picture.

`luna-doctor` reads the session logs the machine already has, reconstructs what was asked for at what tier and what came back, and tells you what to adjust. Thresholds are measured on your machine, not assumed from ours.

## Install

**Requirements:** Python 3.9+. Everything below — `discover`, `analyze`, `fix`, `chart`, and the bare default run — is offline and needs no credentials; `capacity` reads a disk cache the same way. The only fetches in the tool are opt-in and use the routing key `lunaroute login` already stored: `capacity --online`, and `analyze --online` / `fix --online` for the model catalog.

```bash
git clone https://github.com/jesserobbins/luna-doctor.git
cd luna-doctor
pipx install .        # installs the `luna-doctor` command
luna-doctor --version # prints the version in VERSION (0.2.0-alpha.3)
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
luna-doctor analyze --plan <plan name or number>   # declare the account's width
luna-doctor analyze --baseline prev     # …this window against the one before it
luna-doctor analyze --style plain       # undecorated text, for a paste or a pipe
luna-doctor explain           # one request: what it waited for, and whose room it was
luna-doctor explain --blocked # the newest request the gateway refused
luna-doctor explain --slowest --last 5  # the five longest by duration
luna-doctor watch             # during an incident: one line per state change, summary on Ctrl-C
luna-doctor analyze --for-issue  # one redacted, datable block for a support ticket
luna-doctor capacity          # the account's plan and priority concurrency (cached)
luna-doctor fix               # per-agent copy-paste config, from the evidence
luna-doctor fix --agent pi    # …or one agent at a time (pi | claude-code | codex | swamp)
luna-doctor chart --since 24h # SVG: one stacked-area graph of concurrent requests
luna-doctor chart --since 24h --open  # …and open it in your browser
```

Exit codes: `0` no pressure, `1` pressure found, `2` no data — usable from a shell prompt, a skill, or cron. `explain` follows the same codes for the one request it explains: `1` when that request was refused or stalled, `0` when it was served.

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
all. `chart` takes the flag for consistency, but its console output is four plain
lines — the two paths, the counts behind the picture and which source the drawn
rule came from — and the picture is the SVG.

`--copy` is on `discover`, `analyze`, `capacity` and `fix`: it puts the
printed text on the clipboard too — via the first of `pbcopy`, `wl-copy`,
`xclip` or `xsel` found on PATH — as plain text, while stdout stays exactly as
it would have been, so a machine with no clipboard tool loses nothing (stderr
says so, never as an error).

### The attribution line

The default run and `analyze` open with one line. That line answers a question
that an error string cannot answer. Ask: which wall did this traffic hit? The
wall is this machine, the account, or the gateway. The verdict word is one of
`you`, `your org`, `the gateway`, `not concurrency`, or `unknown`.

The line leads the report. Every later row is judged against the wall that the
line names. A report misleads when it confuses one wall with another. If a
gateway fault looks like your own traffic, you lower a setting that was never
the problem.

### `analyze` — what your machine has been doing

Reconstructs every model request from the pi and claude-code log stores and
classifies each outcome against measured gateway behaviour — admission timeouts,
silent background demotions, upstream errors, concurrency rejections that surface
as 429s, and more. Thresholds (admission deadlines, idle budgets) are measured
per model, not hardcoded: the report carries each model's observed deadline with
its sample count rather than lending one model's number to another.

The combined snapshot (`metrics.snapshot` in the `--json` envelope) reads one
store further: codex rollouts are parsed into requests and counted as measured,
per model and per harness, and codex's declared config keys join the catalog
beside their siblings — but codex requests do not enter the outcome classes
above, which stay gated to the harnesses `--harness` names (pi and claude-code
by default). Token totals stay per harness and are never merged: codex's
`input_tokens` includes its cached tokens while pi's `input` excludes
`cacheRead`, so one merged "input tokens" number would be a silent wrong answer.

`--plan <plan name or number>` declares the account's priority concurrency for
that run. It is a declaration and never a measurement, and it is used only when
the account's own capacity payload carries no number: when the account does
report one, the declaration is set aside and the report says so. Account values
are labelled `(account)`, plan-derived ones `(declared plan <name>)`, and a value
neither source supplies is `unknown` with the command that fetches the account
half (`luna-doctor capacity`).

`fix` takes the same `--plan` declaration, so the advice and the analysis are
judged against the same number.

### `--baseline prev`: this window against the one before it

`analyze --baseline prev` compares the window with the window before it. The
second window has the same length and comes from the same logs. The report
prints a `change:` section:

```
blocked at admission: 12 -> 3
```

If either window holds fewer requests than the sample floor, the verdict is
`inconclusive`. A direction that rests on a few requests is a coincidence with
a number attached.

The section never claims a cause. It places two windows of the same logs side
by side. The report does not say that your change made the difference.

### `fix` — what to change, with copy-paste you can audit

Prints one block per agent: the exact file, the snippet, and a command that backs
the file up, validates it, merges the key, and prints `ok`. It never writes
anything itself. Every key it emits was verified against the installed harness
bundles or their shipped docs; anything unverifiable is not emitted.

The `fix` output ends with a check. Restart the harness. Then run `analyze
--baseline prev` and read the `change:` section. Success looks like a lower
blocked-at-admission count over a window that is wide enough to clear the
sample floor.

### `explain`: why was that one request slow

`analyze` describes the window. `explain` describes one request in that window.
The report gives the start and the end of the request, the length of the wait,
and the result. It gives the number of requests that this machine had in flight
when the request started, and the peak during the request. It also says whether
the same model served another request inside that span.

The headline names the owner of the wait. The owner is this machine, the shared
pool of the account, the gateway, or no wall at all. Each number carries a label
that gives its owner.

If you give no flag, `explain` selects the request that went wrong, which is a
refused request or a stalled request. `--blocked` selects the newest refused
request. `--slowest` selects by duration only. A bare id selects one request by
response id, session id, or run id.

### `watch`: the incident, live

`watch` reads the last 15 minutes on a fixed interval. It prints one line for
each change of state. The line gives the pressure classes that moved, the peak
that this machine held, and the occupancy of the account when a capacity sample
covers that moment.

On a terminal, `watch` redraws a frame. Through a pipe, it adds ASCII lines and
never sends cursor control codes. `Ctrl-C` prints a summary of three lines and
exits with the verdict of the last sample. `--count N` and `--once` stop without
a key press. `--json` prints one object for each sample, for a script.

### `analyze --for-issue`: a block that you can send to someone

`--for-issue` replaces the report with one block. The block is safe to paste.
It gives the tool version and the log schema, the window in UTC, and the stores
that the run read. It gives the attribution verdict, the headline, and the rows
with their labels.

The tool redacts home paths, email addresses, and long opaque tokens before it
writes the block. You can put the block in a support ticket or an issue, and the
block does not carry the layout of your machine.

### `chart` — one picture

A standalone SVG (no embedded or remote fonts — the text names a system font
stack — no scripts, no CDN; opens anywhere), with a PNG written beside it under
the same stem — the inline image, pasteable into a chat, a note or a doc;
`--no-image` suppresses it. One stacked-area graph: a single time axis and a
single stack whose height is the account's total concurrent inference requests,
layered by band — one model's traffic from one agent in one scheduling class
(blues for priority, violets for background), each band named for the agent it
belongs to ("glm-5.3 · pi") whenever more than one agent has traffic. A grey
outline carries the busiest moment per column, an open circle marks the exact
peak, red flare ticks hang where the gateway rejected a request, and the
account's priority concurrency is one dashed rule drawn from the capacity API's
cached account value (or declared with `--plan` when the account has no number),
labelled with its source so a fetched value can never be mistaken for a declared
one. The footer carries the provenance: what was measured, what was declared.

The picture deliberately carries no universal admission deadline (it is per
model), no advisory concurrency figure (advice that depends on the plan belongs
to `analyze`) and no latency picture (latency lives in `analyze`).

## What it reads

Read-only, and offline by default for `discover` / `analyze` / `fix` / `chart`:

| harness | store |
|---|---|
| pi | `~/.pi/agent/sessions/**/*.jsonl` |
| claude-code | `~/.claude/projects/**/*.jsonl` |
| codex | `~/.codex/sessions/**/*.jsonl` — parsed into requests and counted as measured per model in the combined snapshot (compressed `*.jsonl.zst` rollouts are skipped: the Python 3.9 stdlib has no zstd), but not classified into the outcome classes above |
| swamp | `~/.swamp` / sibling `swamp-tracker` repo — reported only while its activity falls inside the window |

Swamp is counted only when it is in use: files under `.swamp/data` or
`.swamp/workflow-runs` modified inside the window. A populated but idle swamp
store is installed, not running, and `analyze` says nothing about it — installed
is not traffic. When swamp does appear, its concurrency is described as
configured (its extract and verify settings, and where they came from), never
as measured concurrency.

`luna-doctor capacity` serves a disk cache of the account's plan and priority concurrency by default; a cached payload, a stale one included, is served and the network stays untouched. Two flags can fetch, each a single authenticated GET: `capacity --online` refreshes the capacity cache (5 minutes), and `analyze --online` / `fix --online` refresh the 24-hour model catalog at `~/.luna-doctor/catalog.json`. Both use the routing key already stored by `lunaroute login`, and anything that cannot be fetched is reported unknown — never as an error.

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

Every `--json` document carries `schema`, `version`, and `command`. The reporting commands (`discover`, `analyze`, `fix`, `chart`, `capacity`) also carry `contract`, so a saved report stays readable after the tool moves on. While the project is prerelease, `schema` is bumped freely — the envelope has no stable consumers to protect yet. Usage errors print to stderr and exit `2` with no JSON document.

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
