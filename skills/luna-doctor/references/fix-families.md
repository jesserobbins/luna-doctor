# Reading `fix`, and handing its findings over

`fix` gives advice, not a report. It reads what `analyze` measured. Then it joins
that reading to the harness configuration and the model catalog, and prints one
block per agent. Each entry gives the key, the value as `from -> to`, the
`file:`, the evidence, the `impact:`, and a `Tell your agent (copy-paste):`
sentence to hand over.

`fix` never writes. It edits no file, it emits no shell command, and it makes no
backup. Its own opening line says `luna-doctor does not edit files itself`. The
agent that owns the harness is the only writer. `fix` tells that agent to read
the file first and to keep other keys.

```
  models[glm-5.3].contextWindow
  400,000 (declared) -> at or below 200,000 (the model's real window)
  file: ~/.pi/agent/models-store.json

    Ask your agent: in ~/.pi/agent/models-store.json set contextWindow for
    lunaroute/glm-5.3 to at most 200,000 (was 400,000 (declared)). Restart
    the harness after the change.
```

One `Advice:` line carries the width that the advice aims at. A `verify` section
at the end names `luna-doctor analyze --baseline prev`. With nothing to change,
the output is `Nothing to change: no agent is configured in a way that can overflow
the gateway.`

## The keys, per agent

| agent | key | file |
|---|---|---|
| pi | `globalConcurrencyLimit` | `~/.pi/agent/extensions/subagent/config.json` |
| pi | `httpIdleTimeoutMs`, `subagents.defaultModel` | `~/.pi/agent/settings.json` |
| pi | `models[<id>].contextWindow`, `models[<id>].maxTokens` | `~/.pi/agent/models-store.json` |
| claude-code | `env.CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`, `env.CLAUDE_STREAM_IDLE_TIMEOUT_MS`, `env.API_TIMEOUT_MS`, `env.CLAUDE_CODE_SUBAGENT_MODEL`, `env.CLAUDE_CODE_MAX_OUTPUT_TOKENS`, `env.CLAUDE_CODE_MAX_CONTEXT_TOKENS` | `~/.claude/settings.json` |
| codex | `agents.max_concurrent_threads_per_session`, `agents.default_subagent_model` (JSON-quoted), top-level `model_context_window` | `~/.codex/config.toml` |
| swamp | `SWAMP_EXTRACT_CONCURRENCY`, `SWAMP_LLM_CONCURRENCY` | `fix` names `<repo>/.env.swamp`. The live value resolves env var, then `~/.swamp.yaml`, then that file, so read the shell environment and `~/.swamp.yaml` first |

## The width the advice aims at

Three rungs exist, and the block says which one it used. The first rung is the
account's priority concurrency from the capacity payload. The second rung is a
`--plan` declaration on `fix` itself, as a published plan name or a plain number
of concurrent requests. The third rung is a labelled starting point, 8 today,
whose instruction ends `This value is a starting point`. The `Advice:`
parenthetical is the owner: `(account)`, `(declared plan <name>)` or
`(declared)`. Quote it with the number, or you state a width that belongs to
nobody.

Quote the parenthetical, not the sentence around it. `_fix_block.py:855-858`
still ends the `Advice:` line with "the interactive width this account admits"
for every source, and a `--plan` declaration is one of those sources. When the
parenthetical says `declared`, the number is your declaration and no part of
`fix` measured it. Quote the parenthetical only. Do not repeat the sentence that
it sits in.

## The finding families

Groups run per agent: width, idle budget, tier routing, then sizing. A sizing
finding extends the agent's block after the concurrency advice, never as a new
headline.

- width: `globalConcurrencyLimit`, `env.CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`,
  `agents.max_concurrent_threads_per_session`, and the swamp split, which appears
  only when swamp is in use in the window.
- idle budget: `httpIdleTimeoutMs`, and `env.CLAUDE_STREAM_IDLE_TIMEOUT_MS` with
  `env.API_TIMEOUT_MS`, raised to 900000 only when too short.
- tier routing: `subagents.defaultModel`, `env.CLAUDE_CODE_SUBAGENT_MODEL`,
  `agents.default_subagent_model`. These keys name the `-background` twin of the
  routed model.
- sizing: the six rules below, on the model that the routing keys resolve to.
  Each rule compares a declared value with the catalog value for that same id.

