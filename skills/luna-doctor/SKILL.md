---
name: luna-doctor
description: Diagnose LunaRoute concurrency trouble from local agent logs. Use when an agent session on LunaRoute feels stuck, slow, or silently hangs; when the user asks why requests take minutes or die with "Stream ended without finish_reason" or "scheduler admission timeout"; when deciding between a model and its -background twin; or when many subagents are running at once and the user wants to know whether the wall they hit was this machine, the account, or the gateway.
---

# luna-doctor

LunaRoute bills by **concurrency**, not tokens. When you exceed it, the gateway
does not return an error. It waits, or it demotes your request to the
`background` tier and holds the connection open with SSE keep-alives while
producing nothing. The harness eventually kills the stall with an error of its
own making, which is why the logs look inexplicable.

The width past which that happens belongs to the account, not to the gateway.
These logs cannot see it; `luna-doctor capacity` fetches it, and `--plan`
declares it when the account's own number is unavailable.

Run the tool before theorising:

```bash
luna-doctor                        # the default run: account line, last hour, the chart
luna-doctor analyze --now          # if the user says "it is slow right now"
luna-doctor analyze                # what the machine has been doing for 24h
luna-doctor analyze --since 7d --json
luna-doctor analyze --plan <plan name or number>   # declare the account's width
luna-doctor analyze --baseline prev               # this window vs the one before it
luna-doctor analyze --style plain  # undecorated text, for pasting to the user
luna-doctor analyze --copy         # same text, also on the clipboard, plain
luna-doctor explain                # one request: what it waited for and whose room it was
luna-doctor explain --blocked      # the most recent request the gateway refused
luna-doctor explain --slowest --last 5   # the five longest, by duration
luna-doctor explain <id>           # a response id, session id or run id
luna-doctor watch                  # during an incident: one line per state change
luna-doctor analyze --for-issue    # one redacted, datable block for a ticket
luna-doctor capacity               # the account's plan and priority concurrency
luna-doctor fix                    # copy-paste config per agent, justified by the numbers
luna-doctor fix --agent pi         # one agent at a time
luna-doctor chart --since 24h      # SVG + PNG: one stacked-area concurrency graph
luna-doctor discover               # when the analysis says "no data"
```

`analyze` exits `1` when it finds pressure, `0` when it does not, `2` when the
logs hold nothing in the window.

## One request: `explain`

`analyze` answers what the window did. When the user is holding one bad
request - "this call took 95 seconds", "this one came back 429" - `explain`
answers for that request alone: when it ran, how long it waited, what it got,
how many requests were in flight from this machine when it started and at its
peak, and whether the same model served anyone else inside its own span. Its
headline names the owner of the wait: this machine's own concurrent requests, the account's
shared pool, the gateway, or nothing at all.

Selection matters when you quote the result:

- no flag gives the request that went wrong, which is a refused request or a
  stalled request, with the longest wait first. It uses duration only when no
  request went wrong.
- `--blocked` gives the newest request that the gateway refused. A refusal came
  from the gateway at admission or at the limit of the account.
- A stall is a different event. In a stall, the harness stopped the stream. The
  two events have different fixes. Do not describe one as the other.
- `--slowest` sorts by duration only.

`explain` exits `1` if the request that it explained was refused or stalled. It
exits `0` if the request was served, and `2` if the window holds no request to
explain.

## Watching an incident, and sending the evidence to someone

`watch` reads a short window on a fixed interval and prints one line for each
change of state. The window is 15 minutes by default. The line gives the
pressure classes that moved, the peak that this machine held, and the occupancy
of the account when a capacity sample covers that moment.

On a terminal, `watch` redraws a frame. Through a pipe, it adds ASCII lines and
never sends cursor control codes, because those codes fill a log file with
escape sequences. `Ctrl-C` prints a summary of three lines and exits with the
verdict of the last sample. `--count N` stops after N samples. `--once` prints
one sample. `watch` reads logs only and sends nothing anywhere.

`analyze --for-issue` replaces the report with one block that you can paste. The
block gives the tool version, the window in UTC, the stores that the run read,
the attribution verdict, the headline, and the rows with their labels. The tool
redacts home paths, email addresses, and long opaque tokens before it writes the
block. Use this block when the user sends the evidence to LunaRoute support, to
a GitHub issue, or to a colleague.

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

