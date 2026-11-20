# luna-doctor

> Alpha. Until `1.0.0`, the CLI, the JSON schema and measured gateway behaviour can change without a major bump. If you embed this tool in automation, pin to an exact tag.

[LunaRoute](https://lunaroute.com) runs your coding agents on open models. LunaRoute measures capacity in concurrent requests, not tokens. This tool explains what your LunaRoute agent traffic does to that concurrency, and what to change.

LunaRoute bills concurrent requests, not tokens. Other quotas exist on the same key, and none of them counts concurrency: a monthly BYOK spend cap in USD, a sponsored monthly web-search allowance, and System One metering on input tokens. The account admits a priority number of concurrent requests at once, and that number is fixed. Overflow above it depends on capacity that is free at that moment, so it is not a second guaranteed allowance. Most symptoms below are the priority number being exceeded.

When the account is full, the gateway waits. It can also demote the request to a background tier, and then hold the stream open with keep-alives while it produces nothing. Part of that same overflow also arrives directly as a 429 (`rate_limit_error`, `CONCURRENT_REQUEST_LIMIT_EXCEEDED`). A 429 in your logs usually means the account's concurrent capacity ran out. It is not the only cause. The gateway's own error table lists 429s that come from capacity further down the stack, not from the account's priority concurrency: `embedding_gateway_busy` and `embedding_backend_busy` from an embedding gateway or backend under load, `systemone_backend_busy` from a System One upstream that is rate-limiting, and an audio pool with no room left. That table also lists the BYOK spend cap. A spend cap resets on the first day of each month, so that one is a quota that you can wait out. Often the harness kills a stall with an error string of its own making instead. That is why a grep of the logs for 429 finds only part of the picture.

`luna-doctor` reads the session logs that the machine already has. It reconstructs what was asked for at what tier, and what came back. Then it tells you what to adjust. Thresholds are measured on your machine, not assumed from ours.

## Install

Requirements: Python 3.9+, and a routing key that `lunaroute login` already stored.

The account comes first: `analyze`, `fix`, `chart` and the bare run ask LunaRoute for the account's capacity, because its numbers are what the report explains. The disk cache serves first and the network is touched only past its 5-minute TTL, so a warm run answers at once and a cold one waits once. That is one authenticated GET to `api.lunaroute.com`. `--offline` on any of those commands serves the cache and touches nothing. `discover` and `explain` never fetch, and `capacity` fetches only with `--online`. Nothing else in the tool leaves the machine.

```bash
pipx install luna-doctor   # installs the `luna-doctor` command
luna-doctor --version      # prints the installed version
```

Every release is an alpha. pipx installs it, because the index holds no final
release.

The wheel carries the agent skill, so the install is not a bare command:

```bash
luna-doctor skill          # prints the packaged path and the copy line per harness
```

To run from a checkout instead:

```bash
git clone https://github.com/jesserobbins/luna-doctor.git
cd luna-doctor
pipx install .
```

Give the install to your agent instead: [`docs/install-with-an-agent.md`](https://github.com/jesserobbins/luna-doctor/blob/main/docs/install-with-an-agent.md)
holds one block to paste, which installs the tool, reads the account's capacity
and applies the settings that `fix` names.

Or without packaging, put it on your `PATH`:

```bash
ln -s "$PWD/bin/luna-doctor" ~/.local/bin/luna-doctor
luna-doctor --version
```

## First run

`luna-doctor` with no arguments, in a terminal, opens the triage screen. The
screen is a dashboard. It reads the last hour of local logs and the cached
account capacity, then draws nineteen rows. The rows above the middle group hold
their place in every state: only the numbers, the moons and that group change.

| region | rows | the question it answers |
|---|---|---|
| status | 2 | over capacity now, and is it getting worse |
| load | 6 | by how much, and since when |
| one evidence group | 5 or 6 | what kind of trouble |
| next | 3 | what to do |

The latency group draws six rows - two percentiles for each of the two tiers -
and the other groups draw five.

The window is one hour. `w` cycles 1h, 6h and 24h, and every number and the
picture follow the window. The dashboard holds no 7-day and no 30-day number.
Those live in `capacity` and in `analyze`.

One picture carries the shape of the window: this machine's in-flight
concurrency, one cell per minute. A `█` cell is a minute that reached the
account's priority concurrency count. The caption under the picture gives the
minutes over, out of the minutes in the window, and the clock time of the peak.
A window with nothing measured draws no picture, and the caption says so.

Every number carries a delta against the previous window of the same length.
`▲ 5` is five more, `▾ 9` is nine fewer, `▬` is unchanged, and `·` means the
window has no comparable predecessor.

`NEXT` holds the one sentence on the screen. It names the move, and the row
under it holds the command that makes the move. `f` shows the `fix` output in
place.

The screen asks one question, "What are you seeing?", and offers six answers.
The numbers are on screen while the question is open, and an answer swaps the
middle group. `g` steps through the same four groups, so the rows under the
picture can change without an answer:

| answer | the group it picks |
|---|---|
| 429 or concurrency errors | `REFUSALS`: the 429s, the refusals at the account's limit, and the requests sent to the background tier |
| things are slow | `LATENCY`: a p50 and a p90 for each tier, priority and background |
| nothing yet, just checking | `LATENCY`: the group a quiet window opens on |
| requests failing or hanging, "Stream ended without finish_reason", an agent stuck mid-task | `STALLS`: the stalled streams and the slowest request |
| `g` only | `MODELS`: one row per model, its p50 and its own delta, up to three models by sample count |

The two latency tiers are two measurements, so they are two pairs of rows: a
priority p50 and a background p50 are never averaged into one number. A tier that
measured nothing prints `?`, which is what a background tier nobody used looks
like. Every row of the `MODELS` group is one model's own percentile, with its own
delta against the same model in the previous window. A model below the sample
floor has no row at all.

The last row is the `Footer`, and it lists the keys: `f` for the `fix` output,
`d` for the `analyze` report, `c` for the chart, `g` for the next evidence
group, `w` for the window, `?` for the help overlay, and `q` to quit.

The gateway's fixed wait before headers is a constant, so it is not a row: the
`analyze` adjustment states it beside this window's own p50, and
[`gateway-contract.md`](skills/luna-doctor/references/gateway-contract.md) holds
the measurement.

The first row dates itself. The cell on its right holds the age of the data and
counts up while the screen is open: `age 2m`, and `age 16m stale` once the data
is older than the freshness window. The `now` row under it holds both pools the
account reports. The number column is the priority pool, and the clause beside it
is the background pool, so `now ● 7/12 · background 0/12 · sampled 15:51-08:00`
reads as one sample of two pools at one local time. A pool the account closes
prints `closed` instead of a ratio, and a cap the account did not report prints
`cap unknown`, because a closed pool and an unread cap are not the same fact.

`?` opens the help overlay. It names every key, spells out the badges and the
moons, names the command behind each drill-down key, and shows the account the
numbers belong to: the plan, the priority and background concurrency, the
overflow routing, and the age of the capacity cache. The same key, or `esc`, closes it and gives back the
screen it covered.

The screen reads the capacity cache. As on every command that explains the
account's numbers, a fresh cache is served without network, and a stale one is
refreshed with one GET past the 5-minute TTL. `--offline` forbids that fetch,
and the screen then reads whatever the cache holds, marked stale.

The screen ships with the install. Textual, its only dependency, is a default
dependency, so a plain install gives a terminal the screen. A pipe keeps the
linear default run: the account line, the last hour analyzed, and the chart
beside it. The `tui` extra survives as a compatibility alias for the same pin,
so an install command that names it still works.

```bash
pipx install luna-doctor          # the install includes the triage screen
pipx install 'luna-doctor[tui]'   # the same install: the extra is an alias
luna-doctor                       # a terminal: the triage screen
echo | luna-doctor                # a pipe: the linear default run
```

### What the badges mean

Every number on the dashboard carries a badge, and one key line names all
three:

```
● authoritative (the account reports it) · ○ measured here, a lower bound · ◌ declared, not measured
```

`●` is a number the account reported. `○` is a number that this machine
measured from its own logs, so it is a lower bound, because other machines are
invisible. `◌` is a number that a person or a shipped default stated, and no
measurement stands behind it. A `?` badge with a short reason means unknown,
and an unknown value is never a zero.

A second key line names the four moons. The moon on the first row is the
conclusion, and a moon on a region title says which region the conclusion came
from:

| moon | word | what earns it |
|---|---|---|
| `🌕` | `OK` | nothing blocked and nothing over the account's count |
| `🌗` | `PRESSURE` | a 429, a refusal at the account's limit, a peak over the count, or evidence too stale to rule pressure out |
| `🌑` | `OVER CAPACITY` | work that never ran: at least one admission block, or at least one 429 or at-limit refusal on the group's own rows |
| `🌙` | `NOT ESTABLISHED` | nothing measured in the window |

A region title reads its own three rows. The `REFUSALS` title turns dark as soon
as one 429 or at-limit refusal is counted, because those requests never ran,
while the first row keeps the CLI's word for the whole window. A request sent to
the background tier ran, so a demotion on its own leaves the title clean.

A terminal that refuses emoji draws `(O)`, `(o)` and `()` in its place.

### Where `--plan` goes

`--plan` belongs to the command that carries it. On the bare run it is the front
door's own declaration:

```bash
luna-doctor --plan max        # the triage screen, or the linear default run
```

On a subcommand, put `--plan` after the subcommand. The front door's `--plan`
does not reach a subcommand's analysis:

```bash
luna-doctor analyze --plan max       # the report is judged against it
luna-doctor chart --plan max         # the rule comes from it
luna-doctor --plan max analyze       # the analysis reads no declaration
```

## Quick start

```bash
luna-doctor                  # a terminal: the triage screen. A pipe: the default run
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
luna-doctor latency            # latency per model, harness or outcome: a table
luna-doctor fix               # per-agent copy-paste config, from the evidence
luna-doctor fix --agent pi    # …or one agent at a time (pi | claude-code | codex | swamp)
luna-doctor chart --since 24h # SVG: one stacked-area graph of concurrent requests
luna-doctor chart --since 24h --open  # …and open it in your browser
```

Exit codes: `0` no pressure, `1` pressure found, `2` no data. These codes are usable from a shell prompt, a skill, or cron. `explain` follows the same codes for the one request it explains: `1` when that request was refused or stalled, `0` when it was served.

## How it works

### One report, two renderers

`discover`, `analyze` and `capacity` all build the same `Report`: a headline,
sections of `label: value` rows, adjustments and a footnote. The report goes to
one of two renderers. `plain` is undecorated text: no ANSI, no emoji, and no
column padding. A value longer than its column therefore cannot break the
layout. That is why output survives a narrow terminal, a pipe, or a chat app
that wraps at 40 characters. `pretty` draws the same rows at a terminal, with
color, a rule under each section and a moon.

`--style` is on `discover`, `analyze`, `capacity` and `chart`. `auto` is the
default. It picks `pretty` when stdout is a terminal and `NO_COLOR` is unset, and
`plain` otherwise. `--style pretty` asks for decoration even down a pipe.
`NO_COLOR` still wins over it. `CLICOLOR_FORCE` colors a pipe that otherwise
stays plain. `--json` is never styled: no `Style` is constructed on that path at
all. `chart` takes the flag for consistency, but its console output is four plain
lines. Those lines give the two paths, the counts behind the picture and the
source of the drawn rule. The picture is the SVG.

`--copy` is on `discover`, `analyze`, `capacity` and `fix`. It puts the printed
text on the clipboard too, as plain text. The tool uses the first of `pbcopy`,
`wl-copy`, `xclip` or `xsel` that it finds on PATH. Stdout does not change, so a
machine with no clipboard tool loses nothing. stderr says so, and never as an
error.

### The attribution line

The default run and `analyze` open with one line. That line answers a question
that an error string cannot answer. Ask: which wall did this traffic hit? The
wall is this machine, the account, or the gateway. The verdict word is one of
`you`, `your org`, `the gateway`, `not concurrency`, or `unknown`.

The line leads the report. Every later row is judged against the wall that the
line names. A report misleads when it confuses one wall with another. If a
gateway fault looks like your own traffic, you lower a setting that was never
the problem.

### `analyze`: what your machine was doing

The command reconstructs every model request from the pi and claude-code log
stores. It classifies each outcome against measured gateway behaviour:
admission timeouts, silent background demotions, upstream errors, concurrency
rejections that surface as 429s, and more. Thresholds (admission deadlines,
idle budgets) are measured per model, not hardcoded. The report carries each
model's observed deadline with its sample count. It does not lend one model's
number to another.

The combined snapshot (`metrics.snapshot` in the `--json` envelope) reads one
store further. codex rollouts are parsed into requests and counted as measured,
per model and per harness. codex's declared configuration keys join the catalog
beside their siblings. But codex requests do not enter the outcome classes
above, which stay gated to the harnesses that `--harness` names (pi and
claude-code by default). Token totals stay per harness and the tool never merges
them. codex's `input_tokens` includes its cached tokens, while pi's `input`
excludes `cacheRead`. One merged "input tokens" number is therefore a silent
wrong answer.

`--plan <plan name or number>` declares the account's priority concurrency for
that run. It is a declaration and never a measurement. The tool uses it only
when the account's own capacity payload carries no number. If the account does
report one, the declaration is set aside and the report says so. Account values
are labelled `(account)`, and plan-derived ones `(declared plan <name>)`. A value
that neither source supplies is `unknown`, with the command that fetches the
account half (`luna-doctor capacity`).

`fix` takes the same `--plan` declaration, so the tool judges the advice and the
analysis against the same number.

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

### Subagents: how many ran at once

pi writes each async subagent's transcript beside the session that launched it,
at `<store>/<session>/subagent-artifacts/<runId>_<agent>_transcript.jsonl`. No
record in those files carries `type`, so the request parser reads none of them.
They are not gateway traffic, and they answer a different question: how many
subagents this machine ran at the same time.

The report gains one row, and only when subagent runs fall inside the window:

```
subagents: 113 run(s) · peak 12 in flight · at 2026-11-01T21:53:53Z · widest launch 8 from <session> · 4 exited non-zero
```

`peak` is the largest number of runs open at the same instant, from a sweep over
each run's first and last record. `widest launch` is the same peak inside one
parent session, which is the number behind "is it safe to run eight subagents at
once". A run is counted once even when it starts before the window and ends
inside it.

The row is a count of runs and it never joins a request number: eight subagents
each issue their own requests, and those requests appear in the traffic section
because the logs recorded them. `metrics.subagents` in the `--json` envelope
carries `runs`, `peakInFlight`, `peakAt`, `byAgent`, `byParent` and `failed`.

### Latency, per category

`analyze` carries three bounded lines: the p90 per tier, the slowest model cell
above the sample floor, and how many cells the floor withheld. The table lives in
its own command, because a matrix does not belong in a report:

```bash
luna-doctor latency                    # per model, last 24h
luna-doctor latency --by harness       # or outcome
luna-doctor latency --since 7d --tier background
```

```
cell                                             n      p50      p90      max
background / deepseek-4.1-flash-background    1028      7.6     23.0    264.3
interactive / deepseek-4.1-flash               841      7.3     24.1    166.6
```

Every number is measured on this machine: the account reports no request
latency, no first token and no percentile, so nothing here is quoted from it. A
cell under the floor is counted and left out, never printed, and `--json`
carries the same cells for a script.

### `fix`: what to change, with copy-paste you can audit

Prints one block per agent: the exact file, the snippet, and a command that backs
the file up, makes sure that the file is still valid, merges the key, and prints
`ok`. It never writes anything itself. The tool made sure that every key it
emits is in the installed harness bundles or their shipped docs. It does not
emit a key that it cannot make sure of.

The `fix` output ends with a way to make sure that the change worked. Restart
the harness. Then run `analyze --baseline prev` and read the `change:` section.
Success looks like a lower blocked-at-admission count over a window wide enough
to clear the sample floor.

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

### `chart`: three panels, one time axis

The picture is built to answer one question per panel, and each panel carries
its own unit:

| panel | what it shows | the question |
|---|---|---|
| top | what happened to each request, counted per column: served, demoted, refused at admission, refused at the account's limit | did the account refuse me, and when |
| middle | request duration as a heatmap: log-scale rows, time columns at the measurement resolution, colour as the count per cell, with p50 and p90 traced over it and a completion bar beneath | when did it go wrong, and in what shape |
| bottom | concurrent requests in flight, split by model and scheduling class, with the priority-concurrency rule | how far over the width did this machine go |

A percentile line alone cannot separate "everything got three times slower"
from "one percent started taking twenty minutes". Those are different faults
with different fixes. The heatmap separates them: a vertical stripe is a change at
one moment, a horizontal band is several modes of behaviour, a diagonal drift is
a slow shift. The completion bar is not decoration, because a heatmap with no
sample count invites reading noise as structure.

Only completed requests enter the duration distribution. A refusal that fails in
200 ms pulls the percentiles down, an admission timeout that fails after 120 s
pulls them up, and neither is a statement about how slow the served traffic was.

The account's own counters are day-granular, so the picture does not draw them
at all: a daily count on an hourly axis asserts a step function the data does not
contain. `capacity` and `analyze` report them as sentences.

![Seven days of concurrent requests: the stack peaks above the dashed
priority-concurrency rule, and red flare ticks mark requests the gateway
rejected](https://raw.githubusercontent.com/jesserobbins/luna-doctor/main/assets/chart-7d.png)

Seven days of generated example traffic. The stack is the concurrent requests
in flight, split by model and by scheduling class. The dashed rule is a declared
width, not a measured one. The open circle is the peak. Each red tick on the top
edge is a request that the gateway rejected. The thin stretches are the night.

![One day of the same example: a burst crosses the rule and five requests were
refused](https://raw.githubusercontent.com/jesserobbins/luna-doctor/main/assets/chart-24h.png)

The same store, for the last day only. A burst crosses the declared width, and
five requests were refused at admission. A day with a busy afternoon and a quiet
night is what the tool usually has to describe.

Both pictures come from a synthetic log store, not from an account and not from
a real session. They carry a declared width, so no plan and no account number
appears in them. Run `python3 scripts/make_demo_charts.py` to write them again.
The script generates the store from an hour-of-week rhythm, a mix of models and
tiers, and a small share of refusals, then points the chart at it and leaves the
capacity cache empty, so no account value can reach the picture.

A standalone SVG opens anywhere. It has no embedded or remote fonts, so the text
names a system font stack. It has no scripts and no CDN. A PNG is written
beside it under the same stem, and that PNG is the inline image, pasteable into
a chat, a note or a doc. `--no-image` suppresses it. The graph is one
stacked-area graph: a single time axis and a single stack. The height of the
stack is the account's total concurrent inference requests. The stack is
layered by band. A band is one model's traffic from one agent in one scheduling
class (blues for priority, violets for background). A band is named for the
agent that it belongs to ("glm-5.3 · pi") whenever more than one agent has
traffic. A grey outline carries the busiest moment per column. An open circle
marks the exact peak. Red flare ticks hang where the gateway rejected a request.
The account's priority concurrency is one rule, drawn from the capacity
API's cached account value, or declared with `--plan` when the account has no
number. The mark shows the provenance: a number from the account is a thick
solid line, and a declared number is a thin dashed one. The rule is labelled
with its source as well, so a fetched value can never be mistaken for a declared
one. The picture dates itself in the header, at the right of the title. Two lines
under it carry the rule's provenance and the one reading that matters, and no
more.

The picture deliberately carries no universal admission deadline, because the
deadline is per model. It carries no advisory concurrency figure, because advice
that depends on the plan belongs to `analyze`. Every window draws the same page,
so the admission panel, the latency field and its completion bars sit above the
stack at any width. The story of one request lives in `explain`.

## Use it as an agent skill

The tool explains one incident. The skill is what an agent reads, so that the
agent reaches for the right command before it theorises.

`skills/luna-doctor/SKILL.md` is a router: the invariants that must not be
broken, a table that turns a symptom into the file that answers it, the command
index with the flags an agent actually types, and the exit codes. The nine files
under `skills/luna-doctor/references/` hold the depth. An agent loads one at a
time:

| file | what it answers |
|---|---|
| `playbooks/slow-right-now.md` | the incident loop, and how far back to widen |
| `playbooks/one-bad-request.md` | `explain`: which request, and whose wait |
| `playbooks/too-wide.md` | many requests at once, and whose wall it was |
| `playbooks/after-the-fix.md` | did the change work |
| `playbooks/share-evidence.md` | the block to paste, and `watch` live |
| `outcomes.md` | every outcome id, verdict word and metric |
| `gateway-contract.md` | the measured constants, and what these logs cannot see |
| `fix-families.md` | how to read `fix`, and the sizing family |
| `chart-anatomy.md` | reading the picture above |

Every playbook ends in a verdict, so an agent that loads one cannot finish
without saying whose wall it was, which number belongs to whom, and what to
change. The catalogues are there because the same question keeps arriving in
different words: which outcome id, whose capacity number, which header lies.

Add it to your agent:

```bash
npx skills add jesserobbins/luna-doctor
```

The skill is only as good as its agreement with the CLI, so that agreement is a
test rather than a review note: `tests/test_skill_package.py` parses every
subcommand's `--help` and fails if any skill file names a flag the parser does
not carry, links a path that resolves nowhere, names a command that does not
exist, or pushes the router past its line and word budget.

## What it reads

Read-only, and cached-first for `analyze` / `fix` / `chart`, which make one authenticated GET past the cache TTL unless `--offline` forbids it. `discover` and `explain` never fetch:

| harness | store |
|---|---|
| pi | `~/.pi/agent/sessions/**/*.jsonl` |
| claude-code | `~/.claude/projects/**/*.jsonl` |
| codex | `~/.codex/sessions/**/*.jsonl`: parsed into requests and counted as measured per model in the combined snapshot (compressed `*.jsonl.zst` rollouts are skipped because the Python 3.9 stdlib has no zstd), but not classified into the outcome classes above |
| swamp | only a repository you name: `--swamp-root DIR` reads that repo's `.swamp/run_tracker.db`, and swamp is reported while a run overlapped the window |
| agentsview | the local `agentsview` command, when it is installed: `discover` reports how many projects and sessions it holds, through one `agentsview projects --json` query. It is a second view of session history, never a log store, and the reports still read the harness logs. No daemon, no serve, no sync |

Swamp is opt-in. Swamp keeps run history per repository, in
`<repo>/.swamp/run_tracker.db`, and nothing on the machine lists the repositories
a person uses swamp in: `~/.swamp` holds the toolchain, and
`~/.config/swamp/workspaces.json` is a list you fill in. So the tool never looks
for a swamp repo. Name one with `--swamp-root DIR` and that repository's own
tracker decides: a run that started before the window closed and finished after
it opened was in flight during the window. A store full of old runs is
installed, not running, and the report says nothing about it. Installed is not
traffic. The database is opened read-only, and swamp is never spawned.

When swamp does appear, the report describes its concurrency as configured (its
extract and verify configuration, and where they came from). It never describes
that concurrency as measured.

`luna-doctor capacity` serves a disk cache of the account's plan, priority concurrency and overflow capacity by default. The report keeps the two pools apart. The priority pool is the account's main width. The overflow pool is where overflow traffic runs, and a request that cannot get overflow waits for a priority slot. Each pool has its own occupancy and its own 7-day and 30-day rollups. The report never adds one pool to the other. It serves a cached payload even when that payload is stale, and the network stays untouched. Two flags can fetch, and each one is a single authenticated GET: `capacity --online` refreshes the capacity cache, and `analyze --online` / `fix --online` refresh the 24-hour model catalog at `~/.luna-doctor/catalog.json`. Both use the routing key that `lunaroute login` already stored. The disk cache for capacity expires after 5 minutes (`sources.CAPACITY_TTL_S`), and that TTL is this tool's own. The server's history cache also refreshes about every 5 minutes, and its `now` and `admission` blocks are recomputed on every call. Anything that cannot be fetched is reported as unknown, and never as an error.

## Limits worth knowing

- Log records capture when a request started and when its record was written. They do not record time to first token. So `analyze` measures end-to-end latency and classifies outcomes. The wait before headers is not in these logs.
- Error records zero their token usage, so input size is unavailable on failures.
- Concurrency reconstructed from one machine is a lower bound. Other machines and other tenants are invisible.

## Repo layout

| path | what |
|---|---|
| `luna_doctor/cli.py` | the CLI, the log readers and the analyser |
| `luna_doctor/_fix_block.py` | the `fix` command, kept apart so you can read its snippets on their own and make sure that they are right |
| `luna_doctor/_chart_block.py` | the `chart` command |
| `luna_doctor/report.py` | the report model and its `plain` renderer |
| `luna_doctor/render_pretty.py` | the decorated renderer over the same report |
| `luna_doctor/style.py` | the single decoration gate: color, emoji, motion |
| `luna_doctor/scene.py` | the chart's shape list, and the SVG renderer over it |
| `luna_doctor/png.py` | the raster renderer over the same scene |
| `luna_doctor/font.py` | the hand-drawn 5x7 bitmap font and its ASCII folding |
| `bin/luna-doctor` | launcher for the no-packaging path. It resolves its own symlink and imports the package |
| `skills/luna-doctor/` | the agent skill: a router, five playbooks, four catalogues. It lives at `luna_doctor/skills/luna-doctor/` so a wheel carries it, and `skills/luna-doctor` is a symlink to it |
| `assets/` | the two chart images above, each one written by `luna-doctor chart`, and the social preview, written by `python3 scripts/titlecard.py --moon <screenshot>` |

The package ships `luna_doctor/`, which is the CLI, its sibling modules and the
agent skill.
`cli` loads `_fix_block` and `_chart_block` off disk from its own directory, so
a partial install is the one thing that breaks `fix` and `chart`. The
launcher in `bin/` and the chart images stay in the repository.

## How this project is built

This project is developed using a Greenroom. The Greenroom is this public repo beside a private one that holds the design docs, drafts and review notes. That way, work can be shaped before it takes the stage. If you want to read the private greenroom repo, reach out and ask.

Greenroom is a skill you can use on your own projects:

- how it works: [Announcing Greenroom](https://jesserobbins.com/blog/greenroom/)
- the skill: [github.com/jesserobbins/greenroom](https://github.com/jesserobbins/greenroom), `npx skills add jesserobbins/greenroom`

## Versioning

Semantic versioning with prerelease identifiers (`0.1.1-alpha.1`, `0.1.1-beta.1`, …), single source of truth:

| what | where |
|---|---|
| version number | `VERSION`, mirrored in `luna_doctor/cli.py` |
| history | `CHANGELOG.md` (Keep a Changelog) |
| releases | annotated git tags, `vX.Y.Z` and `vX.Y.Z-prerelease` (e.g. `v0.1.1-alpha.1`), published to PyPI by `scripts/publish.sh` |
| JSON document shape | `"schema": 5` in every `--json` envelope: bumped independently when the shape changes |
| measured gateway facts | `"contract": {...}` in every `--json` envelope |

Prereleases publish to PyPI. The index keeps every uploaded file forever, and a bad one is
yanked rather than deleted, so pin an exact version in automation.

Publishing needs a PyPI token in `PYPI_TOKEN`, or in `~/.config/luna-doctor/pypi-token`.
`scripts/publish.sh` builds the tagged release, uploads it and reads the index back to
confirm that the version is live. It publishes nothing without a tag and a clean tree.

Every `--json` document carries `schema`, `version`, and `command`. The reporting commands (`discover`, `analyze`, `fix`, `chart`, `capacity`) also carry `contract`, so a saved report stays readable after the tool moves on. While the project is prerelease, `schema` is bumped freely, because the envelope has no stable consumers to protect yet. Usage errors print to stderr and exit `2` with no JSON document.

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
