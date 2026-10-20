# luna-doctor

Explain what your local agent traffic is doing to your LunaRoute concurrency, and
what to adjust.

LunaRoute bills **concurrency**, not tokens. That is a different paradigm from
every other provider a user has met, and the failure it produces is quiet: no
429, no error, just an agent that appears to hang. `luna-doctor` reads the
session logs the machine already has, reconstructs what was asked for, at what
tier, and what came back, then says what to change.

## The measured contract

These numbers come from live probes against the gateway, not from inference.
See `../luna-doctor-private/research/` for the run data.

| behaviour | value |
|---|---|
| wait before SSE headers | ~25.2s, fixed, load-independent |
| time to first token, interactive | 59s unloaded, ~92s at burst 10, ~100s median |
| unadmitted request dies at | ~120.2s, `scheduler admission timeout` |
| overflow past a burst of 10 | HTTP 200, demoted to background tier |
| demotion marker | `x-lunaroute-service-class/-tier: background`, `x-lunaroute-warning: overflowed-to-background` |
| background tier, time to first token | ~633s observed |
| background stream while waiting | SSE keep-alive every ~10s, no content |
| pi client idle budget | 300s, surfaces as `Stream ended without finish_reason` |
| `x-lunaroute-queue-ms` | always `0` — do not trust it |

The consequence: an over-full interactive burst does not fail, it decays into
silence, and the harness eventually kills it with an error string of its own
making. That is why grepping logs for 429 finds nothing.

## Commands

```bash
luna-doctor discover          # which log stores exist, how big, how fresh
luna-doctor analyze           # last 24h: what happened and what to adjust
luna-doctor analyze --now     # last 15 minutes
luna-doctor analyze --since 7d --json
luna-doctor fix               # per-agent copy-paste config, from the evidence
luna-doctor fix --agent pi    # ... or one agent at a time
luna-doctor chart --since 24h # SVG: fan-out over time + every request's latency
luna-doctor probe --n 10 --model glm-5.3
luna-doctor compare --n 10 --model glm-5.3   # model vs its -background twin
```

Exit codes: `0` no pressure, `1` pressure found, `2` no data. That makes
`analyze` usable from a shell prompt, a skill, or cron.

## The chart

```bash
luna-doctor chart --since 24h --open     # writes luna-doctor-<ts>.svg
luna-doctor chart --now                  # the last 15 minutes
luna-doctor chart --out day.svg --width 1400 --height 800
```

One self-contained SVG, no fonts or scripts or CDN, so it opens anywhere and
pastes into a doc or a ticket.

The top panel is **requests in flight over time**, split into interactive and
background. The line is the busiest moment in each bucket, the fill is the
average load, and the two dashed rules are the advised fan-out (8) and the
burst cap (10) — past the cap the gateway demotes you to background instead of
failing. If your line never reaches those rules, the chart says so, because
that is the finding.

The bottom panel is **every request end to end** on a log axis. The 120.2s
admission deadline and the 300s client idle budget are drawn as dashed walls,
and every failure is plotted on the wall that killed it: admission deaths as
red crosses at 120.2s, silent stalls as amber dots at 300s. The dark line is
the per-bucket median latency, broken where nothing ran.

What it is for: the two panels put cause above symptom. A burst in the top
panel at the same x as a cluster of red crosses below is the whole diagnosis in
one picture.

## What it reads

Read-only, offline, no credentials needed for `analyze`:

| harness | store |
|---|---|
| pi | `~/.pi/agent/sessions/**/*.jsonl` |
| claude-code | `~/.claude/projects/**/*.jsonl` |
| codex | detected and reported, not analysed (zstd rollouts, usually not LunaRoute traffic) |

`fix` prints per-agent blocks: the exact file, the snippet, and a command that
backs the file up, validates it, merges the key, and prints `ok`. It never writes
anything itself. Every key it emits was verified against the installed harness
bundles or their shipped docs; anything unverifiable is not emitted.

`probe` and `compare` are the only commands that touch the network, and they use
the routing key already stored by `lunaroute login`.

## Limits you should know

- Logs record when a request started and when its record was written. They do
  **not** record time to first token, so `analyze` measures end-to-end latency
  and classifies outcomes; only `probe` can see the wait itself.
- Error records zero their token usage, so input size is unavailable on failures.
- Concurrency reconstructed from one machine is a lower bound; other machines
  and other tenants are invisible.

## Install

```bash
# no packaging step needed
ln -s "$PWD/bin/luna-doctor" ~/.local/bin/luna-doctor

# or as a proper script install
pipx install .        # uses pyproject.toml; installs the script, no library
```

Python 3.9+, standard library only. The `fix` block for codex prefers Python
3.11+ (it validates TOML with `tomllib`) and falls back to a key-presence check
when `tomllib` is unavailable.

## Versioning

Semantic versioning, single source of truth:

| what | where |
|---|---|
| version number | `VERSION`, mirrored in `bin/luna-doctor` |
| history | `CHANGELOG.md` (Keep a Changelog) |
| releases | annotated git tags, `vX.Y.Z` |
| JSON document shape | `"schema": 1` in every `--json` envelope |
| measured gateway facts | `"contract": {...}` in every `--json` envelope |

Every `--json` document carries `schema`, `version` and `command`. The
reporting commands (`discover`, `analyze`, `fix`, `probe`, `compare`) also carry
`contract`, the measured gateway facts they were judged against, so a saved
report stays readable after the tool moves on. A usage error emits no document
at all: it prints to stderr and exits 2.

Bumping a version updates the number in both places, opens a dated changelog
section, and prints the tag and push commands. It never pushes:

```bash
scripts/release.sh patch    # or minor | major | x.y.z
```

Tests are stdlib-only and read-only (they never call `probe`, which costs credits):

```bash
python3 -m unittest discover -s tests -v
```