| rule id | fires on |
|---|---|
| `sizing-unset-underuses-model` | a pi sizing key unset while the catalog value is at least twice pi's shipped default (128000 window, 16384 output) |
| `sizing-exceeds-model-window` | pi's effective `contextWindow` (declared, else the shipped default) above the catalog `context_window` |
| `sizing-output-exceeds-model-max` | pi's effective `maxTokens` above the catalog `max_output_tokens`. On claude-code only the declared value counts, and even then it is informational |
| `sizing-input-plus-output-over-window` | pi's effective `maxTokens` at or above its effective `contextWindow`: one window holds both budgets, so the request is over-window before it is built |
| `codex-sizing-window` | codex's declared `model_context_window` above the catalog window |
| `claude-sizing-window` | `CLAUDE_CODE_MAX_CONTEXT_TOKENS`: one informational line, never a finding |

## V and U: two kinds of row

A V-backed row rests on a verified fact: pi's shipped defaults, cited at three
call sites in the installed bundle, or a value that the file declares. Its status
is `finding`, it is actionable, and `from != to`. A U-backed row is an
observation that nobody verified, such as claude-code's shipped defaults or the
semantics of `CLAUDE_CODE_MAX_CONTEXT_TOKENS`. Its status is `informational` and
it renders `from == to`: the key `env.CLAUDE_CODE_MAX_CONTEXT_TOKENS`,
`unset -> unset`, and a why line that calls it `unverified (U) - informational only, never a finding`.

A U row cannot move the exit code, because a change with `from == to` is not
actionable. `httpIdleTimeoutMs: 0` follows the same precedent: `0 -> 0` with
`No change needed`, so the verdict stays audible. Quote a U line as recorded,
never as a finding or a reason for a change.

## Unchecked is not clean

Clean means judged against the catalog and found fine: a declared value equal to
the catalog value and one below it are both clean. Unchecked means the comparison
was not possible. No snapshot, no catalog, a stale catalog, or a routed model
that the catalog does not serve each leave it unchecked. A bad declared value, an
unparsable store, or a missing field does the same.

`fix` prints exactly one unchecked line, and only when no catalog was established
at all. That line carries the key `model sizing (catalog join)`, the value
`unchecked -> unchecked`, and the file `the LunaRoute model catalog (GET /v1/models)`.
Its why line reads `sizing is unchecked, never clean: <reason>`, and it carries an
instruction to fetch the catalog once with `--online`. Other unchecked rows stay
in `analyze`'s sizing ledger. A stale cache is the awkward case. Every sizing
rule is unchecked there too, and no sizing line prints.

The cache is `~/.luna-doctor/catalog.json`, valid 24 hours. `--offline` is the
default and never touches the network. `--online` is the one flag that refreshes
it: one authenticated `GET /v1/models`, then the checks run offline. A failed
fetch is never an error: `fix` prints `catalog unavailable (...)` on stderr.

A routed model that the catalog does not serve belongs to `model-not-lunaroute`,
not to sizing. Sizing marks its rows `model not in catalog`, unchecked, never
clean.

## What the exit code means

- `2` - usage: an unknown `--agent`, or a bad `--since`.
- `0` - no change moves a value: nothing to change, or only recorded lines.
- `1` - at least one change has `from != to`. That is the whole test.

When every finding is configuration-only, `fix` exits `1`. That `1` means "there
is a change to make", not "pressure was found" (`analyze` is that command). A
window with no measured request still exits `1` for a wide default that it
audited.

Read `from` and `to` before you relay a width row. An unset key is emitted as a
change against the shipped default (`_fix_block.py:599`, `:684`). So on an
account wider than that default, the block can ask for a value that the reader
already fits. If the change narrows nothing, say what the file holds and say
that.

## How this playbook ends

1. The finding. The rule id or the key, and the file that it lives in, exactly as
   the block printed them.
2. What it means for this machine. The numbers that make it bite: this machine's
   measured peak, the account's number with its owner, the catalog's value. Say
   which is measured, which is the account's, which is declared.
3. The exact fix block to hand over. From `luna-doctor fix --agent <agent>` take
   the key, the value, the `Tell your agent (copy-paste)` sentence, then restart
   and run `analyze --baseline prev`.
   [after-the-fix.md](playbooks/after-the-fix.md) says what success looks like.
