---
name: luna-doctor
description: Diagnose LunaRoute concurrency trouble from local agent logs. Use when an agent session on LunaRoute feels stuck, slow, or silently hangs; when the user asks why requests take minutes or die with "Stream ended without finish_reason" or "scheduler admission timeout"; when deciding between a model and its -background twin; or when running many subagents at once and wondering how many concurrent requests are safe.
---

# luna-doctor

LunaRoute bills by **concurrency**, not tokens. When you exceed it, the gateway
does not return an error. It waits, or it demotes your request to the
`background` tier and holds the connection open with SSE keep-alives while
producing nothing. The harness eventually kills the stall with an error of its
own making, which is why the logs look inexplicable.

Run the tool before theorising:

```bash
luna-doctor analyze --now      # if the user says "it is slow right now"
luna-doctor analyze            # what the machine has been doing for 24h
luna-doctor analyze --since 7d --json
luna-doctor analyze --style plain   # undecorated text, for pasting to the user
luna-doctor analyze --copy          # same text, also on the clipboard, plain
luna-doctor fix                # copy-paste config per agent, justified by the numbers
luna-doctor fix --agent pi     # one agent at a time
luna-doctor chart --since 24h  # SVG + PNG: requests over time + every request's latency
luna-doctor discover           # when the analysis says "no data"
```

`analyze` exits `1` when it finds pressure, `0` when it does not, `2` when the
logs hold nothing in the window.

## Rendering

Every report — `discover`, `analyze`, `capacity` — is one model rendered
two ways. Rows are `label: value` with no column alignment, which is why output
survives a narrow terminal, a pipe, or a chat window that wraps at 40 characters.
`--style plain` prints exactly those rows with no color and no emoji; `pretty`
adds color, a rule per section and a moon.

`--style auto` (the default) picks `pretty` on a terminal that is not `NO_COLOR`,
and `plain` otherwise. `NO_COLOR` beats an explicit `--style pretty`;
`CLICOLOR_FORCE` colors a pipe. `--json` is never styled. When you paste a report
to the user, use `--style plain`.

`--copy` (on `discover`, `analyze`, `capacity` and `fix`) also puts the
printed text on the clipboard, as plain text even under `--style pretty`; the
same text still prints to stdout, so nothing is lost when the machine has no
clipboard tool. `--json` is copied exactly.

## Reading the output

| outcome in the report | what it means |
|---|---|
| `admission_timeout` | blocked at the gateway's ~120.2s admission deadline. Concurrency was exhausted; the request was never admitted. |
| `client_idle_timeout` | stalled until the harness's ~300s idle budget killed the stream. Almost always the silent background demotion. |
| `upstream_unavailable` / `upstream_error` | 503/500/502 propagation. Not concurrency; check service health. |
| `rate_limited` | 429 `rate_limit_error`. The account's concurrent-request capacity was exceeded — there is no separate quota dimension behind this error. Keeping concurrent requests within your priority concurrency allowance and moving work to the `-background` tier is exactly the fix. |
| `connection_error` / `request_timeout` | transport-level trouble, not the tier. Retry once, then look elsewhere. |
| `aborted` | the user or a tool cancelled it. |

Two metrics carry most of the diagnosis:

- **admission conversion** — the share of interactive requests that were served
  rather than blocked at admission. Low conversion means the interactive tier is
  oversubscribed.
- **starved-peer evidence** — stalled requests where another request to the same
  model completed inside their own window. When this is non-zero the model was
  healthy and the request was simply not served.

## Reading the chart

`luna-doctor chart` draws the same window as two stacked panels. Top: requests in
flight, split by tier, with the advised concurrency (8) and the gateway's rough
admission level as dashed rules. Bottom: every request end to end on a log
axis, with the 120.2s
admission deadline and the 300s client idle budget as dashed walls — failures sit
on the wall that stopped them (red crosses at 120.2s, amber dots at 300s).

Use it before quoting numbers: a burst in the top panel at the same x as a
cluster of red crosses below is the diagnosis in one picture. When the top panel
never reaches the dashed rules, say that plainly — on a quiet machine the honest
answer is that concurrency is not the problem.

## What to recommend

1. **Keep concurrent requests within your priority concurrency allowance.** The
   admitted width is specific to the account and cannot be read from these logs;
   `luna-doctor capacity` reports the account's own number, and `--plan` declares
   one when the account cannot be reached. Anything past that width is demoted to
   background, where the answer can take ten minutes to start. Never state a
   width as a gateway fact, and never quote one plan's default as if it were
   yours.
2. **Move anything wider to the `-background` variant** of the same model. More
   concurrency, much slower SLA. That is the trade, and it is deliberate.
   `luna-doctor fix` emits the exact key for whichever agent the user runs.
3. **Never put a short client timeout in front of a background request.** pi's
   default idle budget is 300s (set `httpIdleTimeoutMs` to `0` or `900000`);
   claude-code's stream idle floor is 300s (raise
   `CLAUDE_STREAM_IDLE_TIMEOUT_MS` in the `env` block). A background stream may
   not speak for 600s.
4. **Expect a minute of latency as the floor.** ~25.2s is spent before the
   gateway even sends headers.
5. **Do not trust `x-lunaroute-queue-ms`.** It reads `0` on requests that waited
   ten minutes. Use `x-lunaroute-service-class` and
   `x-lunaroute-warning: overflowed-to-background` instead. A demotion has a
   reason: `overflow` (the interactive width spilled it) or `no_entitlement` (a
   `-background` request downgraded into the priority pool). Say which one the
   report names; if the served tier is unknown, say so.

## Swamp

Swamp appears only when it is in use — files under `.swamp/data` or
`.swamp/workflow-runs` written inside the window. Installed but idle is not
traffic, and the report says nothing about it. When swamp does appear, its
configured concurrency is the setting to quote (extract + verify, plus where the
setting came from) — a number to reason with, not a measurement. Only the peak
concurrency row is measured. Keep the two separate when you quote them.

## Rules for the agent using this skill

- Run the tool; do not diagnose from the error string alone. `Stream ended
  without finish_reason` is a pi client string and says nothing about the gateway.
- Quote the numbers from the report rather than paraphrasing them.
- If `analyze` shows no pressure and the user is still slow, say so.