## The attribution line

The default run and `analyze` open with one line that answers the question the
error string cannot: the wall you hit — was it **this machine**, **the account**,
or **the gateway**? Its verdict word is exactly one of:

| verdict word | the wall it names |
|---|---|
| `you` | this machine |
| `your org` | the account |
| `the gateway` | the gateway |
| `not concurrency` | no concurrency wall was found in the window |
| `unknown` | the evidence did not settle it |

Quote that word. Do not upgrade `unknown` into a diagnosis, and do not turn
`your org` into a statement about the user's machine or the reverse.

## Reading the output

| outcome in the report | what it means |
|---|---|
| `admission_timeout` | blocked at the gateway's admission deadline. Concurrency was exhausted; the request was never admitted. The deadline is **per model**: `analyze` reports each model's observed one with its sample count, and `unknown` below three usable observations — never assume another model's number. |
| `client_idle_timeout` | stalled until the harness's own idle budget (~300s on pi) killed the stream. Often the silent background demotion: the served tier is not in these logs, so the report calls this a signature, not a finding. |
| `upstream_unavailable` / `upstream_error` | 503/500/502 propagation. Not concurrency; check service health. |
| `rate_limited` | 429 `rate_limit_error`. The account's concurrent-request capacity was exceeded — there is no separate quota dimension behind this error. Moving work to the `-background` tier and staying inside the account's priority concurrency is exactly the fix. |
| `connection_error` / `request_timeout` | transport-level trouble, not the tier. Retry once, then look elsewhere. |
| `aborted` | the user or a tool cancelled it. |

Two metrics carry most of the diagnosis:

- **admission conversion** — the share of interactive requests that were served
  rather than blocked at admission. Low conversion means the interactive tier is
  oversubscribed.
- **starved-peer evidence** — stalled requests where another request to the same
  model completed inside their own window. When this is non-zero the model was
  healthy and the request was simply not served.

## Whose number is it

Every capacity number in a report is one of two things, and the report says
which:

- an **account** value, labelled `(account)` — fetched from
  `api.lunaroute.com` by `luna-doctor capacity` and served to everything else
  from its disk cache;
- a **plan-derived** value, labelled `(declared plan <name>)` — your
  `--plan` declaration, used only when the account's payload carries no
  number. When the account does report one, the declaration is set aside and
  the report says so: the account is the first authority.

The chart labels the same two sources in its own form — the dashed rule reads
`(account, plan <name>)` or `(--plan <name> = <n>)` — and draws no rule at all
when neither exists.

A value that is neither is `unknown`, printed with the command that fetches the
account half (`luna-doctor capacity`). Unknown is never zero and never a
default: do not fill it in from memory, from another account, or from a plan
table.

## Reading the chart

`luna-doctor chart` draws the window as **one stacked-area graph**: one time
axis, one vertical scale, one stack. The stack's height at any moment is the
account's total concurrent inference requests — everything in flight at once,
which is what the gateway prices, and what the priority-concurrency limit
compares against.

- **Bands.** One band is one model's traffic from one agent in one scheduling
  class. Blues are priority scheduling, violets are background scheduling (the
  `-background` model-id form). The agent's name joins the label
  (`glm-5.3 · pi`) whenever more than one agent has traffic in the window.
  Every priority band sits below every background band.
- **Grey outline.** The busiest moment in each time column — the envelope of
  the account total, which per-column averages cannot show.
- **Open circle.** The exact peak, and when it happened.
- **Red flare ticks.** One per request the gateway rejected at admission or
  refused at the concurrency limit, hung from the top of the plot at the moment
  it died. The hover title names the class, the agent, the model and how long
  it waited.
- **Dashed rule.** The account's priority concurrency, drawn only when the
  number is established, and labelled with its source so a fetched value can
  never be mistaken for a declared one: `priority concurrency <n> (account,
  plan <name>)`, or `priority concurrency (--plan <name> = <n>)` when you
  declared it and the account had no number. No rule is drawn when nothing is
  established — the footer then says the number is specific to the account and
  cannot be read from these logs, and points at `capacity` and `--plan`.
