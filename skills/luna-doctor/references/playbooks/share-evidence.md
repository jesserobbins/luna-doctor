# Share the evidence: a block to paste, or a live view

A user wants to show someone else what happened: LunaRoute support, a colleague, an
issue tracker. `analyze --for-issue` prints one redacted block to paste. `watch`
shows the same pressure live. Neither diagnoses anything.

## The block: `analyze --for-issue`

It prints one block instead of the report, never beside it. `--json` suppresses
it, `--style` does not decorate it, and `--copy` copies it.

```
luna-doctor 0.2.0-alpha.3 (log schema 4)
window: 2026-10-26T12:00:00Z .. 2026-10-27T12:00:00Z
stores: 1 (claude-code:default, pi:default)
verdict: your org (the account) - <the sentence that explains the verdict>
<the headline>
requests: 1661
next: <the first line the report would offer next>
```

The order is fixed: tool and version, the window, the stores, the verdict when
there is one, the headline, then `label: value` rows. The rows are the report's
own, minus `the wall you hit` and `why`, which the verdict already says. Up to
three closing lines follow, each relabelled `next`. The window is UTC, so the
report can be dated, and the stores name what was read. Every line is ASCII,
inside 100 columns.

### What it removes, and what it keeps

- home paths, in both spellings of the same home, plus generic `/Users/<name>` and
  `/home/<name>`: each becomes `~`. The path `~/GitHub/repo` keeps its shape and
  loses only whose machine it was on.
- email addresses: `<redacted:email>`.
- runs of 24 or more characters of letters, digits, `_` and `-`, which is what an
  API key looks like: `<redacted:token>`. An all-caps name with no digit survives,
  because the outcome ids and the configuration keys are evidence.

Kept: dates, ISO timestamps, durations, counts and model ids. They date and
reproduce the report and name nobody. Read the block before you paste it, and let
the user read it. Paste the block, not a screenshot: a screenshot cannot be searched
or quoted, and it takes whatever else was on the screen with it. In a public thread
ask for `--for-issue`, not `--json`: the JSON is not redacted.

### Where it is still imperfect

Two known gaps, both confirmed against the code at `149415c` on 2026-10-27:

- The `stores:` count and the list beside it can disagree. The whole store
  identity is passed as one joined string (`cli.py:4137`), so a default run
  prints `stores: 1 (claude-code:default, pi:default)` (`bundle.py:191` counts
  the elements). Name the stores from the list, never from the count.
- Redaction is a set of patterns, not a classifier. It removes home paths,
  email addresses and token-shaped runs, and it is written to survive text that
  has already been through `error_signature()`. Anything else that identifies a
  machine or a person can survive it. Read the block, and let the user read it,
  before it leaves the machine.

## The live view: `luna-doctor watch`

`watch` samples the last 15 minutes on a fixed interval and prints one line for
each change of state. Left alone it runs until `Ctrl-C`. The first sample
always prints.

A line gives the refusals this sample saw: `blocked at admission`,
`rate limited`, `silent stall(s)`, `refused at the account limit`. It also gives
`(was N)` beside a count that just moved, the peak this machine held, and the
account's occupancy when a fresh capacity sample covers that moment.

- A terminal gets a fixed-height frame that redraws. A pipe gets plain ASCII
  lines with no cursor control, so a redirect keeps them.
- `--count N` stops after N samples. `--once` prints one. `--interval SECONDS`
  sets the gap (10 by default). `--since` and `--now` set the window.
- It reads this machine's logs, sends nothing anywhere and writes no file.
- `--json` writes one object per sample on stdout, banner and summary on stderr.

`Ctrl-C` prints three summary lines: how long and how many samples, the peak and
the refusals, the account's occupancy. It then exits with the verdict of the
last sample: `1` when it found pressure, `0` when it did not, `2` for a bad
`--since`.

## What to say in a support thread

Ask for the `--for-issue` block as text, inside a fenced code block. Ask also
for the command they ran, with `--since`, `--harness` and `--plan` if they used
them. Ask for the UTC time it happened, and the harness and version they run.

When it is happening now, ask for `watch` and the last line it printed. A still
report of a live incident is a photograph of a moving thing. Say what the block
is before you ask: one redacted block, read from local logs, with no prompts and
no responses. A reporter who knows that hands over the right artifact the first
time.

## How this playbook ends

The artifact to paste is the `--for-issue` block, or the `watch` lines and their
summary when the incident is live. It carries the labelled numbers and the verdict,
and it does not carry prompts, responses, or a single raw log line.
