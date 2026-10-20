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
luna-doctor probe --n 10 --model glm-5.3
luna-doctor compare --n 10 --model glm-5.3   # model vs its -background twin
```

Exit codes: `0` no pressure, `1` pressure found, `2` no data. That makes
`analyze` usable from a shell prompt, a skill, or cron.

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
ln -s "$PWD/bin/luna-doctor" ~/.local/bin/luna-doctor
```

Python 3.9+, standard library only.