- **Direct labels, no legend.** Each band is named at its right-hand edge, in
  its own ink, at the height it left the stack.
- **Footer.** Provenance: window, version, each model's measured admission
  deadline with its sample count, and which numbers are declared rather than
  measured. It wraps, and the canvas grows downward rather than cutting it.

Three marks the picture deliberately does not draw: a universal admission
deadline (the deadline is per model), an advisory concurrency figure (advice
that depends on the plan belongs to `analyze`, not to a ruler on this graph),
and a latency picture (latency lives in `analyze`).

Read the footer's `reading:` row before interpreting anything: it says whether
the peak stayed within the account's priority concurrency or exceeded it, and
how many requests waited and were rejected. Beyond that, a burst where bands
pile up and flare ticks cluster at the same x is the diagnosis in one picture.
When the stack never approaches the dashed rule, say plainly that concurrency
is not the problem — on a quiet machine, that is the honest answer.

## Comparing with the previous window

`analyze --baseline prev` compares the window with the immediately preceding
equal-length window from the same logs, and prints a `change:` section of the
form:

```
blocked at admission: 12 -> 3
```

Below its sample floor the verdict is `inconclusive`: say so, and do not read a
direction into a change built on too few requests. Nothing in that section
implies causation — it is two windows of the same logs, side by side. If you
want to explain the change, go and find the evidence that explains it.

## What to recommend

1. **Quote the account's number, not a number from memory.** `luna-doctor
   capacity` reports the account's plan, its priority concurrency and its live
   occupancy (served from a 5-minute disk cache; `capacity --online` is the one
   command that refreshes it, using the routing key `lunaroute login` already
   stored). When it has no number, `--plan <plan name or number>` declares one
   for that run, labelled `(declared plan <name>)` — a declaration, never a
   measurement. Never repeat 8, or any other plan's width, as a universal
   gateway fact.
2. **Move anything wider to the `-background` variant** of the same model. More
   concurrency, much slower SLA. That is the trade, and it is deliberate.
   `luna-doctor fix` emits the exact key for whichever agent the user runs.
3. **Never put a short client timeout in front of a background request.** pi's
   default idle budget is 300s (set `httpIdleTimeoutMs` to `0` or `900000`);
   claude-code's stream idle floor is 300s (raise
   `CLAUDE_STREAM_IDLE_TIMEOUT_MS` and `API_TIMEOUT_MS` in the `env` block).
   A background first token has been observed at ~633s, which is why `fix`
   offers 900000ms as the raised budget.
4. **Expect a fixed wait before headers.** The published contract's
   `pre_header_wait_s` is 25.2s, load-independent. These logs do not record time
   to first token, so `analyze` measures end-to-end latency only: the
   pre-header wait is a documented constant, not something this machine
   measured.
5. **Verify the change.** `fix` ends with a verification step: restart the
   harness, then run `analyze --baseline prev`. What success looks like is a
   `change:` section in which `blocked at admission` came down, over a window
   wide enough to clear the sample floor.

## Swamp

Swamp appears only when it is in use — files under `.swamp/data` or
`.swamp/workflow-runs` written inside the window. Installed but idle is not
traffic, and the report says nothing about it. When swamp does appear, its
configured concurrency is the setting to quote (extract + verify, plus where the
setting came from) — a number to reason with, not a measurement. Only the peak
concurrency row is measured. Keep the two separate when you quote them, and
never add a configured setting to a measured peak.

## Rules for the agent using this skill

- Run the tool; do not diagnose from the error string alone. `Stream ended
  without finish_reason` is a pi client string and says nothing about the gateway.
- **Say whose number you are quoting.** Every number is either this machine's
  (a measured peak, a measured deadline) or the account's (an allowance the
  report labels `(account)` or `(declared plan <name>)`). Never present a
  machine peak as the account's allowance, or the account's allowance as
  something this machine measured. Measured and configured never sum.
- **Never repeat 8, or any other plan's width, as a universal fact.** A plan's
  width is one account's number. If the user needs one, fetch the account's
  with `luna-doctor capacity`, or declare it with `--plan` and say that it is a
  declaration.
- Quote the numbers from the report rather than paraphrasing them.
- If `analyze` shows no pressure and the user is still slow, say so.
