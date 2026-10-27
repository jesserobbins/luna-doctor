---
name: luna-doctor
description: Use when a LunaRoute session feels stuck, slow or silently hangs. Also use when the user holds a "Stream ended without finish_reason" or a "scheduler admission timeout", when a request came back 429 rate_limit_error, when choosing between a model and its -background twin, or asking how many subagents to run at once or how many requests in parallel is safe, when a 400 names maximum context length, maxTokens or contextWindow, when one bad request needs explaining, when you want to make sure that a configuration change worked, or when a redacted block is needed for a support thread.
---

# luna-doctor

LunaRoute bills concurrent sessions, not tokens. When there is no room, the gateway does not
error. It waits, or it demotes the request to the `-background` tier. In the demoted case it
holds the connection with keep-alives and produces nothing. The harness then kills the stall
with an error of its own making. That is why the logs read as inexplicable. The width past
which that starts belongs to the account. It is not in these logs.

## Invariants

- Run the tool before theorising.
- Quote numbers verbatim from the report. Do not round, re-derive or paraphrase them.
- Every number carries its owner: this machine (measured), the account, or a declared plan.
- Measured and configured never sum, and are never compared as a sum.
- Never repeat a plan width as a universal fact: fetch it (`capacity`) or declare it (`--plan`).
- Never diagnose from the error string. `Stream ended without finish_reason` is a client string.
- If there is no pressure, say so.
- Never run `load-test` or `compare`. They open real concurrent sessions.

## Symptom -> Playbook

| what the user said, or what you are looking at | open |
|---|---|
| "it is stuck", "this is slow", a session that went quiet mid-answer, a call that has taken minutes, `Stream ended without finish_reason` | [playbooks/slow-right-now.md](references/playbooks/slow-right-now.md) |
| one request in hand: "this call took 95 seconds", this one came back 429, here is the response id | [playbooks/one-bad-request.md](references/playbooks/one-bad-request.md) |
| too many subagents at once, how many requests in parallel is safe, `scheduler admission timeout`, 429 `rate_limit_error`, a model versus its `-background` twin | [playbooks/too-wide.md](references/playbooks/too-wide.md) |
| a configuration key was just changed: "did the fix work", a `change verdict:` line | [playbooks/after-the-fix.md](references/playbooks/after-the-fix.md) |
| a redacted block to paste into a support thread or an issue, "what do I send" | [playbooks/share-evidence.md](references/playbooks/share-evidence.md) |
| an outcome id in the report, a verdict word, admission conversion, starved peers | [outcomes.md](references/outcomes.md) |
| "why 300 seconds", "why 120 seconds", the pre-header wait, the per-model admission deadline, response headers, the body ceiling | [gateway-contract.md](references/gateway-contract.md) |
| the `fix` block: a key with `from -> to`, a 400 about maximum context length or `maxTokens` versus `contextWindow`, a V or U row, `unchecked` | [fix-families.md](references/fix-families.md) |
| the chart: bands, the grey outline, a flare tick, the dashed rule, the footer rows | [chart-anatomy.md](references/chart-anatomy.md) |

## Command Index

| command | flags an agent types, and what it can touch |
|---|---|
| `luna-doctor` | bare run: account line, `analyze --since 1h`, the chart into cwd. offline, but it writes a run extraction under `~/.luna-doctor/store`. Say so, and use `analyze --no-store` when nothing can persist |
| `luna-doctor discover` | `--json --copy --style`. read-only, offline |
| `luna-doctor explain [ID]` | `--since --now --last N --slowest --blocked --harness --plan --json --copy --style`. read-only, offline |
| `luna-doctor watch` | `--since --now --interval --count --once --json --style`. read-only, offline |
| `luna-doctor analyze` | `--since --now --harness --pi-root --claude-root --plan --baseline prev --for-issue --json --copy --style --online\|--offline`. offline unless `--online` |
| `luna-doctor fix` | `--since --now --harness --agent --plan --json --copy --online\|--offline`. prints advice, never edits a file |
| `luna-doctor chart` | `--since --now --harness --out --no-image --width --height --open --plan --json --style`. writes the SVG and PNG, offline |
| `luna-doctor capacity` | `--json --copy --style --online\|--offline`. offline unless `--online` |

Exit codes, once: `0` no pressure, `1` pressure found, `2` no data (usage errors are also `2`).
That is the contract for `analyze`, `explain` and `watch`. `fix` exits `1` when a change
exists, measured window or not: [fix-families.md](references/fix-families.md) says how to
read that. `load-test` and `compare` exit `1` when a burst did not stream cleanly.
`--offline` is the default for `analyze`, `fix` and `capacity`. `--online` is the only flag that
touches the network. `load-test` and `compare` open real concurrent sessions and refuse to run
without `--load-test-enable`: they are a gate, never a step in a diagnosis.

## Rendering

`--style auto` is the default: `pretty` on a terminal, `plain` elsewhere. `NO_COLOR` beats an
explicit `--style pretty`. `CLICOLOR_FORCE` colors a pipe. `--style plain` prints the bare
`label: value` rows, and that is what you paste to a user. `--json` is never styled and never
redacted. `--copy` mirrors the printed text. `fix` takes no `--style`, and `chart` takes no
`--copy`.

## Rules for the Agent

- Open the file the table names before you answer. This file routes. It does not restate them.
- Read `the wall you hit` first and quote the verdict word whole: `you (this machine)`,
  `your org (the account)`, `the gateway`, `not concurrency`, or `unknown`.
- Answer with four things, in this order: the window, the verdict word with its `why` headline,
  the one change, and the verification step.
- The change is the report's, not yours: `fix` gives the key, the file and the value, and you
  make the edit and restart the harness.
- When the report shows no pressure, say: "No concurrency pressure in this window. That is not
  the wall you hit", and name what the report did measure.
