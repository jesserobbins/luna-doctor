# When many subagents run at once

You launched a wide burst of subagents, or you are deciding how many requests
in parallel is safe. Or one came back 429 `rate_limit_error`, or the gateway
refused one outright. Run the tool before you reason about it:

```bash
luna-doctor analyze --now           # what this machine is doing right now
luna-doctor analyze --since 24h     # the window that produced the burst
luna-doctor capacity --online       # the account's own number
luna-doctor explain --blocked       # the newest request the gateway refused
```

## Quote the verdict word, exactly

Read `the wall you hit` first. Its word is one of five. Each word names an
owner, and each owner has a different fix:

- `you (this machine)` - this machine's peak on the priority tier reached the
  width, as the `why` headline says. The `peak concurrency` rows cover both
  tiers, so never quote one of those as the number behind this verdict. Narrow
  the burst, or move the wide part to the `-background` variant.
- `your org (the account)` - this machine stayed inside the width and the
  refusals or stalls are still there. The shared pool was full and other
  sessions held it: another machine, another agent, another person on the same
  account. Nothing you change here buys room.
- `the gateway` - upstream failures with no admission refusal and no occupancy
  evidence. Retry, and look at service health.
- `not concurrency` - nothing was refused, stalled or failed inside the width.
- `unknown` - the evidence did not settle it, because one half of the picture is
  missing.

Quote the word you were given. `unknown` is an answer, not a gap to fill in: its
headline names the command that fetches the missing half, so you run that
command rather than upgrading the word into a diagnosis. Never turn `your org`
into a claim about this machine, or the reverse.

The account's half arrives as rows labelled (account): `account occupancy`,
`account sample time`, `account admission log`. A row labelled (this machine)
is a lower bound rebuilt from local logs. Never add the two halves, and never
add a measured peak to a value from the configuration: that sum measures nothing.

When the verdict is `your org` and the account's own occupancy looks quiet, give
the reading a date. Today the account half can come from a daily total that
overlaps the window rather than from occupancy at the moment of the request
(`attribution.py:532-534` treats any window of 12 hours or more as day-scale).
Quote the verdict, then the sample time in the same breath.

## The deadline is a measurement, per model

A request refused at admission waited for room until the gateway's park deadline
expired. That deadline belongs to the model, not to the tool and not to the
gateway as a whole. `analyze` prints what this machine observed:

```
admission wall: glm-5.3 120.2s (n=12), deepseek-4.1-flash 60.3s (n=9)
```

The `n` is the sample count and it is part of the number. Below three usable
observations the row says `unknown` with the counts it did see, as in
`unknown (3 blocked at admission, 2 usable)`. A rejection near 60 s on a second
model is unsettled, because 60 s is also how long a priority request can park
for room. These logs cannot tell an admission deadline from an expired park, so
say that rather than quoting the figure as a deadline. The attribution section
carries the same measurement as `park deadline observed`. Never assume another
model's number.

## A 429 here is the classifier's name for one state

`rate_limited` is the report's name for a bare 429 `rate_limit_error`, and the
classifier assigns it by matching the response text. On this gateway that text
means the account's concurrent-request capacity was full: no second dimension
sits behind it. `org_concurrency_limit` says the same out loud, with
`CONCURRENT_REQUEST_LIMIT_EXCEEDED` in the response. Both make the fix a width
question, never a waiting game and never a client timeout. Quote it as the
report's classification rather than as a fact read off the string.

## The width, in the order you can use it

1. The account. `luna-doctor capacity --online` fetches the plan, the
   priority concurrency and the live occupancy, labelled `(account)` in every
   report. When the account reports a number, that is the number.
2. A declaration. `--plan <name or number>` on `analyze`, `explain`,
   `watch`, `fix` or `chart` declares the width for that run, labelled
   `(declared plan <name>)` or `(declared by --plan)`. A declaration, never a
   measurement, and used only when the account reports nothing.
3. A labelled starting point. With neither, the width is `unknown`, and
   `fix` says so under its number. This value is a starting point, so fetch
   the account's number and use that instead.

Never fill the gap from memory or from another account. A width stated as a
gateway fact is the error this ladder exists to prevent.

## The fix for width: the `-background` variant

Send the wide part to the `-background` variant of the same model, for example
`glm-5.3-background` rather than `glm-5.3`. It parks for room instead of
competing for the priority pool, so traffic wider than the account's priority
concurrency is the right traffic for it. `luna-doctor fix` emits the model id
and the idle budget for the agent the user runs.

The trade, stated: the background tier buys room by giving up the priority
queue, so it answers in minutes, not seconds. On this machine's captures a
background first token arrived at around 633 s, which is why `fix` offers
900000 ms as the raised idle budget. Raise the harness budget with it - pi's
`httpIdleTimeoutMs`,
claude-code's `CLAUDE_STREAM_IDLE_TIMEOUT_MS` and `API_TIMEOUT_MS` - and never
put a short client timeout in front of a background request. On an account with
no background entitlement, `-background` requests are downgraded into the
priority pool and still count against the same headroom.

This tool does not open requests of its own. It measures what a machine did, so it
cannot create the pressure it is asked to explain. If someone offers you a load test as
the next step, that is a different tool and a different decision: it spends real
concurrent sessions on the account.

## When the symptom is a 400, not a wait

A 400 about context length is not this playbook, and neither is a 413 for a
payload that was too large. Those are sizing symptoms: `context_length_exceeded`
and the `maxTokens`/`contextWindow` keys are in [fix-families.md](../fix-families.md).

## How this playbook ends

Four statements, and each one carries its source:

1. Whose wall. The verdict word, quoted exactly as the report printed it.
2. The width that applies. A number with its owner: `(account)`,
   `(declared plan <name>)`, or `unknown` plus the command that fetches it.
3. The exact change. From `luna-doctor fix --agent <agent>`: the key, the
   file and the number. Or the move to the `-background` variant, with the idle
   budget raised to match it.
4. The verification step. Restart the harness, let a window wide enough to
   clear the sample floor pass, then `analyze --baseline prev`. Success is a
   `change:` section where `blocked at admission` and the concurrency
   rejections came down. Below the floor the word is `inconclusive`, so say so.
