---
name: luna-doctor
description: Diagnose LunaRoute concurrency trouble from local agent logs. Use when an agent session on LunaRoute feels stuck, slow, or silently hangs; when the user asks why requests take minutes or die with "Stream ended without finish_reason" or "scheduler admission timeout"; when deciding between a model and its -background twin; or when fanning out subagents and wondering how wide is safe.
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
luna-doctor fix                # copy-paste config per agent, justified by the numbers
luna-doctor fix --agent pi     # one agent at a time
luna-doctor chart --since 24h  # SVG: fan-out over time + every request's latency
luna-doctor discover           # when the analysis says "no data"
luna-doctor compare --n 8 --model <model>   # tier facts, costs tokens
```

`analyze` exits `1` when it finds pressure, `0` when it does not, `2` when the
logs hold nothing in the window.

## Reading the output

| outcome in the report | what it means |
|---|---|
| `admission_timeout` | died at the gateway's ~120.2s admission deadline. Concurrency was exhausted; the request was never admitted. |
| `client_idle_timeout` | stalled until the harness's ~300s idle budget killed the stream. Almost always the silent background demotion. |
| `upstream_unavailable` / `upstream_error` | 503/500/502 propagation. Not concurrency; check service health. |
| `rate_limited` | 429 `rate_limit_error` — the subscription quota. Narrowing fan-out will not fix it. |
| `connection_error` / `request_timeout` | transport-level trouble, not the tier. Retry once, then look elsewhere. |
| `aborted` | the user or a tool cancelled it. |

Two metrics carry most of the diagnosis:

- **admission conversion** — the share of interactive requests that were served
  rather than killed on admission. Low conversion means the interactive tier is
  oversubscribed.
- **starved-peer evidence** — stalled requests where another request to the same
  model completed inside their own window. When this is non-zero the model was
  healthy and the request was simply not served.

## Reading the chart

`luna-doctor chart` draws the same window as two stacked panels. Top: requests in
flight, split by tier, with the advised fan-out (8) and the burst cap (10) as
dashed rules. Bottom: every request end to end on a log axis, with the 120.2s
admission deadline and the 300s client idle budget as dashed walls — failures sit
on the wall that killed them (red crosses at 120.2s, amber dots at 300s).

Use it before quoting numbers: a burst in the top panel at the same x as a
cluster of red crosses below is the diagnosis in one picture. When the top panel
never reaches the dashed rules, say that plainly — on a quiet machine the honest
answer is that fan-out is not the problem.

## What to recommend

1. **Keep interactive fan-out at or under 8.** The interactive burst cap is 10,
   and anything past it is demoted to background, where the first token can take
   ten minutes.
2. **Move wide fan-out to the `-background` variant** of the same model. More
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
   `x-lunaroute-warning: overflowed-to-background` instead.

## Rules for the agent using this skill

- Run the tool; do not diagnose from the error string alone. `Stream ended
  without finish_reason` is a pi client string and says nothing about the gateway.
- Quote the numbers from the report rather than paraphrasing them.
- If `analyze` shows no pressure and the user is still slow, say so, and offer
  `probe`/`compare` as the next step — they cost tokens, so ask first.
